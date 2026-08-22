import unittest
from copy import deepcopy

from manager.continuation_decision import (
    decide_continuation, decide_dispatch, decide_next_dispatch, decide_retry, decide_validation,
    dispatch_accepted, evidence_ready, plan_task, provider_started, provider_terminal_evidence,
)
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


def _run_slice_to_pass(store, project_id, chain_id, request_id=None):
    """Drives one slice through a single successful attempt to PASS. Pass
    request_id to also plant the slice from IDLE (the chain's very first
    slice); omit it when the chain is already sitting at TASK_PLANNED
    (e.g. a slice just created by a prior next_dispatch_check apply)."""
    if request_id is not None:
        apply(store, project_id, chain_id, plan_task(), request_id=request_id)
    apply(store, project_id, chain_id,
          decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                           is_production_mutating=False, active_production_mutating_count=0))
    apply(store, project_id, chain_id, dispatch_accepted())
    apply(store, project_id, chain_id, provider_started())
    apply(store, project_id, chain_id, provider_terminal_evidence())
    apply(store, project_id, chain_id, evidence_ready())
    return apply(store, project_id, chain_id,
                 decide_validation("validating", {"result": "pass", "signals": {"tests": "ok"}}))


class CreateChainTests(unittest.TestCase):
    def test_creates_idle_record(self):
        store = InMemoryStore()
        record = create_chain(store, "proj", "chain-1", "req-1", max_depth=3, max_retries=2)
        self.assertEqual(record["state"], "idle")
        self.assertEqual(record["depth"], 0)
        self.assertEqual(record["history"], [])
        self.assertEqual(record["root_request_id"], "req-1")
        self.assertEqual(record["slices"], [])

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
        record = apply(self.store, "proj", "chain-1", plan_task(), request_id="req-1-slice-0")
        self.assertEqual(record["state"], "task_planned")
        self.assertEqual(len(record["history"]), 1)
        self.assertEqual(record["history"][0]["event"], "task_planned")

    def test_stale_decision_rejected(self):
        apply(self.store, "proj", "chain-1", plan_task(), request_id="req-1-slice-0")
        with self.assertRaises(ContinuationError):
            # plan_task() always expects from_state "idle"; chain has moved on.
            apply(self.store, "proj", "chain-1", plan_task(), request_id="req-1-slice-0-again")

    def test_new_slice_requires_a_request_id(self):
        with self.assertRaises(ContinuationError):
            apply(self.store, "proj", "chain-1", plan_task())

    def test_slice_0_may_reuse_root_request_id(self):
        # Slice 0 IS the chain's original dispatch, not a distinct
        # continuation, so it may legitimately carry the same request_id
        # as root_request_id.
        record = apply(self.store, "proj", "chain-1", plan_task(), request_id="req-1")
        self.assertEqual(record["slices"][0]["request_id"], "req-1")
        self.assertEqual(record["slices"][0]["request_id"], record["root_request_id"])

    def test_request_id_rejected_when_not_starting_a_new_slice(self):
        apply(self.store, "proj", "chain-1", plan_task(), request_id="req-1-slice-0")
        with self.assertRaises(ContinuationError):
            apply(self.store, "proj", "chain-1",
                  decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                                   is_production_mutating=False, active_production_mutating_count=0),
                  request_id="not-allowed-here")

    def test_lineage_ids_preserved_across_applies(self):
        apply(self.store, "proj", "chain-1", plan_task(), request_id="req-1-slice-0", task_id="task-1")
        record = apply(self.store, "proj", "chain-1",
                        decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                                         is_production_mutating=False, active_production_mutating_count=0))
        self.assertEqual(record["task_id"], "task-1")
        record = apply(self.store, "proj", "chain-1", dispatch_accepted(), execution_id="exec-1")
        self.assertEqual(record["task_id"], "task-1")
        self.assertEqual(record["execution_id"], "exec-1")
        self.assertEqual(record["slices"][0]["task_id"], "task-1")
        self.assertEqual(record["slices"][0]["execution_ids"], ["exec-1"])

    def test_patch_fields_applied(self):
        apply(self.store, "proj", "chain-1", plan_task(), request_id="req-1-slice-0")
        apply(self.store, "proj", "chain-1",
              decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                               is_production_mutating=False, active_production_mutating_count=0))
        apply(self.store, "proj", "chain-1", dispatch_accepted())
        apply(self.store, "proj", "chain-1", provider_started())
        apply(self.store, "proj", "chain-1", provider_terminal_evidence())
        apply(self.store, "proj", "chain-1", evidence_ready())
        record = apply(self.store, "proj", "chain-1",
                        decide_validation("validating", {"result": "pass", "signals": {"tests": "ok"}}))
        self.assertEqual(record["state"], "pass")
        record = apply(self.store, "proj", "chain-1", decide_continuation("pass", depth=0, max_depth=2))
        self.assertEqual(record["depth"], 1)
        self.assertEqual(record["state"], "next_task_ready")


