"""Backup 1: property and mutation tests.

Two kinds of check:

- **Properties** over randomly built inputs (fixed seeds, so a failure is
  reproducible): omitted fields, duplicated and reordered events, stale
  generations, contradictory facts and garbage text must never produce a
  completion, an invalid result, or a loop that does not stop.
- **Mutation controls** that deliberately break one guard and assert the
  behaviour changes. Without these, a passing suite could be passing for the
  wrong reason: they prove each guard is actually load-bearing.
"""

import copy
import random
import string
import unittest
from unittest.mock import patch

from manager.nextplan import classify as classify_module
from manager.nextplan import harness as h
from manager.nextplan import result as r
from manager.nextplan import verify as verify_module
from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import default_atlas
from manager.nextplan.extract import extract
from manager.nextplan.planner import apply, new_task_state, plan

SEEDS = (1, 7, 13, 42, 99, 2026)
# tests_run belongs here: leaving it out was how the suite mirrored the
# production blind spot the 2026-09-16 review found.
PROOF_FIELDS = ("status", "head_sha", "commit_sha", "remote_sha", "push_status", "git_status",
                "tests_failed", "tests_run")


def working(**changes):
    state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
    state.update(state=v.WORKING, phase_owner={"role": v.WORKER, "session_id": None},
                 worker_session=h.WORKER_SESSION, worker_sessions=[h.WORKER_SESSION])
    state.update(changes)
    return state


def random_text(rng, size=400):
    alphabet = string.printable + "完成失敗測試已推送進度"
    return "".join(rng.choice(alphabet) for _ in range(size))


