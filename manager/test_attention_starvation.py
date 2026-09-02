"""Deterministic reproduction of the 2026-09-02 attention-scheduling starvation.

Live evidence this models (all read-only observations of production):
- `dispatch-adm-provider-reliability-adversarial-ag-20260822-0018`, an
  Antigravity Command stuck in attention/hard_timeout_exceeded_provider_
  unknown with its Execution still "running", was reprocessed on every
  tick; `_attention()` rewrote identical content each time, refreshing the
  record's Drive modifiedTime, so it stayed rank 0 of the `modifiedTime
  desc` recent sweep (RECENT_COMMANDS_PER_PROJECT = 2) forever.
- `dispatch-adm-close-gh-dispatch-test-determinism-20260901T155956Z`, the
  Command whose convergence fix had just been activated, sat at rank 1 of
  that same 2-record window and was never processed across 60+ minutes of
  healthy ticks: its modifiedTime stayed frozen and its Task stayed
  `ready` (any reconcile pass would have written both).

The fake store charges wall-clock cost per Drive read/write against the
same clock poll_once() uses for its budgets, so the recent sweep's 25s
budget is exhausted by the flapper's own reconciliation exactly as it was
in production.
"""

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import json

from manager.command_watcher import (
    RECENT_COMMANDS_PER_PROJECT, _attention, _prioritized_nonterminal_commands, poll_once,
)
from manager.phase1_cursor import load_phase1_cursor, save_phase1_cursor
from manager.execution_lifecycle import enter_running_gate
from manager.executions import cancel_reserved_execution, reserve_execution
from manager.tasks import TaskError, create_project, create_task, now_iso
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import MemoryRegistry
from manager.worktree_locks import canonical_repository, repository_lock_id


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class FakeClock:
    """Drop-in for manager.command_watcher's `time` module: budgets and the
    rotation tick counter both read from here, and the store advances it."""

    def __init__(self, start=1_000_000.0):
        self.now = start
        # Wall clock drives the rotation tick counter (int(time()//60)); it is
        # advanced by exactly one POLL_SECONDS per simulated tick so the
        # attention-group rotation is a strict, deterministic round-robin.
        self.wall = start

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall

    def next_tick(self):
        self.wall += 60.0

    def sleep(self, seconds):
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class CostedStore(Store):
    """Store that (1) charges the fake clock per Drive call and (2) orders a
    `modifiedTime desc` bounded listing by real write order, exactly like
    DriveRecords.list_records_bounded does against Drive."""

    def __init__(self, clock, projects, read_cost=1.0, write_cost=6.0):
        super().__init__()
        self.clock, self.projects = clock, list(projects)
        self.read_cost, self.write_cost = read_cost, write_cost
        self.modified = {}
        self.writes = []
        self._serial = 0

    def list_projects(self):
        return [self.get("projects", p, p) for p in self.projects]

    def get(self, area, project_id, name):
        self.clock.advance(self.read_cost)
        return super().get(area, project_id, name)

    def put(self, area, project_id, name, document):
        self.clock.advance(self.write_cost)
        self._serial += 1
        self.modified[(area, project_id, name)] = self._serial
        self.writes.append((area, project_id, name))
        return super().put(area, project_id, name, document)

    def list_records_bounded(self, area, project_id, deadline=None, single_request_worst_case=None,
                             max_records=None, order_by=None, rotate_offset=0):
        self.clock.advance(self.read_cost)
        items = [(key, value) for key, value in self.records.items() if key[0] == area and key[1] == project_id]
        if order_by == "modifiedTime desc":
            items.sort(key=lambda item: self.modified.get(item[0], 0), reverse=True)
        else:
            items.sort(key=lambda item: item[0][2])
            if items and rotate_offset:
                shift = rotate_offset % len(items)
                items = items[shift:] + items[:shift]
        if max_records is not None:
            items = items[:max_records]
        return [deepcopy(value) for _, value in items]


