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

    def test_derive_and_reconcile_automatic_actions_lifecycle(self):
        """Blocker 1 & 3: Auto-derived actions must be persisted into ActionsStore,

        support Acknowledge/Resolve/Dismiss, retain status and stable waiting_since across reruns,
        and not immediately recreate duplicate open copies when resolved.
        """
        now1 = datetime(2026, 8, 21, 2, 0, 0, tzinfo=timezone.utc)

        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files
        mock_files.list.return_value.execute.return_value = {"files": []}

        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "actions.json"
            store = ActionsStore(drive_service=mock_drive, local_file_path=str(cache_file))

            tasks = [
                {"task_id": "T-BLOCKED-1", "project_id": "proj-a", "title": "Blocked Pipeline", "status": "blocked", "created_at": "2026-08-21T02:00:00Z"},
            ]

            # Step 1: First detection and reconciliation at 02:00
            candidates1 = derive_automatic_actions(all_tasks=tasks, active_executions=[], ideas_conflicted=[], now=now1)
            reconciled1 = store.reconcile_automatic_actions(candidates1)

            self.assertEqual(len(reconciled1), 1)
            item1 = reconciled1[0]
            self.assertEqual(item1.status, STATUS_OPEN)
            self.assertEqual(item1.waiting_since, "2026-08-21T02:00:00Z")

            # Step 2: User Acknowledges the action at 02:10
            store.acknowledge_action(item1.action_id, note="Investigating blocker")
            self.assertEqual(store.get_by_id(item1.action_id).status, STATUS_ACKNOWLEDGED)

            # Step 3: Rerun at 02:15 with same underlying blocked task
            now2 = datetime(2026, 8, 21, 2, 15, 0, tzinfo=timezone.utc)
            candidates2 = derive_automatic_actions(all_tasks=tasks, active_executions=[], ideas_conflicted=[], now=now2)
            reconciled2 = store.reconcile_automatic_actions(candidates2)

            self.assertEqual(len(reconciled2), 1)
            item2 = reconciled2[0]
            # State must remain ACKNOWLEDGED (not reset to OPEN)
            self.assertEqual(item2.status, STATUS_ACKNOWLEDGED)
            # waiting_since must remain stable at 02:00
            self.assertEqual(item2.waiting_since, "2026-08-21T02:00:00Z")
            # Waiting duration derived at 02:15 is 15m
            self.assertEqual(format_waiting_duration(item2.waiting_since, now2), "15m 00s")

            # Step 4: User Resolves the action at 02:20
            store.resolve_action(item1.action_id, note="Blocker resolved")
            self.assertEqual(store.get_by_id(item1.action_id).status, STATUS_RESOLVED)

            # Step 5: Rerun at 02:25 does NOT reopen or duplicate the resolved action
            now3 = datetime(2026, 8, 21, 2, 25, 0, tzinfo=timezone.utc)
            candidates3 = derive_automatic_actions(all_tasks=tasks, active_executions=[], ideas_conflicted=[], now=now3)
            reconciled3 = store.reconcile_automatic_actions(candidates3)

            self.assertEqual(len(reconciled3), 1)
            self.assertEqual(reconciled3[0].status, STATUS_RESOLVED)
            # Summary shows in history, 0 open
            summary = get_actions_summary(reconciled3)
            self.assertEqual(summary["open"], 0)
            self.assertEqual(summary["history"], 1)

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
                "provider": "codex",
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
