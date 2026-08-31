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


class TerminalCleanupClaimAbsentConvergenceTests(unittest.TestCase):
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
        self.execution_id = "command-cmd-round46"

    def _setup_round46_state(self, task_claim_release="retained", persistence="complete",
                             active_execution_id="command-cmd-round46", claim_in_gcs=False):
        reserve_execution(self.store, self.project_id, self.task_id, self.execution_id, "codex", {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(
                self.store, object(), None, self.project_id, self.task_id, self.execution_id, "codex",
                "read_only", task_claim_registry=self.registry,
            )
        exec_doc = self.store.get("executions", self.project_id, self.execution_id)
        exec_doc["session_id"] = "codex:01a05537-round46-real-session"
        exec_doc["provider_evidence"] = {
            "host": socket.gethostname()[:100],
            "pid": 999999,
            "creation_identity": "proc-round46",
            "started_at": _now(),
        }
        self.store.put("executions", self.project_id, self.execution_id, exec_doc)

        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(
                self.store, object(), None, self.registry, self.project_id, self.task_id,
                self.execution_id, "codex", "interrupted", 1, True,
                summary="Recovery: provider_process_stopped",
            )

        # Set specific test condition
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
        validate("task", task_doc)
        self.store.put("tasks", self.project_id, self.task_id, task_doc)

        cmd = command(status="running", execution_id=self.execution_id, claimed_at=_now())
        self.store.put("commands", self.project_id, "cmd-1", cmd)
        return cmd

    def test_scenario_1_claim_already_absent_reconciles_to_terminal(self):
        """Scenario 1: When real GCS claim is already absent and cleanup persistence is complete,
        reconcile must converge to terminal failed/interrupted outcome."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete")
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("failed", outcome.get("status"))
        self.assertTrue(outcome.get("reconciled"))

    def test_scenario_2_pass_after_claim_already_absent_updates_cleanup_evidence_and_command(self):
        """Scenario 2: Verify cleanup_evidence.task_claim_release becomes released and Command result preserves session_id."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete")
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("released", refreshed_exec["cleanup_evidence"]["task_claim_release"])

        stored_cmd = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("failed", stored_cmd["status"])
        self.assertEqual("interrupted", stored_cmd["result"]["status"])
        self.assertEqual("codex:01a05537-round46-real-session", stored_cmd["result"]["session_id"])

    def test_scenario_3_claim_read_unknown_fails_closed(self):
        """Scenario 3: If GCS backend raises TaskError / timeout, reconcile must fail closed
        and NOT falsely sync released."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete")

        with patch("manager.command_watcher.check_task_execution_claim", side_effect=TaskError("backend timeout")):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        self.assertEqual("terminal_cleanup_reconciliation_unknown", outcome.get("recovery_reason"))

        # Stored evidence must NOT be modified
        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    def test_scenario_4_newer_claim_owned_by_different_execution_refuses(self):
        """Scenario 4: If GCS claim belongs to a newer execution, old execution cannot sync released."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete")
        # Put a claim belonging to exec-newer in registry
        claim_task_execution(self.registry, self.project_id, self.task_id, "command-cmd-newer", "codex", _now())

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    def test_scenario_5_newer_execution_on_task_refuses_claim_absent_sync(self):
        """Scenario 5: If claim is absent but Task has already moved to a newer execution,
        old execution must not claim authority."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete",
                                        active_execution_id="command-cmd-newer")
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        self.assertEqual("terminal_cleanup_task_reclaimed_by_newer_execution", outcome.get("recovery_reason"))

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    def test_scenario_6_claim_absent_but_provider_live_fails_closed(self):
        """Scenario 6: If claim is absent but provider process is still live on host, fail closed."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete")
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="live"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        self.assertEqual("terminal_cleanup_provider_still_live", outcome.get("recovery_reason"))

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    def test_scenario_7_cleanup_persistence_incomplete_retries_first(self):
        """Scenario 7: If persistence is incomplete, cannot skip persistence contract."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="incomplete")
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.retry_incomplete_terminal_persistence", return_value=False), \
             patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("attention", outcome.get("status"))
        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("retained", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    def test_scenario_8_concurrent_reconcilers_monotonic_convergence(self):
        """Scenario 8: Concurrent reconcilers converge monotonically without flipping."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete")
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            res1 = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)
            res2 = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("failed", res1.get("status"))
        self.assertEqual("failed", res2.get("status"))

        refreshed_exec = self.store.get("executions", self.project_id, self.execution_id)
        self.assertEqual("released", refreshed_exec["cleanup_evidence"]["task_claim_release"])

    def test_scenario_9_stored_released_is_idempotent(self):
        """Scenario 9: If stored released already exists, reconciliation is an idempotent clean terminal."""
        cmd = self._setup_round46_state(task_claim_release="released", persistence="complete")
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, cmd, lambda *_: self.registry)

        self.assertEqual("failed", outcome.get("status"))
        self.assertTrue(outcome.get("reconciled"))

    def test_scenario_10_round46_exact_shape_regression(self):
        """Scenario 10: Exact Round 46 stuck in attention naturally reconciles to terminal Command."""
        cmd = self._setup_round46_state(task_claim_release="retained", persistence="complete")
        _attention(self.store, cmd, self.store.get("executions", self.project_id, self.execution_id),
                   "terminal_cleanup_not_confirmed")
        attention_cmd = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("attention", attention_cmd["status"])
        self.assertEqual("terminal_cleanup_not_confirmed", attention_cmd["recovery_reason"])

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = _reconcile_active(self.store, None, attention_cmd, lambda *_: self.registry)

        self.assertEqual("failed", outcome.get("status"))
        self.assertTrue(outcome.get("reconciled"))

        terminal_cmd = self.store.get("commands", self.project_id, "cmd-1")
        self.assertEqual("failed", terminal_cmd["status"])
        self.assertEqual("interrupted", terminal_cmd["result"]["status"])
        self.assertEqual("codex:01a05537-round46-real-session", terminal_cmd["result"]["session_id"])


if __name__ == "__main__":
    unittest.main()