def foreign_released_lock(lock_id, canonical):
    # The exact lock shape observed live: owned by the PREVIOUS task, status already released.
    return {
        "project_id": "p1", "task_id": "t-previous", "execution_id": "command-t-previous",
        "provider": "codex", "session_id": "codex:previous-session", "lock_id": lock_id,
        "repository": canonical, "branch": "refs/heads/feat/previous", "scope": ["manager/executions.py"],
        "baseline_head": "a" * 40, "access": "production", "status": "released", "generation": 40,
        "lease_token_hash": "0" * 64, "created_at": "2026-09-01T15:43:14.000000Z",
        "updated_at": "2026-09-01T15:52:10.000000Z", "expires_at": "2026-09-01T16:43:14.000000Z",
        "released_at": "2026-09-01T15:52:10.000000Z",
    }


class ProductionShapeFixture:
    """One project holding the two live records, plus their authority stores."""

    def __init__(self, clock, project_id="p1", read_cost=1.0, write_cost=6.0):
        self.clock = clock
        self.project_id = project_id
        self.store = CostedStore(clock, [project_id], read_cost=read_cost, write_cost=write_cost)
        self.claims = {}
        create_project(self.store, {**project(), "project_id": project_id, "active_tasks": []})
        self.stale_at = "2026-09-01T16:12:37.479272Z"
        self._build_flapper()
        self._build_stuck()
        # Foreign released repo lock, exactly as observed.
        canonical = canonical_repository(self.store.get("projects", project_id, project_id)["repo"])
        self.lock_id = repository_lock_id(canonical)
        self.lock_before = foreign_released_lock(self.lock_id, canonical)
        self.lock_registry = MemoryRegistry({"schema_version": "0.2.0", "locks": {self.lock_id: deepcopy(self.lock_before)}})
        self.store.writes.clear()

    def claim_factory(self, _bucket, project_id, task_id):
        return self.claims.setdefault((project_id, task_id), MemoryClaimRegistry())

    def _build_flapper(self):
        # August AG command: Execution "running" with an exact claim, hard
        # timeout long past, provider evidence from another host -> reconcile
        # reason hard_timeout_exceeded_provider_unknown, every tick.
        flapper_task = {**task(read_only=True), "task_id": "t-flap", "project_id": self.project_id}
        create_task(self.store, flapper_task, assign=False)
        reserve_execution(self.store, self.project_id, "t-flap", "command-cmd-flap", "antigravity", {"decision": "fresh"})
        started = "2026-08-21T16:27:17.296867Z"
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, self.project_id, "t-flap", "command-cmd-flap", "antigravity",
                               "read_only", started_at=started, task_claim_registry=self.claim_factory(None, self.project_id, "t-flap"))
        execution = self.store.get("executions", self.project_id, "command-cmd-flap")
        execution.update(heartbeat_at=started, progress_updated_at=started, last_provider_event="provider_wait",
                         hard_timeout_at="2026-08-21T17:27:17Z",
                         provider_evidence={"host": "some-other-host", "pid": 4321, "creation_identity": "windows-filetime:1",
                                            "started_at": started})
        self.store.put("executions", self.project_id, "command-cmd-flap", execution)
        self.store.put("commands", self.project_id, "cmd-flap", command(
            command_id="cmd-flap", project_id=self.project_id, task_id="t-flap", provider="antigravity",
            status="attention", execution_id="command-cmd-flap", claimed_at=started,
            stale_at="2026-08-21T16:31:24.740306Z", recovery_reason="hard_timeout_exceeded_provider_unknown"))

    def _build_stuck(self):
        # The convergence-fix acceptance object: cancelled Execution, writable
        # task, attention/terminal_writer_authority_reconciliation_unknown.
        create_task(self.store, {**task(read_only=False), "project_id": self.project_id}, assign=False)
        reserve_execution(self.store, self.project_id, "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        cancel_reserved_execution(self.store, MemoryClaimRegistry(), self.project_id, "command-cmd-1",
                                  "prelaunch failure left a reservation without provider authority")
        self.store.put("commands", self.project_id, "cmd-1", command(
            project_id=self.project_id, status="attention", execution_id="command-cmd-1", claimed_at="2026-09-01T16:04:14.847837Z",
            stale_at=self.stale_at, recovery_reason="terminal_writer_authority_reconciliation_unknown",
            worker_pid=48248, worker_creation_identity="windows-filetime:134327522611029922",
            worker_spawned_at="2026-09-01T16:04:21.113587Z"))
        # Live ordering: the flapper was rewritten AFTER the stuck record, so it is rank 0.
        flapper = self.store.get("commands", self.project_id, "cmd-flap")
        self.store.put("commands", self.project_id, "cmd-flap", flapper)

    def tick(self, launch, cursor_path):
        with patch("manager.command_watcher.time", self.clock), \
             patch("manager.command_watcher.launch_task", launch), \
             patch("manager.command_watcher.GCSLockRegistry.from_environment", return_value=self.lock_registry):
            return poll_once(self.store, object(), allowlist=frozenset(), claim_factory=self.claim_factory,
                             health_check=lambda: True, quota_check=lambda service: True, cursor_path=cursor_path)

    def rank(self, command_id):
        ordered = self.store.list_records_bounded("commands", self.project_id, order_by="modifiedTime desc")
        return next(i for i, c in enumerate(ordered) if c["command_id"] == command_id)


class AttentionNoOpSuppressionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.fixture = ProductionShapeFixture(self.clock)
        self.store = self.fixture.store

    def test_identical_attention_content_is_not_rewritten(self):
        stored = self.store.get("commands", "p1", "cmd-flap")
        execution = self.store.get("executions", "p1", "command-cmd-flap")
        # First pass may legitimately settle the Execution/Task evidence.
        _attention(self.store, deepcopy(stored), execution, "hard_timeout_exceeded_provider_unknown")
        self.store.writes.clear()
        result = _attention(self.store, deepcopy(stored), execution, "hard_timeout_exceeded_provider_unknown")
        self.assertEqual("attention", result["status"])
        self.assertTrue(result.get("unchanged"))
        self.assertEqual([], self.store.writes, "identical attention content must not touch Drive")
        self.assertEqual(stored, self.store.get("commands", "p1", "cmd-flap"))

    def test_changed_attention_content_is_still_persisted(self):
        stored = self.store.get("commands", "p1", "cmd-flap")
        execution = self.store.get("executions", "p1", "command-cmd-flap")
        _attention(self.store, deepcopy(stored), execution, "hard_timeout_exceeded_provider_unknown")
        self.store.writes.clear()
        result = _attention(self.store, deepcopy(stored), execution, "provider_process_identity_replaced")
        self.assertFalse(result.get("unchanged"))
        self.assertIn(("commands", "p1", "cmd-flap"), self.store.writes)
        self.assertEqual("provider_process_identity_replaced", self.store.get("commands", "p1", "cmd-flap")["recovery_reason"])
        # Execution evidence changed too -> persisted, once.
        self.assertIn(("executions", "p1", "command-cmd-flap"), self.store.writes)


class AttentionRotationTests(unittest.TestCase):
    def test_rotation_only_reorders_the_attention_group(self):
        claimed = command(command_id="c", status="claimed", execution_id="e")
        queued = command(command_id="q")
        a = command(command_id="a", status="attention", execution_id="ea")
        b = command(command_id="b", status="attention", execution_id="eb")
        done = command(command_id="d", status="completed", execution_id="ed")
        batch = [a, done, b, queued, claimed]
        ids = lambda seq: [c["command_id"] for c in seq]
        self.assertEqual(["c", "q", "a", "b"], ids(_prioritized_nonterminal_commands(batch)))
        self.assertEqual(["c", "q", "b", "a"], ids(_prioritized_nonterminal_commands(batch, attention_rotation=1)))
        self.assertEqual(["c", "q", "a", "b"], ids(_prioritized_nonterminal_commands(batch, attention_rotation=2)))
        self.assertEqual(["c", "q"], ids(_prioritized_nonterminal_commands([queued, claimed], attention_rotation=7)))


