import json
import tempfile
import threading
import time
import unittest
import os
import socket
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from manager.claude_launcher import ClaudeLauncher
from manager.codex_launcher import CodexLauncher, process_creation_identity
from manager.command_watcher import (
    CLAIM_TIMEOUT_SECONDS, MAX_COMMANDS_PER_POLL, MAX_WAITING_QUOTA_PROMOTIONS_PER_POLL, PHASE_1_TIME_BUDGET_SECONDS,
    POLL_TIME_BUDGET_SECONDS, PROVIDER_RUNTIMES,
    REQUIRED_TASK_POLICIES, _provider_state, _enumerate_waiting_quota_tasks, _promote_waiting_quota_task,
    _reconcile_active,
    _prioritized_commands, claude_quota_reliable, codex_quota_reliable, embedded_ingress_enabled, load_allowlist,
    poll_once, process_command, provider_quota_reliable, resolve_provider_runtime, _spawn_claimed_worker,
)
from manager.execution_lifecycle import enter_running_gate
from manager.executions import execution_health, heartbeat_execution, reserve_execution
from manager.scheduler_provenance import command_origin
from manager.task_claims import TaskClaimConflict
from manager.tasks import TaskError, create_project, create_task, now_iso, validate
from manager.test_dispatcher import quota as fresh_quota_fixture
from manager.trusted_ingress import ADMISSION_VERSION_V2_REPO_WRITE, REQUIRED_REPO_WRITE_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN
from manager.test_execution_lifecycle import project, task
from manager.test_execution_lifecycle import quota_document
from manager.test_execution_runner import AccountAwareClaudeStyleLauncher
from manager.test_task_claims import MemoryClaimRegistry


class Store:
    def __init__(self): self.records = {}; self.fail_command_terminal = False
    def put(self, area, project_id, name, document):
        if area == "commands" and document.get("status") in ("completed", "failed") and self.fail_command_terminal:
            raise TaskError("Drive unavailable")
        self.records[(area, project_id, name)] = deepcopy(document); return document
    def get(self, area, project_id, name):
        try: return deepcopy(self.records[(area, project_id, name)])
        except KeyError: raise TaskError("not found") from None
    def list_projects(self): return [self.get("projects", "p1", "p1")]
    def list_records(self, area, project_id):
        return [deepcopy(value) for (record_area, project, _), value in self.records.items() if record_area == area and project == project_id]
    def project_folder(self, area, project_id, create=True):
        # Real DriveRecords.project_folder() resolves (and, when create is
        # True, lazily materializes) the on-disk folder an area's records
        # live under; manager.executions.list_executions() calls this with
        # create=False purely to detect "no folder yet at all" and
        # short-circuit to []. This in-memory double has no folder concept
        # to fail to find, so it always "succeeds" -- list_records() below
        # already independently returns [] for an empty area regardless.
        return f"{area}/{project_id}"
    def latest(self, area, project_id, task_id):
        items = [value for (record_area, project, _), value in self.records.items()
                 if record_area == area and project == project_id and value.get("task_id") == task_id]
        if not items: raise TaskError("no handoff")
        return max(items, key=lambda item: item["created_at"])


# _on_execution_running() (AUTO_OPEN_ADM) now calls the REAL
# manager.open_existing_adm_ui.focus_existing_adm_ui() -- real Win32
# EnumWindows/SetForegroundWindow/subprocess calls -- every time a test's
# fake launch_task() invokes its on_running callback, which is most tests
# in this module. None of those tests are about AUTO_OPEN_ADM itself, so a
# module-wide default patch keeps them from ever touching this machine's
# real interactive desktop; the two tests that DO exercise AUTO_OPEN_ADM
# apply their own local `patch(...)` on top, which safely shadows this one
# for the duration of their `with` block only.
_focus_existing_adm_ui_patch = patch("manager.command_watcher.focus_existing_adm_ui",
                                     Mock(return_value={"status": "completed", "window_title": "ADM Unified Operations Dashboard"}))


def setUpModule():
    _focus_existing_adm_ui_patch.start()


def tearDownModule():
    _focus_existing_adm_ui_patch.stop()


def command(**changes):
    value = {
        "command_id": "cmd-1", "project_id": "p1", "task_id": "t1", "provider": "codex",
        "model": None, "fallback_model": None, "mode": None, "effort": None,
        "selection_reason": [], "quota_evidence": None, "created_at": "2026-08-14T00:00:00Z",
        "status": "queued", "execution_id": None, "claimed_at": None, "completed_at": None, "result": None,
    }
    value.update(changes); return value


