import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.execution_recovery import main, recover_task_claim
from manager.task_claims import claim_task_execution
from manager.tasks import TaskError
from manager.test_execution_lifecycle import build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry


class RecoveryTests(unittest.TestCase):
    def terminal_claim(self, read_only=True):
        store, claim = build_store(read_only=read_only), MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(store, object(), None, "p1", "t1", "exec-a", "codex", "read_only", task_claim_registry=claim)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(store, object(), None, claim, "p1", "t1", "exec-a", "codex", "completed", gate["task_claim"]["generation"], True)
        return store, claim_task_execution(claim, "p1", "t1", "exec-a", "codex", "2026-08-13T01:00:00Z"), claim

    def test_terminal_execution_stale_claim_releases_and_repeats_safely(self):
        store, stale, claim = self.terminal_claim()
        result = recover_task_claim(store, claim, "p1", "t1")
        self.assertEqual("released", result["status"]); self.assertEqual(stale["generation"], result["generation"])
        self.assertEqual({"status": "clean", "released": False, "reason": "no_active_claim"}, recover_task_claim(store, claim, "p1", "t1"))

    def test_generation_change_refuses_without_deleting_new_claim(self):
        class ChangesGeneration(MemoryClaimRegistry):
            def delete_if_generation_matches(self, generation):
                self.generation += 1
                return super().delete_if_generation_matches(generation)
        store, _, original = self.terminal_claim(); claim = ChangesGeneration()
        claim.document, claim.generation = original.document, original.generation
        with self.assertRaises(TaskError): recover_task_claim(store, claim, "p1", "t1")
        self.assertEqual("exec-a", claim.document["execution_id"])

    def test_running_execution_refuses(self):
        store, claim = build_store(read_only=True), MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(store, object(), None, "p1", "t1", "exec-a", "codex", "read_only", task_claim_registry=claim)
        self.assertEqual("running_execution_requires_provider_stop_and_terminal_recovery", recover_task_claim(store, claim, "p1", "t1")["reason"])
        self.assertIsNotNone(claim.document)

    def test_missing_or_mismatched_drive_execution_fails_closed(self):
        store, _, claim = self.terminal_claim(); store.records.pop(("executions", "p1", "exec-a"))
        with self.assertRaisesRegex(TaskError, "cannot confirm"): recover_task_claim(store, claim, "p1", "t1")
        self.assertIsNotNone(claim.document)
        store, _, claim = self.terminal_claim(); item = store.get("executions", "p1", "exec-a"); item["provider"] = "claude"; store.put("executions", "p1", "exec-a", item)
        self.assertEqual("drive_gcs_identity_mismatch", recover_task_claim(store, claim, "p1", "t1")["reason"])

    def test_incomplete_terminal_or_writer_authority_refuses(self):
        store, _, claim = self.terminal_claim(); task = store.get("tasks", "p1", "t1"); task["status"] = "in_progress"; store.put("tasks", "p1", "t1", task)
        self.assertEqual("terminal_drive_state_is_incomplete", recover_task_claim(store, claim, "p1", "t1")["reason"])
        store, _, claim = self.terminal_claim(); item = store.get("executions", "p1", "exec-a"); item["access"] = "production_write"; item["lease_evidence"] = {"authority":"acquired","lock_id":"repo-" + "0" * 64,"generation":1,"repository":"github:ne9221/ai-development-manager","branch":"refs/heads/main","scope":["manager/x.py"],"baseline_head":"0" * 40}; item["cleanup_evidence"] = {"writer_release":"retained"}; store.put("executions", "p1", "exec-a", item)
        self.assertEqual("writer_authority_not_confirmed_released", recover_task_claim(store, claim, "p1", "t1")["reason"])

    def test_ambiguous_delete_is_self_confirmed(self):
        class AmbiguousDelete(MemoryClaimRegistry):
            def delete_if_generation_matches(self, generation):
                super().delete_if_generation_matches(generation); raise TaskError("timeout")
        store, _, original = self.terminal_claim(); claim = AmbiguousDelete(); claim.document, claim.generation = original.document, original.generation
        result = recover_task_claim(store, claim, "p1", "t1")
        self.assertTrue(result["confirmed_after_ambiguous_delete"]); self.assertIsNone(claim.document)

    def test_cli_is_machine_readable_and_redacted(self):
        store, _, claim = self.terminal_claim(); output = io.StringIO()
        with patch("manager.execution_recovery.build_service", return_value=object()), patch("manager.execution_recovery.DriveRecords", return_value=store), patch("manager.execution_recovery.task_claim_registry", return_value=claim), redirect_stdout(output):
            self.assertEqual(0, main(["p1", "t1"]))
        result = json.loads(output.getvalue()); self.assertEqual("released", result["status"]); self.assertNotIn("secret", output.getvalue())


if __name__ == "__main__": unittest.main()
