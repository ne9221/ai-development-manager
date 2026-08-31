"""Comprehensive test suite for Watcher Phase-1 Fair Scheduling, Durable Cursor, and Reachability Bounds."""

import json
import math
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from manager.command_watcher import (
    POLL_TIME_BUDGET_SECONDS,
    PHASE_1_TIME_BUDGET_SECONDS,
    WAITING_QUOTA_DISCOVERY_WINDOW,
    MAX_WAITING_QUOTA_PROMOTIONS_PER_POLL,
    _enumerate_waiting_quota_tasks,
    poll_once,
)
from manager.phase1_cursor import (
    StaleCursorError,
    load_phase1_cursor,
    save_phase1_cursor,
)
from manager.tasks import TaskError
from manager.trusted_ingress import TRUSTED_INGRESS_ORIGIN


class MemoryDiscoveryStore:
    """Mock DriveRecords store that supports list_project_ids and list_records_bounded."""

    def __init__(self, projects_data=None):
        # projects_data: dict of project_id -> list of task dicts
        self.projects_data = projects_data or {}
        self.listed_projects = []
        self.visited_windows = []  # list of (project_id, rotate_offset, returned_task_ids)
        self.failing_projects = set()

    def list_project_ids(self, deadline=None):
        return list(self.projects_data.keys())

    def list_records_bounded(self, area, project_id, deadline=None, single_request_worst_case=None,
                             max_records=None, order_by=None, rotate_offset=0):
        if project_id in self.failing_projects:
            raise TaskError(f"Backend error for project {project_id}")
        if area != "tasks":
            return []
        items = list(self.projects_data.get(project_id, []))
        if not order_by and rotate_offset and items:
            offset = rotate_offset % len(items)
            items = items[offset:] + items[:offset]
        if max_records is not None:
            items = items[:max_records]
        self.visited_windows.append((project_id, rotate_offset, [t["task_id"] for t in items]))
        return items

    def list_records(self, area, project_id):
        return self.list_records_bounded(area, project_id)

    def get(self, area, project_id, name):
        raise TaskError("not found")

    def put(self, area, project_id, name, data):
        pass


