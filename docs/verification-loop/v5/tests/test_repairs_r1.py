"""Remediation Round 1 regressions — B1..B7 plus C7 and C10.

Each case is the independent reviewer's own reproduction, asserted in the
correct direction. `tests/prefix_repro_r1.py` holds the same scenarios asserted
in the broken direction, and is the evidence that these were live bypasses at
the repair base `c116854` rather than hypothetical ones.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fixtures import (
    candidate_env,
    close_ok,
    forged_env,
    honest_open,
    human_env,
    launcher_capture,
    launcher_env,
    mechanical_pass,
    policy,
    review_capture,
    review_ok,
)
from v5_kernel.harness import (
    HARNESS_REQUIRED_AGENTS,
    AgentResult,
    harness_gate,
    safety_row,
)
from v5_kernel.kernel import (
    Event,
    Issuer,
    Obligation,
    ObligationState,
    State,
    apply,
    controller_trust_digest,
    decide,
    open_task,
    retry_identity,
)


def human_record(world, subject, resolution="SATISFIED", lifetime=40, scope="obligation", **over):
    rec = {
        "record_id": over.pop("record_id", "human-%d" % world.tick),
        "subject": subject,
        "scope": scope,
        "issued_tick": world.tick,
        "expires_tick": world.tick + lifetime,
        "provenance_digest": "human-signed-1",
    }
    rec.update(over)
    return {"record": rec, "resolution": resolution}


def _force_obligation(world, oid, state, source="TRANSIENT"):
    world.obligations[oid] = Obligation(obligation_id=oid, required=True, state=state, source=source)


# ===========================================================================
# B1 — trusted-label forgery
# ===========================================================================


class B1TrustedLabelForgery(unittest.TestCase):
    def test_B1_R1_missing_envelope_fails_closed(self):
        """Omitting the attester must NOT default to LAUNCHER."""
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, {"result": "PASS"})  # no envelope
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(w.mechanical_replay_attester, Issuer.CANDIDATE_EXECUTOR.value)
        self.assertNotEqual(w.obligations["mechanical.tests"].state, ObligationState.SATISFIED)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B1_R2_payload_self_declared_launcher_is_ignored(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, {"result": "PASS", "attester": Issuer.LAUNCHER.value})
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertEqual(w.mechanical_replay_attester, Issuer.CANDIDATE_EXECUTOR.value)
        self.assertTrue(any("payload asserted attester" in n for n in w.notes), w.notes)

    def test_B1_R2b_unregistered_capture_id_is_not_a_launcher(self):
        """An envelope-shaped object whose capture id the launcher never
        registered is still the candidate."""
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), forged_env(Issuer.LAUNCHER.value))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertEqual(w.mechanical_replay_attester, Issuer.CANDIDATE_EXECUTOR.value)

    def test_B1_R3_real_launcher_envelope_still_accepts(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)


# ===========================================================================
# B2 — required obligation unavailable / empty oracle domain
# ===========================================================================


class B2RequiredObligations(unittest.TestCase):
    def _verified_world(self, **pol):
        w = open_task(policy(**pol), launcher_capture())
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        return w

    def test_B2_R1_required_unavailable_recoverable_is_not_accepted(self):
        w = self._verified_world(extra_required_obligations=["security.audit"])
        _force_obligation(w, "security.audit", ObligationState.UNAVAILABLE_RECOVERABLE)
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertTrue(any("security.audit" in b for b in d.blockers), d.blockers)

    def test_B2_R1b_no_non_satisfied_state_can_accept(self):
        for state in (
            ObligationState.PENDING,
            ObligationState.UNAVAILABLE_RECOVERABLE,
            ObligationState.UNAVAILABLE_HUMAN,
            ObligationState.INVALIDATED,
            ObligationState.ADVERSE,
        ):
            with self.subTest(state=state):
                w = self._verified_world(extra_required_obligations=["security.audit"])
                _force_obligation(w, "security.audit", state)
                d = decide(w)
                self.assertNotEqual(d.derived_status, State.ACCEPTED, "%s reached ACCEPTED" % state.value)

    def test_B2_R2_explicit_empty_oracle_domain_remains_empty(self):
        w = open_task(policy(oracle_expected=[]), launcher_capture(oracle_expected=[]))
        self.assertEqual(w.oracle_expected, frozenset())
        self.assertNotIn("oracle.unit", w.oracle_expected)
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, {"oracle_observed": [],
                                      "close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertTrue(any("empty oracle domain" in b for b in d.blockers), d.blockers)

    def test_B2_R3_missing_required_item_stays_pending(self):
        w = self._verified_world(extra_required_obligations=["security.audit"])
        self.assertEqual(w.obligations["security.audit"].state, ObligationState.PENDING)
        self.assertEqual(w.obligations["security.audit"].source, "POLICY_FLOOR")
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B2_R3b_floor_satisfaction_is_not_satisfaction(self):
        """A floor row flipped to SATISFIED without a real source stays rejected."""
        w = self._verified_world(extra_required_obligations=["security.audit"])
        _force_obligation(w, "security.audit", ObligationState.SATISFIED, source="POLICY_FLOOR")
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B2_R4_all_required_satisfied_accepts(self):
        w = self._verified_world(extra_required_obligations=["security.audit"])
        _force_obligation(w, "security.audit", ObligationState.SATISFIED, source="DERIVED")
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)


# ===========================================================================
# B3 — partial interval satisfaction
# ===========================================================================


class B3IntervalRecomputation(unittest.TestCase):
    def _staled(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        apply(w, Event.STALE_BINDING, {})
        return w

    def test_B3_R1_one_of_two_required_predicates_missing_is_not_accepted(self):
        w = self._staled()
        apply(w, Event.REDERIVE, {"close_predicates": {"mechanical.tests": "PASS", "oracle.set": "MISSING"}})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        d = decide(w)
        self.assertNotEqual(w.obligations["interval.binding"].state, ObligationState.SATISFIED)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B3_R2_old_satisfied_cannot_survive_a_later_missing_close(self):
        w = self._staled()
        apply(w, Event.REDERIVE, {"close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
        self.assertEqual(w.obligations["interval.binding"].state, ObligationState.SATISFIED)
        # A later, partial rederive must destroy the earlier PASS, not be OR-ed with it.
        apply(w, Event.REDERIVE, {"close_predicates": {"oracle.set": "MISSING"}})
        self.assertNotEqual(w.obligations["interval.binding"].state, ObligationState.SATISFIED)
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B3_R3_full_rederive_pass_accepts(self):
        w = self._staled()
        apply(w, Event.REDERIVE, {"close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)

    def test_B3_R4_order_independence(self):
        orders = (
            ({"mechanical.tests": "PASS"}, {"oracle.set": "MISSING"}),
            ({"oracle.set": "MISSING"}, {"mechanical.tests": "PASS"}),
        )
        for first, second in orders:
            with self.subTest(first=first, second=second):
                w = self._staled()
                apply(w, Event.REDERIVE, {"close_predicates": first})
                apply(w, Event.REDERIVE, {"close_predicates": second})
                apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
                d = decide(w)
                self.assertNotEqual(d.derived_status, State.ACCEPTED)
                self.assertNotEqual(w.obligations["interval.binding"].state, ObligationState.SATISFIED)


# ===========================================================================
# B4 — empty / uncorroborated review claim
# ===========================================================================


class B4ReviewClaimSchema(unittest.TestCase):
    def _medium(self, **launcher_over):
        w = open_task(policy(risk="MEDIUM"), launcher_capture(**launcher_over))
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        return w

    def test_B4_R1_context_digest_only_is_not_satisfying(self):
        w = self._medium()
        apply(w, Event.REVIEW_CLAIM, {"context_digest": "ctx-bound-1"})
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertNotEqual(w.obligations["review.claim"].state, ObligationState.SATISFIED)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertTrue(any("minimum schema" in n or "missing required fields" in n for n in w.notes), w.notes)

    def test_B4_R1b_each_required_field_is_load_bearing(self):
        for field in ("invocation_id", "context_manifest_digest", "launcher_capture_ref",
                      "reviewer_identity", "completion_status"):
            with self.subTest(field=field):
                w = self._medium()
                apply(w, Event.REVIEW_CLAIM, review_ok(**{field: ""}))
                apply(w, Event.CLOSE_WINDOW, close_ok())
                d = decide(w)
                self.assertNotEqual(d.derived_status, State.ACCEPTED, "%s was not load-bearing" % field)

    def test_B4_R1c_absent_findings_is_not_empty_findings(self):
        w = self._medium()
        apply(w, Event.REVIEW_CLAIM, review_ok(findings=None))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B4_R2_self_reported_files_without_launcher_capture_is_not_satisfying(self):
        w = self._medium(review_captures={})
        apply(w, Event.REVIEW_CLAIM, review_ok())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertTrue(any("no launcher capture" in n for n in w.notes), w.notes)

    def test_B4_R2b_claim_disagreeing_with_the_capture_is_adverse(self):
        """The reviewer says it read only clean files; the launcher captured otherwise."""
        w = self._medium(review_captures=review_capture(files_used=("review/policy.md", "CLAUDE.md")))
        apply(w, Event.REVIEW_CLAIM, review_ok())  # claims the clean default set
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(w.obligations["review.claim"].state, ObligationState.ADVERSE)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B4_R2c_claim_cannot_backfill_the_context_binding(self):
        w = open_task(policy(risk="MEDIUM"), launcher_capture(captured_review_context_digest=""))
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.REVIEW_CLAIM, review_ok(context_manifest_digest="digest-i-made-up"))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        self.assertNotEqual(w.captured_review_context_digest, "digest-i-made-up")
        self.assertNotEqual(decide(w).derived_status, State.ACCEPTED)

    def test_B4_R3_complete_claim_with_trusted_capture_accepts(self):
        w = self._medium()
        apply(w, Event.REVIEW_CLAIM, review_ok())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)

    def test_B4_R4_candidate_controlled_context_outside_allowlist_is_adverse(self):
        for poisoned in ("CLAUDE.md", "AGENTS.md", "prompt.override.md"):
            with self.subTest(file=poisoned):
                files = ("review/policy.md", poisoned)
                w = self._medium(review_captures=review_capture(files_used=files))
                apply(w, Event.REVIEW_CLAIM, review_ok(files_used=files))
                apply(w, Event.CLOSE_WINDOW, close_ok())
                d = decide(w)
                self.assertEqual(w.obligations["review.claim"].state, ObligationState.ADVERSE)
                self.assertNotEqual(d.derived_status, State.ACCEPTED)


# ===========================================================================
# B5 — adjudication scope / subject / resolution / issuer / expiry
# ===========================================================================


class B5Adjudication(unittest.TestCase):
    def _at_gate(self):
        w = honest_open("DESTRUCTIVE")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.REVIEW_CLAIM, review_ok())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        apply(w, Event.HUMAN_GATE, {"reason": "destructive_action_approval"})
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        return w

    def _finish(self, w):
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        return decide(w)

    def test_B5_R1_unrelated_subject_cannot_authorize(self):
        w = self._at_gate()
        apply(w, Event.ADJUDICATE, human_record(w, "some.unrelated.thing"), human_env(w))
        d = self._finish(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertNotEqual(w.obligations["human.destructive_approval"].state, ObligationState.SATISFIED)

    def test_B5_R1b_record_for_another_real_obligation_does_not_release_the_gate(self):
        w = self._at_gate()
        apply(w, Event.ADJUDICATE, human_record(w, "oracle.set"), human_env(w))
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B5_R2_wrong_scope_cannot_authorize(self):
        w = self._at_gate()
        apply(w, Event.ADJUDICATE,
              human_record(w, "human.destructive_approval", scope="totally-wrong-scope"), human_env(w))
        d = self._finish(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertTrue(any("scope" in n for n in w.notes), w.notes)

    def test_B5_R2b_missing_provenance_cannot_authorize(self):
        w = self._at_gate()
        apply(w, Event.ADJUDICATE,
              human_record(w, "human.destructive_approval", provenance_digest=""), human_env(w))
        d = self._finish(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B5_R3_adverse_resolution_cannot_satisfy(self):
        w = self._at_gate()
        apply(w, Event.ADJUDICATE,
              human_record(w, "human.destructive_approval", resolution="ADVERSE"), human_env(w))
        d = self._finish(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertEqual(w.obligations["human.destructive_approval"].state, ObligationState.ADVERSE)

    def test_B5_R4_record_expired_at_write_cannot_satisfy(self):
        w = self._at_gate()
        apply(w, Event.ADJUDICATE,
              human_record(w, "human.destructive_approval", lifetime=0), human_env(w))
        d = self._finish(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B5_R4b_record_that_expires_before_decide_cannot_satisfy(self):
        """Expiry is re-checked at every decide(), not only when written."""
        w = self._at_gate()
        apply(w, Event.ADJUDICATE, human_record(w, "human.destructive_approval", lifetime=2), human_env(w))
        self.assertEqual(w.state, State.VERIFYING)
        for _ in range(6):  # let the clock pass the record's expiry
            apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        d = self._finish(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertTrue(any("expired" in b for b in d.blockers), d.blockers)
        self.assertEqual(w.state, State.HUMAN_REQUIRED)

    def test_B5_R4c_pinned_controller_cannot_discharge_a_human_obligation(self):
        from fixtures import controller_env
        w = self._at_gate()
        apply(w, Event.ADJUDICATE, human_record(w, "human.destructive_approval"), controller_env(w))
        d = self._finish(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B5_R5_valid_matching_record_lets_the_honest_path_continue(self):
        w = self._at_gate()
        apply(w, Event.ADJUDICATE, human_record(w, "human.destructive_approval"), human_env(w))
        self.assertEqual(w.state, State.VERIFYING)
        d = self._finish(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)


# ===========================================================================
# B6 — bounded recovery for every route, including the default branch
# ===========================================================================


class B6RecoveryBudget(unittest.TestCase):
    UNKNOWN = "network_timeout"

    def _open(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        return w

    def test_B6_R1_unknown_default_recoverable_consumes_budget(self):
        w = self._open()
        apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": self.UNKNOWN, "obligation": "mechanical.tests"})
        ident = retry_identity(self.UNKNOWN, "mechanical.tests")
        self.assertIn(ident, w.budgets)
        self.assertEqual(w.budgets[ident].used, 1)
        self.assertEqual(w.obligations["mechanical.tests"].state, ObligationState.UNAVAILABLE_RECOVERABLE)

    def test_B6_R2_same_identity_increments_boundedly_then_exhausts(self):
        w = self._open()
        ident = retry_identity(self.UNKNOWN, "mechanical.tests")
        used = []
        for _ in range(20):
            apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": self.UNKNOWN, "obligation": "mechanical.tests"})
            used.append(w.budgets[ident].used)
            if w.state == State.WAITING_RECOVERABLE:
                apply(w, Event.RETRY, {"identity": ident})
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        self.assertEqual(w.human_reason, "automated_recovery_budget_exhausted")
        self.assertLessEqual(max(used), w.budgets[ident].maximum + 1)
        self.assertEqual(len(w.budgets), 1, "one identity must not spawn extra budgets")

    def test_B6_R3_wrong_retry_identity_cannot_borrow_another_budget(self):
        w = self._open()
        apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": "cache_miss", "obligation": "mechanical.tests"})
        other = retry_identity("cache_miss", "mechanical.tests")
        before = w.budgets[other].used
        apply(w, Event.RETRY, {"identity": "an-identity-that-does-not-exist"})
        self.assertEqual(w.state, State.WAITING_RECOVERABLE)
        self.assertEqual(w.budgets[other].used, before)
        self.assertEqual(w.obligations["mechanical.tests"].state, ObligationState.UNAVAILABLE_RECOVERABLE)

    def test_B6_R4_exhausted_budget_is_stable_human_required(self):
        w = self._open()
        for _ in range(8):
            apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": self.UNKNOWN, "obligation": "mechanical.tests"})
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        for _ in range(5):
            apply(w, Event.RETRY, {"identity": retry_identity(self.UNKNOWN, "mechanical.tests")})
            apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": self.UNKNOWN, "obligation": "mechanical.tests"})
            self.assertEqual(w.state, State.HUMAN_REQUIRED)
        self.assertNotEqual(decide(w).derived_status, State.ACCEPTED)

    def test_B6_R5_unresolved_recoverable_failure_cannot_accept(self):
        w = self._open()
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": self.UNKNOWN, "obligation": "mechanical.tests"})
        # Re-run everything except resolving the failed obligation.
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_B6_R6_recover_then_reverify_reaches_accepted(self):
        """MC-D: closing the bypass must not create a permanent latch."""
        w = self._open()
        apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": self.UNKNOWN, "obligation": "mechanical.tests"})
        apply(w, Event.RETRY, {"identity": retry_identity(self.UNKNOWN, "mechanical.tests")})
        self.assertEqual(w.state, State.VERIFYING)
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)


# ===========================================================================
# C7 — oracle lineage is 1:1 ; C10 — human-escalation DoS
# ===========================================================================


class C7OracleLineage(unittest.TestCase):
    def test_C7_one_observation_cannot_satisfy_two_expected_ids(self):
        w = honest_open("LOW")
        w.oracle_lineage["oracle.lint"] = "oracle.unit"
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, {"oracle_observed": ["oracle.unit"],
                                      "close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_C7_non_injective_lineage_is_rejected(self):
        w = honest_open("LOW")
        w.oracle_lineage.update({"oracle.unit": "merged", "oracle.lint": "merged"})
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, {"oracle_observed": ["merged"],
                                      "close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        self.assertTrue(any("injective" in b for b in d.blockers), d.blockers)

    def test_C7_honest_one_to_one_rename_still_accepts(self):
        w = honest_open("LOW")
        w.oracle_lineage["oracle.unit"] = "moved-unit-id"
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass(), launcher_env(w))
        apply(w, Event.CLOSE_WINDOW, {"oracle_observed": ["moved-unit-id", "oracle.lint"],
                                      "close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)


class C10HumanEscalationDoS(unittest.TestCase):
    def test_C10_candidate_cannot_self_declare_budget_exhaustion(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.HUMAN_GATE, {"reason": "automated_recovery_budget_exhausted"})
        self.assertNotEqual(
            (w.state, w.human_reason),
            (State.HUMAN_REQUIRED, "automated_recovery_budget_exhausted"),
        )
        self.assertTrue(any("C10" in n for n in w.notes), w.notes)

    def test_C10_verifier_unavailable_cannot_smuggle_the_reason(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.VERIFIER_UNAVAILABLE,
              {"kind": "transient_read_error", "human_reason": "automated_recovery_budget_exhausted"})
        self.assertEqual(w.state, State.WAITING_RECOVERABLE)

    def test_C10_real_exhaustion_still_escalates(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        for _ in range(6):
            apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": "cache_miss", "obligation": "mechanical.tests"})
            if w.state == State.WAITING_RECOVERABLE:
                apply(w, Event.RETRY, {"identity": retry_identity("cache_miss", "mechanical.tests")})
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        self.assertEqual(w.human_reason, "automated_recovery_budget_exhausted")


# ===========================================================================
# B7 — harness fail-closed
# ===========================================================================


EXPECTED_ATTACKS_T = ("F01", "F02b")


def _rows_t(digest, attacks=EXPECTED_ATTACKS_T):
    return [safety_row(attack_id=a, original_attack_blocked=True, new_variant_found=False,
                       klass="MC-A", evidence="e", kernel_digest=digest) for a in attacks]


def _results_t(digest, effective=None, **over):
    base = [
        AgentResult("liveness_runner", True, "PASS", prose="L", effective={"L": "PASS"}),
        AgentResult("safety_runner", True, "PASS", prose="m", original_blocked="YES",
                    new_variant_found="NO", effective=_rows_t(digest) if effective is None else effective),
        AgentResult("harness_mutator", True, "PASS", prose="H", effective={"H": "K"}),
        AgentResult("aggregator", True, "PASS", prose="a", effective={"r": 1}),
    ]
    idx = {"liveness": 0, "safety": 1, "mutator": 2, "agg": 3}
    for key, changes in over.items():
        for attr, val in changes.items():
            setattr(base[idx[key]], attr, val)
    return base


class B7HarnessFailClosed(unittest.TestCase):
    def setUp(self):
        self.digest = controller_trust_digest()

    _UNSET = object()

    def _gate(self, results, observed=_UNSET, **kw):
        kw.setdefault("expected_safety_attack_ids", EXPECTED_ATTACKS_T)
        if observed is self._UNSET:
            observed = self.digest
        return harness_gate(HARNESS_REQUIRED_AGENTS, results,
                            observed_kernel_digest=observed, **kw)

    def test_B7_R1_fabricated_digest_rejected(self):
        rep = self._gate(_results_t(self.digest), observed="deadbeef")
        self.assertEqual(rep.HARNESS_USABLE, "NO", rep.reasons)

    def test_B7_R1b_absent_digest_rejected(self):
        rep = self._gate(_results_t(self.digest), observed="")
        self.assertEqual(rep.HARNESS_USABLE, "NO", rep.reasons)

    def test_B7_R2_row_missing_new_variant_found_rejected(self):
        for field in ("new_variant_found", "original_attack_blocked", "usable",
                      "kernel_digest", "status", "attack_id", "evidence", "klass"):
            with self.subTest(field=field):
                rows = _rows_t(self.digest)
                rows[0] = {k: v for k, v in rows[0].items() if k != field}
                rep = self._gate(_results_t(self.digest, effective=rows))
                self.assertEqual(rep.HARNESS_USABLE, "NO", "%s was not required" % field)

    def test_B7_R3_replication_flip_rejected(self):
        rows = _rows_t(self.digest)
        flipped = dict(rows[0])
        flipped["original_attack_blocked"] = "NO"
        flipped["new_variant_found"] = "YES"
        rep = self._gate(_results_t(self.digest, effective=rows + [flipped]))
        self.assertEqual(rep.HARNESS_USABLE, "NO", rep.reasons)
        self.assertTrue(any("divergent" in r for r in rep.reasons), rep.reasons)

    def test_B7_R3b_agreeing_replication_is_allowed(self):
        rows = _rows_t(self.digest)
        rep = self._gate(_results_t(self.digest, effective=rows + [dict(rows[0])]))
        self.assertEqual(rep.HARNESS_USABLE, "YES", rep.reasons)

    def test_B7_R4_required_non_safety_agent_not_run_rejected(self):
        for bad in ({"verdict": "NOT_RUN"}, {"verdict": "ERROR"}, {"usable": False}, {"effective": None}):
            with self.subTest(bad=bad):
                rep = self._gate(_results_t(self.digest, liveness=bad))
                self.assertEqual(rep.HARNESS_USABLE, "NO", "%s was tolerated" % bad)

    def test_B7_R5_empty_safety_collection_rejected(self):
        rep = self._gate(_results_t(self.digest, effective=[]))
        self.assertEqual(rep.HARNESS_USABLE, "NO", rep.reasons)

    def test_B7_R5b_empty_expected_roster_rejected(self):
        rep = harness_gate(HARNESS_REQUIRED_AGENTS, _results_t(self.digest),
                           observed_kernel_digest=self.digest, expected_safety_attack_ids=())
        self.assertEqual(rep.HARNESS_USABLE, "NO", rep.reasons)

    def test_B7_R5c_incomplete_attack_roster_rejected(self):
        rep = self._gate(_results_t(self.digest, effective=_rows_t(self.digest, ("F01",))))
        self.assertEqual(rep.HARNESS_USABLE, "NO", rep.reasons)

    def test_B7_R6_full_valid_roster_is_usable(self):
        rep = self._gate(_results_t(self.digest))
        self.assertEqual(rep.HARNESS_USABLE, "YES", rep.reasons)


if __name__ == "__main__":
    unittest.main()
