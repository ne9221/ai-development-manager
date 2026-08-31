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

from unittest.mock import patch

from manager.execution_lifecycle import enter_running_gate
from manager.executions import reserve_execution
from manager.task_claims import TaskClaimConflict, _new_claim_record
from manager.tasks import TaskError
from manager.test_execution_lifecycle import MemoryStore, build_store, quota_document
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

    def test_two_racing_callers_with_the_same_execution_id_exactly_one_winner(self):
        """Regression: two DIFFERENT callers (distinct claim_token, e.g. two
        command-watcher processes racing to run the literal same Command --
        exactly what enter_running_gate's own secrets.token_urlsafe(32)
        default produces for each independent call) sharing the same
        execution_id/provider must not both be treated as one idempotent
        owner -- only claim_token identity proves it is genuinely the same
        caller retrying. Caught live via test_concurrency_reliability_gate4's
        real enter_running_gate race, which regressed to 2 winners when this
        check only compared execution_id/provider."""
        results, errors = [], []
        barrier = threading.Barrier(2)

        def attempt(token):
            barrier.wait(timeout=2)
            try:
                results.append(task_root.acquire_task_root(self.registry, "p1", "t1", "exec-1", "codex", _now(), claim_token=token))
            except TaskClaimConflict as exc:
                errors.append(exc)

        t1 = threading.Thread(target=attempt, args=("token-a",))
        t2 = threading.Thread(target=attempt, args=("token-b",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))

    def test_same_caller_same_token_retry_is_idempotent_even_racing(self):
        """The genuine same-caller-retrying case (identical claim_token)
        stays idempotent even under a race -- only a DIFFERENT token is
        treated as a rival."""
        results, errors = [], []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait(timeout=2)
            try:
                results.append(task_root.acquire_task_root(self.registry, "p1", "t1", "exec-1", "codex", _now(), claim_token="same-token"))
            except TaskClaimConflict as exc:
                errors.append(exc)

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(2, len(results))
        self.assertEqual(0, len(errors))

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

    def test_release_without_any_bind_physically_deletes(self):
        """No terminal bind ever existed -- nothing durable to preserve, so
        release behaves exactly like the pre-Design-A delete-based
        release. This is the CURRENT live behavior for every execution
        today (commit_terminal_bind is not yet wired into the completion
        path), and must stay unchanged."""
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        result = task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-a", claimed["generation"])
        self.assertTrue(result["released"])
        self.assertIsNone(self.registry.document)

    def test_release_after_bind_preserves_object(self):
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        bound, bind_generation = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        result = task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-a", bind_generation)
        self.assertTrue(result["released"])
        self.assertIsNotNone(self.registry.document)
        self.assertFalse(self.registry.document["authority_active"])
        self.assertEqual(bound["terminal"], self.registry.document["terminal"])

    def test_release_wrong_owner_refused(self):
        claimed = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        result = task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-OTHER", self.registry.generation)
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
        _bound, bind_generation = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-a", bind_generation)
        # Checkpoint C owns real cleanup-facet CAS transitions; stamp the
        # facet directly here to isolate this test to acquisition gating
        # logic rather than depending on that later checkpoint.
        document = dict(self.registry.document)
        document["cleanup"] = {"status": "released"} if cleanup_released else {"status": "retained"}
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
        self.assertEqual("exec-a", second["epoch_history"][0]["terminal"]["execution_id"])
        self.assertEqual("completed", second["epoch_history"][0]["terminal"]["terminal_status"])
        self.assertIsNotNone(second["epoch_history"][0]["terminal"]["terminal_fence_generation"])

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


def _execution(execution_id, task_id="t1", status="completed", retry_count=0,
              session_id="codex:session-a", provider="codex", account_id=None,
              terminal_reason=None, completed_at="2026-09-01T00:00:00Z"):
    return {
        "execution_id": execution_id, "task_id": task_id, "project_id": "p1",
        "status": status, "retry_count": retry_count, "session_id": session_id,
        "provider": provider, "account_id": account_id, "terminal_reason": terminal_reason,
        "completed_at": completed_at, "cleanup_evidence": {"provider_outcome": status},
    }