class ProductionStarvationReproductionTests(unittest.TestCase):
    """The live timing, replayed deterministically, then the guarantees."""

    def setUp(self):
        self.clock = FakeClock()
        self.fixture = ProductionShapeFixture(self.clock)
        self.store = self.fixture.store
        self.launch = Mock(side_effect=AssertionError("no provider may ever be launched here"))
        self.cursor_path = tempfile.mktemp(suffix=".json")
        self.assertEqual(0, self.fixture.rank("cmd-flap"))
        self.assertEqual(1, self.fixture.rank("cmd-1"))
        self.assertEqual(2, RECENT_COMMANDS_PER_PROJECT)

    def ticks(self, count):
        for gap in (120.0, 90.0, 180.0, 120.0, 90.0, 120.0)[:count]:
            self.fixture.tick(self.launch, self.cursor_path)
            self.clock.advance(60.0)
            self.clock.wall += gap  # real cadence: never a tidy 60s bucket

    def test_stuck_rank1_command_converges_within_bounded_ticks(self):
        # Before the fix this stayed attention forever (60+ real minutes).
        self.ticks(3)
        stuck = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("failed", stuck["status"], stuck)
        self.assertEqual("prelaunch_failed", stuck["result"]["error_kind"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])
        self.assertEqual("cancelled", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.launch.assert_not_called()
        self.assertIsNone(self.fixture.claims[("p1", "t1")].document)
        self.assertEqual(self.fixture.lock_before, self.fixture.lock_registry.document["locks"][self.fixture.lock_id])
        # Repeated ticks stay stable: terminal stays terminal, nothing relaunches.
        self.store.writes.clear()
        self.ticks(3)
        self.assertEqual("failed", self.store.get("commands", "p1", "cmd-1")["status"])
        self.launch.assert_not_called()
        self.assertNotIn(("commands", "p1", "cmd-1"), self.store.writes)

    def test_flapper_no_longer_refreshes_itself_every_tick(self):
        self.ticks(1)  # may settle its Execution/Task evidence exactly once
        flapper_before = self.store.get("commands", "p1", "cmd-flap")
        self.store.writes.clear()
        self.ticks(3)
        flapper_writes = [w for w in self.store.writes if w[2] in ("cmd-flap", "command-cmd-flap", "t-flap")]
        self.assertEqual([], flapper_writes, "unchanged attention flapper must not be rewritten every tick")
        self.assertEqual(flapper_before, self.store.get("commands", "p1", "cmd-flap"))
        self.assertEqual("attention", flapper_before["status"])
        self.assertEqual("hard_timeout_exceeded_provider_unknown", flapper_before["recovery_reason"])
        # Its modifiedTime no longer moves, so the genuinely newer write (the
        # converged rank-1 record) now outranks it in the recent sweep.
        self.assertEqual(0, self.fixture.rank("cmd-1"))
        self.assertEqual(1, self.fixture.rank("cmd-flap"))

    def test_rotation_alone_rescues_rank1_when_flapper_reads_exceed_the_budget(self):
        # Even with no-op suppression, a flapper whose READS alone blow the
        # recent-sweep budget would still starve rank 1 on every tick where
        # it goes first. The attention-group rotation guarantees rank 1
        # goes first within RECENT_COMMANDS_PER_PROJECT consecutive ticks.
        expensive = ProductionShapeFixture(self.clock, read_cost=6.0, write_cost=6.0)
        cursor_path = tempfile.mktemp(suffix=".json")
        for gap in (120.0, 120.0, 120.0, 120.0):  # constant even cadence: wall-clock parity never changes
            expensive.tick(self.launch, cursor_path)
            self.clock.advance(60.0)
            self.clock.wall += gap
        self.assertEqual("failed", expensive.store.get("commands", "p1", "cmd-1")["status"])
        self.launch.assert_not_called()


class DurableVisitRotationTests(unittest.TestCase):
    """Independent-review finding (round 1): rotation must not depend on the
    wall clock. Real ticks run ~90-120s apart, so a per-60s bucket counter
    advances by 2 per sweep -- a 2-record attention group would then NEVER
    rotate, and skipped buckets can exclude a residue class of a larger
    group forever. The rotation base is now a durable per-project count of
    actual sweeps, so cadence cannot matter."""

    def _attention_batch_store(self, clock, count):
        store = CostedStore(clock, ["p1"], read_cost=1.0, write_cost=6.0)
        create_project(store, {**project(), "project_id": "p1", "active_tasks": []})
        for index in range(count):
            store.put("commands", "p1", f"att-{index}", command(
                command_id=f"att-{index}", task_id=f"t-{index}", status="attention", execution_id=f"exec-{index}",
                claimed_at="2026-08-21T16:27:17Z", stale_at="2026-08-21T16:31:24Z", recovery_reason="some_reason"))
        return store

    def _sweep(self, store, clock, cursor_path, first_seen, wall_gap, reconcile_cost=30.0, recent=3, max_commands=4,
               exits=None):
        # Each reconciliation of an attention record burns the whole
        # recent-sweep budget, so only the FIRST attention record of the
        # batch is ever processed per sweep -- the live shape.
        def reconcile(_store, _service, cmd, _claims, **_kw):
            first_seen.append(cmd["command_id"])
            clock.advance(reconcile_cost)
            return {"status": "attention", "execution_id": cmd["execution_id"], "recovery_reason": "some_reason"}

        with patch("manager.command_watcher.time", clock), \
             patch("manager.command_watcher.RECENT_COMMANDS_PER_PROJECT", recent), \
             patch("manager.command_watcher.MAX_COMMANDS_PER_POLL", max_commands), \
             patch("manager.command_watcher._reconcile_active", side_effect=reconcile), \
             patch("manager.command_watcher.launch_task", Mock(side_effect=AssertionError("no launch"))):
            results = poll_once(store, object(), allowlist=frozenset(), claim_factory=lambda *_: MemoryClaimRegistry(),
                                health_check=lambda: True, quota_check=lambda service: True, cursor_path=cursor_path)
        if exits is not None:
            exits.append(len(results))
        clock.advance(60.0)
        clock.wall += wall_gap

    def test_three_record_group_with_skipped_wall_clock_buckets_is_a_strict_round_robin(self):
        clock = FakeClock()
        store = self._attention_batch_store(clock, 3)
        cursor_path = tempfile.mktemp(suffix=".json")
        first_seen = []
        # The exact cadence from the review: ~90s sweeps skip every third
        # 60s bucket (0,1,3,4,6,7,...); plus one 180s gap for good measure.
        for gap in (90.0, 90.0, 180.0, 90.0, 90.0, 120.0):
            self._sweep(store, clock, cursor_path, first_seen, gap)
        self.assertEqual(6, len(first_seen))
        self.assertEqual({"att-0", "att-1", "att-2"}, set(first_seen[:3]),
                         f"every attention record must be first within 3 consecutive sweeps; got {first_seen}")
        self.assertEqual({"att-0", "att-1", "att-2"}, set(first_seen[3:6]))
        cursor = load_phase1_cursor(cursor_path=cursor_path)
        self.assertEqual(6, cursor["per_project_attention_visits"]["p1"])

    def test_visit_counter_persists_and_generation_only_ever_increases(self):
        # Bounded delta review round 2: the visit save must advance the
        # generation like every other save, so a delayed writer holding an
        # older generation can never roll the file backward.
        cursor_path = tempfile.mktemp(suffix=".json")
        saved = save_phase1_cursor({"project_cursor": 1, "per_project_record_cursor": {"p1": 4},
                                    "per_project_attention_visits": {"p1": 2}, "generation": 10}, cursor_path=cursor_path)
        self.assertEqual(11, saved["generation"])
        again = save_phase1_cursor({**saved, "per_project_attention_visits": {"p1": 3}}, cursor_path=cursor_path,
                                   expected_generation=11)
        self.assertEqual(12, again["generation"])
        loaded = load_phase1_cursor(cursor_path=cursor_path)
        self.assertEqual({"p1": 3}, loaded["per_project_attention_visits"])
        self.assertEqual({"p1": 4}, loaded["per_project_record_cursor"])
        self.assertEqual(12, loaded["generation"])
        # A writer that lost the race (stale expected generation) is refused, never applied.
        from manager.phase1_cursor import StaleCursorError
        with self.assertRaises(StaleCursorError):
            save_phase1_cursor({**saved, "per_project_attention_visits": {"p1": 99}}, cursor_path=cursor_path,
                               expected_generation=11)
        self.assertEqual({"p1": 3}, load_phase1_cursor(cursor_path=cursor_path)["per_project_attention_visits"])
        # Legacy cursor files without the field load cleanly.
        pathlib_path = __import__("pathlib").Path(cursor_path)
        pathlib_path.write_text(json.dumps({"project_cursor": 0, "per_project_record_cursor": {}, "generation": 3}), encoding="utf-8")
        self.assertEqual({}, load_phase1_cursor(cursor_path=cursor_path)["per_project_attention_visits"])

    def test_visit_is_persisted_even_when_the_full_sweep_exits_on_its_deadline(self):
        # Bounded delta review round 2 (high): the full sweep `return`s when
        # the poll deadline passes mid-batch. That exit happens AFTER the
        # recent sweep already decided this tick's rotation, so the visit
        # must still be counted or next tick repeats the same rotation
        # forever. Timing (40s poll deadline, 25s recent budget, 10s per
        # reconciliation, 2-record recent window, 5 attention records,
        # per-poll command cap lifted): recent sweep processes 2 (~23s), the
        # full sweep hydrates before its 25s hydration deadline, processes
        # the 3rd (~34s) and 4th (~44s), then crosses the 40s poll deadline
        # before the 5th -> `return results` with 4 results.
        clock = FakeClock()
        store = self._attention_batch_store(clock, 5)
        cursor_path = tempfile.mktemp(suffix=".json")
        first_seen, exits = [], []
        self._sweep(store, clock, cursor_path, first_seen, 120.0, reconcile_cost=10.0, recent=2, max_commands=10, exits=exits)
        self.assertEqual([4], exits, "the sweep must have exited on the deadline `return` after 4 of 5 records")
        self.assertEqual(1, load_phase1_cursor(cursor_path=cursor_path)["per_project_attention_visits"].get("p1"),
                         "the visit must be durable even though the sweep exited on its deadline")
        first_of_sweep_1 = first_seen[0]
        self._sweep(store, clock, cursor_path, first_seen, 120.0, reconcile_cost=10.0, recent=2, max_commands=10, exits=exits)
        self.assertEqual([4, 4], exits)
        self.assertEqual(2, load_phase1_cursor(cursor_path=cursor_path)["per_project_attention_visits"].get("p1"))
        self.assertNotEqual(first_of_sweep_1, first_seen[4],
                            "rotation must advance across a deadline exit, not replay the same first record")

    def test_one_invocation_saves_the_cursor_exactly_once_with_its_visit(self):
        # Bounded delta review round 3: two advancing saves per invocation
        # let an overlapping writer roll the generation back through the
        # non-atomic CAS. One save per invocation carries the Phase-1
        # advance AND the attention visit together.
        from manager.phase1_cursor import save_phase1_cursor as real_save
        clock = FakeClock()
        store = self._attention_batch_store(clock, 5)
        cursor_path = tempfile.mktemp(suffix=".json")
        saves = []

        def counting_save(cursor_data, **kwargs):
            saves.append(deepcopy(cursor_data))
            return real_save(cursor_data, **kwargs)

        with patch("manager.phase1_cursor.save_phase1_cursor", side_effect=counting_save):
            self._sweep(store, clock, cursor_path, [], 120.0, reconcile_cost=10.0, recent=2, max_commands=10)
        self.assertEqual(1, len(saves), f"exactly one cursor save per invocation, got {len(saves)}")
        self.assertEqual(1, saves[0]["per_project_attention_visits"]["p1"])
        loaded = load_phase1_cursor(cursor_path=cursor_path)
        self.assertEqual(1, loaded["generation"], "one invocation advances the generation exactly once")
        self.assertEqual(1, loaded["per_project_attention_visits"]["p1"])
        self.assertIn("p1", loaded["per_project_record_cursor"], "the Phase-1 record advance rides in the same save")

    def test_single_attention_record_does_not_consume_a_visit(self):
        clock = FakeClock()
        store = self._attention_batch_store(clock, 1)
        cursor_path = tempfile.mktemp(suffix=".json")
        self._sweep(store, clock, cursor_path, [], 90.0)
        self.assertEqual({}, load_phase1_cursor(cursor_path=cursor_path)["per_project_attention_visits"])


class MultiProjectFairnessTests(unittest.TestCase):
    def test_repeated_ticks_reach_every_project_and_launch_queued_work_exactly_once(self):
        clock = FakeClock()
        fixture = ProductionShapeFixture(clock)
        store = fixture.store
        # Two more projects: one with its own attention flapper only, one with fresh queued work.
        for extra in ("p2", "p3"):
            create_project(store, {**project(), "project_id": extra, "active_tasks": []})
            store.projects.append(extra)
        t2 = {**task(read_only=True), "project_id": "p2", "task_id": "t2"}
        create_task(store, t2, assign=False)
        store.put("commands", "p2", "cmd-p2", command(command_id="cmd-p2", project_id="p2", task_id="t2",
                                                       status="attention", execution_id="command-cmd-p2",
                                                       claimed_at=iso(datetime.now(timezone.utc) - timedelta(hours=2)),
                                                       stale_at=now_iso(), recovery_reason="execution_record_missing_or_invalid"))
        t3 = {**task(read_only=True), "project_id": "p3", "task_id": "t3"}
        compliant = create_task(store, t3, assign=False, persist=False)
        compliant["execution_policies"] = sorted(__import__("manager.trusted_ingress", fromlist=["x"]).REQUIRED_TASK_POLICIES)
        store.put("tasks", "p3", "t3", compliant)
        store.put("commands", "p3", "cmd-p3", command(command_id="cmd-p3", project_id="p3", task_id="t3"))
        launched = []

        def launch(*args, **kwargs):
            launched.append(args[7])
            kwargs["on_running"](None)
            from manager.test_command_watcher import CommandWatcherTests
            return CommandWatcherTests.complete(args[7])

        cursor_path = tempfile.mktemp(suffix=".json")
        for _ in range(6):
            with patch("manager.command_watcher.time", clock), \
                 patch("manager.command_watcher.launch_task", side_effect=launch), \
                 patch("manager.command_watcher._focus_adm_ui_best_effort", Mock()), \
                 patch("manager.command_watcher.GCSLockRegistry.from_environment", return_value=fixture.lock_registry):
                poll_once(store, object(), allowlist=frozenset({("p3", "t3")}), claim_factory=fixture.claim_factory,
                          health_check=lambda: True, quota_check=lambda service: True, cursor_path=cursor_path)
            clock.advance(60.0)
            clock.next_tick()
        self.assertEqual(["command-cmd-p3"], launched, "queued work launches exactly once across repeated ticks")
        self.assertEqual("completed", store.get("commands", "p3", "cmd-p3")["status"])
        self.assertEqual("failed", store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual("attention", store.get("commands", "p1", "cmd-flap")["status"])
        self.assertEqual(fixture.lock_before, fixture.lock_registry.document["locks"][fixture.lock_id])


if __name__ == "__main__":
    unittest.main()
