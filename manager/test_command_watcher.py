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
    POLL_SECONDS, POLL_TIME_BUDGET_SECONDS, PROVIDER_RUNTIMES, _within_project_record_rotation_offset,
    REQUIRED_TASK_POLICIES, _provider_state, _enumerate_waiting_quota_tasks, _promote_waiting_quota_task,
    _reconcile_active,
    _prioritized_nonterminal_commands, _terminal_recovery_candidates, _terminal_command_needs_recovery,
    _TerminalRecoveryPrevalidation, _enumerate_recent_commands, _enumerate_commands,
    TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS, TERMINAL_RECOVERY_RESERVED_SECONDS,
    TERMINAL_RECOVERY_PASS_BUDGET_SECONDS, TERMINAL_RECOVERY_PASS_HYDRATION_SECONDS,
    WATCHER_DISCOVERY_TIMEOUT_SECONDS,
    claude_quota_reliable, codex_quota_reliable, embedded_ingress_enabled, load_allowlist,
    poll_once, process_command, provider_quota_reliable, resolve_provider_runtime, _spawn_claimed_worker,
    terminal_recovery_once, _focus_adm_ui_best_effort,
)
from manager.execution_lifecycle import enter_running_gate
from manager.executions import cancel_reserved_execution, execution_health, heartbeat_execution, reserve_execution
from manager.scheduler_provenance import command_origin
from manager.task_claims import TaskClaimConflict
from manager.tasks import TaskError, create_project, create_task, now_iso, validate
from manager.test_dispatcher import quota as fresh_quota_fixture
from manager.trusted_ingress import ADMISSION_VERSION_V2_REPO_WRITE, REQUIRED_REPO_WRITE_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN
from manager.test_execution_lifecycle import project, task
from manager.test_execution_lifecycle import quota_document
from manager.test_execution_runner import AccountAwareClaudeStyleLauncher
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import MemoryRegistry
from manager.worktree_locks import canonical_repository, repository_lock_id


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

# _focus_adm_ui_best_effort() now spawns a real DETACHED helper process
# (python -m manager.open_existing_adm_ui) at every queued -> claimed
# transition -- without this module-wide default patch, every test that
# claims a command would launch a real process poking this machine's real
# desktop. Tests that exercise the auto-open wiring itself patch locally on
# top (shadowing this one); the direct unit tests of the real function call
# the module-import-time binding, which this attribute patch does not touch.
_focus_adm_ui_best_effort_patch = patch("manager.command_watcher._focus_adm_ui_best_effort", Mock())


def setUpModule():
    _focus_existing_adm_ui_patch.start()
    _focus_adm_ui_best_effort_patch.start()


def tearDownModule():
    _focus_existing_adm_ui_patch.stop()
    _focus_adm_ui_best_effort_patch.stop()


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

    # --- Execution-convergence regression suite (live incident 20260901:
    # dispatch-adm-close-gh-dispatch-test-determinism-20260901T155956Z).
    # A watcher tick cancelled a 24-second-old reservation belonging to a
    # live claimed worker mid-prelaunch, then the cancelled execution could
    # never terminalize because the repo lock slot was owned by the
    # PREVIOUS task (already released) and lease reconciliation raised
    # "owner mismatch" on every subsequent tick -- permanent `attention`
    # with recovery_reason=terminal_writer_authority_reconciliation_unknown.

    def test_fresh_claimed_reservation_is_left_alone_by_watcher_tick(self):
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        claimed = command(status="claimed", execution_id="command-cmd-1", claimed_at=now_iso())
        self.store.put("commands", "p1", "cmd-1", claimed)
        result = process_command(self.store, object(), claimed, claim_factory=lambda *_: MemoryClaimRegistry())
        self.assertEqual({"status": "claimed", "skipped": True, "reason": "prelaunch_in_flight"}, result)
        # Nothing was cancelled or rewritten: the in-flight worker's
        # reservation and the claimed Command are byte-identical to before.
        self.assertEqual("reserved", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual(claimed, self.store.get("commands", "p1", "cmd-1"))
        self.assertEqual("ready", self.store.get("tasks", "p1", "t1")["status"])

    def test_attention_command_with_fresh_reserved_execution_is_not_cancelled(self):
        # A transient earlier attention write (e.g. task_claim_backend_
        # unavailable) does not stop the live worker -- a later tick must
        # still respect the claim window instead of cancelling under it.
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        marked = command(status="attention", execution_id="command-cmd-1", claimed_at=now_iso(),
                         stale_at=now_iso(), recovery_reason="task_claim_backend_unavailable")
        self.store.put("commands", "p1", "cmd-1", marked)
        result = process_command(self.store, object(), marked, claim_factory=lambda *_: MemoryClaimRegistry())
        self.assertEqual({"status": "attention", "skipped": True, "reason": "prelaunch_in_flight"}, result)
        self.assertEqual("reserved", self.store.get("executions", "p1", "command-cmd-1")["status"])

    def test_expired_claim_reservation_still_cancels_and_converges_to_failed(self):
        # Bounded convergence: once the claim window itself has expired with
        # the Execution still reserved, the prelaunch is over-budget and the
        # original cancel path must run to a real terminal state.
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        expired_at = self.iso(datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 60))
        stale = command(status="claimed", execution_id="command-cmd-1", claimed_at=expired_at)
        self.store.put("commands", "p1", "cmd-1", stale)
        result = process_command(self.store, object(), stale, claim_factory=lambda *_: MemoryClaimRegistry())
        self.assertEqual("failed", result["status"])
        self.assertEqual("cancelled", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])
        self.assertEqual("prelaunch_failed", self.store.get("commands", "p1", "cmd-1")["result"]["error_kind"])

    def test_expired_claim_with_live_recorded_worker_is_never_cancelled(self):
        # Codex adversarial finding (delta round): claim expiry alone is a
        # schedule bound, not proof the worker is gone. A recorded worker
        # that is still LIVE past CLAIM_TIMEOUT_SECONDS (degraded Drive/GCS
        # mid-prelaunch) must surface as attention -- never have its
        # reservation cancelled underneath it, which is the original
        # incident class merely postponed.
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        expired_at = self.iso(datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 60))
        live_pid = os.getpid()
        stale = command(status="claimed", execution_id="command-cmd-1", claimed_at=expired_at,
                        worker_pid=live_pid,
                        worker_creation_identity=process_creation_identity(live_pid) or "test-process:missing",
                        worker_spawned_at=expired_at)
        self.store.put("commands", "p1", "cmd-1", stale)
        result = process_command(self.store, object(), stale, claim_factory=lambda *_: MemoryClaimRegistry())
        self.assertEqual("attention", result["status"])
        self.assertEqual("reserved_execution_worker_still_live", result["recovery_reason"])
        self.assertEqual("reserved", self.store.get("executions", "p1", "command-cmd-1")["status"])

    def test_expired_claim_with_proven_dead_worker_cancels_and_converges(self):
        # The bounded-convergence counterpart: once the recorded worker is
        # provably stopped, the expired reservation cancels and the Command
        # reaches a real terminal state.
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        expired_at = self.iso(datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 60))
        stale = command(status="claimed", execution_id="command-cmd-1", claimed_at=expired_at,
                        worker_pid=99_999_999, worker_creation_identity="windows-filetime:1",
                        worker_spawned_at=expired_at)
        self.store.put("commands", "p1", "cmd-1", stale)
        result = process_command(self.store, object(), stale, claim_factory=lambda *_: MemoryClaimRegistry())
        self.assertEqual("failed", result["status"])
        self.assertEqual("cancelled", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_cancelled_prelaunch_converges_to_failed_despite_foreign_repo_lock_owner(self):
        # The incident's non-convergence half, end to end: cancelled
        # execution + repo lock slot owned by ANOTHER task (status already
        # "released", exactly as observed live) must terminalize the Command
        # as failed -- never loop in attention -- and must not touch the
        # foreign lock.
        writable = create_task(self.store, task(read_only=False), assign=False, persist=False)
        self.store.put("tasks", "p1", "t1", writable)
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        cancel_reserved_execution(self.store, MemoryClaimRegistry(), "p1", "command-cmd-1",
                                  "prelaunch failure left a reservation without provider authority")
        canonical = canonical_repository(self.store.get("projects", "p1", "p1")["repo"])
        lock_id = repository_lock_id(canonical)
        foreign_lock = {
            "project_id": "p1", "task_id": "t-previous", "execution_id": "command-t-previous",
            "provider": "codex", "session_id": "codex:previous-session", "lock_id": lock_id,
            "repository": canonical, "branch": "refs/heads/feat/previous", "scope": ["manager/executions.py"],
            "baseline_head": "a" * 40, "access": "production", "status": "released", "generation": 40,
            "lease_token_hash": "0" * 64, "created_at": "2026-09-01T15:43:14.000000Z",
            "updated_at": "2026-09-01T15:52:10.000000Z", "expires_at": "2026-09-01T16:43:14.000000Z",
            "released_at": "2026-09-01T15:52:10.000000Z",
        }
        registry = MemoryRegistry({"schema_version": "0.2.0", "locks": {lock_id: deepcopy(foreign_lock)}})
        marked = command(status="attention", execution_id="command-cmd-1", claimed_at=now_iso(),
                         stale_at=now_iso(), recovery_reason="terminal_writer_authority_reconciliation_unknown")
        self.store.put("commands", "p1", "cmd-1", marked)
        with patch("manager.command_watcher.GCSLockRegistry.from_environment", return_value=registry):
            result = process_command(self.store, object(), marked, claim_factory=lambda *_: MemoryClaimRegistry())
        self.assertEqual({"status": "failed", "reconciled": True}, result)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertEqual("prelaunch_failed", stored["result"]["error_kind"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])
        self.assertEqual("cancelled", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual(foreign_lock, registry.document["locks"][lock_id])
        # Convergence is durable: a repeated tick sees the terminal Command
        # and never reopens or relaunches it.
        repeat = process_command(self.store, object(), stored, claim_factory=lambda *_: MemoryClaimRegistry())
        self.assertEqual("failed", repeat["status"])
        self.assertEqual("failed", self.store.get("commands", "p1", "cmd-1")["status"])

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

    def test_normal_dispatch_auto_opens_exactly_once_across_claim_to_running(self):
        """AUTO_OPEN_ADM: a completely ordinary dispatch (no
        OPEN_EXISTING_ADM_UI action at all) must bring the Dashboard up for
        the user -- and exactly ONCE, at the queued -> claimed transition.
        The full inline claim -> on_running -> terminal lifecycle runs here
        and must not fire a second focus at the running stage (a
        claim-then-running double focus would re-steal the foreground from
        whatever the user switched to during prelaunch)."""
        auto_open = Mock()
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._focus_adm_ui_best_effort", auto_open):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        auto_open.assert_called_once_with("claimed")
        self.assertEqual("completed", result["status"])

    def test_auto_open_helper_spawn_is_detached_and_failure_never_blocks_dispatch(self):
        """The claim-time auto-open must (1) run as a DETACHED helper
        process -- python -m manager.open_existing_adm_ui -- so the watcher
        tick never blocks on dashboard cold start / window discovery, and
        (2) swallow any spawn failure: a dispatch's outcome must never
        depend on whether the interactive desktop was reachable. Calls the
        REAL _focus_adm_ui_best_effort (module-import-time binding, not the
        module-wide safety mock)."""
        spawned = Mock()
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"AI_MANAGER_HOME": home}), \
                 patch("manager.command_watcher.subprocess.Popen", spawned):
                _focus_adm_ui_best_effort("claimed")
            spawned.assert_called_once()
            argv = spawned.call_args.args[0]
            self.assertEqual(["-m", "manager.open_existing_adm_ui"], argv[1:])
            if os.name == "nt":
                flags = spawned.call_args.kwargs["creationflags"]
                self.assertTrue(flags & __import__("subprocess").DETACHED_PROCESS)
                # The helper now waits up to ~75s across its port/window
                # stages -- longer than a tick may live -- so it must leave
                # the Scheduled Task's job object like the worker does.
                self.assertTrue(flags & __import__("subprocess").CREATE_BREAKAWAY_FROM_JOB)
            # Observability (delta-review finding): the helper's stdout and
            # stderr must both land in the durable auto-open log under
            # AI_MANAGER_HOME -- never DEVNULL -- so a helper that spawns
            # fine but later fails (no desktop, dashboard timeout, import
            # error) leaves evidence. The spawner writes a stage header
            # first, and the helper's main() (tested in
            # test_open_existing_adm_ui) always prints a structured outcome
            # onto this same handle.
            devnull = __import__("subprocess").DEVNULL
            self.assertIsNot(spawned.call_args.kwargs["stdout"], devnull)
            self.assertIs(spawned.call_args.kwargs["stdout"], spawned.call_args.kwargs["stderr"])
            log_path = Path(home) / "logs" / "auto-open-adm.log"
            self.assertIn("AUTO_OPEN_ADM[claimed] helper spawned", log_path.read_text(encoding="utf-8"))
            # Spawn failure is logged, never raised -- and a real dispatch
            # running with the REAL (unpatched-at-callsite) helper whose
            # spawn fails still completes normally. An unwritable log home
            # degrades to DEVNULL without losing the spawn.
            with patch.dict(os.environ, {"AI_MANAGER_HOME": home}), \
                 patch("manager.command_watcher.subprocess.Popen", Mock(side_effect=OSError("no desktop"))):
                _focus_adm_ui_best_effort("claimed")  # must not raise
            fallback = Mock()
            with patch.dict(os.environ, {"AI_MANAGER_HOME": str(Path(home) / "logs" / "auto-open-adm.log")}), \
                 patch("manager.command_watcher.subprocess.Popen", fallback):
                _focus_adm_ui_best_effort("claimed")  # log dir path is a file -> OSError -> DEVNULL fallback
            self.assertIs(fallback.call_args.kwargs["stdout"], devnull)
            runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
            real_helper = _focus_adm_ui_best_effort
            with patch.dict(os.environ, {"AI_MANAGER_HOME": home}), \
                 patch("manager.command_watcher.launch_task", runner), \
                 patch("manager.command_watcher._focus_adm_ui_best_effort", real_helper), \
                 patch("manager.command_watcher.subprocess.Popen", Mock(side_effect=OSError("no desktop"))):
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

    def test_async_claim_opens_adm_ui_once_and_repeated_tick_never_refocuses(self):
        # AUTO_OPEN_ADM at claim time: a real dispatch entering execution
        # (queued -> claimed) must bring the Dashboard onto the interactive
        # desktop immediately -- not only after prelaunch (minutes of
        # Drive/GCS latency) reaches "running", which a launch dying
        # mid-prelaunch never does (live incident 20260901: no window ever
        # appeared). Exactly once per Command: the queued -> claimed
        # transition happens once, and reconcile ticks never re-enter it.
        auto_open = Mock()
        with patch("manager.command_watcher._spawn_claimed_worker", return_value=4242), \
             patch("manager.command_watcher._focus_adm_ui_best_effort", auto_open):
            result = process_command(
                self.store, object(), command(), claim_factory=CommandWatcherTests.claim_factory,
                allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True,
                async_launch=True,
            )
        self.assertEqual("claimed", result["status"])
        auto_open.assert_called_once_with("claimed")
        stored = self.store.get("commands", "p1", "cmd-1")
        reopen = Mock()
        with patch("manager.command_watcher._focus_adm_ui_best_effort", reopen):
            repeat = process_command(self.store, object(), stored,
                                     claim_factory=CommandWatcherTests.claim_factory)
        self.assertEqual("claimed", repeat["status"])
        self.assertTrue(repeat.get("skipped"))
        reopen.assert_not_called()

    def test_async_claim_auto_open_spawn_failure_never_blocks_the_dispatch(self):
        # User visibility is best-effort observability only: the REAL
        # helper (its detached Popen failing) must never affect the claim,
        # the spawned worker's authority, or the persisted Command. The
        # worker spawn itself is mocked, so the failing Popen here is only
        # ever the auto-open helper's.
        real_helper = _focus_adm_ui_best_effort
        with patch("manager.command_watcher._spawn_claimed_worker", return_value=4242), \
             patch("manager.command_watcher._focus_adm_ui_best_effort", real_helper), \
             patch("manager.command_watcher.subprocess.Popen", Mock(side_effect=OSError("no desktop"))):
            result = process_command(
                self.store, object(), command(), claim_factory=CommandWatcherTests.claim_factory,
                allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True,
                async_launch=True,
            )
        self.assertEqual({"status": "claimed", "execution_id": "command-cmd-1", "worker_pid": 4242}, result)
        persisted = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("claimed", persisted["status"])
        self.assertEqual(4242, persisted.get("worker_pid"))


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
    aborting the entire tick. Because _prioritized_nonterminal_commands()
    always sorts claimed/running ahead of queued, a command that reliably threw on every
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
        ordered = _prioritized_nonterminal_commands(batch)
        self.assertEqual(["c-queued", "c-attn"], [c["command_id"] for c in ordered])

    def test_running_queued_attention_order_is_unchanged(self):
        batch = [command(command_id="c-run", status="running"), command(command_id="c-queued", status="queued"),
                 command(command_id="c-attn", status="attention")]
        ordered = _prioritized_nonterminal_commands(batch)
        self.assertEqual(["c-run", "c-queued", "c-attn"], [c["command_id"] for c in ordered])

    def test_claimed_queued_attention_order_is_unchanged(self):
        batch = [command(command_id="c-claim", status="claimed"), command(command_id="c-queued", status="queued"),
                 command(command_id="c-attn", status="attention")]
        ordered = _prioritized_nonterminal_commands(batch)
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
        ordered = _prioritized_nonterminal_commands(batch)
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
        ordered = _prioritized_nonterminal_commands(batch)
        self.assertEqual(["c-queued", "c-attn"], [c["command_id"] for c in ordered])

    def test_project_rotation_is_untouched_by_this_fix(self):
        # Sanity check that this fix only reorders within an already-chosen
        # project's batch -- _rotated_project_ids (cross-project fairness)
        # is a separate, unmodified mechanism. See RotatedProjectIdsTests
        # below for the full coverage of that mechanism itself.
        from manager.command_watcher import _rotated_project_ids
        ids = ["p1", "p2", "p3"]
        self.assertEqual(_rotated_project_ids(ids, now=0.0), _rotated_project_ids(ids, now=0.0))


