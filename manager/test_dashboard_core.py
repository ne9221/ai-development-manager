import unittest
from datetime import datetime, timezone
from manager.dashboard_core import (
    parse_time,
    is_cleanup_confirmed,
    determine_execution_state,
    is_execution_stale,
    get_global_summary,
    map_task_board,
    format_unknown_field,
    format_status_bar_status,
    format_quota_remaining,
    format_quota_freshness,
    format_blocker,
    format_needs_user_action,
    format_github_state,
    format_drive_reachability,
    format_last_trustworthy_evidence,
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

        # Claude: Running and linked, old heartbeat (remains running)
        claude_running = {
            "status": "running",
            "provider": "claude",
            "provider_session_id": "sess-2",
            "heartbeat_at": "2026-08-15T02:30:00Z" # 30 mins ago
        }
        self.assertEqual(determine_execution_state(claude_running, now), "running")

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

        # Claude: Heartbeat >= 15 mins (not stale)
        claude_exec_old = {
            "status": "running",
            "provider": "Claude",
            "heartbeat_at": "2026-08-15T02:30:00Z" # 30 mins ago
        }
        self.assertFalse(is_execution_stale(claude_exec_old, now))
        
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

class TestStatusBarFormatters(unittest.TestCase):
    """UI Truth Contract: these formatters are the only place status_bar's
    already-correct UNKNOWN/None outputs could still be turned into a
    guess. Each test pins the exact required rendering."""

    def test_unknown_field_never_guesses(self):
        self.assertEqual(format_unknown_field("UNKNOWN"), "Unknown")
        self.assertEqual(format_unknown_field(None), "Unknown")
        self.assertEqual(format_unknown_field("codex"), "codex")

    def test_account_alias_unknown_is_not_a_or_b(self):
        # account_alias is always "UNKNOWN" per status_bar._account_alias;
        # this must never render as a guessed "Claude A" / "Claude B".
        rendered = format_unknown_field("UNKNOWN")
        self.assertEqual(rendered, "Unknown")
        self.assertNotIn("A", rendered)
        self.assertNotIn("B", rendered)

    def test_status_stale_or_no_evidence_is_never_running(self):
        self.assertEqual(format_status_bar_status("UNKNOWN"), "Unknown")
        self.assertNotEqual(format_status_bar_status("UNKNOWN"), "Running")
        self.assertEqual(format_status_bar_status("running"), "Running")

    def test_status_finishing_for_terminal_retained_cleanup(self):
        self.assertEqual(format_status_bar_status("finishing"), "Finishing")

    def test_quota_remaining_none_is_dash_never_zero_percent(self):
        self.assertEqual(format_quota_remaining(None), "—")
        self.assertEqual(format_quota_remaining(0), "0%")
        self.assertEqual(format_quota_remaining(55), "55%")

    def test_quota_freshness_unknown(self):
        self.assertEqual(format_quota_freshness(None), "Unknown")
        self.assertEqual(format_quota_freshness("unknown"), "Unknown")
        self.assertEqual(format_quota_freshness("fresh"), "Fresh")
        self.assertEqual(format_quota_freshness("stale"), "Stale")

    def test_blocker_dash_when_none(self):
        self.assertEqual(format_blocker(None), "—")
        self.assertEqual(format_blocker("waiting on human review"), "waiting on human review")

    def test_needs_user_action_none_is_dash_never_no(self):
        rendered = format_needs_user_action(None)
        self.assertEqual(rendered, "—")
        self.assertNotEqual(rendered, "No")
        self.assertEqual(format_needs_user_action(True), "Yes")
        self.assertEqual(format_needs_user_action(False), "No")

    def test_drive_reachable_renders_reachable_never_synced(self):
        rendered = format_drive_reachability({"state": "reachable"})
        self.assertEqual(rendered, "Reachable")
        self.assertNotEqual(rendered, "Synced")
        self.assertEqual(format_drive_reachability({"state": "unreachable"}), "Unreachable")
        self.assertEqual(format_drive_reachability(None), "Unknown")
        self.assertEqual(format_drive_reachability({}), "Unknown")

    def test_github_state_renders_raw_state_title_cased(self):
        self.assertEqual(format_github_state({"state": "synced"}), "Synced")
        self.assertEqual(format_github_state({"state": "ahead"}), "Ahead")
        self.assertEqual(format_github_state(None), "Unknown")

    def test_last_trustworthy_evidence_dash_when_absent(self):
        self.assertEqual(format_last_trustworthy_evidence({"source": None, "at": None}), "—")
        self.assertEqual(format_last_trustworthy_evidence(None), "—")
        evidence = {"source": "execution_heartbeat_at", "at": "2026-08-17T11:50:00.000000Z"}
        self.assertEqual(format_last_trustworthy_evidence(evidence), "2026-08-17T11:50:00.000000Z")


if __name__ == "__main__":
    unittest.main()
