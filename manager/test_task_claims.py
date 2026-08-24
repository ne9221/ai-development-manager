"""Focused tests for the Slice 3A task-claim primitive.

These exercise task_claims.py's control flow against an in-memory double of
the GCS transport. The double proves the *logic* (winner selection, ambiguous
retry, ABA-safe release, fail-closed behavior) is correct given the documented
GCS `ifGenerationMatch` precondition contract: a conditional write/delete on
one object either lands or is rejected, atomically, server-side. The double
does not itself prove cross-machine atomicity -- that guarantee is GCS's, not
this test's. What the double *does* prove under real threading (see
test_two_concurrent_claims_have_exactly_one_winner) is that task_claims.py's
control flow cannot manufacture a second winner even when two callers race
past the create call at the same instant, which is the same interleaving
shape a real ifGenerationMatch precondition collapses to a single winner on.
"""

import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone

from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError
from manager.task_claims import (
    TaskClaimConflict, check_task_execution_claim, claim_task_execution,
    release_task_execution_claim, task_claim_object_name,
)


NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


class MemoryClaimRegistry:
    """In-memory double of one GCS object's conditional read/create/delete."""

    def __init__(self):
        self.mutex = threading.Lock()
        self.document = None
        self.generation = 0
        self.ambiguous_queue = []   # exceptions to raise on the NEXT create_if_absent, after the write "lands"
        self.unavailable = False
        self.read_unavailable = False

    def create_if_absent(self, document):
        with self.mutex:
            if self.unavailable:
                raise TaskError("simulated backend unavailable")
            if self.document is not None:
                raise RegistryConflict("GCS generation precondition failed")
            self.generation += 1
            self.document = deepcopy(document)
            landed_generation = self.generation
            pending = self.ambiguous_queue.pop(0) if self.ambiguous_queue else None
        if pending is not None:
            # Real GCSLockRegistry._write wraps any non-precondition transport
            # failure (timeout, connection reset, 5xx) in TaskError before it
            # escapes -- only TaskError/RegistryConflict ever surface here.
            raise TaskError("simulated ambiguous transport failure") from pending
        return landed_generation

    def read(self):
        with self.mutex:
            if self.read_unavailable:
                raise TaskError("simulated read unavailable")
            if self.document is None:
                raise TaskError("simulated GCS 404")
            return deepcopy(self.document), self.generation, NOW

    def read_if_exists(self):
        with self.mutex:
            if self.read_unavailable:
                raise TaskError("simulated read unavailable")
            if self.document is None:
                return None
            return deepcopy(self.document), self.generation, NOW

    def delete_if_generation_matches(self, expected_generation):
        with self.mutex:
            if self.unavailable:
                raise TaskError("simulated backend unavailable")
            if self.document is None or self.generation != expected_generation:
                raise RegistryConflict("GCS generation precondition failed on delete")
            self.document = None
            return True

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


class AmbiguousThenUnreadableRegistry(MemoryClaimRegistry):
    """create_if_absent is ambiguous (server outcome unknown), and the
    follow-up re-read used to resolve that ambiguity also fails."""

    def create_if_absent(self, document):
        raise TaskError("simulated ambiguous transport failure")

    def read_if_exists(self):
        raise TaskError("simulated read unavailable during ambiguous resolution")