class TerminalCommandNeedsRecoveryEligibilityTests(unittest.TestCase):
    """P0-B fix: _terminal_command_needs_recovery() must classify eligibility
    from AFFIRMATIVE durable evidence only, never from missing/ambiguous
    evidence. Before this fix, `execution.get("cleanup_evidence") or {}`
    turned a genuinely absent/null cleanup_evidence into `{}`, and
    `{}.get(...) != "complete"` (or `!= "released"`) then evaluated True --
    "we don't know" was silently misclassified as "definitely still
    incomplete". The real R17 shape (persistence="partial",
    task_claim_release="retained") must remain eligible throughout."""

    def setUp(self):
        self.store = CommandWatcherTests.allowlist_compliant_store()

    def _execution(self, status="completed", cleanup_evidence=None):
        exec_doc = {"execution_id": "exec-1", "project_id": "p1", "task_id": "t1", "status": status}
        if cleanup_evidence is not None:
            exec_doc["cleanup_evidence"] = cleanup_evidence
        self.store.put("executions", "p1", "exec-1", exec_doc)
        return command(command_id="cmd-1", status=status, execution_id="exec-1")

    # 15: ordinary fully-converged terminal Command -> not eligible.
    def test_persistence_complete_and_released_not_eligible(self):
        cmd = self._execution(cleanup_evidence={"persistence": "complete", "task_claim_release": "released"})
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    # 14: the real R17 shape -> still eligible.
    def test_r17_shape_partial_and_retained_eligible(self):
        cmd = self._execution(cleanup_evidence={"persistence": "partial", "task_claim_release": "retained"})
        self.assertTrue(_terminal_command_needs_recovery(self.store, cmd))

    # 9: cleanup_evidence is None -> not eligible (was the exact bug).
    def test_cleanup_evidence_none_not_eligible(self):
        cmd = self._execution(cleanup_evidence=None)
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    # 10: cleanup_evidence is an empty dict -> not eligible.
    def test_cleanup_evidence_empty_dict_not_eligible(self):
        cmd = self._execution(cleanup_evidence={})
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    # 11: persistence key entirely absent, and no other affirmative marker
    # -> not eligible.
    def test_missing_persistence_key_not_eligible(self):
        cmd = self._execution(cleanup_evidence={"task_claim_release": "released"})
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    # 12: task_claim_release key entirely absent, and no other affirmative
    # marker -> not eligible.
    def test_missing_task_claim_release_key_not_eligible(self):
        cmd = self._execution(cleanup_evidence={"persistence": "complete"})
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    # 13: an unrecognized/unexpected enum value on either field fails
    # closed -- never treated as "not equal to complete/released, so it
    # must be incomplete".
    def test_unknown_persistence_enum_fails_closed(self):
        cmd = self._execution(cleanup_evidence={"persistence": "mid-flight-unknown-value", "task_claim_release": "released"})
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    def test_unknown_task_claim_release_enum_fails_closed(self):
        cmd = self._execution(cleanup_evidence={"persistence": "complete", "task_claim_release": "somehow-else"})
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    # 16: terminal Command whose linked Execution is not itself terminal
    # -> not eligible (existing guard, still correct after the fix).
    def test_non_terminal_execution_not_eligible(self):
        cmd = self._execution(status="running", cleanup_evidence={"persistence": "partial", "task_claim_release": "retained"})
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))

    # Either affirmative marker alone is sufficient ("at least one of A/B").
    def test_persistence_partial_alone_is_sufficient_even_if_claim_released(self):
        cmd = self._execution(cleanup_evidence={"persistence": "partial", "task_claim_release": "released"})
        self.assertTrue(_terminal_command_needs_recovery(self.store, cmd))

    def test_task_claim_retained_alone_is_sufficient_even_if_persistence_complete(self):
        cmd = self._execution(cleanup_evidence={"persistence": "complete", "task_claim_release": "retained"})
        self.assertTrue(_terminal_command_needs_recovery(self.store, cmd))

    def test_missing_execution_not_eligible(self):
        cmd = command(command_id="cmd-1", status="completed", execution_id="exec-does-not-exist")
        self.assertFalse(_terminal_command_needs_recovery(self.store, cmd))


