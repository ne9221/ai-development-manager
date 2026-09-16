import unittest

from manager.nextplan import harness as h
from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import default_atlas
from manager.nextplan.crosscheck import (
    NOT_PRODUCED, UNDETECTED_SIGNALS, coverage_report, crosscheck_problems, emitted_signals,
)
from manager.nextplan.planner import apply, new_task_state, plan


class AtlasPlannerCrosscheckTests(unittest.TestCase):
    def test_no_failure_code_breaks_a_planner_invariant(self):
        problems = crosscheck_problems()
        self.assertEqual([], problems, "\n".join(problems))

    def test_every_emitted_signal_is_claimed_by_a_failure_code(self):
        atlas = default_atlas()
        known = {signal for code in atlas.codes for signal in atlas.entry(code)["detection_signal"]}
        unclaimed = sorted(s for s in emitted_signals() - known if not s.startswith("orchestration."))
        self.assertEqual([], unclaimed)

    def test_coverage_is_honest_about_what_cannot_be_detected_yet(self):
        report = coverage_report()
        unproduced = {code for code, item in report.items() if not item["produced"]}
        self.assertEqual(NOT_PRODUCED, unproduced,
                         "a failure class gained or lost a detector; update NOT_PRODUCED deliberately")

    def test_most_failure_classes_have_a_real_detector(self):
        report = coverage_report()
        produced = [code for code, item in report.items() if item["produced"]]
        self.assertEqual(len(report) - len(NOT_PRODUCED), len(produced))
        self.assertGreaterEqual(len(produced) / len(report), 0.9)

    def test_declared_but_unimplemented_signals_stay_declared(self):
        atlas = default_atlas()
        known = {signal for code in atlas.codes for signal in atlas.entry(code)["detection_signal"]}
        undetected = {s for s in known - emitted_signals() if not s.startswith("orchestration.")}
        self.assertEqual(UNDETECTED_SIGNALS, undetected,
                         "a declared detection signal gained or lost an implementation; update the list deliberately")


class TerminationTests(unittest.TestCase):
    """The loop always stops, whatever sequence of failures arrives."""

    def repeat(self, signals, rounds=30, execution=None):
        state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
        state["state"], state["worker_session"] = v.WORKING, h.WORKER_SESSION
        actions = []
        for index in range(rounds):
            event = h.event(h.worker_result(event_id=f"evt-{index}"), generation=state["generation"],
                            event_id=f"evt-{index}", verification=h.verification(signals), execution=execution)
            decision = plan(state, event)
            actions.append(decision["action"])
            state = apply(state, event, decision)
            if decision["action"] in v.HOLD_ACTIONS or state["state"] in v.TERMINAL_STATES:
                break
        return actions, state

    def test_every_repeatable_failure_reaches_a_hold(self):
        atlas = default_atlas()
        for code in atlas.codes:
            entry = atlas.entry(code)
            if not entry["retryable"] or entry["human_gate_required"]:
                continue
            signals = [entry["detection_signal"][0]]
            if signals[0].startswith(("orchestration.", "execution.", "planner.")):
                continue
            with self.subTest(code):
                actions, state = self.repeat(signals)
                self.assertIn(actions[-1], v.HOLD_ACTIONS, f"{code} never stopped: {actions}")
                self.assertLessEqual(len(actions), entry["max_retry_policy"]["max_attempts"] + 2)

    def test_alternating_failures_still_terminate(self):
        state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
        state["state"], state["worker_session"] = v.WORKING, h.WORKER_SESSION
        cycle = ["verify.push.local_ahead_of_remote", "verify.tests.failed", "verify.base.behind_required_base",
                 "verify.git_status.dirty_in_scope"]
        last = None
        for index in range(60):
            event = h.event(h.worker_result(event_id=f"evt-{index}"), generation=state["generation"],
                            event_id=f"evt-{index}", verification=h.verification([cycle[index % len(cycle)]]))
            decision = plan(state, event)
            state = apply(state, event, decision)
            last = decision["action"]
            if last in v.HOLD_ACTIONS:
                break
        self.assertIn(last, v.HOLD_ACTIONS)

    def test_a_held_task_never_resumes_itself(self):
        state = new_task_state("t-1")
        state["state"] = v.HUMAN_GATE_STATE
        for index in range(5):
            event = h.event(h.worker_result(event_id=f"evt-{index}"), generation=state["generation"],
                            event_id=f"evt-{index}")
            decision = plan(state, event)
            state = apply(state, event, decision)
            self.assertEqual(v.NO_OP, decision["action"])
        self.assertEqual(v.HUMAN_GATE_STATE, state["state"])


class NoFalsePassTests(unittest.TestCase):
    def test_no_single_failure_can_produce_completion(self):
        atlas = default_atlas()
        state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
        state["state"], state["worker_session"] = v.WORKING, h.WORKER_SESSION
        for code in atlas.codes:
            for signal in atlas.entry(code)["detection_signal"]:
                with self.subTest(f"{code}:{signal}"):
                    if signal.startswith("orchestration."):
                        event = h.event(h.worker_result(), orchestration_signals=[signal])
                    else:
                        event = h.event(h.worker_result(), verification=h.verification([signal]))
                    decision = plan(state, event)
                    self.assertNotEqual(v.MARK_COMPLETE, decision["action"], f"{code} completed the task")

    def test_a_human_gate_outranks_a_more_severe_automated_failure(self):
        state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
        state["state"], state["worker_session"] = v.WORKING, h.WORKER_SESSION
        event = h.event(h.worker_result(), verification=h.verification(["verify.claim_contradicted"]),
                        orchestration_signals=["orchestration.concurrent_writer_same_worktree"])
        decision = plan(state, event)
        self.assertEqual(v.HUMAN_GATE, decision["action"])
        self.assertEqual("parallel_writer_collision", decision["failure_code"])
        self.assertIn("false_pass", decision["failures"])


if __name__ == "__main__":
    unittest.main()
