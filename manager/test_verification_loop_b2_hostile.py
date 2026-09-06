"""Phase B-2 hostile scenarios: every way an executor might forge an ACCEPTED.

Each test starts from a control that genuinely reaches ACCEPTED and changes
**one** thing. That is the difference between a test that proves a mechanism
and a test that merely passes: the Phase B-1 review found attack test 05 was
vacuous precisely because its report differed from the execution in two ways at
once, so mutating away the SHA check left the test green -- the identity check
was silently doing the work.

``assert_control_accepts`` runs in setUp for the shared scenarios, so if a
future change makes the happy path unreachable, these tests fail loudly instead
of passing vacuously for the wrong reason. Requirement W is explicit that
proving fail-closed behaviour is only half the job: the loop has to still be
able to say yes.
"""

import unittest
from dataclasses import replace

from manager.verification_loop.aggregator import task_close_eligibility
from manager.verification_loop.bundle import finalize_bundle
from manager.verification_loop.evaluator import evaluate
from manager.verification_loop.fixtures import (
    BASE_SHA,
    CANDIDATE_SHA,
    GOVERNANCE_DIGEST,
    fx_adm_false_dispatch,
    fx_ledger_freeze_pane,
    fx_ob_mobile_overflow,
)
from manager.verification_loop.identity import Identity, ResolvedIdentity
from manager.verification_loop.models import (
    Budget,
    ContradictionProof,
    EnvironmentFingerprint,
    ExecutionFixture,
    FailureObservation,
    PriorAcceptance,
)

BACKSLASH = chr(92)
LEDGER_GATES = ("V0", "V1", "V2", "V3")
ADM_GATES = ("V0", "V1")


def rebundle(scenario, **overrides):
    """A scenario with a re-finalized bundle, so its hash stays self-consistent.

    Using ``dataclasses.replace`` on a bundle directly would leave
    ``bundle_hash`` describing the old content -- which the evaluator correctly
    treats as a contract defect, and which would make an unrelated test fail
    for the wrong reason.
    """
    fields = {
        name: getattr(scenario.bundle, name)
        for name in scenario.bundle.__dataclass_fields__
        if name != "bundle_hash"
    }
    fields.update(overrides)
    return replace(scenario, bundle=finalize_bundle(**fields))


def run(scenario, reports, tickets=None, **kwargs):
    return evaluate(
        scenario.task,
        scenario.execution,
        scenario.bundle,
        reports,
        preflight=scenario.preflight,
        tickets=tickets if tickets is not None else scenario.tickets_for(
            *[(r.gate_id, r.round) for r in reports]
        ),
        **kwargs,
    )


class HostileTestCase(unittest.TestCase):
    """Shared control: a full, honest, ACCEPTED verification round."""

    gates = LEDGER_GATES

    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.fx = fx
        self.scenario = fx["scenario"](fx["bundle_with_clause"])
        self.assert_control_accepts()

    def control_reports(self, scenario=None):
        scenario = scenario or self.scenario
        return [scenario.report(gate) for gate in self.gates]

    def assert_control_accepts(self):
        result = run(self.scenario, self.control_reports())
        self.assertEqual("ACCEPTED", result.acceptance_state, result.admitted_failure_classes)

    def assert_not_accepted(self, result, *, expect_reason=None, expect_action=None):
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertNotEqual("ACCEPTED", result.next_action)
        if expect_reason is not None:
            reasons = [reason for _key, reason in result.invalidated_report_reasons]
            self.assertIn(expect_reason, reasons, result.invalidated_report_reasons)
        if expect_action is not None:
            self.assertEqual(expect_action, result.next_action)


# ---------------------------------------------------------------------------
# 1-3: identity
# ---------------------------------------------------------------------------