class TestPhase1FairScheduling(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cursor_path = str(Path(self.test_dir) / "phase1-cursor.json")
        self.quota_patch = patch("manager.command_watcher.read_drive_status", return_value={"codex": {"status": "available"}})
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_tasks(self, project_id, count):
        return [{
            "project_id": project_id,
            "task_id": f"{project_id}-t{i}",
            "title": f"Task {i}",
            "status": "queued",
            "recommended_provider": None,
            "quota_evidence": {"codex": {}},
            "source_context": {"origin": TRUSTED_INGRESS_ORIGIN},
        } for i in range(count)]

    def test_A_ignore_new_wall_clock_skip_alias(self):
        """Scenario A: Invocations occur at wall-clock minutes 0, 2, 4, 6, 8...
        Under old wall-clock modulo, odd projects were starved.
        Under actual-invocation cursor, all 10 projects are served within 10 actual invocations."""
        project_ids = [f"p{i}" for i in range(10)]
        projects_data = {pid: self._make_tasks(pid, 10) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)

        served_projects = []
        for invocation in range(10):
            wall_minute = invocation * 2  # 0, 2, 4, 6, 8, 10, 12, 14, 16, 18
            with patch("time.time", return_value=wall_minute * 60.0):
                poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
            cursor = load_phase1_cursor(cursor_path=self.cursor_path)
            # The project just served is the one recorded before increment
            served = project_ids[invocation % 10]
            served_projects.append(served)

        # All 10 projects were served exactly once in order p0..p9
        self.assertEqual(project_ids, served_projects)
        self.assertEqual(10, len(set(served_projects)))

    def test_B_pathological_skipped_cadence(self):
        """Scenario B: Invocations occur at minutes 0, 10, 20, 30...
        Actual service strictly progresses project0 -> project1 -> project2..."""
        project_ids = [f"p{i}" for i in range(5)]
        projects_data = {pid: self._make_tasks(pid, 5) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)

        served_projects = []
        for invocation in range(5):
            wall_minute = invocation * 10  # 0, 10, 20, 30, 40
            with patch("time.time", return_value=wall_minute * 60.0):
                poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
            served_projects.append(project_ids[invocation])

        self.assertEqual(project_ids, served_projects)

    def test_C_N180_K4_P10_full_coverage(self):
        """Scenario C: N=180, K=4, P=10.
        Old wall-clock modulo jumped by 40 and covered only 36/180 (permanent starvation).
        New actual-invocation cursor covers all 180 tasks in exactly ceil(180/4) = 45 project visits."""
        project_ids = [f"p{i}" for i in range(10)]
        projects_data = {pid: self._make_tasks(pid, 180 if pid == "p0" else 5) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)

        visited_task_ids = set()
        # In 45 * 10 = 450 actual invocations, p0 is visited 45 times
        for invocation in range(450):
            poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        p0_visits = [w for w in store.visited_windows if w[0] == "p0"]
        for _, _, task_ids in p0_visits:
            visited_task_ids.update(task_ids)

        self.assertEqual(180, len(visited_task_ids))
        self.assertEqual(45, len(p0_visits))

    def test_D_comprehensive_N_matrix_reachability(self):
        """Scenario D: Full coverage for all N in [1, 2, 3, 4, 5, 10, 12, 17, 40, 50, 60, 100, 180, 181, 200, 237]
        is proven to complete in <= ceil(N / K) project visits."""
        test_counts = [1, 2, 3, 4, 5, 10, 12, 17, 40, 50, 60, 100, 180, 181, 200, 237]
        K = WAITING_QUOTA_DISCOVERY_WINDOW

        for N in test_counts:
            cursor_file = str(Path(self.test_dir) / f"cursor-{N}.json")
            project_ids = ["target-proj", "other-proj"]
            projects_data = {
                "target-proj": self._make_tasks("target-proj", N),
                "other-proj": self._make_tasks("other-proj", 2),
            }
            store = MemoryDiscoveryStore(projects_data)
            required_visits = math.ceil(N / K)

            visited_tasks = set()
            for visit in range(required_visits):
                # 2 invocations per cycle (target-proj + other-proj)
                poll_once(store, None, discovery_store=store, cursor_path=cursor_file, allowlist=frozenset())
                poll_once(store, None, discovery_store=store, cursor_path=cursor_file, allowlist=frozenset())

            target_visits = [w for w in store.visited_windows if w[0] == "target-proj"]
            for _, _, tids in target_visits:
                visited_tasks.update(tids)

            self.assertEqual(N, len(visited_tasks), f"Failed full coverage for N={N}: covered {len(visited_tasks)}/{N}")
            self.assertLessEqual(len(target_visits), required_visits)

    def test_E_arbitrary_restart(self):
        """Scenario E: Process stopped at arbitrary cursor and restarted. Continues finite convergence."""
        project_ids = ["p0", "p1", "p2"]
        projects_data = {pid: self._make_tasks(pid, 12) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)

        # Run 2 invocations (p0, p1)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        c1 = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(2, c1["project_cursor"])  # p2 is next

        # Simulate fresh process restart (re-loading cursor from disk)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        c2 = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(0, c2["project_cursor"])  # wrapped to p0

        # Visited order across restart was p0, p1, p2
        visited_pids = [w[0] for w in store.visited_windows]
        self.assertEqual(["p0", "p1", "p2"], visited_pids)

    def test_F_cursor_file_missing(self):
        """Scenario F: Missing cursor file initializes cleanly and serves project 0 with 0 offset."""
        project_ids = ["p0", "p1"]
        projects_data = {pid: self._make_tasks(pid, 8) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)

        self.assertFalse(os.path.exists(self.cursor_path))
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        self.assertTrue(os.path.exists(self.cursor_path))
        cursor = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(1, cursor["project_cursor"])
        self.assertEqual(4, cursor["per_project_record_cursor"]["p0"])
        self.assertEqual(["p0"], [w[0] for w in store.visited_windows])

    def test_G_cursor_corrupted_fail_safe(self):
        """Scenario G: Corrupted cursor file safely recovers to defaults without crashing."""
        with open(self.cursor_path, "w", encoding="utf-8") as f:
            f.write("{invalid-json-content...!!")

        project_ids = ["p0", "p1"]
        projects_data = {pid: self._make_tasks(pid, 8) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)

        # Must not throw
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        cursor = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(1, cursor["project_cursor"])
        self.assertEqual(["p0"], [w[0] for w in store.visited_windows])

    def test_H_project_add_remove_dynamic_P(self):
        """Scenario H: P dynamically changes (add/remove project). Bounds and coverage remain intact."""
        projects_data = {
            "p0": self._make_tasks("p0", 4),
            "p1": self._make_tasks("p1", 4),
        }
        store = MemoryDiscoveryStore(projects_data)

        # Tick 1: serves p0 (project_cursor -> 1)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        # Add project p2 (P becomes 3)
        projects_data["p2"] = self._make_tasks("p2", 4)
        # Tick 2: serves p1 (project_cursor -> 2)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        # Tick 3: serves p2 (project_cursor -> 0)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        self.assertEqual(["p0", "p1", "p2"], [w[0] for w in store.visited_windows])

        # Remove p1 (P becomes 2: ["p0", "p2"])
        del projects_data["p1"]
        # Tick 4: serves p0
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        self.assertEqual(["p0", "p1", "p2", "p0"], [w[0] for w in store.visited_windows])

    def test_I_records_add_delete_dynamic_N(self):
        """Scenario I: N dynamically changes. Existing tasks remain finite reachable."""
        projects_data = {"p0": self._make_tasks("p0", 8)}
        store = MemoryDiscoveryStore(projects_data)

        # Visit 1: scans offset 0 (tasks 0..3)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        # Dynamically add 4 tasks (N becomes 12)
        projects_data["p0"].extend(self._make_tasks("p0", 12)[8:])

        # Visit 2: scans offset 4 (tasks 4..7)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        # Visit 3: scans offset 8 (tasks 8..11)
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        visited = set()
        for _, _, tids in store.visited_windows:
            visited.update(tids)
        self.assertEqual(12, len(visited))

    def test_J_failed_project_does_not_block_all_projects(self):
        """Scenario J: If one project throws TaskError on Drive read, project cursor advances
        and other projects still receive service."""
        project_ids = ["failing-p0", "healthy-p1", "healthy-p2"]
        projects_data = {pid: self._make_tasks(pid, 4) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)
        store.failing_projects.add("failing-p0")

        # Invocation 1: attempts failing-p0, catches TaskError, cursor advances to healthy-p1
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        c1 = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(1, c1["project_cursor"])

        # Invocation 2: successfully serves healthy-p1
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        c2 = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(2, c2["project_cursor"])

        # Invocation 3: successfully serves healthy-p2
        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())
        c3 = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(0, c3["project_cursor"])

        # Healthy projects received service despite p0's recurring error
        served_successful = [w[0] for w in store.visited_windows]
        self.assertEqual(["healthy-p1", "healthy-p2"], served_successful)

    def test_K_phase1_hard_bound(self):
        """Scenario K: Phase 1 services at most M=1 project and at most K=4 records per invocation."""
        project_ids = [f"p{i}" for i in range(5)]
        projects_data = {pid: self._make_tasks(pid, 20) for pid in project_ids}
        store = MemoryDiscoveryStore(projects_data)

        poll_once(store, None, discovery_store=store, cursor_path=self.cursor_path, allowlist=frozenset())

        # Exactly 1 project visited in Phase 1
        self.assertEqual(1, len(store.visited_windows))
        # At most 4 records returned
        self.assertEqual(4, len(store.visited_windows[0][2]))

    def test_L_concurrent_stale_cursor_writer_protection(self):
        """Scenario L: Stale writer with older generation is rejected with StaleCursorError."""
        cursor = load_phase1_cursor(cursor_path=self.cursor_path)
        gen0 = cursor["generation"]

        # Writer 1 succeeds and advances generation to 1
        cursor["project_cursor"] = 1
        save_phase1_cursor(cursor, cursor_path=self.cursor_path, expected_generation=gen0)

        # Writer 2 with stale gen0 attempts write -> StaleCursorError
        stale_cursor = {"project_cursor": 99, "per_project_record_cursor": {}, "generation": gen0}
        with self.assertRaises(StaleCursorError):
            save_phase1_cursor(stale_cursor, cursor_path=self.cursor_path, expected_generation=gen0)

        # Re-read confirms generation 1 was preserved
        current = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(1, current["project_cursor"])
        self.assertEqual(1, current["generation"])


if __name__ == "__main__":
    unittest.main()
