"""Verification Loop Phase B-1: reference evaluator + false-complete fixtures.

Pure deterministic evaluator tests -- no real provider, Excel, Drive,
GitHub, screenshot or Session Center integration. Every scenario below is
non-vacuous: each assertion is on a field that a broken/naive evaluator
(e.g. one that trusted a report's own claimed result, or that let any
same-gate PASS report count regardless of SHA) would get wrong -- see the
attack-test section for the explicit negative controls.
"""

import unittest

from manager.verification_loop.evaluator import _derive_next_action, _retry_eligible, evaluate
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
        # B6: single-factor -- every other field on wrong_sha_v1 is exactly
        # what a legitimate V1 report would look like (correct checker
        # identity/version, correct bundle/base/execution ids, trusted
        # evidence, resolved identity). candidate_sha is the ONLY thing
        # wrong, so this test can only pass because of the SHA check --
        # removing it (mutant N6) must flip this to ACCEPTED.
        bundle = _simple_bundle()
        good_v0 = _report(gate_id="V0")
        wrong_sha_v1 = _report(
            gate_id="V1",
            checker_identity="integration_checker",
            candidate_sha="sha-candidate-OTHER",
        )
        result = evaluate(_simple_task(), _simple_execution(), bundle, [good_v0, wrong_sha_v1])
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")
        self.assertIn("V1", result.missing_required_gates)
        self.assertEqual(result.invalidated_report_reasons[0][1], "CANDIDATE_SHA_MISMATCH")

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

    # -- Phase B-1R additions -------------------------------------------

    def test_13_category_environment_transient_without_allowlist_cannot_retry(self):
        """B1: a report can no longer relabel an unclaused defect as
        ENVIRONMENT_TRANSIENT via .category when the signature isn't on the
        bundle's transient_allowlist and there is no checker transient_code
        admission path -- it must fall through to the deterministic default,
        never reach RETRY_SAME_CANDIDATE."""
        bundle = _simple_bundle()  # no transient_allowlist entries at all
        v0 = _report(
            result="FAIL",
            failure_observations=(
                FailureObservation(signature="not_allowlisted", category="ENVIRONMENT_TRANSIENT"),
            ),
        )
        v1 = _report(gate_id="V1", checker_identity="integration_checker")
        result = evaluate(_simple_task(), _simple_execution(), bundle, [v0, v1])
        self.assertNotIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)
        self.assertNotEqual(result.next_action, "RETRY_SAME_CANDIDATE")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_14_test_defect_category_without_predicate_forces_unknown(self):
        """B1: TD-1/TD-2 are not implemented in Phase B-1R, so a
        report-authored TEST_DEFECT claim must not be admitted into
        TEST_DEFECT, and must not be quietly downgraded into
        ACCEPTANCE_CONTRACT_DEFECT either -- it must escalate as UNKNOWN."""
        bundle = _simple_bundle()
        v0 = _report(
            result="FAIL",
            failure_observations=(FailureObservation(signature="flaky_maybe", category="TEST_DEFECT"),),
        )
        v1 = _report(gate_id="V1", checker_identity="integration_checker")
        result = evaluate(_simple_task(), _simple_execution(), bundle, [v0, v1])
        self.assertIn("UNKNOWN", result.admitted_failure_classes)
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)
        self.assertNotIn("ACCEPTANCE_CONTRACT_DEFECT", result.admitted_failure_classes)
        self.assertEqual(result.next_action, "ESCALATE_HUMAN")
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")

    def test_15_admissible_fail_on_non_required_gate_blocks_acceptance(self):
        """B3: medium risk only requires V0/V1, but an admissible FAIL on an
        extra gate (V3, not required at this risk tier) must still block
        acceptance -- required_gates governs coverage, not which admissible
        failures may be ignored."""
        bundle = _simple_bundle(
            checker_identities={
                "V0": ("unit_checker", "1.0"),
                "V1": ("integration_checker", "1.0"),
                "V3": ("extra_checker", "1.0"),
            }
        )
        v3_fail = _report(
            gate_id="V3",
            checker_identity="extra_checker",
            result="FAIL",
            failure_observations=(FailureObservation(signature="unexpected_defect"),),
        )
        reports = [
            _report(gate_id="V0"),
            _report(gate_id="V1", checker_identity="integration_checker"),
            v3_fail,
        ]
        result = evaluate(_simple_task(), _simple_execution(), bundle, reports)
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")
        self.assertIn("ACCEPTANCE_CONTRACT_DEFECT", result.admitted_failure_classes)

    def test_16_executor_identity_none_is_inadmissible_not_fail_open(self):
        """B5: execution.executor_identity=None must never be treated as "no
        conflict" -- every report tied to that execution becomes
        inadmissible (EXECUTOR_IDENTITY_UNRESOLVED), never silently ACCEPTED."""
        bundle = _simple_bundle()
        execution = _simple_execution(executor_identity=None)
        reports = [_report(gate_id="V0"), _report(gate_id="V1", checker_identity="integration_checker")]
        result = evaluate(_simple_task(), execution, bundle, reports)
        self.assertNotEqual(result.acceptance_state, "ACCEPTED")
        self.assertEqual(result.admissible_report_keys, ())
        reasons = {reason for _, reason in result.invalidated_report_reasons}
        self.assertEqual(reasons, {"EXECUTOR_IDENTITY_UNRESOLVED"})


