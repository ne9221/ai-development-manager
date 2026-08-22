import unittest

from manager.continuation_decision import (
    classify_evidence, decide_continuation, decide_dispatch, decide_next_dispatch,
    decide_retry, decide_validation, dispatch_accepted, dispatch_failed, evidence_ready,
    plan_task, provider_crashed, provider_started, provider_terminal_evidence,
)
from manager.continuation_states import ContinuationError, validate_transition


class TransitionContractTests(unittest.TestCase):
    def test_legal_transition_accepted(self):
        self.assertEqual(validate_transition("task_planned", "idle", "task_planned"), "task_planned")

    def test_illegal_transition_rejected(self):
        with self.assertRaises(ContinuationError):
            validate_transition("task_planned", "idle", "running")

    def test_unknown_event_rejected(self):
        with self.assertRaises(ContinuationError):
            validate_transition("not_an_event", "idle", "task_planned")

    def test_unknown_state_rejected(self):
        with self.assertRaises(ContinuationError):
            validate_transition("task_planned", "nowhere", "task_planned")

    def test_terminal_states_have_no_automatic_edges(self):
        from manager.continuation_states import TRANSITIONS

        for event, table in TRANSITIONS.items():
            self.assertNotIn("blocked", table, f"event {event} must not leave BLOCKED automatically")
            self.assertNotIn("stop_requires_user", table, f"event {event} must not leave STOP_REQUIRES_USER automatically")


class HappyPathTests(unittest.TestCase):
    def test_full_pass_and_continue_cycle(self):
        d = plan_task()
        self.assertEqual((d.from_state, d.to_state), ("idle", "task_planned"))

        d = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                             is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "dispatching")

        d = dispatch_accepted()
        self.assertEqual(d.to_state, "queued")

        d = provider_started()
        self.assertEqual(d.to_state, "running")

        d = provider_terminal_evidence()
        self.assertEqual(d.to_state, "awaiting_evidence")

        d = evidence_ready()
        self.assertEqual(d.to_state, "validating")

        d = decide_validation("validating", {"result": "pass", "signals": {"tests": "42 passed"}})
        self.assertEqual(d.to_state, "pass")

        d = decide_continuation("pass", depth=0, max_depth=5)
        self.assertEqual(d.to_state, "next_task_ready")
        self.assertEqual(d.patch["depth"], 1)

        d = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                  is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "task_planned")


class EvidenceClassificationTests(unittest.TestCase):
    def test_pass_requires_signals(self):
        outcome, reason = classify_evidence({"result": "pass"})
        self.assertEqual(outcome, "blocked")
        self.assertIn("no supporting signals", reason)

    def test_explicit_pass_with_signals(self):
        outcome, _ = classify_evidence({"result": "pass", "signals": {"tests": "ok"}})
        self.assertEqual(outcome, "pass")

    def test_explicit_fail(self):
        outcome, _ = classify_evidence({"result": "fail", "signals": {"tests": "1 failed"}})
        self.assertEqual(outcome, "fail")

    def test_explicit_ambiguous(self):
        outcome, _ = classify_evidence({"result": "ambiguous", "reason": "provider report unreadable"})
        self.assertEqual(outcome, "blocked")

    def test_missing_result_never_guesses_pass(self):
        outcome, _ = classify_evidence({})
        self.assertEqual(outcome, "blocked")

    def test_malformed_evidence_never_guesses_pass(self):
        outcome, _ = classify_evidence(None)
        self.assertEqual(outcome, "blocked")

    def test_unrecognized_result_value_blocked(self):
        outcome, _ = classify_evidence({"result": "sort of", "signals": {"x": 1}})
        self.assertEqual(outcome, "blocked")

    def test_decide_validation_matches_classify(self):
        d = decide_validation("validating", {"result": "fail", "signals": {"tests": "boom"}, "retryable": True})
        self.assertEqual(d.to_state, "fail")
        self.assertTrue(d.patch["retryable"])


