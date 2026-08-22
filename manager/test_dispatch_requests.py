"""Focused tests for the direct-dispatch-ingress request_id idempotency
primitive (manager/dispatch_requests.py), mirroring manager/test_task_claims.py's
proven approach against an in-memory double of the GCS transport."""

import threading
import unittest

from manager.dispatch_requests import (
    DispatchRequestClaimConflict, claim_dispatch_request, dispatch_request_object_name,
    release_dispatch_request_claim,
)
from manager.tasks import TaskError
from manager.test_task_claims import AmbiguousThenUnreadableRegistry, MemoryClaimRegistry


class DispatchRequestClaimTests(unittest.TestCase):
    def test_first_submission_is_claimed_and_creates_identity(self):
        registry = MemoryClaimRegistry()
        result = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        self.assertTrue(result["claimed"])
        self.assertEqual("dispatch-req-1", result["task_id"])
        self.assertEqual("dispatch-req-1", result["command_id"])
        self.assertEqual(1, result["generation"])
        self.assertTrue(result["created_by_this_call"])

    def test_same_request_id_retry_is_idempotent_no_second_claim(self):
        registry = MemoryClaimRegistry()
        first = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        second = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:05Z")
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["command_id"], second["command_id"])
        self.assertEqual(first["generation"], second["generation"])
        # The retry's own (later) created_at must never overwrite the winner's.
        self.assertEqual("2026-08-17T00:00:00Z", second["created_at"])

    def test_two_concurrent_identical_requests_have_exactly_one_claimant(self):
        for _ in range(20):
            registry = MemoryClaimRegistry()
            barrier = threading.Barrier(2)
            results = []

            def run():
                barrier.wait(timeout=2)
                results.append(claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z"))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
            self.assertEqual(2, len(results))
            claimed = [r for r in results if r["claimed"]]
            replayed = [r for r in results if not r["claimed"]]
            self.assertEqual(1, len(claimed))
            self.assertEqual(1, len(replayed))
            self.assertEqual(claimed[0]["task_id"], replayed[0]["task_id"])
            self.assertEqual(claimed[0]["command_id"], replayed[0]["command_id"])

    def test_ambiguous_create_self_recognizes_own_success(self):
        registry = MemoryClaimRegistry()
        registry.ambiguous_queue.append(ConnectionError("simulated client timeout after server-side success"))
        result = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        self.assertTrue(result["claimed"])
        self.assertEqual(1, result["generation"])
        self.assertFalse(result["created_by_this_call"])

    def test_generation_matched_release_makes_pre_artifact_claim_retryable(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        released = release_dispatch_request_claim(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", claim["generation"])
        self.assertTrue(released["released"])
        retry = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:01:00Z")
        self.assertTrue(retry["claimed"])
        self.assertTrue(retry["created_by_this_call"])

    def test_stale_release_never_deletes_newer_claim(self):
        registry = MemoryClaimRegistry()
        first = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        release_dispatch_request_claim(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", first["generation"])
        newer = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:01:00Z")
        with self.assertRaises(DispatchRequestClaimConflict):
            release_dispatch_request_claim(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", first["generation"])
        self.assertEqual(newer["generation"], registry.generation)
        self.assertIsNotNone(registry.document)

    def test_ambiguous_create_and_reread_failure_fails_closed(self):
        registry = AmbiguousThenUnreadableRegistry()
        with self.assertRaises(TaskError):
            claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        self.assertIsNone(registry.document)

    def test_backend_permission_denied_fails_closed(self):
        registry = MemoryClaimRegistry()
        registry.unavailable = True
        with self.assertRaises(TaskError):
            claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")

    def test_object_name_is_scoped_and_collision_free(self):
        self.assertEqual("dispatch-requests/p1/req-1.json", dispatch_request_object_name("p1", "req-1"))
        with self.assertRaises(TaskError):
            dispatch_request_object_name("../p1", "req-1")


if __name__ == "__main__":
    unittest.main()