class IdentityAttackTests(HostileTestCase):
    def test_01_account_swap_is_rejected(self):
        impostor = ResolvedIdentity(
            Identity("claude", "acct-OTHER", "sess-check-1"),
            "classified", "high", "deterministic_signal",
        )
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", producer_identity=impostor)
        self.assert_not_accepted(
            run(self.scenario, reports), expect_reason="CHECKER_NOT_TICKET_EXPECTED_IDENTITY"
        )

    def test_02_session_swap_is_rejected(self):
        impostor = ResolvedIdentity(
            Identity("claude", "acct-checker", "sess-OTHER"),
            "classified", "high", "deterministic_signal",
        )
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", producer_identity=impostor)
        self.assert_not_accepted(
            run(self.scenario, reports), expect_reason="CHECKER_NOT_TICKET_EXPECTED_IDENTITY"
        )

    def test_03a_missing_checker_identity_is_rejected(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", producer_identity=None)
        self.assert_not_accepted(
            run(self.scenario, reports), expect_reason="CHECKER_IDENTITY_ABSENT"
        )

    def test_03b_missing_executor_identity_is_not_read_as_independence(self):
        scenario = replace(
            self.scenario,
            execution=replace(self.scenario.execution, executor_identity=None),
        )
        self.assert_not_accepted(
            run(scenario, self.control_reports(scenario)),
            expect_reason="EXECUTOR_IDENTITY_ABSENT",
        )

    def test_03c_unresolved_checker_identity_is_rejected(self):
        unresolved = ResolvedIdentity(
            Identity("claude", "acct-checker", "sess-check-1"),
            "needs_review", "high", "conflicting_deterministic_signals",
        )
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", producer_identity=unresolved)
        self.assert_not_accepted(
            run(self.scenario, reports), expect_reason="CHECKER_IDENTITY_UNCLASSIFIED"
        )

    def test_03d_executor_answering_its_own_gate_is_rejected(self):
        scenario = replace(self.scenario, checker=self.scenario.executor)
        self.assert_not_accepted(
            run(scenario, self.control_reports(scenario)),
            expect_reason="CHECKER_IS_FORBIDDEN_IDENTITY",
        )


# ---------------------------------------------------------------------------
# 4-11: lineage cross-binding and tickets
# ---------------------------------------------------------------------------


class LineageAttackTests(HostileTestCase):
    def test_04_task_execution_mismatch_blocks_acceptance(self):
        scenario = replace(
            self.scenario, execution=replace(self.scenario.execution, task_id="T-SOMEONE-ELSE")
        )
        result = run(scenario, self.control_reports(scenario))
        self.assert_not_accepted(result, expect_action="ESCALATE_HUMAN")
        self.assertIn("UNKNOWN", result.admitted_failure_classes)

    def test_04b_execution_pointing_at_another_bundle_blocks_acceptance(self):
        scenario = replace(
            self.scenario,
            task=replace(self.scenario.task, acceptance_bundle_ref="ab-other"),
            execution=replace(self.scenario.execution, acceptance_bundle_ref="ab-other"),
        )
        result = run(scenario, self.control_reports(scenario))
        self.assert_not_accepted(result)
        self.assertIn("UNKNOWN", result.admitted_failure_classes)

    def test_05a_relabelling_a_report_onto_another_candidate_loses_its_ticket(self):
        # ticket_id is derived from candidate_sha, so simply relabelling the
        # report orphans it. This proves the derivation coupling, *not* the
        # candidate binding -- see test_05b for that.
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", candidate_sha="c" * 40)
        self.assert_not_accepted(
            run(self.scenario, reports, tickets=self.scenario.tickets_for(*[(g, 1) for g in self.gates])),
            expect_reason="NO_MATCHING_TICKET",
        )

    def test_05b_candidate_sha_binding_is_single_factor(self):
        # Everything else lines up: the ticket is self-consistent for the other
        # candidate, and the worktree heads match the execution, so no other
        # check can fire. Mutation testing found the naive version of this test
        # passing for the wrong reason -- the head check was catching it, and
        # deleting the candidate binding left the suite green.
        other = "c" * 40
        reports = self.control_reports()
        reports[3] = self.scenario.report(
            "V3",
            candidate_sha=other,
            worktree_head_before=self.scenario.execution.candidate_sha,
            worktree_head_after=self.scenario.execution.candidate_sha,
        )
        tickets = list(self.scenario.tickets_for(("V0", 1), ("V1", 1), ("V2", 1)))
        tickets.append(
            self.scenario.ticket(
                "V3",
                candidate_sha=other,
                candidate_head_at_issue=self.scenario.execution.candidate_sha,
            )
        )
        self.assert_not_accepted(
            run(self.scenario, reports, tickets=tuple(tickets)),
            expect_reason="CANDIDATE_SHA_MISMATCH",
        )

    def test_06_deliverable_sha_mismatch_is_inadmissible(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", deliverable_sha="d" * 40)
        self.assert_not_accepted(
            run(self.scenario, reports), expect_reason="DELIVERABLE_SHA_MISMATCH"
        )

    def test_07a_a_report_bound_to_another_contract_loses_its_ticket(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", bundle_hash="ab-someone-elses-contract")
        self.assert_not_accepted(
            run(self.scenario, reports, tickets=self.scenario.tickets_for(*[(g, 1) for g in self.gates])),
            expect_reason="NO_MATCHING_TICKET",
        )

    def test_07b_bundle_hash_binding_is_single_factor(self):
        # Models an attacker who controls both the ticket and the report but
        # not the frozen bundle: ticket and report agree with each other on a
        # contract that is not the one in force, so only the bundle binding
        # itself can catch it. Mutation testing found the naive version of this
        # test passing on the ticket lookup instead.
        forged = "ab-someone-elses-contract"
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", bundle_hash=forged)
        tickets = list(self.scenario.tickets_for(("V0", 1), ("V1", 1), ("V2", 1)))
        tickets.append(self.scenario.ticket("V3", bundle_hash=forged))
        self.assert_not_accepted(
            run(self.scenario, reports, tickets=tuple(tickets)),
            expect_reason="BUNDLE_HASH_MISMATCH",
        )

    def test_08_checker_version_mismatch_is_inadmissible(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", checker_version="9.9.9")
        self.assert_not_accepted(
            run(self.scenario, reports), expect_reason="CHECKER_VERSION_MISMATCH"
        )

    def test_09_checker_impl_digest_mismatch_is_inadmissible(self):
        # The dangerous one: the version string still looks right in every log.
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", checker_impl_digest="impl-swapped")
        self.assert_not_accepted(
            run(self.scenario, reports), expect_reason="CHECKER_IMPL_DIGEST_MISMATCH"
        )

    def test_10a_report_without_a_ticket_does_not_exist(self):
        reports = self.control_reports()
        self.assert_not_accepted(
            run(self.scenario, reports, tickets=self.scenario.tickets_for(("V0", 1), ("V1", 1), ("V2", 1))),
            expect_reason="NO_MATCHING_TICKET",
        )

    def test_10b_forged_ticket_id_on_the_report_finds_no_ticket(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", ticket_id="vt-forged")
        self.assert_not_accepted(
            run(self.scenario, reports, tickets=self.scenario.tickets_for(*[(g, 1) for g in self.gates])),
            expect_reason="NO_MATCHING_TICKET",
        )

    def test_10c_a_ticket_expecting_a_different_round_is_not_matched(self):
        reports = self.control_reports()
        tickets = list(self.scenario.tickets_for(("V0", 1), ("V1", 1), ("V2", 1)))
        tickets.append(self.scenario.ticket("V3", 2))
        self.assert_not_accepted(
            run(self.scenario, reports, tickets=tuple(tickets)),
            expect_reason="NO_MATCHING_TICKET",
        )

    def test_11a_two_reports_answering_one_ticket_are_both_rejected(self):
        reports = self.control_reports()
        reports.append(self.scenario.report("V3", result="FAIL"))
        result = run(self.scenario, reports, tickets=self.scenario.tickets_for(*[(g, 1) for g in self.gates]))
        self.assert_not_accepted(result, expect_reason="DUPLICATE_REPORT_FOR_TICKET")
        self.assertIn("V3", result.missing_required_gates)

    def test_11b_duplicate_reports_are_order_invariant(self):
        # The B-1 defect: [PASS, FAIL] ACCEPTED and [FAIL, PASS] REPAIRed.
        passing = self.scenario.report("V3")
        failing = self.scenario.report("V3", result="FAIL")
        tickets = self.scenario.tickets_for(*[(g, 1) for g in self.gates])
        head = [self.scenario.report(g) for g in ("V0", "V1", "V2")]
        forward = run(self.scenario, head + [passing, failing], tickets=tickets)
        reverse = run(self.scenario, head + [failing, passing], tickets=tickets)
        self.assertEqual(forward.next_action, reverse.next_action)
        self.assertEqual(forward.acceptance_state, reverse.acceptance_state)
        self.assertEqual(forward.admitted_failure_classes, reverse.admitted_failure_classes)
        self.assertEqual(forward.missing_required_gates, reverse.missing_required_gates)
        self.assertNotEqual("ACCEPTED", forward.acceptance_state)

    def test_11c_duplicate_tickets_for_one_round_drop_both(self):
        tickets = list(self.scenario.tickets_for(*[(g, 1) for g in self.gates]))
        tickets.append(replace(tickets[3], worktree_generation=2))
        self.assert_not_accepted(
            run(self.scenario, self.control_reports(), tickets=tuple(tickets)),
            expect_reason="NO_MATCHING_TICKET",
        )


# ---------------------------------------------------------------------------
# 12: immutable candidate round
# ---------------------------------------------------------------------------


class ImmutableRoundTests(HostileTestCase):
    def test_12a_head_moving_during_the_round_invalidates_it(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", worktree_head_after="f" * 40)
        result = run(self.scenario, reports)
        self.assertEqual("INVALIDATE_ROUND", result.next_action)
        self.assertEqual("ROUND_INVALIDATED", result.acceptance_state)

    def test_12b_a_round_measured_against_another_tree_is_invalid(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report(
            "V3", worktree_head_before="f" * 40, worktree_head_after="f" * 40
        )
        self.assertEqual("ROUND_INVALIDATED", run(self.scenario, reports).acceptance_state)

    def test_12c_a_ticket_issued_against_a_different_head_invalidates_the_round(self):
        tickets = list(self.scenario.tickets_for(*[(g, 1) for g in self.gates]))
        tickets[3] = self.scenario.ticket("V3", candidate_head_at_issue="f" * 40)
        result = run(self.scenario, self.control_reports(), tickets=tuple(tickets))
        self.assertEqual("ROUND_INVALIDATED", result.acceptance_state)

    def test_12d_round_invalidation_never_reads_as_a_pass_or_a_fail(self):
        reports = self.control_reports()
        reports[3] = self.scenario.report("V3", worktree_head_after="f" * 40)
        result = run(self.scenario, reports)
        self.assertNotIn("V3", result.satisfied_gates)
        self.assertNotIn("CONTRACT_VIOLATION", result.admitted_failure_classes)


# ---------------------------------------------------------------------------
# 13-15: baseline-aware regression
# ---------------------------------------------------------------------------


class BaselineRegressionTests(HostileTestCase):
    def _v3_failure(self, **kw):
        reports = self.control_reports()
        reports[3] = self.scenario.report(
            "V3",
            result="FAIL",
            failure_observations=(FailureObservation(signature="freeze_pane_mismatch", **kw),),
        )
        return run(self.scenario, reports)

    def test_13_base_pass_candidate_fail_is_a_regression(self):
        result = self._v3_failure(base_result="PASS")
        self.assertIn("REGRESSION", result.admitted_failure_classes)
        self.assertIn(("freeze_pane_mismatch", "REGRESSION"), result.regression_determinations)
        self.assertEqual("REPAIR", result.next_action)

    def test_14_base_fail_candidate_fail_is_not_a_regression(self):
        result = self._v3_failure(base_result="FAIL", base_signature="freeze_pane_mismatch")
        self.assertNotIn("REGRESSION", result.admitted_failure_classes)
        self.assertIn("CONTRACT_VIOLATION", result.admitted_failure_classes)
        self.assertIn(("freeze_pane_mismatch", "NOT_REGRESSION"), result.regression_determinations)

    def test_14b_a_known_baseline_failure_is_not_a_regression(self):
        scenario = rebundle(
            self.scenario,
            baseline=replace(
                self.scenario.bundle.baseline,
                known_baseline_failures=("freeze_pane_mismatch",),
            ),
        )
        reports = [scenario.report(g) for g in self.gates]
        reports[3] = scenario.report(
            "V3",
            result="FAIL",
            failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
        )
        result = run(scenario, reports)
        self.assertNotIn("REGRESSION", result.admitted_failure_classes)

    def test_15a_environment_mismatch_makes_the_comparison_unknown(self):
        # Not "no regression" and not "regression" -- we genuinely cannot tell.
        other_env = EnvironmentFingerprint("win32", "3.14.7", 999, "ephemeral")
        scenario = rebundle(
            self.scenario,
            baseline=replace(self.scenario.bundle.baseline, environment_fingerprint=other_env),
        )
        reports = [scenario.report(g) for g in self.gates]
        reports[3] = scenario.report(
            "V3",
            result="FAIL",
            failure_observations=(
                FailureObservation(signature="freeze_pane_mismatch", base_result="PASS"),
            ),
        )
        result = run(scenario, reports)
        self.assertIn(("freeze_pane_mismatch", "UNKNOWN"), result.regression_determinations)
        self.assertNotIn("REGRESSION", result.admitted_failure_classes)

    def test_15b_a_baseline_for_a_different_base_sha_is_not_usable(self):
        scenario = rebundle(
            self.scenario,
            baseline=replace(self.scenario.bundle.baseline, base_sha="9" * 40),
        )
        reports = [scenario.report(g) for g in self.gates]
        reports[3] = scenario.report(
            "V3",
            result="FAIL",
            failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
        )
        result = run(scenario, reports)
        self.assertIn(("freeze_pane_mismatch", "UNKNOWN"), result.regression_determinations)

    def test_15c_a_report_cannot_declare_itself_a_regression(self):
        # The field no longer exists; this pins that it cannot come back by
        # way of proposed_class either.
        result = self._v3_failure(base_result="FAIL", base_signature="freeze_pane_mismatch",
                                  proposed_class="REGRESSION")
        self.assertNotIn("REGRESSION", result.admitted_failure_classes)


# ---------------------------------------------------------------------------
# 16-19: TEST_DEFECT admission
# ---------------------------------------------------------------------------


class TestDefectAdmissionTests(HostileTestCase):
    def _fail(self, observation, scenario=None):
        scenario = scenario or self.scenario
        reports = [scenario.report(g) for g in self.gates]
        reports[3] = scenario.report(
            "V3", result="FAIL", failure_observations=(observation,)
        )
        return run(scenario, reports)

    def test_16_a_bare_test_defect_claim_is_not_admitted(self):
        result = self._fail(
            FailureObservation(signature="flaky_thing", proposed_class="TEST_DEFECT")
        )
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)
        self.assertNotEqual("REPAIR_TEST_ONLY", result.next_action)

    def test_17_td1_base_also_fails_with_the_same_signature(self):
        result = self._fail(
            FailureObservation(
                signature="flaky_thing", base_result="FAIL", base_signature="flaky_thing"
            )
        )
        self.assertIn("TEST_DEFECT", result.admitted_failure_classes)
        self.assertEqual("REPAIR_TEST_ONLY", result.next_action)

    def test_17b_td1_needs_the_same_signature_not_merely_a_base_failure(self):
        result = self._fail(
            FailureObservation(
                signature="flaky_thing", base_result="FAIL", base_signature="something_else"
            )
        )
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)

    def test_18_td2_contract_contradiction_proof(self):
        result = self._fail(
            FailureObservation(
                signature="asserts_the_opposite",
                contradiction_proof=ContradictionProof(
                    test_id="test_freeze",
                    asserted_predicate="freeze_panes == A1",
                    clause_id="freeze_pane_mismatch",
                    witness="clause requires A4",
                ),
            )
        )
        self.assertIn("TEST_DEFECT", result.admitted_failure_classes)

    def test_18b_an_incomplete_proof_is_an_assertion_not_a_proof(self):
        result = self._fail(
            FailureObservation(
                signature="asserts_the_opposite",
                contradiction_proof=ContradictionProof(
                    test_id="test_freeze",
                    asserted_predicate="freeze_panes == A1",
                    clause_id="freeze_pane_mismatch",
                    witness="   ",
                ),
            )
        )
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)

    def test_18c_a_proof_against_an_invented_clause_proves_nothing(self):
        result = self._fail(
            FailureObservation(
                signature="asserts_the_opposite",
                contradiction_proof=ContradictionProof(
                    test_id="t", asserted_predicate="p", clause_id="NO-SUCH-CLAUSE", witness="w"
                ),
            )
        )
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)

    def test_19_a_repair_touching_a_frozen_oracle_is_forcibly_reclassified(self):
        result = self._fail(
            FailureObservation(
                signature="flaky_thing",
                base_result="FAIL",
                base_signature="flaky_thing",
                repair_paths=("tests/golden/workbook-descriptor.json",),
            )
        )
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)
        self.assertIn("ACCEPTANCE_CONTRACT_DEFECT", result.admitted_failure_classes)
        self.assertEqual("OPEN_BUNDLE_REVISION", result.next_action)

    def test_19b_frozen_oracle_paths_cannot_be_dodged_by_respelling(self):
        result = self._fail(
            FailureObservation(
                signature="flaky_thing",
                base_result="FAIL",
                base_signature="flaky_thing",
                repair_paths=("./tests/golden" + BACKSLASH + "workbook-descriptor.json",),
            )
        )
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)

    def test_19c_an_unreadable_repair_path_cannot_be_proven_safe(self):
        result = self._fail(
            FailureObservation(
                signature="flaky_thing",
                base_result="FAIL",
                base_signature="flaky_thing",
                repair_paths=("../outside/whatever.json",),
            )
        )
        self.assertNotIn("TEST_DEFECT", result.admitted_failure_classes)


# ---------------------------------------------------------------------------
# 20-24: transient admission and budgets
# ---------------------------------------------------------------------------


class TransientAndBudgetTests(unittest.TestCase):
    gates = ADM_GATES

    def setUp(self):
        self.fx = fx_adm_false_dispatch()
        self.scenario = self.fx["ordinary"]
        control = run(self.scenario, [self.scenario.report(g) for g in self.gates])
        self.assertEqual("ACCEPTED", control.acceptance_state, control.admitted_failure_classes)

    def _with_v1_failures(self, *observations, scenario=None):
        scenario = scenario or self.scenario
        return [
            scenario.report("V0"),
            scenario.report("V1", result="FAIL", failure_observations=observations),
        ]

    def test_20a_an_allowlisted_checker_issued_code_retries(self):
        result = run(self.scenario, self._with_v1_failures(
            FailureObservation(signature="io blew up", transient_code="IO_TIMEOUT")
        ))
        self.assertEqual(("ENVIRONMENT_TRANSIENT",), result.admitted_failure_classes)
        self.assertEqual("RETRY_SAME_CANDIDATE", result.next_action)

    def test_20b_a_code_outside_the_allowlist_does_not_retry(self):
        result = run(self.scenario, self._with_v1_failures(
            FailureObservation(signature="io blew up", transient_code="NOT_ALLOWLISTED")
        ))
        self.assertNotIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)
        self.assertNotEqual("RETRY_SAME_CANDIDATE", result.next_action)

    def test_20c_a_signature_that_looks_transient_is_not_a_code(self):
        # Phase B-1R matched a *signature* against a bundle list. Text that
        # happens to contain the right words is not a decision by the checker.
        result = run(self.scenario, self._with_v1_failures(
            FailureObservation(signature="IO_TIMEOUT")
        ))
        self.assertNotIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)

    def test_20d_a_non_retryable_gate_cannot_retry(self):
        scenario = rebundle(
            self.scenario,
            checkers={
                "V0": self.scenario.bundle.checkers["V0"],
                "V1": replace(self.scenario.bundle.checkers["V1"], retryable=False),
            },
        )
        result = run(scenario, self._with_v1_failures(
            FailureObservation(signature="io blew up", transient_code="IO_TIMEOUT"),
            scenario=scenario,
        ))
        self.assertNotIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)

    def test_21_a_transient_alongside_a_real_defect_never_retries(self):
        # The B-1R residual: the transient allowlist was consulted first, so a
        # signature in both lists produced a retry.
        scenario = rebundle(self.scenario, gate_contract_clauses={"V1": ("real_defect",)})
        result = run(scenario, self._with_v1_failures(
            FailureObservation(signature="io blew up", transient_code="IO_TIMEOUT"),
            FailureObservation(signature="real_defect"),
            scenario=scenario,
        ))
        self.assertNotEqual("RETRY_SAME_CANDIDATE", result.next_action)
        self.assertIn("REGRESSION", result.admitted_failure_classes)

    def test_21b_one_signature_that_is_both_clause_and_transient_is_a_defect(self):
        scenario = rebundle(self.scenario, gate_contract_clauses={"V1": ("both_ways",)})
        result = run(scenario, self._with_v1_failures(
            FailureObservation(signature="both_ways", transient_code="IO_TIMEOUT"),
            scenario=scenario,
        ))
        self.assertNotEqual("RETRY_SAME_CANDIDATE", result.next_action)

    def test_22_transient_retry_budget_exhaustion_escalates(self):
        chain = (
            self.scenario.execution,
            replace(self.scenario.execution, execution_id="E-R1", retry_of_execution_id="E-ADM-01"),
            replace(self.scenario.execution, execution_id="E-R2", retry_of_execution_id="E-R1"),
        )
        result = run(
            self.scenario,
            self._with_v1_failures(
                FailureObservation(signature="io blew up", transient_code="IO_TIMEOUT")
            ),
            chain=chain,
        )
        self.assertEqual("ESCALATE_HUMAN", result.next_action)
        self.assertIn("transient_retries", result.budget_exhausted)

    def test_23_repair_budget_exhaustion_escalates(self):
        scenario = rebundle(self.scenario, gate_contract_clauses={"V1": ("real_defect",)})
        chain = tuple(
            [scenario.execution]
            + [
                replace(scenario.execution, execution_id=f"E-P{i}", repair_of_execution_id=f"E-P{i-1}")
                for i in range(1, 4)
            ]
        )
        result = run(
            scenario,
            self._with_v1_failures(FailureObservation(signature="real_defect"), scenario=scenario),
            chain=chain,
        )
        self.assertEqual("ESCALATE_HUMAN", result.next_action)
        self.assertIn("repair_executions", result.budget_exhausted)

    def test_24_review_round_budget_exhaustion_escalates(self):
        scenario = rebundle(
            self.scenario,
            gate_contract_clauses={"V1": ("real_defect",)},
            budget=Budget(max_review_rounds=1),
        )
        reports = [
            scenario.report("V0", 2),
            scenario.report(
                "V1", 2, result="FAIL",
                failure_observations=(FailureObservation(signature="real_defect"),),
            ),
        ]
        result = run(scenario, reports)
        self.assertEqual("ESCALATE_HUMAN", result.next_action)
        self.assertIn("review_rounds", result.budget_exhausted)

    def test_24b_budget_never_blocks_an_otherwise_clean_acceptance(self):
        chain = (
            self.scenario.execution,
            replace(self.scenario.execution, execution_id="E-R1", retry_of_execution_id="E-ADM-01"),
            replace(self.scenario.execution, execution_id="E-R2", retry_of_execution_id="E-R1"),
        )
        result = run(
            self.scenario, [self.scenario.report(g) for g in self.gates], chain=chain
        )
        self.assertEqual("ACCEPTED", result.acceptance_state)