class DispatchGateTests(unittest.TestCase):
    def test_irreversible_action_stops(self):
        for kind in ("production_deploy", "auth", "payment", "irreversible"):
            with self.subTest(kind=kind):
                d = decide_dispatch("task_planned", action_kind=kind, duplicate_authority=False,
                                     is_production_mutating=False, active_production_mutating_count=0)
                self.assertEqual(d.to_state, "stop_requires_user")

    def test_duplicate_authority_stops(self):
        d = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=True,
                             is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "stop_requires_user")

    def test_duplicate_authority_beats_irreversible_reason_but_still_stops(self):
        d = decide_dispatch("task_planned", action_kind="production_deploy", duplicate_authority=True,
                             is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "stop_requires_user")

    def test_ordinary_dispatch_authorized(self):
        d = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                             is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "dispatching")

    def test_read_only_bypasses_mutual_exclusion_on_initial_dispatch(self):
        d = decide_dispatch("task_planned", action_kind="read_only", duplicate_authority=False,
                             is_production_mutating=False, active_production_mutating_count=5)
        self.assertEqual(d.to_state, "dispatching")

    def test_production_mutating_holds_on_initial_dispatch_when_actor_active(self):
        d = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                             is_production_mutating=True, active_production_mutating_count=1)
        self.assertEqual(d.to_state, "task_planned")
        self.assertIn("holding", d.reason)

    def test_production_mutating_proceeds_on_initial_dispatch_when_no_actor_active(self):
        d = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                             is_production_mutating=True, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "dispatching")

    def test_dispatch_failed_retryable_goes_to_fail(self):
        d = dispatch_failed("dispatching", retryable=True, reason="transient quota API error")
        self.assertEqual(d.to_state, "fail")

    def test_dispatch_failed_not_retryable_stops(self):
        d = dispatch_failed("dispatching", retryable=False, reason="no eligible provider")
        self.assertEqual(d.to_state, "stop_requires_user")

    def test_provider_crash_routes_through_fail_not_a_guessed_verdict(self):
        d = provider_crashed("running", reason="process died, no completion report")
        self.assertEqual(d.to_state, "fail")


class ContinuationDepthTests(unittest.TestCase):
    def test_advances_below_limit(self):
        d = decide_continuation("pass", depth=2, max_depth=5)
        self.assertEqual(d.to_state, "next_task_ready")
        self.assertEqual(d.patch["depth"], 3)

    def test_stops_at_limit(self):
        d = decide_continuation("pass", depth=4, max_depth=5)
        self.assertEqual(d.to_state, "next_task_ready")
        d2 = decide_continuation("pass", depth=5, max_depth=5)
        self.assertEqual(d2.to_state, "stop_requires_user")

    def test_invalid_max_depth_rejected(self):
        with self.assertRaises(ContinuationError):
            decide_continuation("pass", depth=0, max_depth=0)


class NextDispatchGateTests(unittest.TestCase):
    def test_read_only_bypasses_mutual_exclusion(self):
        d = decide_next_dispatch("next_task_ready", action_kind="read_only", duplicate_authority=False,
                                  is_production_mutating=False, active_production_mutating_count=3)
        self.assertEqual(d.to_state, "task_planned")

    def test_production_mutating_holds_when_actor_active(self):
        d = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                  is_production_mutating=True, active_production_mutating_count=1)
        self.assertEqual(d.to_state, "next_task_ready")
        self.assertIn("holding", d.reason)

    def test_production_mutating_proceeds_when_no_actor_active(self):
        d = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                  is_production_mutating=True, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "task_planned")

    def test_irreversible_next_slice_stops(self):
        d = decide_next_dispatch("next_task_ready", action_kind="payment", duplicate_authority=False,
                                  is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "stop_requires_user")

    def test_duplicate_authority_stops(self):
        d = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=True,
                                  is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d.to_state, "stop_requires_user")


