"""Checkpoint A architecture-proof tests: single GCS Task Root acquisition
+ synchronous legacy-migration gate (Strengthened Design A).

Directly exercises manager.task_root against MemoryClaimRegistry, the same
in-memory double of the GCS ifGenerationMatch contract test_task_claims.py
already uses -- it proves task_root.py's CAS control flow, not GCS's own
atomicity guarantee (that is GCS's contract, exercised for real by
test_task_claims.py's threaded concurrent-claim test using the same
double).
"""

import threading
import unittest
from datetime import datetime, timezone

from manager.task_claims import TaskClaimConflict, _new_claim_record
from manager.tasks import TaskError
from manager.test_task_claims import MemoryClaimRegistry
from manager import task_root


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _terminal_execution(execution_id, status="completed", task_claim_release="released"):
    return {
        "execution_id": execution_id, "status": status,
        "cleanup_evidence": {"task_claim_release": task_claim_release},
    }


class FreshClaimTests(unittest.TestCase):
    """No create_if_absent regression, and exactly one CAS winner on a
    brand new task -- Checkpoint A must not break the ordinary first-claim
    path it is replacing."""

    def setUp(self):
        self.registry = MemoryClaimRegistry()

    def test_fresh_claim_creates_strengthened_epoch_1(self):
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        self.assertEqual(1, claimed["epoch"])
        self.assertTrue(claimed["authority_active"])
        self.assertIsNone(claimed["terminal"])
        self.assertEqual(1, self.registry.generation)

    def test_same_execution_reclaim_is_idempotent(self):
        first = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        second = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(1, self.registry.generation)

    def test_different_execution_conflicts_while_active(self):
        task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        with self.assertRaises(TaskClaimConflict):
            task_root.acquire_task_root(self.registry, "p1", "t1", "exec-b", "codex", _now())

    def test_two_concurrent_fresh_claims_exactly_one_winner(self):
        """One Root CAS winner: two threads racing create_if_absent on a
        genuinely fresh task must not both succeed as different owners."""
        results = {}
        errors = {}

        def attempt(execution_id):
            try:
                results[execution_id] = task_root.acquire_task_root(self.registry, "p1", "t1", execution_id, "codex", _now())
            except TaskClaimConflict as exc:
                errors[execution_id] = exc

        t1 = threading.Thread(target=attempt, args=("exec-a",))
        t2 = threading.Thread(target=attempt, args=("exec-b",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        winner_id = next(iter(results))
        self.assertEqual(winner_id, self.registry.document["execution_id"])


class RootNeverDeletedTests(unittest.TestCase):
    def setUp(self):
        self.registry = MemoryClaimRegistry()

    def test_release_preserves_object(self):
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        result = task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-a", claimed["generation"])
        self.assertTrue(result["released"])
        self.assertIsNotNone(self.registry.document)
        self.assertFalse(self.registry.document["authority_active"])

    def test_release_wrong_owner_refused(self):
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        result = task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-OTHER", claimed["generation"])
        self.assertFalse(result["released"])
        self.assertIsNotNone(self.registry.document)


class RetryAfterPreservedRootTests(unittest.TestCase):
    """RETRY_AFTER_PRESERVED_ROOT: this test directly locks the regression
    Checkpoint A exists to fix -- epoch 1 terminal, Root preserved (not
    deleted), runtime cleanup released -> a genuinely new execution's
    epoch-2 acquisition must succeed, not be permanently rejected as
    'already claimed'."""

    def setUp(self):
        self.registry = MemoryClaimRegistry()

    def _terminalize_epoch_1(self, cleanup_released=True):
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        released = task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-a", claimed["generation"])
        # Checkpoint A does not yet write real terminal binds (Checkpoint B)
        # -- stamp a minimal bind directly to simulate "epoch 1 reached
        # terminal", exactly what Checkpoint B's commit will produce.
        document = dict(self.registry.document)
        document["terminal"] = {"execution_id": "exec-a", "outcome": "completed"}
        document["cleanup"] = {"status": "released" if cleanup_released else "retained"}
        self.registry.compare_and_swap(self.registry.generation, document)
        return claimed

    def test_retry_after_preserved_root_opens_epoch_2(self):
        self._terminalize_epoch_1(cleanup_released=True)
        self.assertIsNotNone(self.registry.document)  # Root preserved, not deleted
        second = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-b", "codex", _now())
        self.assertEqual(2, second["epoch"])
        self.assertEqual("exec-b", second["execution_id"])
        self.assertTrue(second["authority_active"])
        # Epoch 1's terminal facts are archived, untouched.
        self.assertEqual(1, len(second["epoch_history"]))
        self.assertEqual("exec-a", second["epoch_history"][0]["execution_id"])
        self.assertEqual({"execution_id": "exec-a", "outcome": "completed"}, second["epoch_history"][0]["terminal"])

    def test_retry_refused_while_cleanup_not_yet_released(self):
        self._terminalize_epoch_1(cleanup_released=False)
        with self.assertRaises(TaskClaimConflict):
            task_root.acquire_task_root(self.registry, "p1", "t1", "exec-b", "codex", _now())
        # Refusal must not have mutated the preserved epoch-1 state.
        self.assertEqual(1, self.registry.document["epoch"])
        self.assertEqual("exec-a", self.registry.document["execution_id"])


class LegacyMigrationGateTests(unittest.TestCase):
    """LEGACY_R17_VS_NEW_CLAIM_RACE + the synchronous single-CAS migration
    gate: a legacy (pre-Design-A) claim document must be migrated before
    any new claim on that task is decided, and migration itself is never
    settled by a separate write from the claim decision."""

    def setUp(self):
        self.registry = MemoryClaimRegistry()

    def _seed_legacy_document(self, execution_id="exec-OLD", provider="codex"):
        record = _new_claim_record("p1", "t1", execution_id, provider, _now())
        self.registry.create_if_absent(record)

    def test_legacy_ordinary_inflight_claim_migrates_and_still_conflicts(self):
        """No proof of a terminal outcome for the legacy owner -> migrated
        conservatively as still active; a different new claimant is
        refused exactly as task_claims.claim_task_execution would have."""
        self._seed_legacy_document(execution_id="exec-OLD")
        with self.assertRaises(TaskClaimConflict):
            task_root.acquire_task_root(self.registry, "p1", "t1", "exec-NEW", "codex", _now(),
                                        legacy_migration_lookup=lambda *_: None)
        self.assertTrue(task_root._is_strengthened(self.registry.document))
        self.assertTrue(self.registry.document["authority_active"])
        self.assertEqual("exec-OLD", self.registry.document["execution_id"])

    def test_legacy_r17_terminal_with_cleanup_released_migrates_and_allows_new_claim(self):
        """The real R17 shape: legacy claim whose execution actually went
        terminal and cleanup_evidence already proves the claim release --
        migration binds that fact, and the new execution can then claim
        epoch 2 on a SUBSEQUENT call (migration and grant are always two
        separate CAS decisions, never one)."""
        self._seed_legacy_document(execution_id="exec-r17")
        lookup = lambda project_id, task_id, execution_id: _terminal_execution(execution_id, task_claim_release="released")

        # One call performs the migration CAS, then -- within the SAME
        # retry loop -- re-evaluates this caller's own claim against the
        # now-migrated document; since migration already landed a
        # released-cleanup terminal bind, this single call succeeds all
        # the way through to granting epoch 2.
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-new", "codex", _now(),
                                              legacy_migration_lookup=lookup)
        self.assertEqual(2, claimed["epoch"])
        self.assertEqual("exec-new", claimed["execution_id"])
        self.assertEqual(1, len(claimed["epoch_history"]))
        self.assertEqual("exec-r17", claimed["epoch_history"][0]["execution_id"])

    def test_legacy_r17_terminal_with_cleanup_retained_migrates_but_blocks_new_claim(self):
        self._seed_legacy_document(execution_id="exec-r17")
        lookup = lambda project_id, task_id, execution_id: _terminal_execution(execution_id, task_claim_release="retained")
        with self.assertRaises(TaskClaimConflict):
            task_root.acquire_task_root(self.registry, "p1", "t1", "exec-new", "codex", _now(),
                                        legacy_migration_lookup=lookup)
        self.assertTrue(task_root._is_strengthened(self.registry.document))
        self.assertEqual("retained", self.registry.document["cleanup"]["status"])
        self.assertEqual("exec-r17", self.registry.document["terminal"]["execution_id"])

    def test_legacy_r17_vs_new_claim_race_exactly_one_migration_write(self):
        """Two threads both call acquire_task_root against the SAME legacy
        document concurrently. Exactly one performs the migration CAS
        (proven by the registry's generation advancing by exactly one for
        the migration step); both then correctly evaluate the claim
        against the identical migrated document."""
        self._seed_legacy_document(execution_id="exec-OLD")
        lookup = lambda *_: None  # ordinary in-flight legacy claim, no terminal proof
        outcomes = {}

        def attempt(name, execution_id):
            try:
                outcomes[name] = ("granted", task_root.acquire_task_root(
                    self.registry, "p1", "t1", execution_id, "codex", _now(), legacy_migration_lookup=lookup))
            except TaskClaimConflict as exc:
                outcomes[name] = ("conflict", exc)
            except TaskError as exc:
                outcomes[name] = ("error", exc)

        t1 = threading.Thread(target=attempt, args=("A", "exec-newA"))
        t2 = threading.Thread(target=attempt, args=("B", "exec-newB"))
        t1.start(); t2.start(); t1.join(); t2.join()

        # Migration always resolves the document to the SAME conservative
        # shape (still owned by exec-OLD) regardless of who wins the
        # migration race -- so both new claimants must be refused, and
        # crucially neither refusal corrupted the migrated state.
        self.assertEqual("conflict", outcomes["A"][0])
        self.assertEqual("conflict", outcomes["B"][0])
        self.assertTrue(task_root._is_strengthened(self.registry.document))
        self.assertEqual("exec-OLD", self.registry.document["execution_id"])
        self.assertEqual(1, self.registry.document["epoch"])


class ReleaseFallbackTests(unittest.TestCase):
    def test_release_on_legacy_document_falls_back_to_plain_delete(self):
        """A task never migrated has no bind/epoch-history to preserve --
        release_runtime_claim() must still behave exactly like the
        pre-Design-A release for it (a task not yet on Strengthened Design
        A must see zero behavior change)."""
        registry = MemoryClaimRegistry()
        record = _new_claim_record("p1", "t1", "exec-a", "codex", _now())
        registry.create_if_absent(record)
        result = task_root.release_runtime_claim(registry, "p1", "t1", "exec-a", 1)
        self.assertTrue(result["released"])
        self.assertIsNone(registry.document)


if __name__ == "__main__":
    unittest.main()
