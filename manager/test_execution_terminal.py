import unittest
from unittest.mock import Mock, patch

from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.task_claims import claim_task_execution
from manager.tasks import TaskError
from manager.test_execution_lifecycle import MemoryStore, build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import MemoryRegistry
from manager.worktree_locks import release


class TerminalLifecycleTests(unittest.TestCase):
    def running(self, read_only=False):
        store = build_store(read_only=read_only)
        writer = None if read_only else MemoryRegistry()
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(store, object(), writer, "p1", "t1", "exec-a", "codex", "read_only" if read_only else "production_write", baseline_head=None if read_only else "a" * 40, started_at="2026-08-13T00:01:00Z", task_claim_registry=claim)
        return store, writer, claim, gate

    def terminal(self, status="completed", read_only=False, **kwargs):
        store, writer, claim, gate = self.running(read_only)
        options = dict(project_id="p1", task_id="t1", execution_id="exec-a", provider="codex", status=status,
                       claim_generation=gate["task_claim"]["generation"], provider_stopped=True,
                       lease_token=None if read_only else gate["lease"]["lease_token"],
                       completed_at="2026-08-13T00:02:00Z", summary=status)
        options.update(kwargs)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = terminalize_execution(store, object(), writer, claim, **options)
        return store, writer, claim, gate, result

    def test_completed_failed_and_interrupted_happy_paths(self):
        for status in ("completed", "failed", "interrupted"):
            with self.subTest(status=status):
                store, writer, claim, _, result = self.terminal(status)
                self.assertEqual(status, result["execution"]["status"])
                self.assertEqual("released", result["cleanup"]["writer_release"])
                self.assertEqual("released", result["cleanup"]["task_claim_release"])
                self.assertIsNone(claim.document)
                task = store.get("tasks", "p1", "t1")
                self.assertEqual("completed" if status == "completed" else "blocked", task["status"])
                handoff = store.get("handoffs", "p1", f"t1-{status}-exec-a")
                self.assertIsNone(handoff["from_session"])

    def test_writer_release_precedes_claim_release(self):
        store, writer, claim, gate = self.running(); events = []
        real_writer, real_claim = release, __import__("manager.task_claims", fromlist=["release_task_execution_claim"]).release_task_execution_claim
        with patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_lifecycle.release", side_effect=lambda *a, **k: (events.append("writer"), real_writer(*a, **k))[1]), \
             patch("manager.execution_lifecycle.release_task_execution_claim", side_effect=lambda *a, **k: (events.append("claim"), real_claim(*a, **k))[1]):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual(["writer", "claim"], events)

    def test_writer_release_failure_fences_claim(self):
        store, writer, claim, gate = self.running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()), patch("manager.execution_lifecycle.release", side_effect=TaskError("writer down")):
            result = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual("failed", result["cleanup"]["writer_release"])
        self.assertEqual("not_attempted", result["cleanup"]["task_claim_release"])
        self.assertEqual("exec-a", claim.document["execution_id"])
        self.assertEqual("completed", result["execution"]["status"])

    def test_claim_release_failure_preserves_outcome_and_duplicate_is_idempotent(self):
        store, writer, claim, gate = self.running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()), patch("manager.execution_lifecycle.release_task_execution_claim", side_effect=TaskError("claim down")):
            first = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual("completed", first["execution"]["status"])
        self.assertEqual("failed", first["cleanup"]["task_claim_release"])
        duplicate = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertTrue(duplicate["idempotent"])
        with self.assertRaisesRegex(TaskError, "conflicting"):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "failed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])

    def test_wrong_generation_and_provider_running_fail_before_drive_mutation(self):
        store, writer, claim, gate = self.running(); before = dict(store.records)
        for changes in ({"claim_generation": gate["task_claim"]["generation"] + 1}, {"provider_stopped": False}):
            options = dict(project_id="p1", task_id="t1", execution_id="exec-a", provider="codex", status="completed", claim_generation=gate["task_claim"]["generation"], provider_stopped=True, lease_token=gate["lease"]["lease_token"])
            options.update(changes)
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                terminalize_execution(store, object(), writer, claim, **options)
            self.assertEqual(before, store.records)

    def test_late_callback_cannot_touch_new_claim(self):
        store, writer, claim, gate = self.running()
        old_generation = gate["task_claim"]["generation"]
        claim.document = None
        fresh = claim_task_execution(claim, "p1", "t1", "exec-b", "claude", "2026-08-13T00:03:00Z")
        before = dict(store.records)
        with self.assertRaisesRegex(TaskError, "exact task claim"):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "failed", old_generation, True, gate["lease"]["lease_token"])
        self.assertEqual(before, store.records)
        self.assertEqual(fresh["generation"], claim.generation)

    def test_read_only_skips_writer_release(self):
        with patch("manager.execution_lifecycle.release") as writer_release:
            _, _, claim, _, result = self.terminal("interrupted", read_only=True)
        writer_release.assert_not_called()
        self.assertEqual("not_required", result["cleanup"]["writer_release"])
        self.assertIsNone(claim.document)

    def test_terminal_persistence_failure_still_cleans_up(self):
        store, writer, claim, gate = self.running(); store.fail_running = False
        real_put = store.put
        def fail_terminal(area, project, name, document):
            if area == "executions" and document.get("status") == "completed":
                raise TaskError("terminal persistence failed")
            return real_put(area, project, name, document)
        store.put = fail_terminal
        with patch("manager.executions.read_drive_status", return_value=quota_document()), self.assertRaisesRegex(TaskError, "terminal persistence failed"):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertIsNone(claim.document)

    def test_handoff_failure_preserves_terminal_outcome_and_cleans_up(self):
        store, writer, claim, gate = self.running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()), patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("handoff failed")):
            with self.assertRaisesRegex(TaskError, "handoff failed"):
                terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual("completed", store.get("executions", "p1", "exec-a")["status"])
        self.assertIsNone(claim.document)

    def test_cleanup_audit_failure_does_not_change_provider_outcome(self):
        store, writer, claim, gate = self.running(); real_put = store.put
        def fail_audit(area, project, name, document):
            if area == "executions" and document.get("cleanup_evidence"):
                raise TaskError("audit failed")
            return real_put(area, project, name, document)
        store.put = fail_audit
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "failed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual("failed", result["execution"]["status"])
        self.assertTrue(any("audit" in item for item in result["cleanup"]["errors"]))

    def test_plaintext_lease_token_is_never_persisted(self):
        store, _, _, gate, result = self.terminal("completed")
        self.assertNotIn(gate["lease"]["lease_token"], repr(store.records))
        self.assertNotIn(gate["lease"]["lease_token"], repr(result))


if __name__ == "__main__":
    unittest.main()
