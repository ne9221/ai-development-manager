import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import streamlit as st
from streamlit.testing.v1 import AppTest


def run_full_data_view(at):
    """Render the current Overview route used by lifecycle truth tests."""
    at.run(timeout=30)
    return at


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
        run_full_data_view(at)

        self.assertFalse(at.exception, f"App crashed on empty drive: {at.exception}")

        # Verify UI semantic: with an empty Drive every expected provider is
        # stale/unknown, so nothing is dispatchable -> exactly ONE page-level
        # warning, and the per-entity state lives inside the cards.
        warning_messages = [el.value for el in at.warning]
        page_level = [msg for msg in warning_messages if "沒有可派工" in msg]
        self.assertEqual(1, len(page_level), f"Expected one page-level warning, got: {warning_messages}")
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertTrue('qe-state-stale">過期' in html or 'qe-state-unknown">未知' in html, html[:400])

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_auto_refresh_arms_without_a_first_paint_rerun(self, mock_build_service, mock_read_drive_status,
                                                            mock_drive_records):
        """The 60s fragment must not rerun the app immediately on first paint."""
        mock_read_drive_status.return_value = {"providers": []}
        mock_drive_records.return_value.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)

        self.assertFalse(at.exception, f"Auto-refresh first paint crashed: {at.exception}")
        self.assertTrue(at.session_state["dashboard_auto_refresh_armed"])

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
        run_full_data_view(at)

        self.assertFalse(at.exception, f"App crashed on remaining_percent=None: {at.exception}")

        # Verify UI semantic: a missing percentage is UNKNOWN inside the card
        # (never 0%, never a page banner).
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertIn('qe-state-unknown">未知', html)
        self.assertIn('<span class="qw-value">未知</span>', html)
        self.assertNotIn('<span class="qw-value">0%</span>', html)

        # Verify progress bar is not drawn for None
        self.assertEqual(len(at.get("progress")), 0, "Progress bar should not be drawn for None values")

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_zero_percent_quota(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        # Setup quota with remaining_percent=0.0
        now = datetime.now(timezone.utc)
        reset_at = (now + timedelta(hours=2)).isoformat()
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
                        {"name": "primary", "remaining_percent": 0.0, "used_percent": 100.0, "resets_at": reset_at}
                    ]
                }
            ]
        }
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)

        self.assertFalse(at.exception, f"App crashed on remaining_percent=0: {at.exception}")
        # Zero remains explicit inside the entity card and is never confused
        # with missing data; the reset time is shown in human form.
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertIn('<span class="qw-label">5H</span><span class="qw-value">0%</span>', html)
        self.assertIn('qe-state-exhausted">已用盡', html)
        self.assertIn("重置 ", html)

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
        run_full_data_view(at)

        self.assertFalse(at.exception, f"App crashed with Claude A/B accounts: {at.exception}")

        # Both Claude accounts are separate entities with their own card.
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertIn('<span class="qe-title">Claude A</span>', html)
        self.assertIn('<span class="qe-title">Claude B</span>', html)
        self.assertEqual(2, html.count('class="qe-card'))

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
        run_full_data_view(at)

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
        run_full_data_view(at)

        self.assertFalse(at.exception, f"App crashed on legacy execution: {at.exception}")

        # Overview remains readable without exposing a dense execution table;
        # the secondary Sessions route owns those raw execution fields.
        self.assertTrue(any("目前沒有" in str(el.value) for el in [*at.warning, *at.info]))

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
        run_full_data_view(at)

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
        run_full_data_view(at)

        self.assertFalse(at.exception, f"App crashed with healthy Claude execution: {at.exception}")

        # The Overview promotes only genuinely active work; old execution
        # details remain available through the Sessions secondary route.
        self.assertTrue(any("account-a" in str(el.value) or "需要處理" in str(el.value)
                            for el in [*at.warning, *at.markdown]))

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_app_render_task_inspector_finds_exact_execution_and_handoff(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        """Regression for dashboard-quota-canonical-truth-20260824: a
        completed task's execution and handoff must be found and rendered
        (not "No active execution." / "No handoff records found for this
        task." -- the pre-fix behavior even when real Drive records exist,
        because active_executions_dict only covered non-terminal executions
        and handoffs_dict was permanently empty)."""
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]

        def mock_children(parent, name=None, deadline=None):
            if "tasks" in parent:
                return [{"name": "test-task-1.json", "mimeType": "application/json"}]
            elif "commands" in parent:
                return [{"name": "cmd-task-1.json", "mimeType": "application/json"}]
            elif "executions" in parent:
                return [{"name": "exec-task-1.json", "mimeType": "application/json"}]
            elif "handoffs" in parent:
                return [{"name": "test-task-1-final-20260823.json", "mimeType": "application/json"}]
            return []

        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = mock_children

        def mock_get(area, project_id, name):
            if area == "tasks":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "title": "Task 1",
                    "status": "completed",
                    "assigned_provider": "claude",
                }
            elif area == "commands":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "command_id": "cmd-task-1",
                    "execution_id": "exec-task-1",
                    "provider": "claude",
                    "account_id": "account-a",
                    "status": "completed",
                    "result": {"outcome": "success", "session_id": "sess-claude-alpha"}
                }
            elif area == "executions":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "execution_id": "exec-task-1",
                    "provider": "claude",
                    "account_id": "account-a",
                    "status": "completed",
                    "provider_session_id": "sess-claude-alpha",
                    "completed_at": "2026-08-23T15:00:00Z"
                }
            elif area == "handoffs":
                return {
                    "project_id": "test-project",
                    "task_id": "test-task-1",
                    "handoff_id": "test-task-1-final-20260823",
                    "created_at": "2026-08-23T15:00:00Z",
                    "from_provider": "claude",
                    "from_session": "sess-claude-alpha",
                    "reason": "completed",
                    "current_state": "completed",
                    "completed_work": ["Full E2E suite executed successfully", "All assertions verified"],
                }
            return {}
        mock_store.get.side_effect = mock_get

        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)

        self.assertFalse(at.exception, f"App crashed during inspector render: {at.exception}")

        text_dump = " ".join(str(el.value) for el in at.markdown)
        # Execution must be found (task-scoped, terminal status included) --
        # not the pre-fix "No active execution."
        self.assertIn("exec-task-1", text_dump)
        self.assertNotIn("此 Task 沒有 Command 或 Execution 紀錄", text_dump)
        # Handoff must be found on-demand -- not the pre-fix "No handoff
        # records found for this task." even though a real handoff exists.
        self.assertTrue(
            any("Full E2E suite executed successfully" in str(el.value) for el in at.markdown)
            or any("Full E2E suite executed successfully" in str(el.value) for el in at.json)
        )


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

        mock_list_ids.return_value = {"request_ids": ["req-1"], "truncated": False}
        mock_registry.return_value = MagicMock()
        mock_resolve.return_value = resolved

        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)
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

    # Blocker 1 (PRETASK_FALSE_NEGATIVE_RISK): when the bounded recent-request
    # scan could not prove completeness (list_recent_dispatch_request_ids's
    # truncated=True), the real rendered Dashboard must show an explicit
    # UNKNOWN/incomplete row -- never silently nothing, even with zero
    # resolved pretask rows.
    @patch("manager.dispatch_requests.dispatch_request_registry")
    @patch("manager.dispatch_requests.resolve_dispatch_status_for_request")
    @patch("manager.dispatch_requests.list_recent_dispatch_request_ids")
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_pretask_truncated_listing_renders_unknown_not_silent_none(self, mock_build_service, mock_read_drive_status,
                                                                         mock_drive_records, mock_list_ids, mock_resolve, mock_registry):
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [{"project_id": "test-project", "title": "Test Project"}]
        mock_store.project_folder.side_effect = lambda area, project_id, create=False: f"folder-{area}"
        mock_store.children.side_effect = lambda parent, name=None: []
        mock_store.get.side_effect = lambda area, project_id, name: {}

        mock_list_ids.return_value = {"request_ids": [], "truncated": True}
        mock_registry.return_value = MagicMock()
        mock_resolve.return_value = {"task": None, "command": None}

        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)
        self.assertFalse(at.exception, f"App crashed rendering a truncated pre-Task listing: {at.exception}")
        markdown_texts = [el.value for el in at.markdown]
        self.assertTrue(any("UNKNOWN" in m and "incomplete" in m for m in markdown_texts),
                        f"Expected a rendered UNKNOWN row for the truncated listing, got: {markdown_texts}")




