"""Phase 2 -- terminal convergence / recovery truth (T-A .. T-J).

Behavioral reproduction of the 2026-09-04 production failure and of the
non-stale variant that shares its root cause, turned into permanent
regression coverage for the state machine adjudicated in Fable's Phase 2
Review 1:

    canonical terminal authority = the GCS Task Root terminal bind
    (first-bind-wins within one epoch, immutable once bound).
    Drive Execution / Task / Handoff are PROJECTIONS, never authority.

Two independent P0 shapes are reproduced here.

Scenario A (stale running projection).  The runner terminalizes `failed`,
CAS-binds that outcome on the Task Root, writes the Handoff, and then loses
the Task write to a transient Drive failure -- the exact
`persistence=partial` shape already covered elsewhere in this suite.  A
later watcher tick reads the Execution back through Drive's
read-after-write staleness window (self-documented in
`manager.execution_lifecycle`: observed live twice in the C Stability Gate)
and sees `running`.  At base the watcher then reconciles the writer lease
and re-proposes `interrupted`: `persist_terminal` overwrites the Drive
projection with the losing outcome BEFORE `commit_terminal_bind` gets to
reject it, the bind conflict aborts cleanup, and the task claim and writer
lease are retained forever.

Scenario B (non-stale terminal).  No staleness at all: the same partial
persistence completes on the next tick, but nothing in the terminal branch
ever releases or re-verifies the writer lease, so `recover_task_claim`
refuses with `writer_authority_not_confirmed_released` on every tick and
the claim is never released either.

Both scenarios must converge from the bind, in bounded ticks, without ever
re-launching a provider or writing to the repository.
"""

import socket
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from manager import task_root
from manager.command_watcher import _reconcile_active
from manager.execution_lifecycle import (enter_running_gate, retry_incomplete_terminal_persistence,
                                         terminalize_execution)
from manager.execution_recovery import recover_task_claim
from manager.executions import reserve_execution
from manager.tasks import TaskError, create_project, create_task
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import NOW, MemoryClaimRegistry
from manager.test_worktree_locks import HEAD, MemoryRegistry
from manager.trusted_ingress import REQUIRED_TASK_POLICIES
from manager.worktree_locks import canonical_repository, read_registry, repository_lock_id


RUNNER_REASON = ("codex turn rejected: repo-write execution modified path(s) "
                 "outside its admitted allowed_paths")
RECOVERY_SUMMARY_FRAGMENT = "provider stop and released writer generation proven"
TERMINAL = ("completed", "failed", "interrupted")


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class StaleReadStore(Store):
    """Drive double with the two failure modes this phase exists to survive.

    ``arm_stale_execution`` reproduces read-after-write staleness: every
    ``get`` of that Execution returns the armed *earlier* version until this
    same store observes a ``put`` on it (a process always sees its own
    write), exactly like the real ``DriveRecords.get_media`` behaviour the
    lifecycle module documents.  Nothing here is a shortcut around a
    production guard: writes still go through the real schema validation and
    the real lifecycle code paths.
    """

    def __init__(self):
        super().__init__()
        self.stale_executions = {}
        self.fail_terminal_task_writes = 0
        self.fail_execution_writes = 0
        self.fail_handoff_writes = 0
        # Real Drive gives each files.update/get_media its own atomicity; what
        # it does NOT give is a compare-and-swap across a read and a later
        # write. This lock models exactly the former, so a concurrency test
        # exercises the real hazard (lost updates between two reconcilers)
        # rather than an artefact of a dict being torn mid-copy.
        self.mutex = threading.RLock()

    def arm_stale_execution(self, project_id, name, snapshot):
        self.stale_executions[(project_id, name)] = deepcopy(snapshot)

    def clear_stale_executions(self):
        self.stale_executions.clear()

    def put(self, area, project_id, name, document):
        with self.mutex:
            if area == "tasks" and document.get("status") == "blocked" and self.fail_terminal_task_writes > 0:
                self.fail_terminal_task_writes -= 1
                raise TaskError("injected Drive failure: terminal task persistence")
            if area == "handoffs" and self.fail_handoff_writes > 0:
                self.fail_handoff_writes -= 1
                raise TaskError("injected Drive failure: handoff persistence")
            if area == "executions" and self.fail_execution_writes > 0:
                self.fail_execution_writes -= 1
                raise TaskError("injected Drive failure: execution persistence")
            if area == "executions":
                self.stale_executions.pop((project_id, name), None)
            return super().put(area, project_id, name, document)

    def get(self, area, project_id, name):
        with self.mutex:
            if area == "executions" and (project_id, name) in self.stale_executions:
                return deepcopy(self.stale_executions[(project_id, name)])
            return super().get(area, project_id, name)

    def list_records(self, area, project_id):
        with self.mutex:
            return super().list_records(area, project_id)


class RacingClaimRegistry(MemoryClaimRegistry):
    """A Task Root whose FIRST read reports the epoch as not-yet-bound (the
    genuine pre-bind moment a reconciler can observe) and whose every later
    read fails -- modelling a concurrent reconciler landing the terminal bind
    in between, followed by the backend becoming unreadable. Only the reads
    are scripted; writes and CAS keep the real double's semantics."""

    def __init__(self, inner):
        super().__init__()
        self.document, self.generation = deepcopy(inner.document), inner.generation
        self.reads = 0

    def read_if_exists(self):
        self.reads += 1
        if self.reads == 1:
            unbound = deepcopy(self.document)
            unbound["terminal"] = None
            return unbound, self.generation, NOW
        raise TaskError("simulated read unavailable")


