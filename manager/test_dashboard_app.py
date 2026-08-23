import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import streamlit as st
from streamlit.testing.v1 import AppTest

from manager.tasks import TaskError


class TestDashboardAppRender(unittest.TestCase):
    def setUp(self):
        # Clear Streamlit cache to prevent test-to-test contamination
        st.cache_data.clear()

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_empty_drive(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        # Setup empty drive state
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed on empty drive: {at.exception}")

        # Verify UI Semantic: Since the drive is empty, all expected providers default to stale
        warning_messages = [el.value for el in at.warning]
        self.assertTrue(
            any("STALE" in msg or "Stale" in msg for msg in warning_messages),
            f"Expected stale warnings, got: {warning_messages}"
        )

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_remaining_percent_none(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        # Setup quota with remaining_percent=None and fresh last_updated
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        mock_read_drive_status.return_value = {
            "providers": [
                {
                    "provider": "codex",
                    "display_name": "Codex",
                    "status": "ok",
                    "collection_mode": "automatic",
                    "source": "codex_app_server",
                    "source_type": "official",
                    "confidence": "official",
                    "last_updated": now_str,
                    "windows": [
                        {"name": "primary", "remaining_percent": None, "used_percent": None}
                    ]
                }
            ]
        }
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed on remaining_percent=None: {at.exception}")

        # Verify UI Semantic: display "Percentage not reported"
        info_messages = [el.value for el in at.info]
        self.assertTrue(
            any("Percentage not reported" in msg for msg in info_messages),
            f"Expected 'Percentage not reported' in info messages: {info_messages}"
        )

        # Verify progress bar is not drawn for None
        self.assertEqual(len(at.get("progress")), 0, "Progress bar should not be drawn for None values")

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_zero_percent_quota(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        # Setup quota with remaining_percent=0.0
        now = datetime.now(timezone.utc)
        mock_read_drive_status.return_value = {
            "providers": [
                {
                    "provider": "codex",
                    "display_name": "Codex",
                    "status": "ok",
                    "collection_mode": "automatic",
                    "source": "codex_app_server",
                    "source_type": "official",
                    "confidence": "official",
                    "last_updated": now.isoformat().replace("+00:00", "Z"),
                    "windows": [
                        {"name": "primary", "remaining_percent": 0.0, "used_percent": 100.0, "resets_at": (now + timedelta(hours=2)).isoformat()}
                    ]
                }
            ]
        }
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed on remaining_percent=0: {at.exception}")
        # Progress bar should be drawn for 0% (value=0.0)
        self.assertEqual(len(at.get("progress")), 1)
        self.assertEqual(at.get("progress")[0].value, 0.0)

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_claude_ab_multi_account(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        now = datetime.now(timezone.utc)
        mock_read_drive_status.return_value = {
            "providers": [
                {
                    "provider": "claude",
                    "account_id": "account-a",
                    "display_name": "Claude Code",
                    "collection_mode": "automatic",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "official",
                    "status": "ok",
                    "last_updated": now.isoformat().replace("+00:00", "Z"),
                    "windows": [
                        {"name": "five_hour", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": (now + timedelta(hours=3)).isoformat()},
                        {"name": "seven_day", "remaining_percent": 90.0, "used_percent": 10.0, "resets_at": (now + timedelta(days=6)).isoformat()}
                    ]
                },
                {
                    "provider": "claude",
                    "account_id": "account-b",
                    "display_name": "Claude Code",
                    "collection_mode": "automatic",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "official",
                    "status": "ok",
                    "last_updated": now.isoformat().replace("+00:00", "Z"),
                    "windows": [
                        {"name": "five_hour", "remaining_percent": 65.0, "used_percent": 35.0, "resets_at": (now + timedelta(hours=1)).isoformat()},
                        {"name": "seven_day", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": (now + timedelta(days=5)).isoformat()}
                    ]
                }
            ]
        }
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed with Claude A/B accounts: {at.exception}")

        # Verify both Claude account headers appear in markdown elements
        markdown_texts = [el.value for el in at.markdown]
        self.assertTrue(any("Claude Code (Account account-a)" in m or "Account account-a" in m for m in markdown_texts))
        self.assertTrue(any("Claude Code (Account account-b)" in m or "Account account-b" in m for m in markdown_texts))

    @patch("manager.quota_history.get_default_quota_history_store")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_corrupted_history_store(self, mock_build_service, mock_read_drive_status, mock_drive_records, mock_get_store):
        # Mock history store failure
        mock_store_inst = MagicMock()
        mock_store_inst.load.side_effect = Exception("Corrupt history JSON")
        mock_get_store.return_value = mock_store_inst

        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed on corrupt quota history: {at.exception}")

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_legacy_execution_missing_optional_fields(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]

        # Mock task and execution with missing optional fields
        def mock_children(parent, name=None):
            if "tasks" in parent:
                return [{"name": "test-task-1.json", "mimeType": "application/json"}]
            elif "executions" in parent:
                return [{"name": "test-exec-1.json", "mimeType": "application/json"}]
            return []

        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = mock_children

        def mock_get(area, project_id, name):
            if area == "tasks":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "title": "Task 1",
                    "status": "in_progress"
                }
            elif area == "executions":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "provider": "codex",
                    "status": "running"
                }
            return {}
        mock_store.get.side_effect = mock_get

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed on legacy execution: {at.exception}")

        # Verify executions table renders with fallback values
        df = at.table[0].value
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Provider Session"], "—")
        self.assertEqual(df.iloc[0]["Current Progress"], "—")
        self.assertEqual(df.iloc[0]["Model/Mode/Effort"], "— / — / —")

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_malformed_record_warning(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]

        # Simulate one valid file and one malformed file in the project folder
        def mock_children(parent, name=None):
            if "tasks" in parent:
                return [
                    {"name": "test-task-1.json", "mimeType": "application/json"},
                    {"name": "bad-task.json", "mimeType": "application/json"}
                ]
            return []

        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = mock_children

        def mock_get(area, project_id, name):
            if name == "test-task-1":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "title": "Valid Task",
                    "status": "ready",
                    "priority": "normal"
                }
            else:
                # Trigger exception for bad-task.json
                raise Exception("JSON schema validation failed")
        mock_store.get.side_effect = mock_get

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed with malformed record: {at.exception}")

        warnings = [el.value for el in at.warning]
        self.assertTrue(
            any("Malformed record" in w and "bad-task.json" in w for w in warnings),
            f"Expected malformed record warning, got: {warnings}"
        )

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_healthy_claude_execution(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]

        def mock_children(parent, name=None):
            if "executions" in parent:
                return [{"name": "test-exec-1.json", "mimeType": "application/json"}]
            return []

        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = mock_children

        def mock_get(area, project_id, name):
            if area == "executions":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "provider": "claude",
                    "account_id": "account-a",
                    "status": "running",
                    "provider_session_id": "sess-claude-1",
                    "heartbeat_at": "2026-08-15T11:00:00Z",  # 30 mins ago
                    "task_snapshot": {"expected_minutes": 45, "model": "claude-3"}
                }
            return {}
        mock_store.get.side_effect = mock_get

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed with healthy Claude execution: {at.exception}")

        # Verify UI Semantic: health is "✅ OK" and state is "RUNNING"
        df = at.table[0].value
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["State"], "RUNNING")
        self.assertEqual(df.iloc[0]["Health"], "✅ OK")
        self.assertEqual(df.iloc[0]["Account"], "account-a")

    # =================================================================
    # zh-TW Dashboard Truth Layer follow-up fixes (ChatGPT review R2)
    # =================================================================

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_zh_truth_no_eligible_provider_message_visible(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        """Wiring gap: dispatch_availability_zh() must actually be rendered
        when no dispatchable AI account exists, not merely exist as a
        tested-but-unused function."""
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        all_text = " ".join(
            [el.value for el in at.markdown] + [el.value for el in at.info] + [el.value for el in at.warning]
        )
        self.assertIn("自動派工目前不可用", all_text)

    @patch("subprocess.run")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_zh_truth_unknown_provenance_visible_as_unknown(
        self, mock_build_service, mock_read_drive_status, mock_drive_records, mock_subprocess_run
    ):
        """Blocker 3: with no persisted Provenance evidence and no real
        `schtasks`/`git` evidence available (forced here, since the actual
        host machine running this test may itself have a real Watcher
        Scheduled Task installed -- that would make the test's outcome
        depend on host state instead of on the code under test), every SHA
        is UNKNOWN -- the zh-TW card must say 未知 / 無法驗證, never 不一致."""
        mock_subprocess_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        all_text = " ".join(el.value for el in at.markdown)
        self.assertIn("未知 / 無法驗證", all_text)
        self.assertNotIn("整體一致性: **不一致**", all_text)

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_zh_truth_linked_session_older_than_recent_preload_still_renders(
        self, mock_build_service, mock_read_drive_status, mock_drive_records
    ):
        """Blocker 2: an Execution-linked Session that is NOT among a
        project's most-recently-modified six Session records must still be
        resolved via the exact bounded lookup and render as readable, not
        as session_unreadable."""
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]

        def mock_children(parent, name=None):
            if parent == "folder-tasks":
                return [{"name": "test-task-1.json", "mimeType": "application/json", "modifiedTime": "2026-08-20T00:00:00Z"}]
            if parent == "folder-executions":
                return [{"name": "test-exec-1.json", "mimeType": "application/json", "modifiedTime": "2026-08-20T00:00:00Z"}]
            if parent == "folder-commands":
                return [{"name": "test-cmd-1.json", "mimeType": "application/json", "modifiedTime": "2026-08-20T00:00:00Z"}]
            if parent == "folder-sessions":
                items = [
                    {"name": f"session-recent-{i}.json", "mimeType": "application/json", "modifiedTime": f"2026-08-{20 - i:02d}T00:00:00Z"}
                    for i in range(6)
                ]
                items.append({"name": "session-old.json", "mimeType": "application/json", "modifiedTime": "2026-01-01T00:00:00Z"})
                return items
            return []

        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = mock_children

        def mock_get(area, project_id, name):
            if area == "tasks":
                return {"project_id": "test-project", "task_id": "test-task-1", "title": "T", "status": "in_progress"}
            if area == "commands":
                return {
                    "project_id": "test-project", "task_id": "test-task-1", "command_id": "test-cmd-1",
                    "provider": "claude", "account_id": None, "status": "running", "execution_id": "test-exec-1",
                    "selection_reason": [],
                }
            if area == "executions":
                return {
                    "project_id": "test-project", "task_id": "test-task-1", "execution_id": "test-exec-1",
                    "provider": "claude", "status": "running",
                    "session_id": "session-old", "provider_session_id": "prov-old",
                }
            if area == "sessions":
                if name == "session-old":
                    return {"session_id": "session-old", "status": "completed", "provider": "claude"}
                return {"session_id": name, "status": "obsolete", "provider": "claude"}
            return {}
        mock_store.get.side_effect = mock_get
        mock_store.latest.side_effect = TaskError("no handoff found for task: test-task-1")

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        all_text = " ".join(el.value for el in at.markdown)
        self.assertIn("session-old", all_text)
        self.assertNotIn("session_unreadable", all_text)


if __name__ == "__main__":
    unittest.main()
