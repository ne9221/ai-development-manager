import json
import tempfile
import unittest
from pathlib import Path

from manager.ideas import (
    STATUS_CONFIRMED,
    STATUS_CONVERTED,
    STATUS_DROPPED,
    STATUS_PENDING,
    IdeaItem,
    IdeasStore,
    get_ideas_summary,
    get_sample_ideas,
    group_ideas_by_status,
)


class IdeasModelTests(unittest.TestCase):
    def test_idea_item_roundtrip(self):
        item = IdeaItem(
            idea_id="IDEA-101",
            title="Test Idea",
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

    def test_ideas_store_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_ideas.json"
            store = IdeasStore(str(f))
            self.assertTrue(len(store.list_ideas()) > 0)  # Loaded sample default

            new_idea = IdeaItem(
                idea_id="CUSTOM-1",
                title="Custom Stored Idea",
                description="Persistent description",
                status=STATUS_PENDING,
            )
            store.add_idea(new_idea)

            # Re-read from disk
            store2 = IdeasStore(str(f))
            fetched = store2.get_by_id("CUSTOM-1")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.title, "Custom Stored Idea")


if __name__ == "__main__":
    unittest.main()