class LateBindClaimRegistry(MemoryClaimRegistry):
    """A Task Root that reports the epoch as UNBOUND for its first
    `unbound_reads` reads and truthfully bound from then on -- the
    interleaving where every fence read legitimately succeeds and sees no
    bind, and a concurrent reconciler commits the terminal bind immediately
    afterwards. Only reads are scripted; writes and CAS keep the real
    double's semantics."""

    def __init__(self, inner, unbound_reads):
        super().__init__()
        self.document, self.generation = deepcopy(inner.document), inner.generation
        self.unbound_reads = unbound_reads
        self.reads = 0

    def read_if_exists(self):
        self.reads += 1
        existing = super().read_if_exists()
        if existing is None or self.reads > self.unbound_reads:
            return existing
        document, generation, server_time = existing
        return {**document, "terminal": None}, generation, server_time


class TerminalConvergenceBase(unittest.TestCase):
    maxDiff = None
    EXECUTION_ID = "command-cmd-1"

    def setUp(self):
        self.store = StaleReadStore()
        create_project(self.store, project())
        create_task(self.store, task(read_only=False), assign=False)
        compliant = self.store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", compliant)
        self.claim = MemoryClaimRegistry()
        self.writer = MemoryRegistry()
        self.lock_id = repository_lock_id(canonical_repository(project()["repo"]))
        self.clock = datetime.now(timezone.utc)
        self.started = iso(self.clock - timedelta(minutes=5))
        self.launcher = Mock(name="launch_task")
        self.repo_write = Mock(name="commit_and_push_repo_write_changes")

    # ---------------------------------------------------------------- setup

    def running_write_execution(self, execution_id=None, provider="codex"):
        execution_id = execution_id or self.EXECUTION_ID
        reserve_execution(self.store, "p1", "t1", execution_id, provider, {"decision": "fresh"})
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(self.store, object(), self.writer, "p1", "t1", execution_id,
                                      provider, "production_write", baseline_head=HEAD,
                                      started_at=self.started, task_claim_registry=self.claim)
        self.gate = gate
        execution = self.store.get("executions", "p1", execution_id)
        execution["heartbeat_at"] = iso(self.clock - timedelta(minutes=1))
        execution["progress_updated_at"] = execution["heartbeat_at"]
        execution["provider_evidence"] = {
            "host": socket.gethostname()[:100], "pid": 424242,
            "creation_identity": "test-process:proven-stopped", "started_at": self.started,
        }
        execution["last_provider_event"] = "provider_wait"
        self.store.put("executions", "p1", execution_id, execution)
        active = command(status="running", execution_id=execution_id, claimed_at=self.started, provider=provider)
        self.store.put("commands", "p1", "cmd-1", active)
        return active, self.store.get("executions", "p1", execution_id)

    def partial_terminal_failed(self, expect_persisted=("execution", "handoff")):
        """The exact 2026-09-04 runner shape: bind committed on `failed`,
        Handoff written, Task write lost, claim + writer lease retained.

        With `fail_handoff_writes` armed by the caller instead, the same call
        reproduces the earlier boundary: bind committed, nothing projected."""
        active, _ = self.running_write_execution()
        running_snapshot = self.store.get("executions", "p1", self.EXECUTION_ID)
        if not self.store.fail_handoff_writes:
            self.store.fail_terminal_task_writes = 1
        with self.assertRaises(TaskError), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(self.store, object(), self.writer, self.claim, "p1", "t1",
                                  self.EXECUTION_ID, "codex", "failed",
                                  self.gate["task_claim"]["generation"], True,
                                  lease_token=self.gate["lease"]["lease_token"], summary=RUNNER_REASON)
        self.assertEqual(list(expect_persisted), self.execution()["cleanup_evidence"]["persisted"])
        return active, running_snapshot

    def damage_projection_to_interrupted(self):
        """Reproduce the durable wreckage the 09-04 recovery left on Drive:
        `persist_terminal` overwrote the bound `failed` Execution projection
        with `interrupted` (executions.py has no CAS), while the Task Root
        bind, the Handoff and the writer lease were untouched."""
        damaged = self.execution()
        damaged["status"] = "interrupted"
        damaged["terminal_reason"] = f"Recovery: provider_process_stopped; {RECOVERY_SUMMARY_FRAGMENT}"
        damaged["notes"] = list(damaged.get("notes") or []) + [damaged["terminal_reason"]]
        Store.put(self.store, "executions", "p1", self.EXECUTION_ID, damaged)
        return damaged

    # ----------------------------------------------------------- observation

    def bind(self):
        return (self.claim.document or {}).get("terminal")

    def execution(self):
        return Store.get(self.store, "executions", "p1", self.EXECUTION_ID)

    def lock(self):
        document, _etag, _now = read_registry(self.writer)
        return document["locks"].get(self.lock_id)

    def tick(self, command_record=None):
        record = command_record if command_record is not None else self.store.get("commands", "p1", "cmd-1")
        with patch("manager.command_watcher.GCSLockRegistry.from_environment", return_value=self.writer), \
             patch("manager.command_watcher.process_identity_state", return_value="stopped"), \
             patch("manager.command_watcher.launch_task", self.launcher), \
             patch("manager.repo_write_enforcement.commit_and_push_repo_write_changes", self.repo_write), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            return _reconcile_active(self.store, object(), record, lambda *_args: self.claim)

    def ticks(self, count):
        return [self.tick() for _ in range(count)]

    def assert_bind_immutable(self, before):
        """Exactly `task_root._reject_bind_mutation`'s contract, asserted from
        the outside: every bound field that already carried a value is frozen
        forever. The projection digests and the two Drive ids are the
        documented null-fill fields `commit_terminal_bind` may complete once
        (and only from null), so they are checked for that, not for
        byte-equality with a pre-fill snapshot."""
        after = self.bind()
        self.assertIsNotNone(after)
        for key, value in before.items():
            if value is not None:
                self.assertEqual(value, after.get(key), f"bound field {key!r} must be immutable")
        self.assertEqual(before["proposal_hash"], after["proposal_hash"])
        self.assertEqual(before["terminal_status"], after["terminal_status"])
        return after

    def assert_no_repo_execution_side_effects(self):
        self.assertEqual(0, self.launcher.call_count, "LAUNCHER_CALLS must be 0")
        self.assertEqual(0, self.repo_write.call_count, "COMMIT_CALLS/PUSH_CALLS must be 0")

    def assert_converged_to_bind(self):
        bind = self.bind()
        self.assertIsNotNone(bind)
        execution = self.execution()
        evidence = execution.get("cleanup_evidence") or {}
        self.assertEqual(bind["terminal_status"], execution["status"])
        self.assertEqual(bind["terminal_reason"], execution["terminal_reason"])
        self.assertEqual(bind["provider_outcome"], evidence.get("provider_outcome"))
        self.assertEqual("complete", evidence.get("persistence"))
        self.assertEqual(["execution", "handoff", "task"], evidence.get("persisted"))
        self.assertEqual("released", evidence.get("task_claim_release"))
        self.assertEqual("released", evidence.get("writer_release"))
        self.assertEqual("released", (self.lock() or {}).get("status"))
        self.assertFalse(self.claim.document["authority_active"])
        task_record = self.store.get("tasks", "p1", "t1")
        self.assertEqual("blocked", task_record["status"])
        self.assertEqual(task_root.projection_of(bind),
                         (task_record.get("source_context") or {}).get("terminal_commit_projection"))
        return execution