class ResultPropertyTests(unittest.TestCase):
    def test_random_fact_omissions_never_complete(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            for _ in range(40):
                dropped = tuple(field for field in PROOF_FIELDS if rng.random() < 0.5)
                if not dropped:
                    continue
                with self.subTest(seed=seed, dropped=dropped):
                    event = h.event(h.worker_result(drop=dropped))
                    decision = plan(working(), event)
                    self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_random_level_downgrades_never_complete(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            for _ in range(40):
                result = h.worker_result()
                field = rng.choice(PROOF_FIELDS[1:])
                value = r.value(result, field)
                result = r.with_fact(result, field, r.fact(value, v.REPORTED, "agent:fenced"))
                with self.subTest(seed=seed, field=field):
                    decision = plan(working(), h.event(result))
                    self.assertNotIn(decision["action"], (v.MARK_COMPLETE, v.SEND_TO_REVIEW))

    def test_random_garbage_text_is_never_verified_and_always_valid(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            for index in range(25):
                content = random_text(rng, rng.randint(0, 800))
                envelope = {"event_id": f"e{index}", "task_id": "t-1", "role": v.WORKER, "format": "text",
                            "content": content}
                with self.subTest(seed=seed, index=index):
                    result = extract(envelope)
                    r.validate_result(result)
                    levels = {r.level(result, field) for field in r.FACT_FIELDS}
                    self.assertEqual(set(), levels & {v.VERIFIED, v.CONTRADICTED})

    def test_truncated_payloads_never_yield_a_reported_pass(self):
        full = ('{"schema_version": "adm-ai-result/1", "task_id": "t-1", "status": "PASS", "tests_failed": 0}')
        for cut in range(10, len(full), 7):
            content = "```adm-result\n" + full[:cut] + "\n```\n"
            with self.subTest(cut=cut):
                result = extract({"event_id": "e", "task_id": "t-1", "role": v.WORKER, "format": "text",
                                  "content": content})
                if r.value(result, "status") == "PASS":
                    self.assertNotEqual(v.REPORTED, r.level(result, "status"))


class EventSequencePropertyTests(unittest.TestCase):
    def build_events(self, count):
        return [h.event(h.worker_result(event_id=f"evt-{index}", status="PARTIAL"), event_id=f"evt-{index}")
                for index in range(count)]

    def feed(self, state, events):
        for event in events:
            event = dict(event, generation=min(event["generation"], state["generation"]))
            decision = plan(state, event)
            state = apply(state, event, decision)
        return state

    def test_duplicates_and_reordering_do_not_change_the_outcome(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            events = self.build_events(4)
            clean = self.feed(working(), events)
            noisy = list(events)
            for _ in range(6):
                noisy.insert(rng.randrange(len(noisy) + 1), copy.deepcopy(rng.choice(events)))
            with self.subTest(seed=seed):
                replayed = self.feed(working(), noisy)
                self.assertEqual(clean["generation"], replayed["generation"])
                self.assertEqual(clean["state"], replayed["state"])
                self.assertEqual(clean["retry_history"], replayed["retry_history"])

    def test_stale_events_never_move_a_task(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            state = self.feed(working(), self.build_events(2))
            before = copy.deepcopy(state)
            for index in range(10):
                stale = h.event(h.worker_result(event_id=f"old-{index}"), event_id=f"old-{index}",
                                generation=rng.randrange(0, max(1, before["generation"])))
                decision = plan(state, stale)
                self.assertEqual(v.NO_OP, decision["action"])
                state = apply(state, stale, decision)
            self.assertEqual(before["state"], state["state"])
            self.assertEqual(before["generation"], state["generation"])
            self.assertEqual(before["candidate"], state["candidate"])

    def test_random_failure_streams_always_stop(self):
        atlas = default_atlas()
        signals = [entry for code in atlas.codes
                   for entry in atlas.entry(code)["detection_signal"]
                   if entry.startswith("verify.")]
        for seed in SEEDS:
            rng = random.Random(seed)
            state, last = working(), None
            with self.subTest(seed=seed):
                for index in range(80):
                    event = h.event(h.worker_result(event_id=f"evt-{index}"), event_id=f"evt-{index}",
                                    generation=state["generation"],
                                    verification=h.verification([rng.choice(signals)]))
                    decision = plan(state, event)
                    state = apply(state, event, decision)
                    last = decision["action"]
                    if last in v.HOLD_ACTIONS:
                        break
                self.assertIn(last, v.HOLD_ACTIONS)


class MutationControlTests(unittest.TestCase):
    """Break one guard on purpose; the behaviour must change."""

    def test_the_completion_proof_is_what_blocks_unverified_work(self):
        event = h.event(h.worker_result(verified=False))
        self.assertNotEqual(v.SEND_TO_REVIEW, plan(working(), event)["action"])
        always_ok = lambda result, requirements: [{"requirement": "mutant", "field": "status", "expected": "",  # noqa: E731
                                                   "value": None, "level": v.VERIFIED, "ok": True}]
        with patch.object(classify_module, "completion_proof", always_ok), \
             patch("manager.nextplan.planner.completion_proof", always_ok):
            self.assertEqual(v.SEND_TO_REVIEW, plan(working(), event)["action"])

    def test_the_human_gate_precedence_is_load_bearing(self):
        event = h.event(h.worker_result(), verification=h.verification(["verify.claim_contradicted"]),
                        orchestration_signals=["orchestration.concurrent_writer_same_worktree"])
        self.assertEqual(v.HUMAN_GATE, plan(working(), event)["action"])

        atlas = default_atlas()
        real_entry = atlas.entry

        def ungated(code):
            entry = dict(real_entry(code))
            entry["human_gate_required"] = False
            entry["safe_default"] = v.RETRY_SAME_AGENT if code == "parallel_writer_collision" else entry["safe_default"]
            return entry

        with patch.object(type(atlas), "entry", staticmethod(ungated)):
            self.assertNotEqual(v.HUMAN_GATE, plan(working(), event, atlas)["action"])

    def test_claim_comparison_is_what_detects_a_false_pass(self):
        probe_result = r.with_fact(h.worker_result(verified=False), "remote_sha",
                                   r.fact(h.HEAD, v.REPORTED, "agent:fenced"))
        probes = [_Stub("git", {"remote_sha": h.REPAIRED})]
        _, report = verify_module.verify(probe_result, {}, probes)
        self.assertIn("verify.claim_contradicted", report["signals"])
        with patch.object(verify_module, "equivalent", lambda *args: True):
            _, blind = verify_module.verify(probe_result, {}, probes)
            self.assertEqual([], blind["signals"])

    def test_the_level_binding_is_what_stops_a_forged_verification(self):
        with self.assertRaises(r.ResultError):
            r.with_fact(h.worker_result(), "status", r.fact("PASS", v.VERIFIED, "agent:fenced"))
        with patch.object(r, "fact_problems", lambda field, item: []):
            forged = r.with_fact(h.worker_result(), "status", r.fact("PASS", v.VERIFIED, "agent:fenced"))
            self.assertEqual(v.VERIFIED, r.level(forged, "status"))


class _Stub:
    def __init__(self, name, values):
        self.name, self._values = name, values

    def run(self, result, context):
        return [verify_module.observation(field, v.VERIFIED, value, self.name)
                for field, value in self._values.items()], set()


if __name__ == "__main__":
    unittest.main()
