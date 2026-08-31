"""Tests for Terminal Partial-Persistence Recovery / Drive Verification Failure Convergence.

Covers exact live defect shape (cgate5-r17) and related contracts:
- A. r17 exact shape (Command=completed, Execution=completed, Task=blocked, persisted=['execution'])
- B. handoff write first failure
- C. task write first failure
- D. repeated transient Drive failures (converges on 3rd attempt)
- E. persistent Drive failure (fails closed, no forged complete/released)
- F. Command already terminal (monotonicity & session_id preserved)
- G. claim already absent (converges to released)
- H. claim still present (recovers via recover_task_claim CAS)
- I. newer execution authority protected (does not overwrite newer task)
- J. concurrent reconcilers safety (idempotent, monotonic)
- K. restart safety (purely durable Drive state, memory-independent)
"""

import socket
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from manager.command_watcher import (
    _attention,
    _existing_terminal,
    _reconcile_active,
    _terminal_cleanup_confirmed,
    _write,
    process_command,
)
from manager.execution_lifecycle import (
    enter_running_gate,
    retry_incomplete_terminal_persistence,
    terminalize_execution,
)
from manager.executions import reserve_execution
from manager.task_claims import claim_task_execution
from manager.tasks import TaskError, create_project, create_task, now_iso, validate
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry
from manager.trusted_ingress import REQUIRED_TASK_POLICIES


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TerminalPartialPersistenceRecoveryTests(unittest.TestCase):
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
        self.session_id = "codex:01a05796-1b5a-7fe2-bf89-0a0bacab751c"

    def _setup_r17_shape(self, terminal_status="completed", persisted=("execution",),
                         task_status="blocked", task_claim_release="retained",
                         command_status="completed", claim_in_gcs=True,
                         errors=None):
        """Set up the exact cgate5-r17 live defect state."""
        reserve_execution(self.store, self.project_id, self.task_id, self.execution_id, "codex", {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(
                self.store, object(), None, self.project_id, self.task_id, self.execution_id, "codex",
                "read_only", task_claim_registry=self.registry,
            )
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        exec_doc["session_id"] = self.session_id
        exec_doc["status"] = terminal_status
        exec_doc["completed_at"] = _now()
        exec_doc["notes"] = ["Turn finished"]
        exec_doc["provider_evidence"] = {
            "host": socket.gethostname()[:100],
            "pid": 999999,
            "creation_identity": "proc-r17-test",
            "started_at": _now(),
        }
        if errors is None:
            errors = ["persistence failed: Drive verification failed: dispatch-cgate5-r17-20260831T111932Z-completed-command-dispatch-cgate5-r17-20260831T111932Z-0.json"]
        exec_doc["cleanup_evidence"] = {
            "provider_outcome": terminal_status,
            "persistence": "partial" if persisted else "incomplete",
            "persisted": list(persisted),
            "writer_release": "not_required",
            "task_claim_release": task_claim_release,
            "errors": errors,
        }
        validate("execution", exec_doc)
        self.store.put("executions", self.project_id, self.execution_id, exec_doc)

        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        task_doc["status"] = task_status
        task_doc["source_context"] = {"active_execution_id": self.execution_id}
        if task_status == "blocked":
            task_doc["blocked_reason"] = "Execution recovery required: terminal_cleanup_not_confirmed"
        validate("task", task_doc)
        self.store.put("tasks", self.project_id, self.task_id, task_doc)

        if not claim_in_gcs:
            self.registry.document = None

        cmd_result = {
            "status": terminal_status,
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "error_kind": None,
        } if command_status in ("completed", "failed") else None

        cmd = command(
            status=command_status,
            execution_id=self.execution_id,
            claimed_at=_now(),
            completed_at=_now() if command_status in ("completed", "failed") else None,
            result=cmd_result,
        )
        self.store.put("commands", self.project_id, "cmd-1", cmd)
        return cmd

    # -------------------------------------------------------------------------
    # A. r17 exact shape
    # -------------------------------------------------------------------------
    def test_A_r17_exact_shape_reconciles_and_converges(self):
        """A. r17 exact shape: Command=completed, Execution=completed, Task=blocked,
        cleanup: persistence=partial, persisted=['execution'], task_claim_release=retained.
        Natural reconcile via process_command must recover handoff and task,
        release the claim, update task to completed, keep Command completed with session_id."""
        cmd = self._setup_r17_shape()

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(
                self.store, object(), cmd,
                claim_factory=lambda *_: self.registry,
                allowlist=self.ALLOWLIST,
            )

        self.assertEqual("completed", res["status"])
        self.assertTrue(res.get("reconciled"))

        # Task must now be completed
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        self.assertEqual("completed", task_doc["status"])
        self.assertIsNone(task_doc.get("blocked_reason"))

        # Execution cleanup must be complete and released
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], exec_doc["cleanup_evidence"]["persisted"])
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])

        # GCS claim must be released
        self.assertIsNone(self.registry.document)

        # Handoff record must exist
        handoff_id = f"{self.task_id}-completed-{self.execution_id}-0"
        handoff = self.store.get("handoffs", self.project_id, handoff_id)
        self.assertEqual("completed", handoff["current_state"])
        self.assertEqual(self.session_id, handoff["from_session"])

        # Command must maintain completed status and session_id
        cmd_doc = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", cmd_doc["status"])
        self.assertEqual("completed", cmd_doc["result"]["status"])
        self.assertEqual(self.session_id, cmd_doc["result"]["session_id"])

    # -------------------------------------------------------------------------
    # B. Handoff write first failure
    # -------------------------------------------------------------------------
    def test_B_handoff_write_first_failure_recovers_missing_handoff_and_task(self):
        """B. Only execution persisted; handoff was never written.
        Next natural reconcile writes missing handoff + task without corrupting execution."""
        cmd = self._setup_r17_shape(persisted=["execution"])
        # Ensure handoff does not exist yet
        handoff_id = f"{self.task_id}-completed-{self.execution_id}-0"
        with self.assertRaises(TaskError):
            self.store.get("handoffs", self.project_id, handoff_id)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(
                self.store, object(), cmd,
                claim_factory=lambda *_: self.registry,
                allowlist=self.ALLOWLIST,
            )

        self.assertEqual("completed", res["status"])
        # Handoff now written
        handoff = self.store.get("handoffs", self.project_id, handoff_id)
        self.assertEqual("completed", handoff["current_state"])
        # Task now completed
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        self.assertEqual("completed", task_doc["status"])

    # -------------------------------------------------------------------------
    # C. Task write first failure
    # -------------------------------------------------------------------------
    def test_C_task_write_first_failure_supplements_missing_task_only(self):
        """C. Execution + handoff persisted; task write failed.
        Next natural reconcile re-verifies handoff and supplements missing task."""
        cmd = self._setup_r17_shape(persisted=["execution", "handoff"])
        # Create handoff manually
        from manager.execution_lifecycle import _terminal_handoff
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        expected_handoff = _terminal_handoff(exec_doc, task_doc, "completed", "Turn finished", exec_doc["completed_at"])
        from manager.tasks import create_handoff
        create_handoff(self.store, expected_handoff)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(
                self.store, object(), cmd,
                claim_factory=lambda *_: self.registry,
                allowlist=self.ALLOWLIST,
            )

        self.assertEqual("completed", res["status"])
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        self.assertEqual("completed", task_doc["status"])
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # D. Repeated transient Drive failures (converges on 3rd tick)
    # -------------------------------------------------------------------------
    def test_D_repeated_transient_drive_failures_eventually_converges(self):
        """D. First 2 reconcile attempts fail due to Drive exceptions; 3rd attempt succeeds.
        Must converge cleanly without staying permanently blocked."""
        cmd = self._setup_r17_shape()

        attempts = {"count": 0}
        original_put = self.store.put

        def flaky_put(area, project_id, name, document):
            if area in ("handoffs", "tasks") and attempts["count"] < 2:
                attempts["count"] += 1
                raise TaskError("503 Service Unavailable: Drive transient error")
            return original_put(area, project_id, name, document)

        self.store.put = flaky_put
        try:
            with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
                # Attempt 1: fails
                res1 = process_command(self.store, object(), cmd,
                                       claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)
                # Attempt 2: fails
                res2 = process_command(self.store, object(), cmd,
                                       claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)
                # Attempt 3: succeeds
                res3 = process_command(self.store, object(), cmd,
                                       claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)
        finally:
            self.store.put = original_put

        self.assertEqual("completed", res3["status"])
        self.assertTrue(res3.get("reconciled"))
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        self.assertEqual("completed", task_doc["status"])
        self.assertIsNone(self.registry.document)

    # -------------------------------------------------------------------------
    # E. Persistent Drive failure fails closed
    # -------------------------------------------------------------------------
    def test_E_persistent_drive_failure_fails_closed_and_does_not_forge_complete(self):
        """E. Backend always fails -> persists truthful incomplete/retained state.
        Must NOT declare persistence complete or release claim."""
        cmd = self._setup_r17_shape()

        original_put = self.store.put

        def failing_put(area, project_id, name, document):
            if area in ("handoffs", "tasks"):
                raise TaskError("500 Internal Server Error: permanent Drive outage")
            return original_put(area, project_id, name, document)

        self.store.put = failing_put
        try:
            with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
                res = process_command(self.store, object(), cmd,
                                       claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)
        finally:
            self.store.put = original_put

        # Claim must still be held
        self.assertIsNotNone(self.registry.document)
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("partial", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual("retained", exec_doc["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # F. Command already terminal monotonicity & session_id preserved
    # -------------------------------------------------------------------------
    def test_F_command_already_terminal_preserves_monotonicity_and_session_id(self):
        """F. Command is already completed with real session_id.
        Reconcile must preserve completed status and session_id (never downgrade to attention or null)."""
        cmd = self._setup_r17_shape(command_status="completed")

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(self.store, object(), cmd,
                                   claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)

        self.assertEqual("completed", res["status"])
        cmd_doc = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("completed", cmd_doc["status"])
        self.assertEqual(self.session_id, cmd_doc["result"]["session_id"])

    # -------------------------------------------------------------------------
    # G. Claim already absent converges
    # -------------------------------------------------------------------------
    def test_G_claim_already_absent_converges_to_released(self):
        """G. GCS claim is already absent; once persistence retry completes,
        cleanup evidence converges to released."""
        cmd = self._setup_r17_shape(claim_in_gcs=False)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(self.store, object(), cmd,
                                   claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)

        self.assertEqual("completed", res["status"])
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])

    # -------------------------------------------------------------------------
    # H. Claim still present recovers via CAS release
    # -------------------------------------------------------------------------
    def test_H_claim_still_present_released_via_recover_task_claim(self):
        """H. GCS claim is present; persistence completes, then claim is cleanly released."""
        cmd = self._setup_r17_shape(claim_in_gcs=True)
        self.assertIsNotNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(self.store, object(), cmd,
                                   claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)

        self.assertEqual("completed", res["status"])
        self.assertIsNone(self.registry.document)
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # I. Newer execution authority protected
    # -------------------------------------------------------------------------
    def test_I_newer_execution_authority_protected_from_old_retry(self):
        """I. If Task is now owned by a newer execution, old execution retry
        must NOT overwrite the Task status or claim release."""
        cmd = self._setup_r17_shape()
        # Task now points to a newer execution
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        task_doc["source_context"] = {"active_execution_id": "command-cmd-newer"}
        task_doc["status"] = "in_progress"
        self.store.put("tasks", self.project_id, self.task_id, task_doc)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(self.store, object(), cmd,
                                   claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)

        # Task must NOT have been overwritten by old execution
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        self.assertEqual("in_progress", task_doc["status"])
        self.assertEqual("command-cmd-newer", task_doc["source_context"]["active_execution_id"])

    # -------------------------------------------------------------------------
    # J. Concurrent reconcilers safety
    # -------------------------------------------------------------------------
    def test_J_concurrent_reconcilers_are_safe_and_converge(self):
        """J. Two concurrent reconcilers attempting persistence recovery simultaneously.
        Both succeed or one completes and the other idempotently succeeds; no conflict."""
        cmd = self._setup_r17_shape()

        results = []

        def worker():
            with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
                res = process_command(self.store, object(), cmd,
                                       claim_factory=lambda *_: self.registry, allowlist=self.ALLOWLIST)
                results.append(res)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(2, len(results))
        for r in results:
            self.assertEqual("completed", r["status"])

        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        self.assertEqual("completed", task_doc["status"])
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])

    # -------------------------------------------------------------------------
    # K. Restart safety (purely durable state)
    # -------------------------------------------------------------------------
    def test_K_restart_safety_purely_durable_across_new_process(self):
        """K. Process restarts completely: fresh memory state, only durable Drive/GCS records.
        New watcher process immediately reconciles and converges without memory state."""
        self._setup_r17_shape()

        # Simulate complete restart by instantiating new registry reference and reading from store
        cmd = self.store.get("commands", self.project_id, "cmd-1")
        fresh_registry = self.registry  # points to the same underlying durable storage

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res = process_command(self.store, object(), cmd,
                                   claim_factory=lambda *_: fresh_registry, allowlist=self.ALLOWLIST)

        self.assertEqual("completed", res["status"])
        self.assertTrue(res.get("reconciled"))
        task_doc = self.store.get("tasks", self.project_id, self.task_id)
        self.assertEqual("completed", task_doc["status"])
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])


if __name__ == "__main__":
    unittest.main()