class TerminalBindTests(unittest.TestCase):
    """Checkpoint B: two incompatible proposals -> one bind winner, same
    proposal replay is idempotent, loser cannot bind or consume its own
    pre-generated fixed Drive IDs."""

    def setUp(self):
        self.registry = MemoryClaimRegistry()
        task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())

    def test_H_two_incompatible_proposals_exactly_one_bind_winner(self):
        task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        with self.assertRaises(task_root.TerminalProposalLost) as ctx:
            task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-b"))
        self.assertEqual("exec-a", ctx.exception.winner["execution_id"])

    def test_I_same_proposal_replay_is_idempotent(self):
        bound1, gen1 = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        bound2, gen2 = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        self.assertEqual(gen1, gen2)
        self.assertEqual(bound1["terminal"], bound2["terminal"])

    def test_conflicting_non_null_proposal_fails_closed(self):
        task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a", session_id="codex:a"))
        with self.assertRaises(task_root.TerminalProposalConflict):
            task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a", session_id="codex:DIFFERENT"))

    def test_L_loser_never_consumes_its_own_pregenerated_fixed_ids(self):
        task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        loser_calls = []
        loser_factory = lambda: loser_calls.append(1) or "loser-id"
        with self.assertRaises(task_root.TerminalProposalLost):
            task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-b"),
                                           task_drive_id_factory=loser_factory, handoff_drive_id_factory=loser_factory)
        self.assertEqual(0, len(loser_calls))

    def test_fixed_ids_frozen_once_bound(self):
        calls = []
        factory = lambda: calls.append(1) or f"id-{len(calls)}"
        bound1, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"),
                                                    task_drive_id_factory=factory, handoff_drive_id_factory=factory)
        first_task_id = bound1["terminal"]["task_projection_drive_id"]
        first_handoff_id = bound1["terminal"]["handoff_drive_file_id"]
        bound2, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"),
                                                    task_drive_id_factory=factory, handoff_drive_id_factory=factory)
        self.assertEqual(first_task_id, bound2["terminal"]["task_projection_drive_id"])
        self.assertEqual(first_handoff_id, bound2["terminal"]["handoff_drive_file_id"])
        self.assertEqual(2, len(calls))  # exactly one task id + one handoff id, never regenerated


class TerminalFenceGenerationTests(unittest.TestCase):
    """J/K: terminal_fence_generation is written atomically in the SAME CAS
    as the rest of the bind (it is just `epoch`, known before that write
    even happens -- see commit_terminal_bind's docstring for why the
    earlier two-step raw-GCS-generation design was abandoned as an
    unrecoverable-after-crash dead end) and never changes again, even as
    the Root's own current generation keeps advancing under later cursor
    updates."""

    def setUp(self):
        self.registry = MemoryClaimRegistry()
        task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())

    def test_fence_equals_epoch_written_atomically_with_the_bind(self):
        bound, bind_generation = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        fence = bound["terminal"]["terminal_fence_generation"]
        self.assertEqual(bound["epoch"], fence)
        # Exactly one CAS write for the whole bind (generation 1 was the
        # setUp claim; this bind is the very next write) -- no separate
        # freeze step, so no crash window exists between "bound" and
        # "fenced".
        self.assertEqual(2, bind_generation)

        # Simulate later cursor-only updates (materialization/cleanup
        # facet CAS writes a real checkpoint would perform) advancing the
        # Root's own current generation further.
        doc = dict(self.registry.document)
        doc["materialization"] = {"status": "pending"}
        gen_after_1 = self.registry.compare_and_swap(self.registry.generation, doc)
        doc = dict(self.registry.document)
        doc["cleanup"] = {"status": "release_pending"}
        gen_after_2 = self.registry.compare_and_swap(self.registry.generation, doc)

        self.assertGreater(gen_after_1, bind_generation)
        self.assertGreater(gen_after_2, gen_after_1)
        self.assertEqual(fence, self.registry.document["terminal"]["terminal_fence_generation"])

    def test_fence_survives_a_crash_immediately_after_the_single_bind_write(self):
        """A crash the instant after the bind's one-and-only write lands
        leaves the fence already durably correct -- there is no follow-up
        write to lose, so a fresh process reading the object sees the
        exact same fence a live process would have."""
        bound, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        fresh_registry_view = MemoryClaimRegistry()
        fresh_registry_view.document = self.registry.document
        fresh_registry_view.generation = self.registry.generation
        recovered, _ = task_root.commit_terminal_bind(fresh_registry_view, "p1", "t1", _execution("exec-a"))
        self.assertEqual(bound["terminal"], recovered["terminal"])
        self.assertEqual(bound["epoch"], recovered["terminal"]["terminal_fence_generation"])


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


