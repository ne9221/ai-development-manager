"""Focused tests for the Autopilot continuation decision idempotency primitive
(manager/autopilot_continuations.py), testing against an in-memory double of GCS."""

import threading
import unittest

from manager.autopilot_continuations import (
    autopilot_continuation_object_name,
    check_autopilot_continuation,
    claim_autopilot_continuation,
)
from manager.tasks import TaskError
from manager.test_task_claims import AmbiguousThenUnreadableRegistry, MemoryClaimRegistry


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


if __name__ == "__main__":
    unittest.main()
