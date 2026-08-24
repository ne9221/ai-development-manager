"""Focused tests for the direct-dispatch-ingress request_id idempotency
primitive (manager/dispatch_requests.py), mirroring manager/test_task_claims.py's
proven approach against an in-memory double of the GCS transport."""

import threading
import unittest

from manager.dispatch_requests import (
    DispatchRequestClaimConflict, claim_dispatch_request, dispatch_request_object_name,
    mark_dispatch_request_status, read_dispatch_request_status, release_dispatch_request_claim,
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

    def test_claim_is_accepted_status_immediately_on_creation(self):
        """P0 dispatch-two-tick-final: status defaults to "accepted" in the
        SAME create-if-absent write the claim itself lands in -- durable,
        queryable request-received truth before any slow provider/quota/
        history resolution ever runs."""
        registry = MemoryClaimRegistry()
        claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("accepted", status["status"])
        self.assertIsNone(status["failure_reason"])

    def test_missing_claim_status_is_none_not_a_fabricated_status(self):
        registry = MemoryClaimRegistry()
        self.assertIsNone(read_dispatch_request_status(registry, "p1", "req-never-seen"))

    def test_mark_status_transitions_and_is_queryable(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        new_generation = mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "dispatched")
        self.assertIsNotNone(new_generation)
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("dispatched", status["status"])
        self.assertIsNone(status["failure_reason"])

    def test_mark_status_failed_carries_a_reason(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "failed", failure_reason="no eligible provider")
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("failed", status["status"])
        self.assertEqual("no eligible provider", status["failure_reason"])

    def test_mark_status_stale_generation_is_a_lost_race_not_an_exception(self):
        """Best-effort, observability-only contract: a stale generation must
        return None (lost race), never raise -- the caller's own real
        success/failure must never be masked by this side channel failing."""
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "dispatched")
        result = mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "failed", failure_reason="stale")
        self.assertIsNone(result)
        # The winning "dispatched" transition must survive untouched.
        self.assertEqual("dispatched", read_dispatch_request_status(registry, "p1", "req-1")["status"])

    def test_mark_status_backend_unavailable_returns_none_not_exception(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        registry.unavailable = True
        self.assertIsNone(mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "dispatched"))

    def test_legacy_record_without_status_field_defaults_to_accepted(self):
        """A record written before this change (no status/failure_reason
        fields at all) must still validate and read as "accepted" rather
        than being treated as malformed -- full backward compatibility with
        every dispatch-request claim already live in GCS."""
        registry = MemoryClaimRegistry()
        registry.document = {"schema_version": "0.1.0", "project_id": "p1", "request_id": "req-legacy",
                              "task_id": "dispatch-req-legacy", "command_id": "dispatch-req-legacy",
                              "created_at": "2026-08-16T00:00:00Z"}
        registry.generation = 1
        status = read_dispatch_request_status(registry, "p1", "req-legacy")
        self.assertEqual("accepted", status["status"])
        self.assertIsNone(status["failure_reason"])

    def test_read_status_of_malformed_record_fails_closed(self):
        registry = MemoryClaimRegistry()
        registry.document = {"schema_version": "0.1.0", "project_id": "p1"}  # missing required fields
        registry.generation = 1
        with self.assertRaises(TaskError):
            read_dispatch_request_status(registry, "p1", "req-malformed")

    def test_invalid_status_value_rejected(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        with self.assertRaises(TaskError):
            mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "bogus-status")


if __name__ == "__main__":
    unittest.main()