class ReproductionFixtureTests(TerminalConvergenceBase):
    """P0_REPRO: the fixtures themselves must match the live 09-04 record."""

    def test_runner_partial_persistence_matches_the_live_0904_shape(self):
        self.partial_terminal_failed()
        execution = self.execution()
        evidence = execution["cleanup_evidence"]
        self.assertEqual("failed", execution["status"])
        self.assertEqual(RUNNER_REASON, execution["terminal_reason"])
        self.assertEqual("partial", evidence["persistence"])
        self.assertEqual(["execution", "handoff"], evidence["persisted"])
        self.assertEqual("retained", evidence["task_claim_release"])
        self.assertEqual("retained", evidence["writer_release"])
        bind = self.bind()
        self.assertEqual("failed", bind["terminal_status"])
        self.assertEqual(RUNNER_REASON, bind["terminal_reason"])
        self.assertTrue(self.claim.document["authority_active"])
        self.assertEqual("active", self.lock()["status"])


class TerminalAuthorityTests(TerminalConvergenceBase):

    def test_a_root_bind_beats_a_stale_running_projection(self):
        """T-A: with the epoch already bound, a stale `running` read must
        never become a new terminal proposal -- the bound `failed` outcome
        survives, and the Drive projection converges to it once the
        staleness window closes."""
        active, running_snapshot = self.partial_terminal_failed()
        bound_before = deepcopy(self.bind())
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)

        self.tick(active)
        self.assertEqual("failed", self.execution()["status"],
                         "a stale running read must never overwrite the bound terminal projection")
        self.assertEqual(bound_before, self.bind(), "the terminal bind is immutable once bound")

        self.store.clear_stale_executions()
        self.ticks(3)
        execution = self.assert_converged_to_bind()
        self.assertEqual("failed", execution["status"])
        self.assert_bind_immutable(bound_before)
        self.assert_no_repo_execution_side_effects()

    def test_b_first_terminal_bind_still_works_on_the_normal_recovery_path(self):
        """T-B: with NO bind on the Task Root and provider stop proven,
        recovery may still propose and bind `interrupted` exactly as before."""
        self.running_write_execution()
        self.assertIsNone(self.bind())
        self.ticks(3)
        bind = self.bind()
        self.assertIsNotNone(bind, "a legal first bind must still be possible")
        self.assertEqual("interrupted", bind["terminal_status"])
        self.assertIn(RECOVERY_SUMMARY_FRAGMENT, bind["terminal_reason"])
        self.assert_converged_to_bind()
        self.assert_no_repo_execution_side_effects()

    def test_c_a_conflicting_observation_cannot_block_cleanup(self):
        """T-C: the Drive projection already carries the LOSING `interrupted`
        outcome (the damage the pre-fix recovery path caused live). The claim
        and the writer lease must still converge."""
        self.partial_terminal_failed()
        self.damage_projection_to_interrupted()
        self.ticks(3)
        self.assert_converged_to_bind()
        self.assertEqual("failed", self.execution()["status"])
        self.assert_no_repo_execution_side_effects()

    def test_d_projection_repair_restores_the_bound_outcome(self):
        """T-D: a Drive Execution terminal projection that disagrees with the
        bind is repaired FROM the bind, never the other way round."""
        self.partial_terminal_failed()
        bound_before = deepcopy(self.bind())
        self.damage_projection_to_interrupted()
        self.ticks(3)
        execution = self.execution()
        self.assertEqual("failed", execution["status"])
        self.assertEqual(RUNNER_REASON, execution["terminal_reason"])
        self.assertEqual("failed", execution["cleanup_evidence"]["provider_outcome"])
        self.assert_bind_immutable(bound_before)
        task_record = self.store.get("tasks", "p1", "t1")
        self.assertEqual("blocked", task_record["status"])
        self.assertIn(RUNNER_REASON, task_record["blocked_reason"])

    def test_e_terminal_execution_with_an_active_writer_lease_converges(self):
        """T-E: the non-stale second deadlock. Persistence completes, but at
        base nothing in the terminal branch ever releases or re-verifies the
        writer lease, so the claim is refused forever."""
        self.partial_terminal_failed()
        self.assertEqual("active", self.lock()["status"])
        self.ticks(3)
        self.assertEqual("released", self.lock()["status"])
        self.assert_converged_to_bind()
        self.assert_no_repo_execution_side_effects()

    def test_f_cleanup_is_idempotent(self):
        """T-F: repeated cleanup changes nothing and destroys no authority."""
        self.partial_terminal_failed()
        self.ticks(3)
        self.assert_converged_to_bind()
        settled_execution = self.execution()
        settled_task = self.store.get("tasks", "p1", "t1")
        settled_command = self.store.get("commands", "p1", "cmd-1")
        settled_bind = deepcopy(self.bind())
        settled_lock = deepcopy(self.lock())

        self.ticks(3)
        self.assertEqual(settled_execution, self.execution())
        self.assertEqual(settled_task, self.store.get("tasks", "p1", "t1"))
        self.assertEqual(settled_command, self.store.get("commands", "p1", "cmd-1"))
        self.assertEqual(settled_bind, self.bind())
        self.assertEqual(settled_lock, self.lock())
        self.assertIsNotNone(self.claim.document, "cleanup must never delete the durable terminal bind")

    def test_g_crash_boundaries_converge_without_duplicate_repo_writes(self):
        """T-G: a crash at each real persistence boundary converges in <= 3
        ticks. Each `arrange` leaves the world in the exact durable state a
        process death at that boundary would leave behind -- none of them is
        a no-op, and no two of them are the same state."""
        for name, arrange in (
            # 1. bind CAS landed; NOTHING projected yet (no Handoff, no Task).
            ("after_bind_before_any_projection", self._crash_after_bind_before_projection),
            # 2. Handoff projected; the Task write is the one that died.
            ("after_handoff_before_task_projection", self._crash_after_handoff_before_task),
            # 3. every projection verified; the writer lease is still held.
            ("after_projection_before_lease_release", self._crash_after_projection_before_lease),
            # 4. writer lease released; the task claim is still held and the
            #    Execution's evidence copy never learned about the release.
            ("after_lease_release_before_claim_release", self._crash_after_lease_before_claim),
            # 5. everything done except persisting the cleanup evidence.
            ("before_cleanup_evidence_persistence", self._crash_before_cleanup_evidence),
        ):
            with self.subTest(boundary=name):
                self.setUp()
                arrange()
                self.ticks(3)
                self.assert_converged_to_bind()
                self.assert_no_repo_execution_side_effects()

    def _crash_after_bind_before_projection(self):
        self.store.fail_handoff_writes = 1
        self.partial_terminal_failed(expect_persisted=["execution"])
        self.assertIsNotNone(self.bind())

    def _crash_after_handoff_before_task(self):
        self.partial_terminal_failed()
        self.assertEqual(["execution", "handoff"], self.execution()["cleanup_evidence"]["persisted"])

    def _crash_after_projection_before_lease(self):
        self.partial_terminal_failed()
        self.assertTrue(retry_incomplete_terminal_persistence(
            self.store, "p1", "t1", self.EXECUTION_ID, claim_registry=self.claim))
        self.assertEqual("complete", self.execution()["cleanup_evidence"]["persistence"])
        self.assertEqual("active", self.lock()["status"])
        self.assertTrue(self.claim.document["authority_active"])

    def _crash_after_lease_before_claim(self):
        self._crash_after_projection_before_lease()
        self._release_lease_directly()
        self.assertEqual("retained", self.execution()["cleanup_evidence"]["writer_release"])
        self.assertTrue(self.claim.document["authority_active"])

    def _crash_before_cleanup_evidence(self):
        self._crash_after_lease_before_claim()
        self.store.fail_execution_writes = 1

    def _release_lease_directly(self):
        document, etag, _now = read_registry(self.writer)
        lock = dict(document["locks"][self.lock_id])
        lock.update(status="released", released_at="2026-08-12T00:05:00Z", updated_at="2026-08-12T00:05:00Z")
        self.writer.cas(etag, {**document, "locks": {**document["locks"], self.lock_id: lock}})

    def test_h_conflicting_terminal_evidence_is_preserved(self):
        """T-H: the losing observation is never silently dropped -- both
        proposal hashes plus the candidate status/reason stay on the
        Execution's append-only forensic trail."""
        self.partial_terminal_failed()
        bind = deepcopy(self.bind())
        damaged = self.damage_projection_to_interrupted()
        self.ticks(3)
        errors = " | ".join(self.execution()["cleanup_evidence"].get("errors") or [])
        self.assertIn(bind["proposal_hash"], errors, "bound proposal hash must be preserved")
        self.assertIn("interrupted", errors, "candidate terminal status must be preserved")
        self.assertIn(RECOVERY_SUMMARY_FRAGMENT, errors, "candidate terminal reason must be preserved")
        candidate_hash = task_root.proposal_hash(task_root.terminal_proposal(damaged, bind["epoch"]))
        self.assertIn(candidate_hash, errors, "candidate proposal hash must be preserved")
        self.assert_bind_immutable(bind)

    def test_i_cleanup_evidence_reflects_registry_truth(self):
        """T-I: `_retain_terminal_authority` must never record `retained` for
        a writer authority the caller already proved released, and
        `recover_task_claim` must read the registry, not that copy."""
        from manager.execution_lifecycle import _retain_terminal_authority

        self.partial_terminal_failed()
        self._release_lease_directly()
        execution = self.execution()
        audit = _retain_terminal_authority(self.store, execution, "failed", ["execution"],
                                           TaskError("injected"), writer_authority_released=True)
        self.assertEqual("released", audit["writer_release"],
                         "evidence must not claim a released writer authority is retained")

        retained = self.execution()
        retained["cleanup_evidence"] = {**retained["cleanup_evidence"], "writer_release": "retained",
                                        "persistence": "complete",
                                        "persisted": ["execution", "handoff", "task"],
                                        "provider_outcome": "failed"}
        Store.put(self.store, "executions", "p1", self.EXECUTION_ID, retained)
        blocked = self.store.get("tasks", "p1", "t1")
        blocked.update(status="blocked", blocked_reason=f"Execution failed: {RUNNER_REASON}")
        Store.put(self.store, "tasks", "p1", "t1", blocked)
        result = recover_task_claim(self.store, self.claim, "p1", "t1", writer_registry=self.writer)
        self.assertEqual("released", result["status"],
                         "a stale `retained` evidence copy must not outrank the registry's released truth")

    def test_j_a_bound_epoch_is_never_re_proposed_or_re_executed(self):
        """T-J: recovery of an already-bound epoch must not reconcile the
        writer lease, must not re-terminalize, must not create a second
        Execution or a second writer lease generation, and must not re-launch
        or re-write the repository."""
        import manager.command_watcher as watcher

        active, running_snapshot = self.partial_terminal_failed()
        lease_generation = self.lock()["generation"]
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        terminalize_spy = Mock(name="terminalize_execution", side_effect=watcher.terminalize_execution)
        lease_spy = Mock(name="reconcile_stopped_provider_terminal_lease",
                         side_effect=watcher.reconcile_stopped_provider_terminal_lease)
        with patch("manager.command_watcher.terminalize_execution", terminalize_spy), \
             patch("manager.command_watcher.reconcile_stopped_provider_terminal_lease", lease_spy):
            self.tick(active)
        self.assertEqual(0, terminalize_spy.call_count,
                         "a bound epoch must never be re-terminalized from a stale read")
        self.assertEqual(0, lease_spy.call_count,
                         "a bound epoch's writer lease must not be reconciled off a stale read")

        self.store.clear_stale_executions()
        self.ticks(3)
        self.assert_converged_to_bind()
        executions = self.store.list_records("executions", "p1")
        self.assertEqual([self.EXECUTION_ID], [record["execution_id"] for record in executions],
                         "recovery must never create a second Execution")
        self.assertEqual(lease_generation, self.lock()["generation"],
                         "recovery must never create a second writer lease generation")
        self.assert_no_repo_execution_side_effects()