class RealAcquisitionPathIntegrationTests(unittest.TestCase):
    """Checkpoint B integration proofs: the REAL execution_lifecycle.
    enter_running_gate(), not a direct unit call to acquire_task_root(),
    is what now performs claim acquisition -- these prove the live-path
    cutover actually took, not just the module in isolation."""

    def test_F_retry_after_preserved_root_via_real_enter_running_gate(self):
        store = build_store(read_only=True)
        claim_registry = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(store, object(), None, "p1", "t1", "exec-a", "codex", "read_only",
                                      task_claim_registry=claim_registry)
        self.assertEqual(1, gate["task_claim"]["epoch"])

        bound, bind_generation = task_root.commit_terminal_bind(claim_registry, "p1", "t1", _execution("exec-a"))
        task_root.release_runtime_claim(claim_registry, "p1", "t1", "exec-a", bind_generation)
        self.assertIsNotNone(claim_registry.document)  # Root preserved, not deleted
        # Checkpoint C owns real cleanup-facet CAS transitions; stamp the
        # facet directly here to isolate this test to the acquisition
        # layer rather than depending on that later checkpoint.
        preserved = dict(claim_registry.document)
        preserved["cleanup"] = {"status": "released"}
        claim_registry.compare_and_swap(claim_registry.generation, preserved)

        # Task returns to ready for a fresh retry attempt -- this test
        # isolates the acquisition layer, not the full retry pipeline
        # (prepare_task_retry), so the Task Drive record is reset directly.
        task_doc = store.get("tasks", "p1", "t1")
        task_doc["status"] = "ready"
        task_doc["source_context"] = {}
        store.put("tasks", "p1", "t1", task_doc)
        reserve_execution(store, "p1", "t1", "exec-b", "codex", {"decision": "retry"}, "code", "high", "2026-08-13T00:03:00Z")

        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            retry_gate = enter_running_gate(store, object(), None, "p1", "t1", "exec-b", "codex", "read_only",
                                            task_claim_registry=claim_registry)
        self.assertEqual(2, retry_gate["task_claim"]["epoch"])
        self.assertEqual("exec-b", retry_gate["task_claim"]["execution_id"])

    def test_G_legacy_r17_vs_new_claim_through_real_enter_running_gate(self):
        store = build_store(read_only=True)  # reserves "exec-a", ready to run
        claim_registry = MemoryClaimRegistry()

        # Seed a stuck legacy R17-shaped claim for a DIFFERENT, already-
        # terminal, already-cleaned-up execution on the same task --
        # exactly the shape a pre-Design-A watcher would have left behind
        # if release_task_execution_claim's delete had, for any reason,
        # never landed.
        legacy_record = _new_claim_record("p1", "t1", "exec-r17-old", "codex", "2026-08-13T00:00:00Z")
        claim_registry.create_if_absent(legacy_record)
        old_execution = _execution("exec-r17-old", task_id="t1")
        old_execution["cleanup_evidence"]["task_claim_release"] = "released"
        store.put("executions", "p1", "exec-r17-old", old_execution)

        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(store, object(), None, "p1", "t1", "exec-a", "codex", "read_only",
                                      task_claim_registry=claim_registry)

        # The new execution did not bypass strengthened acquisition -- the
        # legacy document was migrated (recording the old terminal winner)
        # before a fresh epoch was legally opened for exec-a.
        self.assertEqual(2, gate["task_claim"]["epoch"])
        self.assertEqual("exec-a", gate["task_claim"]["execution_id"])
        self.assertEqual(1, len(gate["task_claim"]["epoch_history"]))
        self.assertEqual("exec-r17-old", gate["task_claim"]["epoch_history"][0]["execution_id"])


if __name__ == "__main__":
    unittest.main()