def _entry(provider, account_id, five, weekly, updated, names=("five_hour", "seven_day"), metadata=None):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    entry = {
        "provider": provider, "account_id": account_id,
        "display_name": "Codex" if provider == "codex" else "Claude Code",
        "collection_mode": "automatic",
        "source": "codex_app_server" if provider == "codex" else "claude_oauth_usage",
        "source_type": "official", "confidence": "official", "status": "ok",
        "last_updated": updated,
        "windows": [
            {"name": names[0], "duration_minutes": 300, "remaining_percent": five, "used_percent": 100 - five,
             "resets_at": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")},
            {"name": names[1], "duration_minutes": 10080, "remaining_percent": weekly, "used_percent": 100 - weekly,
             "resets_at": (now + timedelta(days=4)).isoformat().replace("+00:00", "Z")},
        ],
    }
    if metadata is not None:
        entry["metadata"] = metadata
    return entry


class TestQuotaEntityCardsAndInformationArchitecture(unittest.TestCase):
    """Phase C acceptance: exactly three identical entity cards (Codex, Claude
    A, Claude B) each with 5H + weekly, freshness inside the card, one
    page-level warning at most, zh-TW navigation, and no invented running
    state. Fixtures mirror the live 2026-09-02 status.json shape."""

    def _live_shape(self):
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        stale = "2026-09-01T14:41:18Z"
        note = {"refresh": {"outcome": "unchanged", "attempted_at": now_str,
                            "error": "CredentialsUnavailableError: access token missing from credentials"}}
        return {"providers": [
            _entry("claude", None, 100, 6, stale, metadata=note),          # legacy aggregate -- must be hidden
            _entry("claude", "account-a", 100, 6, stale, metadata=note),   # stale, needs re-login
            _entry("codex", None, 75, 39, now_str, names=("primary", "secondary")),
            _entry("claude", "account-b", 51, 93, now_str),
        ]}

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_overview_shows_exactly_three_entity_cards_with_both_windows(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = self._live_shape()
        mock_drive_records.return_value.list_projects.return_value = []
        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)
        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertEqual(3, html.count('class="qe-card'), "exactly Codex, Claude A, Claude B")
        for title in ("Codex", "Claude A", "Claude B"):
            self.assertIn(f'<span class="qe-title">{title}</span>', html)
        self.assertNotIn('<span class="qe-title">Claude</span>', html, "no nameless Claude aggregate card")
        self.assertNotIn("Claude Code", html.split('class="qe-grid"')[1].split("</div></div></div>")[0] if 'class="qe-grid"' in html else "")
        # Every card carries BOTH windows with a reset time.
        self.assertEqual(3, html.count('<span class="qw-label">5H</span>'))
        self.assertEqual(3, html.count('<span class="qw-label">每週</span>'))
        self.assertEqual(0, html.count("每週窗口：來源未提供"))
        self.assertGreaterEqual(html.count("重置 "), 6)
        # Codex weekly (secondary) is 39%, Claude B weekly is 93%.
        self.assertIn('<span class="qw-value">39%</span>', html)
        self.assertIn('<span class="qw-value">93%</span>', html)

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_stale_account_is_explained_inside_its_card_not_as_page_banners(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = self._live_shape()
        mock_drive_records.return_value.list_projects.return_value = []
        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)
        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        html = "\n".join(str(el.value) for el in at.markdown)
        # Claude A: stale, last-good number still visible (STALE != 0), reason inside the card.
        a_card = html.split('<span class="qe-title">Claude A</span>')[1].split('class="qe-card')[0]
        self.assertIn('qe-state-stale">過期', a_card)
        self.assertIn('<span class="qw-value">100%</span>', a_card)
        self.assertIn("需要重新登入", a_card)
        # Claude B: fresh, no success chatter, no warning.
        b_card = html.split('<span class="qe-title">Claude B</span>')[1].split('class="qe-card')[0]
        self.assertIn('qe-state-fresh">最新', b_card)
        self.assertNotIn("qe-note", b_card)
        # Page-level: at most one warning, and here (Claude B and Codex are dispatchable) none about quota.
        quota_warnings = [str(el.value) for el in at.warning if "配額" in str(el.value) or "STALE" in str(el.value) or "派工" in str(el.value)]
        self.assertEqual([], quota_warnings, f"stale must not produce page banners: {quota_warnings}")
        self.assertEqual(0, len(at.success), "no success chatter when data is fresh")

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_single_page_warning_only_when_nothing_is_dispatchable(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        stale = "2026-09-01T14:41:18Z"
        mock_read_drive_status.return_value = {"providers": [
            _entry("claude", "account-a", 100, 6, stale), _entry("claude", "account-b", 51, 93, stale),
            _entry("codex", None, 75, 39, stale, names=("primary", "secondary")),
        ]}
        mock_drive_records.return_value.list_projects.return_value = []
        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)
        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        page_warnings = [str(el.value) for el in at.warning if "沒有可派工" in str(el.value)]
        self.assertEqual(1, len(page_warnings), f"exactly one page-level warning: {[str(e.value) for e in at.warning]}")
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertEqual(3, html.count('qe-state-stale">過期'))

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_navigation_is_traditional_chinese_and_idle_state_never_says_running(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = self._live_shape()
        mock_drive_records.return_value.list_projects.return_value = []
        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)
        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        nav = next(r for r in at.sidebar.radio if r.key == "adm_ui_route")
        self.assertEqual(["總覽", "專案", "任務", "執行", "配額", "想法", "系統健康"], list(nav.options))
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertIn("目前閒置", html)
        self.assertNotIn('class="state-running">執行中', html, "no Execution -> never 執行中")
        for label in ("Projects", "Tasks", "Sessions", "History", "Logs", "Quota"):
            self.assertNotIn(f">{label}<", html)


    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_unknown_status_with_numeric_window_renders_as_unknown_not_fresh(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        entry = _entry("claude", "account-b", 51, 93, now_str)
        entry["status"] = "unknown"
        mock_read_drive_status.return_value = {"providers": [entry]}
        mock_drive_records.return_value.list_projects.return_value = []
        at = AppTest.from_file("../dashboard.py")
        run_full_data_view(at)
        self.assertFalse(at.exception, f"App crashed: {at.exception}")
        html = "\n".join(str(el.value) for el in at.markdown)
        self.assertIn('qe-state-unknown">未知', html)
        self.assertNotIn('qe-state-fresh">最新', html)
        # The retained numeric evidence is still shown as itself, never as 0%.
        self.assertIn('<span class="qw-value">51%</span>', html)

if __name__ == "__main__":
    unittest.main()