# ---------------------------------------------------------------------------
# 25-27: risk monotonicity
# ---------------------------------------------------------------------------


class RiskEscalationTests(HostileTestCase):
    def test_25_declared_low_with_a_high_risk_diff_escalates(self):
        scenario = replace(self.scenario, task=replace(self.scenario.task, declared_risk="low"))
        result = run(scenario, [scenario.report("V0")])
        self.assertEqual("artifact_sensitive", result.effective_risk)
        self.assertEqual(("V0", "V1", "V2", "V3"), result.required_gates)
        self.assertNotEqual("ACCEPTED", result.acceptance_state)

    def test_26_a_high_declared_risk_cannot_be_downgraded_by_the_diff(self):
        scenario = replace(
            self.scenario,
            task=replace(self.scenario.task, declared_risk="artifact_sensitive"),
            execution=replace(self.scenario.execution, diff_paths=("README.md",)),
        )
        result = run(scenario, [scenario.report("V0"), scenario.report("V1")])
        self.assertEqual("artifact_sensitive", result.effective_risk)
        self.assertIn("V2", result.missing_required_gates)
        self.assertIn("V3", result.missing_required_gates)
        self.assertNotEqual("ACCEPTED", result.acceptance_state)

    def test_27_a_non_monotone_tier_map_is_a_contract_defect(self):
        scenario = rebundle(
            self.scenario,
            gate_requirements_by_risk={
                "low": ("V0",),
                "medium": ("V0", "V1", "V2"),
                "artifact_sensitive": ("V3",),
            },
        )
        result = run(scenario, [scenario.report(g) for g in self.gates])
        self.assertIn("ACCEPTANCE_CONTRACT_DEFECT", result.admitted_failure_classes)
        self.assertEqual("OPEN_BUNDLE_REVISION", result.next_action)

    def test_27b_escalating_risk_can_never_shrink_the_required_gate_set(self):
        scenario = rebundle(
            self.scenario,
            gate_requirements_by_risk={
                "low": ("V0",),
                "medium": ("V0", "V1", "V2"),
                "artifact_sensitive": ("V3",),
            },
        )
        result = run(scenario, [scenario.report(g) for g in self.gates])
        for gate in ("V0", "V1", "V2", "V3"):
            self.assertIn(gate, result.required_gates)

    def test_27c_a_bundle_whose_hash_does_not_match_its_content_fails_closed(self):
        scenario = replace(
            self.scenario, bundle=replace(self.scenario.bundle, bundle_version="9.9.9")
        )
        result = evaluate(
            scenario.task, scenario.execution, scenario.bundle,
            [scenario.report(g) for g in self.gates],
            preflight=scenario.preflight,
            tickets=scenario.tickets_for(*[(g, 1) for g in self.gates]),
        )
        self.assertIn("ACCEPTANCE_CONTRACT_DEFECT", result.admitted_failure_classes)


