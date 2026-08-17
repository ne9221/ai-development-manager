import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import streamlit as st
from streamlit.testing.v1 import AppTest

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
            any("Quota data is stale" in msg for msg in warning_messages),
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
        # We assert no progress elements exist since there is no progress bar rendered
        self.assertEqual(len(at.get("progress")), 0, "Progress bar should not be drawn for None values")

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
                    # missing task_snapshot, provider_session_id, heartbeat_at, expected_minutes
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
        
        # Verify UI Semantic: the valid task is rendered (we can verify in tab title count or cards)
        # Verify UI Semantic: warning message for malformed record is displayed
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

if __name__ == "__main__":
    unittest.main()
