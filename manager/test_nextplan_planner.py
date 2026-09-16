import copy
import unittest

from manager.nextplan import harness as h
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.classify import completion_proof
from manager.nextplan.planner import (
    MAX_CONTINUATIONS, MAX_TOTAL_ROUNDS, PlannerInputError, apply, new_task_state, plan,
)

PROOF_FIELDS = ("head_sha", "commit_sha", "remote_sha", "push_status", "git_status", "tests_failed")


def state(**changes):
    base = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
    base.update(changes)
    return base


def verified_pass(**kwargs):
    return h.event(h.worker_result(**kwargs))


class GuardOrderTests(unittest.TestCase):
    def test_terminal_task_rejects_every_transition(self):
        before = state(state=v.COMPLETE)
        decision = plan(before, verified_pass())
        self.assertEqual((v.NO_OP, "task_already_completed"), (decision["action"], decision["failure_code"]))
        self.assertEqual(before, apply(before, verified_pass(), decision))

    def test_duplicate_event_is_an_idempotent_no_op(self):
        before = state(processed_event_ids=["evt-w1"])
        decision = plan(before, verified_pass())
        self.assertEqual((v.NO_OP, "duplicate_result"), (decision["action"], decision["failure_code"]))
        self.assertEqual(before["generation"], apply(before, verified_pass(), decision)["generation"])

    def test_stale_result_never_overwrites_a_newer_transition(self):
        before = state(generation=3, state=v.WORKING)
        event = h.event(h.worker_result(), generation=1)
        decision = plan(before, event)
        self.assertEqual((v.NO_OP, "stale_task_state"), (decision["action"], decision["failure_code"]))
        after = apply(before, event, decision)
        self.assertEqual((3, v.WORKING), (after["generation"], after["state"]))
        self.assertIn(event["event_id"], after["processed_event_ids"])

    def test_event_from_a_newer_generation_waits_rather_than_guessing(self):
        decision = plan(state(), h.event(h.worker_result(), generation=5))
        self.assertEqual((v.WAIT_DEPENDENCY, "stale_ssot"), (decision["action"], decision["failure_code"]))

    def test_held_task_moves_only_by_human_resolution(self):
        for held in (v.BLOCKED, v.HUMAN_GATE_STATE):
            with self.subTest(held):
                self.assertEqual(v.NO_OP, plan(state(state=held), verified_pass())["action"])

    def test_event_for_another_task_is_an_input_error(self):
        foreign = h.event(h.worker_result(task_id="t-2"), task_id="t-2")
        with self.assertRaises(PlannerInputError):
            plan(state(), foreign)

    def test_result_from_the_wrong_owner_is_not_applied(self):
        before = state(state=v.WORKING, phase_owner={"role": v.WORKER, "session_id": "s-worker-1"})
        decision = plan(before, h.event(h.reviewer_result(), role=v.REVIEWER))
        self.assertEqual((v.MARK_BLOCKED, "conflicting_owner"), (decision["action"], decision["failure_code"]))

    def test_malformed_planner_input_goes_to_a_human(self):
        decision = plan(state(), h.event(h.worker_result(), generation="one"))
        self.assertEqual((v.HUMAN_GATE, "planner_no_valid_action"), (decision["action"], decision["failure_code"]))


class CompletionGateTests(unittest.TestCase):
    def test_verified_worker_result_goes_to_a_fresh_reviewer(self):
        decision = plan(state(), verified_pass())
        self.assertEqual(v.SEND_TO_REVIEW, decision["action"])
        self.assertTrue(decision["constraints"]["fresh_reviewer"])
        self.assertIn(h.WORKER_SESSION, decision["constraints"]["exclude_sessions"])
        self.assertTrue(all(item["ok"] for item in decision["proof"]))

    def test_completion_without_review_requires_the_same_proof(self):
        before = state(requirements=dict(new_task_state("t-1")["requirements"], requires_review=False))
        decision = plan(before, verified_pass())
        self.assertEqual(v.MARK_COMPLETE, decision["action"])
        self.assertEqual(v.COMPLETE, apply(before, verified_pass(), decision)["state"])

    def test_no_missing_proof_item_can_complete_or_pass_review(self):
        for field in PROOF_FIELDS:
            with self.subTest(field):
                decision = plan(state(), h.event(h.worker_result(drop=(field,))))
                self.assertNotIn(decision["action"], (v.MARK_COMPLETE, v.SEND_TO_REVIEW))

    def test_reported_but_unverified_success_is_never_enough(self):
        decision = plan(state(), h.event(h.worker_result(verified=False)))
        self.assertNotIn(decision["action"], (v.MARK_COMPLETE, v.SEND_TO_REVIEW))
        self.assertEqual("evidence_missing", decision["failure_code"])

    def test_heuristic_pass_is_not_a_claim(self):
        facts = {"status": r.fact("PASS", v.DERIVED, "agent:heuristic")}
        decision = plan(state(), h.event(h.worker_result(status=None, facts=facts)))
        self.assertNotIn(decision["action"], (v.MARK_COMPLETE, v.SEND_TO_REVIEW))

    def test_partial_progress_continues_the_same_worker(self):
        decision = plan(state(state=v.WORKING), h.event(h.worker_result(status="PARTIAL")))
        self.assertEqual(v.CONTINUE_WORKER, decision["action"])
        self.assertEqual("continuation", decision["budget"]["key"])

    def test_endless_partial_progress_eventually_reaches_a_human(self):
        history = [{"key": "continuation", "code": None, "action": v.CONTINUE_WORKER}] * MAX_CONTINUATIONS
        before = state(state=v.WORKING, retry_history=history)
        decision = plan(before, h.event(h.worker_result(status="PARTIAL")))
        self.assertEqual((v.HUMAN_GATE, "retry_exhausted"), (decision["action"], decision["failure_code"]))