class TaskClaimTests(unittest.TestCase):
    def test_first_claim_success(self):
        registry = MemoryClaimRegistry()
        result = claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        self.assertEqual("exec-a", result["execution_id"])
        self.assertEqual(1, result["generation"])

    def test_same_execution_retry_is_idempotent_no_second_claim(self):
        registry = MemoryClaimRegistry()
        first = claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        second = claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:01Z")
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(first["claimed_at"], second["claimed_at"])  # original claim preserved, no second write

    def test_different_execution_conflict_loser_does_not_overwrite(self):
        registry = MemoryClaimRegistry()
        claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        with self.assertRaisesRegex(TaskClaimConflict, "already claimed by execution exec-a"):
            claim_task_execution(registry, "p1", "t1", "exec-b", "claude", "2026-08-13T00:00:01Z")
        current, generation, _ = registry.read_if_exists()
        self.assertEqual("exec-a", current["execution_id"])
        self.assertEqual(1, generation)

    def test_two_concurrent_claims_have_exactly_one_winner(self):
        for _ in range(20):
            registry = MemoryClaimRegistry()
            barrier = threading.Barrier(2)
            results, errors = [], []

            def run(execution_id):
                barrier.wait(timeout=2)
                try:
                    results.append(claim_task_execution(registry, "p1", "t1", execution_id, "codex", "2026-08-13T00:00:00Z")["execution_id"])
                except TaskClaimConflict as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=run, args=(execution_id,)) for execution_id in ("exec-a", "exec-b")]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
            self.assertEqual(1, len(results))
            self.assertEqual(1, len(errors))
            self.assertEqual(results[0], registry.document["execution_id"])

    def test_ambiguous_create_self_recognizes_own_success(self):
        registry = MemoryClaimRegistry()
        registry.ambiguous_queue.append(ConnectionError("simulated client timeout after server-side success"))
        result = claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        self.assertEqual("exec-a", result["execution_id"])
        self.assertEqual(1, result["generation"])
        # exactly one object was ever created despite the ambiguous exception
        self.assertEqual(1, registry.generation)

    def test_ambiguous_create_finds_other_owner_conflicts(self):
        registry = MemoryClaimRegistry()
        claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")

        class AmbiguousOnCreate(MemoryClaimRegistry):
            def create_if_absent(self, document):
                raise TaskError("simulated ambiguous transport failure")

        proxy = AmbiguousOnCreate()
        proxy.document, proxy.generation = registry.document, registry.generation
        with self.assertRaisesRegex(TaskClaimConflict, "already claimed by execution exec-a"):
            claim_task_execution(proxy, "p1", "t1", "exec-b", "claude", "2026-08-13T00:00:01Z")

    def test_ambiguous_create_and_reread_failure_fails_closed(self):
        registry = AmbiguousThenUnreadableRegistry()
        with self.assertRaises(TaskError):
            claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        self.assertIsNone(registry.document)

    def test_backend_permission_denied_fails_closed(self):
        registry = MemoryClaimRegistry(); registry.unavailable = True
        with self.assertRaises(TaskError):
            claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")

    def test_backend_5xx_fails_closed(self):
        registry = MemoryClaimRegistry(); registry.read_unavailable = True
        registry.ambiguous_queue.append(RuntimeError("simulated 500"))
        with self.assertRaises(TaskError):
            claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")

    def test_malformed_claim_fails_closed(self):
        registry = MemoryClaimRegistry()
        registry.document = {"schema_version": "0.1.0", "project_id": "p1"}  # missing required fields
        registry.generation = 1
        with self.assertRaises(TaskError):
            check_task_execution_claim(registry, "p1", "t1")
        with self.assertRaises(TaskError):
            claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")

    def test_generation_matched_release(self):
        registry = MemoryClaimRegistry()
        claimed = claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        result = release_task_execution_claim(registry, "p1", "t1", "exec-a", claimed["generation"])
        self.assertTrue(result["released"])
        self.assertIsNone(registry.document)
        # released -> a fresh claim can now win
        second = claim_task_execution(registry, "p1", "t1", "exec-b", "claude", "2026-08-13T00:01:00Z")
        self.assertEqual("exec-b", second["execution_id"])

    def test_stale_generation_aba_rollback_cannot_delete_new_owner(self):
        registry = MemoryClaimRegistry()
        first = claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        release_task_execution_claim(registry, "p1", "t1", "exec-a", first["generation"])
        second = claim_task_execution(registry, "p1", "t1", "exec-b", "claude", "2026-08-13T00:01:00Z")
        # exec-a's stale rollback arrives late, holding its own old generation
        result = release_task_execution_claim(registry, "p1", "t1", "exec-a", first["generation"])
        self.assertFalse(result["released"])
        self.assertEqual("exec-b", registry.document["execution_id"])
        # even if a stale caller somehow still claims the SAME execution_id key
        # at the old generation, the generation mismatch must still block it
        with self.assertRaises(TaskClaimConflict):
            release_task_execution_claim(registry, "p1", "t1", "exec-b", first["generation"])
        self.assertEqual("exec-b", registry.document["execution_id"])

    def test_release_backend_unavailable_fails_closed(self):
        registry = MemoryClaimRegistry()
        claimed = claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        registry.read_unavailable = True
        with self.assertRaises(TaskError):
            release_task_execution_claim(registry, "p1", "t1", "exec-a", claimed["generation"])

    def test_canonical_key_is_path_and_case_sensitive_no_collisions(self):
        self.assertEqual("task-claims/p1/t1.json", task_claim_object_name("p1", "t1"))
        self.assertNotEqual(task_claim_object_name("p1", "t1"), task_claim_object_name("P1", "T1"))
        for project_id, task_id in (("p/1", "t1"), ("p1", "../t1"), ("p1", "t1/../t2"), ("p1", "t1.json/x")):
            with self.subTest(project_id=project_id, task_id=task_id), self.assertRaises(TaskError):
                task_claim_object_name(project_id, task_id)
        # a path-escaping combination can never collapse onto an unrelated task's key
        self.assertNotEqual(task_claim_object_name("p1", "t1"), task_claim_object_name("p1", "t2"))
        self.assertNotEqual(task_claim_object_name("p1", "t1"), task_claim_object_name("p2", "t1"))

    def test_check_is_read_only_and_returns_none_when_unclaimed(self):
        registry = MemoryClaimRegistry()
        self.assertIsNone(check_task_execution_claim(registry, "p1", "t1"))
        claim_task_execution(registry, "p1", "t1", "exec-a", "codex", "2026-08-13T00:00:00Z")
        result = check_task_execution_claim(registry, "p1", "t1")
        self.assertEqual("exec-a", result["execution_id"])
        # inspecting never writes
        self.assertEqual(1, registry.generation)


if __name__ == "__main__":
    unittest.main()
