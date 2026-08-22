import unittest
from copy import deepcopy

from manager.continuation_decision import (
    decide_continuation, decide_dispatch, decide_next_dispatch, decide_retry, decide_validation,
    dispatch_accepted, evidence_ready, plan_task, provider_started, provider_terminal_evidence,
)


def _drive_to_pass(store, project_id, chain_id):
    apply(store, project_id, chain_id, plan_task())
    apply(store, project_id, chain_id,
          decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False))
    apply(store, project_id, chain_id, dispatch_accepted())
    apply(store, project_id, chain_id, provider_started())
    apply(store, project_id, chain_id, provider_terminal_evidence())
    apply(store, project_id, chain_id, evidence_ready())
    return apply(store, project_id, chain_id,
                 decide_validation("validating", {"result": "pass", "signals": {"tests": "ok"}}))
from manager.continuation_orchestrator import apply, create_chain, is_halted, load_chain
from manager.continuation_states import ContinuationError


class InMemoryStore:
    """Minimal fake matching manager.tasks.DriveRecords' get/put shape
    (`store.get(area, project_id, id)` / `store.put(area, project_id, id,
    doc)`), so the orchestrator can be tested without touching Drive."""

    def __init__(self):
        self.records = {}

    def get(self, area, project_id, record_id):
        try:
            return deepcopy(self.records[(area, project_id, record_id)])
        except KeyError:
            raise KeyError(record_id)

    def put(self, area, project_id, record_id, document):
        self.records[(area, project_id, record_id)] = deepcopy(document)


class CreateChainTests(unittest.TestCase):
    def test_creates_idle_record(self):
        store = InMemoryStore()
        record = create_chain(store, "proj", "chain-1", "req-1", max_depth=3, max_retries=2)
        self.assertEqual(record["state"], "idle")
        self.assertEqual(record["depth"], 0)
        self.assertEqual(record["history"], [])

    def test_duplicate_chain_id_rejected(self):
        store = InMemoryStore()
        create_chain(store, "proj", "chain-1", "req-1", max_depth=3, max_retries=2)
        with self.assertRaises(ContinuationError):
            create_chain(store, "proj", "chain-1", "req-2", max_depth=3, max_retries=2)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        create_chain(self.store, "proj", "chain-1", "req-1", max_depth=2, max_retries=1)

    def test_apply_advances_state_and_records_history(self):
        record = apply(self.store, "proj", "chain-1", plan_task())
        self.assertEqual(record["state"], "task_planned")
        self.assertEqual(len(record["history"]), 1)
        self.assertEqual(record["history"][0]["event"], "task_planned")

    def test_stale_decision_rejected(self):
        apply(self.store, "proj", "chain-1", plan_task())
        with self.assertRaises(ContinuationError):
            # plan_task() always expects from_state "idle"; chain has moved on.
            apply(self.store, "proj", "chain-1", plan_task())

    def test_lineage_ids_preserved_across_applies(self):
        apply(self.store, "proj", "chain-1", plan_task(), task_id="task-1")
        record = apply(self.store, "proj", "chain-1",
                        decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False))
        self.assertEqual(record["task_id"], "task-1")
        record = apply(self.store, "proj", "chain-1", dispatch_accepted(), execution_id="exec-1")
        self.assertEqual(record["task_id"], "task-1")
        self.assertEqual(record["execution_id"], "exec-1")

    def test_patch_fields_applied(self):
        apply(self.store, "proj", "chain-1", plan_task())
        apply(self.store, "proj", "chain-1",
              decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False))
        apply(self.store, "proj", "chain-1", dispatch_accepted())
        apply(self.store, "proj", "chain-1", provider_started())
        apply(self.store, "proj", "chain-1", provider_terminal_evidence())
        from manager.continuation_decision import evidence_ready
        apply(self.store, "proj", "chain-1", evidence_ready())
        record = apply(self.store, "proj", "chain-1",
                        decide_validation("validating", {"result": "pass", "signals": {"tests": "ok"}}))
        self.assertEqual(record["state"], "pass")
        record = apply(self.store, "proj", "chain-1", decide_continuation("pass", depth=0, max_depth=2))
        self.assertEqual(record["depth"], 1)
        self.assertEqual(record["state"], "next_task_ready")


class DepthAndRetryBoundTests(unittest.TestCase):
    def test_depth_limit_halts_chain(self):
        store = InMemoryStore()
        create_chain(store, "proj", "chain-2", "req-2", max_depth=1, max_retries=0)
        record = _drive_to_pass(store, "proj", "chain-2")
        self.assertEqual(record["state"], "pass")
        # Simulate a chain that has already exhausted its one allowed
        # automatic continuation (depth == max_depth) reaching PASS again.
        record = apply(store, "proj", "chain-2",
                        decide_continuation("pass", depth=record["max_depth"], max_depth=record["max_depth"]))
        self.assertEqual(record["state"], "stop_requires_user")
        self.assertTrue(is_halted(record))

    def test_retry_bound_eventually_halts(self):
        store = InMemoryStore()
        create_chain(store, "proj", "chain-3", "req-3", max_depth=5, max_retries=1)
        record = load_chain(store, "proj", "chain-3")
        self.assertFalse(is_halted(record))
        d = decide_retry("fail", retryable=True, retry_count=record["max_retries"], max_retries=record["max_retries"])
        self.assertEqual(d.to_state, "stop_requires_user")


class MutualExclusionTests(unittest.TestCase):
    def test_production_actor_hold_is_not_a_stop(self):
        d = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                  is_production_mutating=True, active_production_mutating_count=2)
        self.assertEqual(d.to_state, "next_task_ready")
        self.assertNotIn(d.to_state, ("stop_requires_user", "blocked"))


if __name__ == "__main__":
    unittest.main()
