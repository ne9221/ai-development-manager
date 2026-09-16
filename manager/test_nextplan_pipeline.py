"""Phase I: the whole pipeline over the extraction corpus, read-only.

Every corpus case is a real agent output. Run through extract -> verify ->
classify -> plan with no probes at all (nothing can be verified), the pipeline
must still produce a legal decision for each one, and never a completion.
"""

import json
import unittest
from pathlib import Path

from manager.nextplan import harness as h
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import default_atlas
from manager.nextplan.pipeline import advance, audit_trail, process, replay
from manager.nextplan.planner import new_task_state

CORPUS = Path(__file__).parent / "nextplan" / "fixtures" / "extraction"


def corpus_envelopes():
    for path in sorted(CORPUS.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        envelope = dict(case["input"])
        envelope.setdefault("agent", "claude")
        envelope["generation"] = 0
        envelope["task_id"] = "t-1"
        yield path.stem, envelope


def working():
    state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
    state.update(state=v.WORKING, phase_owner={"role": v.WORKER, "session_id": None})
    return state


class PipelineOverCorpusTests(unittest.TestCase):
    def test_every_real_agent_output_yields_a_legal_decision(self):
        atlas = default_atlas()
        for name, envelope in corpus_envelopes():
            with self.subTest(name):
                record = process(working(), envelope)
                decision = record["decision"]
                self.assertIn(decision["action"], v.ACTIONS)
                self.assertIn(decision["next_state"], v.STATES)
                self.assertTrue(decision["failure_code"] is None or decision["failure_code"] in atlas.codes)
                r.validate_result(record["verified"])

    def test_unverified_output_can_never_complete_a_task(self):
        for name, envelope in corpus_envelopes():
            with self.subTest(name):
                self.assertNotEqual(v.MARK_COMPLETE, process(working(), envelope)["decision"]["action"])

    def test_the_pipeline_is_deterministic(self):
        for name, envelope in corpus_envelopes():
            with self.subTest(name):
                self.assertEqual(process(working(), envelope)["decision"],
                                 process(working(), envelope)["decision"])

    def test_the_audit_trail_explains_the_decision(self):
        _, envelope = next(iter(corpus_envelopes()))
        trail = audit_trail(process(working(), envelope))
        for key in ("action", "reason", "failure_code", "signals", "extraction_tier", "input_digest"):
            self.assertIn(key, trail)

    def test_an_envelope_without_identity_is_refused(self):
        _, envelope = next(iter(corpus_envelopes()))
        del envelope["generation"]
        with self.assertRaises(ValueError):
            process(working(), envelope)

    def test_no_probes_means_nothing_is_verified(self):
        for name, envelope in corpus_envelopes():
            with self.subTest(name):
                record = process(working(), envelope)
                levels = {r.level(record["verified"], field) for field in r.FACT_FIELDS}
                self.assertNotIn(v.VERIFIED, levels)
                self.assertIsNone(record["verification"])


class ReplayTests(unittest.TestCase):
    def test_a_sequence_of_outputs_folds_into_one_final_state(self):
        envelopes = []
        for index, (_, envelope) in enumerate(corpus_envelopes()):
            envelope = dict(envelope, event_id=f"evt-{index}", generation=0)
            envelopes.append(envelope)
        final, records = replay(working(), envelopes[:6])
        self.assertEqual(len(records), 6)
        self.assertIn(final["state"], v.STATES)
        self.assertLessEqual(len(final["retry_history"]), 6)

    def test_replaying_the_same_events_twice_reaches_the_same_state(self):
        envelopes = [dict(envelope, event_id=f"evt-{index}", generation=0)
                     for index, (_, envelope) in enumerate(corpus_envelopes())][:5]
        first, _ = replay(working(), envelopes)
        second, _ = replay(working(), envelopes)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
