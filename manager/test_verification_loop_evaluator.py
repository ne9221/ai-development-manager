"""Verification Loop Phase B-1: reference evaluator + false-complete fixtures.

Pure deterministic evaluator tests -- no real provider, Excel, Drive,
GitHub, screenshot or Session Center integration. Every scenario below is
non-vacuous: each assertion is on a field that a broken/naive evaluator
(e.g. one that trusted a report's own claimed result, or that let any
same-gate PASS report count regardless of SHA) would get wrong -- see the
attack-test section for the explicit negative controls.
"""

import unittest

from manager.verification_loop.evaluator import _retry_eligible, evaluate
from manager.verification_loop.fixtures import (
    fx_adm_false_dispatch,
    fx_ledger_freeze_pane,
    fx_ob_mobile_overflow,
)
from manager.verification_loop.models import (
    AcceptanceBundleFixture,
    ExecutionFixture,
    FailureObservation,
    RiskRule,
    TaskFixture,
    VerificationReportFixture,
)


# ---------------------------------------------------------------------------
# Fixture 1: FX-LEDGER-FREEZE-PANE
# ---------------------------------------------------------------------------

class FreezePaneFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_ledger_freeze_pane()

    def test_risk_escalates_to_artifact_sensitive(self):
        bundle = self.fx["bundle_with_clause"]
        result = evaluate(
            self.fx["task"], self.fx["execution"], bundle,
            self.fx["reports_v0_v1_v2_pass"](bundle),
        )
        self.assertEqual(result.effective_risk, "artifact_sensitive")

    def test_v3_missing_blocks_acceptance(self):
        bundle = self.fx["bundle_with_clause"]
        result = evaluate(
            self.fx["task"], self.fx["execution"], bundle,
            self.fx["reports_v0_v1_v2_pass"](bundle),
        )
        self.assertIn("V3", result.missing_required_gates)
        self.assertEqual(result.next_action, "CONTINUE_VERIFICATION")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_v3_fail_with_contract_clause_is_repairable_contract_violation(self):
        bundle = self.fx["bundle_with_clause"]
        reports = self.fx["reports_v0_v1_v2_pass"](bundle) + [self.fx["v3_fail_report"](bundle)]
        result = evaluate(self.fx["task"], self.fx["execution"], bundle, reports)
        self.assertIn("CONTRACT_VIOLATION", result.admitted_failure_classes)
        self.assertEqual(result.next_action, "REPAIR")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_v3_fail_with_no_contract_clause_opens_bundle_revision(self):
        bundle = self.fx["bundle_without_clause"]
        reports = self.fx["reports_v0_v1_v2_pass"](bundle) + [self.fx["v3_fail_report"](bundle)]
        result = evaluate(self.fx["task"], self.fx["execution"], bundle, reports)
        self.assertIn("ACCEPTANCE_CONTRACT_DEFECT", result.admitted_failure_classes)
        self.assertEqual(result.next_action, "OPEN_BUNDLE_REVISION")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")


# ---------------------------------------------------------------------------
# Fixture 2: FX-OB-MOBILE-OVERFLOW
# ---------------------------------------------------------------------------

class MobileOverflowFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_ob_mobile_overflow()

    def test_incomplete_mobile_coverage_pass_is_unknown_escalate(self):
        reports = self.fx["reports_v0_v1_pass"] + [self.fx["v2_pass_desktop_only"]]
        result = evaluate(self.fx["task"], self.fx["execution"], self.fx["bundle"], reports)
        self.assertIn("UNKNOWN", result.admitted_failure_classes)
        self.assertEqual(result.next_action, "ESCALATE_HUMAN")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_full_coverage_overflow_defect_is_contract_violation_repair(self):
        v2 = self.fx["v2_report_full_coverage"](
            "FAIL", (FailureObservation(signature="mobile_overflow"),)
        )
        reports = self.fx["reports_v0_v1_pass"] + [self.fx["v2_pass_desktop_only"], v2]
        result = evaluate(self.fx["task"], self.fx["execution"], self.fx["bundle"], reports)
        self.assertIn("CONTRACT_VIOLATION", result.admitted_failure_classes)
        self.assertEqual(result.next_action, "REPAIR")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_full_coverage_genuine_pass_is_accepted(self):
        v2 = self.fx["v2_report_full_coverage"]("PASS")
        reports = self.fx["reports_v0_v1_pass"] + [v2]
        result = evaluate(self.fx["task"], self.fx["execution"], self.fx["bundle"], reports)
        self.assertEqual(result.next_action, "ACCEPTED")
        self.assertEqual(result.acceptance_state, "ACCEPTED")


# ---------------------------------------------------------------------------
# Fixture 3: FX-ADM-FALSE-DISPATCH
# ---------------------------------------------------------------------------

class FalseDispatchFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_adm_false_dispatch()

    def test_executor_self_report_cannot_satisfy_gate(self):
        reports = [self.fx["report_v0_pass"], self.fx["report_v1_false_pass"]]
        result = evaluate(
            self.fx["task"], self.fx["execution_non_production"], self.fx["bundle"], reports
        )
        self.assertIn("UNKNOWN", result.admitted_failure_classes)
        self.assertEqual(result.next_action, "ESCALATE_HUMAN")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_production_diff_routes_to_pfp_never_accepted(self):
        reports = [self.fx["report_v0_pass"], self.fx["report_v1_false_pass"]]
        result = evaluate(
            self.fx["task"], self.fx["execution_production"], self.fx["bundle"], reports
        )
        self.assertEqual(result.effective_risk, "production")
        self.assertEqual(result.next_action, "ROUTE_TO_PFP")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_production_diff_routes_to_pfp_even_with_no_reports_at_all(self):
        result = evaluate(
            self.fx["task"], self.fx["execution_production"], self.fx["bundle"], []
        )
        self.assertEqual(result.next_action, "ROUTE_TO_PFP")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")


# ---------------------------------------------------------------------------
# Section G: 12 minimum required attack tests
# ---------------------------------------------------------------------------

def _simple_bundle(**overrides):
    base = dict(
        bundle_hash="bundle-attack-hash-1",
        governance_digest="gov-digest-1",
        gate_requirements_by_risk={"low": ("V0",), "medium": ("V0", "V1")},
        checker_identities={
            "V0": ("unit_checker", "1.0"),
            "V1": ("integration_checker", "1.0"),
        },
    )
    base.update(overrides)
    return AcceptanceBundleFixture(**base)


def _simple_task(declared_risk="medium"):
    return TaskFixture(
        task_id="attack-task",
        declared_risk=declared_risk,
        deliverable_sha="sha-candidate-1",
        acceptance_bundle_ref="bundle-attack",
    )


def _simple_execution(executor_identity="codex-impl", diff_paths=("some/file.py",)):
    return ExecutionFixture(
        execution_id="exec-attack-1",
        task_id="attack-task",
        base_sha="sha-base-1",
        candidate_sha="sha-candidate-1",
        diff_paths=diff_paths,
        status="completed",
        executor_identity=executor_identity,
    )


def _report(**overrides):
    base = dict(
        execution_id="exec-attack-1",
        gate_id="V0",
        round=1,
        candidate_sha="sha-candidate-1",
        base_sha="sha-base-1",
        bundle_hash="bundle-attack-hash-1",
        checker_identity="unit_checker",
        checker_version="1.0",
        evidence_source="independent_check",
        identity_resolution="resolved",
        result="PASS",
    )
    base.update(overrides)
    return VerificationReportFixture(**base)


