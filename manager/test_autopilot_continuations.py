"""Focused tests for the Autopilot continuation decision idempotency primitive
(manager/autopilot_continuations.py), testing against an in-memory double of GCS."""

import threading
import unittest
from copy import deepcopy

from manager.autopilot_continuations import (
    STATE_ATTENTION_REQUIRED,
    STATE_CLAIMED,
    STATE_COMPLETED,
    STATE_DISPATCHED,
    STATE_DISPATCHING,
    STATE_FAILED_SAFE,
    autopilot_continuation_object_name,
    check_autopilot_continuation,
    claim_autopilot_continuation,
    mark_continuation_attention_required,
    mark_continuation_completed,
    mark_continuation_dispatched,
    mark_continuation_dispatching,
    mark_continuation_failed_safe,
)
from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError
from manager.test_task_claims import AmbiguousThenUnreadableRegistry, MemoryClaimRegistry


class MemoryClaimRegistryWithCAS(MemoryClaimRegistry):
    """Adds compare_and_swap/cas to the shared in-memory GCS double so the
    Autopilot continuation CAS state machine (CLAIMED -> DISPATCHING -> ...)
    can be exercised without touching real GCS. manager/gcs_lock_registry.py's
    real GCSLockRegistry already implements compare_and_swap/cas (it is the
    same `_write` primitive create_if_absent uses, just with a non-zero
    expected generation) -- this only teaches the existing shared test double
    the same capability, since task_claims.py's own claim flow never needed
    a post-create update and so never exercised it before."""

    def compare_and_swap(self, expected_generation, document):
        with self.mutex:
            if self.unavailable:
                raise TaskError("simulated backend unavailable")
            if self.document is None or self.generation != expected_generation:
                raise RegistryConflict("GCS generation precondition failed")
            self.generation += 1
            self.document = deepcopy(document)
            return self.generation

    def cas(self, expected_generation, document):
        return self.compare_and_swap(expected_generation, document)


class AutopilotContinuationClaimTests(unittest.TestCase):
    def test_first_submission_is_claimed_and_creates_record(self):
        registry = MemoryClaimRegistry()
        result = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
        )
        self.assertTrue(result["claimed"])
        self.assertEqual("exec-src-1", result["source_execution_id"])
        self.assertEqual("task-src-1", result["source_task_id"])
        self.assertEqual("task-next-1", result["next_task_id"])
        self.assertEqual("cmd-next-1", result["next_command_id"])
        self.assertEqual(1, result["continuation_count"])
        self.assertEqual(1, result["generation"])

    def test_duplicate_submission_is_idempotent_no_second_claim(self):
        registry = MemoryClaimRegistry()
        first = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
        )
        second = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:10Z"
        )
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(first["next_task_id"], second["next_task_id"])
        self.assertEqual(first["next_command_id"], second["next_command_id"])
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual("2026-08-19T10:00:00Z", second["decided_at"])

    def test_two_concurrent_claims_have_exactly_one_winner(self):
        for _ in range(20):
            registry = MemoryClaimRegistry()
            barrier = threading.Barrier(2)
            results = []

            def run():
                barrier.wait(timeout=2)
                results.append(claim_autopilot_continuation(
                    registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
                ))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
            self.assertEqual(2, len(results))
            claimed = [r for r in results if r["claimed"]]
            replayed = [r for r in results if not r["claimed"]]
            self.assertEqual(1, len(claimed))
            self.assertEqual(1, len(replayed))
            self.assertEqual(claimed[0]["next_task_id"], replayed[0]["next_task_id"])

    def test_ambiguous_create_self_recognizes_own_success(self):
        registry = MemoryClaimRegistry()
        registry.ambiguous_queue.append(ConnectionError("simulated timeout"))
        result = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
        )
        self.assertTrue(result["claimed"])
        self.assertEqual(1, result["generation"])

    def test_ambiguous_create_and_reread_failure_fails_closed(self):
        registry = AmbiguousThenUnreadableRegistry()
        with self.assertRaises(TaskError):
            claim_autopilot_continuation(
                registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
            )
        self.assertIsNone(registry.document)

    def test_check_autopilot_continuation_read_only(self):
        registry = MemoryClaimRegistry()
        self.assertIsNone(check_autopilot_continuation(registry, "p1", "exec-src-1"))
        claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
        )
        checked = check_autopilot_continuation(registry, "p1", "exec-src-1")
        self.assertIsNotNone(checked)
        self.assertEqual("task-next-1", checked["next_task_id"])
        self.assertEqual(1, checked["generation"])

    def test_object_name_is_scoped_and_safe(self):
        self.assertEqual("autopilot-continuations/p1/exec-1.json",
                         autopilot_continuation_object_name("p1", "exec-1"))
        with self.assertRaises(TaskError):
            autopilot_continuation_object_name("../p1", "exec-1")

    def test_fresh_claim_starts_in_claimed_state(self):
        registry = MemoryClaimRegistryWithCAS()
        result = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
        )
        self.assertEqual(STATE_CLAIMED, result["state"])


