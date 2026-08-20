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

    def test_local_cache_persistence_and_degraded_flag(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_ideas.json"
            store = IdeasStore(drive_service=None, local_file_path=str(f))
            self.assertTrue(store.is_degraded)
            self.assertEqual(len(store.list_ideas()), 0)

            new_idea = IdeaItem(
                idea_id="CUSTOM-1",
                title="Real Local Idea",
                description="Persistent description",
                status=STATUS_PENDING,
            )
            store.add_idea(new_idea)

            # Re-read from disk
            store2 = IdeasStore(drive_service=None, local_file_path=str(f))
            fetched = store2.get_by_id("CUSTOM-1")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.title, "Real Local Idea")

    def test_drop_requires_reason_and_problem(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_ideas.json"
            store = IdeasStore(drive_service=None, local_file_path=str(f))
            idea = IdeaItem(idea_id="IDEA-D", title="Idea to Drop", description="", status=STATUS_PENDING)
            store.add_idea(idea)

            # Drop with empty reason must fail
            with self.assertRaises(ValueError):
                store.drop_idea("IDEA-D", drop_reason="", drop_problem="Technical blocker")

            # Drop with empty problem must fail
            with self.assertRaises(ValueError):
                store.drop_idea("IDEA-D", drop_reason="Deprioritized", drop_problem="  ")

            # Valid drop
            store.drop_idea("IDEA-D", drop_reason="Too complex", drop_problem="Lack of API support", note="Will re-evaluate in Q4")
            dropped = store.get_by_id("IDEA-D")
            self.assertEqual(dropped.status, STATUS_DROPPED)
            self.assertEqual(dropped.drop_reason, "Too complex")
            self.assertEqual(dropped.drop_problem, "Lack of API support")
            self.assertIsNotNone(dropped.dropped_at)

    def test_convert_requires_valid_project_id(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_ideas.json"
            store = IdeasStore(drive_service=None, local_file_path=str(f))
            idea = IdeaItem(idea_id="IDEA-C", title="Idea to Convert", description="", status=STATUS_CONFIRMED)
            store.add_idea(idea)

            # Cannot convert to Unassigned or empty
            with self.assertRaises(ValueError):
                store.convert_idea("IDEA-C", project_id="Unassigned")
            with self.assertRaises(ValueError):
                store.convert_idea("IDEA-C", project_id="")

            # Valid conversion
            store.convert_idea("IDEA-C", project_id="ai-development-manager", milestone_id="M1", task_id="T100")
            converted = store.get_by_id("IDEA-C")
            self.assertEqual(converted.status, STATUS_CONVERTED)
            self.assertEqual(converted.project_id, "ai-development-manager")
            self.assertEqual(converted.milestone_id, "M1")
            self.assertEqual(converted.task_id, "T100")
            self.assertIsNotNone(converted.converted_at)

    def test_drive_ssot_read_write_and_failure_not_silent(self):
        mock_drive = MagicMock()
        mock_files = MagicMock()
        mock_drive.files.return_value = mock_files

        def mock_list_side_effect(**kwargs):
            q = kwargs.get("q", "")
            m = MagicMock()
            if "name='IDEAS'" in q:
                m.execute.return_value = {"files": [{"id": "folder-ideas"}]}
            else:
                m.execute.return_value = {"files": []}
            return m

        mock_files.list.side_effect = mock_list_side_effect

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ideas_cache.json"
            store = IdeasStore(drive_service=mock_drive, local_file_path=str(f))
            self.assertFalse(store.is_degraded)

            # Mock create failure on Drive
            mock_files.create.side_effect = Exception("Google Drive API 503 Service Unavailable")

            idea = IdeaItem(idea_id="I-DRIVE", title="Drive Idea", description="")
            with self.assertRaises(RuntimeError) as ctx:
                store.add_idea(idea)
            self.assertIn("Drive SSOT", str(ctx.exception))
            self.assertIsNotNone(store.last_error)


if __name__ == "__main__":
    unittest.main()
