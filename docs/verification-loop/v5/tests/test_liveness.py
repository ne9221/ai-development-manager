"""L1–L7 honest-path fixtures. LIVENESS_REQUIRED = 7/7 PASS before any attack fleet."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fixtures import close_ok, honest_open, mechanical_pass, review_ok
from v5_kernel.kernel import Event, Issuer, State, apply, decide


class LivenessTests(unittest.TestCase):
    def test_L1_low_risk_honest_reaches_accepted(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)
        self.assertEqual(w.state, State.ACCEPTED)
        self.assertTrue(d.trust_ok)

    def test_L2_medium_risk_independent_review_reaches_accepted(self):
        w = honest_open("MEDIUM")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.REVIEW_CLAIM, review_ok())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)
        self.assertIn("review.claim", w.required_obligation_ids)

    def test_L3_temporary_verifier_unavailable_retries_then_accepted(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        t = apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": "transient_read_error"})
        self.assertEqual(w.state, State.WAITING_RECOVERABLE)
        self.assertNotEqual(w.state, State.ACCEPTED)
        self.assertNotEqual(w.state, State.HUMAN_REQUIRED)
        self.assertIn("retry", t.next_action)
        apply(w, Event.RETRY, {"identity": "unavailable:transient_read_error:mechanical.tests"})
        self.assertEqual(w.state, State.VERIFYING)
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)

    def test_L4_stale_observation_rederives_then_accepted(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.CLOSE_WINDOW, {
            "oracle_observed": ["oracle.unit", "oracle.lint"],
            "close_predicates": {"mechanical.tests": "FAIL", "oracle.set": "PASS"},
        })
        apply(w, Event.STALE_BINDING, {})
        self.assertEqual(w.state, State.REQUIRES_RE_ADJUDICATION)
        d_mid = decide(w)
        self.assertNotEqual(d_mid.derived_status, State.ACCEPTED)
        self.assertNotEqual(w.state, State.HUMAN_REQUIRED)
        apply(w, Event.REDERIVE, {"close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d = decide(w)
        self.assertEqual(d.derived_status, State.ACCEPTED, d.blockers)

    def test_L5_adverse_real_blocker_never_accepted(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.ADVERSE_BLOCKER, {"obligation": "mechanical.tests"})
        d = decide(w)
        self.assertEqual(w.state, State.REJECTED)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)

    def test_L6_genuine_human_required_destructive(self):
        w = honest_open("DESTRUCTIVE")
        apply(w, Event.EXECUTOR_DONE, {})
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.REVIEW_CLAIM, review_ok())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        apply(w, Event.HUMAN_GATE, {"reason": "destructive_action_approval"})
        d = decide(w)
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        self.assertEqual(d.derived_status, State.HUMAN_REQUIRED)
        self.assertEqual(w.human_reason, "destructive_action_approval")
        # INV-4: after allowed human issuer record, honest path can continue.
        apply(
            w,
            Event.ADJUDICATE,
            {
                "record": {
                    "issuer": Issuer.HUMAN_OPERATOR.value,
                    "subject": "human.destructive_approval",
                    "expires_tick": w.tick + 20,
                    "provenance_digest": "human-1",
                },
                "resolution": "SATISFIED",
            },
        )
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
        apply(w, Event.CLOSE_WINDOW, close_ok())
        d2 = decide(w)
        self.assertEqual(d2.derived_status, State.ACCEPTED, d2.blockers)

    def test_L7_retry_exhausted_terminates_without_loop(self):
        w = honest_open("LOW")
        apply(w, Event.EXECUTOR_DONE, {})
        identity = "unavailable:transient_read_error:mechanical.tests"
        seen = []
        for _ in range(6):
            apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": "transient_read_error"})
            seen.append(w.state)
            if w.state == State.WAITING_RECOVERABLE:
                apply(w, Event.RETRY, {"identity": identity})
                seen.append(w.state)
        self.assertIn(State.HUMAN_REQUIRED, seen)
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        self.assertEqual(w.human_reason, "automated_recovery_budget_exhausted")
        d = decide(w)
        self.assertNotEqual(d.derived_status, State.ACCEPTED)
        # Further retry must not loop back into WAITING forever.
        apply(w, Event.RETRY, {"identity": identity})
        self.assertEqual(w.state, State.HUMAN_REQUIRED)
        apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": "transient_read_error"})
        self.assertIn(w.state, (State.HUMAN_REQUIRED, State.REJECTED))


if __name__ == "__main__":
    unittest.main()
