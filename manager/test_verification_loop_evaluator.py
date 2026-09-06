"""Verification Loop: the three false-complete fixtures and the guard order.

Carried forward from Phase B-1/B-1R and re-expressed on the Phase B-2 API. Every
property the B-1R independent review confirmed as closed still has a test here,
so the B-2 work cannot quietly reopen one: the class of a failure is never taken
from a report-authored field, duplicate reports are order-invariant, a failure on
a non-required gate still blocks, a production-scope violation always routes to
the Production Fix Protocol, and an unresolved executor identity is never read as
independence.

Nothing here talks to a real provider, Excel, Drive, GitHub, screenshot or
Session Center; those surfaces are fixture fields, which is what lets the
admission and classification logic be proven before any real checker exists.
"""

import unittest
from dataclasses import replace

from manager.verification_loop.evaluator import (
    _derive_next_action,
    _retry_eligible,
    evaluate,
)
from manager.verification_loop.fixtures import (
    consumed_against,
    fx_adm_false_dispatch,
    fx_ledger_freeze_pane,
    fx_ob_mobile_overflow,
)
from manager.verification_loop.models import FailureObservation

LEDGER_GATES = ("V0", "V1", "V2", "V3")
OB_GATES = ("V0", "V1", "V2")
ADM_GATES = ("V0", "V1")