class AttackTests(unittest.TestCase):
    def test_01_candidate_sha_mismatch_is_inadmissible(self):
        bundle = _simple_bundle()
        report = _report(candidate_sha="sha-candidate-WRONG")
        result = evaluate(_simple_task(), _simple_execution(), bundle, [report])
        self.assertEqual(result.admissible_report_keys, ())
        self.assertEqual(result.invalidated_report_reasons[0][1], "CANDIDATE_SHA_MISMATCH")

    def test_02_bundle_hash_mismatch_is_inadmissible(self):
        bundle = _simple_bundle()
        report = _report(bundle_hash="bundle-attack-hash-WRONG")
        result = evaluate(_simple_task(), _simple_execution(), bundle, [report])
        self.assertEqual(result.admissible_report_keys, ())
        self.assertEqual(result.invalidated_report_reasons[0][1], "BUNDLE_HASH_MISMATCH")

    def test_03_checker_version_mismatch_is_inadmissible(self):
        bundle = _simple_bundle()
        report = _report(checker_version="9.9")
        result = evaluate(_simple_task(), _simple_execution(), bundle, [report])
        self.assertEqual(result.admissible_report_keys, ())
        self.assertEqual(result.invalidated_report_reasons[0][1], "CHECKER_VERSION_MISMATCH")

    def test_04_executor_identity_equals_checker_identity_is_inadmissible(self):
        bundle = _simple_bundle()
        execution = _simple_execution(executor_identity="unit_checker")
        report = _report()
        result = evaluate(_simple_task(), execution, bundle, [report])
        self.assertEqual(result.admissible_report_keys, ())
        self.assertEqual(result.invalidated_report_reasons[0][1], "EXECUTOR_EQUALS_CHECKER")

    def test_05_mixed_sha_reports_cannot_be_accepted(self):
        bundle = _simple_bundle()
        good_v0 = _report(gate_id="V0")
        wrong_sha_v1 = _report(gate_id="V1", candidate_sha="sha-candidate-OTHER")
        result = evaluate(_simple_task(), _simple_execution(), bundle, [good_v0, wrong_sha_v1])
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")
        self.assertIn("V1", result.missing_required_gates)

    def test_06_declared_low_risk_diff_high_escalates_effective_risk(self):
        bundle = _simple_bundle(risk_rules=(RiskRule("sensitive/", "high"),))
        execution = _simple_execution(diff_paths=("sensitive/thing.py",))
        result = evaluate(_simple_task(declared_risk="low"), execution, bundle, [])
        self.assertEqual(result.effective_risk, "high")

    def test_07_regression_plus_transient_cannot_retry(self):
        bundle = _simple_bundle(
            gate_contract_clauses={"V1": ("known_regression",)},
            transient_allowlist=("flaky_timeout",),
        )
        report = _report(
            gate_id="V1",
            checker_identity="integration_checker",
            result="FAIL",
            failure_observations=(
                FailureObservation(signature="known_regression", is_regression=True),
                FailureObservation(signature="flaky_timeout"),
            ),
        )
        result = evaluate(_simple_task(), _simple_execution(), bundle, [_report(), report])
        self.assertIn("REGRESSION", result.admitted_failure_classes)
        self.assertIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)
        self.assertNotEqual(result.next_action, "RETRY_SAME_CANDIDATE")

    def test_08_unknown_plus_regression_escalates_to_human(self):
        bundle = _simple_bundle(gate_contract_clauses={"V1": ("known_regression",)})
        regression_report = _report(
            gate_id="V1",
            checker_identity="integration_checker",
            result="FAIL",
            failure_observations=(FailureObservation(signature="known_regression", is_regression=True),),
        )
        unknown_report = _report(
            gate_id="V0",
            evidence_source="executor_claim",
            identity_resolution="needs_review",
        )
        result = evaluate(_simple_task(), _simple_execution(), bundle, [unknown_report, regression_report])
        self.assertIn("UNKNOWN", result.admitted_failure_classes)
        self.assertIn("REGRESSION", result.admitted_failure_classes)
        self.assertEqual(result.next_action, "ESCALATE_HUMAN")

    def test_09_frozen_oracle_hash_mismatch_rejects_stale_report(self):
        bundle = _simple_bundle(frozen_oracle_hashes={"V0": "oracle-hash-CURRENT"})
        stale_report = _report(oracle_hash="oracle-hash-OLD")
        result = evaluate(_simple_task(), _simple_execution(), bundle, [stale_report])
        self.assertEqual(result.admissible_report_keys, ())
        self.assertEqual(result.invalidated_report_reasons[0][1], "FROZEN_ORACLE_HASH_MISMATCH")

    def test_10_missing_required_gate_continues_verification_never_accepted(self):
        bundle = _simple_bundle()
        result = evaluate(_simple_task(), _simple_execution(), bundle, [_report(gate_id="V0")])
        self.assertIn("V1", result.missing_required_gates)
        self.assertEqual(result.next_action, "CONTINUE_VERIFICATION")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_11_production_risk_routes_to_pfp(self):
        bundle = _simple_bundle(risk_rules=(RiskRule("prod/", "production"),))
        execution = _simple_execution(diff_paths=("prod/canonical_runtime.py",))
        result = evaluate(_simple_task(), execution, bundle, [_report(gate_id="V0"), _report(gate_id="V1")])
        self.assertEqual(result.next_action, "ROUTE_TO_PFP")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_12_all_required_same_sha_pass_is_accepted(self):
        bundle = _simple_bundle()
        result = evaluate(
            _simple_task(), _simple_execution(), bundle,
            [_report(gate_id="V0"), _report(gate_id="V1", checker_identity="integration_checker")],
        )
        self.assertEqual(result.next_action, "ACCEPTED")
        self.assertEqual(result.acceptance_state, "ACCEPTED")


# ---------------------------------------------------------------------------
# proposed_class must never directly decide the result (Phase B-1 spec E)
# ---------------------------------------------------------------------------

class ProposedClassIgnoredTests(unittest.TestCase):
    def test_misleading_proposed_class_is_ignored(self):
        bundle = _simple_bundle(gate_contract_clauses={"V1": ("known_regression",)})
        report = _report(
            gate_id="V1",
            checker_identity="integration_checker",
            result="FAIL",
            proposed_class="ENVIRONMENT_TRANSIENT",
            failure_observations=(FailureObservation(signature="known_regression", is_regression=True),),
        )
        result = evaluate(_simple_task(), _simple_execution(), bundle, [_report(), report])
        self.assertIn("REGRESSION", result.admitted_failure_classes)
        self.assertNotIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)


class RetryEligiblePredicateTests(unittest.TestCase):
    def test_exact_transient_only_is_eligible(self):
        self.assertTrue(_retry_eligible({"ENVIRONMENT_TRANSIENT"}))

    def test_transient_plus_regression_is_not_eligible(self):
        self.assertFalse(_retry_eligible({"ENVIRONMENT_TRANSIENT", "REGRESSION"}))

    def test_empty_set_is_not_eligible(self):
        self.assertFalse(_retry_eligible(set()))

    def test_other_class_alone_is_not_eligible(self):
        self.assertFalse(_retry_eligible({"CONTRACT_VIOLATION"}))


if __name__ == "__main__":
    unittest.main()
