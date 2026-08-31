"""Deterministic regression tests proving Command.result preserves Execution truth

Covers:
1. provider_process_stopped: Execution interrupted with valid session_id, Command.result must preserve status='interrupted' and real session_id.
2. completed: Execution completed with valid session_id, Command.result must preserve status='completed' and real session_id.
3. interrupted: Execution interrupted with valid session_id, Command.result must preserve status='interrupted' and real session_id.
4. cleanup transient then recovered: When _terminal_cleanup_confirmed is not yet satisfied, Command terminal derivation still uses Execution truth.
5. concurrent reconciler: Reconciler resolving terminal state concurrently does not lose session_id.
6. stale Command snapshot: Attempting to write a fallback session_id=None result over an already-terminal Command does not clobber terminal truth.
7. terminal Execution already persisted: When Execution is terminal in store, worker/process_command never downgrades to generic prelaunch TaskError.
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from manager.command_watcher import (
    _existing_terminal, _reconcile_active, _run_claimed_command, _terminal, _write, process_command,
)
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import reserve_execution
from manager.tasks import TaskError, create_project, create_task, now_iso, validate
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry
from manager.trusted_ingress import REQUIRED_TASK_POLICIES


class CommandTerminalTruthTests(unittest.TestCase):
    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = Store()
        create_project(self.store, project())
        create_task(self.store, task(read_only=True), assign=False)
        compliant = self.store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", compliant)
        self.claim_registry = MemoryClaimRegistry()

    def _setup_running_execution(self, provider="codex", execution_id="command-cmd-1"):
        reserve_execution(self.store, "p1", "t1", execution_id, provider, {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(
                self.store, object(), None, "p1", "t1", execution_id, provider,
                "read_only", task_claim_registry=self.claim_registry,
            )
        exec_doc = self.store.get("executions", "p1", execution_id)
        exec_doc["session_id"] = f"{provider}:session-truth-12345"
        exec_doc["provider_evidence"] = {
            "host": "test-host",
            "pid": 12345,
            "creation_identity": "proc-12345",
            "started_at": "2026-08-30T21:00:00Z",
        }
        self.store.put("executions", "p1", execution_id, exec_doc)
        return exec_doc

    def test_scenario_1_provider_process_stopped_preserves_interrupted_truth(self):
        """Scenario 1: provider_process_stopped terminalizes execution as interrupted with session_id.
        Worker exception must NOT overwrite Command.result with TaskError / session_id=null.
        """
        self._setup_running_execution(provider="codex", execution_id="command-cmd-1")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(
                self.store, object(), None, self.claim_registry, "p1", "t1",
                "command-cmd-1", "codex", "interrupted", 1, True,
                summary="Recovery: provider_process_stopped; provider stop proven on owning host",
            )
        cmd = command(status="claimed", execution_id="command-cmd-1", claimed_at="2026-08-30T21:00:00Z")
        self.store.put("commands", "p1", "cmd-1", cmd)

        with patch("manager.command_watcher.launch_task", side_effect=TaskError("codex runner interrupted: provider_process_stopped")):
            _run_claimed_command(
                self.store, object(), cmd, Mock(), Mock(),
                lambda *_: self.claim_registry, None, [], "watcher_poll",
            )

        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertIsNotNone(stored.get("result"))
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("codex:session-truth-12345", stored["result"]["session_id"])
        self.assertIsNone(stored["result"]["error_kind"])

    def test_scenario_2_completed_execution_preserves_completed_truth(self):
        """Scenario 2: Execution completed with real session_id, worker exception during teardown preserves completed."""
        self._setup_running_execution(provider="codex", execution_id="command-cmd-1")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(
                self.store, object(), None, self.claim_registry, "p1", "t1",
                "command-cmd-1", "codex", "completed", 1, True,
                summary="Execution completed successfully",
            )
        cmd = command(status="claimed", execution_id="command-cmd-1", claimed_at="2026-08-30T21:00:00Z")
        self.store.put("commands", "p1", "cmd-1", cmd)

        with patch("manager.command_watcher.launch_task", side_effect=RuntimeError("teardown glitch")):
            _run_claimed_command(
                self.store, object(), cmd, Mock(), Mock(),
                lambda *_: self.claim_registry, None, [], "watcher_poll",
            )

        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("completed", stored["result"]["status"])
        self.assertEqual("codex:session-truth-12345", stored["result"]["session_id"])

    def test_scenario_3_interrupted_claude_execution_preserves_session_truth(self):
        """Scenario 3: Claude execution interrupted, Command result must preserve claude session_id and interrupted status."""
        self._setup_running_execution(provider="claude", execution_id="command-cmd-1")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(
                self.store, object(), None, self.claim_registry, "p1", "t1",
                "command-cmd-1", "claude", "interrupted", 1, True,
                summary="Recovery: provider_process_stopped",
            )
        cmd = command(status="claimed", execution_id="command-cmd-1", claimed_at="2026-08-30T21:00:00Z", provider="claude")
        self.store.put("commands", "p1", "cmd-1", cmd)

        with patch("manager.command_watcher.launch_task", side_effect=TaskError("claude process stopped")):
            _run_claimed_command(
                self.store, object(), cmd, Mock(), Mock(),
                lambda *_: self.claim_registry, None, [], "watcher_poll",
            )

        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("claude:session-truth-12345", stored["result"]["session_id"])

    def test_scenario_4_transient_cleanup_lag_still_uses_terminal_execution_truth(self):
        """Scenario 4: If cleanup_evidence is transiently incomplete (e.g. _terminal_cleanup_confirmed=False),
        the worker fallback must still derive truth from the terminal Execution in store rather than degrading to TaskError.
        """
        self._setup_running_execution(provider="codex", execution_id="command-cmd-1")
        exec_doc = self.store.get("executions", "p1", "command-cmd-1")
        exec_doc["status"] = "interrupted"
        exec_doc["cleanup_evidence"] = None  # incomplete cleanup evidence
        self.store.put("executions", "p1", "command-cmd-1", exec_doc)

        cmd = command(status="claimed", execution_id="command-cmd-1", claimed_at="2026-08-30T21:00:00Z")
        self.store.put("commands", "p1", "cmd-1", cmd)

        self.assertIsNone(_existing_terminal(self.store, cmd))

        with patch("manager.command_watcher.launch_task", side_effect=TaskError("provider stopped")):
            _run_claimed_command(
                self.store, object(), cmd, Mock(), Mock(),
                lambda *_: self.claim_registry, None, [], "watcher_poll",
            )

        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("codex:session-truth-12345", stored["result"]["session_id"])
        self.assertIsNone(stored["result"]["error_kind"])

    def test_scenario_5_concurrent_reconciler_result_is_never_downgraded_by_worker(self):
        """Scenario 5: If reconciler or worker already persisted terminal truth with session_id,
        subsequent writes with session_id=None must not clobber it.
        """
        cmd = command(
            status="failed",
            execution_id="command-cmd-1",
            result={"status": "interrupted", "execution_id": "command-cmd-1", "session_id": "codex:reconciled-session", "error_kind": None},
        )
        self.store.put("commands", "p1", "cmd-1", cmd)

        stale_cmd = command(
            status="failed",
            execution_id="command-cmd-1",
            result={"status": "error", "execution_id": "command-cmd-1", "session_id": None, "error_kind": "TaskError"},
        )
        _write(self.store, stale_cmd)

        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("codex:reconciled-session", stored["result"]["session_id"])

    def test_scenario_6_stale_command_snapshot_write_protection(self):
        """Scenario 6: _write refuses to overwrite completed command with session_id=None error."""
        cmd = command(
            status="completed",
            execution_id="command-cmd-1",
            result={"status": "completed", "execution_id": "command-cmd-1", "session_id": "codex:completed-sess", "error_kind": None},
        )
        self.store.put("commands", "p1", "cmd-1", cmd)

        stale_cmd = command(
            status="failed",
            execution_id="command-cmd-1",
            result={"status": "error", "execution_id": "command-cmd-1", "session_id": None, "error_kind": "TaskError"},
        )
        _write(self.store, stale_cmd)

        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("codex:completed-sess", stored["result"]["session_id"])

    def test_scenario_7_terminal_execution_already_persisted_in_process_command(self):
        """Scenario 7: In process_command (sync launch), when Execution was terminalized in store before exception,
        process_command produces terminal truth with session_id rather than TaskError.
        """
        self._setup_running_execution(provider="codex", execution_id="command-cmd-1")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(
                self.store, object(), None, self.claim_registry, "p1", "t1",
                "command-cmd-1", "codex", "interrupted", 1, True,
                summary="Recovery: provider_process_stopped",
            )
        self.store.put("commands", "p1", "cmd-1", command())

        with patch("manager.command_watcher.launch_task", side_effect=TaskError("runner stopped")):
            result = process_command(
                self.store, object(), command(),
                claim_factory=lambda *_: self.claim_registry,
                allowlist=self.ALLOWLIST,
                health_check=lambda: True,
                quota_check=lambda service: True,
                async_launch=False,
            )

        self.assertEqual("failed", result["status"])
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("codex:session-truth-12345", stored["result"]["session_id"])


class WriteGuardPrecisionTests(unittest.TestCase):
    """The 6 write-guard precision cases explicitly required alongside this
    fix: the guard in _write() must block only a real authority downgrade
    (real session_id/specific terminal status -> generic error/null
    session), never a legitimate monotonic update. Cases A/B/E substantially
    overlap CommandTerminalTruthTests' scenarios 1/2/3/5/6 above (kept there
    for their AG provenance); this class adds the remaining C/D/F cases plus
    one more explicit A/B-style dual-writer case for completeness."""

    def setUp(self):
        self.store = Store()
        create_project(self.store, project())
        create_task(self.store, task(read_only=True), assign=False)

    def test_case_a_stale_taskerror_null_session_does_not_overwrite_interrupted_truth(self):
        real = command(status="failed", execution_id="command-cmd-1",
                        result={"status": "interrupted", "execution_id": "command-cmd-1",
                                "session_id": "codex:real-session", "error_kind": None})
        self.store.put("commands", "p1", "cmd-1", real)
        stale = command(status="failed", execution_id="command-cmd-1",
                         result={"status": "error", "execution_id": "command-cmd-1",
                                 "session_id": None, "error_kind": "TaskError"})
        _write(self.store, stale)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("codex:real-session", stored["result"]["session_id"])

    def test_case_b_generic_failed_snapshot_does_not_overwrite_completed_truth(self):
        real = command(status="completed", execution_id="command-cmd-1",
                        result={"status": "completed", "execution_id": "command-cmd-1",
                                "session_id": "codex:real-session", "error_kind": None})
        self.store.put("commands", "p1", "cmd-1", real)
        stale = command(status="failed", execution_id="command-cmd-1",
                         result={"status": "error", "execution_id": "command-cmd-1",
                                 "session_id": None, "error_kind": "TaskError"})
        _write(self.store, stale)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("codex:real-session", stored["result"]["session_id"])

    def test_case_c_cleanup_convergence_from_incomplete_to_confirmed_is_allowed(self):
        """Terminal cleanup subsequently going incomplete -> confirmed is a
        legitimate metadata update on the EXECUTION record (not gated by
        _write() at all, which only guards Command writes) -- and once
        _reconcile_active re-derives a Command write from that now-complete
        execution, the write carries a real session_id, so the guard's
        null-session activation condition never even triggers."""
        helper = _RunningCommandHelper()
        active, claim, execution = helper.running_command()
        execution["status"] = "completed"
        execution["completed_at"] = now_iso()
        execution["notes"] = ["codex turn completed"]
        execution["cleanup_evidence"] = {
            "provider_outcome": "completed", "persistence": "partial",
            "persisted": ["execution", "handoff"], "writer_release": "not_required",
            "task_claim_release": "retained", "errors": ["persistence failed: terminal task persistence verification failed"],
        }
        helper.store.put("executions", "p1", "command-cmd-1", execution)
        result = _reconcile_active(helper.store, object(), active, lambda *_: claim)
        self.assertEqual("completed", result["status"])
        stored = helper.store.get("commands", "p1", "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("completed", stored["result"]["status"])
        self.assertIsNone(claim.document, "cleanup convergence must also release the now-confirmed claim")

    def test_case_d_recovery_reason_completion_is_not_blocked_by_write_guard(self):
        """A later attention() write that only refines/clears
        recovery_reason (status stays 'attention', never 'completed' or
        'failed') is never subject to the terminal write guard at all --
        the guard only inspects command.status in (completed, failed)."""
        attn = command(status="attention", execution_id="command-cmd-1",
                        recovery_reason="terminal_cleanup_not_confirmed", result=None)
        self.store.put("commands", "p1", "cmd-1", attn)
        refined = {**attn, "recovery_reason": "terminal_cleanup_reconciliation_unknown"}
        written = _write(self.store, refined)
        self.assertEqual("terminal_cleanup_reconciliation_unknown", written["recovery_reason"])
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("terminal_cleanup_reconciliation_unknown", stored["recovery_reason"])

    def test_case_f_genuinely_newer_canonical_truth_may_update_a_stale_command(self):
        """A command previously (wrongly, e.g. pre-fix) stuck with a generic
        null-session result CAN legitimately be updated once a real,
        session-bearing terminal truth becomes available -- the guard only
        ever blocks incoming null-session writes, never incoming
        real-session ones, regardless of what is currently stored."""
        stale = command(status="failed", execution_id="command-cmd-1",
                         result={"status": "error", "execution_id": "command-cmd-1",
                                 "session_id": None, "error_kind": "TaskError"})
        self.store.put("commands", "p1", "cmd-1", stale)
        newer_truth = command(status="failed", execution_id="command-cmd-1",
                               result={"status": "interrupted", "execution_id": "command-cmd-1",
                                       "session_id": "codex:newly-confirmed-session", "error_kind": None})
        _write(self.store, newer_truth)
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("codex:newly-confirmed-session", stored["result"]["session_id"])


class _RunningCommandHelper:
    """Minimal standalone reuse of CommandWatcherTests.running_command()'s
    setup so WriteGuardPrecisionTests doesn't need to inherit its unrelated
    test methods."""

    def __init__(self):
        self.store = Store()
        create_project(self.store, project())
        create_task(self.store, task(read_only=True), assign=False)
        compliant = self.store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", compliant)

    def running_command(self):
        from copy import deepcopy as _deepcopy
        from datetime import datetime, timezone, timedelta
        from manager.executions import reserve_execution
        from manager.execution_lifecycle import enter_running_gate
        now = datetime.now(timezone.utc)
        started = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", "command-cmd-1", "codex",
                               "read_only", started_at=started, task_claim_registry=claim)
        execution = self.store.get("executions", "p1", "command-cmd-1")
        execution["session_id"] = "codex:session-truth-12345"
        execution["provider_evidence"] = {
            "host": "test-host", "pid": 12345, "creation_identity": "proc-12345", "started_at": started,
        }
        self.store.put("executions", "p1", "command-cmd-1", execution)
        active = command(status="running", execution_id="command-cmd-1", claimed_at=started, provider="codex")
        self.store.put("commands", "p1", "cmd-1", active)
        return active, claim, execution


if __name__ == "__main__":
    unittest.main()