# ---------------------------------------------------------------------------
# B2: duplicate (gate_id, round) reports must be permutation-invariant
# ---------------------------------------------------------------------------

class DuplicateReportOrderInvarianceTests(unittest.TestCase):
    def test_duplicate_same_gate_round_is_permutation_invariant_and_never_accepted(self):
        bundle = _simple_bundle()
        v0 = _report(gate_id="V0")
        pass_v1 = _report(gate_id="V1", checker_identity="integration_checker", result="PASS")
        fail_v1 = _report(
            gate_id="V1",
            checker_identity="integration_checker",
            result="FAIL",
            failure_observations=(FailureObservation(signature="whatever"),),
        )

        order_fail_then_pass = evaluate(_simple_task(), _simple_execution(), bundle, [v0, fail_v1, pass_v1])
        order_pass_then_fail = evaluate(_simple_task(), _simple_execution(), bundle, [v0, pass_v1, fail_v1])

        self.assertEqual(order_fail_then_pass.next_action, order_pass_then_fail.next_action)
        self.assertEqual(order_fail_then_pass.acceptance_state, order_pass_then_fail.acceptance_state)
        self.assertEqual(
            order_fail_then_pass.admitted_failure_classes, order_pass_then_fail.admitted_failure_classes
        )
        self.assertEqual(
            order_fail_then_pass.missing_required_gates, order_pass_then_fail.missing_required_gates
        )
        self.assertNotEqual(order_fail_then_pass.acceptance_state, "ACCEPTED")
        self.assertNotEqual(order_pass_then_fail.acceptance_state, "ACCEPTED")


# ---------------------------------------------------------------------------
# B4: next_action guard order, tested directly against the extracted
# predicate so the priority order is provable independent of how a given
# admitted class actually got admitted.
# ---------------------------------------------------------------------------

class NextActionGuardOrderTests(unittest.TestCase):
    def test_production_scope_violation_alone_routes_to_pfp(self):
        self.assertEqual(
            _derive_next_action(
                {"PRODUCTION_SCOPE_VIOLATION"}, missing_required_gates=(), satisfied_gates=(), required_gates=("V0",)
            ),
            "ROUTE_TO_PFP",
        )

    def test_production_scope_violation_with_contract_violation_still_routes_to_pfp(self):
        self.assertEqual(
            _derive_next_action(
                {"PRODUCTION_SCOPE_VIOLATION", "CONTRACT_VIOLATION"},
                missing_required_gates=(),
                satisfied_gates=(),
                required_gates=("V0",),
            ),
            "ROUTE_TO_PFP",
        )

    def test_governance_conflict_outranks_production_scope_violation(self):
        self.assertEqual(
            _derive_next_action(
                {"GOVERNANCE_CONFLICT", "PRODUCTION_SCOPE_VIOLATION"},
                missing_required_gates=(),
                satisfied_gates=(),
                required_gates=("V0",),
            ),
            "ESCALATE_HUMAN",
        )

    def test_fallback_with_no_admitted_class_and_no_required_gates_escalates_and_records_unknown(self):
        admitted = set()
        result = _derive_next_action(admitted, missing_required_gates=(), satisfied_gates=(), required_gates=())
        self.assertEqual(result, "ESCALATE_HUMAN")
        self.assertEqual(admitted, {"UNKNOWN"})

    def test_fallback_never_adds_a_second_unknown_on_top_of_an_admitted_class(self):
        admitted = {"UNKNOWN"}
        result = _derive_next_action(admitted, missing_required_gates=(), satisfied_gates=(), required_gates=())
        self.assertEqual(result, "ESCALATE_HUMAN")
        self.assertEqual(admitted, {"UNKNOWN"})


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
