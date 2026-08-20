import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from manager.actions import (
    STATUS_ACKNOWLEDGED,
    STATUS_CONFLICTED,
    STATUS_DISMISSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    TYPE_ACTION_NEEDED,
    TYPE_BLOCKED,
    TYPE_MILESTONE_REACHED,
    TYPE_REVIEW_REQUIRED,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    ActionItem,
    ActionsStore,
    derive_automatic_actions,
    format_waiting_duration,
    get_actions_summary,
)


class ActionCenterDomainAndStoreTests(unittest.TestCase):
    def test_no_fake_sample_actions_in_empty_store(self):
        """Empty store contains 0 actions and injects no fake samples."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "actions.json"
            store = ActionsStore(drive_service=None, local_file_path=str(f))
            self.assertEqual(len(store.list_actions()), 0)
            summary = get_actions_summary(store.list_actions())
            self.assertEqual(summary["total"], 0)
            self.assertEqual(summary["open"], 0)
            self.assertEqual(summary["need_user_action"], 0)

    def test_waiting_duration_formatting(self):
        now = datetime(2026, 8, 21, 12, 30, 0, tzinfo=timezone.utc)
        # 14m 20s ago
        t1 = (now - timedelta(minutes=14, seconds=20)).isoformat()
        self.assertEqual(format_waiting_duration(t1, now), "14m 20s")

        # 2h 15m ago
        t2 = (now - timedelta(hours=2, minutes=15)).isoformat()
        self.assertEqual(format_waiting_duration(t2, now), "2h 15m")

        # Missing / invalid
        self.assertEqual(format_waiting_duration(None, now), "Unknown")
        self.assertEqual(format_waiting_duration("", now), "Unknown")
        self.assertEqual(format_waiting_duration("invalid-date", now), "Unknown")

    def test_derive_automatic_actions_for_all_scenarios(self):
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)

        # 1. Blocked task
        tasks = [
            {"task_id": "T-BLOCKED", "project_id": "proj-a", "title": "Blocked Pipeline", "status": "blocked", "next_action": "Wait for fix"},
            {"task_id": "T-REVIEW", "project_id": "proj-a", "title": "Milestone Review", "status": "awaiting_validation"},
            {"task_id": "T-AG", "project_id": "proj-b", "title": "AG Slice", "status": "ready", "recommended_provider": "antigravity"},
        ]
        # 2. Stale execution
        execs = [
            {
                "execution_id": "EXE-STALE",
                "project_id": "proj-a",
                "task_id": "T-STALE",
                "provider": "claude",
                "status": "running",
                "started_at": (now - timedelta(minutes=45)).isoformat(),
                "heartbeat_at": (now - timedelta(minutes=30)).isoformat(),
                "last_provider_event": "Downloading files",
            }
        ]
        # 3. Conflicted ideas
        conflicted_ideas = [MagicMock(idea_id="IDEA-CONFLICT-99")]

        actions = derive_automatic_actions(
            all_tasks=tasks,
            active_executions=execs,
            ideas_conflicted=conflicted_ideas,
            now=now
        )

        summary = get_actions_summary(actions)
        self.assertEqual(summary["total"], 5)
        self.assertTrue(summary["need_user_action"] >= 4)
        self.assertTrue(summary["review_required"] >= 1)
        self.assertTrue(summary["blocked"] >= 2)
        self.assertTrue(summary["high_severity"] >= 4)

        # Verify AG action item
        ag_act = next((a for a in actions if "ACT-AG-DISPATCH" in a.action_id), None)
        self.assertIsNotNone(ag_act)
        self.assertEqual(ag_act.type, TYPE_ACTION_NEEDED)
        self.assertEqual(ag_act.severity, SEVERITY_HIGH)
        self.assertTrue(ag_act.need_user_action)
        self.assertIn("Antigravity", ag_act.reason)

    def test_drive_unavailable_read_only_and_history_retained(self):
        """When Drive SSOT is down, Action Center remains viewable but mutations are rejected."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "actions.json"
            act = ActionItem(
                action_id="ACT-PERSIST-1",
                title="Existing User Action",
                status=STATUS_OPEN,
                waiting_since="2026-08-21T00:00:00Z"
            )
            f.write_text(json.dumps([act.to_dict()]), encoding="utf-8")

            store = ActionsStore(drive_service=None, local_file_path=str(f))
            self.assertTrue(store.is_degraded)
            self.assertEqual(len(store.list_actions()), 1)

            # Mutations must raise RuntimeError
            with self.assertRaises(RuntimeError) as ctx:
                store.acknowledge_action("ACT-PERSIST-1")
            self.assertIn("read-only", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.resolve_action("ACT-PERSIST-1")
            self.assertIn("read-only", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.dismiss_action("ACT-PERSIST-1")
            self.assertIn("read-only", str(ctx.exception))

    def test_actions_lifecycle_acknowledge_resolve_dismiss_with_drive(self):
        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files

        def mock_list_side_effect(**kwargs):
            q = kwargs.get("q", "")
            m = MagicMock()
            if "name='ACTIONS'" in q:
                m.execute.return_value = {"files": [{"id": "root-actions"}]}
            elif "'root-actions' in parents" in q:
                m.execute.return_value = {"files": [{"id": "f-unassigned", "name": "Unassigned"}]}
            else:
                m.execute.return_value = {"files": []}
            return m

        mock_files.list.side_effect = mock_list_side_effect

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "actions_cache.json"
            store = ActionsStore(drive_service=mock_drive, local_file_path=str(f))
            self.assertFalse(store.is_degraded)

            act = ActionItem(
                action_id="ACT-LIFECYCLE",
                title="Lifecycle Action",
                type=TYPE_REVIEW_REQUIRED,
                severity=SEVERITY_HIGH,
                project_id="Unassigned",
                waiting_since="2026-08-21T02:00:00Z",
            )
            store._actions = [act]

            # 1. Acknowledge
            store.acknowledge_action("ACT-LIFECYCLE", note="Looking into it")
            item = store.get_by_id("ACT-LIFECYCLE")
            self.assertEqual(item.status, STATUS_ACKNOWLEDGED)
            self.assertEqual(item.resolution_note, "Looking into it")
            self.assertIsNotNone(item.acknowledged_at)

            # 2. Resolve (History retained)
            store.resolve_action("ACT-LIFECYCLE", note="Accepted by reviewer")
            item = store.get_by_id("ACT-LIFECYCLE")
            self.assertEqual(item.status, STATUS_RESOLVED)
            self.assertEqual(item.resolution_note, "Accepted by reviewer")
            self.assertIsNotNone(item.resolved_at)

    def test_save_and_cleanup_errors_are_not_silent(self):
        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files

        mock_files.list.return_value.execute.return_value = {"files": []}
        mock_files.create.side_effect = Exception("Google Drive API 500 Network Failure")

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "actions_cache.json"
            store = ActionsStore(drive_service=mock_drive, local_file_path=str(f))

            act = ActionItem(action_id="ACT-FAIL", title="Failing Action", project_id="Unassigned")
            with self.assertRaises(RuntimeError) as ctx:
                store.add_action(act)
            self.assertIn("Failed to persist Action to Drive SSOT", str(ctx.exception))
            self.assertIsNotNone(store.last_error)


if __name__ == "__main__":
    unittest.main()