# ---------------------------------------------------------------------------
# 28-31: path spelling
# ---------------------------------------------------------------------------


class PathSpellingBypassTests(unittest.TestCase):
    """The production risk rule must not be evadable by respelling a path."""

    def setUp(self):
        self.fx = fx_adm_false_dispatch()
        self.base = self.fx["ordinary"]
        control = run(self.base, [self.base.report(g) for g in ADM_GATES])
        self.assertEqual("ACCEPTED", control.acceptance_state)

    def _routes_to_pfp(self, spelling):
        scenario = replace(
            self.base, execution=replace(self.base.execution, diff_paths=(spelling,))
        )
        return run(scenario, [scenario.report(g) for g in ADM_GATES])

    def test_28_dot_slash_prefix_does_not_evade_the_production_rule(self):
        result = self._routes_to_pfp("./manager/execution_runner.py")
        self.assertEqual("ROUTE_TO_PFP", result.next_action)
        self.assertEqual("DEFERRED_TO_PFP", result.acceptance_state)

    def test_29_backslashes_do_not_evade_the_production_rule(self):
        result = self._routes_to_pfp("manager" + BACKSLASH + "execution_runner.py")
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_29b_mixed_dot_and_backslash_does_not_evade_it(self):
        result = self._routes_to_pfp("." + BACKSLASH + "manager" + BACKSLASH + "execution_runner.py")
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_30_case_differences_do_not_evade_the_production_rule(self):
        result = self._routes_to_pfp("Manager/Execution_Runner.py")
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_30b_redundant_separators_do_not_evade_the_production_rule(self):
        result = self._routes_to_pfp("manager//./execution_runner.py")
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_30c_an_unreadable_diff_path_fails_closed_rather_than_matching_nothing(self):
        result = self._routes_to_pfp("../manager/execution_runner.py")
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertIn("UNKNOWN", result.admitted_failure_classes)

    def test_31_an_unmatched_path_forces_every_required_gate_to_rerun(self):
        fx = fx_ledger_freeze_pane()
        scenario = fx["scenario"](fx["bundle_with_clause"])
        scenario = replace(
            scenario,
            execution=replace(
                scenario.execution, diff_paths=("manager/artifact/w.py", "unknown/place.py")
            ),
        )
        result = run(scenario, [scenario.report(g) for g in LEDGER_GATES])
        self.assertEqual(result.required_gates, result.rerun_gates)