def run(scenario, reports, tickets=None, **kwargs):
    return evaluate(
        scenario.task,
        scenario.execution,
        scenario.bundle,
        reports,
        preflight=scenario.preflight,
        tickets=consumed_against(
            tickets
            if tickets is not None
            else scenario.tickets_for(*[(r.gate_id, r.round) for r in reports]),
            reports,
        ),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Fixture 1: FX-LEDGER-FREEZE-PANE
# The suite is green because it never asserted the frozen panes at all.
# ---------------------------------------------------------------------------


class FreezePaneFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_ledger_freeze_pane()
        self.scenario = self.fx["scenario"](self.fx["bundle_with_clause"])

    def test_artifact_diff_escalates_risk_above_the_declared_tier(self):
        result = run(self.scenario, [self.scenario.report("V0")])
        self.assertEqual("medium", self.scenario.task.declared_risk)
        self.assertEqual("artifact_sensitive", result.effective_risk)

    def test_a_green_suite_without_the_artifact_gate_is_not_an_acceptance(self):
        # The whole fixture in one assertion: V0-V2 pass, and the workbook is
        # still wrong, because nothing has looked at it yet.
        result = run(self.scenario, [self.scenario.report(g) for g in ("V0", "V1", "V2")])
        self.assertEqual("CONTINUE_VERIFICATION", result.next_action)
        self.assertEqual(("V3",), result.missing_required_gates)
        self.assertNotEqual("ACCEPTED", result.acceptance_state)

    def test_a_clause_matched_freeze_pane_defect_is_repairable(self):
        reports = [self.scenario.report(g) for g in ("V0", "V1", "V2")]
        reports.append(
            self.scenario.report(
                "V3",
                result="FAIL",
                failure_observations=(self.fx["freeze_pane_failure"],),
            )
        )
        result = run(self.scenario, reports)
        self.assertEqual("REPAIR", result.next_action)
        self.assertEqual("REJECTED_NEEDS_REPAIR", result.acceptance_state)

    def test_a_defect_the_contract_does_not_cover_opens_a_bundle_revision(self):
        # Phase A v3 I1's third path: reproducible, but no clause covers it, so
        # the contract is incomplete. There is no ADVISORY-then-ACCEPTED route.
        scenario = self.fx["scenario"](self.fx["bundle_without_clause"])
        reports = [scenario.report(g) for g in ("V0", "V1", "V2")]
        reports.append(
            scenario.report(
                "V3",
                result="FAIL",
                failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
            )
        )
        result = run(scenario, reports)
        self.assertEqual("OPEN_BUNDLE_REVISION", result.next_action)
        self.assertNotEqual("ACCEPTED", result.acceptance_state)

    def test_a_failure_on_a_gate_this_tier_did_not_require_still_blocks(self):
        # B-1 blocking finding 3: an admissible FAIL on a non-required gate was
        # discarded and the run was ACCEPTED.
        scenario = replace(
            self.scenario,
            task=replace(self.scenario.task, declared_risk="low"),
            execution=replace(self.scenario.execution, diff_paths=("README.md",)),
        )
        reports = [
            scenario.report("V0"),
            scenario.report(
                "V3",
                result="FAIL",
                failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
            ),
        ]
        result = run(scenario, reports)
        self.assertEqual(("V0",), result.required_gates)
        self.assertEqual(("V0",), result.satisfied_gates)
        self.assertEqual((), result.missing_required_gates)
        # Coverage is complete for this tier, so a naive evaluator accepts.
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual("REPAIR", result.next_action)
        # The attested baseline shows this signature passing at base_sha, so
        # the failure is admitted as a proven regression rather than a
        # pre-existing contract violation.
        self.assertIn("REGRESSION", result.admitted_failure_classes)


# ---------------------------------------------------------------------------
# Fixture 2: FX-OB-MOBILE-OVERFLOW
# Desktop DOM is green; the mobile viewport was never measured.
# ---------------------------------------------------------------------------


class MobileOverflowFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_ob_mobile_overflow()
        self.scenario = self.fx["scenario"]

    def test_a_pass_declaring_partial_coverage_is_unknown_not_a_pass(self):
        reports = [
            self.scenario.report("V0"),
            self.scenario.report("V1"),
            self.scenario.report("V2", coverage_dimensions=("viewport:desktop-1440",)),
        ]
        result = run(self.scenario, reports)
        self.assertIn("UNKNOWN", result.admitted_failure_classes)
        self.assertEqual("ESCALATE_HUMAN", result.next_action)
        self.assertNotIn("V2", result.satisfied_gates)

    def test_partial_coverage_escalates_rather_than_looking_like_progress(self):
        # A claimed-complete gate on partial evidence is a live false-complete
        # signal, so it must not fall back to CONTINUE_VERIFICATION.
        reports = [
            self.scenario.report("V0"),
            self.scenario.report("V1"),
            self.scenario.report("V2", coverage_dimensions=()),
        ]
        self.assertEqual("ESCALATE_HUMAN", run(self.scenario, reports).next_action)

    def test_full_coverage_overflow_defect_is_repairable(self):
        reports = [
            self.scenario.report("V0"),
            self.scenario.report("V1"),
            self.scenario.report(
                "V2",
                result="FAIL",
                failure_observations=(self.fx["overflow_failure"],),
            ),
        ]
        result = run(self.scenario, reports)
        self.assertEqual("REPAIR", result.next_action)

    def test_full_coverage_genuine_pass_is_accepted(self):
        result = run(self.scenario, [self.scenario.report(g) for g in OB_GATES])
        self.assertEqual("ACCEPTED", result.acceptance_state)


# ---------------------------------------------------------------------------
# Fixture 3: FX-ADM-FALSE-DISPATCH
# HTTP 200, a listening port and a generated prompt file are not evidence.
# ---------------------------------------------------------------------------


class FalseDispatchFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_adm_false_dispatch()
        self.scenario = self.fx["ordinary"]

    def test_executor_self_report_cannot_satisfy_a_gate(self):
        reports = [
            self.scenario.report("V0"),
            self.scenario.report(
                "V1", evidence_source=self.fx["self_reported_evidence_source"]
            ),
        ]
        result = run(self.scenario, reports)
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertIn(
            "UNTRUSTED_EVIDENCE_SOURCE",
            [reason for _key, reason in result.invalidated_report_reasons],
        )

    def test_unresolved_session_correlation_is_unknown_never_transient(self):
        # The known Session Center defect is absorbed as UNKNOWN, which wakes a
        # human, rather than as noise that would be retried forever.
        unresolved = replace(
            self.scenario.checker, status="needs_review",
            method="conflicting_deterministic_signals", confidence=None,
        )
        reports = [
            self.scenario.report("V0"),
            self.scenario.report("V1", producer_identity=unresolved),
        ]
        result = run(self.scenario, reports)
        self.assertNotIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)
        self.assertNotEqual("ACCEPTED", result.acceptance_state)

    def test_a_production_diff_routes_to_pfp_even_with_no_reports_at_all(self):
        result = run(self.fx["production"], [])
        self.assertEqual("ROUTE_TO_PFP", result.next_action)
        self.assertEqual("DEFERRED_TO_PFP", result.acceptance_state)


