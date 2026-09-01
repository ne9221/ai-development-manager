import unittest
from unittest.mock import Mock, patch

from manager import task_root
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import reserve_execution
from manager.task_claims import TaskClaimConflict, claim_task_execution
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
                self.assertIsNotNone(claim.document)
                self.assertFalse(claim.document["authority_active"])
                self.assertEqual("released", claim.document["cleanup"]["status"])
                task = store.get("tasks", "p1", "t1")
                self.assertEqual("completed" if status == "completed" else "blocked", task["status"])
                handoff = store.get("handoffs", "p1", f"t1-{status}-exec-a-0")
                self.assertIsNone(handoff["from_session"])

    def test_writer_release_precedes_claim_release(self):
        store, writer, claim, gate = self.running(); events = []
        real_writer, real_claim = release, __import__("manager.task_root", fromlist=["release_runtime_claim"]).release_runtime_claim
        with patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_lifecycle.release", side_effect=lambda *a, **k: (events.append("writer"), real_writer(*a, **k))[1]), \
             patch("manager.task_root.release_runtime_claim", side_effect=lambda *a, **k: (events.append("claim"), real_claim(*a, **k))[1]):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual(["writer", "claim"], events)

    def test_writer_release_failure_fences_claim_and_retry_finishes_cleanup(self):
        store, writer, claim, gate = self.running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()), patch("manager.execution_lifecycle.release", side_effect=TaskError("writer down")):
            result = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual("failed", result["cleanup"]["writer_release"])
        self.assertEqual("not_attempted", result["cleanup"]["task_claim_release"])
        self.assertEqual("exec-a", claim.document["execution_id"])
        self.assertEqual("completed", result["execution"]["status"])
        completed_at = result["execution"]["completed_at"]
        retry = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertFalse(retry["idempotent"])
        self.assertEqual("released", retry["cleanup"]["writer_release"])
        self.assertEqual("released", retry["cleanup"]["task_claim_release"])
        self.assertEqual(completed_at, retry["execution"]["completed_at"])

    def test_claim_release_failure_is_retried_before_idempotent_return(self):
        store, writer, claim, gate = self.running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()), patch("manager.task_root.release_runtime_claim", side_effect=TaskError("claim down")):
            first = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual("completed", first["execution"]["status"])
        self.assertEqual("failed", first["cleanup"]["task_claim_release"])
        recovered = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertFalse(recovered["idempotent"])
        self.assertEqual("released", recovered["cleanup"]["task_claim_release"])
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
        with self.assertRaisesRegex(TaskError, "does not hold the task claim"):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "failed", old_generation, True, gate["lease"]["lease_token"])
        self.assertEqual(before, store.records)
        self.assertEqual(fresh["generation"], claim.generation)

    def test_read_only_skips_writer_release(self):
        with patch("manager.execution_lifecycle.release") as writer_release:
            _, _, claim, _, result = self.terminal("interrupted", read_only=True)
        writer_release.assert_not_called()
        self.assertEqual("not_required", result["cleanup"]["writer_release"])
        self.assertIsNotNone(claim.document)
        self.assertFalse(claim.document["authority_active"])
        self.assertEqual("released", claim.document["cleanup"]["status"])

    def test_terminal_persistence_failure_retains_authority_and_retry_succeeds(self):
        store, writer, claim, gate = self.running(); store.fail_running = False
        real_put = store.put
        def fail_terminal(area, project, name, document):
            if area == "executions" and document.get("status") == "completed":
                raise TaskError("terminal persistence failed")
            return real_put(area, project, name, document)
        store.put = fail_terminal
        with patch("manager.executions.read_drive_status", return_value=quota_document()), self.assertRaisesRegex(TaskError, "terminal persistence failed"):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        running = store.get("executions", "p1", "exec-a")
        self.assertEqual("running", running["status"])
        self.assertEqual("incomplete", running["cleanup_evidence"]["persistence"])
        self.assertEqual("retained", running["cleanup_evidence"]["writer_release"])
        self.assertEqual("exec-a", claim.document["execution_id"])
        self.assertEqual("active", next(iter(writer.document["locks"].values()))["status"])
        store.put = real_put
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            retry = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"], completed_at="2026-08-13T00:02:00Z", summary="completed")
        self.assertEqual("completed", retry["execution"]["status"])
        self.assertIsNotNone(claim.document)
        self.assertFalse(claim.document["authority_active"])
        self.assertEqual("released", claim.document["cleanup"]["status"])
        self.assertEqual("complete", retry["execution"]["cleanup_evidence"]["persistence"])

    def test_handoff_failure_preserves_outcome_and_retry_completes(self):
        store, writer, claim, gate = self.running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()), patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("handoff failed")):
            with self.assertRaisesRegex(TaskError, "handoff failed"):
                terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual("completed", store.get("executions", "p1", "exec-a")["status"])
        original_timestamp = store.get("executions", "p1", "exec-a")["completed_at"]
        self.assertIsNone(store.records.get(("handoffs", "p1", "t1-completed-exec-a-0")))
        self.assertEqual("in_progress", store.get("tasks", "p1", "t1")["status"])
        self.assertEqual("exec-a", claim.document["execution_id"])
        retry = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"], completed_at="2026-08-13T00:09:00Z", summary="changed")
        self.assertFalse(retry["idempotent"])
        self.assertEqual(original_timestamp, retry["execution"]["completed_at"])
        self.assertEqual("completed", store.get("tasks", "p1", "t1")["status"])
        self.assertIsNotNone(claim.document)
        self.assertFalse(claim.document["authority_active"])
        self.assertEqual("released", claim.document["cleanup"]["status"])

    def test_task_failure_retry_does_not_duplicate_handoff(self):
        store, writer, claim, gate = self.running(); real_put = store.put
        def fail_task(area, project, name, document):
            if area == "tasks" and document.get("status") == "completed":
                raise TaskError("task failed")
            return real_put(area, project, name, document)
        store.put = fail_task
        with patch("manager.executions.read_drive_status", return_value=quota_document()), self.assertRaisesRegex(TaskError, "task failed"):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"], completed_at="2026-08-13T00:02:00Z", summary="done")
        handoff_key = ("handoffs", "p1", "t1-completed-exec-a-0")
        handoff_version = store.versions[handoff_key]
        store.put = real_put
        retry = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertEqual(handoff_version, store.versions[handoff_key])
        self.assertEqual("completed", store.get("tasks", "p1", "t1")["status"])
        self.assertFalse(retry["idempotent"])

    def test_incomplete_terminalization_retains_claim_against_new_execution(self):
        store, writer, claim, gate = self.running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()), patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("handoff failed")), self.assertRaises(TaskError):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        with self.assertRaises(TaskClaimConflict):
            task_root.acquire_task_root(claim, "p1", "t1", "exec-b", "claude", "2026-08-13T00:03:00Z")

    def test_complete_state_same_outcome_is_idempotent_without_rewrites(self):
        store, _, claim, gate, result = self.terminal("completed")
        execution_key = ("executions", "p1", "exec-a")
        handoff_key = ("handoffs", "p1", "t1-completed-exec-a-0")
        versions = (store.versions[execution_key], store.versions[handoff_key])
        duplicate = terminalize_execution(store, object(), None, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True, gate["lease"]["lease_token"])
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(result["execution"]["completed_at"], duplicate["execution"]["completed_at"])
        self.assertEqual(versions, (store.versions[execution_key], store.versions[handoff_key]))

    def test_retry_reusing_same_execution_id_does_not_collide_on_prior_handoff(self):
        """Regression: command_watcher retries reuse the exact same execution_id
        as the attempt they retry (see reserve_execution's own retry contract).
        A second attempt that terminalizes with the *same* status as the first
        must not collide with the first attempt's already-persisted handoff --
        confirmed live: it did, leaving cleanup partial and the task claim
        retained, which then made the next retry attempt fail closed too."""
        store, writer, claim, gate, first = self.terminal("interrupted", read_only=True, summary="first attempt")
        self.assertEqual("released", first["cleanup"]["task_claim_release"])
        first_handoff = store.get("handoffs", "p1", "t1-interrupted-exec-a-0")
        self.assertEqual("first attempt", first_handoff["minimal_context"])

        task = store.get("tasks", "p1", "t1")
        task.update(status="ready", blocked_reason=None,
                   source_context={"retry_count": 1, "retry_of_execution_id": "exec-a"})
        store.put("tasks", "p1", "t1", task)
        reserve_execution(store, "p1", "t1", "exec-a", "codex", {"decision": "retry"}, "code", "high",
                          retry_count=1, retry_of_execution_id="exec-a")
        with patch("manager.execution_lifecycle.validate_local_preflight"), patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate2 = enter_running_gate(store, object(), None, "p1", "t1", "exec-a", "codex", "read_only",
                                       started_at="2026-08-13T00:05:00Z", task_claim_registry=claim)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            second = terminalize_execution(store, object(), None, claim, "p1", "t1", "exec-a", "codex", "interrupted",
                                           gate2["task_claim"]["generation"], True, None,
                                           completed_at="2026-08-13T00:06:00Z", summary="second attempt")

        self.assertFalse(second.get("idempotent"))
        self.assertEqual("released", second["cleanup"]["task_claim_release"])
        self.assertEqual("complete", second["execution"]["cleanup_evidence"]["persistence"])
        second_handoff = store.get("handoffs", "p1", "t1-interrupted-exec-a-1")
        self.assertEqual("second attempt", second_handoff["minimal_context"])
        # The first attempt's handoff is untouched, not overwritten.
        self.assertEqual(first_handoff, store.get("handoffs", "p1", "t1-interrupted-exec-a-0"))

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