class PerSliceLineageTests(unittest.TestCase):
    """Regression coverage: the chain must retain full per-slice lineage
    (request_id/task_id/execution_ids/session_id/handoff_id/outcome/
    retry_count/started_at/terminal_at) after later slices run, and a new
    slice must always use a fresh dispatch request_id."""

    def setUp(self):
        self.store = InMemoryStore()
        create_chain(self.store, "proj", "chain-lineage", "root-req", max_depth=5, max_retries=2)

    def _advance_to_next_slice(self, chain_id, next_request_id):
        d = decide_continuation("pass", depth=0, max_depth=5)
        record = apply(self.store, "proj", chain_id, d)
        d = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                  is_production_mutating=False, active_production_mutating_count=0)
        return apply(self.store, "proj", chain_id, d, request_id=next_request_id)

    def test_slice_lineage_is_retained_after_a_new_slice_starts(self):
        record = _run_slice_to_pass(self.store, "proj", "chain-lineage", request_id="slice-0-req")
        # Attach identifiers discovered mid-slice retroactively is not
        # supported by apply(); attach them as the slice progresses instead
        # via a second full drive that supplies ids, exercised below.
        record = self._advance_to_next_slice("chain-lineage", "slice-1-req")
        self.assertEqual(record["state"], "task_planned")
        self.assertEqual(len(record["slices"]), 2)

        slice_0 = record["slices"][0]
        self.assertEqual(slice_0["slice_index"], 0)
        self.assertEqual(slice_0["request_id"], "slice-0-req")
        self.assertEqual(slice_0["outcome"], "pass")
        self.assertIsNotNone(slice_0["terminal_at"])

        slice_1 = record["slices"][1]
        self.assertEqual(slice_1["slice_index"], 1)
        self.assertEqual(slice_1["request_id"], "slice-1-req")
        self.assertNotEqual(slice_1["request_id"], slice_0["request_id"])
        self.assertNotEqual(slice_1["request_id"], record["root_request_id"])
        self.assertIsNone(slice_1["outcome"])

        # Finishing slice 1 must not touch slice 0's recorded lineage.
        record = _run_slice_to_pass(self.store, "proj", "chain-lineage")
        self.assertEqual(record["slices"][0], slice_0)

    def test_new_slice_request_id_cannot_repeat_an_earlier_slice(self):
        _run_slice_to_pass(self.store, "proj", "chain-lineage", request_id="slice-0-req")
        with self.assertRaises(ContinuationError):
            self._advance_to_next_slice("chain-lineage", "slice-0-req")

    def test_execution_ids_accumulate_within_one_slice(self):
        apply(self.store, "proj", "chain-lineage", plan_task(), request_id="slice-0-req")
        apply(self.store, "proj", "chain-lineage",
              decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                               is_production_mutating=False, active_production_mutating_count=0))
        apply(self.store, "proj", "chain-lineage", dispatch_accepted(), execution_id="exec-attempt-1")
        record = apply(self.store, "proj", "chain-lineage", provider_started())
        self.assertEqual(record["slices"][0]["execution_ids"], ["exec-attempt-1"])


class RootRequestIdReuseTests(unittest.TestCase):
    """Regression for the slice 0 / continuation request_id contract:
    slice 0 (the chain's original dispatch) may equal root_request_id;
    every automatic continuation (slice_index >= 1) must mint its own
    distinct request_id and may never fall back to root_request_id or any
    earlier slice's request_id."""

    def setUp(self):
        self.store = InMemoryStore()
        self.chain_id = "chain-root-reuse"
        create_chain(self.store, "proj", self.chain_id, "root-req", max_depth=5, max_retries=1)

    def _advance_to_next_slice(self, next_request_id):
        record = apply(self.store, "proj", self.chain_id, decide_continuation("pass", depth=0, max_depth=5))
        d = decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                  is_production_mutating=False, active_production_mutating_count=0)
        return apply(self.store, "proj", self.chain_id, d, request_id=next_request_id)

    def test_slice0_request_id_equal_to_root_request_id_passes(self):
        record = _run_slice_to_pass(self.store, "proj", self.chain_id, request_id="root-req")
        self.assertEqual(record["slices"][0]["request_id"], "root-req")

    def test_slice1_request_id_equal_to_root_request_id_fails(self):
        _run_slice_to_pass(self.store, "proj", self.chain_id, request_id="root-req")
        with self.assertRaises(ContinuationError):
            self._advance_to_next_slice("root-req")

    def test_slice1_reusing_slice0_request_id_fails(self):
        _run_slice_to_pass(self.store, "proj", self.chain_id, request_id="slice-0-req")
        with self.assertRaises(ContinuationError):
            self._advance_to_next_slice("slice-0-req")

    def test_slice1_with_a_genuinely_new_request_id_passes(self):
        _run_slice_to_pass(self.store, "proj", self.chain_id, request_id="slice-0-req")
        record = self._advance_to_next_slice("slice-1-req")
        self.assertEqual(record["slices"][1]["request_id"], "slice-1-req")
        self.assertEqual(record["slices"][1]["slice_index"], 1)