# ---------------------------------------------------------------------------
# 32-34: cross-SHA assembly, STALE, REVOKED
# ---------------------------------------------------------------------------


class EvidenceLifecycleTests(HostileTestCase):
    def test_32_gates_measured_at_different_shas_cannot_be_assembled(self):
        other_sha = "e" * 40
        reports = [self.scenario.report(g) for g in ("V0", "V1", "V2")]
        # V3 measured against a different candidate, everything else identical.
        reports.append(
            self.scenario.report(
                "V3",
                candidate_sha=other_sha,
                worktree_head_before=other_sha,
                worktree_head_after=other_sha,
            )
        )
        tickets = list(self.scenario.tickets_for(("V0", 1), ("V1", 1), ("V2", 1)))
        tickets.append(
            self.scenario.ticket("V3", candidate_sha=other_sha, candidate_head_at_issue=other_sha)
        )
        result = run(self.scenario, reports, tickets=tuple(tickets))
        self.assert_not_accepted(result)
        self.assertIn("V3", result.missing_required_gates)

    def test_33_a_contract_revision_makes_a_prior_acceptance_stale(self):
        accepted = run(self.scenario, self.control_reports())
        prior = PriorAcceptance(
            derivation_key=accepted.derivation_key,
            bundle_hash="ab-an-older-contract",
            candidate_sha=self.scenario.execution.candidate_sha,
            governance_digest=self.scenario.bundle.governance_digest,
        )
        result = run(self.scenario, [self.scenario.report("V0")], prior_acceptance=prior)
        self.assertEqual("STALE", result.prior_acceptance_status)
        self.assertEqual("STALE", result.acceptance_state)

    def test_33b_stale_never_blocks_an_acceptance_the_new_inputs_actually_earn(self):
        accepted = run(self.scenario, self.control_reports())
        prior = PriorAcceptance(
            derivation_key="an-older-derivation",
            bundle_hash="ab-an-older-contract",
            candidate_sha=self.scenario.execution.candidate_sha,
            governance_digest=self.scenario.bundle.governance_digest,
        )
        result = run(self.scenario, self.control_reports(), prior_acceptance=prior)
        self.assertEqual("STALE", result.prior_acceptance_status)
        self.assertEqual("ACCEPTED", result.acceptance_state)
        self.assertNotEqual(accepted.derivation_key, prior.derivation_key)

    def test_34_contradictory_evidence_under_identical_inputs_revokes(self):
        accepted = run(self.scenario, self.control_reports())
        prior = PriorAcceptance(
            derivation_key=accepted.derivation_key,
            bundle_hash=self.scenario.bundle.bundle_hash,
            candidate_sha=self.scenario.execution.candidate_sha,
            governance_digest=self.scenario.bundle.governance_digest,
        )
        reports = self.control_reports()
        reports[3] = self.scenario.report(
            "V3",
            2,
            result="FAIL",
            failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
        )
        tickets = self.scenario.tickets_for(("V0", 1), ("V1", 1), ("V2", 1), ("V3", 2))
        result = run(self.scenario, reports, tickets=tickets, prior_acceptance=prior)
        self.assertEqual("REVOKED", result.prior_acceptance_status)
        self.assertEqual("REVOKED", result.acceptance_state)
        self.assertEqual("ESCALATE_HUMAN", result.next_action)

    def test_34b_an_unchanged_derivation_stays_current(self):
        accepted = run(self.scenario, self.control_reports())
        prior = PriorAcceptance(
            derivation_key=accepted.derivation_key,
            bundle_hash=self.scenario.bundle.bundle_hash,
            candidate_sha=self.scenario.execution.candidate_sha,
            governance_digest=self.scenario.bundle.governance_digest,
        )
        result = run(self.scenario, self.control_reports(), prior_acceptance=prior)
        self.assertEqual("CURRENT", result.prior_acceptance_status)
        self.assertEqual("ACCEPTED", result.acceptance_state)


