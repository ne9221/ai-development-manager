import unittest
from unittest.mock import patch, MagicMock
from streamlit.testing.v1 import AppTest

class TestDashboardAppRender(unittest.TestCase):
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_combined_scenarios(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        # 1. Mock quota status document containing adversarial values
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
                    "last_updated": "2026-08-15T11:00:00Z",
                    "windows": [
                        {"name": "primary", "remaining_percent": None, "used_percent": None}
                    ]
                },
                {
                    "provider": "claude",
                    "display_name": "Claude Code",
                    "status": "unknown",
                    "collection_mode": "automatic",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "unknown",
                    "last_updated": "2026-08-09T04:14:40Z",
                    "windows": []
                }
            ]
        }

        # 2. Mock DriveRecords store
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]
        
        # We mock store.project_folder and children to return mock list of files for list_records_isolated
        def mock_children(parent, name=None):
            if "tasks" in parent:
                return [{"name": "test-task-1.json", "mimeType": "application/json"}]
            elif "executions" in parent:
                return [{"name": "test-exec-1.json", "mimeType": "application/json"}]
            return []
            
        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = mock_children
        
        # We mock store.get to return task and execution details
        def mock_get(area, project_id, name):
            if area == "tasks":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "title": "Test Task 1",
                    "status": "in_progress",
                    "priority": "high",
                    "assigned_provider": "claude",
                    "current_progress": "Running checks",
                    "next_action": "Wait for results"
                }
            elif area == "executions":
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

        # Run AppTest
        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)
        
        # Verify app ran and did not throw exception
        self.assertFalse(at.exception, f"App crashed with exception: {at.exception}")

if __name__ == "__main__":
    unittest.main()
