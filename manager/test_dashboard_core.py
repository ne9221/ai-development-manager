import unittest
from datetime import datetime, timezone
from manager.dashboard_core import (
    parse_time,
    is_cleanup_confirmed,
    determine_execution_state,
    is_execution_stale,
    get_global_summary,
    map_task_board
)

class TestDashboardCore(unittest.TestCase):
    def test_parse_time(self):
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time("invalid-date"))
        
        t = parse_time("2026-08-15T02:34:26Z")
        self.assertIsNotNone(t)
        self.assertEqual(t.tzinfo, timezone.utc)
        self.assertEqual(t.year, 2026)
        
        # Test with offset representation
        t2 = parse_time("2026-08-15T02:34:26+00:00")
        self.assertEqual(t, t2)

    def test_is_cleanup_confirmed(self):
        # Read-only execution with non-required writer
        ro_exec = {
            "access": "read_only",
            "cleanup_evidence": {
                "task_claim_release": "released",
                "writer_release": "not_required"
            }
        }
        self.assertTrue(is_cleanup_confirmed(ro_exec))
        
        # Read-only execution with missing cleanup evidence
        self.assertFalse(is_cleanup_confirmed({"access": "read_only"}))
        
        # Production write execution with released writer lease
        write_exec = {
            "access": "production_write",
            "cleanup_evidence": {
                "task_claim_release": "released",
                "writer_release": "released"
            }
        }
        self.assertTrue(is_cleanup_confirmed(write_exec))

    def test_determine_execution_state(self):
        now = datetime(2026, 8, 15, 3, 0, 0, tzinfo=timezone.utc)
        
        # Terminal state with confirmed cleanup
        term_exec = {
            "status": "completed",
            "access": "read_only",
            "cleanup_evidence": {
                "task_claim_release": "released",
                "writer_release": "not_required"
            }
        }
        self.assertEqual(determine_execution_state(term_exec, now), "completed")
        
        # Terminal state but cleanup not confirmed
        term_exec_dirty = {
            "status": "completed",
            "access": "read_only"
        }
        self.assertEqual(determine_execution_state(term_exec_dirty, now), "finishing")
        
        # Running but no session linked (correlating)
        running_no_session = {
            "status": "running"
        }
        self.assertEqual(determine_execution_state(running_no_session, now), "correlating")
        
        # Running and linked, recent heartbeat
        running_linked = {
            "status": "running",
            "provider_session_id": "sess-1",
            "heartbeat_at": "2026-08-15T02:50:00Z" # 10 mins ago
        }
        self.assertEqual(determine_execution_state(running_linked, now), "running")
        
        # Running and linked, old heartbeat (waiting)
        running_waiting = {
            "status": "running",
            "provider_session_id": "sess-1",
            "heartbeat_at": "2026-08-15T02:30:00Z" # 30 mins ago
        }
        self.assertEqual(determine_execution_state(running_waiting, now), "waiting")

    def test_is_execution_stale(self):
        now = datetime(2026, 8, 15, 3, 0, 0, tzinfo=timezone.utc)
        
        # Terminal execution is never stale
        term = {"status": "completed"}
        self.assertFalse(is_execution_stale(term, now))
        
        # Exceeded hard timeout
        exec_timeout = {
            "status": "running",
            "hard_timeout_at": "2026-08-15T02:59:00Z"
        }
        self.assertTrue(is_execution_stale(exec_timeout, now))
        
        # Heartbeat >= 15 mins (900 seconds)
        exec_old_hb = {
            "status": "running",
            "heartbeat_at": "2026-08-15T02:44:00Z" # 16 mins ago
        }
        self.assertTrue(is_execution_stale(exec_old_hb, now))
        
        # Heartbeat < 15 mins
        exec_fresh = {
            "status": "running",
            "heartbeat_at": "2026-08-15T02:55:00Z" # 5 mins ago
        }
        self.assertFalse(is_execution_stale(exec_fresh, now))

    def test_get_global_summary(self):
        providers = [
            {"provider": "codex", "has_reliable_quota": True},
            {"provider": "claude", "has_reliable_quota": False}
        ]
        tasks = [
            {"status": "in_progress"},
            {"status": "blocked"},
            {"status": "ready"}
        ]
        executions = [
            {"status": "running"}
        ]
        
        summary = get_global_summary(providers, tasks, executions)
        self.assertEqual(summary["running_tasks_count"], 1)
        self.assertEqual(summary["blocked_tasks_count"], 1)
        self.assertEqual(summary["active_sessions_count"], 1)
        self.assertEqual(summary["reliable_providers_count"], 1)

    def test_map_task_board(self):
        now = datetime(2026, 8, 15, 3, 0, 0, tzinfo=timezone.utc)
        tasks = [
            {"task_id": "t1", "project_id": "p1", "status": "ready"},
            {"task_id": "t2", "project_id": "p1", "status": "in_progress"},
            {"task_id": "t3", "project_id": "p1", "status": "blocked"},
            {"task_id": "t4", "project_id": "p1", "status": "completed"}
        ]
        
        board = map_task_board(tasks, {}, now)
        self.assertEqual(len(board["Ready"]), 1)
        self.assertEqual(len(board["In progress"]), 1)
        self.assertEqual(len(board["Blocked / Attention"]), 1)
        self.assertEqual(len(board["Completed"]), 1)
        self.assertEqual(board["Ready"][0]["task_id"], "t1")

if __name__ == "__main__":
    unittest.main()