# ---------------------------------------------------------------------------
# 35: Task close aggregation
# ---------------------------------------------------------------------------


class TaskCloseAggregationTests(HostileTestCase):
    def _accepted(self):
        return run(self.scenario, self.control_reports())

    def test_35a_an_accepted_execution_at_the_deliverable_sha_closes_the_task(self):
        result = self._accepted()
        decision = task_close_eligibility(
            self.scenario.task,
            [self.scenario.execution],
            {self.scenario.execution.execution_id: result},
            current_governance_digest=GOVERNANCE_DIGEST,
        )
        self.assertTrue(decision.close_eligible, decision.blocking_reasons)
        self.assertEqual(self.scenario.execution.execution_id, decision.accepted_execution_id)

    def test_35b_an_acceptance_at_another_sha_does_not_close_the_task(self):
        result = self._accepted()
        task = replace(self.scenario.task, deliverable_sha="9" * 40)
        decision = task_close_eligibility(
            task,
            [self.scenario.execution],
            {self.scenario.execution.execution_id: result},
            current_governance_digest=GOVERNANCE_DIGEST,
        )
        self.assertFalse(decision.close_eligible)
        self.assertIn("NO_ACCEPTED_EXECUTION_AT_DELIVERABLE_SHA", decision.blocking_reasons)

    def test_35c_a_task_without_a_deliverable_sha_cannot_close(self):
        result = self._accepted()
        task = replace(self.scenario.task, deliverable_sha=None)
        decision = task_close_eligibility(
            task,
            [self.scenario.execution],
            {self.scenario.execution.execution_id: result},
            current_governance_digest=GOVERNANCE_DIGEST,
        )
        self.assertFalse(decision.close_eligible)
        self.assertIn("NO_DELIVERABLE_SHA", decision.blocking_reasons)

    def test_35d_an_unresolved_blocker_or_pending_pfp_holds_the_task_open(self):
        result = self._accepted()
        for kwargs, expected in (
            ({"unresolved_blockers": ("P1-open",)}, "UNRESOLVED_BLOCKER:P1-open"),
            ({"pending_pfp": True}, "PENDING_PRODUCTION_FIX_PROTOCOL"),
        ):
            decision = task_close_eligibility(
                self.scenario.task,
                [self.scenario.execution],
                {self.scenario.execution.execution_id: result},
                current_governance_digest=GOVERNANCE_DIGEST,
                **kwargs,
            )
            self.assertFalse(decision.close_eligible)
            self.assertIn(expected, decision.blocking_reasons)

    def test_35e_stale_or_revoked_evidence_holds_the_task_open(self):
        accepted = self._accepted()
        for status in ("STALE", "REVOKED"):
            evaluation = replace(accepted, prior_acceptance_status=status)
            decision = task_close_eligibility(
                self.scenario.task,
                [self.scenario.execution],
                {self.scenario.execution.execution_id: evaluation},
                current_governance_digest=GOVERNANCE_DIGEST,
            )
            self.assertFalse(decision.close_eligible, status)
            self.assertTrue(
                any(r.startswith("EVIDENCE_" + status) for r in decision.blocking_reasons),
                decision.blocking_reasons,
            )

    def test_35f_a_task_cannot_close_on_an_execution_with_missing_gates(self):
        partial = run(self.scenario, [self.scenario.report(g) for g in ("V0", "V1")])
        decision = task_close_eligibility(
            self.scenario.task,
            [self.scenario.execution],
            {self.scenario.execution.execution_id: partial},
            current_governance_digest=GOVERNANCE_DIGEST,
        )
        self.assertFalse(decision.close_eligible)


