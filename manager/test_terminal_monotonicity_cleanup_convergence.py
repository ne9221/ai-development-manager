import socket
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from manager.command_watcher import (
    _attention,
    _reconcile_active,
    _terminal_cleanup_confirmed,
    _write,
)
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import reserve_execution
from manager.task_claims import TaskClaimConflict, claim_task_execution
from manager.tasks import TaskError, create_project, create_task, now_iso, validate
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry
from manager.trusted_ingress import REQUIRED_TASK_POLICIES


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TerminalMonotonicityAndCleanupTruthTests(unittest.TestCase):
    ALLOWLIST = frozenset({("p1", "t1")})

    def setUp(self):
        self.store = Store()
        create_project(self.store, project())
        create_task(self.store, task(read_only=True), assign=False)
        compliant = self.store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", compliant)
        self.registry = MemoryClaimRegistry()
        self.project_id = "p1"
        self.task_id = "t1"
        self.execution_id = "command-cmd-1"

    def _setup_terminal_state(self, terminal_status="completed", task_claim_release="retained",
                              persistence="complete", active_execution_id="command-cmd-1",
                              claim_in_gcs=False, command_status="running",
                              command_result=None, session_id="codex:01a05537-real-session"):
        reserve_execution(self.store, self.project_id, self.task_id, self.execution_id, "codex", {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(
                self.store, object(), None, self.project_id, self.task_id, self.execution_id, "codex",
                "read_only", task_claim_registry=self.registry,
            )
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        exec_doc["session_id"] = session_id
        exec_doc["provider_evidence"] = {
            "host": socket.gethostname()[:100],
            "pid": 999999,
            "creation_identity": "proc-mon-test",
            "started_at": _now(),
        }
        self.store.put("executions", self.project_id, self.execution_id, exec_doc)

        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(
                self.store, object(), None, self.registry, self.project_id, self.task_id,
                self.execution_id, "codex", terminal_status, 1, True,
                summary=f"Execution terminal {terminal_status}",
            )

        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        exec_doc["cleanup_evidence"]["task_claim_release"] = task_claim_release
        if persistence != "complete":
            exec_doc["cleanup_evidence"]["persistence"] = "incomplete"
            exec_doc["cleanup_evidence"]["persisted"] = ["execution"]
        validate("execution", exec_doc)
        self.store.put("executions", self.project_id, self.execution_id, exec_doc)

        if not claim_in_gcs:
            self.registry.document = None

        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        task_doc["source_context"] = {"active_execution_id": active_execution_id}
        if terminal_status == "completed":
            task_doc["status"] = "completed"
        else:
            task_doc["status"] = "blocked"
        validate("task", task_doc)
        self.store.put("tasks", self.project_id, self.task_id, task_doc)

        cmd = command(status=command_status, execution_id=self.execution_id, claimed_at=_now(),
                      result=command_result, completed_at=_now() if command_status in ("completed", "failed") else None)
        self.store.put("commands", self.project_id, "cmd-1", cmd)
        return cmd

    # -------------------------------------------------------------------------
    # A. Round 46 exact case
    # -------------------------------------------------------------------------
    def test_A_round46_exact_claim_already_absent_convergences_to_terminal(self):
        """A. Round 46: Execution terminal interrupted, persistence complete,
        task_claim_release=retained, GCS claim ABSENT -> converges to released and Command terminal."""
        cmd = self._setup_terminal_state(terminal_status="interrupted", task_claim_release="retained",
                                         command_status="running", claim_in_gcs=False)
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("failed", outcome.get("status"))
        self.assertTrue(outcome.get("reconciled"))

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("released", refreshed_exec["cleanup_evidence"]["task_claim_release"])

        stored_cmd = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("failed", stored_cmd["status"])
        self.assertEqual("interrupted", stored_cmd["result"]["status"])
        self.assertEqual("codex:01a05537-real-session", stored_cmd["result"]["session_id"])

    # -------------------------------------------------------------------------
    # B. Round 38 exact stale snapshot downgrade prevention
    # -------------------------------------------------------------------------
    def test_B_round38_stale_reconciler_cannot_downgrade_completed_command_to_attention(self):
        """B. Round 38: Command is completed in Store with real session_id.
        A stale reconciler holding a running snapshot attempting _attention must NOT downgrade Command."""
        # Drive Command is already completed
        res = {"status": "completed", "session_id": "codex:01a05537-round38-session", "error_kind": None}
        self._setup_terminal_state(terminal_status="completed", task_claim_release="retained",
                                  command_status="completed", command_result=res,
                                  session_id="codex:01a05537-round38-session", claim_in_gcs=False)

        # Stale snapshot held by reconciler is 'running'
        stale_snapshot = command(status="running", execution_id=self.execution_id, claimed_at=_now())

        # Attempt _attention on the stale snapshot
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        _attention(self.store, stale_snapshot, exec_doc, "terminal_cleanup_not_confirmed")

        # Command in Store must STILL be completed with original real session_id
        stored_cmd = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", stored_cmd["status"])
        self.assertIsNotNone(stored_cmd["result"])
        self.assertEqual("completed", stored_cmd["result"]["status"])
        self.assertEqual("codex:01a05537-round38-session", stored_cmd["result"]["session_id"])

    # -------------------------------------------------------------------------
    # C. Result preservation
    # -------------------------------------------------------------------------
    def test_C_completed_result_cannot_be_cleared_to_null_by_stale_write(self):
        """C. Command completed + real session_id. Non-terminal or stale write cannot set result to null."""
        res = {"status": "completed", "session_id": "codex:01a05537-session-c", "error_kind": None}
        self._setup_terminal_state(terminal_status="completed", command_status="completed", command_result=res)

        # Attempt _write with attention and result=None
        downgrade_cmd = command(status="attention", execution_id=self.execution_id, claimed_at=_now(), result=None)
        written = _write(self.store, downgrade_cmd)

        self.assertEqual("completed", written["status"])
        self.assertEqual("codex:01a05537-session-c", written["result"]["session_id"])

        stored = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("codex:01a05537-session-c", stored["result"]["session_id"])

    # -------------------------------------------------------------------------
    # D. Interrupted / failed terminal cannot be downgraded to attention
    # -------------------------------------------------------------------------
    def test_D_interrupted_terminal_cannot_be_downgraded_to_attention(self):
        """D. Execution interrupted/failed with real session_id. Stale reconciler attention cannot downgrade."""
        res = {"status": "interrupted", "session_id": "codex:01a05537-session-d", "error_kind": None}
        self._setup_terminal_state(terminal_status="interrupted", command_status="failed", command_result=res)

        stale_cmd = command(status="running", execution_id=self.execution_id, claimed_at=_now())
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        _attention(self.store, stale_cmd, exec_doc, "terminal_cleanup_not_confirmed")

        stored = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertEqual("interrupted", stored["result"]["status"])
        self.assertEqual("codex:01a05537-session-d", stored["result"]["session_id"])

    # -------------------------------------------------------------------------
    # E. Legitimate enrichment preserved
    # -------------------------------------------------------------------------
    def test_E_legitimate_terminal_enrichment_allowed(self):
        """E. Legitimate enrichment (e.g. updating recovery_reason or result) on terminal command is allowed."""
        res1 = {"status": "interrupted", "session_id": "codex:01a05537-session-e", "error_kind": None}
        self._setup_terminal_state(terminal_status="interrupted", command_status="failed", command_result=res1)

        # Enrich recovery_reason while keeping terminal truth
        enriched = command(status="failed", execution_id=self.execution_id, claimed_at=_now(),
                           result=res1, recovery_reason="provider_process_stopped_verified")
        _write(self.store, enriched)

        stored = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("failed", stored["status"])
        self.assertEqual("provider_process_stopped_verified", stored["recovery_reason"])
        self.assertEqual("codex:01a05537-session-e", stored["result"]["session_id"])

    # -------------------------------------------------------------------------
    # F. Persistence enrichment preserved
    # -------------------------------------------------------------------------
    def test_F_persistence_incomplete_retries_and_converges(self):
        """F. Persistence incomplete triggers retry; when completed, converges cleanly."""
        cmd = self._setup_terminal_state(terminal_status="interrupted", persistence="incomplete")

        def succeed_retry(store, proj, task_id, exec_id, *_args, **_kwargs):
            e = store.get("executions", proj, exec_id)
            e["cleanup_evidence"]["persistence"] = "complete"
            e["cleanup_evidence"]["persisted"] = ["execution", "handoff", "task"]
            store.put("executions", proj, exec_id, e)
            return True

        with patch("manager.command_watcher.retry_incomplete_terminal_persistence", side_effect=succeed_retry), \
             patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("failed", outcome.get("status"))
        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("complete", refreshed_exec["cleanup_evidence"]["persistence"])
        self.assertEqual("released", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # G. Claim ABSENT converges
    # -------------------------------------------------------------------------
    def test_G_explicit_absent_claim_converges(self):
        """G. Explicit successful registry read with claim=None converges to released."""
        cmd = self._setup_terminal_state(terminal_status="completed", task_claim_release="retained", claim_in_gcs=False)
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("completed", outcome.get("status"))
        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("released", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # H. Claim UNKNOWN fails closed
    # -------------------------------------------------------------------------
    def test_H_claim_read_unknown_fails_closed(self):
        """H. Claim read error / timeout fails closed and does NOT declare released."""
        cmd = self._setup_terminal_state(terminal_status="completed", task_claim_release="retained")

        with patch("manager.command_watcher.check_task_execution_claim", side_effect=TaskError("503 timeout")):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        self.assertEqual("terminal_cleanup_reconciliation_unknown", outcome.get("recovery_reason"))

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # I. Newer claim generation protected
    # -------------------------------------------------------------------------
    def test_I_newer_claim_generation_refuses_old_execution_release(self):
        """I. If GCS claim is owned by a newer execution, old reconciler cannot touch or claim released."""
        cmd = self._setup_terminal_state(terminal_status="completed", task_claim_release="retained")
        claim_task_execution(self.registry, self.project_id, self.task_id, "command-cmd-newer", "codex", _now())

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # J. Newer execution authority protected
    # -------------------------------------------------------------------------
    def test_J_newer_execution_on_task_refuses_old_claim_absent_sync(self):
        """J. If Task already moved to newer execution, old execution cannot claim unreleased authority."""
        cmd = self._setup_terminal_state(terminal_status="completed", task_claim_release="retained",
                                         active_execution_id="command-cmd-newer", claim_in_gcs=False)
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        self.assertEqual("terminal_cleanup_task_reclaimed_by_newer_execution", outcome.get("recovery_reason"))

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # K. Active provider / session fails closed
    # -------------------------------------------------------------------------
    def test_K_active_provider_process_fails_closed(self):
        """K. Claim absent but matching provider process still live on host -> fail closed."""
        cmd = self._setup_terminal_state(terminal_status="interrupted", task_claim_release="retained", claim_in_gcs=False)

        with patch("manager.command_watcher.process_identity_state", return_value="live"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        self.assertEqual("terminal_cleanup_provider_still_live", outcome.get("recovery_reason"))

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # L. Dual reconciler race
    # -------------------------------------------------------------------------
    def test_L_dual_reconciler_race_terminal_truth_wins(self):
        """L. Reconciler 1 reaches terminal; Reconciler 2 with stale running snapshot tries attention.
        Terminal truth wins; state never flips to attention."""
        cmd = self._setup_terminal_state(terminal_status="completed", task_claim_release="retained", claim_in_gcs=False)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res1 = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("completed", res1["status"])

        # Reconciler 2 attempts attention with stale snapshot
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        _attention(self.store, cmd, exec_doc, "terminal_cleanup_not_confirmed")

        stored = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertIsNotNone(stored["result"])

    # -------------------------------------------------------------------------
    # M. Repeated natural ticks idempotency
    # -------------------------------------------------------------------------
    def test_M_repeated_20_ticks_idempotency(self):
        """M. Terminal converged state reconciled across 20 successive ticks remains strictly idempotent."""
        cmd = self._setup_terminal_state(terminal_status="completed", task_claim_release="retained", claim_in_gcs=False)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            for _ in range(20):
                outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)
                self.assertEqual("completed", outcome["status"])

        stored = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("completed", stored["result"]["status"])
        self.assertEqual("codex:01a05537-real-session", stored["result"]["session_id"])

    # -------------------------------------------------------------------------
    # N. Multithreaded Race: Thread A (stale attention) vs Thread B (write completed)
    # -------------------------------------------------------------------------
    def test_N_multithreaded_stale_attention_vs_completed_write(self):
        """Thread A holds stale running snapshot and attempts _attention;
        Thread B writes completed with real session_id. Completed always wins."""
        import threading
        self._setup_terminal_state(terminal_status="completed", task_claim_release="released",
                                  command_status="running", claim_in_gcs=False)
        stale_cmd = command(status="running", execution_id=self.execution_id, claimed_at=_now())
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        completed_cmd = command(status="completed", execution_id=self.execution_id, claimed_at=_now(),
                                completed_at=_now(), result={"status": "completed", "session_id": "codex:01a05537-threadb", "error_kind": None})

        def run_thread_a():
            _attention(self.store, stale_cmd, exec_doc, "terminal_cleanup_not_confirmed")

        def run_thread_b():
            _write(self.store, completed_cmd)

        t_b = threading.Thread(target=run_thread_b)
        t_a = threading.Thread(target=run_thread_a)
        t_b.start()
        t_a.start()
        t_b.join()
        t_a.join()

        stored = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", stored["status"])
        self.assertEqual("codex:01a05537-threadb", stored["result"]["session_id"])

    # -------------------------------------------------------------------------
    # O. Multithreaded Dual Reconciler: Cleanup Enrichment vs Stale Attention
    # -------------------------------------------------------------------------
    def test_O_multithreaded_cleanup_enrichment_vs_stale_attention(self):
        """Reconciler 1 performs cleanup absent convergence (retained -> released),
        Reconciler 2 attempts attention with stale snapshot. Final state is terminal + released."""
        import threading
        cmd = self._setup_terminal_state(terminal_status="completed", task_claim_release="retained", claim_in_gcs=False)
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)

        def reconciler_1():
            with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
                _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        def reconciler_2():
            _attention(self.store, cmd, exec_doc, "terminal_cleanup_not_confirmed")

        t1 = threading.Thread(target=reconciler_1)
        t2 = threading.Thread(target=reconciler_2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        stored_cmd = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", stored_cmd["status"])
        self.assertEqual("codex:01a05537-real-session", stored_cmd["result"]["session_id"])
        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("released", refreshed_exec["cleanup_evidence"]["task_claim_release"])


if __name__ == "__main__":
    unittest.main()
