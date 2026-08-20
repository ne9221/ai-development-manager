import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from streamlit.testing.v1 import AppTest

from manager.runtime_visibility import (
    STATE_AUTO_RUNNING,
    STATE_AUTO_STALLED,
    STATE_BLOCKED,
    STATE_IDLE,
    STATE_UNKNOWN,
    STATE_WAITING_USER,
    compute_global_runtime_state,
    compute_next_auto_action,
    determine_ai_runtime_activity,
    format_activity_timestamp_and_age,
    format_elapsed_duration,
)
from manager.actions import ActionItem, STATUS_OPEN, TYPE_REVIEW_REQUIRED, SEVERITY_HIGH


class TestDashboardP1BRuntimeVisibilityAndActionCenter(unittest.TestCase):
    def test_elapsed_duration_formatting(self):
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
        # Known start: 14m 27s
        start1 = (now - timedelta(minutes=14, seconds=27)).isoformat()
        self.assertEqual(format_elapsed_duration(start1, now), "14m 27s")

        # Known start: 1h 05m
        start2 = (now - timedelta(hours=1, minutes=5, seconds=10)).isoformat()
        self.assertEqual(format_elapsed_duration(start2, now), "1h 05m")

        # Unknown / None start -> Must be 'Unknown', NOT '0m' or '0'
        self.assertEqual(format_elapsed_duration(None, now), "Unknown")
        self.assertEqual(format_elapsed_duration("", now), "Unknown")
        self.assertEqual(format_elapsed_duration("invalid-timestamp", now), "Unknown")

    def test_activity_timestamp_and_age_formatting(self):
        now = datetime(2026, 8, 21, 12, 31, 8, tzinfo=timezone.utc)
        # 1m ago
        act1 = (now - timedelta(minutes=1)).isoformat()
        res1 = format_activity_timestamp_and_age(act1, now)
        self.assertIn("12:30:08", res1)
        self.assertIn("1m ago", res1)

        # 30s ago
        act2 = (now - timedelta(seconds=30)).isoformat()
        res2 = format_activity_timestamp_and_age(act2, now)
        self.assertIn("30s ago", res2)

        # Unknown
        self.assertEqual(format_activity_timestamp_and_age(None, now), "Unknown")

    def test_ai_runtime_activity_stalled_vs_healthy_long_run(self):
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)

        # Case A: Long running (elapsed 45m) with RECENT activity (1m ago) -> Healthy RUNNING
        healthy_long_exe = {
            "status": "running",
            "started_at": (now - timedelta(minutes=45)).isoformat(),
            "heartbeat_at": (now - timedelta(minutes=1)).isoformat(),
            "last_provider_event_at": (now - timedelta(minutes=1)).isoformat(),
        }
        state, badge, _ = determine_ai_runtime_activity(healthy_long_exe, now)
        self.assertEqual(state, "RUNNING")
        self.assertEqual(badge, "badge-ok")

        # Case B: Execution with OLD activity (15m ago) -> POSSIBLY STALLED
        stalled_exe = {
            "status": "running",
            "started_at": (now - timedelta(minutes=45)).isoformat(),
            "heartbeat_at": (now - timedelta(minutes=15)).isoformat(),
        }
        state2, badge2, _ = determine_ai_runtime_activity(stalled_exe, now)
        self.assertEqual(state2, "POSSIBLY STALLED")
        self.assertEqual(badge2, "badge-err")

    def test_global_runtime_state_derivation(self):
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)

        # 1. No active execs, no blockers -> IDLE
        s1, b1, _ = compute_global_runtime_state([], [], [], now=now)
        self.assertEqual(s1, STATE_IDLE)

        # 2. Active running execution -> AUTO RUNNING
        active_exec = [{"status": "running", "heartbeat_at": (now - timedelta(minutes=1)).isoformat()}]
        s2, b2, _ = compute_global_runtime_state(active_exec, [], [], now=now)
        self.assertEqual(s2, STATE_AUTO_RUNNING)

        # 3. All active executions stalled -> AUTO STALLED
        stalled_exec = [{"status": "running", "heartbeat_at": (now - timedelta(minutes=25)).isoformat()}]
        s3, b3, _ = compute_global_runtime_state(stalled_exec, [], [], now=now)
        self.assertEqual(s3, STATE_AUTO_STALLED)

        # 4. Open user action item pending -> WAITING USER
        user_act = ActionItem(action_id="ACT-1", title="Manual Approval", status=STATUS_OPEN, need_user_action=True)
        s4, b4, _ = compute_global_runtime_state([], [], [user_act], now=now)
        self.assertEqual(s4, STATE_WAITING_USER)

    def test_next_auto_action_derivation(self):
        # When execution active
        execs = [{"project_id": "ADM", "task_id": "P1-B", "provider": "Claude B"}]
        act_str = compute_next_auto_action([], execs, [])
        self.assertIn("Awaiting completion of ADM/P1-B on Claude B", act_str)

        # When ready tasks exist
        tasks = [{"project_id": "ADM", "task_id": "T-READY", "status": "ready", "recommended_provider": "codex"}]
        act_str2 = compute_next_auto_action(tasks, [], [])
        self.assertIn("Dispatch task T-READY", act_str2)

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_dashboard_action_center_navigation_and_rendering(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)
        self.assertFalse(at.exception, f"App crashed on Overview: {at.exception}")

        # Verify Action Center is present in sidebar navigation
        self.assertEqual(len(at.sidebar.radio), 1)
        self.assertIn("Action Center", at.sidebar.radio[0].options)

        # Switch to Action Center
        at.sidebar.radio[0].set_value("Action Center")
        at.run(timeout=30)
        self.assertFalse(at.exception, f"App crashed on Action Center page: {at.exception}")

        title_texts = [el.value for el in at.title]
        self.assertTrue(any("Action Center" in t for t in title_texts))

        # Check expanders for Needs Attention and History
        expander_labels = [el.label for el in at.expander]
        self.assertTrue(any("Needs Attention" in l or "待处理" in l for l in expander_labels))
        self.assertTrue(any("Action History" in l or "历史归档" in l for l in expander_labels))


if __name__ == "__main__":
    unittest.main()
