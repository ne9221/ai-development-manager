import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from manager.ideas import (
    STATUS_CONFIRMED,
    STATUS_CONFLICTED,
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
            cached_item = IdeaItem(
                idea_id="CACHED-1",
                title="Existing Cached Idea",
                description="Viewable in read-only",
                status=STATUS_PENDING,
            )
            f.write_text(json.dumps([cached_item.to_dict()]), encoding="utf-8")

            store = IdeasStore(drive_service=None, local_file_path=str(f))
            self.assertTrue(store.is_degraded)
            self.assertEqual(len(store.list_ideas()), 1)
            self.assertEqual(store.get_by_id("CACHED-1").title, "Existing Cached Idea")

            new_idea = IdeaItem(idea_id="NEW-1", title="New Mutation", description="")
            with self.assertRaises(RuntimeError) as ctx:
                store.add_idea(new_idea)
            self.assertIn("read-only", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.confirm_idea("CACHED-1")
            self.assertIn("read-only", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.convert_idea("CACHED-1", project_id="ai-development-manager")
            self.assertIn("read-only", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.drop_idea("CACHED-1", drop_reason="R", drop_problem="P")
            self.assertIn("read-only", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.restore_idea("CACHED-1")
            self.assertIn("read-only", str(ctx.exception))

    def test_duplicate_records_fail_closed_no_guessing_and_block_mutations(self):
        """Duplicate records for same idea_id produce NO automatic winner, record error, and block mutations."""
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
                m.execute.return_value = {"files": [{"id": "file-1", "name": "IDEA-CONFLICT.json"}]}
            elif "'f-adm' in parents" in q:
                m.execute.return_value = {"files": [{"id": "file-2", "name": "IDEA-CONFLICT.json"}]}
            else:
                m.execute.return_value = {"files": []}
            return m

        mock_files.list.side_effect = mock_list_side_effect

        doc1 = json.dumps({"idea_id": "IDEA-CONFLICT", "title": "Copy 1 in Unassigned", "description": "", "status": "pending", "project_id": "Unassigned"}).encode("utf-8")
        doc2 = json.dumps({"idea_id": "IDEA-CONFLICT", "title": "Copy 2 in ADM", "description": "", "status": "converted", "project_id": "ai-development-manager"}).encode("utf-8")

        def mock_get_media_side_effect(**kwargs):
            file_id = kwargs.get("fileId")
            m = MagicMock()
            m.execute.return_value = doc1 if file_id == "file-1" else doc2
            return m

        mock_files.get_media.side_effect = mock_get_media_side_effect

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ideas_cache.json"
            store = IdeasStore(drive_service=mock_drive, local_file_path=str(f))
            self.assertFalse(store.is_degraded)

            # Neither copy is picked as winner; item is marked CONFLICTED
            conflicted_item = store.get_by_id("IDEA-CONFLICT")
            self.assertIsNotNone(conflicted_item)
            self.assertEqual(conflicted_item.status, STATUS_CONFLICTED)
            self.assertTrue(conflicted_item.is_conflicted)
            self.assertIn("IDEA-CONFLICT", store.conflicted_idea_ids)

            # Explicit error message emitted
            self.assertIsNotNone(store.last_error)
            self.assertIn("SSOT Consistency Error", store.last_error)
            self.assertIn("file-1", store.last_error)
            self.assertIn("file-2", store.last_error)

            # All mutations on this conflicted idea MUST be blocked
            with self.assertRaises(RuntimeError) as ctx:
                store.confirm_idea("IDEA-CONFLICT")
            self.assertIn("conflicted state", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.convert_idea("IDEA-CONFLICT", project_id="ai-development-manager")
            self.assertIn("conflicted state", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.drop_idea("IDEA-CONFLICT", drop_reason="R", drop_problem="P")
            self.assertIn("conflicted state", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.restore_idea("IDEA-CONFLICT")
            self.assertIn("conflicted state", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx:
                store.update_idea(conflicted_item)
            self.assertIn("conflicted state", str(ctx.exception))

    def test_search_strictly_scoped_to_ideas_root_and_leaves_unrelated_files_alone(self):
        """Search and write operations strictly scope within IDEAS root and never touch unrelated files elsewhere in Drive."""
        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files

        # Unrelated file outside IDEAS hierarchy (e.g. in some other folder)
        unrelated_file = {"id": "unrelated-file-id", "name": "IDEA-SCOPE.json", "parents": ["other-folder-root"]}

        def mock_list_side_effect(**kwargs):
            q = kwargs.get("q", "")
            m = MagicMock()
            if "name='IDEAS'" in q:
                m.execute.return_value = {"files": [{"id": "root-ideas"}]}
            elif "'root-ideas' in parents" in q:
                m.execute.return_value = {"files": [{"id": "folder-unassigned", "name": "Unassigned"}]}
            elif "'folder-unassigned' in parents" in q and "name='IDEA-SCOPE.json'" in q:
                # Target file inside IDEAS/Unassigned
                m.execute.return_value = {"files": [{"id": "file-inside-ideas", "name": "IDEA-SCOPE.json", "parents": ["folder-unassigned"]}]}
            else:
                m.execute.return_value = {"files": []}
            return m

        mock_files.list.side_effect = mock_list_side_effect

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ideas_cache.json"
            store = IdeasStore(drive_service=mock_drive, local_file_path=str(f))
            self.assertFalse(store.is_degraded)

            idea = IdeaItem(idea_id="IDEA-SCOPE", title="Scoped Idea", description="", status=STATUS_PENDING, project_id="Unassigned")
            store._ideas = [idea]

            # Convert within IDEAS
            # Mock folder resolution for new project folder
            def mock_list_side_effect2(**kwargs):
                q = kwargs.get("q", "")
                m = MagicMock()
                if "name='IDEAS'" in q:
                    m.execute.return_value = {"files": [{"id": "root-ideas"}]}
                elif "'root-ideas' in parents" in q:
                    m.execute.return_value = {"files": [{"id": "folder-unassigned", "name": "Unassigned"}, {"id": "folder-adm", "name": "ai-development-manager"}]}
                elif "'folder-unassigned' in parents" in q and "name='IDEA-SCOPE.json'" in q:
                    m.execute.return_value = {"files": [{"id": "file-inside-ideas", "name": "IDEA-SCOPE.json", "parents": ["folder-unassigned"]}]}
                elif "'folder-adm' in parents" in q and "name='IDEA-SCOPE.json'" in q:
                    m.execute.return_value = {"files": []}
                elif "name='ai-development-manager'" in q:
                    m.execute.return_value = {"files": [{"id": "folder-adm"}]}
                else:
                    m.execute.return_value = {"files": []}
                return m

            mock_files.list.side_effect = mock_list_side_effect2

            store.convert_idea("IDEA-SCOPE", project_id="ai-development-manager")

            # Verify update was called specifically on file-inside-ideas and NOT on unrelated-file-id
            mock_files.update.assert_called()
            for call in mock_files.update.call_args_list:
                self.assertEqual(call.kwargs.get("fileId"), "file-inside-ideas")
                self.assertNotEqual(call.kwargs.get("fileId"), "unrelated-file-id")

            # Verify delete was never called on unrelated-file-id
            for call in mock_files.delete.call_args_list:
                self.assertNotEqual(call.kwargs.get("fileId"), "unrelated-file-id")

    def test_duplicate_cleanup_failure_is_not_silent(self):
        """If deleting an obsolete duplicate file inside IDEAS fails, raise RuntimeError and surface error."""
        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files

        doc = json.dumps({
            "idea_id": "IDEA-CLEANUP",
            "title": "Cleanup Fail Idea",
            "description": "",
            "status": "pending",
            "project_id": "Unassigned"
        }).encode("utf-8")

        def mock_list_side_effect(**kwargs):
            q = kwargs.get("q", "")
            m = MagicMock()
            if "name='IDEAS'" in q:
                m.execute.return_value = {"files": [{"id": "root-ideas"}]}
            elif "'root-ideas' in parents" in q:
                m.execute.return_value = {"files": [{"id": "folder-unassigned", "name": "Unassigned"}]}
            elif "'folder-unassigned' in parents" in q and "name='IDEA-CLEANUP.json'" in q:
                # Two duplicate files matching this idea_id inside IDEAS
                m.execute.return_value = {
                    "files": [
                        {"id": "file-primary", "name": "IDEA-CLEANUP.json", "parents": ["folder-unassigned"]},
                        {"id": "file-obsolete-dup", "name": "IDEA-CLEANUP.json", "parents": ["folder-unassigned"]}
                    ]
                }
            elif "name='Unassigned'" in q:
                m.execute.return_value = {"files": [{"id": "folder-unassigned"}]}
            else:
                m.execute.return_value = {"files": []}
            return m

        mock_files.list.side_effect = mock_list_side_effect
        mock_files.get_media.return_value.execute.return_value = doc
        mock_files.delete.side_effect = Exception("Google Drive 403 Insufficient Permission")

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ideas_cache.json"
            store = IdeasStore(drive_service=mock_drive, local_file_path=str(f))
            self.assertFalse(store.is_degraded)

            idea = IdeaItem(idea_id="IDEA-CLEANUP", title="Cleanup Fail Idea", description="", status=STATUS_PENDING)
            store._ideas = [idea]
            store.conflicted_idea_ids = set()

            with self.assertRaises(RuntimeError) as ctx:
                store.confirm_idea("IDEA-CLEANUP")
            self.assertIn("Failed to clean up obsolete duplicate Idea file", str(ctx.exception))
            self.assertIsNotNone(store.last_error)
            self.assertIn("Failed to clean up obsolete duplicate", store.last_error)


if __name__ == "__main__":
    unittest.main()