# ---------------------------------------------------------------------------
# 36-38: production boundary, and the happy path
# ---------------------------------------------------------------------------


class ProductionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_adm_false_dispatch()

    def test_36a_a_production_diff_routes_to_pfp_with_every_report_admissible(self):
        # Single-factor by construction: the B-1R residual was that this
        # fixture's reports were execution_id-mismatched, so routing could have
        # been an artefact of lineage rejection rather than production risk.
        scenario = self.fx["production"]
        reports = [scenario.report(g) for g in ADM_GATES]
        result = run(scenario, reports)
        self.assertEqual("ROUTE_TO_PFP", result.next_action)
        self.assertEqual("DEFERRED_TO_PFP", result.acceptance_state)
        self.assertEqual((), result.invalidated_report_reasons)

    def test_36b_the_same_reports_on_a_non_production_diff_are_accepted(self):
        # The control that makes 36a single-factor.
        result = run(self.fx["ordinary"], [self.fx["ordinary"].report(g) for g in ADM_GATES])
        self.assertEqual("ACCEPTED", result.acceptance_state)

    def test_36c_a_marked_production_worktree_routes_to_pfp(self):
        scenario = self.fx["ordinary"]
        scenario = replace(
            scenario,
            preflight=replace(scenario.preflight, worktree_is_marked_production=True),
        )
        result = run(scenario, [scenario.report(g) for g in ADM_GATES])
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_36d_a_blacklisted_worktree_path_routes_to_pfp(self):
        scenario = rebundle(
            self.fx["ordinary"],
            production_canonical_checkout_paths=("/prod/adm-checkout",),
        )
        scenario = replace(
            scenario,
            preflight=replace(scenario.preflight, worktree_path="/prod/adm-checkout/nested"),
        )
        result = run(scenario, [scenario.report(g) for g in ADM_GATES])
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_37a_the_production_manager_home_routes_to_pfp(self):
        scenario = rebundle(
            self.fx["ordinary"], production_ai_manager_home="/prod/.ai-development-manager"
        )
        scenario = replace(
            scenario,
            preflight=replace(
                scenario.preflight, ai_manager_home="/prod/.ai-development-manager"
            ),
        )
        result = run(scenario, [scenario.report(g) for g in ADM_GATES])
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_37b_a_manager_home_classed_production_routes_to_pfp(self):
        scenario = self.fx["ordinary"]
        scenario = replace(
            scenario,
            preflight=replace(scenario.preflight, ai_manager_home_class="production"),
        )
        result = run(scenario, [scenario.report(g) for g in ADM_GATES])
        self.assertEqual("ROUTE_TO_PFP", result.next_action)

    def test_37c_a_production_write_gate_can_never_be_passed(self):
        scenario = rebundle(
            self.fx["ordinary"],
            checkers={
                "V0": self.fx["ordinary"].bundle.checkers["V0"],
                "V1": replace(
                    self.fx["ordinary"].bundle.checkers["V1"],
                    production_write=True,
                    environment="production",
                ),
            },
        )
        result = run(scenario, [scenario.report(g) for g in ADM_GATES])
        self.assertEqual("ROUTE_TO_PFP", result.next_action)
        self.assertEqual("DEFERRED_TO_PFP", result.acceptance_state)

    def test_37e_a_production_write_gate_this_tier_did_not_require_still_routes(self):
        # Found in self-review: the production-write check originally looked
        # only at the required set, so a production-write gate the current tier
        # happened not to require could be reported on without tripping the
        # boundary. Its PASS could not fill a required slot -- so this was not a
        # false-complete -- but a report about a production write is itself the
        # signal that this loop must not be the one deciding.
        scenario = rebundle(
            self.fx["ordinary"],
            gate_requirements_by_risk={"low": ("V0",), "medium": ("V0",)},
            checkers={
                "V0": self.fx["ordinary"].bundle.checkers["V0"],
                "V1": replace(
                    self.fx["ordinary"].bundle.checkers["V1"],
                    production_write=True,
                    environment="production",
                ),
            },
        )
        result = run(scenario, [scenario.report(g) for g in ADM_GATES])
        self.assertEqual(("V0",), result.required_gates)
        self.assertEqual("ROUTE_TO_PFP", result.next_action)
        self.assertEqual("DEFERRED_TO_PFP", result.acceptance_state)

    def test_37f_an_ordinary_gate_set_is_unaffected_by_that_widening(self):
        # Control for 37e: widening the check must not route every run to PFP.
        result = run(self.fx["ordinary"], [self.fx["ordinary"].report(g) for g in ADM_GATES])
        self.assertEqual("ACCEPTED", result.acceptance_state)

    def test_37d_a_checker_reporting_deferred_to_pfp_is_never_a_pass(self):
        scenario = self.fx["ordinary"]
        reports = [scenario.report("V0"), scenario.report("V1", result="DEFERRED_TO_PFP")]
        result = run(scenario, reports)
        self.assertEqual("ROUTE_TO_PFP", result.next_action)
        self.assertNotIn("V1", result.satisfied_gates)