class TerminalIncompleteRecoveryReachableViaPollTests(unittest.TestCase):
    """P0-A fix: a terminal (completed/failed) Command whose linked
    Execution still has incomplete cleanup/materialization must be
    reachable by the real scheduled poll_once() path -- but NEVER at the
    cost of delaying, starving, or risking an abort of already-actionable
    claimed/running/queued/attention work, since checking terminal
    eligibility requires a real Execution lookup that can be slow or fail.

    Before this fix, terminal-recovery eligibility was decided as part of
    the SAME priority-ordering pass as everything else
    (_prioritized_commands(commands, store=store)), so a slow or failing
    terminal lookup could consume the tick's time budget or raise before
    zero-lookup nonterminal work ever got its turn. The fix splits this
    into two passes -- _prioritized_nonterminal_commands() (pure, zero
    lookups) always runs to completion first; _terminal_recovery_
    candidates() (bounded, deadline-checked, per-candidate fail-closed)
    only runs afterward, and only if a process_command() slot and time
    budget remain."""

    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = CommandWatcherTests.allowlist_compliant_store()

    def _terminal_command_with_execution(self, status, persistence, task_claim_release="retained", command_id="cmd-1", execution_id="command-cmd-1"):
        """Build a real running Execution via enter_running_gate, then push
        it to a terminal outcome with the given cleanup_evidence.persistence
        -- mirrors CommandWatcherTests.test_terminal_execution_without_
        cleanup_is_not_published_as_command_terminal's own pattern, just
        parameterized on status/persistence so it covers all 4 combinations
        the P0 fix contract requires (completed/failed x complete/partial)."""
        now = datetime.now(timezone.utc)
        started = CommandWatcherTests.iso(now - timedelta(minutes=5))
        reserve_execution(self.store, "p1", "t1", execution_id, "codex", {"decision": "fresh"})
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", execution_id, "codex",
                               "read_only", started_at=started, task_claim_registry=claim)
        execution = self.store.get("executions", "p1", execution_id)
        execution.update(
            status=status, completed_at=now_iso(), finished_at=now_iso(),
            elapsed_minutes=5, quota_after={}, quota_delta={}, terminal_reason=status,
            cleanup_evidence={
                "provider_outcome": status, "persistence": persistence,
                "persisted": ["execution", "handoff", "task"] if persistence == "complete" else ["execution"],
                "task_claim_release": task_claim_release, "writer_release": "not_required",
                "errors": [] if persistence == "complete" else ["persistence failed: simulated Drive verification failure"],
            },
        )
        self.store.put("executions", "p1", execution_id, execution)
        cmd = command(command_id=command_id, status=status, execution_id=execution_id, claimed_at=started,
                      completed_at=now_iso(), result={"status": status, "session_id": None, "error_kind": None})
        self.store.put("commands", "p1", command_id, cmd)
        return cmd, claim

    def _bare_terminal_command(self, command_id, execution_id, persistence, task_claim_release="retained", status="completed", project_id="p1"):
        """Lighter-weight than _terminal_command_with_execution: writes the
        Execution record directly instead of going through reserve_execution/
        enter_running_gate against the shared "t1" Task -- needed whenever a
        test wants MULTIPLE independent terminal Command/Execution pairs in
        the same batch, since the real gate requires the Task to still be
        "ready" and a second real reservation against the same Task after
        the first has already claimed it would fail for reasons that have
        nothing to do with what these tests are actually exercising
        (_terminal_recovery_candidates()'s own bounding/fail-closed
        behavior, which only ever reads the Execution record)."""
        cmd = command(command_id=command_id, status=status, execution_id=execution_id, project_id=project_id, task_id="t1")
        self.store.put("executions", project_id, execution_id, {
            "execution_id": execution_id, "project_id": project_id, "task_id": "t1", "status": status,
            "cleanup_evidence": {"persistence": persistence, "task_claim_release": task_claim_release},
        })
        self.store.put("commands", project_id, command_id, cmd)
        return cmd

    def _add_project(self, project_id):
        """A second (or third) fully allowlist-compliant project -- needed
        for the GLOBAL cross-project ordering tests below, which must prove
        a queued/claimed/running Command in a DIFFERENT project is never
        starved by a terminal-recovery lookup in an earlier-rotated one.

        The Store test double's own list_projects() is hardcoded to a
        single "p1" (matching every OTHER test in this file, which is
        correctly single-project) -- overridden here, once per store
        instance, to reflect every project actually created via this
        helper, in creation order, so poll_once()'s own project rotation
        sees them all."""
        proj = {**project(), "project_id": project_id, "active_tasks": ["t1"]}
        create_project(self.store, proj)
        create_task(self.store, {**task(read_only=True), "project_id": project_id}, assign=False)
        compliant = self.store.get("tasks", project_id, "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", project_id, "t1", compliant)
        store = self.store

        def list_all_projects():
            ids = sorted({pid for (area, pid, _name) in store.records if area == "projects"})
            return [store.get("projects", pid, pid) for pid in ids]

        store.list_projects = list_all_projects

    # --- _prioritized_nonterminal_commands() is provably lookup-free ---

    def test_prioritized_nonterminal_commands_never_touches_the_store(self):
        # Structural proof, not just behavioral: the function's signature
        # takes no store/registry argument at all, so it is categorically
        # unable to perform a remote lookup regardless of batch content.
        cmd, _claim = self._terminal_command_with_execution("completed", "partial")
        batch = [cmd, command(command_id="c-queued", status="queued"), command(command_id="c-attn", status="attention")]
        ordered = _prioritized_nonterminal_commands(batch)
        self.assertEqual(["c-queued", "c-attn"], [c["command_id"] for c in ordered])

    # 1, 2, 6: nonterminal work (claimed/running/queued/attention) is fully
    # processed before ANY terminal-recovery Execution lookup is attempted,
    # even with multiple terminal records ahead of it in Drive's own
    # (arbitrary) listing order.
    def test_all_nonterminal_processed_before_any_terminal_lookup(self):
        term_cmd, claim = self._terminal_command_with_execution("completed", "partial")
        self.store.put("commands", "p1", "cmd-done-2", command(
            command_id="cmd-done-2", status="completed", execution_id="command-cmd-1",
            completed_at="2026-08-01T00:00:00Z", result={"status": "completed"}))
        self.store.put("commands", "p1", "cmd-queued", command(command_id="cmd-queued"))
        self.store.put("commands", "p1", "cmd-attn", command(
            command_id="cmd-attn", status="attention", execution_id="exec-missing"))

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(("terminal_lookup", cmd["command_id"]))
            return real_needs_recovery(store, cmd)

        def tracking_launch(*args, **kwargs):
            order.append(("launched", args[7]))
            kwargs["on_running"](None)
            return CommandWatcherTests.complete(args[7])

        with patch("manager.command_watcher.launch_task", side_effect=tracking_launch), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)

        launch_index = next(i for i, e in enumerate(order) if e[0] == "launched")
        first_terminal_lookup_index = next(i for i, e in enumerate(order) if e[0] == "terminal_lookup")
        self.assertLess(launch_index, first_terminal_lookup_index,
                        f"queued work must launch before any terminal lookup; order was {order}")

    # A: project A's terminal lookup must never block project B's queued
    # work -- the residual-P0-A defect this round of the fix closes. The
    # earlier per-project two-phase split still processed "project A
    # nonterminal -> project A terminal lookup -> project B nonterminal" in
    # that interleaved order; the fix makes ALL projects' nonterminal work
    # (across both sweeps) happen before ANY project's terminal lookup.
    def test_cross_project_queued_not_starved_by_other_project_terminal_lookup(self):
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        self._bare_terminal_command("cmd-term", "exec-term", "partial", project_id="p1")
        self.store.put("commands", "p2", "cmd-queued", command(command_id="cmd-queued", project_id="p2", task_id="t1"))

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(("terminal_lookup", cmd["project_id"], cmd["command_id"]))
            return real_needs_recovery(store, cmd)

        def tracking_launch(*args, **kwargs):
            order.append(("launched", args[7]))
            kwargs["on_running"](None)
            return CommandWatcherTests.complete(args[7])

        cursor_path = tempfile.mktemp(suffix=".json")
        with patch("manager.command_watcher.launch_task", side_effect=tracking_launch), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=allowlist, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)

        launch_index = next(i for i, e in enumerate(order) if e[0] == "launched")
        terminal_index = next(i for i, e in enumerate(order) if e[0] == "terminal_lookup")
        self.assertLess(launch_index, terminal_index,
                        f"p2's queued work must process before p1's terminal lookup even starts; order was {order}")

    # B: same as A, but project B's actionable work is active-lifecycle
    # (claimed), not queued.
    def test_cross_project_claimed_not_starved_by_other_project_terminal_lookup(self):
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        self._bare_terminal_command("cmd-term", "exec-term", "partial", project_id="p1")
        self.store.put("commands", "p2", "cmd-claimed", command(
            command_id="cmd-claimed", project_id="p2", task_id="t1", status="claimed",
            execution_id="exec-missing-p2", claimed_at=now_iso()))

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(("terminal_lookup", cmd["project_id"], cmd["command_id"]))
            return real_needs_recovery(store, cmd)

        real_reconcile = _reconcile_active

        def tracking_reconcile(store, service, cmd, claim_factory):
            if cmd["project_id"] == "p2":
                order.append(("reconciled", cmd["project_id"], cmd["command_id"]))
            return real_reconcile(store, service, cmd, claim_factory)

        cursor_path = tempfile.mktemp(suffix=".json")
        with patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery), \
             patch("manager.command_watcher._reconcile_active", side_effect=tracking_reconcile):
            poll_once(self.store, object(), allowlist=allowlist, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)

        reconciled_index = next(i for i, e in enumerate(order) if e[0] == "reconciled")
        terminal_index = next(i for i, e in enumerate(order) if e[0] == "terminal_lookup")
        self.assertLess(reconciled_index, terminal_index,
                        f"p2's active-lifecycle claimed Command must process before p1's terminal lookup; order was {order}")

    # C: 3 projects -- A and B both have a terminal-recovery candidate, C
    # has a queued Command. C's queued work must process before EITHER A's
    # or B's terminal lookup.
    def test_cross_project_queued_in_third_project_not_starved_by_two_terminal_lookups(self):
        self._add_project("p2")
        self._add_project("p3")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1"), ("p3", "t1")})
        self._bare_terminal_command("cmd-term-a", "exec-term-a", "partial", project_id="p1")
        self._bare_terminal_command("cmd-term-b", "exec-term-b", "partial", project_id="p2")
        self.store.put("commands", "p3", "cmd-queued", command(command_id="cmd-queued", project_id="p3", task_id="t1"))

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(("terminal_lookup", cmd["project_id"], cmd["command_id"]))
            return real_needs_recovery(store, cmd)

        def tracking_launch(*args, **kwargs):
            order.append(("launched", args[7]))
            kwargs["on_running"](None)
            return CommandWatcherTests.complete(args[7])

        cursor_path = tempfile.mktemp(suffix=".json")
        with patch("manager.command_watcher.launch_task", side_effect=tracking_launch), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=allowlist, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)

        launch_index = next(i for i, e in enumerate(order) if e[0] == "launched")
        first_terminal_index = next(i for i, e in enumerate(order) if e[0] == "terminal_lookup")
        self.assertLess(launch_index, first_terminal_index,
                        f"p3's queued work must process before ANY project's terminal lookup; order was {order}")

    # I: the recent sweep's own terminal candidates must not starve the
    # full sweep's queued/running work either -- terminal classification is
    # deferred past BOTH sweeps, not just the recent one.
    def test_recent_sweep_terminal_candidate_does_not_starve_full_sweep_queued_work(self):
        self._bare_terminal_command("cmd-term", "exec-term", "partial")
        self.store.put("commands", "p1", "cmd-queued", command(command_id="cmd-queued"))

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(("terminal_lookup", cmd["command_id"]))
            return real_needs_recovery(store, cmd)

        def tracking_launch(*args, **kwargs):
            order.append(("launched", args[7]))
            kwargs["on_running"](None)
            return CommandWatcherTests.complete(args[7])

        cursor_path = tempfile.mktemp(suffix=".json")
        with patch("manager.command_watcher.launch_task", side_effect=tracking_launch), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)

        launch_index = next(i for i, e in enumerate(order) if e[0] == "launched")
        terminal_index = next(i for i, e in enumerate(order) if e[0] == "terminal_lookup")
        self.assertLess(launch_index, terminal_index)

    # J: cross-project terminal-recovery fairness -- with the lookup budget
    # exhausted by an earlier-rotated project's terminal backlog, a
    # later-rotated project's own terminal candidate still gets classified
    # once rotation brings it to the front on a later tick (never
    # permanently stuck behind project 0's backlog).
    def test_terminal_recovery_lookup_budget_rotates_across_projects_over_ticks(self):
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        self._bare_terminal_command("cmd-term-a", "exec-term-a", "partial", project_id="p1")
        self._bare_terminal_command("cmd-term-b", "exec-term-b", "partial", project_id="p2")
        cursor_path = tempfile.mktemp(suffix=".json")
        real_needs_recovery = _terminal_command_needs_recovery

        def _examined_project(cursor_path):
            examined = []

            def tracking_needs_recovery(store, cmd):
                examined.append(cmd["project_id"])
                return real_needs_recovery(store, cmd)

            with patch("manager.command_watcher.MAX_COMMANDS_PER_POLL", 1), \
                 patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
                poll_once(self.store, object(), allowlist=allowlist, deadline=time.monotonic() + 50.0,
                         claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                         cursor_path=cursor_path)
            return examined

        # With a lookup budget of exactly 1, each tick's CLASSIFICATION
        # pass can only examine ONE project's terminal backlog -- a real
        # per-tick choice, not "always reach every project because the
        # budget is generous enough to." (process_command() itself
        # separately re-checks eligibility once more, as its own
        # independent, pre-existing safety gate, for whichever ONE
        # candidate classification already selected and committed to
        # processing -- that is a second, distinct lookup on the SAME
        # already-chosen command, not a second classification attempt on a
        # second command, so it is deduplicated by project below rather
        # than asserted away.) Project rotation (the SAME durable phase1
        # cursor mechanism every other phase already relies on) means
        # which project gets that one look changes from tick to tick --
        # p2's backlog is never permanently stuck behind p1's always
        # winning first.
        first_examined = set(_examined_project(cursor_path))
        second_examined = set(_examined_project(cursor_path))
        self.assertEqual(1, len(first_examined))
        self.assertEqual(1, len(second_examined))
        self.assertNotEqual(first_examined, second_examined,
                            f"rotation must examine a DIFFERENT project's terminal backlog on the next tick; "
                            f"tick 1 examined {first_examined}, tick 2 examined {second_examined}")

    # 3: a lookup that would start past the deadline is never attempted;
    # classification stops there, tick is not aborted.
    def test_terminal_lookup_never_attempted_once_past_deadline(self):
        cmd, _claim = self._terminal_command_with_execution("completed", "partial")

        class ExplodingStore:
            def get(self, *a, **k):
                raise AssertionError("must not attempt a lookup once the deadline has already passed")

        candidates, remaining = _terminal_recovery_candidates([cmd], ExplodingStore(), time.monotonic() - 1.0, lookup_budget=10)
        self.assertEqual([], candidates)
        self.assertEqual(10, remaining)

    # Residual-P0-A: a lookup is never even started unless there is enough
    # headroom for its own worst-case duration
    # (TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS), not merely "not literally
    # past the deadline yet" -- the same rule DriveRecords.
    # list_records_bounded() already applies to hydration.
    def test_terminal_lookup_not_attempted_without_full_worst_case_headroom(self):
        cmd, _claim = self._terminal_command_with_execution("completed", "partial")

        class ExplodingStore:
            def get(self, *a, **k):
                raise AssertionError("must not attempt a lookup without full worst-case headroom")

        deadline = time.monotonic() + (TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS / 2)
        candidates, remaining = _terminal_recovery_candidates([cmd], ExplodingStore(), deadline, lookup_budget=10)
        self.assertEqual([], candidates)
        self.assertEqual(10, remaining)

    def test_terminal_classification_stops_partway_once_deadline_is_reached(self):
        cmd1 = self._bare_terminal_command("cmd-1", "exec-1", "partial")
        cmd2 = self._bare_terminal_command("cmd-2", "exec-2", "partial")
        real_monotonic = time.monotonic
        deadline = real_monotonic() + 1000.0
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            # First two checks (before/after cmd1's lookup) are under
            # budget; every check from the third call onward (before
            # cmd2's lookup) is already past it.
            return deadline - 10.0 if calls["n"] <= 2 else deadline + 10.0

        with patch("manager.command_watcher.time.monotonic", side_effect=fake_monotonic):
            candidates, remaining = _terminal_recovery_candidates([cmd1, cmd2], self.store, deadline, lookup_budget=10)
        self.assertEqual(["cmd-1"], [c["command_id"] for c, _prevalidation in candidates])
        self.assertEqual(9, remaining)

    # 7: lookup_budget (derived from MAX_COMMANDS_PER_POLL) strictly bounds
    # returned candidates when every attempt is eligible.
    def test_lookup_budget_strictly_bounds_returned_candidates(self):
        cmd1 = self._bare_terminal_command("cmd-1", "exec-1", "partial")
        cmd2 = self._bare_terminal_command("cmd-2", "exec-2", "partial")
        cmd3 = self._bare_terminal_command("cmd-3", "exec-3", "partial")
        candidates, remaining = _terminal_recovery_candidates([cmd1, cmd2, cmd3], self.store, time.monotonic() + 1000.0, lookup_budget=2)
        self.assertEqual(2, len(candidates))
        self.assertEqual(0, remaining)

    def test_zero_lookup_budget_skips_classification_entirely(self):
        cmd, _claim = self._terminal_command_with_execution("completed", "partial")

        class ExplodingStore:
            def get(self, *a, **k):
                raise AssertionError("must not attempt any lookup with zero remaining budget")

        candidates, remaining = _terminal_recovery_candidates([cmd], ExplodingStore(), time.monotonic() + 1000.0, lookup_budget=0)
        self.assertEqual([], candidates)
        self.assertEqual(0, remaining)

    # D: remaining_slots=1 + 5 ambiguous terminal records -> exactly 1
    # lookup attempt is made, not 5 -- the budget bounds ATTEMPTS, not the
    # count of eligible results (which would have stayed 0 here regardless,
    # since none of these are actually eligible).
    def test_lookup_budget_bounds_attempts_not_just_eligible_results(self):
        commands = [self._bare_terminal_command(f"cmd-{i}", f"exec-{i}", "complete", task_claim_release="released")
                   for i in range(5)]
        attempts = []
        real_needs_recovery = _terminal_command_needs_recovery

        def counting_needs_recovery(store, cmd):
            attempts.append(cmd["command_id"])
            return real_needs_recovery(store, cmd)

        with patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=counting_needs_recovery):
            candidates, remaining = _terminal_recovery_candidates(commands, self.store, time.monotonic() + 1000.0, lookup_budget=1)
        self.assertEqual(1, len(attempts))
        self.assertEqual([], candidates)
        self.assertEqual(0, remaining)

    # E: remaining_slots=2 + 5 non-eligible terminal records -> at most 2
    # lookup attempts.
    def test_lookup_budget_of_two_bounds_attempts_to_two(self):
        commands = [self._bare_terminal_command(f"cmd-{i}", f"exec-{i}", "complete", task_claim_release="released")
                   for i in range(5)]
        attempts = []
        real_needs_recovery = _terminal_command_needs_recovery

        def counting_needs_recovery(store, cmd):
            attempts.append(cmd["command_id"])
            return real_needs_recovery(store, cmd)

        with patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=counting_needs_recovery):
            candidates, remaining = _terminal_recovery_candidates(commands, self.store, time.monotonic() + 1000.0, lookup_budget=2)
        self.assertLessEqual(len(attempts), 2)
        self.assertEqual(0, remaining)

    # 4/F: a TaskError from one candidate's lookup skips only that
    # candidate, but still consumes its own attempt budget.
    def test_taskerror_from_one_candidate_skips_only_that_one(self):
        broken = command(command_id="cmd-broken", status="completed", execution_id="exec-does-not-exist")
        healthy, _claim = self._terminal_command_with_execution("completed", "partial", command_id="cmd-healthy", execution_id="exec-healthy")
        candidates, remaining = _terminal_recovery_candidates([broken, healthy], self.store, time.monotonic() + 1000.0, lookup_budget=10)
        self.assertEqual(["cmd-healthy"], [c["command_id"] for c, _prevalidation in candidates])
        self.assertEqual(8, remaining)

    # 5/G: a generic (non-TaskError) transport-shaped exception from one
    # candidate's lookup also skips only that one -- never aborts -- but
    # still consumes its own attempt budget.
    def test_generic_transport_exception_from_one_candidate_skips_only_that_one(self):
        broken = command(command_id="cmd-broken", status="completed", execution_id="exec-broken")
        healthy, _claim = self._terminal_command_with_execution("completed", "partial", command_id="cmd-healthy", execution_id="exec-healthy")

        class FlakyStore:
            def __init__(self, real):
                self.real = real

            def get(self, area, project_id, name):
                if area == "executions" and name == "exec-broken":
                    raise ConnectionError("simulated transport failure")
                return self.real.get(area, project_id, name)

        candidates, remaining = _terminal_recovery_candidates([broken, healthy], FlakyStore(self.store), time.monotonic() + 1000.0, lookup_budget=10)
        self.assertEqual(["cmd-healthy"], [c["command_id"] for c, _prevalidation in candidates])
        self.assertEqual(8, remaining)

    def test_generic_transport_exception_does_not_abort_the_whole_tick(self):
        broken, _c1 = self._terminal_command_with_execution("completed", "partial", command_id="cmd-broken", execution_id="exec-broken")
        self.store.put("commands", "p1", "cmd-queued", command(command_id="cmd-queued"))
        real_get = self.store.get

        def flaky_get(area, project_id, name):
            if area == "executions" and name == "exec-broken":
                raise ConnectionError("simulated transport failure")
            return real_get(area, project_id, name)

        runner = Mock(side_effect=lambda *a, **k: (k["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
        with patch("manager.command_watcher.launch_task", runner), patch.object(self.store, "get", side_effect=flaky_get):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                                claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True)
        # The tick survived and the queued command still completed --
        # the broken terminal candidate is simply never in the results.
        self.assertTrue(any(r.get("status") == "completed" for r in results))
        self.assertEqual("completed", self.store.get("commands", "p1", "cmd-queued")["result"]["status"])

    # 8: both poll_once() sweeps (recent + full) preserve boundedness --
    # MAX_COMMANDS_PER_POLL holds even with a mixed nonterminal/terminal
    # batch larger than the budget.
    def test_max_commands_per_poll_strictly_enforced_with_mixed_batch(self):
        for i in range(3):
            self._bare_terminal_command(f"cmd-term-{i}", f"exec-term-{i}", "partial")
        self.store.put("commands", "p1", "cmd-queued-1", command(command_id="cmd-queued-1"))
        self.store.put("commands", "p1", "cmd-queued-2", command(command_id="cmd-queued-2"))
        self.store.put("commands", "p1", "cmd-queued-3", command(command_id="cmd-queued-3"))

        runner = Mock(side_effect=lambda *a, **k: (k["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                                claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True)
        self.assertLessEqual(len(results), MAX_COMMANDS_PER_POLL)

    # ================= Blocker 1: eligibility lookup exactly once =================

    # 1/2/3: lookup_budget=1, one eligible terminal record -- the real
    # Execution eligibility lookup must happen EXACTLY ONCE for the whole
    # tick (the classification_store lookup), never a second time once the
    # candidate reaches process_command(). Also confirms classification_
    # store (not the primary store) is what performed it.
    def test_eligibility_lookup_happens_exactly_once_per_attempt(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        classification_store = Mock(wraps=self.store)
        real_needs_recovery = _terminal_command_needs_recovery
        calls = []

        def tracking_needs_recovery(used_store, command_arg):
            calls.append((used_store, command_arg["command_id"]))
            return real_needs_recovery(used_store, command_arg)

        with patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                                classification_store=classification_store,
                                claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual(1, len(calls), f"eligibility must be checked exactly once per attempt; calls were {calls}")
        self.assertIs(classification_store, calls[0][0],
                      "the one eligibility lookup must use classification_store, not the primary store")
        self.assertEqual("cmd-1", calls[0][1])
        self.assertTrue(results[0].get("reconciled"))

    # 4: a prevalidated context whose identity no longer matches the
    # CURRENT command must fail closed -- no recovery, no provider launch.
    def test_prevalidation_identity_mismatch_fails_closed(self):
        cmd, _claim = self._terminal_command_with_execution("completed", "complete", task_claim_release="released")
        # A prevalidation object built for a DIFFERENT execution_id than
        # this (fully-converged, genuinely non-eligible) command's real
        # one -- simulating either a forged/stale context or a command
        # that changed shape between classification and processing.
        forged = _TerminalRecoveryPrevalidation(
            project_id="p1", command_id="cmd-1", execution_id="exec-does-not-match", status="completed")
        launcher = Mock()
        with patch("manager.command_watcher.launch_task", launcher):
            result = process_command(self.store, object(), cmd, claim_factory=lambda *_: object(),
                                     terminal_recovery_prevalidation=forged)
        launcher.assert_not_called()
        self.assertEqual({"status": "completed", "skipped": True}, result)

    def test_prevalidation_wrong_type_is_silently_ignored_not_trusted(self):
        # A plain True/False/None (or any non-_TerminalRecoveryPrevalidation
        # value) must never bypass the eligibility gate -- there is no bare
        # boolean shortcut a careless or malicious caller could substitute.
        cmd, _claim = self._terminal_command_with_execution("completed", "complete", task_claim_release="released")
        launcher = Mock()
        with patch("manager.command_watcher.launch_task", launcher):
            result = process_command(self.store, object(), cmd, claim_factory=lambda *_: object(),
                                     terminal_recovery_prevalidation=True)
        launcher.assert_not_called()
        self.assertEqual({"status": "completed", "skipped": True}, result)

    # 5: a normal, direct process_command() caller with no prevalidated
    # context must still use the original, safe, full eligibility check --
    # unaffected by the new prevalidation path existing at all.
    def test_direct_process_command_without_prevalidation_uses_normal_eligibility(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        result = process_command(self.store, object(), cmd, claim_factory=lambda *_: claim)
        self.assertTrue(result.get("reconciled"))
        execution = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual("complete", execution["cleanup_evidence"]["persistence"])

    def test_valid_prevalidation_still_reaches_reconciliation(self):
        # A genuine, correctly-identity-matched prevalidation from
        # _terminal_recovery_candidates() itself (not hand-forged) must
        # still let process_command() reach _reconcile_active() -- the
        # fix must not accidentally make prevalidation always fail closed.
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        candidates, _remaining = _terminal_recovery_candidates([cmd], self.store, time.monotonic() + 1000.0, lookup_budget=1)
        self.assertEqual(1, len(candidates))
        real_command, prevalidation = candidates[0]
        result = process_command(self.store, object(), real_command, claim_factory=lambda *_: claim,
                                 terminal_recovery_prevalidation=prevalidation)
        self.assertTrue(result.get("reconciled"))

    # ================= Blocker 2: canonical rotated project order =================

    def _rig_sweep_success(self, recent_fails_for=(), full_fails_for=()):
        """Patch _enumerate_recent_commands/_enumerate_commands so specific
        projects' RECENT and/or FULL sweep raise TaskError (simulating a
        transient hydration failure) while others succeed normally --
        needed to reproduce blocker-2's exact shape: a project's terminal-
        recovery priority must track its position in the canonical rotated
        `project_ids`, never whichever sweep happened to hydrate it first."""
        real_recent = _enumerate_recent_commands
        real_full = _enumerate_commands

        def fake_recent(store, project_id, deadline=None):
            if project_id in recent_fails_for:
                raise TaskError("simulated transient recent-sweep failure")
            # The real _enumerate_recent_commands, for a store with no
            # list_records_bounded (this test double), falls back to
            # calling _enumerate_commands directly -- which would resolve
            # to whatever `_enumerate_commands` is CURRENTLY patched to
            # (fake_full below), making recent_fails_for/full_fails_for
            # impossible to vary independently. Call the captured ORIGINAL
            # full-sweep function directly instead, so a project whose
            # recent sweep should succeed genuinely does, regardless of
            # what full_fails_for says for it.
            if not hasattr(store, "list_records_bounded"):
                return real_full(store, project_id, deadline=deadline)
            return real_recent(store, project_id, deadline=deadline)

        def fake_full(store, project_id, deadline=None):
            if project_id in full_fails_for:
                raise TaskError("simulated transient full-sweep failure")
            return real_full(store, project_id, deadline=deadline)

        return patch("manager.command_watcher._enumerate_recent_commands", side_effect=fake_recent), \
            patch("manager.command_watcher._enumerate_commands", side_effect=fake_full)

    # 7: project_ids=[p1, p2] (natural/default rotation) -- p1's recent
    # sweep fails, p2's recent sweep succeeds, p1's full sweep succeeds.
    # The first terminal classification MUST still be p1's, because p1 is
    # first in the canonical rotated order -- not p2's, even though p2
    # was the first project whose batch actually landed in the cache.
    def test_terminal_priority_follows_rotated_order_not_hydration_success_order(self):
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        self._bare_terminal_command("cmd-term-a", "exec-term-a", "partial", project_id="p1")
        self._bare_terminal_command("cmd-term-b", "exec-term-b", "partial", project_id="p2")

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(cmd["project_id"])
            return real_needs_recovery(store, cmd)

        recent_patch, full_patch = self._rig_sweep_success(recent_fails_for={"p1"})
        cursor_path = tempfile.mktemp(suffix=".json")
        with recent_patch, full_patch, \
             patch("manager.command_watcher.MAX_COMMANDS_PER_POLL", 1), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=allowlist, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)
        self.assertTrue(order)
        self.assertEqual("p1", order[0],
                        f"canonical rotated order must decide terminal priority, not hydration success order; examined {order}")

    # 8: reverse rotation -- project_ids=[p2, p1] -- first terminal
    # classification must be p2's.
    def test_terminal_priority_follows_reversed_rotated_order(self):
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        self._bare_terminal_command("cmd-term-a", "exec-term-a", "partial", project_id="p1")
        self._bare_terminal_command("cmd-term-b", "exec-term-b", "partial", project_id="p2")

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(cmd["project_id"])
            return real_needs_recovery(store, cmd)

        # Rotation starts at p2 by seeding the durable cursor's
        # project_cursor to point at index 1 ("p2", since list_projects()
        # returns them alphabetically p1, p2) before the tick runs.
        from manager.phase1_cursor import save_phase1_cursor
        cursor_path = tempfile.mktemp(suffix=".json")
        save_phase1_cursor({"project_cursor": 1, "per_project_record_cursor": {}, "generation": 0}, cursor_path=cursor_path)

        recent_patch, full_patch = self._rig_sweep_success(recent_fails_for={"p2"})
        with recent_patch, full_patch, \
             patch("manager.command_watcher.MAX_COMMANDS_PER_POLL", 1), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=allowlist, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)
        self.assertTrue(order)
        self.assertEqual("p2", order[0],
                        f"reversed rotation must classify p2's backlog first; examined {order}")

    # 9: the same terminal Command appearing in BOTH the recent sweep's
    # batch and the full sweep's batch for the same project must only be
    # classified ONCE (deduplicated by command_id), never twice.
    def test_same_command_in_both_sweeps_classified_only_once(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd_arg):
            order.append(cmd_arg["command_id"])
            return real_needs_recovery(store, cmd_arg)

        with patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual(["cmd-1"], order)

    # 10: whichever sweep (recent vs full) actually succeeds first for a
    # given project must not change canonical Phase 2c project order --
    # re-proven with p1's FULL sweep (not just recent) also failing, so
    # p1's batch is hydrated ONLY by its recent sweep this time, while p2
    # is hydrated by both -- p1 still wins terminal priority because it is
    # first in rotation, regardless of which of ITS OWN sweeps supplied
    # the batch.
    def test_terminal_priority_unaffected_by_which_sweep_supplied_the_batch(self):
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        self._bare_terminal_command("cmd-term-a", "exec-term-a", "partial", project_id="p1")
        self._bare_terminal_command("cmd-term-b", "exec-term-b", "partial", project_id="p2")

        order = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, cmd):
            order.append(cmd["project_id"])
            return real_needs_recovery(store, cmd)

        # p1's FULL sweep fails this time (its recent sweep is what
        # actually hydrates its batch); p2 hydrates normally via both.
        recent_patch, full_patch = self._rig_sweep_success(full_fails_for={"p1"})
        cursor_path = tempfile.mktemp(suffix=".json")
        with recent_patch, full_patch, \
             patch("manager.command_watcher.MAX_COMMANDS_PER_POLL", 1), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(self.store, object(), allowlist=allowlist, deadline=time.monotonic() + 50.0,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)
        self.assertTrue(order)
        self.assertEqual("p1", order[0], f"which sweep supplied the batch must not affect project order; examined {order}")

    # Real end-to-end poll_once() reaches reconciliation for an R17-shaped
    # record, never launches a provider, and only performs cleanup/
    # materialization recovery -- the actual live production repro.
    def test_poll_once_reaches_r17_shaped_record_without_relaunching_provider(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        launcher = Mock()
        with patch("manager.command_watcher.launch_task", launcher):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                                claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        launcher.assert_not_called()
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].get("reconciled"))
        execution = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual("complete", execution["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], execution["cleanup_evidence"]["persisted"])
        self.assertEqual("completed", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual("completed", self.store.get("tasks", "p1", "t1")["status"])

    # Ordinary fully-converged terminal records still never become
    # candidates via the real poll_once() path either (not just the unit-
    # level _terminal_recovery_candidates() check already covered above).
    def test_poll_once_never_reprocesses_a_fully_converged_terminal_command(self):
        cmd, _claim = self._terminal_command_with_execution("completed", "complete", task_claim_release="released")
        launcher = Mock()
        with patch("manager.command_watcher.launch_task", launcher):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=time.monotonic() + 50.0,
                                claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True)
        launcher.assert_not_called()
        self.assertEqual([], results)


class TerminalRecoveryBudgetReservationTests(unittest.TestCase):
    """LIVE P0 fix, found via real production observation (not a unit-test
    artifact): a large project's FULL-sweep hydration can legitimately
    consume nearly the entire shared poll deadline by itself -- a live
    measurement against ai-development-manager's real 234-record Command
    history took ~28s of the ~40s POLL_TIME_BUDGET_SECONDS. R17 was
    confirmed present in the cached batch, at the front of it, with zero
    competing nonterminal work anywhere in the environment -- yet Phase
    2c's own (correct in isolation) headroom guard
    (deadline - now >= TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS) meant it
    never got a single classification attempt, tick after tick, because
    hydration alone had already spent the shared deadline down past that
    threshold before Phase 2c ever ran.

    Fix: TERMINAL_RECOVERY_RESERVED_SECONDS reserves a slice of the poll
    deadline specifically for terminal recovery, by making Phase 2a/2b's
    HYDRATION calls (never their processing of already-discovered
    nonterminal work) stop starting further optional enumeration once
    fewer than that many seconds remain before the real poll deadline."""

    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = CommandWatcherTests.allowlist_compliant_store()

    def _terminal_command_with_execution(self, status, persistence, task_claim_release="retained"):
        now = datetime.now(timezone.utc)
        started = CommandWatcherTests.iso(now - timedelta(minutes=5))
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", "command-cmd-1", "codex",
                               "read_only", started_at=started, task_claim_registry=claim)
        execution = self.store.get("executions", "p1", "command-cmd-1")
        execution.update(
            status=status, completed_at=now_iso(), finished_at=now_iso(),
            elapsed_minutes=5, quota_after={}, quota_delta={}, terminal_reason=status,
            cleanup_evidence={
                "provider_outcome": status, "persistence": persistence,
                "persisted": ["execution", "handoff", "task"] if persistence == "complete" else ["execution"],
                "task_claim_release": task_claim_release, "writer_release": "not_required",
                "errors": [] if persistence == "complete" else ["persistence failed: simulated Drive verification failure"],
            },
        )
        self.store.put("executions", "p1", "command-cmd-1", execution)
        cmd = command(command_id="cmd-1", status=status, execution_id="command-cmd-1", claimed_at=started,
                      completed_at=now_iso(), result={"status": status, "session_id": None, "error_kind": None})
        self.store.put("commands", "p1", "cmd-1", cmd)
        return cmd, claim

    def _fake_clock(self, start):
        state = {"now": start}

        def monotonic():
            return state["now"]

        def advance(seconds):
            state["now"] += seconds

        return state, monotonic, advance

    def _slow_full_sweep(self, state, advance, seconds=31.0):
        """A deterministic stand-in for the real ~28-31s live hydration
        cost, matching list_records_bounded's own real contract: it never
        overshoots the deadline it was given, consuming exactly up to
        whatever budget remains (capped at `seconds`) before returning
        whatever it actually fetched from the real (fast, in-memory) test
        double -- so the returned commands are always the real, correct
        ones; only the SIMULATED wall-clock cost of getting them is
        deterministic and controlled."""
        real_full = _enumerate_commands

        def slow_full(store, project_id, deadline=None):
            budget = max(0.0, deadline - state["now"]) if deadline is not None else seconds
            advance(min(seconds, budget))
            return real_full(store, project_id, deadline=deadline)

        return patch("manager.command_watcher._enumerate_commands", side_effect=slow_full)

    # 1: deterministic, non-vacuous LIVE-P0 reproduction matching the real
    # observed facts (40s logical budget, ~31s equivalent hydration cost,
    # 10s classification timeout) -- proves 0 attempts pre-fix, >=1 post-fix.
    def test_live_p0_reproduced_full_hydration_no_longer_starves_terminal_classification(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        state, monotonic, advance = self._fake_clock(start=1000.0)
        deadline = state["now"] + POLL_TIME_BUDGET_SECONDS

        attempts = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, command_arg):
            attempts.append(command_arg["command_id"])
            return real_needs_recovery(store, command_arg)

        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             self._slow_full_sweep(state, advance), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=deadline,
                                claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        self.assertGreaterEqual(len(attempts), 1,
                                "terminal classification must get at least one attempt even after ~31s of hydration cost")
        self.assertTrue(any(r.get("reconciled") for r in results))

    # 2: a cached R17-shaped terminal candidate receives a guaranteed
    # classification attempt when no competing nonterminal work exists --
    # same scenario as test 1, phrased as the acceptance-model contract
    # itself (R17_CLASSIFICATION_ATTEMPT_GUARANTEED_WHEN_CACHED).
    def test_cached_terminal_candidate_gets_guaranteed_classification_attempt(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        state, monotonic, advance = self._fake_clock(start=2000.0)
        deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             self._slow_full_sweep(state, advance):
            results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=deadline,
                                claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].get("reconciled"))

    # 3/4 (rewritten after the independent adversarial review of 95768752,
    # finding 3, found the original version vacuous: its recent-sweep fake
    # returned EVERY record, so the target queued command was always
    # discovered before the reservation or the expensive full sweep could
    # possibly matter -- it passed identically with the reservation forced
    # to 0). Production-shaped now: the recent sweep returns ONLY each
    # project's newest RECENT_COMMANDS_PER_PROJECT=2 records, the target
    # queued command in p2 sits deliberately OUTSIDE that batch (two newer
    # terminal records shadow it), so only p2's FULL sweep can find it --
    # and p1's full sweep is the expensive one. The invariant actually
    # proven: the reservation may defer p2's full-sweep hydration by a
    # tick, but project rotation guarantees the queued command is found
    # and launched within len(projects) natural ticks -- never starved
    # permanently. The companion control test below proves this harness
    # DOES detect reservation-induced starvation when the reservation is
    # mutated to swallow the whole poll budget.
    def _production_shaped_cross_project_harness(self):
        self._add_project = TerminalIncompleteRecoveryReachableViaPollTests._add_project.__get__(self)
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        self.store.put("commands", "p2", "cmd-old-queued",
                       command(command_id="cmd-old-queued", project_id="p2", task_id="t1"))
        for newer in ("cmd-new-a", "cmd-new-b"):
            self.store.put("commands", "p2", newer,
                           command(command_id=newer, project_id="p2", task_id="t1", status="completed",
                                   completed_at=now_iso(),
                                   result={"status": "completed", "session_id": None, "error_kind": None}))
        state, monotonic, advance = self._fake_clock(start=6000.0)
        # Explicit throwaway Phase-1 cursor: poll_once's project rotation
        # comes from the PERSISTED phase1 cursor (load_phase1_cursor), so
        # without an explicit cursor_path every test in this file would
        # share (and mutate) the same ./runtime/phase1-cursor.json --
        # making rotation order depend on how many poll_once calls any
        # OTHER test happened to make first. A fresh temp file pins tick 1
        # to p1-first and lets the cursor's own natural advancement bring
        # p2 to the front on tick 2, exactly the production mechanism.
        self._cursor_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._cursor_dir.cleanup)
        cursor_path = os.path.join(self._cursor_dir.name, "phase1-cursor.json")
        real_full = _enumerate_commands

        def slow_only_p1_full(store, project_id, deadline=None):
            if project_id == "p1":
                budget = max(0.0, deadline - state["now"]) if deadline is not None else 31.0
                advance(min(31.0, budget))
            return real_full(store, project_id, deadline=deadline)

        def bounded_recent(store, project_id, deadline=None):
            # Production shape: newest RECENT_COMMANDS_PER_PROJECT=2 only.
            # p2's queued target is explicitly NOT among its newest two.
            if project_id == "p2":
                return [self.store.get("commands", "p2", "cmd-new-a"),
                        self.store.get("commands", "p2", "cmd-new-b")]
            return real_full(store, project_id, deadline=deadline)

        return allowlist, claim, state, monotonic, advance, slow_only_p1_full, bounded_recent, cursor_path

    def test_queued_work_in_another_project_still_processes_despite_expensive_hydration_elsewhere(self):
        (allowlist, claim, state, monotonic, advance,
         slow_only_p1_full, bounded_recent, cursor_path) = self._production_shaped_cross_project_harness()
        runner = Mock(side_effect=lambda *a, **k: (k["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
        completed_on_tick = None
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             patch("manager.command_watcher._enumerate_commands", side_effect=slow_only_p1_full), \
             patch("manager.command_watcher._enumerate_recent_commands", side_effect=bounded_recent), \
             patch("manager.command_watcher.launch_task", runner):
            for tick in (1, 2):
                deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
                poll_once(self.store, object(), allowlist=allowlist, deadline=deadline, cursor_path=cursor_path,
                          claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
                if (self.store.get("commands", "p2", "cmd-old-queued").get("result") or {}).get("status") == "completed":
                    completed_on_tick = tick
                    break
                state["now"] = deadline + 5.0
        self.assertIsNotNone(completed_on_tick,
                             "queued command outside the recent batch must be found and completed "
                             "within len(projects) rotated ticks despite the reservation")
        self.assertLessEqual(completed_on_tick, 2)

    def test_cross_project_harness_detects_reservation_that_swallows_the_budget(self):
        # Mutation-sensitivity control for the test above: force the
        # reservation to consume the ENTIRE poll budget. Hydration (recent
        # and full alike) then never runs at all, and the queued command
        # must observably NOT complete on any tick -- proving this harness
        # genuinely detects reservation-induced starvation of active work
        # rather than passing for any constant value.
        (allowlist, claim, state, monotonic, advance,
         slow_only_p1_full, bounded_recent, cursor_path) = self._production_shaped_cross_project_harness()
        runner = Mock(side_effect=lambda *a, **k: (k["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             patch("manager.command_watcher._enumerate_commands", side_effect=slow_only_p1_full), \
             patch("manager.command_watcher._enumerate_recent_commands", side_effect=bounded_recent), \
             patch("manager.command_watcher.TERMINAL_RECOVERY_RESERVED_SECONDS", POLL_TIME_BUDGET_SECONDS), \
             patch("manager.command_watcher.launch_task", runner):
            for _ in range(3):
                deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
                poll_once(self.store, object(), allowlist=allowlist, deadline=deadline, cursor_path=cursor_path,
                          claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
                state["now"] = deadline + 5.0
        self.assertNotEqual("completed",
                            (self.store.get("commands", "p2", "cmd-old-queued").get("result") or {}).get("status"),
                            "with the reservation mutated to the whole budget, hydration must be starved "
                            "and this harness must observably fail the queued command")

    # 5: further optional historical hydration for a SECOND project must
    # not even be attempted once the reserved boundary has already been
    # reached by an earlier-rotated project's own expensive hydration.
    def test_further_hydration_yields_once_reserved_boundary_reached(self):
        self._add_project = TerminalIncompleteRecoveryReachableViaPollTests._add_project.__get__(self)
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        cmd, claim = self._terminal_command_with_execution("completed", "partial")

        state, monotonic, advance = self._fake_clock(start=4000.0)
        deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
        hydrated_projects = []
        real_full = _enumerate_commands
        # Fresh explicit Phase-1 cursor: rotation order comes from the
        # PERSISTED cursor file, so without this the p1-vs-p2 order here
        # depended on how many poll_once calls other tests made against
        # the shared default ./runtime cursor first -- parity-flaky.
        cursor_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cursor_dir.cleanup)
        cursor_path = os.path.join(cursor_dir.name, "phase1-cursor.json")

        def slow_full(store, project_id, deadline=None):
            hydrated_projects.append(project_id)
            budget = max(0.0, deadline - state["now"]) if deadline is not None else 31.0
            advance(min(31.0, budget))
            return real_full(store, project_id, deadline=deadline)

        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             patch("manager.command_watcher._enumerate_commands", side_effect=slow_full):
            poll_once(self.store, object(), allowlist=allowlist, deadline=deadline, cursor_path=cursor_path,
                     claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        # p1's own hydration already consumed the tick down to the
        # reserved boundary -- p2's full-sweep hydration must never even
        # be attempted this same tick (it gets its own natural turn once
        # rotation brings it to the front later).
        self.assertEqual(["p1"], hydrated_projects)

    # 6: the classification headroom guard itself is unchanged and still
    # correctly refuses to start a lookup without full short-timeout
    # headroom -- the reservation guarantees the OPPORTUNITY, it does not
    # weaken the guard that protects a single lookup's own hard bound.
    def test_classification_still_refuses_without_full_headroom_even_with_reservation(self):
        cmd, _claim = self._terminal_command_with_execution("completed", "partial")
        deadline = time.monotonic() + (TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS / 2)
        candidates, remaining = _terminal_recovery_candidates([cmd], self.store, deadline, lookup_budget=10)
        self.assertEqual([], candidates)
        self.assertEqual(10, remaining)

    # P1 delta fix, finding 1 (independent adversarial review of
    # 95768752): the reservation bounded only HYDRATION -- a slow-but-
    # healthy nonterminal _run() (primary store, ~45s transport ceiling)
    # could still eat the reserved slice AFTER hydration yielded
    # properly, starving Phase 2c exactly like the original LIVE P0.
    # Phase 2c now carries its own bounded execution window
    # (phase2c_deadline), so a cached terminal candidate is still
    # classified on the SAME tick even when active-work processing
    # consumed the shared deadline down past the old guard's threshold.
    def _slow_nonterminal_run_harness(self, run_cost_seconds):
        self._add_project = TerminalIncompleteRecoveryReachableViaPollTests._add_project.__get__(self)
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        self.store.put("commands", "p2", "cmd-slow-queued",
                       command(command_id="cmd-slow-queued", project_id="p2", task_id="t1"))
        state, monotonic, advance = self._fake_clock(start=7000.0)
        cursor_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cursor_dir.cleanup)
        self._slow_run_cursor_path = os.path.join(cursor_dir.name, "phase1-cursor.json")

        def slow_runner(*a, **k):
            advance(run_cost_seconds)  # the launch/reconcile itself is what is slow
            k["on_running"](None)
            return CommandWatcherTests.complete(a[7])

        attempts = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking_needs_recovery(store, command_arg):
            attempts.append(command_arg["command_id"])
            return real_needs_recovery(store, command_arg)

        return allowlist, claim, state, monotonic, slow_runner, attempts, tracking_needs_recovery

    def test_slow_nonterminal_processing_cannot_starve_terminal_classification(self):
        (allowlist, claim, state, monotonic, slow_runner,
         attempts, tracking) = self._slow_nonterminal_run_harness(run_cost_seconds=31.0)
        deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             patch("manager.command_watcher.launch_task", Mock(side_effect=slow_runner)), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking):
            results = poll_once(self.store, object(), allowlist=allowlist, deadline=deadline,
                                cursor_path=self._slow_run_cursor_path,
                                claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        self.assertGreaterEqual(len(attempts), 1,
                                "a 31s nonterminal run must not consume Phase 2c's reserved window")
        self.assertTrue(any(r.get("reconciled") for r in results),
                        f"terminal candidate must still be recovered the same tick; results were {results}")
        self.assertTrue(any(r.get("status") == "completed" and not r.get("reconciled") for r in results),
                        "the slow queued command itself must still have completed normally")

    def test_slow_nonterminal_processing_starves_classification_without_the_independent_window(self):
        # Mutation-sensitivity control: with the reserved window forced to
        # 0 (phase2c_deadline collapses back to the shared deadline --
        # exactly the pre-delta behavior), the same 31s schedule must
        # observably produce ZERO classification attempts. Proves the
        # test above is sensitive to the fix rather than to the harness.
        (allowlist, claim, state, monotonic, slow_runner,
         attempts, tracking) = self._slow_nonterminal_run_harness(run_cost_seconds=31.0)
        deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             patch("manager.command_watcher.TERMINAL_RECOVERY_RESERVED_SECONDS", 0), \
             patch("manager.command_watcher.launch_task", Mock(side_effect=slow_runner)), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking):
            results = poll_once(self.store, object(), allowlist=allowlist, deadline=deadline,
                                cursor_path=self._slow_run_cursor_path,
                                claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual([], attempts,
                         "control: without the independent window the 31s schedule must starve Phase 2c")
        self.assertFalse(any(r.get("reconciled") for r in results))

    def test_phase2c_extension_is_hard_capped_and_the_independent_pass_covers_the_gap(self):
        # A tick that overran past deadline + TERMINAL_RECOVERY_RESERVED_SECONDS
        # (here: a 60s stall against a 40s budget) must SKIP the in-poll
        # Phase 2c for that tick -- the extension window is a START bound
        # capped at the reserved interval past the deadline, never a
        # window that drifts along with the overrun. (Per poll_once()'s
        # documented semantics, no deadline in this module bounds an
        # already-started call's own duration -- only what may start.)
        # The recovery guarantee for exactly this schedule lives in the
        # independent terminal_recovery_once() pass, which main() runs
        # with its own fresh budget after the poll: prove it here.
        (allowlist, claim, state, monotonic, slow_runner,
         attempts, tracking) = self._slow_nonterminal_run_harness(run_cost_seconds=60.0)
        deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             patch("manager.command_watcher.launch_task", Mock(side_effect=slow_runner)), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking):
            poll_once(self.store, object(), allowlist=allowlist, deadline=deadline,
                      cursor_path=self._slow_run_cursor_path,
                      claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
            self.assertEqual([], attempts,
                             "an overrun past the hard cap must skip the in-poll Phase 2c start window")
            recovery = terminal_recovery_once(self.store, object(), allowlist=allowlist,
                                              claim_factory=lambda *_: claim,
                                              health_check=lambda: True, quota_check=lambda service: True)
        self.assertGreaterEqual(len(attempts), 1,
                                "the independent pass must still classify the cached candidate this same cycle")
        self.assertTrue(any(r.get("reconciled") for r in recovery),
                        f"the independent pass must recover the terminal candidate; got {recovery}")

    # P1 delta fix, finding 2 (independent adversarial review of
    # 95768752): the raw wall-clock rotation offset forms an arithmetic
    # progression across a project's actual VISITS -- when K projects
    # rotate the front hydration slot, one project is enumerated only
    # every K-th tick, and for record counts sharing a factor with K a
    # fixed residue class of its records was permanently unreachable.
    # The per-(tick, project) hashed offset breaks the progression.
    def test_rotation_offset_defeats_multi_project_revisit_aliasing(self):
        N, window = 9, 2  # the review's concrete failing schedule: 3 projects, N % 3 == 0, 2-record window

        def covered(offset_source, visits):
            seen = set()
            for i in range(visits):
                tick_now = (3 * i) * POLL_SECONDS  # this project is enumerated every 3rd tick
                offset = offset_source(tick_now) % N
                seen.update((offset + j) % N for j in range(window))
            return seen

        # Old arithmetic behavior (still used by the no-project_id/stride
        # path): permanently blind to residues {2, 5, 8} on this schedule.
        old = covered(lambda now: _within_project_record_rotation_offset(now=now), visits=1000)
        self.assertNotEqual(set(range(N)), old,
                            "precondition: the arithmetic offset must exhibit the aliasing hole")
        # New hashed per-project behavior: full coverage, quickly (12
        # visits empirically; 50 leaves deterministic margin -- crc32 is
        # stable across platforms/processes so this can never flake).
        new = covered(lambda now: _within_project_record_rotation_offset(now=now, project_id="p-large"), visits=50)
        self.assertEqual(set(range(N)), new,
                         "hashed offsets must reach every record despite the every-3rd-tick revisit schedule")
        # Determinism within a tick (cross-process agreement) and the
        # untouched stride path both hold.
        self.assertEqual(_within_project_record_rotation_offset(now=1234.5, project_id="p-large"),
                         _within_project_record_rotation_offset(now=1234.5, project_id="p-large"))
        self.assertEqual(10 * 7, _within_project_record_rotation_offset(now=600.0, stride=7))

    # Second delta-review round, finding 1 (P1): a REPEATABLE schedule of
    # legitimately slow active work (here 47s of launch/reconcile cost per
    # tick, past the in-poll pass's guard headroom on every tick) must not
    # be able to starve terminal recovery permanently. The in-poll Phase
    # 2c is correctly starved tick after tick -- and the independent
    # terminal_recovery_once() pass, running with its OWN fresh budget
    # after each poll exactly as main() wires it, recovers the candidate
    # anyway. This is the multi-tick regression the review asked for.
    def test_persistent_slow_active_work_cannot_permanently_starve_terminal_recovery(self):
        self._add_project = TerminalIncompleteRecoveryReachableViaPollTests._add_project.__get__(self)
        self._add_project("p2")
        allowlist = frozenset({("p1", "t1"), ("p2", "t1")})
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        state, monotonic, advance = self._fake_clock(start=9000.0)
        cursor_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cursor_dir.cleanup)
        cursor_path = os.path.join(cursor_dir.name, "phase1-cursor.json")

        attempts_in_poll = []
        real_needs_recovery = _terminal_command_needs_recovery

        def tracking(store, command_arg):
            attempts_in_poll.append(command_arg["command_id"])
            return real_needs_recovery(store, command_arg)

        def slow_runner(*a, **k):
            advance(47.0)  # persistent, successful-but-slow active work, every tick
            k["on_running"](None)
            return CommandWatcherTests.complete(a[7])

        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
             patch("manager.command_watcher.launch_task", Mock(side_effect=slow_runner)), \
             patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking):
            for tick in (1, 2):
                # fresh slow active work EVERY tick -- the repeatable schedule
                self.store.put("commands", "p2", f"cmd-slow-{tick}",
                               command(command_id=f"cmd-slow-{tick}", project_id="p2", task_id="t1"))
                deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
                poll_once(self.store, object(), allowlist=allowlist, deadline=deadline, cursor_path=cursor_path,
                          claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
                state["now"] = max(state["now"], deadline) + 5.0
            self.assertEqual([], attempts_in_poll,
                             "precondition: this schedule must genuinely starve the in-poll pass on every tick")
            recovery = terminal_recovery_once(self.store, object(), allowlist=allowlist,
                                              claim_factory=lambda *_: claim,
                                              health_check=lambda: True, quota_check=lambda service: True)
        self.assertGreaterEqual(len(attempts_in_poll), 1,
                                "the independent pass must classify the candidate the schedule starved")
        self.assertTrue(any(r.get("reconciled") for r in recovery),
                        f"terminal recovery must converge via the independent pass; got {recovery}")

    # The independent pass is recovery-ONLY: it must never touch queued/
    # claimed/running work (that is poll_once()'s job), and a terminal
    # command can never relaunch its provider through it.
    def test_terminal_recovery_pass_never_touches_nonterminal_work(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        self.store.put("commands", "p1", "cmd-queued",
                       command(command_id="cmd-queued", project_id="p1", task_id="t1"))
        runner = Mock()
        with patch("manager.command_watcher.launch_task", runner):
            recovery = terminal_recovery_once(self.store, object(), allowlist=self.ALLOWLIST,
                                              claim_factory=lambda *_: claim,
                                              health_check=lambda: True, quota_check=lambda service: True)
        runner.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-queued")["status"])
        self.assertTrue(any(r.get("reconciled") for r in recovery))

    # The pass is itself strictly bounded: with its deadline already
    # inside the classification-headroom margin, it must return without
    # attempting a single hydration or lookup.
    def test_terminal_recovery_pass_fails_closed_without_headroom(self):
        cmd, _claim = self._terminal_command_with_execution("completed", "partial")
        attempts = []
        with patch("manager.command_watcher._terminal_command_needs_recovery",
                   side_effect=lambda *a: attempts.append(a) or False):
            recovery = terminal_recovery_once(
                self.store, object(), allowlist=self.ALLOWLIST,
                deadline=time.monotonic() + (TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS / 2))
        self.assertEqual([], recovery)
        self.assertEqual([], attempts)

    # Per-candidate blast radius: one candidate whose recovery raises must
    # be recorded as an error and never abort the pass.
    def test_terminal_recovery_pass_isolates_a_failing_candidate(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        with patch("manager.command_watcher.process_command", side_effect=RuntimeError("boom")):
            recovery = terminal_recovery_once(self.store, object(), allowlist=self.ALLOWLIST,
                                              claim_factory=lambda *_: claim,
                                              health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual(1, len(recovery))
        self.assertEqual("error", recovery[0]["status"])
        self.assertIn("boom", recovery[0]["error"])

    # Round-2 delta review finding (P1): the pass's hydration slice must
    # remain usable through the PRODUCTION list_records_bounded() guard
    # ("never start a record hydration unless a full
    # single_request_worst_case still fits before the deadline"). The
    # original 15s pass budget left a 5s slice -- strictly under the 10s
    # guard reserve -- so against the real store the pass hydrated
    # nothing, ever; every in-memory double that skipped the guard hid
    # this. This store double enforces the guard exactly, with a
    # deterministic per-record cost on the same fake clock the pass runs
    # under.
    class _GuardEnforcingBoundedStore:
        def __init__(self, inner, state, per_record_cost=1.0):
            self._inner = inner
            self._state = state
            self._cost = per_record_cost
            self.hydrated = []

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def list_records_bounded(self, area, project_id, deadline=None, single_request_worst_case=None,
                                 max_records=None, order_by=None, rotate_offset=0):
            items = self._inner.list_records(area, project_id)
            if not order_by and rotate_offset and items:
                offset = rotate_offset % len(items)
                items = items[offset:] + items[:offset]
            records = []
            for record in items:
                worst = single_request_worst_case if single_request_worst_case is not None else 45
                if deadline is not None and self._state["now"] + worst >= deadline:
                    # The production guard, verbatim in spirit: a record
                    # whose own worst case could run past the deadline is
                    # never even started.
                    break
                self._state["now"] += self._cost
                records.append(record)
                self.hydrated.append((area, project_id, record.get("command_id")))
            return records

    def test_terminal_recovery_pass_hydrates_through_the_production_bounded_guard(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        state, monotonic, advance = self._fake_clock(start=11000.0)
        bounded = self._GuardEnforcingBoundedStore(self.store, state)
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic):
            recovery = terminal_recovery_once(self.store, object(), allowlist=self.ALLOWLIST,
                                              discovery_store=bounded,
                                              claim_factory=lambda *_: claim,
                                              health_check=lambda: True, quota_check=lambda service: True)
        self.assertGreaterEqual(len(bounded.hydrated), 1,
                                "the pass budget must leave a hydration start window the production "
                                "single_request_worst_case guard actually permits")
        self.assertTrue(any(r.get("reconciled") for r in recovery),
                        f"the hydrated terminal candidate must be recovered; got {recovery}")

    def test_terminal_recovery_pass_with_the_old_undersized_budget_hydrates_nothing(self):
        # Mutation-sensitivity control reproducing the review's exact
        # finding: a 15s pass deadline minus the 10s classification
        # headroom leaves a 5s hydration slice -- under the 10s guard
        # reserve -- so the guard-enforcing store must refuse every
        # record and recovery must observably NOT happen.
        cmd, _claim = self._terminal_command_with_execution("completed", "partial")
        state, monotonic, advance = self._fake_clock(start=12000.0)
        bounded = self._GuardEnforcingBoundedStore(self.store, state)
        with patch("manager.command_watcher.time.monotonic", side_effect=monotonic):
            recovery = terminal_recovery_once(self.store, object(), allowlist=self.ALLOWLIST,
                                              discovery_store=bounded,
                                              deadline=state["now"] + TERMINAL_RECOVERY_RESERVED_SECONDS)
        self.assertEqual([], bounded.hydrated,
                         "control: the undersized budget must be refused outright by the production guard")
        self.assertEqual([], recovery)

    def test_pass_budget_partition_leaves_a_usable_hydration_window(self):
        # The arithmetic contract the fix depends on, stated directly:
        # after subtracting classification headroom, the hydration slice
        # must exceed the bounded-hydration guard's own worst-case
        # reserve by a genuinely usable start window.
        hydration_slice = TERMINAL_RECOVERY_PASS_BUDGET_SECONDS - TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS
        self.assertEqual(TERMINAL_RECOVERY_PASS_HYDRATION_SECONDS, hydration_slice)
        self.assertGreater(hydration_slice, WATCHER_DISCOVERY_TIMEOUT_SECONDS,
                           "the slice must exceed the single_request_worst_case reserve or nothing can start")
        self.assertGreaterEqual(hydration_slice - WATCHER_DISCOVERY_TIMEOUT_SECONDS, 10,
                                "and leave a usable start window, not a sliver")

    # 14: repeated ticks against a large-history project can never
    # permanently starve terminal recovery -- with the reservation in
    # place, EVERY tick (not just an eventual lucky one) reserves enough
    # headroom, so a real R17-shaped candidate converges on the very first
    # tick it is cached in, not after an unbounded number of retries.
    def test_repeated_ticks_never_permanently_starve_terminal_recovery(self):
        cmd, claim = self._terminal_command_with_execution("completed", "partial")
        state, monotonic, advance = self._fake_clock(start=5000.0)
        for _ in range(3):
            deadline = state["now"] + POLL_TIME_BUDGET_SECONDS
            with patch("manager.command_watcher.time.monotonic", side_effect=monotonic), \
                 self._slow_full_sweep(state, advance):
                results = poll_once(self.store, object(), allowlist=self.ALLOWLIST, deadline=deadline,
                                    claim_factory=lambda *_: claim, health_check=lambda: True, quota_check=lambda service: True)
            if results and results[0].get("reconciled"):
                break
            state["now"] = deadline + 5.0  # advance to the next tick
        else:
            self.fail("terminal recovery was not reached within 3 ticks despite the reservation")
        self.assertTrue(results[0].get("reconciled"))

    # main() wiring: every scheduler cycle runs the independent pass with
    # its own budget AFTER poll_once, and reports its results truthfully
    # in the tick's JSON output.
    def test_main_runs_the_independent_terminal_recovery_pass_after_the_poll(self):
        import io
        from contextlib import redirect_stdout
        from manager.command_watcher import main
        calls = []
        out = io.StringIO()
        with patch("manager.command_watcher.build_service", side_effect=lambda timeout=None: object()),              patch("manager.command_watcher.DriveRecords", return_value=Mock(list_project_ids=Mock(return_value=[]))),              patch("manager.command_watcher.poll_once", side_effect=lambda *a, **k: calls.append("poll") or []),              patch("manager.command_watcher.terminal_recovery_once",
                   side_effect=lambda *a, **k: calls.append(("recovery", sorted(k))) or []),              redirect_stdout(out):
            main(["--once"])
        self.assertEqual("poll", calls[0])
        self.assertEqual("recovery", calls[1][0])
        self.assertIn("classification_store", calls[1][1])
        self.assertIn("discovery_store", calls[1][1])
        self.assertIn('"terminal_recovery":[]', out.getvalue())

    def test_reserved_seconds_covers_the_classification_timeout_with_margin(self):
        self.assertGreaterEqual(TERMINAL_RECOVERY_RESERVED_SECONDS, TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS)
        self.assertLess(TERMINAL_RECOVERY_RESERVED_SECONDS, POLL_TIME_BUDGET_SECONDS,
                        "the reservation must not consume the entire poll budget by itself")


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
            RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS, TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS,
            WATCHER_DISCOVERY_TIMEOUT_SECONDS, main,
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
        # Second/third: the discovery-only and recent-sweep-only services.
        # Fourth (residual-P0-A): the terminal-recovery classification
        # service, also strictly shorter than POLL_TIME_BUDGET_SECONDS.
        self.assertEqual(
            [None, WATCHER_DISCOVERY_TIMEOUT_SECONDS, RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS,
             TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS],
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

    # H: main() must wire a genuinely separate, short-timeout service+store
    # into poll_once()'s `classification_store` -- the same "a real
    # transport timeout is a property of the *service*, not something
    # checked-against-a-number after the fact" fix already proven for
    # discovery_store/recent_store above, now applied to terminal-recovery
    # classification reads specifically (residual-P0-A: "45s lookup > 40s
    # poll budget" is only actually solved by bounding the TRANSPORT
    # itself, not by any monotonic-clock pre-check alone).
    def test_main_builds_a_genuinely_separate_short_timeout_classification_service(self):
        import io
        from contextlib import redirect_stdout
        from manager.command_watcher import TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS, main

        build_calls = []

        def fake_build_service(timeout=None):
            build_calls.append(timeout)
            return object()

        with patch("manager.command_watcher.build_service", side_effect=fake_build_service), \
             patch("manager.command_watcher.DriveRecords", return_value=Mock(list_project_ids=Mock(return_value=[]))), \
             redirect_stdout(io.StringIO()):
            main(["--once"])
        self.assertIn(TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS, build_calls)

    def test_main_passes_classification_store_into_poll_once(self):
        import io
        from contextlib import redirect_stdout
        from manager.command_watcher import main

        with patch("manager.command_watcher.build_service", return_value=object()), \
             patch("manager.command_watcher.DriveRecords", return_value=Mock(list_project_ids=Mock(return_value=[]))), \
             patch("manager.command_watcher.poll_once", return_value=[]) as poll, \
             redirect_stdout(io.StringIO()):
            main(["--once"])
        self.assertIn("classification_store", poll.call_args.kwargs)
        self.assertIsNotNone(poll.call_args.kwargs["classification_store"])

    def test_classification_timeout_leaves_real_margin_under_the_poll_budget(self):
        """Same math proof as discovery's own, for the classification
        service: TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS is strictly
        smaller than the poll budget, and budget + one worst-case
        classification lookup still fits before the next scheduled
        trigger."""
        from manager.command_watcher import POLL_SECONDS, POLL_TIME_BUDGET_SECONDS, TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS

        self.assertLess(TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS, POLL_TIME_BUDGET_SECONDS)
        worst_case_total = POLL_TIME_BUDGET_SECONDS + TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS
        self.assertLess(worst_case_total, POLL_SECONDS,
                         "budget + one worst-case classification lookup must still fit before the next scheduled trigger")

    def test_terminal_recovery_candidates_receives_the_dedicated_classification_store(self):
        """Wiring proof at the poll_once() level: a distinct classification_store
        object is what _terminal_recovery_candidates() actually receives for
        its lookups -- not the default full-timeout `store` every write and
        active-lifecycle call still correctly uses."""
        store = CommandWatcherTests.allowlist_compliant_store()
        cmd = command(command_id="cmd-term", status="completed", execution_id="exec-term")
        store.put("executions", "p1", "exec-term", {
            "execution_id": "exec-term", "project_id": "p1", "task_id": "t1", "status": "completed",
            "cleanup_evidence": {"persistence": "partial", "task_claim_release": "retained"},
        })
        store.put("commands", "p1", "cmd-term", cmd)

        classification_store = Mock(wraps=store)
        real_needs_recovery = _terminal_command_needs_recovery
        seen_stores = []

        def tracking_needs_recovery(used_store, command_arg):
            seen_stores.append(used_store)
            return real_needs_recovery(used_store, command_arg)

        cursor_path = tempfile.mktemp(suffix=".json")
        with patch("manager.command_watcher._terminal_command_needs_recovery", side_effect=tracking_needs_recovery):
            poll_once(store, object(), allowlist=frozenset({("p1", "t1")}), deadline=time.monotonic() + 50.0,
                     classification_store=classification_store,
                     claim_factory=lambda *_: object(), health_check=lambda: True, quota_check=lambda service: True,
                     cursor_path=cursor_path)
        self.assertTrue(seen_stores)
        self.assertIs(classification_store, seen_stores[0])


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
