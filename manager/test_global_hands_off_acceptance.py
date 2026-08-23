"""Tests for manager.global_hands_off_acceptance -- the read-only
HOME_VISIBLE / GLOBAL_HANDS_OFF_COMPLETE evidence verifier.

Every test here is pure: no subprocess, no network, no filesystem, no git,
no provider launch. `now` is always fixed and injected so nothing depends
on wall-clock time.
"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from manager.global_hands_off_acceptance import (
    GLOBAL_HANDS_OFF_COMPLETE,
    HOME_VISIBLE_MILESTONE_PASS,
    STATUS_FAIL,
    STATUS_NOT_READY,
    STATUS_PASS,
    STATUS_UNKNOWN,
    evaluate_global_hands_off_complete,
    evaluate_home_visible,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(seconds=60)).isoformat()
STALE = (NOW - timedelta(seconds=99999)).isoformat()
SHA_A = "a" * 40
SHA_B = "b" * 40


def _complete_smoke():
    return {
        "request_id": "smoke-req-0001",
        "generated_at": FRESH,
        "max_age_seconds": 3600,
        "is_duplicate": False,
        "reused_historical_request_id": False,
        "components": {
            "ingress": True,
            "task": True,
            "command": True,
            "execution": True,
            "provider_process_evidence": True,
            "session": True,
            "terminal_completion": True,
            "handoff": True,
        },
        "links": {
            "execution_linked_to_session": True,
            "session_linked_to_handoff": True,
        },
    }


def _complete_home_visible_evidence():
    return {
        "formal_identity": {"expected": SHA_A, "actual": SHA_A},
        "runtime_state": {"tested": SHA_A, "activated": SHA_A, "running": SHA_A},
        "remote_runtime_identity": {"expected_sha": SHA_A, "actual_sha": SHA_A},
        "workspace_root_authority": {"valid": True},
        "watcher_identity": {"valid": True},
        "dashboard_health": {"healthy": True, "session_center_healthy": True},
        "provider": {
            "name": "claude",
            "reliable": True,
            "usable": True,
            "remaining": 42,
            "generated_at": FRESH,
            "last_updated": FRESH,
            "max_age_seconds": 3600,
        },
        "smoke": _complete_smoke(),
        "integrity": {"duplicate_found": False, "stale_leaked_execution_found": False},
        "status_agreement": {"dashboard_agrees_with_record_chain": True},
    }


def _complete_write_e2e():
    return {
        "kind": "write",
        "generated_at": FRESH,
        "max_age_seconds": 3600,
        "project_correct": True,
        "governance_valid": True,
        "baseline_valid": True,
        "isolation_valid": True,
        "provider_execution_valid": True,
        "tests_passed": True,
        "commit_present": True,
        "pushed": True,
        "chatgpt_validation": True,
        "lifecycle": {"task": True, "command": True, "execution": True, "session": True, "handoff": True},
    }


def _complete_global_evidence():
    return {
        "home_visible": _complete_home_visible_evidence(),
        "canonical_baseline": {"status": STATUS_PASS, "reason": "main/formal/tested/target identical"},
        "direct_invoke": {"accepted": True, "frozen": True},
        "continuation": {"accepted": True, "frozen": True},
        "rule44": {"status": STATUS_PASS, "reason": "promotion gate passed"},
        "adm_write_e2e": _complete_write_e2e(),
        "non_adm_write_e2e": _complete_write_e2e(),
        "autonomous_continuation": {
            "proven": True,
            "manual_continue_required": False,
            "human_authorization_boundary_stops_correctly": True,
        },
    }


class HomeVisiblePositiveTest(unittest.TestCase):
    def test_complete_evidence_passes(self):
        result = evaluate_home_visible(_complete_home_visible_evidence(), now=NOW)
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.label, HOME_VISIBLE_MILESTONE_PASS)
        self.assertEqual(result.failing, [])

    def test_missing_evidence_is_unknown_not_fail(self):
        result = evaluate_home_visible({}, now=NOW)
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_non_dict_evidence_is_unknown(self):
        result = evaluate_home_visible(None, now=NOW)  # type: ignore[arg-type]
        self.assertEqual(result.status, STATUS_UNKNOWN)


class HomeVisibleAntiFakeTest(unittest.TestCase):
    def _mutated(self, mutate):
        evidence = _complete_home_visible_evidence()
        mutate(evidence)
        return evaluate_home_visible(evidence, now=NOW)

    def test_task_and_command_only_no_execution(self):
        def mutate(e):
            e["smoke"]["components"]["execution"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_fake_execution_no_provider_process_evidence(self):
        def mutate(e):
            e["smoke"]["components"]["provider_process_evidence"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_execution_but_no_session(self):
        def mutate(e):
            e["smoke"]["components"]["session"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_session_but_no_handoff(self):
        def mutate(e):
            e["smoke"]["components"]["handoff"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_stale_provider_quota(self):
        def mutate(e):
            e["provider"]["last_updated"] = STALE
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_generated_at_fresh_but_provider_last_updated_stale(self):
        def mutate(e):
            e["provider"]["generated_at"] = FRESH
            e["provider"]["last_updated"] = STALE
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_provider_with_zero_remaining(self):
        def mutate(e):
            e["provider"]["remaining"] = 0
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_tested_activated_running_mismatch(self):
        def mutate(e):
            e["runtime_state"]["running"] = SHA_B
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_cloud_runtime_old_sha(self):
        def mutate(e):
            e["remote_runtime_identity"]["actual_sha"] = SHA_B
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_broken_execution_link(self):
        def mutate(e):
            e["smoke"]["links"]["execution_linked_to_session"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_broken_session_link(self):
        def mutate(e):
            e["smoke"]["links"]["session_linked_to_handoff"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_duplicate_smoke_records(self):
        def mutate(e):
            e["smoke"]["is_duplicate"] = True
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_reused_historical_request_id(self):
        def mutate(e):
            e["smoke"]["reused_historical_request_id"] = True
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_duplicate_execution_record(self):
        def mutate(e):
            e["integrity"]["duplicate_found"] = True
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_stale_leaked_execution(self):
        def mutate(e):
            e["integrity"]["stale_leaked_execution_found"] = True
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_dashboard_status_disagrees_with_record_chain(self):
        def mutate(e):
            e["status_agreement"]["dashboard_agrees_with_record_chain"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_missing_provider_section_is_unknown(self):
        def mutate(e):
            del e["provider"]
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertNotEqual(result.status, STATUS_PASS)


class GlobalHandsOffPositiveTest(unittest.TestCase):
    def test_complete_evidence_passes(self):
        result = evaluate_global_hands_off_complete(_complete_global_evidence(), now=NOW)
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.label, GLOBAL_HANDS_OFF_COMPLETE)
        self.assertEqual(result.failing, [])

    def test_precomputed_home_visible_result_is_honored(self):
        home_result = evaluate_home_visible(_complete_home_visible_evidence(), now=NOW)
        evidence = _complete_global_evidence()
        del evidence["home_visible"]
        result = evaluate_global_hands_off_complete(evidence, home_visible_result=home_result, now=NOW)
        self.assertEqual(result.status, STATUS_PASS)


class GlobalHandsOffAntiFakeTest(unittest.TestCase):
    def _mutated(self, mutate):
        evidence = _complete_global_evidence()
        mutate(evidence)
        return evaluate_global_hands_off_complete(evidence, now=NOW)

    def test_home_visible_not_passed_blocks_global(self):
        def mutate(e):
            e["home_visible"]["runtime_state"]["running"] = SHA_B
        result = self._mutated(mutate)
        self.assertNotEqual(result.status, STATUS_PASS)

    def test_canonical_main_formal_mismatch(self):
        def mutate(e):
            e["canonical_baseline"] = {"status": STATUS_FAIL, "reason": "diverged"}
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_direct_invoke_absent(self):
        def mutate(e):
            del e["direct_invoke"]
        result = self._mutated(mutate)
        self.assertNotEqual(result.status, STATUS_PASS)
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_continuation_foundation_absent(self):
        def mutate(e):
            del e["continuation"]
        result = self._mutated(mutate)
        self.assertNotEqual(result.status, STATUS_PASS)

    def test_rule44_result_absent(self):
        def mutate(e):
            del e["rule44"]
        result = self._mutated(mutate)
        self.assertNotEqual(result.status, STATUS_PASS)
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_adm_write_e2e_only_no_non_adm(self):
        def mutate(e):
            del e["non_adm_write_e2e"]
        result = self._mutated(mutate)
        self.assertNotEqual(result.status, STATUS_PASS)

    def test_two_read_only_e2es_instead_of_write(self):
        def mutate(e):
            e["adm_write_e2e"]["kind"] = "read_only"
            e["non_adm_write_e2e"]["kind"] = "read_only"
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_autonomous_chain_requires_manual_continue(self):
        def mutate(e):
            e["autonomous_continuation"]["manual_continue_required"] = True
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_human_authorization_boundary_broken(self):
        def mutate(e):
            e["autonomous_continuation"]["human_authorization_boundary_stops_correctly"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_write_e2e_missing_chatgpt_validation(self):
        def mutate(e):
            e["adm_write_e2e"]["chatgpt_validation"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_write_e2e_broken_lifecycle_link(self):
        def mutate(e):
            e["non_adm_write_e2e"]["lifecycle"]["handoff"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_FAIL)

    def test_rule44_reports_convergence_required_not_pass(self):
        def mutate(e):
            e["rule44"] = {"status": STATUS_NOT_READY, "reason": "convergence required"}
        result = self._mutated(mutate)
        self.assertNotEqual(result.status, STATUS_PASS)

    def test_direct_invoke_accepted_but_not_frozen(self):
        def mutate(e):
            e["direct_invoke"]["frozen"] = False
        result = self._mutated(mutate)
        self.assertEqual(result.status, STATUS_NOT_READY)


class StatusVocabularyTest(unittest.TestCase):
    def test_unknown_and_fail_never_collapse(self):
        unknown_result = evaluate_home_visible({}, now=NOW)
        fail_evidence = _complete_home_visible_evidence()
        fail_evidence["runtime_state"]["running"] = SHA_B
        fail_result = evaluate_home_visible(fail_evidence, now=NOW)
        self.assertEqual(unknown_result.status, STATUS_UNKNOWN)
        self.assertEqual(fail_result.status, STATUS_FAIL)
        self.assertNotEqual(unknown_result.status, fail_result.status)

    def test_not_ready_distinct_from_fail_and_unknown(self):
        evidence = _complete_global_evidence()
        evidence["continuation"]["frozen"] = False
        result = evaluate_global_hands_off_complete(evidence, now=NOW)
        self.assertEqual(result.status, STATUS_NOT_READY)


class ReadOnlyPurityTest(unittest.TestCase):
    def test_evaluators_do_not_mutate_input(self):
        evidence = _complete_global_evidence()
        snapshot = copy.deepcopy(evidence)
        evaluate_global_hands_off_complete(evidence, now=NOW)
        self.assertEqual(evidence, snapshot)

    def test_module_has_no_subprocess_or_network_imports(self):
        import manager.global_hands_off_acceptance as module
        with open(module.__file__, encoding="utf-8") as fh:
            lines = fh.readlines()
        import_lines = [ln for ln in lines if ln.startswith("import ") or ln.startswith("from ")]
        for forbidden in ("subprocess", "socket", "requests", "urllib"):
            for line in import_lines:
                self.assertNotIn(forbidden, line, f"module must not import {forbidden!r} (found: {line!r})")


if __name__ == "__main__":
    unittest.main()
