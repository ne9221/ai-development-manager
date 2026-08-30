import unittest
import time
from unittest.mock import patch

from manager.command_watcher import (
    POLL_SECONDS,
    _enumerate_waiting_quota_tasks,
    _within_project_record_rotation_offset,
)
from manager.tasks import DriveRecords
from manager.test_tasks import FakeDriveService


def _seed_tasks(store, project_id, count, waiting_indices=None):
    """Seed tasks in a fake drive store. If waiting_indices is given,
    only tasks at those indices have the waiting_quota signature;
    otherwise all tasks are waiting_quota."""
    if waiting_indices is None:
        waiting_indices = set(range(count))
    else:
        waiting_indices = set(waiting_indices)

    for i in range(count):
        is_waiting = i in waiting_indices
        task = {
            "task_id": f"task-{i:03d}",
            "project_id": project_id,
            "title": f"Task {i}",
            "status": "submitted",
            "needs_repo_edit": True,
            "expected_minutes": 20,
            "recommended_provider": None if is_waiting else "codex",
            "quota_evidence": {"decision": "waiting_quota"} if is_waiting else None,
            "source_context": {"origin": "direct_dispatch"},
        }
        store.put("tasks", project_id, f"task-{i:03d}", task)


class WaitingQuotaAccelerationTests(unittest.TestCase):
    """Test suite covering Bounded Convergence Acceleration for waiting_quota tasks."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)

    def test_case_a_tail_task_reachability_181_records(self):
        """Case A: N=181, tail waiting_quota task at index 180 must be reached
        within ceil(181/K) ticks (46 ticks for K=4), not 181 ticks."""
        _seed_tasks(self.store, "p1", 181, waiting_indices={180})
        
        from manager.command_watcher import WAITING_QUOTA_DISCOVERY_WINDOW
        k = WAITING_QUOTA_DISCOVERY_WINDOW
        expected_max_ticks = (181 + k - 1) // k

        reached_tick = None
        for tick in range(expected_max_ticks):
            now = tick * POLL_SECONDS
            with patch("manager.command_watcher.time.time", return_value=now):
                tasks = _enumerate_waiting_quota_tasks(self.store, "p1")
                if any(t["task_id"] == "task-180" for t in tasks):
                    reached_tick = tick
                    break

        self.assertIsNotNone(reached_tick, f"Task-180 was not reached within {expected_max_ticks} ticks")
        self.assertLessEqual(reached_tick, expected_max_ticks)

    def test_case_b_arbitrary_n_finite_full_coverage(self):
        """Case B: N=1,2,3,4,5,10,12,17,181,200 must all achieve finite full
        coverage in ceil(N/K) ticks -- includes N below, at, and above the
        bounded window K=4 (N=1..5 straddle K itself, exercising the N<K,
        N==K, and N==K+1 boundary cases explicitly)."""
        from manager.command_watcher import WAITING_QUOTA_DISCOVERY_WINDOW
        k = WAITING_QUOTA_DISCOVERY_WINDOW

        for n in [1, 2, 3, 4, 5, 10, 12, 17, 181, 200]:
            service = FakeDriveService()
            store = DriveRecords(service)
            _seed_tasks(store, f"p-{n}", n)

            expected_ticks = (n + k - 1) // k
            seen = set()
            for tick in range(expected_ticks):
                now = tick * POLL_SECONDS
                with patch("manager.command_watcher.time.time", return_value=now):
                    tasks = _enumerate_waiting_quota_tasks(store, f"p-{n}")
                    seen.update(t["task_id"] for t in tasks)

            all_task_ids = {f"task-{i:03d}" for i in range(n)}
            self.assertEqual(all_task_ids, seen, f"Failed full coverage for N={n} within {expected_ticks} ticks")

    def test_case_c_common_factor_no_starvation(self):
        """Case C: When N and stride share common factors (e.g. N=12, K=4; N=10, K=4; N=200, K=4),
        no elements are starved and all elements are covered in ceil(N/K) ticks."""
        from manager.command_watcher import WAITING_QUOTA_DISCOVERY_WINDOW
        k = WAITING_QUOTA_DISCOVERY_WINDOW

        for n in [10, 12, 200]:
            service = FakeDriveService()
            store = DriveRecords(service)
            _seed_tasks(store, f"p-gcd-{n}", n)

            expected_ticks = (n + k - 1) // k
            seen = set()
            for tick in range(expected_ticks):
                now = tick * POLL_SECONDS
                with patch("manager.command_watcher.time.time", return_value=now):
                    tasks = _enumerate_waiting_quota_tasks(store, f"p-gcd-{n}")
                    seen.update(t["task_id"] for t in tasks)

            all_task_ids = {f"task-{i:03d}" for i in range(n)}
            self.assertEqual(all_task_ids, seen, f"Starvation detected for N={n} with common factor gcd({n}, {k})")

    def test_case_d_single_tick_drive_read_hard_bound_not_increased(self):
        """Case D: Single tick Drive read/hydration is strictly bounded by K."""
        from manager.command_watcher import WAITING_QUOTA_DISCOVERY_WINDOW
        k = WAITING_QUOTA_DISCOVERY_WINDOW
        _seed_tasks(self.store, "p1", 100)

        get_media_count = 0
        orig_get_media = self.service.files().get_media

        def count_get_media(*args, **kwargs):
            nonlocal get_media_count
            get_media_count += 1
            return orig_get_media(*args, **kwargs)

        with patch.object(self.service.files(), "get_media", side_effect=count_get_media):
            tasks = _enumerate_waiting_quota_tasks(self.store, "p1")

        self.assertLessEqual(len(tasks), k)
        self.assertLessEqual(get_media_count, k)

    def test_case_e_multi_project_independent_rotation(self):
        """Case E: Multiple projects rotate independently without cross-project starvation."""
        from manager.command_watcher import WAITING_QUOTA_DISCOVERY_WINDOW
        k = WAITING_QUOTA_DISCOVERY_WINDOW
        _seed_tasks(self.store, "p1", 20)
        _seed_tasks(self.store, "p2", 20)

        seen_p1 = set()
        seen_p2 = set()
        for tick in range(5):
            now = tick * POLL_SECONDS
            with patch("manager.command_watcher.time.time", return_value=now):
                tasks_p1 = _enumerate_waiting_quota_tasks(self.store, "p1")
                tasks_p2 = _enumerate_waiting_quota_tasks(self.store, "p2")
                seen_p1.update(t["task_id"] for t in tasks_p1)
                seen_p2.update(t["task_id"] for t in tasks_p2)

        self.assertEqual(20, len(seen_p1))
        self.assertEqual(20, len(seen_p2))

    def test_case_f_dynamic_new_records_preserves_reachability(self):
        """Case F: When new records are added over time, existing waiting_quota tasks
        remain finite reachable."""
        from manager.command_watcher import WAITING_QUOTA_DISCOVERY_WINDOW
        k = WAITING_QUOTA_DISCOVERY_WINDOW
        _seed_tasks(self.store, "p1", 20)

        # Add 5 more tasks mid-way
        seen = set()
        for tick in range(10):
            now = tick * POLL_SECONDS
            if tick == 2:
                for j in range(20, 25):
                    self.store.put("tasks", "p1", f"task-{j:03d}", {
                        "task_id": f"task-{j:03d}", "project_id": "p1", "title": f"Task {j}",
                        "status": "submitted", "recommended_provider": None,
                        "quota_evidence": {"decision": "waiting_quota"},
                        "source_context": {"origin": "direct_dispatch"},
                    })
            with patch("manager.command_watcher.time.time", return_value=now):
                tasks = _enumerate_waiting_quota_tasks(self.store, "p1")
                seen.update(t["task_id"] for t in tasks)

        # All original 20 tasks plus newly added 5 tasks should be reached
        for i in range(25):
            self.assertIn(f"task-{i:03d}", seen)

    def test_case_g_n_smaller_than_bounded_window_no_duplicate_io(self):
        """Case G: When N < K (e.g. N=2, K=4), exactly N records are read with no duplicates."""
        _seed_tasks(self.store, "p1", 2)
        get_media_count = 0
        orig_get_media = self.service.files().get_media

        def count_get_media(*args, **kwargs):
            nonlocal get_media_count
            get_media_count += 1
            return orig_get_media(*args, **kwargs)

        with patch.object(self.service.files(), "get_media", side_effect=count_get_media):
            tasks = _enumerate_waiting_quota_tasks(self.store, "p1")

        self.assertEqual(2, len(tasks))
        self.assertEqual(2, get_media_count)

    def test_case_i_arbitrary_starting_tick_still_achieves_full_coverage(self):
        """Case I: the ceil(N/K) coverage guarantee must hold starting from
        ANY arbitrary natural tick (e.g. a watcher process that has been
        running for days), not just from tick=0 -- the offset is purely a
        function of wall-clock time modulo the record count, so any window
        of M consecutive ticks is contiguous on the unwrapped circle
        regardless of where it starts, including wrapping past the end of
        the list back to the beginning."""
        from manager.command_watcher import WAITING_QUOTA_DISCOVERY_WINDOW
        k = WAITING_QUOTA_DISCOVERY_WINDOW
        n = 17
        _seed_tasks(self.store, "p1", n)

        # Start far from tick=0 (an arbitrary large starting tick), including
        # a starting offset that is itself not a multiple of K -- this
        # exercises wrap-around (the window crossing back past index 0)
        # within the covering set of ticks.
        arbitrary_start_tick = 733
        expected_ticks = (n + k - 1) // k
        seen = set()
        for tick in range(arbitrary_start_tick, arbitrary_start_tick + expected_ticks):
            now = tick * POLL_SECONDS
            with patch("manager.command_watcher.time.time", return_value=now):
                tasks = _enumerate_waiting_quota_tasks(self.store, "p1")
                seen.update(t["task_id"] for t in tasks)

        all_task_ids = {f"task-{i:03d}" for i in range(n)}
        self.assertEqual(all_task_ids, seen, f"Arbitrary-start coverage failed for N={n} starting at tick {arbitrary_start_tick}")

    def test_case_h_deadline_exhausted_early_bails_gracefully(self):
        """Case H: When deadline is exhausted early, enumeration bails gracefully."""
        _seed_tasks(self.store, "p1", 50)
        
        # deadline already in the past
        tasks = _enumerate_waiting_quota_tasks(self.store, "p1", deadline=time.monotonic() - 10)
        self.assertEqual([], tasks)


if __name__ == "__main__":
    unittest.main()