class SingleWriterGateTests(unittest.TestCase):
    """Regression coverage for the shared _dispatch_gate: the initial
    TASK_PLANNED -> DISPATCHING path and the NEXT_TASK_READY -> TASK_PLANNED
    continuation path must agree on every input, and two independent chains
    must never both be authorized to hold the sole production-mutating
    slot concurrently."""

    def test_initial_and_continuation_paths_agree_on_every_gate_outcome(self):
        cases = [
            dict(action_kind="implementation", duplicate_authority=False, is_production_mutating=False, active_production_mutating_count=4),
            dict(action_kind="implementation", duplicate_authority=False, is_production_mutating=True, active_production_mutating_count=0),
            dict(action_kind="implementation", duplicate_authority=False, is_production_mutating=True, active_production_mutating_count=1),
            dict(action_kind="payment", duplicate_authority=False, is_production_mutating=False, active_production_mutating_count=0),
            dict(action_kind="implementation", duplicate_authority=True, is_production_mutating=True, active_production_mutating_count=0),
        ]
        for case in cases:
            with self.subTest(**case):
                initial = decide_dispatch("task_planned", **case)
                continuation = decide_next_dispatch("next_task_ready", **case)
                # Same outcome family (proceed/hold/stop), regardless of the
                # differing state names each path's self-loop hold targets.
                self.assertEqual(initial.to_state == "stop_requires_user", continuation.to_state == "stop_requires_user")
                initial_proceeded = initial.to_state == "dispatching"
                continuation_proceeded = continuation.to_state == "task_planned"
                self.assertEqual(initial_proceeded, continuation_proceeded)
                initial_held = initial.to_state == "task_planned" and not initial_proceeded
                continuation_held = continuation.to_state == "next_task_ready"
                self.assertEqual(initial_held, continuation_held)

    def test_two_independent_chains_cannot_both_authorize_production_mutation(self):
        # Chain A dispatches first and becomes the sole active production
        # writer (active_production_mutating_count reflects chain B's view
        # of the world once A is active).
        chain_a_initial = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                                           is_production_mutating=True, active_production_mutating_count=0)
        self.assertEqual(chain_a_initial.to_state, "dispatching")

        # Chain B, independently, tries the same gate while A is active --
        # both the initial-dispatch path and the continuation path must hold
        # B rather than let it proceed alongside A.
        chain_b_initial = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                                           is_production_mutating=True, active_production_mutating_count=1)
        self.assertNotEqual(chain_b_initial.to_state, "dispatching")
        self.assertEqual(chain_b_initial.to_state, "task_planned")

        chain_b_continuation = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                                      is_production_mutating=True, active_production_mutating_count=1)
        self.assertNotEqual(chain_b_continuation.to_state, "task_planned")
        self.assertEqual(chain_b_continuation.to_state, "next_task_ready")

        # Once A's slot frees (count drops back to 0), B may proceed.
        chain_b_retry = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                                         is_production_mutating=True, active_production_mutating_count=0)
        self.assertEqual(chain_b_retry.to_state, "dispatching")


class RetryTests(unittest.TestCase):
    def test_retryable_under_limit_retries(self):
        d = decide_retry("fail", retryable=True, retry_count=0, max_retries=2)
        self.assertEqual(d.to_state, "dispatching")
        self.assertEqual(d.patch["retry_count"], 1)

    def test_retryable_at_limit_stops(self):
        d = decide_retry("fail", retryable=True, retry_count=2, max_retries=2)
        self.assertEqual(d.to_state, "stop_requires_user")

    def test_no_infinite_retry_loop(self):
        state, retry_count, max_retries = "fail", 0, 3
        stops = 0
        for _ in range(10):
            d = decide_retry(state, retryable=True, retry_count=retry_count, max_retries=max_retries)
            if d.to_state == "stop_requires_user":
                stops += 1
                break
            retry_count = d.patch["retry_count"]
        self.assertEqual(stops, 1)
        self.assertLessEqual(retry_count, max_retries)

    def test_non_retryable_always_stops(self):
        d = decide_retry("fail", retryable=False, retry_count=0, max_retries=5)
        self.assertEqual(d.to_state, "stop_requires_user")

    def test_invalid_max_retries_rejected(self):
        with self.assertRaises(ContinuationError):
            decide_retry("fail", retryable=True, retry_count=0, max_retries=-1)


class DecisionEqualityTests(unittest.TestCase):
    def test_equal_decisions_compare_equal(self):
        d1 = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                              is_production_mutating=False, active_production_mutating_count=0)
        d2 = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                              is_production_mutating=False, active_production_mutating_count=0)
        self.assertEqual(d1, d2)


if __name__ == "__main__":
    unittest.main()