class BudgetTests(unittest.TestCase):
    def push_failure_event(self, generation, index):
        return h.event(h.worker_result(event_id=f"evt-w{index}"), generation=generation,
                       verification=h.verification(["verify.push.local_ahead_of_remote"]))

    def test_a_repeated_failure_ends_in_a_hold_not_a_loop(self):
        current, actions = state(state=v.WORKING), []
        for index in range(3):
            event = self.push_failure_event(current["generation"], index)
            decision = plan(current, event)
            actions.append(decision["action"])
            current = apply(current, event, decision)
        self.assertEqual([v.RETRY_SAME_AGENT, v.RETRY_SAME_AGENT, v.MARK_BLOCKED], actions)

    def test_the_global_round_cap_stops_alternating_failures(self):
        history = [{"key": f"code:c{index}", "code": None, "action": v.CONTINUE_WORKER}
                   for index in range(MAX_TOTAL_ROUNDS)]
        decision = plan(state(state=v.WORKING, retry_history=history), self.push_failure_event(0, 1))
        self.assertIn(decision["action"], v.HOLD_ACTIONS)
        self.assertIn("retry_exhausted", decision["failures"])

    def test_quota_reroutes_when_another_agent_is_eligible(self):
        decision = plan(state(state=v.WORKING), h.event(h.worker_result(), execution={"kind": "quota_exhausted"}))
        self.assertEqual((v.REROUTE_AGENT, "quota_exhausted"), (decision["action"], decision["failure_code"]))
        self.assertEqual("codex", decision["constraints"]["agent"])

    def test_quota_waits_when_no_other_agent_is_eligible(self):
        before = state(state=v.WORKING, reroute_candidates=["claude"])
        decision = plan(before, h.event(h.worker_result(), execution={"kind": "quota_exhausted"}))
        self.assertEqual(v.WAIT_DEPENDENCY, decision["action"])

    def test_returning_to_an_unknown_original_worker_falls_back(self):
        before = state(state=v.AWAITING_REVIEW, phase_owner={"role": v.REVIEWER, "session_id": None},
                       candidate={"head_sha": h.HEAD, "proof": completion_proof(h.worker_result(), {}),
                                  "worker_session": None})
        decision = plan(before, h.event(h.reviewer_result(verdict="FAIL"), role=v.REVIEWER))
        self.assertEqual("reviewer_fail", decision["failure_code"])
        self.assertIn(decision["action"], v.HOLD_ACTIONS)


class HumanGateTests(unittest.TestCase):
    def test_a_human_gated_failure_is_never_automated(self):
        for signal in ("orchestration.same_key_different_payload", "orchestration.concurrent_writer_same_worktree",
                       "verify.ssot.github_drive_disagree"):
            with self.subTest(signal):
                decision = plan(state(state=v.WORKING), h.event(h.worker_result(), orchestration_signals=[signal]))
                self.assertEqual(v.HUMAN_GATE, decision["action"])

    def test_wrong_repo_stops_the_execution(self):
        decision = plan(state(state=v.WORKING),
                        h.event(h.worker_result(), verification=h.verification(["verify.repo.identity_mismatch"])))
        self.assertEqual((v.HUMAN_GATE, "wrong_repo"), (decision["action"], decision["failure_code"]))


class PurityTests(unittest.TestCase):
    def test_same_input_gives_the_same_decision(self):
        before, event = state(), verified_pass()
        self.assertEqual(plan(before, event), plan(before, event))

    def test_plan_and_apply_never_mutate_their_inputs(self):
        before, event = state(state=v.WORKING), verified_pass()
        snapshot_state, snapshot_event = copy.deepcopy(before), copy.deepcopy(event)
        decision = plan(before, event)
        apply(before, event, decision)
        self.assertEqual((snapshot_state, snapshot_event), (before, event))

    def test_the_decision_records_what_it_was_computed_from(self):
        first = plan(state(), verified_pass())
        second = plan(state(state=v.WORKING), verified_pass())
        self.assertNotEqual(first["input_digest"], second["input_digest"])

    def test_every_action_comes_from_the_closed_set(self):
        cases = [verified_pass(), h.event(h.worker_result(verified=False)),
                 h.event(h.worker_result(status="BLOCKED")),
                 h.event(h.worker_result(), execution={"kind": "timed_out"}),
                 h.event(h.reviewer_result(), role=v.REVIEWER), h.event(kind="tick", result=None)]
        for event in cases:
            with self.subTest(event["event_id"]):
                before = state(state=v.WORKING, phase_owner={"role": event["role"], "session_id": None})
                decision = plan(before, event)
                self.assertIn(decision["action"], v.ACTIONS)
                self.assertIn(decision["next_state"], v.STATES)


class TickTests(unittest.TestCase):
    def test_an_unsatisfied_dependency_waits(self):
        before = state(dependencies=[{"id": "ledger-phase-1", "satisfied": False}])
        decision = plan(before, h.event(kind="tick"))
        self.assertEqual((v.WAIT_DEPENDENCY, "blocked_dependency"), (decision["action"], decision["failure_code"]))

    def test_a_satisfied_dependency_dispatches_the_worker(self):
        before = state(dependencies=[{"id": "ledger-phase-1", "satisfied": True}])
        self.assertEqual(v.CONTINUE_WORKER, plan(before, h.event(kind="tick"))["action"])

    def test_a_tick_on_a_busy_task_does_nothing(self):
        self.assertEqual(v.NO_OP, plan(state(state=v.WORKING), h.event(kind="tick"))["action"])


if __name__ == "__main__":
    unittest.main()
