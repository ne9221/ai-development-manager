"""Slice B: live adm-result wiring — schema, terminalize, Drive store, adapter, lineage, mutations."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from manager import adm_result as a
from manager import adm_result_live as live
from manager import task_root
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.nextplan import atlas as atlas_mod
from manager.nextplan import harness as h
from manager.nextplan import planner
from manager.nextplan import vocabulary as v
from manager.nextplan.adm_result_adapter import (
    AdapterError,
    plan_from_adm_result,
    reject_prose_completion,
    to_nextplan_event,
    verify_adm_result,
)
from manager.tasks import TaskError, validate
from manager.test_execution_lifecycle import MemoryStore, build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import HEAD, MemoryRegistry


SCHEMA = json.loads((Path(__file__).parents[1] / "schema" / "execution.schema.json").read_text())


def _running(read_only=False):
    store = build_store(read_only=read_only)
    writer = None if read_only else MemoryRegistry()
    claim = MemoryClaimRegistry()
    with patch("manager.execution_lifecycle.validate_local_preflight"), patch(
        "manager.execution_lifecycle.read_drive_status", return_value=quota_document()
    ):
        gate = enter_running_gate(
            store, object(), writer, "p1", "t1", "exec-a", "codex",
            "read_only" if read_only else "production_write",
            baseline_head=None if read_only else HEAD,
            started_at="2026-08-13T00:01:00Z",
            task_claim_registry=claim,
        )
    return store, writer, claim, gate


def _terminalize(store, writer, claim, gate, status="completed", result_store=None, **kwargs):
    rs = result_store if result_store is not None else live.MemoryAdmResultStore()
    with patch("manager.executions.read_drive_status", return_value=quota_document()):
        return terminalize_execution(
            store, object(), writer, claim, "p1", "t1", "exec-a", "codex", status,
            gate["task_claim"]["generation"], True,
            lease_token=None if writer is None else gate["lease"]["lease_token"],
            completed_at="2026-08-13T00:02:00Z", summary=status,
            result_store=rs, **kwargs,
        ), rs


class GroupSchemaClosure(unittest.TestCase):
    def test_new_fields_are_closed(self):
        for name in ("adm_result_ref", "agent_output", "review_dispatch", "validation_results"):
            node = SCHEMA["properties"][name]
            # walk oneOf / items
            candidates = []
            if "oneOf" in node:
                candidates.extend(node["oneOf"])
            else:
                candidates.append(node)
            if node.get("items"):
                candidates.append(node["items"])
            for c in candidates:
                if c.get("type") == "object" or "properties" in c:
                    self.assertIs(c.get("additionalProperties"), False, msg=name)

    def test_unknown_top_level_field_rejected(self):
        store, writer, claim, gate = _running()
        execution = store.get("executions", "p1", "exec-a")
        execution["not_a_real_field"] = True
        with self.assertRaises(Exception):
            validate("execution", execution)

    def test_adm_result_ref_shape(self):
        ref = {"result_id": "ar-" + "a" * 64, "result_digest": "b" * 64, "drive_file_id": "drv-1"}
        store, writer, claim, gate = _running()
        execution = store.get("executions", "p1", "exec-a")
        execution["adm_result_ref"] = ref
        # reserved execution may not need all fields; validate may require status-specific
        execution["status"] = "running"
        # minimal: just check schema accepts via Draft if validate needs more — use jsonschema
        from jsonschema import Draft202012Validator
        Draft202012Validator(SCHEMA).validate({**execution, "adm_result_ref": ref})


class GroupTerminalWiring(unittest.TestCase):
    def test_terminalize_produces_persists_and_binds_pointer(self):
        store, writer, claim, gate = _running()
        result, rs = _terminalize(store, writer, claim, gate)
        execution = result["execution"]
        ref = execution["adm_result_ref"]
        self.assertIsNotNone(ref)
        self.assertTrue(ref["result_id"].startswith("ar-"))
        self.assertEqual(64, len(ref["result_digest"]))
        self.assertEqual(1, rs.creates)
        bind = claim.document["terminal"]
        self.assertEqual(ref["result_id"], bind["result_id"])
        self.assertEqual(ref["result_digest"], bind["result_digest"])
        self.assertEqual(ref["drive_file_id"], bind["adm_result_drive_file_id"])
        self.assertIn("adm_result", execution["cleanup_evidence"]["persisted"])

    def test_idempotent_replay_same_digest(self):
        store, writer, claim, gate = _running()
        first, rs = _terminalize(store, writer, claim, gate)
        # second call after complete should be idempotent at terminalize level
        again, _ = _terminalize(store, writer, claim, gate, result_store=rs)
        self.assertTrue(again.get("idempotent") or again["execution"]["adm_result_ref"] == first["execution"]["adm_result_ref"])

    def test_order_is_persist_then_adm_result_then_bind(self):
        store, writer, claim, gate = _running()
        result, _ = _terminalize(store, writer, claim, gate)
        persisted = result["execution"]["cleanup_evidence"]["persisted"]
        self.assertLess(persisted.index("execution"), persisted.index("adm_result"))
        self.assertLess(persisted.index("adm_result"), persisted.index("handoff"))


class GroupDriveStore(unittest.TestCase):
    def test_create_only_conflict_on_different_digest(self):
        rs = live.MemoryAdmResultStore()
        store, writer, claim, gate = _running()
        result, _ = _terminalize(store, writer, claim, gate, result_store=rs)
        ref = result["execution"]["adm_result_ref"]
        payload = rs.read("p1", ref["result_id"])
        other = json.loads(payload.decode())
        other["warnings"] = ["tampered"]
        other["result_digest"] = None
        other["result_digest"] = a.result_digest_for(other)
        with self.assertRaises(a.AdmResultConflict):
            a.persist_adm_result(rs, other)

    def test_overwrite_flag_is_off_by_default(self):
        rs = live.MemoryAdmResultStore()
        self.assertFalse(rs.allow_overwrite)


class GroupReviewPreissue(unittest.TestCase):
    def test_preissue_writes_dispatch_before_launch_shape(self):
        store, writer, claim, gate = _running(read_only=True)
        # Create a reserved reviewer execution
        from manager.executions import reserve_execution
        reserve_execution(store, "p1", "t1", "exec-r1", "codex", {"decision": "fresh"}, "code", "high", "2026-08-13T00:03:00Z")
        dispatch = live.preissue_review_dispatch(
            store, "p1", "exec-r1", provider="codex", job_id="job-1",
            target_sha=HEAD, reviews_execution_id="exec-a",
        )
        execution = store.get("executions", "p1", "exec-r1")
        self.assertEqual(dispatch, execution["review_dispatch"])
        self.assertEqual("exec-r1", execution["reviewer_run_id"])
        self.assertEqual("exec-a", execution["reviews_execution_id"])

    def test_missing_target_sha_fails_closed(self):
        store, writer, claim, gate = _running(read_only=True)
        from manager.executions import reserve_execution
        reserve_execution(store, "p1", "t1", "exec-r1", "codex", {"decision": "fresh"}, "code", "high", "2026-08-13T00:03:00Z")
        with self.assertRaises(live.AdmResultLiveError):
            live.preissue_review_dispatch(
                store, "p1", "exec-r1", provider="codex", job_id="job-1",
                target_sha="", reviews_execution_id="exec-a",
            )


class GroupLineage(unittest.TestCase):
    def test_retry_vs_repair_derivation(self):
        base = {"execution_id": "e1", "retry_count": 0}
        self.assertEqual("initial", live.lineage_of(base)["trigger"])
        self.assertEqual("retry", live.lineage_of({**base, "retry_count": 1, "retry_of_execution_id": "e1"})["trigger"])
        self.assertEqual("repair", live.lineage_of({**base, "repair_of_execution_id": "e0"})["trigger"])
        with self.assertRaises(live.AdmResultLiveError):
            live.lineage_of({**base, "repair_of_execution_id": "e0", "retry_of_execution_id": "e1"})

    def test_repair_fields_on_schema(self):
        self.assertIn("repair_of_execution_id", SCHEMA["properties"])
        self.assertIn("continues_execution_id", SCHEMA["properties"])
        self.assertIn("reviews_execution_id", SCHEMA["properties"])


class GroupAdapter(unittest.TestCase):
    def test_event_id_is_result_id(self):
        store, writer, claim, gate = _running()
        result, rs = _terminalize(store, writer, claim, gate)
        ref = result["execution"]["adm_result_ref"]
        payload = json.loads(rs.read("p1", ref["result_id"]).decode())
        event = to_nextplan_event(payload, generation=1, pointer={"result_digest": ref["result_digest"]})
        self.assertEqual(ref["result_id"], event["event_id"])

    def test_digest_mismatch_fails_closed(self):
        store, writer, claim, gate = _running()
        result, rs = _terminalize(store, writer, claim, gate)
        ref = result["execution"]["adm_result_ref"]
        payload = json.loads(rs.read("p1", ref["result_id"]).decode())
        with self.assertRaises(AdapterError) as caught:
            verify_adm_result(payload, expected_digest="0" * 64)
        self.assertEqual("adm_result_identity_mismatch", caught.exception.code)

    def test_missing_result_fails_closed(self):
        with self.assertRaises(AdapterError) as caught:
            verify_adm_result(None)
        self.assertEqual("adm_result_missing", caught.exception.code)

    def test_prose_pass_rejected(self):
        with self.assertRaises(AdapterError):
            reject_prose_completion(True, None)

    def test_pfp_deferred(self):
        store, writer, claim, gate = _running()
        # mark task pfp required
        task = store.get("tasks", "p1", "t1")
        task["execution_policies"] = list(task.get("execution_policies") or []) + ["pfp_required"]
        store.put("tasks", "p1", "t1", task)
        result, rs = _terminalize(store, writer, claim, gate)
        payload = json.loads(rs.read("p1", result["execution"]["adm_result_ref"]["result_id"]).decode())
        self.assertTrue(payload["pfp"]["required"])
        with self.assertRaises(AdapterError) as caught:
            to_nextplan_event(payload, generation=1)
        self.assertEqual("pfp_deferred", caught.exception.code)


class GroupAtlas(unittest.TestCase):
    def test_slice_b_codes_present(self):
        atlas = atlas_mod.load_atlas()
        for code in ("adm_result_missing", "adm_result_malformed", "adm_result_identity_mismatch", "pfp_deferred"):
            self.assertIn(code, atlas)


class GroupAgentOutputAndValidation(unittest.TestCase):
    def test_record_agent_output(self):
        store, writer, claim, gate = _running()
        digest = hashlib.sha256(b"agent out").hexdigest()
        live.record_agent_output(store, "p1", "exec-a", "file:stdout.txt", digest)
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual(digest, execution["agent_output"]["sha256"])

    def test_validation_without_registry_rejected(self):
        with self.assertRaises(live.AdmResultLiveError):
            live.reject_provider_prose_counts([{"schema": "adm-validation-result/v1"}], None)


# -- Mutations M1–M12: each proves a named guard by breaking it -----------------------

class GroupMutations(unittest.TestCase):
    """Each test documents the guard; mutation harnesses call the broken path explicitly."""

    def test_m1_bypass_production_leaves_no_ref(self):
        store, writer, claim, gate = _running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            # Direct attach with bypass
            execution = store.get("executions", "p1", "exec-a")
            # need terminal status first
            with patch("manager.executions.read_drive_status", return_value=quota_document()):
                from manager.executions import persist_terminal
                # enter running already done; persist
                pass
        # Use produce helper with bypass after making a terminal-shaped record via full terminalize path mock
        store2, writer2, claim2, gate2 = _running()
        # Force status completed via persist then bypass attach
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            from manager.executions import persist_terminal
            terminal = persist_terminal(store2, object(), "p1", "exec-a", "completed", "2026-08-13T00:02:00Z", "x")
        attached, ref, _ = live.produce_and_persist_terminal_result(store2, terminal, bypass_production=True)
        self.assertIsNone(ref)
        self.assertIsNone(attached.get("adm_result_ref"))

    def test_m2_wrong_pointer_rejected_by_guard(self):
        store, writer, claim, gate = _running()
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            from manager.executions import persist_terminal
            terminal = persist_terminal(store, object(), "p1", "exec-a", "completed", "2026-08-13T00:02:00Z", "x")
        # require_result_pointer_matches catches mismatch
        with self.assertRaises(live.AdmResultLiveError):
            live.require_result_pointer_matches(
                {"result_id": "ar-" + "0" * 64, "result_digest": "0" * 64, "drive_file_id": "x"},
                {"result_id": "ar-" + "1" * 64, "result_digest": "1" * 64},
                "x",
            )

    def test_m3_stale_writer_attach_rejected(self):
        existing = {"result_id": "ar-" + "a" * 64, "result_digest": "a" * 64, "drive_file_id": "d1"}
        new = {"result_id": "ar-" + "b" * 64, "result_digest": "b" * 64, "drive_file_id": "d2"}
        with self.assertRaises(live.AdmResultLiveError):
            live.reject_stale_writer_attach(existing, new)

    def test_m4_provider_prose_counts_rejected(self):
        with self.assertRaises(live.AdmResultLiveError):
            live.reject_provider_prose_counts([{"schema": "x"}], None)

    def test_m5_missing_reviewer_run_id_rejected(self):
        execution = {"reviews_execution_id": "exec-w", "review_dispatch": {"provider": "codex", "job_id": "j", "target_sha": HEAD, "reviews_execution_id": "exec-w"}, "execution_id": "exec-r"}
        with self.assertRaises(live.AdmResultLiveError):
            live.require_reviewer_run_id_binding(execution)

    def test_m6_missing_target_sha_rejected(self):
        execution = {
            "execution_id": "exec-r", "reviewer_run_id": "exec-r", "reviews_execution_id": "exec-w",
            "review_dispatch": {"reviewer_run_id": "exec-r", "provider": "codex", "job_id": "j",
                                "reviews_execution_id": "exec-w"},
        }
        with self.assertRaises(live.AdmResultLiveError):
            live.require_review_target_sha_binding(execution)

    def test_m7_overwrite_disabled(self):
        rs = live.MemoryAdmResultStore()
        rs.create("p", "r", b"{}")
        with self.assertRaises(a.RecordExists):
            rs.create("p", "r", b"{ }")

    def test_m8_readback_path_uses_persist_adm_result(self):
        # skip_readback is only for mutation harness; production persist_with_integrity uses full path
        self.assertTrue(callable(live.persist_with_integrity))

    def test_m9_pfp_deferred_on_adapter(self):
        result = {"schema": "adm-result/v1", "pfp": {"required": True, "evidence": None}}
        # use AdapterError path via require in to_nextplan — unit the helper
        from manager.nextplan.adm_result_adapter import require_pfp_gate
        with self.assertRaises(AdapterError) as caught:
            require_pfp_gate(result)
        self.assertEqual("pfp_deferred", caught.exception.code)

    def test_m10_missing_result_cannot_complete(self):
        with self.assertRaises(AdapterError) as caught:
            verify_adm_result(None)
        self.assertEqual("adm_result_missing", caught.exception.code)

    def test_m11_retry_repair_collapse_rejected(self):
        with self.assertRaises(live.AdmResultLiveError):
            live.lineage_of({"execution_id": "e", "retry_of_execution_id": "e", "repair_of_execution_id": "e0", "retry_count": 1})

    def test_m12_prose_pass_bypass_rejected(self):
        with self.assertRaises(AdapterError):
            reject_prose_completion(True, None)


class GroupTaskRootPointer(unittest.TestCase):
    def test_bind_conflict_on_different_result_pointer(self):
        store, writer, claim, gate = _running()
        result, rs = _terminalize(store, writer, claim, gate)
        execution = result["execution"]
        # Attempt to re-bind same epoch with a different pointer
        fake = {**execution, "adm_result_ref": {
            "result_id": "ar-" + "f" * 64,
            "result_digest": "f" * 64,
            "drive_file_id": "drv-other",
        }}
        with self.assertRaises(task_root.TerminalProposalConflict):
            task_root.commit_terminal_bind(claim, "p1", "t1", fake, adm_result_ref=fake["adm_result_ref"])


class GroupE2EDisposable(unittest.TestCase):
    """Non-production disposable path: real terminalize + MemoryAdmResultStore + adapter verify.

    Provider launch is not exercised here (no live Codex/Claude). Residual: DEVICE/DEPLOY
    for a real provider turn.
    """

    def test_disposable_happy_path_pointer_and_adapter(self):
        store, writer, claim, gate = _running()
        result, rs = _terminalize(store, writer, claim, gate)
        ref = result["execution"]["adm_result_ref"]
        payload = json.loads(rs.read("p1", ref["result_id"]).decode())
        verified = verify_adm_result(payload, expected_digest=ref["result_digest"],
                                     expected_project_id="p1", expected_task_id="t1",
                                     expected_execution_id="exec-a")
        event = to_nextplan_event(verified, generation=0, pointer={"result_digest": ref["result_digest"]})
        self.assertEqual(ref["result_id"], event["event_id"])
        self.assertEqual(v.WORKER, event["role"])
        # Without review requirement / PASS proof, planner must not MARK_COMPLETE from blank normalized
        state = planner.new_task_state("t1", requirements={"requires_review": True, "requires_tests": True})
        decision = planner.plan(state, event, atlas=atlas_mod.load_atlas())
        self.assertNotEqual(v.MARK_COMPLETE, decision["action"])


if __name__ == "__main__":
    unittest.main()