class RetryResetPerSliceTests(unittest.TestCase):
    """Regression: slice 1 retries twice then PASSes; slice 2 must begin
    with retry_count=0, and slice 1's lineage must still show its two
    retries afterward."""

    def setUp(self):
        self.store = InMemoryStore()
        self.chain_id = "chain-retry-reset"
        create_chain(self.store, "proj", self.chain_id, "root-req", max_depth=5, max_retries=2)

    def _fail_then_retry(self, retry_count_before):
        apply(self.store, "proj", self.chain_id, dispatch_accepted())
        apply(self.store, "proj", self.chain_id, provider_started())
        apply(self.store, "proj", self.chain_id, provider_terminal_evidence())
        apply(self.store, "proj", self.chain_id, evidence_ready())
        apply(self.store, "proj", self.chain_id,
              decide_validation("validating", {"result": "fail", "signals": {"tests": "boom"}, "retryable": True}))
        return apply(self.store, "proj", self.chain_id,
                      decide_retry("fail", retryable=True, retry_count=retry_count_before, max_retries=2))

    def test_slice_retries_twice_then_passes_and_next_slice_resets(self):
        apply(self.store, "proj", self.chain_id, plan_task(), request_id="slice-0-req")
        apply(self.store, "proj", self.chain_id,
              decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                               is_production_mutating=False, active_production_mutating_count=0))

        record = self._fail_then_retry(retry_count_before=0)
        self.assertEqual(record["state"], "dispatching")
        self.assertEqual(record["retry_count"], 1)
        self.assertEqual(record["slices"][0]["retry_count"], 1)

        record = self._fail_then_retry(retry_count_before=1)
        self.assertEqual(record["state"], "dispatching")
        self.assertEqual(record["retry_count"], 2)
        self.assertEqual(record["slices"][0]["retry_count"], 2)

        apply(self.store, "proj", self.chain_id, dispatch_accepted())
        apply(self.store, "proj", self.chain_id, provider_started())
        apply(self.store, "proj", self.chain_id, provider_terminal_evidence())
        apply(self.store, "proj", self.chain_id, evidence_ready())
        record = apply(self.store, "proj", self.chain_id,
                        decide_validation("validating", {"result": "pass", "signals": {"tests": "ok"}}))
        self.assertEqual(record["state"], "pass")
        self.assertEqual(record["slices"][0]["outcome"], "pass")
        self.assertEqual(record["slices"][0]["retry_count"], 2, "slice 1 lineage must still show its two retries")

        record = apply(self.store, "proj", self.chain_id, decide_continuation("pass", depth=0, max_depth=5))
        record = apply(self.store, "proj", self.chain_id,
                        decide_next_dispatch("next_task_ready", action_kind="implementation", duplicate_authority=False,
                                              is_production_mutating=False, active_production_mutating_count=0),
                        request_id="slice-1-req")
        self.assertEqual(record["state"], "task_planned")
        self.assertEqual(record["retry_count"], 0, "slice 2 must begin with a fresh retry budget")
        self.assertEqual(record["slices"][1]["retry_count"], 0)
        # Slice 1's own retry history remains intact after slice 2 begins.
        self.assertEqual(record["slices"][0]["retry_count"], 2)


class DepthAndRetryBoundTests(unittest.TestCase):
    def test_depth_limit_halts_chain(self):
        store = InMemoryStore()
        create_chain(store, "proj", "chain-2", "req-2", max_depth=1, max_retries=0)
        record = _run_slice_to_pass(store, "proj", "chain-2", request_id="chain-2-slice-0")
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

    def test_initial_dispatch_hold_is_also_not_a_stop(self):
        d = decide_dispatch("task_planned", action_kind="implementation", duplicate_authority=False,
                             is_production_mutating=True, active_production_mutating_count=2)
        self.assertEqual(d.to_state, "task_planned")
        self.assertNotIn(d.to_state, ("stop_requires_user", "blocked"))


if __name__ == "__main__":
    unittest.main()