class CommandWatcherTests(unittest.TestCase):
    ALLOWLIST = frozenset({("p1", "t1")})

    @staticmethod
    def allowlist_compliant_store():
        store = Store(); create_project(store, project()); create_task(store, task(read_only=True), assign=False)
        compliant = store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        store.put("tasks", "p1", "t1", compliant)
        return store

    def setUp(self):
        self.store = self.allowlist_compliant_store()

    @staticmethod
    def complete(execution_id):
        return {"terminal": {"execution": {"status": "completed"}}, "session": {"session_id": "codex:read-only"}, "execution_id": execution_id,
                "dispatch": {"provider": "codex", "model": None, "fallback_model": None, "mode": "code", "effort": "medium", "selection_reason": ["fresh quota"], "quota_evidence": {"codex": {"freshness": "fresh"}}}}

    @staticmethod
    def claim_factory(*_args): return object()

    @staticmethod
    def iso(value): return value.isoformat().replace("+00:00", "Z")

    def running_command(self, heartbeat_minutes=0, started_minutes=1, pid=None, legacy=False, provider="codex"):
        now = datetime.now(timezone.utc)
        started = self.iso(now - timedelta(minutes=started_minutes))
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", provider, {"decision": "fresh"})
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", "command-cmd-1", provider,
                               "read_only", started_at=started, task_claim_registry=claim)
        execution = self.store.get("executions", "p1", "command-cmd-1")
        execution["heartbeat_at"] = self.iso(now - timedelta(minutes=heartbeat_minutes))
        execution["progress_updated_at"] = execution["heartbeat_at"]
        provider_pid = pid or os.getpid()
        execution["provider_evidence"] = {
            "host": socket.gethostname()[:100], "pid": provider_pid,
            "creation_identity": process_creation_identity(provider_pid) or "test-process:missing",
            "started_at": started,
        }
        execution["last_provider_event"] = "provider_wait"
        if legacy:
            for key in ("heartbeat_at", "progress_updated_at", "provider_evidence", "last_provider_event", "hard_timeout_at"):
                execution[key] = None
        self.store.put("executions", "p1", "command-cmd-1", execution)
        active = command(status="running", execution_id="command-cmd-1", claimed_at=started, provider=provider)
        self.store.put("commands", "p1", "cmd-1", active)
        return active, claim, execution

    def test_duplicate_polling_runs_once_and_persists_terminal_result(self):
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            self.store.put("commands", "p1", "cmd-1", command())
            first = poll_once(self.store, object(), allowlist=self.ALLOWLIST, claim_factory=self.claim_factory, health_check=lambda: True, quota_check=lambda service: True)
            second = poll_once(self.store, object(), allowlist=self.ALLOWLIST, claim_factory=self.claim_factory, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("completed", first[0]["status"]); self.assertEqual([], second); runner.assert_called_once()
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("command-cmd-1", stored["execution_id"]); self.assertEqual("completed", stored["result"]["status"])
        self.assertEqual("code", stored["mode"]); self.assertEqual(["fresh quota"], stored["selection_reason"])

    def test_scheduled_origin_claim_validates_and_persists_through_watcher_write_path(self):
        context = {
            "scheduler_invocation_id": "a" * 32,
            "wrapper_pid": 41,
            "wrapper_parent_pid": 111,
            "wrapper_creation_identity": "windows-filetime:123456789",
            "os_scheduler_evidence": {
                "status": "PASS", "reason": "event_129_pid_and_instance_link",
                "task_name": "AI Development Manager - Command Watcher", "instance_id": "instance-1",
                "trigger_event_record_id": 10, "trigger_event_id": 107,
                "trigger_time": "2026-08-25T00:00:00Z", "action_event_record_id": 12,
                "action_process_id": 111, "action_executable": "wscript.exe",
                "trigger_origin": "scheduled_time", "ignore_new_events": [],
            },
        }
        expected = command_origin(context)
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST,
                health_check=lambda: True, quota_check=lambda _service: True, origin_context=context,
            )
        self.assertEqual("completed", result["status"])
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual(expected, stored["process_provenance"])
        self.assertEqual(expected, runner.call_args.kwargs["provenance"])
        validate("command", stored)

    def test_process_provenance_legacy_shapes_remain_valid(self):
        validate("command", command())
        validate("command", command(process_provenance={
            "caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32,
        }))

    def test_unknown_and_fail_os_scheduler_evidence_remain_valid(self):
        for status, reason in (("UNKNOWN", "windows_operational_log_unavailable"),
                               ("FAIL", "wrapper_creation_identity_mismatch")):
            with self.subTest(status=status):
                validate("command", command(process_provenance={
                    "caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32,
                    "wrapper_pid": 41, "wrapper_creation_identity": "wrapper-41",
                    "os_scheduler_evidence": {"status": status, "reason": reason},
                }))

    def test_process_provenance_schema_rejects_malformed_evidence(self):
        provenance = {
            "caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32,
            "wrapper_pid": 41, "wrapper_parent_pid": 111, "wrapper_creation_identity": "wrapper-41",
            "os_scheduler_evidence": {
                "status": "PASS", "reason": "event_129_pid_and_instance_link",
                "task_name": "AI Development Manager - Command Watcher", "instance_id": "instance-1",
                "trigger_event_record_id": 10, "trigger_event_id": 107,
                "trigger_time": "2026-08-25T00:00:00Z", "action_event_record_id": 12,
                "action_process_id": 41, "action_executable": "powershell.exe",
                "trigger_origin": "scheduled_time", "ignore_new_events": [],
            },
        }
        cases = {
            "wrapper_pid_string": {**provenance, "wrapper_pid": "41"},
            "wrapper_pid_nonpositive": {**provenance, "wrapper_pid": 0},
            "wrapper_parent_pid_string": {**provenance, "wrapper_parent_pid": "111"},
            "wrapper_parent_pid_nonpositive": {**provenance, "wrapper_parent_pid": 0},
            "invalid_status": {**provenance, "os_scheduler_evidence": {**provenance["os_scheduler_evidence"], "status": "MAYBE"}},
            "invalid_trigger_origin": {**provenance, "os_scheduler_evidence": {**provenance["os_scheduler_evidence"], "trigger_origin": "manual"}},
            "unexpected_field": {**provenance, "unexpected": True},
            "malformed_ignore_new": {**provenance, "os_scheduler_evidence": {**provenance["os_scheduler_evidence"], "ignore_new_events": [{"reason": "already_running"}]}},
            "event_record_id_string": {**provenance, "os_scheduler_evidence": {**provenance["os_scheduler_evidence"], "trigger_event_record_id": "10"}},
            "action_pid_string": {**provenance, "os_scheduler_evidence": {**provenance["os_scheduler_evidence"], "action_process_id": "41"}},
        }
        for name, value in cases.items():
            with self.subTest(name=name), self.assertRaises(TaskError):
                validate("command", command(process_provenance=value))

    def test_restart_never_relaunches_claimed_or_running_command(self):
        claimed = command(status="claimed", execution_id="command-cmd-1", claimed_at=now_iso())
        runner = Mock()
        result = process_command(self.store, object(), claimed, launcher_factory=runner)
        self.assertEqual("claimed", result["status"]); runner.assert_not_called()

    def test_command_becomes_running_only_after_running_gate_callback(self):
        states = []
        def runner(*args, **kwargs):
            states.append(self.store.get("commands", "p1", "cmd-1")["status"])
            kwargs["on_running"](None)
            states.append(self.store.get("commands", "p1", "cmd-1")["status"])
            return self.complete(args[7])
        with patch("manager.command_watcher.launch_task", side_effect=runner):
            process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual(["claimed", "running"], states)

    def test_orphaned_claim_times_out_without_relaunch(self):
        claimed = command(status="claimed", execution_id="command-cmd-1", claimed_at="2000-01-01T00:00:00Z")
        runner = Mock()
        result = process_command(self.store, object(), claimed, launcher_factory=runner)
        # With no observable GCS claim backend, an expired claim is UNKNOWN,
        # not proof that the task is safe to terminalize.
        self.assertEqual("attention", result["status"]); runner.assert_not_called()
        self.assertEqual("execution_record_missing_claim_state_unknown", self.store.get("commands", "p1", "cmd-1")["recovery_reason"])

    def test_terminal_execution_without_cleanup_is_not_published_as_command_terminal(self):
        active, claim, execution = self.running_command(pid=os.getpid())
        execution.update(status="completed", completed_at=now_iso(), finished_at=now_iso(),
                         elapsed_minutes=1, quota_after={}, quota_delta={},
                         terminal_reason="completed",
                         cleanup_evidence={"provider_outcome": "completed", "persistence": "complete",
                                           "persisted": ["execution", "handoff", "task"],
                                           "task_claim_release": "retained", "writer_release": "not_required"})
        self.store.put("executions", "p1", "command-cmd-1", execution)
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertEqual("attention", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertIsNotNone(claim.document)

    def test_malformed_command_fails_closed_without_launch(self):
        runner = Mock()
        result = process_command(self.store, object(), {"command_id": "bad"}, launcher_factory=runner)
        self.assertEqual({"status": "rejected"}, result); runner.assert_not_called()

    def test_missing_governance_inheritance_hard_rejects_before_launch(self):
        task_record = self.store.get("tasks", "p1", "t1")
        del task_record["governance"]
        self.store.put("tasks", "p1", "t1", task_record)
        runner = Mock()
        result = process_command(
            self.store, object(), command(), launcher_factory=runner,
            allowlist=self.ALLOWLIST, health_check=lambda: True,
            quota_check=lambda _service: True,
        )
        self.assertEqual({"status": "rejected", "reason": "mandatory_governance_missing_or_stale"}, result)
        runner.assert_not_called()

    def test_task_claim_collision_and_missing_writer_authority_do_not_launch(self):
        collision = Mock(side_effect=TaskClaimConflict("already claimed"))
        with patch("manager.command_watcher.launch_task", collision):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"]); collision.assert_called_once()
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("TaskClaimConflict", stored["result"]["error_kind"])

        writable = create_task(self.store, task(read_only=False), assign=False, persist=False)
        self.store.put("tasks", "p1", "t1", writable)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(command_id="cmd-2"), claim_factory=self.claim_factory, writer_factory=Mock(side_effect=TaskError("no writer authority")), allowlist=self.ALLOWLIST)
        self.assertEqual("attention", result["status"]); launch.assert_not_called()

    def test_provider_failure_terminalizes_and_drive_failure_never_fakes_completed(self):
        failed = Mock(side_effect=TaskError("provider launch failed"))
        with patch("manager.command_watcher.launch_task", failed):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"])
        # Truth-contract: launch_task() raised before any Execution record was
        # ever created (no reserve_execution() call happened), so the Task
        # must never be silently left "ready"/"Not started" while its Command
        # is terminal "failed" -- same blocked contract _block_prelaunch_task
        # already enforces for a reserved-then-cancelled Execution.
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])
        self.assertIn("Execution did not start", self.store.get("tasks", "p1", "t1")["current_progress"])
        self.assertEqual("TaskError", self.store.get("commands", "p1", "cmd-1")["result"]["error_kind"])
        self.assertNotIn("executions", {area for area, project_id, name in self.store.records})

        self.store = self.allowlist_compliant_store()
        self.store.fail_command_terminal = True
        with patch("manager.command_watcher.launch_task", Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])):
            with self.assertRaisesRegex(TaskError, "Drive unavailable"):
                process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("running", self.store.get("commands", "p1", "cmd-1")["status"])

    def test_prelaunch_working_directory_failure_blocks_task_without_leaking_raw_text(self):
        # Real-world reproduction: launch_task() -> execution_runner.
        # _resolve_working_directory() raises before reserve_execution() is
        # ever reached, so no Execution record exists at all -- distinct
        # from test_prelaunch_reservation_is_cancelled_and_not_left_running
        # (which reserves one first). error_kind classification is
        # unchanged from before this fix (bare exception class name) --
        # never the raw exception message, which could carry an absolute
        # filesystem path.
        failed = Mock(side_effect=TaskError("working_directory does not exist or is not a directory: 'X'"))
        with patch("manager.command_watcher.launch_task", failed):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"])
        stored_command = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("TaskError", stored_command["result"]["error_kind"])
        stored_task = self.store.get("tasks", "p1", "t1")
        self.assertEqual("blocked", stored_task["status"])
        self.assertNotIn("'X'", stored_task["blocked_reason"])
        self.assertNotIn("executions", {area for area, project_id, name in self.store.records})

    def test_prelaunch_generic_exception_still_blocks_task_truthfully(self):
        # Not a TaskError at all -- must still block the Task (contract
        # requirement 2) with the same bounded classification, never raw
        # exception text.
        failed = Mock(side_effect=RuntimeError("unexpected boom"))
        with patch("manager.command_watcher.launch_task", failed):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"])
        self.assertEqual("RuntimeError", self.store.get("commands", "p1", "cmd-1")["result"]["error_kind"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_reserved_execution_prelaunch_failure_is_not_reclassified_as_no_execution(self):
        # AG adversarial review constraint: the no-Execution _block_prelaunch_
        # task() branch must never fire when an Execution record actually
        # exists (reserved/running) -- that authority belongs entirely to
        # the existing _reconcile_active()/cancel_reserved_execution()
        # contract (see test_prelaunch_reservation_is_cancelled_and_not_
        # left_running), which this test proves is untouched by this fix.
        def reserve_then_fail(*args, **kwargs):
            reserve_execution(self.store, "p1", "t1", args[7], "codex", {"decision": "fresh"})
            raise TaskError("preflight failed")
        with patch("manager.command_watcher.launch_task", side_effect=reserve_then_fail):
            result = process_command(self.store, object(), command(), claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"])
        self.assertEqual("cancelled", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])
        self.assertEqual("prelaunch_failed", self.store.get("commands", "p1", "cmd-1")["result"]["error_kind"])

    def test_health_contract_distinguishes_healthy_stale_and_over_expected(self):
        _, _, healthy = self.running_command()
        self.assertEqual("healthy", execution_health(healthy)["state"])

        healthy["started_at"] = self.iso(datetime.now(timezone.utc) - timedelta(minutes=20))
        healthy["progress_updated_at"] = self.iso(datetime.now(timezone.utc) - timedelta(minutes=16))
        self.assertEqual("provider_progress_stale", execution_health(healthy)["reason"])

        healthy["started_at"] = self.iso(datetime.now(timezone.utc) - timedelta(minutes=21))
        healthy["heartbeat_at"] = now_iso()
        healthy["progress_updated_at"] = healthy["heartbeat_at"]
        healthy["hard_timeout_at"] = self.iso(datetime.now(timezone.utc) + timedelta(minutes=39))
        result = execution_health(healthy)
        self.assertEqual("healthy", result["state"]); self.assertTrue(result["over_expected"])

    def test_provider_wait_without_progress_cannot_keep_execution_healthy(self):
        active, claim, execution = self.running_command(started_minutes=20)
        stale_progress = self.iso(datetime.now(timezone.utc) - timedelta(minutes=16))
        execution["progress_updated_at"] = stale_progress
        self.store.put("executions", "p1", "command-cmd-1", execution)
        heartbeat_execution(self.store, "p1", "command-cmd-1", "provider_wait", progress=False)
        refreshed = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual(stale_progress, refreshed["progress_updated_at"])
        self.assertEqual("provider_progress_stale", execution_health(refreshed)["reason"])
        self.assertEqual("attention", process_command(
            self.store, object(), active, claim_factory=lambda *_: claim)["status"])

    def test_same_pid_with_different_creation_identity_is_not_live(self):
        active, claim, execution = self.running_command(pid=os.getpid())
        execution["provider_evidence"]["creation_identity"] = "original-process"
        self.store.put("executions", "p1", "command-cmd-1", execution)
        with patch("manager.codex_launcher.process_creation_identity", return_value="reused-process"):
            self.assertEqual("replaced", _provider_state(execution))
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertEqual("command-cmd-1", claim.document["execution_id"])

    def test_stale_live_provider_is_attention_and_never_reclaimed(self):
        active, claim, _ = self.running_command(heartbeat_minutes=16, pid=os.getpid())
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertEqual("command-cmd-1", claim.document["execution_id"])
        self.assertEqual(1, claim.generation)
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_proven_dead_read_only_provider_terminalizes_and_writes_command_task(self):
        active, claim, _ = self.running_command(heartbeat_minutes=16, pid=99_999_999)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("failed", result["status"])
        self.assertIsNotNone(claim.document)
        self.assertFalse(claim.document["authority_active"])
        self.assertEqual("released", claim.document["cleanup"]["status"])
        self.assertEqual("interrupted", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("failed", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_proven_dead_writer_releases_exact_linked_lease_before_terminalizing(self):
        active, claim, execution = self.running_command(heartbeat_minutes=16, pid=99_999_999)
        execution.update(
            access="production_write", session_id="codex:session-a", provider_session_id="session-a",
            lease_evidence={
                "authority": "acquired", "lock_id": "repo-" + "0" * 64, "generation": 7,
                "repository": "github:ne9221/ai-development-manager", "branch": "refs/heads/main",
                "scope": ["manager/executions.py"], "baseline_head": "0" * 40,
            },
        )
        self.store.put("executions", "p1", "command-cmd-1", execution)
        registry = object()
        with patch("manager.command_watcher.GCSLockRegistry.from_environment", return_value=registry), \
             patch("manager.command_watcher.reconcile_stopped_provider_terminal_lease") as release_linked, \
             patch("manager.command_watcher.terminalize_execution") as terminalize:
            result = _reconcile_active(self.store, object(), active, lambda *_: claim)
        release_linked.assert_called_once_with(
            registry, execution["lease_evidence"]["lock_id"], "p1", "t1", "command-cmd-1", "codex",
            7, "codex:session-a", True,
        )
        terminalize.assert_called_once()
        self.assertEqual("attention", result["status"])

    # Phase 4E parity gate item: process identity / PID reuse safety and the
    # full stale-provider auto-recovery path, reproduced exactly for
    # provider="claude" -- byte-identical scenario to the Codex test above,
    # just with the provider substituted, proving the recovery path carries
    # no Codex-only assumption.
    def test_proven_dead_read_only_claude_provider_terminalizes_and_writes_command_task(self):
        active, claim, _ = self.running_command(heartbeat_minutes=16, pid=99_999_999, provider="claude")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("failed", result["status"])
        self.assertIsNotNone(claim.document)
        self.assertFalse(claim.document["authority_active"])
        self.assertEqual("released", claim.document["cleanup"]["status"])
        execution = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual("interrupted", execution["status"])
        self.assertEqual("claude", execution["provider"])
        self.assertEqual("failed", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_same_pid_with_different_creation_identity_is_not_live_for_claude_provider(self):
        # PID-reuse safety is implemented once in process_identity_state()
        # (shared by both providers) -- this proves a Claude execution gets
        # the same "replaced" fail-closed treatment a Codex one already does.
        active, claim, execution = self.running_command(pid=os.getpid(), provider="claude")
        execution["provider_evidence"]["creation_identity"] = "impersonated-identity"
        self.store.put("executions", "p1", "command-cmd-1", execution)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertIsNotNone(claim.document)  # never released against an unverified/impersonated process

    def test_legacy_uncertain_execution_and_claim_are_not_mutated(self):
        active, claim, execution = self.running_command(heartbeat_minutes=99, started_minutes=999, legacy=True)
        before_claim = deepcopy(claim.document); before_generation = claim.generation
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertEqual(execution, self.store.get("executions", "p1", "command-cmd-1"))
        self.assertEqual(before_claim, claim.document); self.assertEqual(before_generation, claim.generation)

    def test_prelaunch_reservation_is_cancelled_and_not_left_running(self):
        def reserve_then_fail(*args, **kwargs):
            reserve_execution(self.store, "p1", "t1", args[7], "codex", {"decision": "fresh"})
            raise TaskError("preflight failed")
        with patch("manager.command_watcher.launch_task", side_effect=reserve_then_fail):
            result = process_command(self.store, object(), command(), claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"])
        self.assertEqual("cancelled", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_no_allowlist_means_zero_launch(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory)
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

    def test_other_project_or_task_not_in_allowlist_means_zero_launch(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        unrelated = frozenset({("other-project", "t1"), ("p1", "other-task")})
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=unrelated)
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

    def test_allowlisted_but_policy_violation_means_zero_launch(self):
        incomplete = self.store.get("tasks", "p1", "t1")
        incomplete["execution_policies"] = ["disposable", "read_only"]  # missing no_repo_writes/no_external_writes
        self.store.put("tasks", "p1", "t1", incomplete)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST)
        self.assertEqual("attention", result["status"]); launch.assert_not_called()
        self.assertEqual("allowlisted_task_policy_not_satisfied", self.store.get("commands", "p1", "cmd-1")["recovery_reason"])

    def test_allowlisted_disposable_read_only_is_eligible(self):
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_normal_dispatch_reaching_running_auto_opens_the_dashboard(self):
        """AUTO_OPEN_ADM: a completely ordinary dispatch (no
        OPEN_EXISTING_ADM_UI action at all) that genuinely reaches
        "running" must, as a side effect, call the same user-visible
        focus-or-launch-or-noop path -- so the Dashboard shows up on the
        user's desktop for ANY real dispatch, not only one that explicitly
        asked to open it."""
        focus = Mock(return_value={"status": "completed", "window_title": "ADM Unified Operations Dashboard"})
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner), patch("manager.command_watcher.focus_existing_adm_ui", focus):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        focus.assert_called_once_with()
        self.assertEqual("completed", result["status"])

    def test_auto_open_dashboard_failure_never_blocks_or_fails_dispatch(self):
        """A real dispatch's success must never depend on whether the
        interactive desktop happened to be visible/available -- a failed or
        raising focus_existing_adm_ui() is only ever logged (see
        _on_execution_running), never allowed to change the Command/Task's
        own terminal outcome."""
        for focus in (
            Mock(return_value={"status": "failed", "error_kind": "no_interactive_desktop"}),
            Mock(side_effect=RuntimeError("unexpected desktop error")),
        ):
            with self.subTest(focus=focus):
                runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
                with patch("manager.command_watcher.launch_task", runner), patch("manager.command_watcher.focus_existing_adm_ui", focus):
                    result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
                self.assertEqual("completed", result["status"])

    def test_open_existing_adm_ui_is_governed_and_does_not_launch_codex(self):
        focus = Mock(return_value={"status": "completed", "window_title": "ADM Unified Operations Dashboard"})
        launch = Mock()
        with patch("manager.command_watcher.focus_existing_adm_ui", focus), patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(action="OPEN_EXISTING_ADM_UI"),
                                     claim_factory=self.claim_factory, allowlist=self.ALLOWLIST,
                                     health_check=lambda: False, quota_check=lambda service: False)
        self.assertEqual("completed", result["status"])
        focus.assert_called_once_with()
        launch.assert_not_called()
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("completed", stored["result"]["status"])

    def test_open_existing_adm_ui_failure_is_persisted_closed(self):
        focus = Mock(return_value={"status": "failed", "error_kind": "adm_dashboard_not_running"})
        with patch("manager.command_watcher.focus_existing_adm_ui", focus):
            result = process_command(self.store, object(), command(action="OPEN_EXISTING_ADM_UI"),
                                     claim_factory=self.claim_factory, allowlist=self.ALLOWLIST)
        self.assertEqual({"status": "failed", "execution_id": "command-cmd-1", "error_kind": "adm_dashboard_not_running"}, result)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertEqual("adm_dashboard_not_running", stored["result"]["error_kind"])

    def test_session_center_unhealthy_blocks_new_launch_but_never_touches_running_authority(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: False)
        self.assertEqual({"status": "rejected", "reason": "session_center_unavailable"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

        # An already-running execution must never be touched by health status.
        active, claim, _ = self.running_command(heartbeat_minutes=0)
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim, health_check=lambda: False)
        self.assertEqual("running", result["status"])

    def test_stale_or_unreliable_codex_quota_blocks_new_launch_but_never_touches_running_authority(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory,
                                     allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: False)
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

        # An already-running execution must never be touched by quota status.
        active, claim, _ = self.running_command(heartbeat_minutes=0)
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim, quota_check=lambda service: False)
        self.assertEqual("running", result["status"])

    def test_codex_quota_reliable_fails_closed_on_stale_unknown_or_unreachable(self):
        from manager.command_watcher import codex_quota_reliable
        fresh_reliable = {
            "providers": [{"provider": "codex", "has_reliable_quota": True}],
        }
        stale_or_unknown = {
            "providers": [{"provider": "codex", "has_reliable_quota": False}],
        }
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=fresh_reliable):
            self.assertTrue(codex_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=stale_or_unknown):
            self.assertFalse(codex_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", side_effect=RuntimeError("Drive unavailable")):
            self.assertFalse(codex_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value={"providers": []}):
            self.assertFalse(codex_quota_reliable(object()))  # no codex entry at all

    def test_session_center_healthy_probe_fails_closed_on_any_error(self):
        from manager.command_watcher import session_center_healthy
        import urllib.error
        with patch("manager.command_watcher.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertFalse(session_center_healthy("http://127.0.0.1:8765/health"))
        with patch("manager.command_watcher.urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.status = 500
            self.assertFalse(session_center_healthy("http://127.0.0.1:8765/health"))
        with patch("manager.command_watcher.urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.status = 200
            opened.return_value.__enter__.return_value.read.return_value = b"not json"
            self.assertFalse(session_center_healthy("http://127.0.0.1:8765/health"))
        with patch("manager.command_watcher.urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.status = 200
            opened.return_value.__enter__.return_value.read.return_value = b'{"status":"ok"}'
            self.assertTrue(session_center_healthy("http://127.0.0.1:8765/health"))

    def test_production_write_even_allowlisted_never_launches(self):
        writable = create_task(self.store, task(read_only=False), assign=False, persist=False)
        writable["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)  # everything else compliant except read_only
        self.store.put("tasks", "p1", "t1", writable)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST)
        self.assertEqual("attention", result["status"]); launch.assert_not_called()
        self.assertEqual("allowlisted_task_policy_not_satisfied", self.store.get("commands", "p1", "cmd-1")["recovery_reason"])

    def test_stray_attention_command_is_never_relaunched_even_with_permissive_allowlist(self):
        stray = command(status="attention", stale_at=now_iso(), recovery_reason="legacy")
        self.store.put("commands", "p1", "cmd-1", stray)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), stray, claim_factory=self.claim_factory, allowlist=self.ALLOWLIST)
        launch.assert_not_called()
        self.assertNotEqual("running", result.get("status"))

    def test_load_allowlist_fails_closed_on_missing_or_malformed_config(self):
        self.assertEqual(frozenset(), load_allowlist(None))
        self.assertEqual(frozenset(), load_allowlist("/no/such/file.json"))
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "bad.json"
            malformed.write_text("not json", encoding="utf-8")
            self.assertEqual(frozenset(), load_allowlist(str(malformed)))

            wrong_shape = Path(directory) / "wrong_shape.json"
            wrong_shape.write_text(json.dumps({"entries": "not-a-list"}), encoding="utf-8")
            self.assertEqual(frozenset(), load_allowlist(str(wrong_shape)))

            partial = Path(directory) / "partial.json"
            partial.write_text(json.dumps({"entries": [
                {"project_id": "p1", "task_id": "t1"},
                {"project_id": "p1"},  # missing task_id: dropped, not crashed
                "not-a-dict",
            ]}), encoding="utf-8")
            self.assertEqual(frozenset({("p1", "t1")}), load_allowlist(str(partial)))

    def test_load_allowlist_reads_env_var_when_path_not_given(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "allowlist.json"
            config.write_text(json.dumps({"entries": [{"project_id": "p1", "task_id": "t1"}]}), encoding="utf-8")
            with patch.dict(os.environ, {"ADM_WATCHER_ALLOWLIST_PATH": str(config)}):
                self.assertEqual(frozenset({("p1", "t1")}), load_allowlist())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(frozenset(), load_allowlist())

    def test_production_allowlist_admits_zero_state_bootstrap_smoke(self):
        allowlist = Path(__file__).parents[1] / "templates" / "watcher_allowlist.json"
        self.assertIn(("ai-development-manager", "phase3-zero-state-bootstrap-final-smoke"),
                      load_allowlist(str(allowlist)))

    def test_windows_watcher_task_wires_allowlist_path_to_runtime(self):
        manager = Path(__file__).parent
        installer = (manager / "install_command_watcher.ps1").read_text(encoding="utf-8")
        runner = (manager / "run_command_watcher.ps1").read_text(encoding="utf-8")
        self.assertIn('-AllowlistPath `"$AllowlistPath`"', installer)
        self.assertIn('$env:ADM_WATCHER_ALLOWLIST_PATH = $AllowlistPath', runner)
        self.assertIn('-IngressFolderId `"$IngressFolderId`"', installer)
        self.assertIn('$env:ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID = $IngressFolderId', runner)
        self.assertIn('$env:ADM_DRIVE_DISPATCH_INGRESS_OWNER = $IngressOwner', runner)
        self.assertIn('-ClaudeAccountsConfig `"$ClaudeAccountsConfig`"', installer)
        self.assertIn('$env:CLAUDE_ACCOUNTS_CONFIG = $ClaudeAccountsConfig', runner)

    # -- Phase 4C: provider routing + Claude quota fail-closed wiring --

    # 1 & 2: provider resolves the correct launcher/quota-gate pairing
    def test_codex_provider_resolves_codex_launcher_and_quota_gate(self):
        runtime = resolve_provider_runtime("codex")
        self.assertIs(runtime["launcher_factory"], CodexLauncher)
        self.assertIs(runtime["quota_check"], codex_quota_reliable)

    def test_claude_provider_resolves_claude_launcher_and_quota_gate(self):
        runtime = resolve_provider_runtime("claude")
        self.assertIs(runtime["launcher_factory"], ClaudeLauncher)
        self.assertIs(runtime["quota_check"], claude_quota_reliable)

    # 3: unknown provider fails closed, never falls back to Codex
    def test_unknown_provider_resolves_to_none_never_falls_back_to_codex(self):
        self.assertIsNone(resolve_provider_runtime("gemini_app"))
        self.assertIsNone(resolve_provider_runtime(""))
        self.assertIsNone(resolve_provider_runtime(None))

    def test_unknown_provider_command_is_rejected_without_touching_codex_defaults(self):
        # command.schema.json's provider enum (codex/claude only) already
        # rejects this at validate("command", ...) before dispatch resolution
        # is even reached -- an even earlier fail-closed point than
        # resolve_provider_runtime()'s own None-return, which is exercised
        # directly (and does return None for unknown providers) in
        # test_unknown_provider_resolves_to_none_never_falls_back_to_codex.
        with patch("manager.command_watcher.CodexLauncher") as codex_ctor, \
             patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                self.store, object(), command(provider="gemini_app"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        self.assertEqual({"status": "rejected"}, result)
        codex_ctor.assert_not_called()
        runner.assert_not_called()

    def test_command_schema_now_accepts_claude_provider(self):
        # A regression guard on the schema.provider const->enum widening this
        # phase required: an invalid command must still be rejected up front.
        self.assertEqual({"status": "rejected"}, process_command(self.store, object(), {"command_id": "bad"}))

    # 4 & 5: Claude quota gate blocks before any launcher is even constructed
    def test_claude_stale_or_unknown_quota_blocks_before_prepare(self):
        with patch("manager.command_watcher.ClaudeLauncher") as claude_ctor, \
             patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                self.store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: False,
            )
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        claude_ctor.assert_not_called()
        runner.assert_not_called()

    # 6: reliable fresh Claude quota lets the command reach launch_task with the right launcher/provider
    def test_claude_reliable_quota_allows_mocked_prepare_path(self):
        runner = Mock(return_value=self.complete("exec-claude"))
        with patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("claude", kwargs.get("provider"))
        self.assertEqual("completed", result["status"])

    # -- P0.4: explicit account_id contract (Command > Task > None) --

    REGISTRY = [
        {"account_id": "account-a", "enabled": True, "config_dir": None},
        {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        {"account_id": "account-disabled", "enabled": False, "config_dir": r"C:\accounts\d\.claude"},
    ]

    _NO_OVERRIDE = object()

    def _run_explicit(self, cmd, quota_reliable=True, registry=_NO_OVERRIDE):
        runner = Mock(return_value=self.complete("exec-claude"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry",
                   return_value=self.REGISTRY if registry is self._NO_OVERRIDE else registry):
            result = process_command(
                self.store, object(), cmd,
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: quota_reliable,
            )
        return result, runner

    def test_command_explicit_account_a_threads_through_to_launch_task(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-a", kwargs.get("account_id"))
        self.assertEqual(self.REGISTRY, kwargs.get("claude_accounts"))
        self.assertEqual("completed", result["status"])

    def test_direct_process_command_is_explicitly_marked_unknown(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a"))
        _, kwargs = runner.call_args
        self.assertEqual({"caller_origin": "direct_or_unknown", "scheduler_invocation_id": None},
                         kwargs.get("provenance"))
        self.assertEqual("completed", result["status"])

    def test_watcher_context_threads_one_invocation_id_to_launch(self):
        runner = Mock(return_value=self.complete("exec-claude"))
        context = {"scheduler_invocation_id": "a" * 32}
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY):
            process_command(self.store, object(), command(provider="claude", account_id="account-a"),
                            claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")} ),
                            health_check=lambda: True, quota_check=lambda service: True, origin_context=context)
        self.assertEqual({"caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32},
                         runner.call_args.kwargs["provenance"])

    def test_command_explicit_account_b_threads_through_to_launch_task(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-b"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-b", kwargs.get("account_id"))
        self.assertEqual("completed", result["status"])

    def test_task_explicit_account_id_used_as_fallback_when_command_has_none(self):
        task_record = self.store.get("tasks", "p1", "t1")
        task_record["account_id"] = "account-b"
        self.store.put("tasks", "p1", "t1", task_record)
        result, runner = self._run_explicit(command(provider="claude"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-b", kwargs.get("account_id"))
        self.assertEqual("completed", result["status"])

    def test_provisional_account_with_null_requested_account_reaches_r2_selection(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a", requested_account_id=None))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertIsNone(kwargs.get("account_id"))
        self.assertEqual(self.REGISTRY, kwargs.get("claude_accounts"))
        self.assertEqual("completed", result["status"])

    def test_command_account_id_takes_priority_over_task_account_id(self):
        task_record = self.store.get("tasks", "p1", "t1")
        task_record["account_id"] = "account-b"
        self.store.put("tasks", "p1", "t1", task_record)
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-a", kwargs.get("account_id"))

    def test_unknown_explicit_account_id_rejected(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-ghost"))
        self.assertEqual({"status": "rejected", "reason": "unknown_or_disabled_claude_account"}, result)
        runner.assert_not_called()

    def test_disabled_explicit_account_id_rejected(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-disabled"))
        self.assertEqual({"status": "rejected", "reason": "unknown_or_disabled_claude_account"}, result)
        runner.assert_not_called()

    def test_explicit_account_id_with_no_registry_configured_rejected(self):
        # An explicit account_id with no local registry at all cannot be
        # validated or resolved to a config_dir -- must fail closed, not
        # silently fall through to the single-account default.
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a"), registry=None)
        self.assertEqual({"status": "rejected", "reason": "unknown_or_disabled_claude_account"}, result)
        runner.assert_not_called()

    def test_claude_explicit_valid_account_enforces_quota_gate_when_unreliable(self):
        # Truth Contract Fix: quota_check is enforced even when an explicit,
        # validated account_id is present. Unreliable/exhausted quota fails closed.
        quota_check = Mock(return_value=False)
        runner = Mock(return_value=self.complete("exec-claude"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY):
            result = process_command(
                self.store, object(), command(provider="claude", account_id="account-a"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=quota_check,
            )
        quota_check.assert_called_once()
        runner.assert_not_called()
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)

    def test_claude_explicit_valid_account_launches_when_quota_reliable(self):
        quota_check = Mock(return_value=True)
        runner = Mock(return_value=self.complete("exec-claude"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY):
            result = process_command(
                self.store, object(), command(provider="claude", account_id="account-a"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=quota_check,
            )
        quota_check.assert_called_once()
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_claude_no_explicit_account_and_unreliable_quota_still_rejected(self):
        # Same unreliable-quota condition as above, but with no explicit
        # account_id: the AUTO-selection quota gate still applies unchanged.
        result, runner = self._run_explicit(command(provider="claude"), quota_reliable=False)
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        runner.assert_not_called()

    def test_codex_explicit_account_id_field_is_ignored_quota_gate_unchanged(self):
        # account_id is meaningless for Codex; an unreliable Codex quota must
        # still block exactly as before, even if a (nonsensical) account_id
        # were ever present on the command.
        result, runner = self._run_explicit(command(provider="codex", account_id="account-a"), quota_reliable=False)
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        runner.assert_not_called()

    def test_command_schema_rejects_config_dir_field(self):
        # config_dir must never be persisted to Drive Command/Task records --
        # only the local registry may resolve account_id -> config_dir.
        from manager.tasks import TaskError as _TaskError, validate as _validate
        with self.assertRaises(_TaskError):
            _validate("command", command(provider="claude", account_id="account-a", config_dir=r"C:\accounts\a\.claude"))

    # 8: Claude and Codex quota gates never cross-contaminate on the same document
    def test_claude_and_codex_quota_do_not_cross_contaminate(self):
        mixed = {"providers": [
            {"provider": "codex", "has_reliable_quota": True},
            {"provider": "claude", "has_reliable_quota": False},
        ]}
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=mixed):
            self.assertTrue(codex_quota_reliable(object()))
            self.assertFalse(claude_quota_reliable(object()))

        mixed_reversed = {"providers": [
            {"provider": "codex", "has_reliable_quota": False},
            {"provider": "claude", "has_reliable_quota": True},
        ]}
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=mixed_reversed):
            self.assertFalse(codex_quota_reliable(object()))
            self.assertTrue(claude_quota_reliable(object()))

    def test_claude_quota_reliable_fails_closed_on_stale_unknown_or_unreachable(self):
        fresh_reliable = {"providers": [{"provider": "claude", "has_reliable_quota": True}]}
        stale_or_unknown = {"providers": [{"provider": "claude", "has_reliable_quota": False}]}
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=fresh_reliable):
            self.assertTrue(claude_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=stale_or_unknown):
            self.assertFalse(claude_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", side_effect=RuntimeError("Drive unavailable")):
            self.assertFalse(claude_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value={"providers": []}):
            self.assertFalse(claude_quota_reliable(object()))  # no claude entry at all

    def test_automatic_claude_gate_uses_provider_eligibility_but_explicit_account_stays_scoped(self):
        summary = {"providers": [{"provider": "claude", "has_usable_quota": True}], "accounts": [
            {"provider": "claude", "account_id": "account-a", "has_usable_quota": False},
            {"provider": "claude", "account_id": "account-b", "has_usable_quota": True},
        ]}
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=summary):
            self.assertTrue(claude_quota_reliable(object()))
            self.assertFalse(claude_quota_reliable(object(), account_id="account-a"))

    # 12: existing allowlist/policy gates still run before launch for Claude too
    def test_claude_command_off_allowlist_means_zero_launch(self):
        with patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                self.store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset(),  # empty: nothing allowlisted
                health_check=lambda: True, quota_check=lambda service: True,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        runner.assert_not_called()

    def test_claude_command_policy_violation_still_blocks_before_launch(self):
        store = Store(); create_project(store, project())
        create_task(store, task(read_only=False), assign=False)  # not disposable/read-only
        with patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        self.assertEqual("attention", result["status"])
        runner.assert_not_called()

    def test_explicit_override_still_wins_over_provider_registry(self):
        # The existing single-factory override pattern tests already rely on
        # must keep working even though defaults are now provider-resolved.
        sentinel_launcher = Mock(return_value="sentinel-launcher-instance")
        runner = Mock(return_value=self.complete("exec-override"))
        with patch("manager.command_watcher.launch_task", runner):
            process_command(
                self.store, object(), command(provider="claude"), launcher_factory=sentinel_launcher,
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        runner.assert_called_once()
        args, _ = runner.call_args
        self.assertEqual("sentinel-launcher-instance", args[4])
        sentinel_launcher.assert_called_once()


class DirectDispatchAutoAccountRoutingIntegrationTests(unittest.TestCase):
    """P0 claude-auth-routing-truth R2: reproduces the exact real Direct
    Dispatch integration path end to end (process_command -> the real,
    unmocked launch_task -> resolve_claude_account -> a stub launcher),
    not just resolve_claude_account()/select_claude_account() in isolation.

    The real failure mode: cloud/dispatch_ingress.py stamps
    command.account_id with whatever manager.dispatcher.dispatch()
    automatically recommended even when the caller requested nothing
    (command.requested_account_id stays null in that case). Before this fix,
    manager.command_watcher._explicit_account_id() could not tell that
    apart from a caller's real explicit ask, so it treated the automatic
    recommendation as explicit and launch_task() never got a chance to
    re-check live auth readiness or quota freshness against the current
    local state.
    """

    REGISTRY = [
        {"account_id": "account-a", "enabled": True, "config_dir": None},
        {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
    ]

    class _IntegrationStore(Store):
        """Store, plus project_folder() so the real (unmocked)
        manager.dispatcher.dispatch() -> manager.executions.list_executions()
        this integration test deliberately exercises can run against it --
        list_executions() treats a TaskError here as "no folder yet" and
        returns []."""

        def project_folder(self, area, project_id, create=False):
            raise TaskError("no executions folder in this test double")

        def latest(self, area, project_id, task_id):
            raise TaskError("no handoff in this test double")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lock_home = tempfile.TemporaryDirectory()
        self._lock_home_patch = patch.dict(os.environ, {"AI_MANAGER_HOME": self.lock_home.name})
        self._lock_home_patch.start()
        self.store = self._IntegrationStore()
        create_project(self.store, project())
        create_task(self.store, task(read_only=True, working_directory=self.temp.name), assign=False)
        compliant = self.store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", compliant)

    def tearDown(self):
        self._lock_home_patch.stop()
        self.lock_home.cleanup()
        self.temp.cleanup()

    @staticmethod
    def _fresh_quota_document():
        fresh = now_iso()
        entries = [
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": fresh, "status": "ok",
             "windows": [], "account_id": "account-a"},
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": fresh, "status": "ok",
             "windows": [], "account_id": "account-b"},
        ]
        return {"schema_version": "0.1.0", "generated_at": fresh, "providers": entries}

    @staticmethod
    def _stale_account_a_document():
        stale = "2026-01-01T00:00:00Z"
        fresh = now_iso()
        entries = [
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": stale, "status": "ok",
             "windows": [], "account_id": "account-a"},
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": fresh, "status": "ok",
             "windows": [], "account_id": "account-b"},
        ]
        return {"schema_version": "0.1.0", "generated_at": fresh, "providers": entries}

    def _direct_dispatch_command(self, **changes):
        # The exact real shape: dispatcher already auto-recommended
        # account-a and stamped it onto command.account_id, but the caller's
        # original request never asked for a specific account at all.
        base = command(
            provider="claude", account_id="account-a", requested_account_id=None,
            created_via="direct_dispatch_ingress", admission_version="v1", request_id="req-1",
        )
        base.update(changes)
        return base

    def _run(self, cmd, auth_ready_map, quota_document=None):
        launcher = AccountAwareClaudeStyleLauncher()
        quota_document = quota_document or self._fresh_quota_document()
        with patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY), \
             patch("manager.execution_runner.read_drive_status", return_value=quota_document), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document), \
             patch("manager.executions.read_drive_status", return_value=quota_document), \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_runner._claude_account_auth_ready",
                   side_effect=lambda account: auth_ready_map.get(account["account_id"], False)):
            result = process_command(
                self.store, object(), cmd, launcher_factory=lambda: launcher,
                claim_factory=lambda *_a: MemoryClaimRegistry(),
                allowlist=frozenset({("p1", "t1")}), health_check=lambda: True,
                quota_check=lambda service: True,
            )
        return result, launcher

    def test_provisional_command_account_id_is_not_treated_as_explicit(self):
        # account-a is auth-unready, account-b is auth-ready + fresh quota:
        # real launch selection must be account-b, never the provisional
        # command.account_id=account-a.
        cmd = self._direct_dispatch_command()
        result, launcher = self._run(cmd, {"account-a": False, "account-b": True})
        self.assertEqual("completed", result["status"])
        self.assertEqual(["prepare", "start", "wait", "close"], launcher.events)
        self.assertEqual("account-b", launcher.received_account_id)
        self.assertEqual(r"C:\accounts\b\.claude", launcher.received_config_dir)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("account-b", stored["account_id"])

    def test_auth_ready_but_stale_quota_falls_back_to_fresh_sibling(self):
        cmd = self._direct_dispatch_command()
        result, launcher = self._run(
            cmd, {"account-a": True, "account-b": True}, quota_document=self._stale_account_a_document(),
        )
        self.assertEqual("completed", result["status"])
        self.assertEqual("account-b", launcher.received_account_id)
        self.assertEqual("account-b", self.store.get("commands", "p1", "cmd-1")["account_id"])

    def test_account_a_auth_ready_stale_quota_account_b_auth_unavailable_fails_closed(self):
        cmd = self._direct_dispatch_command()
        result, launcher = self._run(
            cmd, {"account-a": True, "account-b": False}, quota_document=self._stale_account_a_document(),
        )
        self.assertEqual("failed", result["status"])
        self.assertEqual([], launcher.events)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("account-a", stored["account_id"])  # never substituted or overwritten by a failed attempt

    def test_all_accounts_unavailable_fails_closed_before_any_provider_spawn(self):
        cmd = self._direct_dispatch_command()
        result, launcher = self._run(cmd, {"account-a": False, "account-b": False})
        self.assertEqual("failed", result["status"])
        self.assertEqual([], launcher.events)  # no provider process spawned

    def test_caller_explicit_request_never_substituted_even_when_auth_unavailable(self):
        # requested_account_id=account-a is a real caller ask: the new
        # auto-selection auth_ready cross-check must never even be consulted
        # for it (account-a's auth_ready=False here must not cause a silent
        # swap to the healthy account-b) -- explicit requests still rely
        # solely on ClaudeLauncher's own real auth preflight to fail closed,
        # which is covered at the unit level in test_claude_launcher.py; the
        # stub launcher used here does not reproduce that subprocess check,
        # so this integration test asserts the one thing this layer actually
        # controls: no substitution, ever.
        cmd = self._direct_dispatch_command(account_id="account-a", requested_account_id="account-a")
        result, launcher = self._run(cmd, {"account-a": False, "account-b": True})
        self.assertEqual("account-a", launcher.received_account_id)
        self.assertNotEqual("account-b", launcher.received_account_id)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("account-a", stored["account_id"])

    def test_caller_explicit_request_with_stale_quota_fails_closed(self):
        cmd = self._direct_dispatch_command(account_id="account-a", requested_account_id="account-a")
        result, launcher = self._run(
            cmd, {"account-a": True, "account-b": True}, quota_document=self._stale_account_a_document(),
        )
        # select_claude_account() honors an explicit id regardless of quota
        # confidence (by design -- the launcher's own auth preflight is the
        # real gate for an explicit ask); staleness alone does not fail this
        # closed, so this documents that contract rather than asserting a
        # rejection that select_claude_account never produces for an
        # explicit id.
        self.assertEqual("completed", result["status"])
        self.assertEqual("account-a", launcher.received_account_id)

    def test_requested_account_id_null_with_provisional_account_id_still_automatic(self):
        # Direct assertion on the authority function itself, at the exact
        # shape the real ingress produces.
        from manager.command_watcher import _explicit_account_id
        cmd = self._direct_dispatch_command()
        self.assertIsNone(_explicit_account_id(cmd, self.store.get("tasks", "p1", "t1")))

    def test_no_duplicate_provider_launch_on_automatic_reselection(self):
        cmd = self._direct_dispatch_command()
        result, launcher = self._run(cmd, {"account-a": False, "account-b": True})
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, launcher.events.count("prepare"))
        self.assertEqual(1, launcher.events.count("start"))


class TrustedIngressAdmissionTests(unittest.TestCase):
    """Adversarial coverage for v1 Safe Auto-Admission
    (manager.trusted_ingress): a disposable read-only Task/Command stamped
    by the authenticated Direct Dispatch ingress can be launched without a
    static ADM_WATCHER_ALLOWLIST_PATH entry -- but only when its
    self-declared evidence is corroborated by the separate,
    ingress-only-writable dispatch-request idempotency record. Every
    negative case here must fail exactly the same way an ordinary
    never-allowlisted command does: {"status": "rejected", "reason":
    "not_allowlisted"}, no write, no launch."""

    def setUp(self):
        self.store = Store()
        create_project(self.store, project())
        self.registry = MemoryClaimRegistry()
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-1",
            "task_id": "t1", "command_id": "cmd-1", "created_at": now_iso(),
        }
        self.registry.generation = 1

    def registry_factory(self, bucket, project_id, request_id):
        return self.registry

    def admitted_task(self, **overrides):
        built = create_task(self.store, task(read_only=True), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        built["source_context"] = {
            "origin": "direct_dispatch_ingress", "external_request_id": "req-1", "admission_version": "v1",
        }
        built.update(overrides)
        self.store.put("tasks", "p1", "t1", built)
        return built

    @staticmethod
    def admitted_command(**overrides):
        value = command(created_via="direct_dispatch_ingress", admission_version="v1", request_id="req-1")
        value.update(overrides)
        return value

    def test_trusted_ingress_admitted_task_launches_without_static_allowlist(self):
        self.admitted_task()
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_retry_linked_command_admitted_despite_external_request_id_mismatch(self):
        # A retry targets a pre-existing task, so its own request_id ("req-2")
        # necessarily differs from the task's *original* creation request
        # ("req-1") -- that specific mismatch must not block admission for a
        # retry, only the idempotency-record cross-check (below, for req-2)
        # actually matters.
        self.admitted_task()  # source_context.external_request_id == "req-1"
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-2",
            "task_id": "t1", "command_id": "cmd-1", "created_at": now_iso(),
        }
        retry_command = self.admitted_command(request_id="req-2", retry_count=1, retry_of_execution_id="prior-exec")
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher.prepare_task_retry", return_value=None):
            result = process_command(
                self.store, object(), retry_command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual("completed", result["status"])

    def test_retry_linked_command_still_fails_closed_on_bad_idempotency_record(self):
        # Same retry-shaped command, but the idempotency record's own
        # task_id doesn't actually match -- the relaxed external_request_id
        # check must never become a free pass; every other cross-check
        # still fully applies.
        self.admitted_task()
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-2",
            "task_id": "some-other-task", "command_id": "cmd-1", "created_at": now_iso(),
        }
        retry_command = self.admitted_command(request_id="req-2", retry_count=1, retry_of_execution_id="prior-exec")
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), retry_command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_non_retry_command_still_requires_exact_external_request_id_match(self):
        # Regression: a plain (non-retry) command must still be rejected on
        # a mismatched external_request_id exactly as before -- the relaxed
        # check is retry_of_execution_id-gated only.
        self.admitted_task()  # external_request_id == "req-1"
        mismatched = self.admitted_command(request_id="req-2")  # no retry_of_execution_id
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-2",
            "task_id": "t1", "command_id": "cmd-1", "created_at": now_iso(),
        }
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), mismatched, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_ordinary_untrusted_command_off_allowlist_still_rejected(self):
        self.admitted_task()  # task itself is fully compliant
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), command(),  # no created_via/admission_version/request_id at all
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_caller_read_only_false_task_never_admitted(self):
        self.admitted_task(read_only=False)
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_injected_write_policy_never_admitted(self):
        self.admitted_task(execution_policies=["disposable", "read_only"])  # missing no_repo_writes/no_external_writes
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_forged_created_via_without_any_idempotency_record_never_admitted(self):
        """A Task/Command manually stamped with the ingress's own evidence
        fields -- but naming a request_id nobody ever actually claimed
        through the authenticated ingress -- must not be admitted on the
        self-declared fields alone."""
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(request_id="never-claimed"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_forged_created_via_with_mismatched_idempotency_record_never_admitted(self):
        """The idempotency record exists (a real, distinct request_id was
        claimed) but for a different command_id than this Command claims --
        proving the check cross-references identity, not just presence."""
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(command_id="cmd-not-the-claimed-one"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_no_gcs_bucket_configured_fails_closed_even_with_full_evidence(self):
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": ""}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_wrong_admission_version_never_admitted(self):
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(admission_version="v2-not-yet-supported"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_idempotency_backend_error_fails_closed(self):
        self.admitted_task()
        self.registry.read_unavailable = True
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_static_allowlist_still_bypasses_ingress_check_for_manual_tasks(self):
        """The trusted-ingress path is additive, not a replacement: a
        normal, non-ingress task/command on the static allowlist keeps
        launching exactly as before, with zero ingress evidence and zero
        idempotency-record lookups."""
        built = create_task(self.store, task(read_only=True), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", built)
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset({("p1", "t1")}), health_check=lambda: True, quota_check=lambda service: True,
            )
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_duplicate_admitted_command_only_launches_once(self):
        """The trusted-ingress check only ever runs for a freshly `queued`
        command; replaying the same admitted command after it has already
        gone terminal must be reconciled (skipped), never relaunched."""
        self.admitted_task()
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", runner):
            first = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
            stored = self.store.get("commands", "p1", "cmd-1")
            second = process_command(
                self.store, object(), stored, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
        runner.assert_called_once()
        self.assertEqual("completed", first["status"])
        self.assertEqual({"status": "completed", "skipped": True}, second)


class RepoWriteAdmissionTests(unittest.TestCase):
    """Slice A of the Global Hands-off Execution Layer: v2-repo-write
    admission. manager.command_watcher must become admission-version-aware
    (v1 keeps its existing disposable-read-only semantics; v2-repo-write
    gets its own bounded-write policy gate) rather than running every
    admitted Task through one global read-only gate -- and the static
    ADM_WATCHER_ALLOWLIST_PATH allowlist must never, by itself, be able to
    grant repo-write authority to a Task, no matter how compliant that
    Task's own self-declared fields look."""

    def setUp(self):
        self.store = Store()
        create_project(self.store, project())
        self.registry = MemoryClaimRegistry()
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-1",
            "task_id": "t1", "command_id": "cmd-1", "created_at": now_iso(),
        }
        self.registry.generation = 1

    def registry_factory(self, bucket, project_id, request_id):
        return self.registry

    def v2_task(self, **overrides):
        built = create_task(self.store, task(read_only=False), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_REPO_WRITE_TASK_POLICIES)
        built["source_context"] = {
            "origin": "direct_dispatch_ingress", "external_request_id": "req-1",
            "admission_version": ADMISSION_VERSION_V2_REPO_WRITE, "repo": "https://github.com/example/project",
        }
        built.update(overrides)
        self.store.put("tasks", "p1", "t1", built)
        return built

    @staticmethod
    def v2_command(**overrides):
        value = command(created_via="direct_dispatch_ingress", admission_version=ADMISSION_VERSION_V2_REPO_WRITE, request_id="req-1")
        value.update(overrides)
        return value

    def test_correct_v2_triple_launches_without_static_allowlist(self):
        self.v2_task()
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), self.v2_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                # A repo-write Task takes the writer-authority branch
                # (writer_registry = None if read_only else writer_factory());
                # this is a bare stand-in since launch_task itself is mocked
                # and never actually touches the registry.
                writer_factory=lambda: object(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_command_admission_version_alone_cannot_upgrade_a_v1_task(self):
        """A v1-shaped Task (its own source_context.admission_version is
        still "v1") whose Command alone claims admission_version
        "v2-repo-write" must not be admitted: Task, Command, and
        source_context admission_version must all agree, not just the
        Command's own field."""
        built = create_task(self.store, task(read_only=True), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        built["source_context"] = {
            "origin": "direct_dispatch_ingress", "external_request_id": "req-1", "admission_version": "v1",
        }
        self.store.put("tasks", "p1", "t1", built)
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.v2_command(),  # Command alone claims v2-repo-write
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_static_allowlist_alone_cannot_launch_repo_write(self):
        """Even a Task that is fully v2-repo-write-policy-compliant must
        not launch off the static allowlist alone: static-allowlist
        admission is always evaluated under v1 read-only semantics, never
        the Task's own claimed admission_version."""
        self.v2_task()
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset({("p1", "t1")}), health_check=lambda: True, quota_check=lambda service: True,
            )
        launch.assert_not_called()
        self.assertEqual("attention", result["status"])
        self.assertEqual("allowlisted_task_policy_not_satisfied", result["recovery_reason"])

    def test_unknown_admission_version_never_admitted(self):
        self.v2_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.v2_command(admission_version="v3-unsupported"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_v1_read_only_false_task_still_never_admitted(self):
        """Regression: a Command claiming plain v1 admission for a
        read_only=False Task must still be rejected exactly as before --
        v2-repo-write is a separate, additive contract, not a relaxation of
        v1's own read-only requirement."""
        built = create_task(self.store, task(read_only=False), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        built["source_context"] = {
            "origin": "direct_dispatch_ingress", "external_request_id": "req-1", "admission_version": "v1",
        }
        self.store.put("tasks", "p1", "t1", built)
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), command(created_via="direct_dispatch_ingress", admission_version="v1", request_id="req-1"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()


class WatcherSessionCenterBootstrapIntegrationTests(unittest.TestCase):
    """Reproduces the exact deadlock the reviewer found and proves the fix:
    Session Center used to block its own HTTP bind on wait_for_execution(),
    so /health never came up until an Execution already existed -- but the
    watcher requires /health before it will create that Execution. No Codex
    involved; this only exercises the real HTTP bind and the real gate."""

    def test_health_gate_crosses_before_correlation_completes_no_livelock(self):
        from http.server import ThreadingHTTPServer
        from manager.command_watcher import session_center_healthy
        from manager.session_center import SessionView, build_pending, handler_for

        port = 18899
        url = f"http://127.0.0.1:{port}/health"

        # Before Session Center exists at all, the gate correctly reports
        # unavailable -- this is normal sequencing, not the livelock.
        self.assertFalse(session_center_healthy(url, timeout=0.5))

        args = argparse_namespace(execution_project_id="p1", execution_id="cmd-1", port=port)
        view = SessionView(build_pending(args))
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(view))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # The fix: /health answers immediately, before any Execution
            # exists and before correlation has even been attempted.
            self.assertTrue(session_center_healthy(url, timeout=2))

            store = CommandWatcherTests.allowlist_compliant_store()
            store.put("commands", "p1", "cmd-1", command())
            runner = Mock(side_effect=lambda *a, **kw: (kw["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
            with patch("manager.command_watcher.launch_task", runner):
                result = process_command(
                    store, object(), command(), claim_factory=CommandWatcherTests.claim_factory,
                    allowlist=frozenset({("p1", "t1")}), health_check=lambda: session_center_healthy(url, timeout=2),
                    quota_check=lambda service: True,
                )
            runner.assert_called_once()
            self.assertEqual("completed", result["status"])
        finally:
            server.shutdown()
            server.server_close()


class AsyncClaimContinuationTests(unittest.TestCase):
    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = CommandWatcherTests.allowlist_compliant_store()

    def test_claimed_worker_receives_exact_lifecycle_identity(self):
        claimed = command(project_id="p1", task_id="t1", execution_id="execution-1", status="claimed")
        fake_process = Mock(pid=4242)
        with patch("manager.command_watcher.subprocess.Popen", return_value=fake_process) as popen:
            self.assertEqual(4242, _spawn_claimed_worker(claimed))

        argv = popen.call_args.args[0]
        self.assertEqual(["-m", "manager.command_watcher_worker", "p1", "t1", "execution-1"], argv[1:])
        self.assertEqual(os.getcwd(), popen.call_args.kwargs["cwd"])
        self.assertIs(popen.call_args.kwargs["stdin"], __import__("subprocess").DEVNULL)
        self.assertIs(popen.call_args.kwargs["stdout"], __import__("subprocess").DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], __import__("subprocess").DEVNULL)

    def test_async_claim_returns_before_provider_launch(self):
        provider_launch = Mock()
        with patch("manager.command_watcher._spawn_claimed_worker", return_value=4242) as spawn, \
             patch("manager.command_watcher.launch_task", provider_launch):
            result = process_command(
                self.store, object(), command(), claim_factory=CommandWatcherTests.claim_factory,
                allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True,
                async_launch=True,
            )

        self.assertEqual({"status": "claimed", "execution_id": "command-cmd-1", "worker_pid": 4242}, result)
        spawn.assert_called_once()
        provider_launch.assert_not_called()
        persisted = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("claimed", persisted["status"])
        self.assertEqual("command-cmd-1", persisted["execution_id"])
        self.assertEqual(4242, persisted.get("worker_pid"))
        self.assertIsNotNone(persisted.get("worker_spawned_at"))
        validate("command", persisted)


class PollOnceTimeBudgetTests(unittest.TestCase):
    """Covers the P0 fix: a real HOME Watcher tick could get stuck for
    several minutes enumerating Drive projects/commands before any command
    was ever claimed, freezing runtime_evidence.json refresh under
    MultipleInstances=IgnoreNew. poll_once()'s deadline is the lifecycle-
    safe half of the fix (the other half, a bounded Drive HTTP timeout, is
    covered by collectors/test_publish_drive.py): it only ever stops the
    watcher from STARTING new project/command work once a budget is spent,
    and never interrupts a process_command() call already under way."""

    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = CommandWatcherTests.allowlist_compliant_store()

    def test_normal_idle_poll_with_no_commands_returns_immediately(self):
        results = poll_once(self.store, object(), allowlist=self.ALLOWLIST,
                             claim_factory=CommandWatcherTests.claim_factory,
                             health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual([], results)

    def test_already_expired_deadline_stops_before_starting_any_work(self):
        self.store.put("commands", "p1", "cmd-1", command())
        runner = Mock()
        with patch("manager.command_watcher.launch_task", runner):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST,
                                 deadline=time.monotonic() - 1,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual([], results)
        runner.assert_not_called()
        # Nothing was written -- the command is exactly as queued as before
        # this tick ran, so the next tick can process it normally.
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

    def test_deadline_never_interrupts_a_command_already_started(self):
        self.store.put("commands", "p1", "cmd-1", command())
        self.store.put("commands", "p1", "cmd-2", command(command_id="cmd-2"))
        state = {"started": 0}

        def fake_monotonic():
            # Under budget for every check up through (and including) the
            # one that lets cmd-1 start; past-deadline for every check
            # afterward, regardless of exactly how many time.monotonic()
            # calls Phase 1's waiting_quota sweep (which runs first, across
            # all projects, before any command is even looked at) happens
            # to make -- a call-count-based mock would be brittle against
            # that phase's own internal check count.
            return 100.0 if state["started"] >= 1 else 0.0

        def instrumented_runner(*args, **kwargs):
            state["started"] += 1
            return (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1]

        runner = Mock(side_effect=instrumented_runner)
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher.time.monotonic", side_effect=fake_monotonic):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=50.0,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)

        # cmd-1's process_command() call ran to completion, uninterrupted --
        # this is the real provider-lifecycle-safety guarantee.
        self.assertEqual(1, len(results))
        self.assertEqual("completed", results[0]["status"])
        runner.assert_called_once()
        # cmd-2 was never started at all (not partially processed, not
        # claimed) -- it is exactly as queued as before this tick, so a
        # later tick can pick it up with no state lost.
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-2")["status"])

    def test_default_deadline_is_well_under_the_scheduled_task_cadence(self):
        from manager.command_watcher import POLL_TIME_BUDGET_SECONDS
        self.assertLess(POLL_TIME_BUDGET_SECONDS, 60)


class PollOncePerCommandIsolationTests(unittest.TestCase):
    """Covers a real P0: before this isolation existed, an uncaught
    exception raised while processing ONE command propagated straight out
    of poll_once() (the Phase 2 loop had no per-command try/except),
    aborting the entire tick. Because _prioritized_commands() always sorts
    claimed/running ahead of queued, a command that reliably threw on every
    attempt would reliably abort the tick before any LATER command in the
    same project -- or any later-rotated project -- was ever reached,
    starving them indefinitely. main()'s own top-level `except Exception`
    catches the escaped exception but discards the whole tick's results
    with no detail beyond a generic {"status": "unavailable"}, so this
    failure mode is invisible to Scheduled Task LastTaskResult monitoring
    and repeats identically forever. Live-observed: a genuinely queued
    Command sat unclaimed for many hours in a project whose only "claimed"
    Command (sorted first) could not be reconciled on any natural tick,
    even though every individual step of that reconciliation independently
    proved correct under isolated inspection."""

    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = CommandWatcherTests.allowlist_compliant_store()

    def test_one_commands_exception_does_not_abort_the_rest_of_the_tick(self):
        self.store.put("commands", "p1", "cmd-broken",
                       command(command_id="cmd-broken", status="claimed", execution_id="exec-missing",
                              claimed_at="2020-01-01T00:00:00Z"))
        self.store.put("commands", "p1", "cmd-2", command(command_id="cmd-2"))

        real_process_command = process_command

        def flaky_process_command(store, service, cmd, **kwargs):
            if cmd["command_id"] == "cmd-broken":
                raise RuntimeError("boom")
            return real_process_command(store, service, cmd, **kwargs)

        runner = Mock(side_effect=lambda *a, **k: (k["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
        with patch("manager.command_watcher.process_command", side_effect=flaky_process_command), \
             patch("manager.command_watcher.launch_task", runner):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)

        statuses = {r.get("command_id"): r.get("status") for r in results if isinstance(r, dict)}
        # cmd-broken's own exception is recorded in this tick's results, not
        # allowed to propagate out of poll_once() uncaught.
        self.assertEqual("error", statuses.get("cmd-broken"))
        # cmd-2, sorted right after it in the same project, still got
        # processed in the SAME tick -- proving isolation, not just that a
        # later tick would eventually retry it.
        self.assertEqual("completed", self.store.get("commands", "p1", "cmd-2")["status"])
        # cmd-broken itself is untouched by the failed attempt -- still
        # exactly as claimed as before, available for a real fix or a later
        # tick to reconcile, no partial/corrupt state written.
        self.assertEqual("claimed", self.store.get("commands", "p1", "cmd-broken")["status"])


class CommandProcessingPriorityTests(unittest.TestCase):
    """Covers the P0 fix for a real production trace: a stale `attention`
    Command sitting ahead of a genuinely actionable `queued` Command in
    Drive's own (effectively arbitrary) listing order could consume the one
    process_command() slot a tight remaining poll budget leaves after a slow
    bounded enumeration, starving the queued Command indefinitely. Fix is a
    pure reordering of an already-returned batch -- no change to which
    records are hydrated, how many are processed, or any
    process_command()/_reconcile_active() semantics."""

    def test_max_commands_per_poll_is_unchanged(self):
        self.assertEqual(4, MAX_COMMANDS_PER_POLL)

    def test_attention_then_queued_is_reordered_queued_first(self):
        batch = [command(command_id="c-attn", status="attention"), command(command_id="c-queued", status="queued")]
        ordered = _prioritized_commands(batch)
        self.assertEqual(["c-queued", "c-attn"], [c["command_id"] for c in ordered])

    def test_running_queued_attention_order_is_unchanged(self):
        batch = [command(command_id="c-run", status="running"), command(command_id="c-queued", status="queued"),
                 command(command_id="c-attn", status="attention")]
        ordered = _prioritized_commands(batch)
        self.assertEqual(["c-run", "c-queued", "c-attn"], [c["command_id"] for c in ordered])

    def test_claimed_queued_attention_order_is_unchanged(self):
        batch = [command(command_id="c-claim", status="claimed"), command(command_id="c-queued", status="queued"),
                 command(command_id="c-attn", status="attention")]
        ordered = _prioritized_commands(batch)
        self.assertEqual(["c-claim", "c-queued", "c-attn"], [c["command_id"] for c in ordered])

    def test_ordering_is_stable_within_each_priority_group(self):
        batch = [
            command(command_id="c-attn-1", status="attention"),
            command(command_id="c-queued-1", status="queued"),
            command(command_id="c-attn-2", status="attention"),
            command(command_id="c-queued-2", status="queued"),
            command(command_id="c-run-1", status="running"),
            command(command_id="c-claim-1", status="claimed"),
        ]
        ordered = _prioritized_commands(batch)
        # claimed/running group keeps its own relative (Drive-return) order,
        # then queued group keeps its own relative order, then attention.
        self.assertEqual(
            ["c-run-1", "c-claim-1", "c-queued-1", "c-queued-2", "c-attn-1", "c-attn-2"],
            [c["command_id"] for c in ordered],
        )

    def test_terminal_completed_and_failed_remain_skipped(self):
        batch = [
            command(command_id="c-done", status="completed", completed_at="2026-08-14T00:05:00Z", result={"status": "completed"}),
            command(command_id="c-failed", status="failed", completed_at="2026-08-14T00:05:00Z", result={"status": "failed"}),
            command(command_id="c-queued", status="queued"),
            command(command_id="c-attn", status="attention"),
        ]
        ordered = _prioritized_commands(batch)
        self.assertEqual(["c-queued", "c-attn"], [c["command_id"] for c in ordered])

    def test_project_rotation_is_untouched_by_this_fix(self):
        # Sanity check that this fix only reorders within an already-chosen
        # project's batch -- _rotated_project_ids (cross-project fairness)
        # is a separate, unmodified mechanism. See RotatedProjectIdsTests
        # below for the full coverage of that mechanism itself.
        from manager.command_watcher import _rotated_project_ids
        ids = ["p1", "p2", "p3"]
        self.assertEqual(_rotated_project_ids(ids, now=0.0), _rotated_project_ids(ids, now=0.0))


class PollOnceProcessesQueuedBeforeStaleAttentionTests(unittest.TestCase):
    """Integration-level coverage (real poll_once(), not just
    _prioritized_commands() in isolation): reproduces the exact production
    shape -- one stale `attention` Command (its execution record missing, so
    _reconcile_active() just re-writes it back to attention, same as the
    real predecessor observed in production) ahead of one actionable
    `queued` Command, under a poll budget too tight to process both."""

    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = CommandWatcherTests.allowlist_compliant_store()

    def test_queued_work_is_selected_before_stale_attention_recovery_under_tight_budget(self):
        # The stale attention command's own execution_id points at nothing
        # -- exactly the real predecessor's shape (a recovery/backlog record
        # with no live execution behind it any more).
        self.store.put("commands", "p1", "cmd-attn", command(
            command_id="cmd-attn", status="attention", execution_id="command-cmd-attn",
        ))
        self.store.put("commands", "p1", "cmd-queued", command(command_id="cmd-queued"))

        state = {"started": 0}

        def instrumented_runner(*args, **kwargs):
            state["started"] += 1
            return (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1]

        runner = Mock(side_effect=instrumented_runner)

        def fake_monotonic():
            # Under budget through the check that lets cmd-queued start;
            # past-deadline for every check after that -- robust against
            # exactly how many time.monotonic() calls Phase 1's
            # waiting_quota sweep (all projects, before any command is
            # looked at) happens to make, unlike a hardcoded call count.
            return 100.0 if state["started"] >= 1 else 0.0

        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher.time.monotonic", side_effect=fake_monotonic):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=50.0,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)

        # Exactly one command got its process_command() slot this tick --
        # and thanks to the priority reorder, it was the actionable queued
        # one, not the stale attention backlog.
        self.assertEqual(1, len(results))
        self.assertEqual("completed", results[0]["status"])
        runner.assert_called_once()
        self.assertEqual("completed", self.store.get("commands", "p1", "cmd-queued")["result"]["status"])
        # The stale attention command was never touched this tick -- it is
        # exactly as attention as before, available on a later tick.
        self.assertEqual("attention", self.store.get("commands", "p1", "cmd-attn")["status"])

    def test_process_command_started_before_deadline_still_completes_naturally(self):
        # Same shape, but the budget is tight enough that only the FIRST
        # slot (now the queued command, post-reorder) is even attempted --
        # once process_command() is called for it, it must still run to
        # completion even though the deadline check would fail immediately
        # after. This re-proves the existing lifecycle-safety guarantee
        # still holds with the new ordering in front of it.
        self.store.put("commands", "p1", "cmd-attn", command(
            command_id="cmd-attn", status="attention", execution_id="command-cmd-attn",
        ))
        self.store.put("commands", "p1", "cmd-queued", command(command_id="cmd-queued"))

        state = {"started": 0}

        def instrumented_runner(*args, **kwargs):
            state["started"] += 1
            return (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1]

        runner = Mock(side_effect=instrumented_runner)

        def fake_monotonic():
            return 100.0 if state["started"] >= 1 else 0.0

        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher.time.monotonic", side_effect=fake_monotonic):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=50.0,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual(1, len(results))
        self.assertEqual("completed", results[0]["status"])
        runner.assert_called_once()


class RotatedProjectIdsTests(unittest.TestCase):
    """Covers the deterministic fairness repair: a project with a large
    historical Command backlog can consume the whole remaining poll budget
    on its own bounded hydration, so whichever project is enumerated first
    matters. Rotation is a pure function of wall-clock time -- never a
    process-local counter, since every `--once` invocation is a fresh
    process with no memory of prior ticks."""

    def test_rotation_is_a_pure_function_of_time_not_process_state(self):
        from manager.command_watcher import _rotated_project_ids
        ids = ["p1", "p2", "p3", "p4"]
        first = _rotated_project_ids(ids, now=0.0)
        second = _rotated_project_ids(ids, now=0.0)
        self.assertEqual(first, second)

    def test_every_project_becomes_first_within_one_full_rotation(self):
        from manager.command_watcher import POLL_SECONDS, _rotated_project_ids
        ids = ["p1", "p2", "p3", "p4"]
        seen_first = {_rotated_project_ids(ids, now=tick * POLL_SECONDS)[0] for tick in range(len(ids))}
        self.assertEqual(set(ids), seen_first, "every project must be first at least once per full rotation")

    def test_empty_and_single_project_lists_are_unaffected(self):
        from manager.command_watcher import _rotated_project_ids
        self.assertEqual([], _rotated_project_ids([], now=12345))
        self.assertEqual(["only"], _rotated_project_ids(["only"], now=12345))

    def test_rotation_preserves_the_full_set_and_relative_cycle_order(self):
        from manager.command_watcher import _rotated_project_ids
        ids = ["p1", "p2", "p3"]
        rotated = _rotated_project_ids(ids, now=1 * 60)  # POLL_SECONDS default = 60
        self.assertEqual(set(ids), set(rotated))
        self.assertEqual(3, len(rotated))


class WithinProjectRecordRotationTests(unittest.TestCase):
    """Covers the within-project counterpart to RotatedProjectIdsTests
    above: _rotated_project_ids alone cannot save a Command stuck *inside*
    one large project's own historical backlog, past _enumerate_commands's
    bounded-hydration cutoff -- see
    _within_project_record_rotation_offset's docstring for the real live
    HOME canary (a queued repo-write Command, unclaimed 50+ minutes across
    continuous natural ticks) this closes."""

    def test_rotation_is_a_pure_function_of_time_not_process_state(self):
        from manager.command_watcher import _within_project_record_rotation_offset
        first = _within_project_record_rotation_offset(now=0.0)
        second = _within_project_record_rotation_offset(now=0.0)
        self.assertEqual(first, second)

    def test_offset_advances_by_one_per_poll_tick(self):
        # Default stride=1 preserves the original one-position-per-tick
        # behavior for every existing caller (e.g. _enumerate_commands).
        from manager.command_watcher import POLL_SECONDS, _within_project_record_rotation_offset
        base = _within_project_record_rotation_offset(now=0.0)
        one_tick_later = _within_project_record_rotation_offset(now=POLL_SECONDS)
        self.assertEqual(base + 1, one_tick_later)

    def test_offset_advances_by_stride_per_poll_tick(self):
        from manager.command_watcher import POLL_SECONDS, _within_project_record_rotation_offset
        base = _within_project_record_rotation_offset(now=0.0, stride=4)
        one_tick_later = _within_project_record_rotation_offset(now=POLL_SECONDS, stride=4)
        self.assertEqual(base + 4, one_tick_later)

    def test_enumerate_commands_forwards_a_time_derived_rotate_offset(self):
        from manager.command_watcher import _enumerate_commands, _within_project_record_rotation_offset

        class FakeBoundedStore:
            def __init__(self):
                self.calls = []

            def list_records_bounded(self, area, project_id, **kwargs):
                self.calls.append((area, project_id, kwargs))
                return []

        store = FakeBoundedStore()
        with patch("manager.command_watcher._within_project_record_rotation_offset", return_value=42):
            _enumerate_commands(store, "p1", deadline=100.0)

        self.assertEqual(1, len(store.calls))
        area, project_id, kwargs = store.calls[0]
        self.assertEqual("commands", area)
        self.assertEqual("p1", project_id)
        self.assertEqual(42, kwargs["rotate_offset"])

    def test_enumerate_commands_falls_back_to_list_records_without_bounded_support(self):
        """A store without list_records_bounded (e.g. a test double) must
        keep working exactly as before -- rotate_offset is never referenced
        on that path at all."""
        from manager.command_watcher import _enumerate_commands

        class PlainStore:
            def list_records(self, area, project_id):
                return [{"command_id": "c1", "project_id": project_id}]

        self.assertEqual([{"command_id": "c1", "project_id": "p1"}], _enumerate_commands(PlainStore(), "p1"))

    def test_enumerate_waiting_quota_tasks_forwards_a_time_derived_rotate_offset(self):
        """_enumerate_waiting_quota_tasks is documented as "the exact mirror
        of _enumerate_commands() above" but, unlike it, never forwarded a
        rotate_offset -- so a project whose Tasks backlog exceeds one tick's
        bounded hydration budget can permanently strand its own waiting_quota
        Task past every tick's cutoff, exactly the same starvation class
        _enumerate_commands was already fixed for. Live-reproduced: a real
        v2-repo-write waiting_quota Task in the ai-development-manager
        project sat unpromoted for 40+ minutes across many natural ticks,
        with confirmed-fresh codex quota available the whole time."""
        from manager.command_watcher import _enumerate_waiting_quota_tasks

        class FakeBoundedStore:
            def __init__(self):
                self.calls = []

            def list_records_bounded(self, area, project_id, **kwargs):
                self.calls.append((area, project_id, kwargs))
                return []

        store = FakeBoundedStore()
        with patch("manager.command_watcher._within_project_record_rotation_offset", return_value=42):
            _enumerate_waiting_quota_tasks(store, "p1", deadline=100.0)

        self.assertEqual(1, len(store.calls))
        area, project_id, kwargs = store.calls[0]
        self.assertEqual("tasks", area)
        self.assertEqual("p1", project_id)
        self.assertEqual(42, kwargs["rotate_offset"])

    def test_enumerate_waiting_quota_tasks_forwards_stride_rotate_offset_and_max_records(self):
        """Bounded Convergence Acceleration (2026-08-31): the plain stride=1
        rotate_offset fix above only guarantees EVENTUAL reachability -- for
        a project whose Tasks backlog is very large (confirmed live: a real
        181-record backlog in ai-development-manager), full-cycle coverage
        could take on the order of hours. Passing WAITING_QUOTA_DISCOVERY_
        WINDOW as both max_records and rotation stride bounds this to
        ceil(N / K) ticks for any N, while keeping the single-tick Drive
        read count capped at K."""
        from manager.command_watcher import (
            WAITING_QUOTA_DISCOVERY_WINDOW,
            _enumerate_waiting_quota_tasks,
        )

        class FakeBoundedStore:
            def __init__(self):
                self.calls = []

            def list_records_bounded(self, area, project_id, **kwargs):
                self.calls.append((area, project_id, kwargs))
                return []

        store = FakeBoundedStore()
        with patch("manager.command_watcher._within_project_record_rotation_offset", return_value=16):
            _enumerate_waiting_quota_tasks(store, "p1", deadline=100.0)

        self.assertEqual(1, len(store.calls))
        area, project_id, kwargs = store.calls[0]
        self.assertEqual("tasks", area)
        self.assertEqual("p1", project_id)
        self.assertEqual(16, kwargs["rotate_offset"])
        self.assertEqual(WAITING_QUOTA_DISCOVERY_WINDOW, kwargs["max_records"])


class BoundedCommandEnumerationLifecycleSafetyTests(unittest.TestCase):
    """Covers the P0 fix using a real DriveRecords + fake Drive backend
    (not the simplified in-memory Store), so this exercises the real
    list_records_bounded() call graph poll_once() now goes through."""

    ALLOWLIST = frozenset({("p1", "t1")})

    def _real_store(self):
        from manager.tasks import DriveRecords, ROOT_FOLDER_ID, ROOT_FOLDERS, create_project, create_task
        from manager.test_tasks import FakeDriveService
        from manager.test_execution_lifecycle import project, task

        service = FakeDriveService()
        store = DriveRecords(service)
        create_project(store, project())
        create_task(store, task(read_only=True), assign=False)
        compliant = store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        store.put("tasks", "p1", "t1", compliant)
        return store, service

    def test_large_backlog_in_one_project_does_not_prevent_a_queued_command_from_running(self):
        """A project with many historical terminal commands plus one real
        queued command: the queued command must still be found and
        launched -- bounded hydration must not silently drop it just
        because it comes after a lot of terminal noise."""
        store, service = self._real_store()
        for index in range(15):
            command_id = f"old-{index:03d}"
            store.put("commands", "p1", command_id, command(
                command_id=command_id, status="completed", completed_at="2026-08-14T00:05:00Z",
                result={"status": "completed"},
            ))
        store.put("commands", "p1", "cmd-1", command())

        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            results = poll_once(store, object(), allowlist=self.ALLOWLIST,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual(1, len(results))
        self.assertEqual("completed", results[0]["status"])
        runner.assert_called_once()

    def test_recent_sweep_reaches_late_project_before_full_history_hydration(self):
        store, _ = self._real_store()
        store.put("commands", "p1", "cmd-1", command())
        projects = ["noise-1", "noise-2", "noise-3", "noise-4", "p1"]
        seen = []
        recent_source = object()

        def recent(_store, project_id, deadline=None):
            self.assertIs(recent_source, _store)
            seen.append(project_id)
            if project_id == "p1":
                return [command()]
            return [command(command_id=f"done-{project_id}", status="completed",
                            completed_at="2026-08-14T00:05:00Z", result={"status": "completed"})]

        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch("manager.command_watcher._enumerate_project_ids", return_value=projects), \
             patch("manager.command_watcher._rotated_project_ids", side_effect=lambda values: values), \
             patch("manager.command_watcher._enumerate_waiting_quota_tasks", return_value=[]), \
             patch("manager.command_watcher._enumerate_recent_commands", side_effect=recent), \
             patch("manager.command_watcher._enumerate_commands", return_value=[]), \
             patch("manager.command_watcher.launch_task", runner):
            results = poll_once(store, object(), allowlist=self.ALLOWLIST,
                                recent_store=recent_source,
                                claim_factory=CommandWatcherTests.claim_factory,
                                health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual(projects, seen)
        self.assertEqual("completed", results[0]["status"])
        runner.assert_called_once()

    def test_deadline_expiring_during_command_hydration_never_interrupts_a_started_process_command(self):
        """The deadline can legitimately run out WHILE list_records_bounded
        is still hydrating a project's backlog -- process_command for a
        command already found and started must still complete."""
        store, service = self._real_store()
        store.put("commands", "p1", "cmd-1", command())

        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            results = poll_once(store, object(), allowlist=self.ALLOWLIST,
                                 deadline=time.monotonic() + 30,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual(1, len(results))
        self.assertEqual("completed", results[0]["status"])

    def test_expired_deadline_leaves_queued_command_untouched_for_next_tick(self):
        store, service = self._real_store()
        store.put("commands", "p1", "cmd-1", command())

        runner = Mock()
        with patch("manager.command_watcher.launch_task", runner):
            results = poll_once(store, object(), allowlist=self.ALLOWLIST,
                                 deadline=time.monotonic() - 1,
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual([], results)
        runner.assert_not_called()
        self.assertEqual("queued", store.get("commands", "p1", "cmd-1")["status"])

    def test_no_duplicate_launch_across_two_polls_with_a_large_backlog(self):
        store, service = self._real_store()
        for index in range(10):
            command_id = f"old-{index:03d}"
            store.put("commands", "p1", command_id, command(
                command_id=command_id, status="completed", completed_at="2026-08-14T00:05:00Z",
                result={"status": "completed"},
            ))
        store.put("commands", "p1", "cmd-1", command())

        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            first = poll_once(store, object(), allowlist=self.ALLOWLIST,
                               claim_factory=CommandWatcherTests.claim_factory,
                               health_check=lambda: True, quota_check=lambda service: True)
            second = poll_once(store, object(), allowlist=self.ALLOWLIST,
                                claim_factory=CommandWatcherTests.claim_factory,
                                health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual(1, len(first))
        self.assertEqual([], second)
        runner.assert_called_once()


class PollOnceUsesCheapProjectEnumerationTests(unittest.TestCase):
    """Covers the final remaining gap: poll_once() must enumerate projects
    via DriveRecords.list_project_ids() (no per-project get()), not
    all_projects()/list_projects()'s full hydration -- otherwise the 40s
    poll deadline can never even be checked until that full O(N) listing
    has already returned, exactly as ChatGPT's review confirmed. Uses a
    real DriveRecords backed by the same FakeDriveService fixture
    manager/test_tasks.py uses, not the simplified in-memory Store used by
    the rest of this file, so this exercises the real children()/folder()
    call graph rather than a test double that bypasses it."""

    def test_multi_project_poll_never_hydrates_any_project_document(self):
        from manager.tasks import DriveRecords, ROOT_FOLDER_ID, ROOT_FOLDERS
        from manager.test_tasks import FakeDriveService

        service = FakeDriveService()
        store = DriveRecords(service)
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["projects"])
        for project_id in ("p1", "p2", "p3", "p4", "p5"):
            store.folder(root, project_id)
            store.put("projects", project_id, project_id, {
                "project_id": project_id, "name": project_id, "created_at": "2026-08-22T00:00:00Z", "aliases": [],
            })

        get_media_calls = {"n": 0}
        original = service.transport.get_media

        def counting_get_media(fileId):
            get_media_calls["n"] += 1
            return original(fileId)

        service.transport.get_media = counting_get_media
        try:
            results = poll_once(store, object(), allowlist=frozenset(),
                                 claim_factory=CommandWatcherTests.claim_factory,
                                 health_check=lambda: True, quota_check=lambda service: True)
        finally:
            service.transport.get_media = original

        self.assertEqual([], results)
        # 5 real projects, zero commands in any of them -- if poll_once were
        # still hydrating each project's own JSON document (the old
        # all_projects() path), get_media would be called at least 5 times
        # here (once per project.json). It must be called zero times.
        self.assertEqual(0, get_media_calls["n"])


class MainOnceFastFailTests(unittest.TestCase):
    """Covers requirement 9: a pre-launch failure (here, a bounded Drive
    HTTP timeout surfacing as any exception out of build_service()) must
    make one `--once` invocation return quickly with status "unavailable"
    rather than hang or crash -- proving the next scheduled tick is free to
    run normally, which is the actual mechanism that keeps
    runtime_evidence.json refreshing every cadence."""

    def test_build_service_failure_yields_fast_unavailable_tick(self):
        import io
        from contextlib import redirect_stdout
        from manager.command_watcher import main

        output = io.StringIO()
        with patch("manager.command_watcher.build_service", side_effect=TimeoutError("simulated bounded Drive timeout")), \
             redirect_stdout(output):
            exit_code = main(["--once"])

        self.assertEqual(0, exit_code)
        printed = json.loads(output.getvalue().strip())
        self.assertEqual("unavailable", printed["status"])

    def test_main_builds_a_genuinely_separate_short_timeout_discovery_service(self):
        """The critical timing-boundary fix: main() must build discovery_store
        from a SEPARATE build_service(timeout=WATCHER_DISCOVERY_TIMEOUT_SECONDS)
        call, not just pass a smaller bookkeeping number against the same
        45s-timeout service every write/active-lifecycle call uses -- a
        margin check against a timeout the transport doesn't actually have
        would not really bound anything."""
        import io
        from contextlib import redirect_stdout
        from manager.command_watcher import (
            RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS, WATCHER_DISCOVERY_TIMEOUT_SECONDS, main,
        )

        build_calls = []

        def fake_build_service(timeout=None):
            build_calls.append(timeout)
            return object()

        with patch("manager.command_watcher.build_service", side_effect=fake_build_service), \
             patch("manager.command_watcher.DriveRecords", return_value=Mock(list_project_ids=Mock(return_value=[]))), \
             redirect_stdout(io.StringIO()):
            main(["--once"])

        # First call: the normal, full-timeout service (default -> None
        # means "use DRIVE_REQUEST_TIMEOUT_SECONDS", untouched by this fix).
        # Second call: the discovery-only service, with an explicit,
        # strictly shorter timeout than POLL_TIME_BUDGET_SECONDS.
        self.assertEqual(
            [None, WATCHER_DISCOVERY_TIMEOUT_SECONDS, RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS],
            build_calls,
        )

    def test_discovery_timeout_leaves_real_margin_under_the_poll_budget(self):
        """Documents the actual math this fix depends on: with the real
        POLL_TIME_BUDGET_SECONDS and WATCHER_DISCOVERY_TIMEOUT_SECONDS
        constants, a hydration that starts at the last safe moment still
        finishes with time to spare before the next 60s Scheduled Task
        trigger -- and, just as important, the discovery timeout is
        strictly smaller than the poll budget (the inverse was a real bug
        this task's own tests caught: with the old 45s DRIVE_REQUEST_
        TIMEOUT_SECONDS used as the margin against a 40s budget, no
        hydration could ever start at all)."""
        from manager.command_watcher import POLL_SECONDS, POLL_TIME_BUDGET_SECONDS, WATCHER_DISCOVERY_TIMEOUT_SECONDS

        self.assertLess(WATCHER_DISCOVERY_TIMEOUT_SECONDS, POLL_TIME_BUDGET_SECONDS)
        worst_case_total = POLL_TIME_BUDGET_SECONDS + WATCHER_DISCOVERY_TIMEOUT_SECONDS
        self.assertLess(worst_case_total, POLL_SECONDS,
                         "budget + one worst-case discovery request must still fit before the next scheduled trigger")


def argparse_namespace(**overrides):
    import argparse
    base = {
        "provider_session_id": None, "execution_file": None, "execution_project_id": None,
        "execution_id": None, "wait_seconds": 5.0, "project_id": None, "task_id": None,
        "branch": None, "port": 0, "idle_seconds": 15.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class EmbeddedIngressSwitchTests(unittest.TestCase):
    """Covers fix/command-watcher-embedded-ingress-decouple-20260823: an
    explicit ADM_COMMAND_WATCHER_EMBEDDED_INGRESS switch that lets the
    dedicated Drive Dispatch Ingress Scheduled Task become the sole polling
    authority, without deleting the embedded path pre-migration installs
    still rely on."""

    # 1: embedded ingress enabled (unset, default) -> current behavior preserved
    def test_unset_env_var_defaults_to_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(embedded_ingress_enabled())

    def test_explicit_true_strings_enable(self):
        for value in ("1", "true", "TRUE", "  yes  ", "Yes"):
            self.assertTrue(embedded_ingress_enabled(value), value)

    # 2 & 5: disabled -> poll_drive_dispatch_requests never invoked, and
    # folder/owner env presence cannot override an explicit disable
    def test_explicit_false_strings_disable(self):
        for value in ("0", "false", "FALSE", "  no  ", "No"):
            self.assertFalse(embedded_ingress_enabled(value), value)

    # 6: malformed/ambiguous config fails closed to disabled
    def test_malformed_value_fails_closed_to_disabled(self):
        for value in ("", "garbage", "2", "yesplease", "None"):
            self.assertFalse(embedded_ingress_enabled(value), value)

    def test_reads_env_var_when_no_raw_argument_given(self):
        with patch.dict(os.environ, {"ADM_COMMAND_WATCHER_EMBEDDED_INGRESS": "0"}, clear=True):
            self.assertFalse(embedded_ingress_enabled())
        with patch.dict(os.environ, {"ADM_COMMAND_WATCHER_EMBEDDED_INGRESS": "1"}, clear=True):
            self.assertTrue(embedded_ingress_enabled())


class EmbeddedIngressMainWiringTests(unittest.TestCase):
    """Exercises the actual poll_drive_dispatch_requests() call site inside
    main() -- the unit-level embedded_ingress_enabled() tests above prove the
    switch's own truth table, these prove main() actually obeys it."""

    def _run_once(self, env):
        import io
        from contextlib import redirect_stdout
        from manager.command_watcher import main

        ingress_calls = []

        def fake_poll(store, service, bucket):
            ingress_calls.append((store, service, bucket))
            return []

        with patch.dict(os.environ, env, clear=True), \
             patch("manager.command_watcher.build_service", return_value=object()), \
             patch("manager.command_watcher.DriveRecords", return_value=Mock(list_project_ids=Mock(return_value=[]))), \
             patch("manager.drive_dispatch_ingress.poll_drive_dispatch_requests", side_effect=fake_poll), \
             redirect_stdout(io.StringIO()):
            main(["--once"])
        return ingress_calls

    # 1: folder id present, switch unset (default enabled) -> ingress polled, current behavior preserved
    def test_unset_switch_with_folder_id_polls_ingress(self):
        calls = self._run_once({"ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID": "folder-1"})
        self.assertEqual(1, len(calls))

    # 2: switch explicitly disabled, folder id still present -> never polled
    def test_disabled_switch_with_folder_id_never_polls_ingress(self):
        calls = self._run_once({
            "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID": "folder-1",
            "ADM_COMMAND_WATCHER_EMBEDDED_INGRESS": "0",
        })
        self.assertEqual(0, len(calls))

    # 5: folder id alone (no switch value at all disabling it) cannot
    # accidentally re-enable ingress once explicitly disabled
    def test_disabled_switch_ignores_owner_env_too(self):
        calls = self._run_once({
            "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID": "folder-1",
            "ADM_DRIVE_DISPATCH_INGRESS_OWNER": "someone@example.com",
            "ADM_COMMAND_WATCHER_EMBEDDED_INGRESS": "false",
        })
        self.assertEqual(0, len(calls))

    # No folder id at all -> never polls regardless of switch (unchanged prior contract)
    def test_no_folder_id_never_polls_even_if_switch_enabled(self):
        calls = self._run_once({"ADM_COMMAND_WATCHER_EMBEDDED_INGRESS": "1"})
        self.assertEqual(0, len(calls))

    # 3: disabled mode still proceeds to poll_once() (command polling unaffected)
    def test_disabled_switch_still_runs_poll_once(self):
        import io
        from contextlib import redirect_stdout
        from manager.command_watcher import main

        with patch.dict(os.environ, {
            "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID": "folder-1",
            "ADM_COMMAND_WATCHER_EMBEDDED_INGRESS": "0",
        }, clear=True), \
             patch("manager.command_watcher.build_service", return_value=object()), \
             patch("manager.command_watcher.DriveRecords", return_value=Mock(list_project_ids=Mock(return_value=[]))), \
             patch("manager.command_watcher.poll_once", return_value=["sentinel"]) as poll_once_mock, \
             redirect_stdout(output := io.StringIO()):
            exit_code = main(["--once"])

        self.assertEqual(0, exit_code)
        poll_once_mock.assert_called_once()
        printed = json.loads(output.getvalue().strip())
        self.assertEqual(["sentinel"], printed["commands"])
        self.assertEqual([], printed["ingress"])


class WindowsWatcherEmbeddedIngressWiringTests(unittest.TestCase):
    """7 & 8: the Windows installer/runner carry an explicit switch through
    to the runtime env var, same wiring style as the existing allowlist/
    ingress-folder-id/claude-accounts params this test class sits next to
    (see test_windows_watcher_task_wires_allowlist_path_to_runtime above)."""

    def test_installer_and_runner_wire_embedded_ingress_switch(self):
        manager = Path(__file__).parent
        installer = (manager / "install_command_watcher.ps1").read_text(encoding="utf-8")
        runner = (manager / "run_command_watcher.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$EmbeddedIngress", installer)
        self.assertIn('-EmbeddedIngress `"$EmbeddedIngress`"', installer)
        self.assertIn("[string]$EmbeddedIngress", runner)
        self.assertIn("$env:ADM_COMMAND_WATCHER_EMBEDDED_INGRESS = $EmbeddedIngress", runner)

    def test_installer_does_not_touch_dedicated_ingress_scripts(self):
        manager = Path(__file__).parent
        for name in ("install_drive_dispatch_ingress.ps1", "run_drive_dispatch_ingress.ps1"):
            path = manager / name
            if path.exists():
                self.assertNotIn("EmbeddedIngress", path.read_text(encoding="utf-8"))


class WaitingQuotaPromotionTests(unittest.TestCase):
    """DASHBOARD_TRUTH_CONNECTED gate 1/4: the retry sweep that promotes a
    waiting_quota Task (manager.dispatcher.dispatch()'s own admission fix --
    a Task admitted with recommended_provider=None because no provider had
    usable quota at admission time) into a real Command once quota
    recovers, hooked into poll_once()'s own already-scheduled tick instead
    of a separate mechanism."""

    ALLOWLIST = frozenset({("p1", "t1")})

    @staticmethod
    def claim_factory(*_args): return object()

    def store_with_waiting_quota_task(self, request_id="req-wq"):
        store = Store()
        create_project(store, project())
        waiting_task = task(read_only=True)
        waiting_task.update(
            recommended_provider=None, quota_evidence={},
            source_context={"origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": request_id,
                             "goal": "test", "admission_version": "1.0"},
        )
        create_task(store, waiting_task, assign=False)
        return store

    def poll(self, store):
        return poll_once(store, object(), allowlist=self.ALLOWLIST, claim_factory=self.claim_factory,
                          health_check=lambda: True, quota_check=lambda service: True)

    def test_waiting_quota_task_is_promoted_to_a_command_on_the_next_natural_tick(self):
        store = self.store_with_waiting_quota_task()
        with patch("manager.command_watcher.read_drive_status", return_value=fresh_quota_fixture(80, 90)):
            self.poll(store)
        promoted = store.get("commands", "p1", "t1")
        self.assertEqual("codex", promoted["provider"])
        self.assertEqual("queued", promoted["status"])
        self.assertEqual("t1", promoted["task_id"])
        # Same Task identity throughout -- never a duplicate Task.
        self.assertEqual("t1", store.get("tasks", "p1", "t1")["task_id"])
        self.assertEqual("req-wq", promoted.get("request_id"))
        self.assertEqual("1.0", promoted.get("admission_version"))

    def test_promotion_is_idempotent_and_never_double_creates_a_command(self):
        """Two repeated sweep attempts for the same identity must produce
        exactly one Command -- never a duplicate write once a Command
        already exists for this identity (the known Drive-record-creation
        race class this function's own docstring calls out). Calls
        _promote_waiting_quota_task() directly (not poll_once()) so this
        isolates the sweep's own idempotency from the separate, unrelated
        fact that a newly-queued Command also becomes eligible for the
        ordinary process_command() launch pipeline on next tick -- covered
        by test_waiting_quota_task_is_promoted_to_a_command_on_the_next_natural_tick
        already proving the promotion itself happens on a real poll_once()
        tick."""
        store = self.store_with_waiting_quota_task()
        [waiting_task] = _enumerate_waiting_quota_tasks(store, "p1")
        fresh = fresh_quota_fixture(80, 90)
        first = _promote_waiting_quota_task(store, object(), waiting_task, fresh)
        self.assertIsNotNone(first)
        second = _promote_waiting_quota_task(store, object(), waiting_task, fresh)
        self.assertIsNone(second)  # no-op: a Command already exists for this identity
        self.assertEqual(first, store.get("commands", "p1", "t1"))

    def test_scheduler_batch_path_tasks_are_never_swept(self):
        """Only Tasks admitted via the trusted Direct Dispatch ingress carry
        the task_id == command_id identity contract this sweep relies on --
        a Task with no source_context.origin at all (e.g. a manual
        task-create/scheduler.py batch task) must never be promoted by this
        mechanism; its own caller already sees a waiting_quota dispatcher
        result directly."""
        store = Store()
        create_project(store, project())
        plain = task(read_only=True)
        plain.update(recommended_provider=None, quota_evidence={})
        create_task(store, plain, assign=False)
        with patch("manager.command_watcher.read_drive_status", return_value=fresh_quota_fixture(80, 90)):
            self.poll(store)
        with self.assertRaises(TaskError):
            store.get("commands", "p1", "t1")

    def test_completed_or_cancelled_tasks_are_never_swept(self):
        store = self.store_with_waiting_quota_task()
        stored = store.get("tasks", "p1", "t1")
        stored["status"] = "completed"
        store.put("tasks", "p1", "t1", stored)
        found = _enumerate_waiting_quota_tasks(store, "p1")
        self.assertEqual([], found)

    def test_promotion_must_not_discard_real_completed_execution_history(self):
        """CONFIRMED BUG (parallel-validation review of 7f2e91f):
        _promote_waiting_quota_task() used to call
        `dispatcher_dispatch(store, service, request, quota_document, history or [])`
        -- `history` was a parameter of this function that poll_once() (its
        only real caller) never actually supplied, so this was always
        `dispatcher_dispatch(..., quota_document, [])`. That `[]` landed
        positionally in manager.dispatcher.dispatch()'s 5th parameter,
        `executions`. Because `[]` is not None, dispatch() took
        `history = executions` (dispatcher.py's own `if executions is not
        None:` branch) instead of calling its own
        `list_executions(store, project_id)` -- silently discarding this
        project's REAL completed-execution history for every promotion, no
        matter how much matching history actually existed. Every promoted
        Command's `quota_evidence[provider]["historical_estimate"]` was
        therefore always the "No matching completed executions" fallback
        (confidence "none", sample_count 0) -- fabricated evidence surfaced
        straight to the Dashboard, exactly the class of bug
        DASHBOARD_TRUTH_CONNECTED exists to eliminate. (Provider selection/
        eligibility itself was unaffected -- manager.assignment.decide()
        scores purely off live quota + the task's own expected_minutes,
        never off historical_estimate -- so this was a truth/evidence bug,
        not a mis-routing bug.)

        Fix: _promote_waiting_quota_task() no longer passes `executions` at
        all -- it passes `history_deadline` instead, so dispatch() takes
        its normal bounded list_executions_bounded()/list_executions()
        path, exactly like every other real dispatch() caller. The dead
        `history=None` parameter was removed entirely -- nothing ever
        supplied it."""
        store = self.store_with_waiting_quota_task()
        real_history = [{
            "provider": "codex", "mode": "code", "effort": "medium", "status": "completed",
            "elapsed_minutes": 42,
            "task_snapshot": {"task_type": "implementation", "complexity": "medium", "needs_repo_edit": True},
            "quota_delta": {"status": "known", "windows": [{"name": "primary", "status": "known", "used_percent_delta": 3}]},
        }]
        [waiting_task] = _enumerate_waiting_quota_tasks(store, "p1")
        fresh = fresh_quota_fixture(80, 90)
        # Patched at its real call site: manager.dispatcher.dispatch()'s
        # history_deadline path calls manager.executions.list_executions_
        # bounded(), which -- for this fake Store (no list_records_bounded)
        # -- falls back to calling list_executions() using ITS OWN
        # manager.executions module-global name, not manager.dispatcher's
        # separately-imported reference; patching the latter would not
        # intercept this call at all.
        with patch("manager.executions.list_executions", return_value=real_history) as mock_list_executions:
            promoted = _promote_waiting_quota_task(store, object(), waiting_task, fresh)
        mock_list_executions.assert_called_once()
        historical = promoted["quota_evidence"]["codex"]["historical_estimate"]
        self.assertEqual(1, historical["sample_count"])
        self.assertEqual(42, historical["estimated_minutes"])

    def test_promotion_persists_recommended_provider_onto_the_task(self):
        """CONFIRMED BUG (parallel-validation finding 3): after successfully
        creating a Command, the Task's own recommended_provider/mode/effort
        were never persisted back onto the Task record -- every later tick
        would re-discover the same already-promoted Task via
        _enumerate_waiting_quota_tasks() (recommended_provider still None)
        and redo a wasted Command-existence Drive lookup forever, and
        anything reading the Task record directly (not joined with its
        Command) would keep seeing stale waiting_quota truth after the
        Task was actually already running."""
        store = self.store_with_waiting_quota_task()
        [waiting_task] = _enumerate_waiting_quota_tasks(store, "p1")
        fresh = fresh_quota_fixture(80, 90)
        promoted = _promote_waiting_quota_task(store, object(), waiting_task, fresh)
        self.assertIsNotNone(promoted)
        task_after = store.get("tasks", "p1", "t1")
        self.assertEqual(promoted["provider"], task_after["recommended_provider"])
        self.assertEqual(promoted.get("mode"), task_after["mode"])
        self.assertEqual(promoted.get("effort"), task_after["effort"])
        # No longer even discoverable as waiting_quota on a later tick.
        self.assertEqual([], _enumerate_waiting_quota_tasks(store, "p1"))

    def test_promotion_restores_original_preferred_provider_and_account_id(self):
        """CONFIRMED BUG (parallel-validation finding 4): the rebuilt
        re-dispatch request dropped the original caller's preferred_
        provider/excluded_provider/account_id entirely, and the promoted
        Command's requested_provider/requested_account_id were hardcoded to
        None regardless of what was actually originally requested --
        breaking provenance continuity between the original dispatch
        intent and the promoted Command. A caller who explicitly asked for
        Claude account-b specifically must still get account-b honored on
        promotion, not a silently different automatic selection, and the
        Command's own requested_provider/requested_account_id must reflect
        that original ask, not a fabricated None."""
        store = Store()
        create_project(store, project())
        waiting_task = task(read_only=True)
        waiting_task.update(
            recommended_provider=None, quota_evidence={}, preferred_provider="claude", account_id="account-b",
            source_context={"origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": "req-preferred-wq",
                             "goal": "test", "admission_version": "1.0"},
        )
        create_task(store, waiting_task, assign=False)
        [waiting] = _enumerate_waiting_quota_tasks(store, "p1")
        self.assertEqual("claude", waiting.get("preferred_provider"))
        self.assertEqual("account-b", waiting.get("account_id"))

        fresh = {"schema_version": "0.1.0", "generated_at": now_iso(), "providers": [
            {"provider": "claude", "account_id": "account-a", "display_name": "Claude Code", "collection_mode": "automatic",
             "source": "test", "source_type": "official", "confidence": "official", "last_updated": now_iso(), "status": "ok",
             "windows": [{"name": "five_hour", "remaining_percent": 95, "used_percent": 5, "resets_at": None}]},
            {"provider": "claude", "account_id": "account-b", "display_name": "Claude Code", "collection_mode": "automatic",
             "source": "test", "source_type": "official", "confidence": "official", "last_updated": now_iso(), "status": "ok",
             "windows": [{"name": "five_hour", "remaining_percent": 60, "used_percent": 40, "resets_at": None}]},
            {"provider": "codex", "display_name": "Codex", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": now_iso(), "status": "ok",
             "windows": [{"name": "primary", "remaining_percent": 80, "used_percent": 20, "resets_at": None}]},
        ]}
        promoted = _promote_waiting_quota_task(store, object(), waiting, fresh)
        self.assertEqual("claude", promoted["provider"])
        self.assertEqual("account-b", promoted["account_id"])
        self.assertEqual("claude", promoted["requested_provider"])
        self.assertEqual("account-b", promoted["requested_account_id"])

    def test_promotion_still_refuses_when_the_restored_preferred_provider_remains_unavailable(self):
        """The other half of finding 4's own caution: restoring the
        original preference must never bypass an actually-unavailable
        provider. If the originally-requested account is STILL unreliable
        at promotion time, this must stay a no-op (still waiting_quota) --
        never force a launch, and never silently substitute a different,
        available provider the caller never asked for."""
        store = Store()
        create_project(store, project())
        waiting_task = task(read_only=True)
        waiting_task.update(
            recommended_provider=None, quota_evidence={}, preferred_provider="claude", account_id="account-b",
            source_context={"origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": "req-still-stale",
                             "goal": "test", "admission_version": "1.0"},
        )
        create_task(store, waiting_task, assign=False)
        [waiting] = _enumerate_waiting_quota_tasks(store, "p1")

        still_stale = {"schema_version": "0.1.0", "generated_at": "2020-01-01T00:00:00Z", "providers": [
            {"provider": "claude", "account_id": "account-b", "display_name": "Claude Code", "collection_mode": "automatic",
             "source": "test", "source_type": "official", "confidence": "official", "last_updated": "2020-01-01T00:00:00Z", "status": "ok",
             "windows": [{"name": "five_hour", "remaining_percent": 60, "used_percent": 40, "resets_at": None}]},
            {"provider": "codex", "display_name": "Codex", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": now_iso(), "status": "ok",
             "windows": [{"name": "primary", "remaining_percent": 80, "used_percent": 20, "resets_at": None}]},
        ]}
        result = _promote_waiting_quota_task(store, object(), waiting, still_stale)
        self.assertIsNone(result)
        with self.assertRaises(TaskError):
            store.get("commands", "p1", "t1")


class WaitingQuotaSweepStarvationTests(unittest.TestCase):
    """Live-reproduced 2026-08-28 (BLOCKER 3 acceptance testing): a project
    whose command backlog alone fills MAX_COMMANDS_PER_POLL (stale
    `attention` recovery-check records, each with a dangling execution_id --
    exactly the real production shape observed: 9 attention + 2 queued
    Commands dating back to 2026-08-13..22, still unresolved) must never
    starve that SAME project's own waiting_quota sweep. Before this fix,
    poll_once()'s inner command loop did `return results` the moment the
    global results cap filled -- exiting poll_once() entirely and skipping
    the waiting_quota block below for the rest of the tick, for this
    project AND every later-rotated one, for as long as that backlog
    persisted (which, live, was indefinitely: the same stale commands
    refill the cap every single tick)."""

    ALLOWLIST = frozenset({("p1", "t1")})

    def test_a_full_stale_attention_backlog_never_blocks_this_projects_own_promotion(self):
        store = Store()
        create_project(store, project())
        waiting_task = task(read_only=True)
        waiting_task.update(
            recommended_provider=None, quota_evidence={},
            source_context={"origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": "req-starvation",
                             "goal": "test", "admission_version": "1.0"},
        )
        create_task(store, waiting_task, assign=False)

        # MAX_COMMANDS_PER_POLL + 1 stale attention commands, unrelated to
        # t1, each with a dangling execution_id -- the exact real
        # production shape (a recovery/backlog record with no live
        # execution behind it any more) that alone fills the shared
        # per-tick command budget. The "+1" matters: the buggy `return
        # results` this test guards against is only ever reached on the
        # cap-check *before* processing what WOULD be a 5th command --
        # exactly MAX_COMMANDS_PER_POLL commands lets the for loop simply
        # exhaust itself without ever re-checking the cap, which would make
        # this test pass even against the unfixed code (confirmed live
        # while writing this test).
        for i in range(MAX_COMMANDS_PER_POLL + 1):
            cmd_id = f"cmd-stale-{i}"
            store.put("commands", "p1", cmd_id, command(
                command_id=cmd_id, task_id=f"stale-task-{i}", status="attention",
                execution_id=f"command-{cmd_id}",
            ))

        with patch("manager.command_watcher.read_drive_status", return_value=fresh_quota_fixture(80, 90)):
            results = poll_once(store, object(), allowlist=self.ALLOWLIST,
                                claim_factory=CommandWatcherTests.claim_factory,
                                health_check=lambda: True, quota_check=lambda service: True)

        # The stale backlog alone consumed the whole command-processing
        # budget, exactly as intended -- this fix does not weaken that cap.
        self.assertEqual(MAX_COMMANDS_PER_POLL, len(results))
        for entry in results:
            self.assertEqual("attention", entry["status"])
        # ...but the waiting_quota Task on this SAME project was still
        # promoted to a real Command this same tick, not starved.
        promoted = store.get("commands", "p1", "t1")
        self.assertEqual("codex", promoted["provider"])
        self.assertEqual("queued", promoted["status"])
        self.assertEqual("req-starvation", promoted.get("request_id"))

    def test_waiting_quota_enumeration_happens_before_any_command_enumeration(self):
        """Structural lock-in of the fix's actual mechanism (not just its
        observable effect above): Phase 1's waiting_quota sweep must
        complete for every rotated project BEFORE Phase 2 ever calls
        _enumerate_commands for any project -- this is what protects a
        late-rotated project's own promotion from cumulative discovery cost
        piling up in front of it (live-reproduced: with enough real
        registered projects, that cumulative cost alone can exhaust the
        entire poll budget). A call-order regression here would silently
        reopen the starvation window even if the other two tests above
        still happened to pass in their own narrower shapes."""
        store = Store()
        create_project(store, project())
        waiting_task = task(read_only=True)
        waiting_task.update(
            recommended_provider=None, quota_evidence={},
            source_context={"origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": "req-order",
                             "goal": "test", "admission_version": "1.0"},
        )
        create_task(store, waiting_task, assign=False)
        store.put("commands", "p1", "cmd-attn", command(
            command_id="cmd-attn", task_id="stale-task", status="attention",
            execution_id="command-cmd-attn",
        ))

        import manager.command_watcher as cw_module
        call_order = []
        orig_enum_commands = cw_module._enumerate_commands
        orig_enum_waiting = cw_module._enumerate_waiting_quota_tasks

        def traced_commands(*a, **k):
            call_order.append("commands")
            return orig_enum_commands(*a, **k)

        def traced_waiting(*a, **k):
            call_order.append("waiting")
            return orig_enum_waiting(*a, **k)

        with patch("manager.command_watcher.read_drive_status", return_value=fresh_quota_fixture(80, 90)), \
             patch("manager.command_watcher._enumerate_commands", traced_commands), \
             patch("manager.command_watcher._enumerate_waiting_quota_tasks", traced_waiting):
            poll_once(store, object(), allowlist=self.ALLOWLIST,
                     claim_factory=CommandWatcherTests.claim_factory,
                     health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual("waiting", call_order[0])
        self.assertTrue(call_order[1:])
        self.assertTrue(all(entry == "commands" for entry in call_order[1:]))


class Phase1BudgetCapTests(unittest.TestCase):
    """Covers a real P0 (2026-08-29): before PHASE_1_TIME_BUDGET_SECONDS
    existed, Phase 1's waiting_quota sweep shared the tick's FULL deadline
    with Phase 2. Live-reproduced: a single project's waiting_quota Task
    enumeration alone took 20+ real seconds under real Drive latency
    against a backlog grown over a long session, leaving under 10s of the
    40s tick budget for Phase 2 -- below WATCHER_DISCOVERY_TIMEOUT_SECONDS'
    own per-request safety margin, so Phase 2's command discovery returned
    zero records EVERY tick, indefinitely, even though the project sat at
    rotation position 2 of 10 with nothing ahead of it. A claimed Command
    long past its claim timeout, and a genuinely queued Command, both sat
    frozen for hours while ticks kept completing successfully (exit 0),
    never once touching either. Capping Phase 1 to its own sub-budget
    guarantees Phase 2 a real floor regardless of how expensive any single
    project's waiting_quota sweep turns out to be."""

    ALLOWLIST = frozenset({("p1", "t1"), ("p1", "t2")})

    def test_an_expensive_phase1_sweep_still_leaves_phase2_a_real_floor(self):
        store = Store()
        create_project(store, project())
        waiting_task = task(read_only=True)
        waiting_task.update(
            recommended_provider=None, quota_evidence={},
            source_context={"origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": "req-phase1-slow",
                             "goal": "test", "admission_version": "1.0"},
        )
        create_task(store, waiting_task, assign=False)
        second_task = {**task(read_only=True), "task_id": "t2",
                      "execution_policies": sorted(REQUIRED_TASK_POLICIES)}
        create_task(store, second_task, assign=False)
        store.put("commands", "p1", "cmd-2", command(command_id="cmd-2", task_id="t2"))

        state = {"now": 0.0}

        def fake_monotonic():
            return state["now"]

        def slow_enumerate_waiting_quota_tasks(discovery_store, project_id, deadline=None):
            # Simulate the live-reproduced cost: one project's own
            # waiting_quota enumeration alone burns well past Phase 1's
            # own sub-budget, real seconds under real Drive latency.
            state["now"] += PHASE_1_TIME_BUDGET_SECONDS + 5
            return []

        runner = Mock(side_effect=lambda *a, **k: (k["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
        with patch("manager.command_watcher.time.monotonic", side_effect=fake_monotonic), \
             patch("manager.command_watcher._enumerate_waiting_quota_tasks",
                   side_effect=slow_enumerate_waiting_quota_tasks), \
             patch("manager.command_watcher.launch_task", runner):
            results = poll_once(store, object(), allowlist=self.ALLOWLIST, deadline=POLL_TIME_BUDGET_SECONDS,
                                claim_factory=CommandWatcherTests.claim_factory,
                                health_check=lambda: True, quota_check=lambda service: True)

        # Phase 1's own sub-budget was blown through by a single project --
        # confirmed by the fake clock having advanced well past
        # PHASE_1_TIME_BUDGET_SECONDS already.
        self.assertGreater(state["now"], PHASE_1_TIME_BUDGET_SECONDS)
        # ...yet Phase 2 still ran, in the SAME tick, because it checks
        # against the tick's full deadline, not Phase 1's shorter one.
        self.assertEqual(1, len(results))
        self.assertEqual("completed", results[0]["status"])
        runner.assert_called_once()

    def test_phase1_deadline_never_exceeds_an_already_tight_caller_supplied_deadline(self):
        """A caller-supplied deadline tighter than PHASE_1_TIME_BUDGET_SECONDS
        (e.g. a test, or a tick with little budget left for some other
        reason) must still be respected exactly -- phase1_deadline is a
        min(), never an extension of whatever the real deadline already
        was."""
        store = Store()
        create_project(store, project())
        store.put("commands", "p1", "cmd-1", command())

        results = poll_once(store, object(), allowlist=frozenset({("p1", "t1")}),
                            deadline=time.monotonic() - 1,
                            claim_factory=CommandWatcherTests.claim_factory,
                            health_check=lambda: True, quota_check=lambda service: True)

        self.assertEqual([], results)
        self.assertEqual("queued", store.get("commands", "p1", "cmd-1")["status"])


class OrphanClaimRecoveryTests(unittest.TestCase):
    """P0-2: autonomous recovery for orphaned pre-execution GCS claims.

    Complete test matrix (A through J) as specified in requirements:
      A. expired + exact claim + no Execution + worker dead -> release + requeue
      B. worker still alive -> do not release (fail closed)
      C. PID reused / creation identity mismatch -> worker proven dead -> release + requeue
      D. worker identity unknown (e.g. legacy command) -> fail closed (do not release)
      E. matching Session/provider evidence exists -> do not release
      F. newer claim generation / different execution owner -> stale recovery does not release
      G. two concurrent reconcilers -> exactly one CAS wins, other does not double-requeue
      H. CAS backend transient error -> fail closed, do not requeue
      I. release succeeded -> next watcher tick can cleanly re-claim and execute
      J. _claim_expired(now=datetime) deterministic time-injection correctness
    """

    ALLOWLIST = frozenset({("p1", "t1")})

    @staticmethod
    def allowlist_compliant_store():
        store = Store()
        from manager.test_execution_lifecycle import project, task
        from manager.tasks import create_project, create_task
        create_project(store, project())
        create_task(store, task(read_only=True), assign=False)
        compliant = store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        store.put("tasks", "p1", "t1", compliant)
        return store

    def _orphan_claimed_command(self, worker_pid=99999999, worker_creation_identity="dead-worker-creation-id"):
        """A claimed Command whose claim_timeout has already elapsed, with
        worker identity recorded. Default PID 99999999 is dead on any normal system."""
        cmd = command(
            status="claimed",
            execution_id="command-cmd-1",
            claimed_at="2000-01-01T00:00:00Z",  # well past CLAIM_TIMEOUT_SECONDS
            worker_pid=worker_pid,
            worker_creation_identity=worker_creation_identity,
            worker_spawned_at="2000-01-01T00:00:00Z",
        )
        return cmd

    def _pre_seeded_claim_registry(self, execution_id="command-cmd-1"):
        """A MemoryClaimRegistry with the orphan claim already written."""
        registry = MemoryClaimRegistry()
        from manager.task_claims import claim_task_execution
        claim_task_execution(registry, "p1", "t1", execution_id, "codex", "2000-01-01T00:00:00Z")
        return registry

    # -------------------------------------------------------------------
    # Test A: expired + exact claim + no Execution + worker dead -> release + requeue
    # -------------------------------------------------------------------

    def test_A_expired_orphan_claim_worker_dead_releases_and_requeues(self):
        store = self.allowlist_compliant_store()
        cmd = self._orphan_claimed_command(worker_pid=99999999, worker_creation_identity="dead-worker")
        store.put("commands", "p1", "cmd-1", cmd)

        registry = self._pre_seeded_claim_registry()
        result = process_command(store, object(), cmd, claim_factory=lambda *_: registry)

        self.assertEqual("queued", result["status"])
        self.assertTrue(result.get("orphan_claim_released"))
        stored = store.get("commands", "p1", "cmd-1")
        self.assertEqual("queued", stored["status"])
        self.assertIsNone(registry.document, "GCS claim must be CAS-released")
        self.assertEqual("orphaned_pre_execution_claim_released", stored.get("recovery_reason"))
        self.assertIsNone(stored.get("execution_id"))
        self.assertIsNone(stored.get("claimed_at"))
        self.assertIsNone(stored.get("worker_pid"))
        self.assertIsNone(stored.get("worker_creation_identity"))
        self.assertIsNone(stored.get("worker_spawned_at"))

    # -------------------------------------------------------------------
    # Test B: worker still alive -> do not release
    # -------------------------------------------------------------------

    def test_B_worker_still_alive_refuses_release(self):
        store = self.allowlist_compliant_store()
        live_pid = os.getpid()
        live_identity = process_creation_identity(live_pid)
        cmd = self._orphan_claimed_command(worker_pid=live_pid, worker_creation_identity=live_identity)
        store.put("commands", "p1", "cmd-1", cmd)

        registry = self._pre_seeded_claim_registry()
        result = process_command(store, object(), cmd, claim_factory=lambda *_: registry)

        self.assertEqual("attention", result["status"])
        self.assertEqual("execution_record_missing_worker_process_live", result["recovery_reason"])
        self.assertIsNotNone(registry.document, "Claim must not be released while worker is live")
        stored = store.get("commands", "p1", "cmd-1")
        self.assertEqual("attention", stored["status"])

    # -------------------------------------------------------------------
    # Test C: PID reused / creation identity mismatch -> worker dead -> release + requeue
    # -------------------------------------------------------------------

    def test_C_pid_reused_creation_identity_mismatch_proves_worker_dead(self):
        store = self.allowlist_compliant_store()
        # Use our own live PID, but with an old/different creation identity (simulating PID reuse)
        cmd = self._orphan_claimed_command(worker_pid=os.getpid(), worker_creation_identity="old-defunct-identity:12345")
        store.put("commands", "p1", "cmd-1", cmd)

        registry = self._pre_seeded_claim_registry()
        result = process_command(store, object(), cmd, claim_factory=lambda *_: registry)

        self.assertEqual("queued", result["status"])
        self.assertTrue(result.get("orphan_claim_released"))
        self.assertIsNone(registry.document, "Claim must be released when creation identity mismatch proves dead")
        stored = store.get("commands", "p1", "cmd-1")
        self.assertEqual("queued", stored["status"])

    # -------------------------------------------------------------------
    # Test D: worker identity unknown -> fail closed (do not release)
    # -------------------------------------------------------------------

    def test_D_worker_identity_unknown_fails_closed(self):
        store = self.allowlist_compliant_store()
        # Legacy record without worker_pid / worker_creation_identity
        cmd = command(
            status="claimed",
            execution_id="command-cmd-1",
            claimed_at="2000-01-01T00:00:00Z",
        )
        store.put("commands", "p1", "cmd-1", cmd)

        registry = self._pre_seeded_claim_registry()
        result = process_command(store, object(), cmd, claim_factory=lambda *_: registry)

        self.assertEqual("attention", result["status"])
        self.assertEqual("execution_record_missing_claim_retained_worker_liveness_unknown", result["recovery_reason"])
        self.assertIsNotNone(registry.document, "Claim must not be released when worker liveness is unknown")
        stored = store.get("commands", "p1", "cmd-1")
        self.assertEqual("attention", stored["status"])

    # -------------------------------------------------------------------
    # Test E: matching Session/provider evidence exists -> do not release
    # -------------------------------------------------------------------

    def test_E_session_evidence_present_refuses_recovery(self):
        store = self.allowlist_compliant_store()
        cmd = self._orphan_claimed_command(worker_pid=99999999, worker_creation_identity="dead-worker")
        store.put("commands", "p1", "cmd-1", cmd)

        from manager.session_identity import manager_session_key
        session_id = manager_session_key("codex", "test-provider-session-001")
        store.put("sessions", "p1", session_id, {
            "session_id": session_id, "provider": "codex",
            "provider_session_id": "test-provider-session-001",
            "account_id": None,
            "project_id": "p1", "task_id": "t1",
            "conversation_label": None, "title": None, "summary": None,
            "started_at": "2000-01-01T00:00:00Z", "updated_at": "2000-01-01T00:00:00Z",
            "working_directory": None, "repository": None,
            "source_identifier": "test-provider-session-001",
            "source_path": None,
            "classification_method": "working_directory",
            "classification_confidence": "high",
            "classification_status": "classified",
            "status": "active", "message_count": 0,
            "model": None, "first_user_prompt": None,
        })

        registry = self._pre_seeded_claim_registry()
        result = process_command(store, object(), cmd, claim_factory=lambda *_: registry)

        self.assertEqual("attention", result["status"])
        self.assertEqual("execution_record_missing_session_evidence_present", result["recovery_reason"])
        self.assertIsNotNone(registry.document, "Claim must not be released when session evidence exists")
        stored = store.get("commands", "p1", "cmd-1")
        self.assertEqual("attention", stored["status"])

    # -------------------------------------------------------------------
    # Test F: newer claim generation / different execution owner -> refuse release
    # -------------------------------------------------------------------

    def test_F_newer_claim_owner_or_generation_refuses_stale_release(self):
        store = self.allowlist_compliant_store()
        cmd = self._orphan_claimed_command(worker_pid=99999999, worker_creation_identity="dead-worker")
        store.put("commands", "p1", "cmd-1", cmd)

        # Registry has a different execution_id (newer owner)
        registry = self._pre_seeded_claim_registry(execution_id="command-cmd-NEWER")
        result = process_command(store, object(), cmd, claim_factory=lambda *_: registry)

        self.assertEqual("attention", result["status"])
        self.assertEqual("execution_record_missing_claim_replaced_by_newer", result["recovery_reason"])
        self.assertIsNotNone(registry.document, "Claim owned by different execution must not be deleted")

    # -------------------------------------------------------------------
    # Test G: two concurrent reconcilers -> only one CAS wins
    # -------------------------------------------------------------------

    def test_G_concurrent_reconciliation_only_one_cas_wins(self):
        import threading
        store = self.allowlist_compliant_store()
        cmd = self._orphan_claimed_command(worker_pid=99999999, worker_creation_identity="dead-worker")
        store.put("commands", "p1", "cmd-1", cmd)

        registry = self._pre_seeded_claim_registry()
        results = []
        errors = []

        def reconcile():
            try:
                r = process_command(store, object(), cmd, claim_factory=lambda *_: registry)
                results.append(r["status"])
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=reconcile)
        t2 = threading.Thread(target=reconcile)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual([], errors, f"concurrent recovery raised: {errors}")
        self.assertIsNone(registry.document, "GCS claim must be deleted exactly once")
        self.assertIn("queued", results, "At least one reconciler must have successfully requeued")
        stored = store.get("commands", "p1", "cmd-1")
        self.assertEqual("queued", stored["status"])

    # -------------------------------------------------------------------
    # Test H: CAS backend transient error -> fail closed, do not requeue
    # -------------------------------------------------------------------

    def test_H_backend_unavailable_fails_closed_without_requeue(self):
        store = self.allowlist_compliant_store()
        cmd = self._orphan_claimed_command(worker_pid=99999999, worker_creation_identity="dead-worker")
        store.put("commands", "p1", "cmd-1", cmd)

        registry = self._pre_seeded_claim_registry()
        registry.unavailable = True  # simulate GCS 5xx / connection failure

        result = process_command(store, object(), cmd, claim_factory=lambda *_: registry)

        self.assertEqual("attention", result["status"])
        self.assertIn("claim", result["recovery_reason"])
        stored = store.get("commands", "p1", "cmd-1")
        self.assertNotEqual("queued", stored["status"], "Command must not be requeued on backend error")

    # -------------------------------------------------------------------
    # Test I: release succeeded -> next watcher tick can cleanly re-claim and execute
    # -------------------------------------------------------------------

    def test_I_requeued_command_executes_cleanly_on_next_tick(self):
        store = self.allowlist_compliant_store()
        cmd = self._orphan_claimed_command(worker_pid=99999999, worker_creation_identity="dead-worker")
        store.put("commands", "p1", "cmd-1", cmd)

        registry = self._pre_seeded_claim_registry()

        # Tick 1: Reconcile orphaned claim
        res1 = process_command(store, object(), cmd, claim_factory=lambda *_: registry)
        self.assertEqual("queued", res1["status"])
        self.assertIsNone(registry.document)

        # Tick 2: Next watcher poll cycle executes requeued command
        new_registry = MemoryClaimRegistry()
        runner = Mock(side_effect=lambda *args, **kwargs: (
            kwargs["on_running"](None),
            {"terminal": {"execution": {"status": "completed"}},
             "session": {"session_id": "codex:test-session-2"},
             "execution_id": args[7],
             "dispatch": {"provider": "codex", "model": None, "fallback_model": None,
                          "mode": "code", "effort": "medium",
                          "selection_reason": ["fresh quota"],
                          "quota_evidence": {"codex": {"freshness": "fresh"}},
                          "account_id": None}},
        )[1])

        with patch("manager.command_watcher.launch_task", runner):
            requeued = store.get("commands", "p1", "cmd-1")
            res2 = process_command(
                store, object(), requeued,
                claim_factory=lambda *_: new_registry,
                allowlist=self.ALLOWLIST,
                health_check=lambda: True,
                quota_check=lambda service: True,
            )

        self.assertEqual("completed", res2["status"])
        runner.assert_called_once()


class ClaimExpiredTimeBugTests(unittest.TestCase):
    """Latent operator-precedence bug in _claim_expired(command, now=...).

    `(now or datetime.now(timezone.utc) - claimed).total_seconds()` —
    when now is a datetime, Python parses this as
    `now or (datetime.now(timezone.utc) - claimed)`, so now.total_seconds()
    is called, which raises AttributeError (datetime has no .total_seconds).
    The except catches it and returns True (expired), making every claim look
    expired when now= is injected. This is a latent test-injection API bug.
    """

    def _make_command(self, claimed_at_iso):
        return command(
            status="claimed",
            execution_id="command-cmd-1",
            claimed_at=claimed_at_iso,
        )

    def test_not_expired_when_now_is_recent(self):
        """Passing a recent datetime as now= must return False (not expired)."""
        from manager.command_watcher import _claim_expired, CLAIM_TIMEOUT_SECONDS
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        claimed_at = now - timedelta(seconds=1)
        claimed_at_iso = claimed_at.isoformat().replace("+00:00", "Z")
        cmd = self._make_command(claimed_at_iso)
        result = _claim_expired(cmd, now=now)
        self.assertFalse(result, "_claim_expired(now=recent_datetime) must return False for a non-expired claim")

    def test_expired_when_now_is_far_future(self):
        """Passing an expired datetime as now= must return True."""
        from manager.command_watcher import _claim_expired, CLAIM_TIMEOUT_SECONDS
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        claimed_at = now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 1)
        claimed_at_iso = claimed_at.isoformat().replace("+00:00", "Z")
        cmd = self._make_command(claimed_at_iso)
        result = _claim_expired(cmd, now=now)
        self.assertTrue(result, "_claim_expired(now=...) must return True for a claim older than CLAIM_TIMEOUT_SECONDS")

    def test_none_now_uses_real_clock_not_expired(self):
        """The default now=None path must work correctly for recent claims."""
        from manager.command_watcher import _claim_expired
        cmd = self._make_command(now_iso())
        self.assertFalse(_claim_expired(cmd), "_claim_expired(now=None) must return False for a just-claimed command")

    def test_none_now_uses_real_clock_expired(self):
        """Default path for an old claim must still return True."""
        from manager.command_watcher import _claim_expired
        cmd = self._make_command("2000-01-01T00:00:00Z")
        self.assertTrue(_claim_expired(cmd), "_claim_expired(now=None) must return True for an ancient claim")


class TerminalPersistenceRetryTests(CommandWatcherTests):
    """Reproduces a real production defect observed twice live in the C
    Stability Gate campaign (round13 and round42): terminalize_execution()
    persists 'execution' and 'handoff' successfully, but the immediate
    write-then-readback verification of the Task write transiently
    mismatches (a real Drive/GCS eventual-consistency hiccup -- the
    underlying write is not corrupt, just not yet visible to the very next
    read), raising TaskError('terminal task persistence verification
    failed') from inside terminalize_execution's own try/except.
    _retain_terminal_authority() then durably records
    cleanup_evidence={persistence:'partial', persisted:['execution',
    'handoff'], task_claim_release:'retained'} -- correctly reflecting that
    the GCS claim genuinely was never released (cleanup_execution() is only
    ever reached after task persistence succeeds). recover_task_claim()
    correctly REFUSES to release the claim while persistence is incomplete
    (a real, intentional safety fence -- see execution_recovery.py). But
    nothing anywhere ever retries the one specific missing Task write, so
    _reconcile_active() falls to attention/'terminal_cleanup_not_confirmed'
    on every single tick forever: a real Task/Command stuck permanently,
    not a transient that clears on its own."""

    def _stuck_completed_execution(self):
        active, claim, execution = self.running_command()
        from manager.execution_lifecycle import terminalize_execution
        original_get = self.store.get
        stale_snapshot = self.store.get("tasks", "p1", "t1")
        state = {"n": 0}

        def flaky_get(area, project_id, name):
            if area == "tasks" and project_id == "p1" and name == "t1":
                n = state["n"]
                state["n"] += 1
                # 3 "tasks" reads happen inside terminalize_execution: #0 is
                # its own initial fetch (execution_lifecycle.py); #1 is a
                # hidden nested read inside create_handoff()'s
                # completion-report validation (manager/tasks.py); #2 is the
                # actual write-then-readback verification right after
                # store.put() (execution_lifecycle.py) -- that is the one a
                # real transient eventual-consistency glitch would hit.
                if n == 2:
                    return deepcopy(stale_snapshot)
            return original_get(area, project_id, name)

        self.store.get = flaky_get
        try:
            with self.assertRaises(TaskError), \
                 patch("manager.executions.read_drive_status", return_value=quota_document()):
                terminalize_execution(
                    self.store, object(), None, claim, "p1", "t1", "command-cmd-1",
                    "codex", "completed", claim.generation, True,
                )
        finally:
            self.store.get = original_get
        execution = self.store.get("executions", "p1", "command-cmd-1")
        evidence = execution["cleanup_evidence"]
        self.assertEqual("partial", evidence["persistence"])
        self.assertEqual(["execution", "handoff"], evidence["persisted"])
        self.assertEqual("retained", evidence["task_claim_release"])
        self.assertIsNotNone(claim.document, "claim must genuinely still be held -- persistence never completed")
        return active, claim, execution

    def test_stuck_completed_execution_reproduces_real_cleanup_evidence_shape(self):
        """Sanity check on the reproduction fixture itself, independent of
        _reconcile_active -- confirms the setup matches round42's live
        evidence exactly (see project memory: round42 execution had
        persistence='partial', persisted=['execution','handoff'],
        task_claim_release='retained')."""
        self._stuck_completed_execution()

    def test_reconcile_active_recovers_a_stuck_partial_persistence(self):
        """The behavior this hotfix adds: _reconcile_active() must retry the
        specific missing Task write (not just refuse to release the claim
        forever) so a transient readback glitch self-heals on a later tick,
        exactly like every other transient recovery_reason in this file.
        Before the fix, this assertion fails: it stays 'attention' forever
        (see test_current_behavior_never_recovers_without_the_fix below,
        preserved as a permanent regression guard)."""
        active, claim, _ = self._stuck_completed_execution()
        result = _reconcile_active(self.store, object(), active, lambda *_: claim)
        self.assertEqual("completed", result["status"])
        self.assertTrue(result.get("reconciled"))
        # Strengthened Design A: once persistence genuinely completes, the
        # terminal proposal is CAS-bound as this task's durable winner, so
        # the runtime claim is released (authority_active=False) WITHOUT
        # deleting the Task Root object -- that bind is exactly the
        # durable terminal authority this architecture exists to keep.
        self.assertIsNotNone(claim.document, "a genuine terminal bind must never be deleted")
        self.assertFalse(claim.document["authority_active"], "claim must be released once persistence genuinely completes")
        self.assertEqual("command-cmd-1", claim.document["terminal"]["execution_id"])
        task = self.store.get("tasks", "p1", "t1")
        self.assertEqual("completed", task["status"])
        execution = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual("complete", execution["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], execution["cleanup_evidence"]["persisted"])
        self.assertEqual("released", execution["cleanup_evidence"]["task_claim_release"])
        command = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("completed", command["status"])

    def test_two_ticks_without_a_retry_helper_would_stay_stuck_forever(self):
        """Documents the exact pre-fix symptom for posterity: calling
        _reconcile_active() repeatedly with the retry helper DISABLED makes
        zero progress across ticks -- the real production shape (round13,
        round42), not a one-tick transient. Guards against a future
        regression that reintroduces a silent no-retry path."""
        active, claim, _ = self._stuck_completed_execution()
        with patch("manager.command_watcher.retry_incomplete_terminal_persistence", return_value=False):
            first = _reconcile_active(self.store, object(), active, lambda *_: claim)
            self.assertEqual("attention", first["status"])
            self.assertEqual("terminal_cleanup_not_confirmed", first["recovery_reason"])
            second = _reconcile_active(self.store, object(), self.store.get("commands", "p1", "cmd-1"), lambda *_: claim)
            self.assertEqual("attention", second["status"])
            self.assertEqual("terminal_cleanup_not_confirmed", second["recovery_reason"])
        self.assertIsNotNone(claim.document, "claim must never be released while persistence stays incomplete")
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])


class RetryIncompleteTerminalPersistenceTests(CommandWatcherTests):
    """Unit coverage for the new helper directly, independent of
    _reconcile_active's own control flow."""

    def test_completes_a_missing_task_write_and_marks_persistence_complete(self):
        from manager.execution_lifecycle import retry_incomplete_terminal_persistence
        active, claim, execution = self.running_command()
        execution["status"] = "completed"
        execution["completed_at"] = now_iso()
        execution["notes"] = ["codex turn completed"]
        execution["cleanup_evidence"] = {
            "provider_outcome": "completed", "persistence": "partial",
            "persisted": ["execution", "handoff"], "writer_release": "not_required",
            "task_claim_release": "retained", "errors": ["persistence failed: terminal task persistence verification failed"],
        }
        self.store.put("executions", "p1", "command-cmd-1", execution)
        result = retry_incomplete_terminal_persistence(self.store, "p1", "t1", "command-cmd-1")
        self.assertTrue(result)
        refreshed = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual("complete", refreshed["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], refreshed["cleanup_evidence"]["persisted"])
        # task_claim_release is untouched here -- releasing the actual GCS
        # claim is _reconcile_active's/recover_task_claim's job, not this
        # helper's; it only fixes the specific incomplete persistence step.
        self.assertEqual("retained", refreshed["cleanup_evidence"]["task_claim_release"])
        task = self.store.get("tasks", "p1", "t1")
        self.assertEqual("completed", task["status"])

    def test_is_a_noop_when_already_fully_persisted(self):
        from manager.execution_lifecycle import retry_incomplete_terminal_persistence
        active, claim, execution = self.running_command()
        execution["status"] = "completed"
        execution["completed_at"] = now_iso()
        execution["cleanup_evidence"] = {
            "provider_outcome": "completed", "persistence": "complete",
            "persisted": ["execution", "handoff", "task"], "writer_release": "not_required",
            "task_claim_release": "released", "errors": [],
        }
        self.store.put("executions", "p1", "command-cmd-1", execution)
        before = self.store.get("executions", "p1", "command-cmd-1")
        result = retry_incomplete_terminal_persistence(self.store, "p1", "t1", "command-cmd-1")
        self.assertTrue(result)
        after = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual(before, after, "must not touch an already-complete execution")

    def test_returns_false_and_leaves_state_untouched_when_task_write_keeps_failing(self):
        from manager.execution_lifecycle import retry_incomplete_terminal_persistence
        active, claim, execution = self.running_command()
        execution["status"] = "completed"
        execution["completed_at"] = now_iso()
        execution["notes"] = ["codex turn completed"]
        execution["cleanup_evidence"] = {
            "provider_outcome": "completed", "persistence": "partial",
            "persisted": ["execution", "handoff"], "writer_release": "not_required",
            "task_claim_release": "retained", "errors": ["persistence failed: terminal task persistence verification failed"],
        }
        self.store.put("executions", "p1", "command-cmd-1", execution)
        before = self.store.get("executions", "p1", "command-cmd-1")
        original_put = self.store.put

        def failing_put(area, project_id, name, document):
            if area == "tasks":
                raise TaskError("simulated persistent Drive outage")
            return original_put(area, project_id, name, document)

        self.store.put = failing_put
        try:
            result = retry_incomplete_terminal_persistence(self.store, "p1", "t1", "command-cmd-1")
        finally:
            self.store.put = original_put
        self.assertFalse(result)
        after = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual(before, after, "a failed retry must not leave partially-updated cleanup_evidence behind")

    def test_returns_false_for_a_running_execution(self):
        from manager.execution_lifecycle import retry_incomplete_terminal_persistence
        active, claim, execution = self.running_command()
        self.assertEqual("running", execution["status"])
        result = retry_incomplete_terminal_persistence(self.store, "p1", "t1", "command-cmd-1")
        self.assertFalse(result)


if __name__ == "__main__": unittest.main()