class ContinuationStateMachineTests(unittest.TestCase):
    """Codex P0-4: CAS'd recoverable state machine
    (CLAIMED -> DISPATCHING -> DISPATCHED, with FAILED_SAFE and
    ATTENTION_REQUIRED escape hatches) replacing a single irreversible CAS
    flag that could permanently strand a continuation after a crash."""

    def _claimed(self, registry=None):
        registry = registry or MemoryClaimRegistryWithCAS()
        claim = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:00:00Z"
        )
        return registry, claim

    def test_claimed_to_dispatching_to_dispatched_happy_path(self):
        registry, claim = self._claimed()
        dispatching = mark_continuation_dispatching(registry, claim)
        self.assertEqual(STATE_DISPATCHING, dispatching["state"])
        dispatched = mark_continuation_dispatched(registry, dispatching)
        self.assertEqual(STATE_DISPATCHED, dispatched["state"])
        # Persisted, not just the in-memory return value.
        reread = check_autopilot_continuation(registry, "p1", "exec-src-1")
        self.assertEqual(STATE_DISPATCHED, reread["state"])

    def test_invalid_transition_from_dispatched_fails_closed(self):
        registry, claim = self._claimed()
        dispatching = mark_continuation_dispatching(registry, claim)
        dispatched = mark_continuation_dispatched(registry, dispatching)
        with self.assertRaises(TaskError):
            mark_continuation_dispatching(registry, dispatched)

    def test_prelaunch_failure_before_command_creation_is_recoverable(self):
        """AG P0-4 adversarial test: 'failure before Command creation'.
        A CLAIMED (or DISPATCHING) record proven to have failed before any
        Command existed transitions to FAILED_SAFE, and a fresh claim attempt
        for the SAME source_execution_id (simulating a retried poll) is
        allowed to recover and proceed -- reusing the same CAS slot, not
        creating a second continuation."""
        registry, claim = self._claimed()
        dispatching = mark_continuation_dispatching(registry, claim)
        failed = mark_continuation_failed_safe(registry, dispatching, reason="dispatcher raised before Command write")
        self.assertEqual(STATE_FAILED_SAFE, failed["state"])

        retry = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-2", "cmd-next-2", 1, "2026-08-19T10:05:00Z"
        )
        self.assertTrue(retry["claimed"])
        self.assertEqual(STATE_CLAIMED, retry["state"])
        # The recovered record carries the NEW attempt's identity, not the
        # stale failed one's.
        self.assertEqual("task-next-2", retry["next_task_id"])
        self.assertEqual("cmd-next-2", retry["next_command_id"])

    def test_crash_immediately_after_claim_is_observable_and_not_auto_retried(self):
        """AG P0-4 adversarial test: 'crash immediately after claim'.
        A record stuck in bare CLAIMED (no DISPATCHING/FAILED_SAFE reached)
        proves nothing about whether a concurrent attempt is still live --
        it must be surfaced as an unresolved claim, never silently reused as
        if it were a proven-safe FAILED_SAFE retry."""
        registry, claim = self._claimed()
        replay = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-1", "cmd-next-1", 1, "2026-08-19T10:05:00Z"
        )
        self.assertFalse(replay["claimed"])
        self.assertEqual(STATE_CLAIMED, replay["state"])

    def test_timeout_with_unknown_outcome_marks_attention_required_and_is_not_auto_retried(self):
        """AG P0-4 adversarial test: 'timeout with unknown outcome' /
        'outcome-evidence write failure'. ATTENTION_REQUIRED must be a dead
        end for automatic retry -- unlike FAILED_SAFE, a later claim attempt
        must NOT recover it, since dispatch may or may not have actually
        happened."""
        registry, claim = self._claimed()
        dispatching = mark_continuation_dispatching(registry, claim)
        attention = mark_continuation_attention_required(registry, dispatching, reason="Command write timed out")
        self.assertEqual(STATE_ATTENTION_REQUIRED, attention["state"])

        replay = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-2", "cmd-next-2", 1, "2026-08-19T10:05:00Z"
        )
        self.assertFalse(replay["claimed"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, replay["state"])
        # Original (unresolved) identity preserved, not silently replaced.
        self.assertEqual("task-next-1", replay["next_task_id"])

    def test_successful_dispatch_then_restart_replay_is_idempotent(self):
        """AG P0-4 adversarial test: 'successful dispatch + restart'."""
        registry, claim = self._claimed()
        dispatching = mark_continuation_dispatching(registry, claim)
        mark_continuation_dispatched(registry, dispatching)

        replay = claim_autopilot_continuation(
            registry, "p1", "exec-src-1", "task-src-1", "task-next-2", "cmd-next-2", 1, "2026-08-19T10:05:00Z"
        )
        self.assertFalse(replay["claimed"])
        self.assertEqual(STATE_DISPATCHED, replay["state"])
        self.assertEqual("task-next-1", replay["next_task_id"])  # original winner, not the replay's request

    def test_mark_dispatched_from_claimed_without_dispatching_fails_closed(self):
        registry, claim = self._claimed()
        with self.assertRaises(TaskError):
            mark_continuation_dispatched(registry, claim)

    def test_mark_completed_from_dispatched(self):
        registry, claim = self._claimed()
        dispatching = mark_continuation_dispatching(registry, claim)
        dispatched = mark_continuation_dispatched(registry, dispatching)
        completed = mark_continuation_completed(registry, dispatched)
        self.assertEqual(STATE_COMPLETED, completed["state"])

    def test_stale_generation_transition_fails_closed(self):
        """Two holders of the same CLAIMED record race to transition; only
        the first CAS wins, the second must fail closed rather than
        silently overwrite the winner's DISPATCHING state."""
        registry, claim = self._claimed()
        first = mark_continuation_dispatching(registry, claim)
        self.assertEqual(STATE_DISPATCHING, first["state"])
        with self.assertRaises(TaskError):
            mark_continuation_dispatching(registry, claim)  # stale generation


if __name__ == "__main__":
    unittest.main()
