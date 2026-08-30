import time
import unittest
from unittest.mock import patch

from manager.command_watcher import (
    POLL_SECONDS,
    WAITING_QUOTA_DISCOVERY_WINDOW,
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


class WaitingQuotaAccelerationComprehensiveTests(unittest.TestCase):
    """Rigorous, deterministic test suite covering Bounded Convergence Acceleration
    for waiting_quota tasks across arbitrary N, arbitrary starting ticks, GCDs,
    and dynamic mutations."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        self.k = WAITING_QUOTA_DISCOVERY_WINDOW

    def test_arbitrary_n_full_coverage_within_ceil_n_over_k(self):
        """Proof: For every N in [1, 2, 3, 4, 5, 10, 12, 17, 181, 200],
        starting from tick 0, all N tasks are visited in ceil(N/K) consecutive ticks."""
        test_sizes = [1, 2, 3, 4, 5, 10, 12, 17, 181, 200]
        for n in test_sizes:
            service = FakeDriveService()
            store = DriveRecords(service)
            _seed_tasks(store, f"p-{n}", n)

            expected_ticks = (n + self.k - 1) // self.k
            seen = set()
            for tick in range(expected_ticks):
                now = tick * POLL_SECONDS
                with patch("manager.command_watcher.time.time", return_value=now):
                    tasks = _enumerate_waiting_quota_tasks(store, f"p-{n}")
                    seen.update(t["task_id"] for t in tasks)

            all_task_ids = {f"task-{i:03d}" for i in range(n)}
            self.assertEqual(all_task_ids, seen, f"Failed full coverage for N={n} within {expected_ticks} ticks")

    def test_arbitrary_starting_ticks_full_coverage(self):
        """Proof: Starting from any arbitrary tick t_start (e.g. 17, 100, 12345, 99999),
        any N items are fully covered in any consecutive ceil(N/K) ticks."""
        n = 181
        expected_ticks = (n + self.k - 1) // self.k
        _seed_tasks(self.store, "p-start", n)
        all_task_ids = {f"task-{i:03d}" for i in range(n)}

        for t_start in [0, 7, 17, 100, 12345, 99999]:
            seen = set()
            for step in range(expected_ticks):
                now = (t_start + step) * POLL_SECONDS
                with patch("manager.command_watcher.time.time", return_value=now):
                    tasks = _enumerate_waiting_quota_tasks(self.store, "p-start")
                    seen.update(t["task_id"] for t in tasks)
            self.assertEqual(all_task_ids, seen, f"Failed full coverage for t_start={t_start}")

    def test_gcd_common_factors_no_starvation(self):
        """Proof: When gcd(N, K) > 1 (e.g. N=12, K=4 gcd=4; N=10, K=4 gcd=2; N=6, K=4 gcd=2; N=200, K=4 gcd=4),
        no elements are starved and all elements are visited within ceil(N/K) ticks."""
        for n in [4, 6, 8, 10, 12, 16, 20, 100, 200]:
            service = FakeDriveService()
            store = DriveRecords(service)
            _seed_tasks(store, f"p-gcd-{n}", n)

            expected_ticks = (n + self.k - 1) // self.k
            seen = set()
            for tick in range(expected_ticks):
                now = tick * POLL_SECONDS
                with patch("manager.command_watcher.time.time", return_value=now):
                    tasks = _enumerate_waiting_quota_tasks(store, f"p-gcd-{n}")
                    seen.update(t["task_id"] for t in tasks)

            all_task_ids = {f"task-{i:03d}" for i in range(n)}
            self.assertEqual(all_task_ids, seen, f"Starvation detected for gcd({n}, {self.k}) > 1")

    def test_tail_task_reachability_181_records(self):
        """Proof: In N=181 backlog with tail task at index 180, the tail task
        is reached in at most ceil(181/4) = 46 ticks, rather than 181 ticks."""
        _seed_tasks(self.store, "p181", 181, waiting_indices={180})
        expected_max_ticks = (181 + self.k - 1) // self.k

        reached_tick = None
        for tick in range(expected_max_ticks):
            now = tick * POLL_SECONDS
            with patch("manager.command_watcher.time.time", return_value=now):
                tasks = _enumerate_waiting_quota_tasks(self.store, "p181")
                if any(t["task_id"] == "task-180" for t in tasks):
                    reached_tick = tick
                    break

        self.assertIsNotNone(reached_tick, f"Task-180 was not reached within {expected_max_ticks} ticks")
        self.assertLessEqual(reached_tick, expected_max_ticks)

    def test_single_tick_drive_read_hard_bound(self):
        """Proof: Single tick Drive get_media calls are strictly capped at min(N, K)."""
        _seed_tasks(self.store, "p-bound", 100)

        get_media_count = 0
        orig_get_media = self.service.files().get_media

        def count_get_media(*args, **kwargs):
            nonlocal get_media_count
            get_media_count += 1
            return orig_get_media(*args, **kwargs)

        with patch.object(self.service.files(), "get_media", side_effect=count_get_media):
            tasks = _enumerate_waiting_quota_tasks(self.store, "p-bound")

        self.assertLessEqual(len(tasks), self.k)
        self.assertLessEqual(get_media_count, self.k)

    def test_n_smaller_than_k_no_duplicate_io(self):
        """Proof: When N < K (e.g. N=1, 2, 3), exactly N records are read with zero duplicates."""
        for n in [1, 2, 3]:
            service = FakeDriveService()
            store = DriveRecords(service)
            _seed_tasks(store, f"p-small-{n}", n)

            get_media_count = 0
            orig_get_media = service.files().get_media

            def count_get_media(*args, **kwargs):
                nonlocal get_media_count
                get_media_count += 1
                return orig_get_media(*args, **kwargs)

            with patch.object(service.files(), "get_media", side_effect=count_get_media):
                tasks = _enumerate_waiting_quota_tasks(store, f"p-small-{n}")

            self.assertEqual(n, len(tasks))
            self.assertEqual(n, get_media_count)

    def test_multi_project_independent_rotation(self):
        """Proof: Multiple projects rotate independently without cross-project interference."""
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

    def test_dynamic_insertions_preserves_reachability(self):
        """Proof: Dynamically adding records mid-rotation does not starve any task."""
        _seed_tasks(self.store, "p-dyn-add", 20)

        seen = set()
        for tick in range(10):
            now = tick * POLL_SECONDS
            if tick == 2:
                for j in range(20, 25):
                    self.store.put("tasks", "p-dyn-add", f"task-{j:03d}", {
                        "task_id": f"task-{j:03d}", "project_id": "p-dyn-add", "title": f"Task {j}",
                        "status": "submitted", "recommended_provider": None,
                        "quota_evidence": {"decision": "waiting_quota"},
                        "source_context": {"origin": "direct_dispatch"},
                    })
            with patch("manager.command_watcher.time.time", return_value=now):
                tasks = _enumerate_waiting_quota_tasks(self.store, "p-dyn-add")
                seen.update(t["task_id"] for t in tasks)

        for i in range(25):
            self.assertIn(f"task-{i:03d}", seen)

    def test_dynamic_deletions_or_completions_preserves_reachability(self):
        """Proof: Completing or removing tasks mid-rotation does not corrupt enumeration."""
        _seed_tasks(self.store, "p-dyn-del", 20)

        seen_after_completion = set()
        for tick in range(10):
            now = tick * POLL_SECONDS
            if tick == 2:
                # Mark task-000 through task-003 as completed
                for j in range(4):
                    self.store.put("tasks", "p-dyn-del", f"task-{j:03d}", {
                        "task_id": f"task-{j:03d}", "project_id": "p-dyn-del", "title": f"Task {j}",
                        "status": "completed", "recommended_provider": "codex",
                    })
            with patch("manager.command_watcher.time.time", return_value=now):
                tasks = _enumerate_waiting_quota_tasks(self.store, "p-dyn-del")
                if tick >= 2:
                    seen_after_completion.update(t["task_id"] for t in tasks)

        # All remaining tasks (4..19) must be discovered after tick 2
        for i in range(4, 20):
            self.assertIn(f"task-{i:03d}", seen_after_completion)
        # Completed tasks must not be seen after completion
        for i in range(4):
            self.assertNotIn(f"task-{i:03d}", seen_after_completion)

    def test_deadline_truncation_bails_gracefully(self):
        """Proof: When deadline is exhausted, enumeration bails cleanly without errors."""
        _seed_tasks(self.store, "p-deadline", 50)
        tasks = _enumerate_waiting_quota_tasks(self.store, "p-deadline", deadline=time.monotonic() - 10)
        self.assertEqual([], tasks)


if __name__ == "__main__":
    unittest.main()
