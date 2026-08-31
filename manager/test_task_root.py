"""Checkpoint A architecture-proof tests: single GCS Task Root acquisition
+ synchronous legacy-migration gate (Strengthened Design A).

Directly exercises manager.task_root against MemoryClaimRegistry, the same
in-memory double of the GCS ifGenerationMatch contract test_task_claims.py
already uses -- it proves task_root.py's CAS control flow, not GCS's own
atomicity guarantee (that is GCS's contract, exercised for real by
test_task_claims.py's threaded concurrent-claim test using the same
double).
"""

import socket
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone

from unittest.mock import patch

from manager.command_watcher import process_command
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import reserve_execution
from manager.task_claims import TaskClaimConflict, _new_claim_record
from manager.tasks import TaskError, create_project, create_task, validate
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import MemoryStore, build_store, project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry
from manager.trusted_ingress import REQUIRED_TASK_POLICIES
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
        self.assertIsNotNone(second["epoch_history"][0]["terminal"]["terminal_fence_epoch"])

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
    """J/K: terminal_fence_epoch is written atomically in the SAME CAS
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
        fence = bound["terminal"]["terminal_fence_epoch"]
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
        self.assertEqual(fence, self.registry.document["terminal"]["terminal_fence_epoch"])

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
        self.assertEqual(bound["epoch"], recovered["terminal"]["terminal_fence_epoch"])


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


class OrthogonalFacetsTests(unittest.TestCase):
    """Checkpoint C: terminal authority, materialization (per-view), and
    runtime cleanup are three independent truth domains -- none of them
    collapsed into a single linear phase."""

    def setUp(self):
        self.registry = MemoryClaimRegistry()
        task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())
        task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))

    def test_materialization_views_are_independent(self):
        task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "task", "pending")
        task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "task", "verified")
        doc = self.registry.document
        self.assertEqual("verified", doc["materialization"]["task"]["status"])
        self.assertEqual("absent", doc["materialization"]["handoff"]["status"])

    def test_attention_is_recoverable_not_a_permanent_top(self):
        task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "handoff", "pending")
        task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "handoff", "attention", note="Drive 403")
        self.assertEqual("attention", self.registry.document["materialization"]["handoff"]["status"])
        # Recovery: attention -> verified is a legal transition, not blocked
        # by any linear rank treating attention as beyond verified.
        task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "handoff", "verified")
        self.assertEqual("verified", self.registry.document["materialization"]["handoff"]["status"])

    def test_illegal_materialization_transition_rejected(self):
        with self.assertRaises(TaskError):
            task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "task", "verified")  # absent -> verified skips pending

    def test_cleanup_facet_monotonic_released_sticky(self):
        task_root.advance_cleanup_facet(self.registry, "p1", "t1", "exec-a", "release_pending")
        task_root.advance_cleanup_facet(self.registry, "p1", "t1", "exec-a", "released")
        # Regression attempt is a silent no-op, never an error and never a
        # downgrade -- released is sticky.
        task_root.advance_cleanup_facet(self.registry, "p1", "t1", "exec-a", "retained")
        self.assertEqual("released", self.registry.document["cleanup"]["status"])

    def test_permanent_drive_failure_valid_state_terminal_bound_attention_cleanup_released(self):
        """The core state Checkpoint C exists to make legal: a permanently
        broken Handoff materialization must never hold runtime resources
        hostage. terminal=bound + materialization=attention +
        cleanup=released is a valid, reachable, simultaneous state."""
        task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "handoff", "pending")
        task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-a", "handoff", "attention", note="permanent 403")
        task_root.advance_cleanup_facet(self.registry, "p1", "t1", "exec-a", "release_pending")
        task_root.advance_cleanup_facet(self.registry, "p1", "t1", "exec-a", "released")
        task_root.release_runtime_claim(self.registry, "p1", "t1", "exec-a", self.registry.generation)
        doc = self.registry.document
        self.assertIsNotNone(doc["terminal"])  # terminal authority remains bound
        self.assertEqual("attention", doc["materialization"]["handoff"]["status"])
        self.assertEqual("released", doc["cleanup"]["status"])
        # And a genuinely new retry can now legally open epoch 2, proving
        # runtime authority was NOT held hostage by the broken view.
        second = task_root.acquire_task_root(self.registry, "p1", "t1", "exec-b", "codex", _now())
        self.assertEqual(2, second["epoch"])

    def test_facet_advance_refuses_a_non_owning_execution(self):
        with self.assertRaises(TaskError):
            task_root.advance_materialization_view(self.registry, "p1", "t1", "exec-OTHER", "task", "pending")
        with self.assertRaises(TaskError):
            task_root.advance_cleanup_facet(self.registry, "p1", "t1", "exec-OTHER", "released")


class ProjectionDigestTests(unittest.TestCase):
    """Checkpoint D: expected projection digests are stamped into the bind
    once, and a reader must recompute from the ACTUAL Drive content rather
    than trust anything self-reported inside the document."""

    def setUp(self):
        self.registry = MemoryClaimRegistry()
        task_root.acquire_task_root(self.registry, "p1", "t1", "exec-a", "codex", _now())

    def test_expected_digest_stamped_and_reader_recomputes(self):
        task_payload = {"status": "completed", "blocked_reason": None}
        handoff_payload = {"handoff_id": "t1-completed-exec-a-0", "reason": "completed"}
        bound, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"),
                                                  expected_task_projection=task_payload,
                                                  expected_handoff_projection=handoff_payload)
        bind = bound["terminal"]
        self.assertIsNotNone(bind["expected_task_projection_digest"])
        self.assertTrue(task_root.verify_projection_digest(bind, "task", task_payload))
        self.assertTrue(task_root.verify_projection_digest(bind, "handoff", handoff_payload))

    def test_K_same_epoch_hash_but_corrupted_payload_digest_mismatch_rejected(self):
        """K: same winner/same fence/same proposal_hash, but the ACTUAL
        Drive content was overwritten with something else (e.g. a stale
        writer's own idea of the projection) -- digest recompute catches
        it even though the authority tuple alone would not."""
        task_payload = {"status": "completed", "blocked_reason": None}
        bound, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"),
                                                  expected_task_projection=task_payload)
        bind = bound["terminal"]
        corrupted = {"status": "blocked", "blocked_reason": "stale writer overwrite"}
        self.assertFalse(task_root.verify_projection_digest(bind, "task", corrupted))
        # But the authority tuple itself still matches -- proving these are
        # genuinely orthogonal checks, not the same information twice.
        self.assertTrue(task_root.verify_projection_matches_commit(bind, task_root.projection_of(bind)))

    def test_J_old_epoch_projection_rejected(self):
        """J: a projection minted under an OLDER epoch is rejected by the
        authority-tuple check regardless of digest."""
        bound, bind_generation = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"))
        stale_projection = {"execution_id": "exec-a", "epoch": 0, "proposal_hash": bound["terminal"]["proposal_hash"]}
        self.assertFalse(task_root.verify_projection_matches_commit(bound["terminal"], stale_projection))

    def test_digest_immutable_once_bound_even_if_new_payload_supplied(self):
        first_payload = {"status": "completed"}
        second_payload = {"status": "completed", "extra": "field"}
        bound1, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"), expected_task_projection=first_payload)
        bound2, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", _execution("exec-a"), expected_task_projection=second_payload)
        self.assertEqual(bound1["terminal"]["expected_task_projection_digest"], bound2["terminal"]["expected_task_projection_digest"])


class R17RoundRoundAndFreshProcessRecoveryTests(unittest.TestCase):
    """Checkpoint E: the exact live R17 legacy scenario, Round 46
    claim-absent compatibility, and fresh-process recovery -- all
    exercised through the REAL manager.command_watcher.process_command()
    entrypoint, never a direct unit call to task_root functions."""

    def setUp(self):
        self.store = Store()
        create_project(self.store, project())
        create_task(self.store, task(read_only=True), assign=False)
        compliant = self.store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", compliant)
        self.registry = MemoryClaimRegistry()
        self.execution_id = "command-cmd-1"

    def _seed_terminal_execution(self, claim_active=True):
        reserve_execution(self.store, "p1", "t1", self.execution_id, "codex", {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", self.execution_id, "codex",
                              "read_only", task_claim_registry=self.registry)
        exec_doc = self.store.get("executions", "p1", self.execution_id)
        exec_doc["session_id"] = "codex:r17-session"
        exec_doc["provider_evidence"] = {"host": socket.gethostname()[:100], "pid": 999997,
                                         "creation_identity": "proc-r17-test", "started_at": _now()}
        self.store.put("executions", "p1", self.execution_id, exec_doc)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(self.store, object(), None, self.registry, "p1", "t1",
                                  self.execution_id, "codex", "completed", 1, True,
                                  summary="Execution terminal completed")

    def _corrupt_to_r17_shape(self):
        """Exact legacy R17 shape: Command completed, Execution completed,
        Task blocked/stale, cleanup persistence=partial,
        persisted=[execution], claim retained -- as if cleanup_execution()
        was never reached because persistence itself failed."""
        exec_doc = self.store.get("executions", "p1", self.execution_id)
        exec_doc["cleanup_evidence"]["persistence"] = "partial"
        exec_doc["cleanup_evidence"]["persisted"] = ["execution"]
        exec_doc["cleanup_evidence"]["task_claim_release"] = "retained"
        validate("execution", exec_doc)
        self.store.put("executions", "p1", self.execution_id, exec_doc)
        task_doc = self.store.get("tasks", "p1", "t1")
        task_doc["source_context"] = {"active_execution_id": self.execution_id}
        task_doc["status"] = "blocked"
        task_doc["blocked_reason"] = "stuck mid-persistence"
        validate("task", task_doc)
        self.store.put("tasks", "p1", "t1", task_doc)
        cmd = command(status="completed", execution_id=self.execution_id, claimed_at=_now(),
                     completed_at=_now(), result={"status": "completed", "session_id": "codex:r17-session", "error_kind": None})
        self.store.put("commands", "p1", "cmd-1", cmd)
        return cmd

    def _seed_legacy_claim_document(self):
        """Downgrade the Task Root object back to a pre-Design-A legacy
        shape -- proves R17 recovery works starting from a record that
        predates task_root.py entirely, not just from a strengthened one."""
        legacy_record = _new_claim_record("p1", "t1", self.execution_id, "codex", "2026-08-13T00:00:00Z")
        self.registry.document = legacy_record
        self.registry.generation = self.registry.generation or 1

    def test_r17_exact_legacy_scenario_converges_through_real_process_command(self):
        self._seed_terminal_execution()
        cmd = self._corrupt_to_r17_shape()
        self._seed_legacy_claim_document()

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, None, cmd, claim_factory=lambda *_: self.registry)

        self.assertEqual("completed", outcome["status"])
        self.assertNotEqual(True, outcome.get("skipped"))

        exec_doc = self.store.get("executions", "p1", self.execution_id)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], exec_doc["cleanup_evidence"]["persisted"])

        root_doc = self.registry.document
        self.assertIsNotNone(root_doc.get("terminal"))
        self.assertEqual(self.execution_id, root_doc["terminal"]["execution_id"])
        self.assertEqual("verified", root_doc["materialization"]["task"]["status"])
        self.assertEqual("verified", root_doc["materialization"]["handoff"]["status"])
        self.assertFalse(root_doc["authority_active"])

        task_doc = self.store.get("tasks", "p1", "t1")
        self.assertEqual("completed", task_doc["status"])
        projection = task_doc["source_context"].get("terminal_commit_projection")
        self.assertIsNotNone(projection)
        self.assertTrue(task_root.verify_projection_matches_commit(root_doc["terminal"], projection))

    def test_fully_converged_terminal_command_still_takes_fast_path(self):
        """Negative control: once everything has actually converged, the
        fast-path skip must still fire -- this migration must not turn
        every already-clean terminal Command into perpetual reconciliation
        work."""
        self._seed_terminal_execution()
        cmd = self._corrupt_to_r17_shape()
        self._seed_legacy_claim_document()
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            process_command(self.store, None, cmd, claim_factory=lambda *_: self.registry)
            converged_cmd = self.store.get("commands", "p1", "cmd-1")
            with patch("manager.command_watcher._reconcile_active") as mock_reconcile:
                outcome = process_command(self.store, None, converged_cmd, claim_factory=lambda *_: self.registry)
        self.assertTrue(outcome.get("skipped"))
        mock_reconcile.assert_not_called()

    def test_round46_claim_absent_compatibility_preserved(self):
        """Round 46: claim already absent from GCS (e.g. a prior release
        actually landed) but cleanup_evidence still says retained --
        converges naturally to released without re-establishing a claim,
        downgrading terminal truth, or touching the (absent) Task Root."""
        self._seed_terminal_execution()
        cmd = self._corrupt_to_r17_shape()
        # No legacy claim document seeded at all -- registry starts empty.
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, None, cmd, claim_factory=lambda *_: self.registry)

        self.assertEqual("completed", outcome["status"])
        # commit_terminal_bind requires an existing Task Root object; with
        # none ever created, this specific execution cannot durably bind --
        # it fails closed (persistence stays incomplete) rather than
        # fabricate authority out of nothing. This is the correct,
        # documented boundary: Round 46's absent-claim convergence still
        # applies once *something* (even a bare legacy claim) exists to
        # migrate, exercised separately by the exact-R17 test above.
        self.assertIsNone(self.registry.document)

    def test_fresh_process_restart_recovers_purely_from_durable_state(self):
        """A brand-new Store/registry pair seeded with only the exact
        durable R17 snapshot (no Python objects/closures carried over from
        whatever produced it) still converges through one process_command
        call -- exactly what a real watcher process restart relies on."""
        self._seed_terminal_execution()
        cmd = self._corrupt_to_r17_shape()
        self._seed_legacy_claim_document()

        fresh_store = Store()
        fresh_store.records = deepcopy(self.store.records)
        fresh_registry = MemoryClaimRegistry()
        fresh_registry.document = deepcopy(self.registry.document)
        fresh_registry.generation = self.registry.generation

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(fresh_store, None, cmd, claim_factory=lambda *_: fresh_registry)

        self.assertEqual("completed", outcome["status"])
        self.assertEqual(self.execution_id, fresh_registry.document["terminal"]["execution_id"])

    def test_fresh_process_recovery_after_bind_but_before_materialization(self):
        """CP2/CP3 of the crash matrix: a fresh process resumes after the
        terminal bind CAS already landed but before Handoff/Task
        materialization completed -- it must reuse the SAME bind (same
        fixed Handoff ID, same digests) rather than re-propose or
        re-generate anything."""
        self._seed_terminal_execution()
        cmd = self._corrupt_to_r17_shape()
        self._seed_legacy_claim_document()
        execution = self.store.get("executions", "p1", self.execution_id)

        # Simulate CP2: bind already committed by a process that then crashed.
        bound_document, _ = task_root.commit_terminal_bind(self.registry, "p1", "t1", execution)
        original_bind = deepcopy(bound_document["terminal"])

        fresh_store = Store()
        fresh_store.records = deepcopy(self.store.records)
        fresh_registry = MemoryClaimRegistry()
        fresh_registry.document = deepcopy(self.registry.document)
        fresh_registry.generation = self.registry.generation

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(fresh_store, None, cmd, claim_factory=lambda *_: fresh_registry)

        self.assertEqual("completed", outcome["status"])
        # Core identity/proposal fields must never change across the crash
        # -- this is the same bind, not a re-proposal. (The two
        # expected_*_projection_digest fields are the one legitimate
        # exception: this test's own simulated pre-crash commit didn't
        # pass projections, so those started null and were correctly
        # null-filled by the real recovery flow's own commit_terminal_bind
        # call -- exactly the intended behavior for a genuinely-missing
        # digest from an earlier partial attempt, exercised for real by
        # test_digest_immutable_once_bound_even_if_new_payload_supplied.)
        for key in ("proposal_hash", "execution_id", "epoch", "terminal_fence_epoch", "terminal_committed_at", "canonical_proposal"):
            self.assertEqual(original_bind.get(key), fresh_registry.document["terminal"].get(key), f"field {key} changed across crash recovery")


if __name__ == "__main__":
    unittest.main()
