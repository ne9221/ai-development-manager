import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from manager.ideas import (
    STATUS_CONFIRMED,
    STATUS_CONVERTED,
    STATUS_DROPPED,
    STATUS_PENDING,
    IdeaItem,
    IdeasStore,
    get_ideas_summary,
    group_ideas_by_status,
)


class IdeasModelAndStoreTests(unittest.TestCase):
    def test_empty_store_has_zero_ideas_and_no_fake_sample_injection(self):
        """Zero ideas when no file or drive exists -- no fake ideas injected."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "non_existent_ideas.json"
            store = IdeasStore(drive_service=None, local_file_path=str(f))
            self.assertEqual(len(store.list_ideas()), 0)
            summary = get_ideas_summary(store.list_ideas())
            self.assertEqual(summary["total"], 0)
            self.assertEqual(summary["pending"], 0)

    def test_idea_item_roundtrip(self):
        item = IdeaItem(
            idea_id="IDEA-101",
            title="Real User Idea",
            description="Testing roundtrip serialization",
            status=STATUS_CONFIRMED,
            priority="high",
            project_id="test-proj",
            milestone_id="M1",
            task_id="T1",
            created_at="2026-08-21T00:00:00Z",
            source="Chat",
            decision_note="Approved",
            converted_at="2026-08-21T01:00:00Z",
        )
        d = item.to_dict()
        self.assertEqual(d["idea_id"], "IDEA-101")
        self.assertEqual(d["status"], "confirmed")

        reconstructed = IdeaItem.from_dict(d)
        self.assertEqual(reconstructed.idea_id, "IDEA-101")
        self.assertEqual(reconstructed.milestone_id, "M1")
        self.assertEqual(reconstructed.decision_note, "Approved")

    def test_grouping_and_summary(self):
        ideas = [
            IdeaItem(idea_id="1", title="A", description="", status=STATUS_PENDING),
            IdeaItem(idea_id="2", title="B", description="", status=STATUS_PENDING),
            IdeaItem(idea_id="3", title="C", description="", status=STATUS_CONFIRMED),
            IdeaItem(idea_id="4", title="D", description="", status=STATUS_CONVERTED),
            IdeaItem(idea_id="5", title="E", description="", status=STATUS_DROPPED),
        ]
        grouped = group_ideas_by_status(ideas)
        self.assertEqual(len(grouped[STATUS_PENDING]), 2)
        self.assertEqual(len(grouped[STATUS_CONFIRMED]), 1)
        self.assertEqual(len(grouped[STATUS_CONVERTED]), 1)
        self.assertEqual(len(grouped[STATUS_DROPPED]), 1)

        summary = get_ideas_summary(ideas)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["converted"], 1)
        self.assertEqual(summary["dropped"], 1)

    def test_drive_unavailable_read_only_rejection_and_cache_viewable(self):
        """When Drive SSOT is unavailable, local cache is viewable but all mutations are rejected."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_ideas.json"
            # Pre-seed local cache
            cached_item = IdeaItem(
                idea_id="CACHED-1",
                title="Existing Cached Idea",
                description="Viewable in read-only",
                status=STATUS_PENDING,
            )
            f.write_text(json.dumps([cached_item.to_dict()]), encoding="utf-8")

            # Initialize with no drive service
            store = IdeasStore(drive_service=None, local_file_path=str(f))
            self.assertTrue(store.is_degraded)
            # Viewable
            self.assertEqual(len(store.list_ideas()), 1)
            self.assertEqual(store.get_by_id("CACHED-1").title, "Existing Cached Idea")

            # Mutation rejected: add_idea
            new_idea = IdeaItem(idea_id="NEW-1", title="New Mutation", description="")
            with self.assertRaises(RuntimeError) as ctx:
                store.add_idea(new_idea)
            self.assertIn("read-only", str(ctx.exception))

            # Mutation rejected: confirm_idea
            with self.assertRaises(RuntimeError) as ctx:
                store.confirm_idea("CACHED-1")
            self.assertIn("read-only", str(ctx.exception))

            # Mutation rejected: convert_idea
            with self.assertRaises(RuntimeError) as ctx:
                store.convert_idea("CACHED-1", project_id="ai-development-manager")
            self.assertIn("read-only", str(ctx.exception))

            # Mutation rejected: drop_idea
            with self.assertRaises(RuntimeError) as ctx:
                store.drop_idea("CACHED-1", drop_reason="R", drop_problem="P")
            self.assertIn("read-only", str(ctx.exception))

            # Mutation rejected: restore_idea
            with self.assertRaises(RuntimeError) as ctx:
                store.restore_idea("CACHED-1")
            self.assertIn("read-only", str(ctx.exception))

    def test_drive_project_migration_moves_file_without_duplicates(self):
        """Converting an Idea from Unassigned to a project updates/moves the Drive file without orphan duplicates."""
        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files

        # Mock folder resolution
        def mock_list_side_effect(**kwargs):
            q = kwargs.get("q", "")
            m = MagicMock()
            if "name='IDEAS'" in q:
                m.execute.return_value = {"files": [{"id": "root-ideas"}]}
            elif "name='Unassigned'" in q:
                m.execute.return_value = {"files": [{"id": "folder-unassigned"}]}
            elif "name='ai-development-manager'" in q:
                m.execute.return_value = {"files": [{"id": "folder-adm"}]}
            elif "name='IDEA-MIGRATE.json'" in q:
                # File exists in Unassigned folder
                m.execute.return_value = {"files": [{"id": "file-idea-migrate", "parents": ["folder-unassigned"]}]}
            else:
                m.execute.return_value = {"files": []}
            return m

        mock_files.list.side_effect = mock_list_side_effect

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ideas_cache.json"
            store = IdeasStore(drive_service=mock_drive, local_file_path=str(f))
            self.assertFalse(store.is_degraded)

            idea = IdeaItem(idea_id="IDEA-MIGRATE", title="Migrating Idea", description="", status=STATUS_PENDING, project_id="Unassigned")
            store._ideas = [idea]

            # Convert to formal project
            store.convert_idea("IDEA-MIGRATE", project_id="ai-development-manager", milestone_id="M1")

            # Check files_client.update was called with addParents='folder-adm' and removeParents='folder-unassigned'
            mock_files.update.assert_called()
            update_calls = mock_files.update.call_args_list
            latest_call_kwargs = update_calls[-1].kwargs
            self.assertEqual(latest_call_kwargs.get("fileId"), "file-idea-migrate")
            self.assertEqual(latest_call_kwargs.get("addParents"), "folder-adm")
            self.assertEqual(latest_call_kwargs.get("removeParents"), "folder-unassigned")

    def test_duplicate_records_detected_on_load_and_reported(self):
        """When multiple folders in Drive contain the same idea_id, detect and report duplicate without crash."""
        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files

        def mock_list_side_effect(**kwargs):
            q = kwargs.get("q", "")
            m = MagicMock()
            if "name='IDEAS'" in q:
                m.execute.return_value = {"files": [{"id": "root-ideas"}]}
            elif "'root-ideas' in parents" in q:
                m.execute.return_value = {"files": [{"id": "f-unassigned", "name": "Unassigned"}, {"id": "f-adm", "name": "ai-development-manager"}]}
            elif "'f-unassigned' in parents" in q:
                m.execute.return_value = {"files": [{"id": "file-1", "name": "IDEA-DUP.json"}]}
            elif "'f-adm' in parents" in q:
                m.execute.return_value = {"files": [{"id": "file-2", "name": "IDEA-DUP.json"}]}
            else:
                m.execute.return_value = {"files": []}
            return m

        mock_files.list.side_effect = mock_list_side_effect

        doc_unassigned = json.dumps({
            "idea_id": "IDEA-DUP",
            "title": "Dup Title",
            "description": "",
            "status": "pending",
            "priority": "medium",
            "project_id": "Unassigned",
            "created_at": "2026-08-21T00:00:00Z",
            "source": "Chat",
            "decision_note": "",
        }).encode("utf-8")

        doc_adm = json.dumps({
            "idea_id": "IDEA-DUP",
            "title": "Dup Title",
            "description": "",
            "status": "converted",
            "priority": "medium",
            "project_id": "ai-development-manager",
            "created_at": "2026-08-21T00:00:00Z",
            "source": "Chat",
            "decision_note": "Converted",
        }).encode("utf-8")

        def mock_get_media_side_effect(**kwargs):
            file_id = kwargs.get("fileId")
            m = MagicMock()
            if file_id == "file-1":
                m.execute.return_value = doc_unassigned
            else:
                m.execute.return_value = doc_adm
            return m

        mock_files.get_media.side_effect = mock_get_media_side_effect

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ideas_cache.json"
            store = IdeasStore(drive_service=mock_drive, local_file_path=str(f))
            self.assertFalse(store.is_degraded)
            # Only one idea loaded
            self.assertEqual(len(store.list_ideas()), 1)
            # Resolved deterministically to the converted one
            loaded = store.get_by_id("IDEA-DUP")
            self.assertEqual(loaded.project_id, "ai-development-manager")
            self.assertEqual(loaded.status, STATUS_CONVERTED)
            # Consistency warning recorded
            self.assertIsNotNone(store.last_error)
            self.assertIn("Duplicate idea records detected", store.last_error)


if __name__ == "__main__":
    unittest.main()
