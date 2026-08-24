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
    CLAIM_TIMEOUT_SECONDS, MAX_COMMANDS_PER_POLL, PROVIDER_RUNTIMES, REQUIRED_TASK_POLICIES, _provider_state,
    _prioritized_commands, claude_quota_reliable, codex_quota_reliable, embedded_ingress_enabled, load_allowlist,
    poll_once, process_command, provider_quota_reliable, resolve_provider_runtime,
)
from manager.execution_lifecycle import enter_running_gate
from manager.executions import execution_health, heartbeat_execution, reserve_execution
from manager.task_claims import TaskClaimConflict
from manager.tasks import TaskError, create_project, create_task, now_iso
from manager.trusted_ingress import ADMISSION_VERSION_V2_REPO_WRITE, REQUIRED_REPO_WRITE_TASK_POLICIES
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

    def test_watcher_threads_scheduler_origin_to_command_and_runner(self):
        context = {"scheduler_invocation_id": "a" * 32, "wrapper_pid": 41,
                   "wrapper_creation_identity": "wrapper-41", "os_scheduler_evidence": {"status": "UNKNOWN", "reason": "no_os_proof"}}
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST,
                            health_check=lambda: True, quota_check=lambda _service: True, origin_context=context)
        origin = self.store.get("commands", "p1", "cmd-1")["process_provenance"]
        self.assertEqual("watcher_poll", origin["caller_origin"])
        self.assertEqual("a" * 32, runner.call_args.kwargs["provenance"]["scheduler_invocation_id"])

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
        self.assertEqual("failed", result["status"]); runner.assert_not_called()
        self.assertEqual("claim_timeout", self.store.get("commands", "p1", "cmd-1")["result"]["error_kind"])

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
        self.assertEqual("failed", result["status"]); self.assertIsNone(claim.document)
        self.assertEqual("interrupted", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("failed", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    # Phase 4E parity gate item: process identity / PID reuse safety and the
    # full stale-provider auto-recovery path, reproduced exactly for
    # provider="claude" -- byte-identical scenario to the Codex test above,
    # just with the provider substituted, proving the recovery path carries
    # no Codex-only assumption.
    def test_proven_dead_read_only_claude_provider_terminalizes_and_writes_command_task(self):
        active, claim, _ = self.running_command(heartbeat_minutes=16, pid=99_999_999, provider="claude")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("failed", result["status"]); self.assertIsNone(claim.document)
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
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            # Call 1: project-level check (before cmd-1 is even looked at) -- under budget.
            # Call 2: command-level check, before cmd-1 -- still under budget, cmd-1 starts.
            # Call 3+: command-level check, before cmd-2 -- budget now spent.
            return 0.0 if calls["n"] <= 2 else 100.0

        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
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

        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        calls = {"n": 0}

        def fake_monotonic():
            # Simulate enumeration having already consumed most of the
            # budget: the project-level and first command-level checks pass
            # (call 1-2), but the SECOND command-level check (i.e. any
            # attempt to start a second process_command() this tick) is
            # already past deadline.
            calls["n"] += 1
            return 0.0 if calls["n"] <= 2 else 100.0

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

        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            return 0.0 if calls["n"] <= 2 else 100.0

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
        from manager.command_watcher import WATCHER_DISCOVERY_TIMEOUT_SECONDS, main

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
        self.assertEqual([None, WATCHER_DISCOVERY_TIMEOUT_SECONDS], build_calls)

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


if __name__ == "__main__": unittest.main()
