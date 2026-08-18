import io
import json
import socket
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.execution_recovery import main, main_break_glass, recover_stale_running_execution, recover_task_claim
from manager.executions import finish_execution
from manager.task_claims import claim_task_execution
from manager.tasks import TaskError
from manager.test_execution_lifecycle import build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import HEAD, MemoryRegistry


class RecoveryTests(unittest.TestCase):
    def terminal_claim(self, read_only=True):
        store, claim, writer = build_store(read_only=read_only), MemoryClaimRegistry(), None if read_only else MemoryRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(store, object(), writer, "p1", "t1", "exec-a", "codex",
                                      "read_only" if read_only else "production_write",
                                      baseline_head=None if read_only else HEAD, task_claim_registry=claim)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", "codex", "completed",
                                  gate["task_claim"]["generation"], True,
                                  lease_token=gate["lease"]["lease_token"] if gate["lease"] else None)
        return store, claim_task_execution(claim, "p1", "t1", "exec-a", "codex", "2026-08-13T01:00:00Z"), claim

    def test_terminal_execution_stale_claim_releases_and_repeats_safely(self):
        store, stale, claim = self.terminal_claim()
        result = recover_task_claim(store, claim, "p1", "t1")
        self.assertEqual("released", result["status"]); self.assertEqual(stale["generation"], result["generation"])
        self.assertEqual({"status": "clean", "released": False, "reason": "no_active_claim"}, recover_task_claim(store, claim, "p1", "t1"))

    def test_legacy_read_only_terminal_without_cleanup_evidence_refuses(self):
        for status in ("completed", "failed", "interrupted"):
            with self.subTest(status=status):
                store, claim = build_store(read_only=True), MemoryClaimRegistry()
                with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
                    enter_running_gate(store, object(), None, "p1", "t1", "exec-a", "codex", "read_only", task_claim_registry=claim)
                with patch("manager.executions.read_drive_status", return_value=quota_document()):
                    finish_execution(store, object(), "p1", "exec-a", status)
                result = recover_task_claim(store, claim, "p1", "t1")
                self.assertEqual("authoritative_terminal_cleanup_not_confirmed", result["reason"])
                self.assertIsNotNone(claim.document)

    def test_production_terminal_requires_released_writer(self):
        store, _, claim = self.terminal_claim(read_only=False)
        self.assertEqual("released", recover_task_claim(store, claim, "p1", "t1")["status"])
        store, _, claim = self.terminal_claim(read_only=False)
        execution = store.get("executions", "p1", "exec-a")
        execution["cleanup_evidence"]["writer_release"] = "retained"
        store.put("executions", "p1", "exec-a", execution)
        self.assertEqual("writer_authority_not_confirmed_released", recover_task_claim(store, claim, "p1", "t1")["reason"])
        self.assertIsNotNone(claim.document)

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


class FailingPutStore:
    """Wraps a real store double and raises on the specific write this test wants
    to prove fails closed -- everything else passes straight through."""

    def __init__(self, inner, fail_when):
        self._inner = inner
        self._fail_when = fail_when

    def get(self, *args, **kwargs):
        return self._inner.get(*args, **kwargs)

    def put(self, area, project, name, document):
        if self._fail_when(area, document):
            raise TaskError("simulated Drive write failure")
        return self._inner.put(area, project, name, document)

    def list_records(self, *args, **kwargs):
        return self._inner.list_records(*args, **kwargs)