# ---------------------------------------------------------------------------
# Guard order, exercised directly on _derive_next_action so each branch is
# provable independently of how an admitted class arrived there.
# ---------------------------------------------------------------------------


class NextActionGuardOrderTests(unittest.TestCase):
    def action(self, admitted, **kw):
        params = dict(
            round_invalidated=False,
            prior_status="NONE",
            missing_required=(),
            satisfied=("V0",),
            required=("V0",),
        )
        params.update(kw)
        return _derive_next_action(set(admitted), **params)

    def test_governance_conflict_outranks_everything(self):
        self.assertEqual(
            "ESCALATE_HUMAN",
            self.action({"GOVERNANCE_CONFLICT", "PRODUCTION_SCOPE_VIOLATION"}),
        )

    def test_production_scope_violation_is_never_masked_by_another_class(self):
        for companion in ("CONTRACT_VIOLATION", "REGRESSION", "UNKNOWN", "TEST_DEFECT"):
            self.assertEqual(
                "ROUTE_TO_PFP", self.action({"PRODUCTION_SCOPE_VIOLATION", companion}), companion
            )

    def test_round_invalidation_outranks_ordinary_defects(self):
        self.assertEqual(
            "INVALIDATE_ROUND", self.action({"CONTRACT_VIOLATION"}, round_invalidated=True)
        )

    def test_production_routing_outranks_round_invalidation(self):
        self.assertEqual(
            "ROUTE_TO_PFP",
            self.action({"PRODUCTION_SCOPE_VIOLATION"}, round_invalidated=True),
        )

    def test_contract_defect_outranks_unknown(self):
        # Phase A v3 D.2 puts G3 (C2) before G4 (C7); B-1 had it the other way.
        self.assertEqual(
            "OPEN_BUNDLE_REVISION", self.action({"ACCEPTANCE_CONTRACT_DEFECT", "UNKNOWN"})
        )

    def test_unknown_outranks_repair(self):
        # A repair changes candidate_sha, and an UNKNOWN bound to the old SHA
        # would evaporate with it -- so a trivial manufactured regression could
        # be used to make an inconvenient UNKNOWN disappear.
        self.assertEqual("ESCALATE_HUMAN", self.action({"UNKNOWN", "REGRESSION"}))

    def test_regression_and_contract_violation_repair_the_code(self):
        self.assertEqual("REPAIR", self.action({"REGRESSION"}))
        self.assertEqual("REPAIR", self.action({"CONTRACT_VIOLATION"}))

    def test_test_defect_repairs_tests_only(self):
        self.assertEqual("REPAIR_TEST_ONLY", self.action({"TEST_DEFECT"}))

    def test_a_real_defect_alongside_test_defect_repairs_the_code_first(self):
        self.assertEqual("REPAIR", self.action({"TEST_DEFECT", "CONTRACT_VIOLATION"}))

    def test_revoked_prior_acceptance_escalates(self):
        self.assertEqual("ESCALATE_HUMAN", self.action(set(), prior_status="REVOKED"))

    def test_missing_coverage_continues_rather_than_accepting(self):
        self.assertEqual(
            "CONTINUE_VERIFICATION",
            self.action(set(), missing_required=("V1",), satisfied=(), required=("V0", "V1")),
        )

    def test_full_coverage_with_nothing_admitted_accepts(self):
        self.assertEqual("ACCEPTED", self.action(set()))

    def test_a_tier_declaring_no_required_gates_fails_closed(self):
        # "Nothing required" must not read as "nothing to prove".
        self.assertEqual("ESCALATE_HUMAN", self.action(set(), satisfied=(), required=()))


class RetryEligiblePredicateTests(unittest.TestCase):
    def test_exactly_transient_is_eligible(self):
        self.assertTrue(_retry_eligible({"ENVIRONMENT_TRANSIENT"}))

    def test_membership_is_not_enough(self):
        # Set equality, not `in`: this is the single line that stops a
        # regression being retried away alongside a genuine transient.
        self.assertFalse(_retry_eligible({"ENVIRONMENT_TRANSIENT", "REGRESSION"}))
        self.assertFalse(_retry_eligible({"ENVIRONMENT_TRANSIENT", "UNKNOWN"}))

    def test_empty_or_other_classes_are_not_eligible(self):
        self.assertFalse(_retry_eligible(set()))
        self.assertFalse(_retry_eligible({"CONTRACT_VIOLATION"}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