class InvariantTests(TerminalConvergenceBase):
    """I1-I12 asserted directly, independently of the T-* scenarios."""

    def converged(self):
        self.partial_terminal_failed()
        self.ticks(3)
        return self.execution()

    def test_i1_i2_terminal_state_is_monotonic_under_stale_reads(self):
        _active, running_snapshot = self.partial_terminal_failed()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        self.ticks(3)
        self.assertEqual("failed", self.execution()["status"])
        self.store.clear_stale_executions()
        self.ticks(3)
        self.assertIn(self.execution()["status"], TERMINAL)
        self.assertEqual(self.bind()["terminal_status"], self.execution()["status"])

    def test_i3_i4_cleanup_is_idempotent_and_never_blocked_by_a_conflict(self):
        self.partial_terminal_failed()
        self.damage_projection_to_interrupted()
        self.ticks(3)
        first = self.execution()
        self.assertEqual("released", first["cleanup_evidence"]["task_claim_release"])
        self.ticks(2)
        self.assertEqual(first, self.execution())

    def test_i5_i6_at_most_one_live_lease_and_terminal_releases_both(self):
        self.converged()
        document, _etag, _now = read_registry(self.writer)
        live = [lock for lock in document["locks"].values() if lock["status"] == "active"]
        self.assertEqual([], live, "no live writer lease may survive a terminal execution")
        self.assertFalse(self.claim.document["authority_active"])

    def test_i7_recovery_never_re_executes_completed_work(self):
        self.converged()
        self.assert_no_repo_execution_side_effects()

    def test_i9_task_converges_to_the_bound_outcome(self):
        self.converged()
        task_record = self.store.get("tasks", "p1", "t1")
        self.assertEqual("blocked", task_record["status"])
        self.assertEqual(task_root.projection_of(self.bind()),
                         (task_record.get("source_context") or {}).get("terminal_commit_projection"))

    def test_i10_crash_at_a_persistence_boundary_converges_without_duplicate_writes(self):
        self.partial_terminal_failed()
        self.store.fail_execution_writes = 1
        self.ticks(3)
        self.assertEqual("complete", self.execution()["cleanup_evidence"]["persistence"])
        self.assert_no_repo_execution_side_effects()

    def test_i11_a_drive_projection_never_outranks_the_root_bind(self):
        _active, running_snapshot = self.partial_terminal_failed()
        bound = deepcopy(self.bind())
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        self.ticks(2)
        self.assert_bind_immutable(bound)
        self.assertEqual("failed", self.execution()["status"])

    def test_i11_terminalize_refuses_a_stale_running_proposal_on_a_bound_epoch(self):
        """The fail-closed gate itself, exercised directly rather than through
        the watcher: a bound epoch read back as `running` must be refused
        BEFORE persist_terminal touches Drive. This is the exact call the
        2026-09-04 recovery made."""
        from manager.execution_lifecycle import TerminalBindAlreadyBound

        _active, running_snapshot = self.partial_terminal_failed()
        before = self.execution()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        with self.assertRaises(TerminalBindAlreadyBound), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(self.store, object(), self.writer, self.claim, "p1", "t1",
                                  self.EXECUTION_ID, "codex", "interrupted",
                                  self.gate["task_claim"]["generation"], True,
                                  lease_token=self.gate["lease"]["lease_token"],
                                  summary=f"Recovery: {RECOVERY_SUMMARY_FRAGMENT}")
        self.assertEqual(before, self.execution(),
                         "a refused proposal must leave the bound projection byte-identical")

    def test_i11_terminalize_refuses_a_same_outcome_stale_running_reproposal(self):
        """The stale-running refusal has to stand on its own, not lean on the
        different-outcome one. A crashed runner retrying its OWN outcome
        through a stale read would otherwise re-run persist_terminal and
        restamp completed_at / quota_after / terminal_reason on top of the
        projection the winning bind froze -- a new proposal hash that then
        loses its own CAS, which is precisely how the 09-04 deadlock starts."""
        from manager.execution_lifecycle import TerminalBindAlreadyBound

        _active, running_snapshot = self.partial_terminal_failed()
        before = self.execution()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        with self.assertRaises(TerminalBindAlreadyBound), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(self.store, object(), self.writer, self.claim, "p1", "t1",
                                  self.EXECUTION_ID, "codex", "failed",
                                  self.gate["task_claim"]["generation"], True,
                                  lease_token=self.gate["lease"]["lease_token"],
                                  summary="retried after a crash with a differently-worded note")
        self.assertEqual(before, self.execution(),
                         "a bound projection must survive its own execution's stale re-proposal")
        self.assertEqual(RUNNER_REASON, self.bind()["terminal_reason"])

    def test_i4_i6_a_permanently_failing_projection_still_releases_claim_and_lease(self):
        """I4 + Fable's state-machine row 7: once the outcome is bound and the
        provider is proven stopped, cleanup is DECOUPLED from whether the
        Drive projection can ever be materialized. A permanently-failing Task
        write escalates that view to `attention` -- it must not hold the task
        claim or the writer lease hostage forever."""
        self.partial_terminal_failed()
        self.store.fail_terminal_task_writes = 99
        self.ticks(5)
        self.assertEqual("released", self.lock()["status"],
                         "a permanently unmaterializable projection must not retain the writer lease")
        self.assertFalse(self.claim.document["authority_active"],
                         "a permanently unmaterializable projection must not retain the task claim")
        self.assertEqual("attention", self.claim.document["materialization"]["task"]["status"])
        self.assertEqual("failed", self.bind()["terminal_status"])
        self.assert_no_repo_execution_side_effects()

    def test_i11_terminalize_refuses_a_different_outcome_on_a_bound_epoch(self):
        """Even against an already-terminal, non-stale record, a DIFFERENT
        terminal outcome is never re-proposed for a bound epoch."""
        from manager.execution_lifecycle import TerminalBindAlreadyBound

        self.partial_terminal_failed()
        self.damage_projection_to_interrupted()
        with self.assertRaises(TerminalBindAlreadyBound), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(self.store, object(), self.writer, self.claim, "p1", "t1",
                                  self.EXECUTION_ID, "codex", "interrupted",
                                  self.gate["task_claim"]["generation"], True,
                                  lease_token=self.gate["lease"]["lease_token"],
                                  writer_authority_released=False)
        self.assertEqual("failed", self.bind()["terminal_status"])

    def test_i2_repair_never_writes_back_a_non_terminal_read(self):
        """PROJECT_FROM_BIND must never become a route to resurrecting
        `running`: repairing from a stale non-terminal read would write that
        whole stale snapshot (no cleanup_evidence, no quota_after) over the
        winner's real terminal record."""
        from manager.execution_lifecycle import repair_terminal_projection_from_bind

        _active, running_snapshot = self.partial_terminal_failed()
        before = self.execution()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        repaired = repair_terminal_projection_from_bind(self.store, "p1", "t1", self.EXECUTION_ID,
                                                        self.bind(), note="stale read")
        self.assertFalse(repaired, "a stale non-terminal read is not repairable on this pass")
        self.assertEqual(before, self.execution())

    def test_i5_a_live_provider_writer_lease_is_never_released(self):
        """Terminal + bound is not on its own a licence to release a writer
        lease: without proven provider stop, releasing it is exactly how a
        second live writer appears on the same repository slot."""
        self.partial_terminal_failed()
        with patch("manager.command_watcher.GCSLockRegistry.from_environment", return_value=self.writer), \
             patch("manager.command_watcher.process_identity_state", return_value="live"), \
             patch("manager.command_watcher.launch_task", self.launcher), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            for _ in range(3):
                _reconcile_active(self.store, object(), self.store.get("commands", "p1", "cmd-1"),
                                  lambda *_args: self.claim)
        self.assertEqual("active", self.lock()["status"],
                         "a live provider's writer lease must never be released by terminal cleanup")
        self.assertTrue(self.claim.document["authority_active"])

    def test_i2_an_unreadable_task_root_never_licenses_a_drive_write(self):
        """Codex adversarial review finding, reproduced and closed. With the
        Task Root unreadable and the Execution read stale, the pre-fix
        watcher fell through to the running branch and `_attention()` wrote
        that stale `running` snapshot back over the bound `failed` record --
        an I1/I2 violation reached with no terminal proposal involved at all.
        Not knowing whether a bind exists must never read as "there is
        none"."""
        active, running_snapshot = self.partial_terminal_failed()
        before = self.execution()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        self.claim.read_unavailable = True
        try:
            result = self.tick(active)
        finally:
            self.claim.read_unavailable = False
        self.assertTrue(result.get("skipped"))
        self.assertEqual("task_root_bind_unknown", result.get("reason"))
        self.assertEqual(before, self.execution(),
                         "an unreadable Task Root must produce no Drive write at all")
        self.assertEqual("active", self.lock()["status"])

        self.store.clear_stale_executions()
        self.ticks(3)
        self.assert_converged_to_bind()
        self.assert_no_repo_execution_side_effects()

    def _attention_command_with_a_live_looking_provider(self):
        active, running_snapshot = self.partial_terminal_failed()
        attention = {**active, "status": "attention", "stale_at": self.started,
                     "recovery_reason": "provider_process_stopped"}
        Store.put(self.store, "commands", "p1", "cmd-1", attention)
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        return attention, self.execution(), self.store.get("tasks", "p1", "t1")

    def _tick_with_a_live_provider(self, record):
        with patch("manager.command_watcher.GCSLockRegistry.from_environment", return_value=self.writer), \
             patch("manager.command_watcher.process_identity_state", return_value="live"), \
             patch("manager.command_watcher.launch_task", self.launcher), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            return _reconcile_active(self.store, object(), record, lambda *_args: self.claim)

    def test_i2_a_bound_epoch_never_flips_a_stale_running_projection_back_to_healthy(self):
        """The early Root-bind gate has to stand on its own. The healthy+live
        short-circuit returns BEFORE the running branch takes its own Root
        read, so no later fence can catch a stale `running` read of an epoch
        whose outcome is already bound. Left unguarded, an attention Command
        is written back to `running` and its Task to `in_progress` -- ADM
        reporting a task as in progress after its execution terminally
        failed, which is exactly the truth violation PROJECT-RULES rule 4
        forbids."""
        attention, before_execution, before_task = self._attention_command_with_a_live_looking_provider()
        result = self._tick_with_a_live_provider(attention)
        self.assertTrue(result.get("skipped"))
        self.assertEqual("attention", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual(before_task, self.store.get("tasks", "p1", "t1"))
        self.assertEqual(before_execution, self.execution())

    def test_i2_an_unreadable_root_never_flips_a_stale_running_projection_back_to_healthy(self):
        """Same short-circuit, reached the other way: when the early Root read
        FAILS rather than returning a bind, "we could not ask" must still not
        license the healthy-path rewrite."""
        attention, before_execution, before_task = self._attention_command_with_a_live_looking_provider()
        self.claim.read_unavailable = True
        try:
            result = self._tick_with_a_live_provider(attention)
        finally:
            self.claim.read_unavailable = False
        self.assertTrue(result.get("skipped"))
        self.assertEqual("task_root_bind_unknown", result.get("reason"))
        self.assertEqual("attention", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual(before_task, self.store.get("tasks", "p1", "t1"))
        self.assertEqual(before_execution, self.execution())

    def test_i2_a_bind_landing_between_the_two_root_reads_never_licenses_a_write(self):
        """Codex delta re-review finding, reproduced and closed. The first
        Root read can legitimately see no bind; by the time the running
        branch takes its own Root read, a concurrent reconciler may have
        bound the outcome -- and if that second read then fails, "backend
        unavailable" used to read as "no claim", fall through to
        `_attention()`, and let a stale `running` snapshot be written back
        over the freshly bound terminal projection."""
        active, running_snapshot = self.partial_terminal_failed()
        before = self.execution()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        racing = RacingClaimRegistry(self.claim)
        self.claim = racing
        result = self.tick(active)
        self.assertGreaterEqual(racing.reads, 2, "the running branch must take its own Root read")
        self.assertTrue(result.get("skipped"))
        self.assertEqual("task_root_bind_unknown", result.get("reason"))
        self.assertEqual(before, self.execution(),
                         "a bind landing between the two Root reads must produce no Drive write")
        self.assertEqual("active", self.lock()["status"])

    def test_i5_a_bind_seen_only_by_the_running_branch_stops_it_before_it_touches_the_lease(self):
        """The running branch's own Root read is a fence in its own right,
        not just a lookup. When the early gate legitimately saw no bind and
        the claim read legitimately saw no bind, but the bind is there by the
        time the branch checks, everything downstream -- the writer-lease
        reconciliation and the terminal proposal -- is reasoning from a stale
        Execution snapshot about an epoch that is already decided, and must
        not run at all."""
        import manager.command_watcher as watcher

        active, running_snapshot = self.partial_terminal_failed()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        # Reads 1 (early gate) and 2 (claim read) see no bind; read 3, the
        # running branch's own bind read, is the first to see it.
        self.claim = LateBindClaimRegistry(self.claim, unbound_reads=2)
        terminalize_spy = Mock(name="terminalize_execution", side_effect=watcher.terminalize_execution)
        lease_spy = Mock(name="reconcile_stopped_provider_terminal_lease",
                         side_effect=watcher.reconcile_stopped_provider_terminal_lease)
        with patch("manager.command_watcher.terminalize_execution", terminalize_spy), \
             patch("manager.command_watcher.reconcile_stopped_provider_terminal_lease", lease_spy):
            result = self.tick(active)
        self.assertTrue(result.get("skipped"))
        self.assertEqual("terminal_bind_projection_stale", result.get("reason"))
        self.assertEqual(0, lease_spy.call_count,
                         "a bound epoch's writer lease must not be reconciled from a stale snapshot")
        self.assertEqual(0, terminalize_spy.call_count,
                         "a bound epoch must not be re-terminalized from a stale snapshot")
        self.assertEqual("active", self.lock()["status"])

    def test_i2_a_bind_committed_after_every_fence_read_still_never_regresses_the_truth(self):
        """Codex delta re-review round 3, reproduced and bounded. Every Root
        read a tick takes legitimately succeeds and legitimately reports the
        epoch as unbound; the bind lands only afterwards. The last-moment
        re-check in front of `_attention()` catches it here, and -- because
        Drive Executions have no conditional write, so no read-before-write
        can ever be atomic -- what is asserted is the property that actually
        holds regardless of where the bind lands: the bind is never mutated,
        no provider is re-launched, and the projection converges to the bind
        within bounded ticks."""
        active, running_snapshot = self.partial_terminal_failed()
        bound_before = deepcopy(self.bind())
        before = self.execution()
        self.store.arm_stale_execution("p1", self.EXECUTION_ID, running_snapshot)
        # 3 reads happen before the pre-attention re-check: the early bind
        # gate, the running branch's claim read, and its own bind read.
        self.claim = LateBindClaimRegistry(self.claim, unbound_reads=3)
        result = self.tick(active)
        self.assertGreater(self.claim.reads, 3, "the pre-attention re-check must take its own read")
        self.assertTrue(result.get("skipped"))
        self.assertEqual("terminal_bind_projection_stale", result.get("reason"))
        self.assertEqual(before, self.execution())
        self.assert_bind_immutable(bound_before)

        self.store.clear_stale_executions()
        self.ticks(3)
        self.assert_converged_to_bind()
        self.assert_bind_immutable(bound_before)
        self.assert_no_repo_execution_side_effects()

    def test_i12_an_unreadable_lock_registry_refuses_to_release_the_claim(self):
        """Codex adversarial review finding, reproduced and closed. An
        execution that names a specific lock generation must be verified
        against the registry and nowhere else: falling back to the Drive
        evidence copy when the registry cannot be read would release the task
        claim while a production writer may still hold the lock."""
        self.partial_terminal_failed()
        self.assertTrue(retry_incomplete_terminal_persistence(
            self.store, "p1", "t1", self.EXECUTION_ID, claim_registry=self.claim))
        lying = self.execution()
        lying["cleanup_evidence"] = {**lying["cleanup_evidence"], "writer_release": "released"}
        Store.put(self.store, "executions", "p1", self.EXECUTION_ID, lying)
        result = recover_task_claim(self.store, self.claim, "p1", "t1", writer_registry=None)
        self.assertEqual("writer_authority_not_confirmed_released", result["reason"])
        self.assertTrue(self.claim.document["authority_active"])
        self.assertEqual("active", self.lock()["status"])

    def test_two_concurrent_reconcilers_converge_to_one_bound_truth(self):
        """Two watcher processes reconciling the same Command at the same
        time. Drive has no CAS, so their Execution writes genuinely can lose
        each other's updates -- what must NOT happen is two terminal truths,
        two writer-lease generations, two Executions, or a permanently
        un-converged task. The authorities (Task Root CAS, lock registry CAS)
        are the real doubles here and enforce that."""
        self.partial_terminal_failed()
        bound_before = deepcopy(self.bind())
        lease_generation = self.lock()["generation"]
        start = threading.Barrier(2)
        errors = []

        def worker():
            try:
                start.wait(timeout=5)
                for _ in range(4):
                    self.tick()
            except Exception as exc:  # noqa: BLE001 -- surfaced as a failure below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual([], errors, "a concurrent reconciler must never raise out of the tick")

        self.ticks(2)
        self.assert_bind_immutable(bound_before)
        self.assert_converged_to_bind()
        self.assertEqual(lease_generation, self.lock()["generation"],
                         "two reconcilers must never produce a second writer lease generation")
        self.assertEqual([self.EXECUTION_ID],
                         [record["execution_id"] for record in self.store.list_records("executions", "p1")])
        self.assert_no_repo_execution_side_effects()

    def test_i12_cleanup_evidence_matches_registry_truth(self):
        execution = self.converged()
        self.assertEqual("released", execution["cleanup_evidence"]["writer_release"])
        self.assertEqual("released", self.lock()["status"])
        self.assertEqual("released", execution["cleanup_evidence"]["task_claim_release"])
        self.assertFalse(self.claim.document["authority_active"])


if __name__ == "__main__":
    unittest.main()