class HappyPathTests(unittest.TestCase):
    """Requirement W: fail-closed must not have sealed the loop shut."""

    def test_38a_ledger_full_coverage_is_accepted(self):
        fx = fx_ledger_freeze_pane()
        scenario = fx["scenario"](fx["bundle_with_clause"])
        result = run(scenario, [scenario.report(g) for g in LEDGER_GATES])
        self.assertEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual("ACCEPTED", result.next_action)
        self.assertEqual((), result.admitted_failure_classes)
        self.assertEqual((), result.missing_required_gates)

    def test_38b_obsidian_full_viewport_coverage_is_accepted(self):
        fx = fx_ob_mobile_overflow()
        scenario = fx["scenario"]
        result = run(scenario, [scenario.report(g) for g in ("V0", "V1", "V2")])
        self.assertEqual("ACCEPTED", result.acceptance_state)

    def test_38c_adm_full_coverage_is_accepted(self):
        fx = fx_adm_false_dispatch()
        scenario = fx["ordinary"]
        result = run(scenario, [scenario.report(g) for g in ADM_GATES])
        self.assertEqual("ACCEPTED", result.acceptance_state)

    def test_38d_a_repaired_candidate_can_still_be_accepted(self):
        # A repair Execution is a *new* Execution; it must still be able to
        # finish, or bounded budgets would just be a slower way of failing.
        fx = fx_ledger_freeze_pane()
        scenario = fx["scenario"](fx["bundle_with_clause"])
        repaired = replace(
            scenario.execution, execution_id="E-LED-02", repair_of_execution_id="E-LED-01"
        )
        scenario = replace(scenario, execution=repaired)
        result = run(
            scenario,
            [scenario.report(g) for g in LEDGER_GATES],
            chain=(
                ExecutionFixture(
                    execution_id="E-LED-01",
                    task_id=scenario.task.task_id,
                    base_sha=BASE_SHA,
                    candidate_sha=CANDIDATE_SHA,
                    diff_paths=(),
                    status="completed",
                    acceptance_bundle_ref=scenario.bundle.bundle_id,
                ),
                repaired,
            ),
        )
        self.assertEqual("ACCEPTED", result.acceptance_state)

    def test_38e_proposed_class_is_read_nowhere(self):
        fx = fx_ledger_freeze_pane()
        scenario = fx["scenario"](fx["bundle_with_clause"])
        honest = [scenario.report(g) for g in LEDGER_GATES]
        misleading = list(honest)
        misleading[3] = scenario.report(
            "V3",
            result="FAIL",
            failure_observations=(
                FailureObservation(
                    signature="freeze_pane_mismatch", proposed_class="ENVIRONMENT_TRANSIENT"
                ),
            ),
        )
        result = run(scenario, misleading)
        self.assertNotIn("ENVIRONMENT_TRANSIENT", result.admitted_failure_classes)
        self.assertNotEqual("RETRY_SAME_CANDIDATE", result.next_action)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