class BreakGlassRecoveryTests(unittest.TestCase):
    """Regression coverage for manager.execution_recovery.recover_stale_running_execution:
    the operator break-glass path for a legacy running Execution whose
    provider_evidence is missing/unverifiable and would otherwise stay stuck
    forever (Command permanently 'attention', GCS claim never released)."""

    HOST = socket.gethostname()[:100]

    def running_claim(self, provider_evidence="__unset__"):
        store, claim = build_store(read_only=True), MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(store, object(), None, "p1", "t1", "exec-a", "codex", "read_only",
                               task_claim_registry=claim)
        if provider_evidence != "__unset__":
            execution = store.get("executions", "p1", "exec-a")
            execution["provider_evidence"] = provider_evidence
            store.put("executions", "p1", "exec-a", execution)
        return store, claim

    def recover(self, store, claim, actor="on-call-operator", reason="confirmed dead via host inventory",
               break_glass=False, service=None):
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            return recover_stale_running_execution(
                store, service or object(), None, claim, "p1", "t1", "exec-a", "codex",
                actor=actor, reason=reason, break_glass=break_glass,
            )

    # 1. missing provider_evidence (the real legacy case: provider_evidence is
    # None because it was never recorded) must never be silently treated as
    # stopped -- it requires the explicit break_glass flag.
    def test_missing_provider_evidence_requires_break_glass(self):
        store, claim = self.running_claim(provider_evidence=None)
        result = self.recover(store, claim, break_glass=False)
        self.assertEqual("refused", result["status"])
        self.assertEqual("provider_liveness_unknown_break_glass_required", result["reason"])
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])
        self.assertIsNone(store.get("executions", "p1", "exec-a").get("recovery_attestation"))
        self.assertIsNotNone(claim.document)

        result = self.recover(store, claim, break_glass=True)
        self.assertEqual("recovered", result["status"])
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("interrupted", execution["status"])
        attestation = execution["recovery_attestation"]
        self.assertEqual("unknown", attestation["provider_liveness"])
        self.assertTrue(attestation["break_glass"])
        self.assertTrue(attestation["break_glass_recovery"])
        self.assertEqual("on-call-operator", attestation["actor"])
        self.assertIsNone(claim.document)

    # 2. A live PID with a matching creation identity must always refuse,
    # break_glass or not -- this is the one case that can never be overridden.
    def test_live_provider_refuses_even_with_break_glass(self):
        evidence = {"host": self.HOST, "pid": 4242, "creation_identity": "windows-filetime:1", "started_at": "2026-08-13T00:00:00Z"}
        store, claim = self.running_claim(provider_evidence=evidence)
        with patch("manager.execution_recovery.process_identity_state", return_value="live"):
            result = self.recover(store, claim, break_glass=True)
        self.assertEqual("refused", result["status"])
        self.assertEqual("provider_process_is_live", result["reason"])
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])
        self.assertIsNotNone(claim.document)

    # 3. PID reuse ("replaced") is distinguished from both "live" and
    # "unknown": it is strong OS-proven evidence the *original* process is
    # gone, so it does not require break_glass, but the attestation records
    # it as its own distinct liveness value (never conflated with "stopped").
    def test_pid_replaced_is_distinguished_and_does_not_require_break_glass(self):
        evidence = {"host": self.HOST, "pid": 4242, "creation_identity": "windows-filetime:1", "started_at": "2026-08-13T00:00:00Z"}
        store, claim = self.running_claim(provider_evidence=evidence)
        with patch("manager.execution_recovery.process_identity_state", return_value="replaced"):
            result = self.recover(store, claim, break_glass=False)
        self.assertEqual("recovered", result["status"])
        attestation = store.get("executions", "p1", "exec-a")["recovery_attestation"]
        self.assertEqual("replaced", attestation["provider_liveness"])
        self.assertFalse(attestation["break_glass"])

    # 4. A claim that exists but names a different execution (or none at all)
    # must refuse -- releasing/terminalizing under someone else's claim would
    # corrupt an unrelated, possibly-legitimate execution's authority.
    def test_mismatched_claim_refuses_and_leaves_foreign_claim_untouched(self):
        store, claim = self.running_claim(provider_evidence=None)
        claim.document = {**claim.document, "execution_id": "exec-other"}
        before = dict(claim.document)
        result = self.recover(store, claim, break_glass=True)
        self.assertEqual("refused", result["status"])
        self.assertEqual("task_claim_missing_or_mismatched", result["reason"])
        self.assertEqual(before, claim.document)
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])
        self.assertIsNone(store.get("executions", "p1", "exec-a").get("recovery_attestation"))

    def test_no_claim_at_all_refuses(self):
        store, claim = self.running_claim(provider_evidence=None)
        claim.document = None
        result = self.recover(store, claim, break_glass=True)
        self.assertEqual("refused", result["status"])
        self.assertEqual("task_claim_missing_or_mismatched", result["reason"])
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])

    # 5. The claim generation changes concurrently (a newer, legitimate claim
    # now exists) between recovery reading it and the CAS delete at
    # terminalization time: the execution still terminalizes (it genuinely was
    # stuck), but the newer claim is never deleted under a stale generation.
    def test_generation_change_terminalizes_but_retains_newer_claim(self):
        class ChangesGeneration(MemoryClaimRegistry):
            def delete_if_generation_matches(self, generation):
                self.generation += 1
                return super().delete_if_generation_matches(generation)

        store, original = self.running_claim(provider_evidence=None)
        claim = ChangesGeneration()
        claim.document, claim.generation = original.document, original.generation
        result = self.recover(store, claim, break_glass=True)
        self.assertEqual("recovered_claim_not_released", result["status"])
        self.assertEqual("interrupted", store.get("executions", "p1", "exec-a")["status"])
        self.assertEqual("failed", result["cleanup"]["task_claim_release"])
        self.assertIsNotNone(claim.document)
        self.assertEqual("exec-a", claim.document["execution_id"])

    # 6. A hard Drive persistence failure during terminalization must
    # propagate (never be swallowed into a false "recovered"), and the claim
    # must never be released when terminalization did not actually succeed.
    def test_drive_terminalization_failure_does_not_release_claim(self):
        store, claim = self.running_claim(provider_evidence=None)
        failing = FailingPutStore(store, lambda area, doc: area == "executions" and doc.get("status") == "interrupted")
        with self.assertRaises(TaskError):
            self.recover(failing, claim, break_glass=True)
        self.assertIsNotNone(claim.document)
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])

    # 7. The full happy path: explicit operator attestation persisted with
    # actor/timestamp/reason/prior-state evidence, execution terminalized,
    # claim released, task unblocked from "attention".
    def test_successful_operator_attestation_recovers_and_persists_evidence(self):
        store, claim = self.running_claim(provider_evidence=None)
        result = self.recover(store, claim, actor="ops-jane", reason="host inventory confirms box was reimaged",
                              break_glass=True)
        self.assertEqual("recovered", result["status"])
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("interrupted", execution["status"])
        attestation = execution["recovery_attestation"]
        self.assertEqual("ops-jane", attestation["actor"])
        self.assertEqual("host inventory confirms box was reimaged", attestation["reason"])
        self.assertIn("attested_at", attestation)
        self.assertEqual("running", attestation["prior_status"])
        self.assertEqual("released", execution["cleanup_evidence"]["task_claim_release"])
        self.assertIsNone(claim.document)
        task = store.get("tasks", "p1", "t1")
        self.assertEqual("blocked", task["status"])

    # 8. Repeated/idempotent recovery: calling it again after a successful
    # recovery must not error and must not attempt to re-release an
    # already-released claim.
    def test_repeated_recovery_is_idempotent(self):
        store, claim = self.running_claim(provider_evidence=None)
        first = self.recover(store, claim, break_glass=True)
        self.assertEqual("recovered", first["status"])
        second = self.recover(store, claim, break_glass=True)
        self.assertEqual("recovered", second["status"])
        self.assertTrue(second.get("idempotent"))
        self.assertEqual("interrupted", store.get("executions", "p1", "exec-a")["status"])

    # An execution that reached "interrupted" through some other, unrelated
    # path must never be silently reinterpreted/reprocessed by this tool.
    def test_execution_terminal_via_other_path_is_never_reinterpreted(self):
        store, claim = self.terminal_via_normal_path()
        result = self.recover(store, claim, break_glass=True)
        self.assertEqual("refused", result["status"])
        self.assertEqual("execution_already_terminal_not_via_break_glass_recovery", result["reason"])

    def terminal_via_normal_path(self):
        store, claim = self.running_claim(provider_evidence=None)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(store, object(), None, claim, "p1", "t1", "exec-a", "codex", "interrupted",
                                  claim.generation, True, summary="stopped normally")
        return store, claim

    # production_write executions are conservatively out of this tool's reach
    # without the original writer lease token (which break-glass recovery
    # never has by construction): both a missing registry and a real
    # registry-without-token must fail closed with a clean TaskError, never a
    # bare AttributeError/TypeError, and must never terminalize the execution
    # or touch the claim.
    def production_write_running_claim(self):
        store, claim, registry = build_store(read_only=False), MemoryClaimRegistry(), MemoryRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(store, object(), registry, "p1", "t1", "exec-a", "codex", "production_write",
                               baseline_head=HEAD, task_claim_registry=claim)
        return store, claim, registry

    def test_production_write_without_writer_registry_raises_clean_error(self):
        store, claim, _registry = self.production_write_running_claim()
        with self.assertRaises(TaskError):
            self.recover(store, claim, break_glass=True)
        self.assertIsNotNone(claim.document)
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])

    def test_production_write_without_lease_token_fails_closed(self):
        store, claim, registry = self.production_write_running_claim()
        with self.assertRaises(TaskError):
            with patch("manager.executions.read_drive_status", return_value=quota_document()):
                recover_stale_running_execution(store, object(), registry, claim, "p1", "t1", "exec-a", "codex",
                                                actor="ops", reason="no lease token available", break_glass=True)
        self.assertIsNotNone(claim.document)
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])

    def test_cli_break_glass_is_machine_readable(self):
        store, claim = self.running_claim(provider_evidence=None)
        output = io.StringIO()
        with patch("manager.execution_recovery.build_service", return_value=object()), \
             patch("manager.execution_recovery.DriveRecords", return_value=store), \
             patch("manager.execution_recovery.task_claim_registry", return_value=claim), \
             patch("manager.execution_recovery.GCSLockRegistry") as mock_writer_cls, \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             redirect_stdout(output):
            mock_writer_cls.from_environment.return_value = None
            code = main_break_glass(["p1", "t1", "exec-a", "codex", "--actor", "ops-jane",
                                     "--reason", "confirmed dead", "--break-glass"])
        self.assertEqual(0, code)
        result = json.loads(output.getvalue())
        self.assertEqual("recovered", result["status"])


if __name__ == "__main__": unittest.main()
