import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
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


class TestDashboardVisibleBeforeTaskRender(unittest.TestCase):
    """Real Streamlit rendering coverage for VISIBLE_BEFORE_TASK: a
    dispatch request ingress has durably observed (ACCEPTED/REJECTED/
    FAILED, or whose status read itself failed) before any Task record
    exists yet must still be shown in the actual rendered Dashboard page --
    not just proven at the dashboard_core view-model level."""

    def setUp(self):
        st.cache_data.clear()
        self._bucket_patch = patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"})
        self._bucket_patch.start()
        self.addCleanup(self._bucket_patch.stop)

    def _run_with_pretask_resolution(self, resolved, mock_build_service, mock_read_drive_status, mock_drive_records,
                                      mock_list_ids, mock_resolve, mock_registry):
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]
        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = lambda parent, name=None: []
        mock_store.get.side_effect = lambda area, project_id, name: {}

        mock_list_ids.return_value = ["req-1"]
        mock_registry.return_value = MagicMock()
        mock_resolve.return_value = resolved

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)
        self.assertFalse(at.exception, f"App crashed rendering a pre-Task dispatch row: {at.exception}")
        return at

    # F.1 / C.1: task=None, request status=accepted -> Dashboard shows ACCEPTED
    @patch("manager.dispatch_requests.dispatch_request_registry")
    @patch("manager.dispatch_requests.resolve_dispatch_status_for_request")
    @patch("manager.dispatch_requests.list_recent_dispatch_request_ids")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_pretask_accepted_request_renders_accepted(self, mock_build_service, mock_read_drive_status,
                                                         mock_drive_records, mock_list_ids, mock_resolve, mock_registry):
        resolved = {"task": None, "command": None, "task_id": "dispatch-req-1", "command_id": "dispatch-req-1",
                    "dispatch_request_status": {"status": "accepted", "failure_reason": None},
                    "dispatch_request_read_failed": False}
        at = self._run_with_pretask_resolution(resolved, mock_build_service, mock_read_drive_status,
                                                mock_drive_records, mock_list_ids, mock_resolve, mock_registry)
        markdown_texts = [el.value for el in at.markdown]
        self.assertTrue(any("ACCEPTED" in m and "req-1" in m for m in markdown_texts),
                        f"Expected a rendered ACCEPTED pre-Task row for req-1, got: {markdown_texts}")

    # F.2 / C.2: task=None, request status=rejected -> Dashboard shows REJECTED + sanitized reason
    @patch("manager.dispatch_requests.dispatch_request_registry")
    @patch("manager.dispatch_requests.resolve_dispatch_status_for_request")
    @patch("manager.dispatch_requests.list_recent_dispatch_request_ids")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_pretask_rejected_request_renders_rejected_with_reason(self, mock_build_service, mock_read_drive_status,
                                                                     mock_drive_records, mock_list_ids, mock_resolve, mock_registry):
        resolved = {"task": None, "command": None, "task_id": "dispatch-req-1", "command_id": "dispatch-req-1",
                    "dispatch_request_status": {"status": "rejected", "reason_code": "malformed_request"},
                    "dispatch_request_read_failed": False}
        at = self._run_with_pretask_resolution(resolved, mock_build_service, mock_read_drive_status,
                                                mock_drive_records, mock_list_ids, mock_resolve, mock_registry)
        markdown_texts = [el.value for el in at.markdown]
        self.assertTrue(any("REJECTED" in m and "malformed_request" in m for m in markdown_texts),
                        f"Expected a rendered REJECTED row with reason, got: {markdown_texts}")

    # F.3: task=None, request status=failed -> Dashboard shows FAILED + reason
    @patch("manager.dispatch_requests.dispatch_request_registry")
    @patch("manager.dispatch_requests.resolve_dispatch_status_for_request")
    @patch("manager.dispatch_requests.list_recent_dispatch_request_ids")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_pretask_failed_request_renders_failed_with_reason(self, mock_build_service, mock_read_drive_status,
                                                                 mock_drive_records, mock_list_ids, mock_resolve, mock_registry):
        resolved = {"task": None, "command": None, "task_id": "dispatch-req-1", "command_id": "dispatch-req-1",
                    "dispatch_request_status": {"status": "failed", "failure_reason": "no eligible provider"},
                    "dispatch_request_read_failed": False}
        at = self._run_with_pretask_resolution(resolved, mock_build_service, mock_read_drive_status,
                                                mock_drive_records, mock_list_ids, mock_resolve, mock_registry)
        markdown_texts = [el.value for el in at.markdown]
        self.assertTrue(any("FAILED" in m and "no eligible provider" in m for m in markdown_texts),
                        f"Expected a rendered FAILED row with reason, got: {markdown_texts}")

    # C.4 / F.6: request status read failure -> Dashboard shows UNKNOWN, never nothing.
    @patch("manager.dispatch_requests.dispatch_request_registry")
    @patch("manager.dispatch_requests.resolve_dispatch_status_for_request")
    @patch("manager.dispatch_requests.list_recent_dispatch_request_ids")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_pretask_read_failure_renders_unknown_not_nothing(self, mock_build_service, mock_read_drive_status,
                                                                mock_drive_records, mock_list_ids, mock_resolve, mock_registry):
        resolved = {"task": None, "command": None, "task_id": "dispatch-req-1", "command_id": "dispatch-req-1",
                    "dispatch_request_status": None, "dispatch_request_read_failed": True}
        at = self._run_with_pretask_resolution(resolved, mock_build_service, mock_read_drive_status,
                                                mock_drive_records, mock_list_ids, mock_resolve, mock_registry)
        markdown_texts = [el.value for el in at.markdown]
        self.assertTrue(any("UNKNOWN" in m and "req-1" in m and "read failed" in m for m in markdown_texts),
                        f"Expected an UNKNOWN row with a read-failure reason, not nothing at all, got: {markdown_texts}")

    # C.3 / STATE_PROMOTION: once a Task exists for the request, the
    # pre-Task ingress-only row must NOT also be shown (no duplicate row).
    @patch("manager.dispatch_requests.dispatch_request_registry")
    @patch("manager.dispatch_requests.resolve_dispatch_status_for_request")
    @patch("manager.dispatch_requests.list_recent_dispatch_request_ids")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_task_existing_suppresses_duplicate_pretask_row(self, mock_build_service, mock_read_drive_status,
                                                              mock_drive_records, mock_list_ids, mock_resolve, mock_registry):
        # resolve_dispatch_status_for_request() reports a real Task now
        # exists for this request_id -- load_pretask_dispatch_requests()
        # must filter it out entirely (never build a pretask row for it).
        resolved = {"task": {"task_id": "dispatch-req-1", "project_id": "test-project", "title": "Real task",
                              "status": "ready"},
                    "command": None, "task_id": "dispatch-req-1", "command_id": "dispatch-req-1",
                    "dispatch_request_status": None, "dispatch_request_read_failed": False}
        at = self._run_with_pretask_resolution(resolved, mock_build_service, mock_read_drive_status,
                                                mock_drive_records, mock_list_ids, mock_resolve, mock_registry)
        markdown_texts = [el.value for el in at.markdown]
        self.assertFalse(any("(pre-task) request req-1" in m for m in markdown_texts),
                         f"A Task now exists for req-1 -- no ingress-only pre-Task row must be shown, got: {markdown_texts}")


if __name__ == "__main__":
    unittest.main()
