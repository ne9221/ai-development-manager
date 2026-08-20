import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from streamlit.testing.v1 import AppTest

from manager.ideas import (
    STATUS_CONFIRMED,
    STATUS_CONVERTED,
    STATUS_DROPPED,
    STATUS_PENDING,
    IdeaItem,
    group_ideas_by_status,
    get_ideas_summary,
)


class TestDashboardP1ARequirements(unittest.TestCase):
    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_overview_ia_and_truth_contract_health(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_build_service.return_value = None  # Drive unavailable
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = [
            {"project_id": "ai-development-manager", "title": "AI Development Manager"}
        ]

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)
        self.assertFalse(at.exception, f"App crashed on Overview: {at.exception}")

        # Check title
        title_texts = [el.value for el in at.title]
        self.assertTrue(any("Operations Overview" in t for t in title_texts))

        # Check sidebar navigation radio
        self.assertEqual(len(at.sidebar.radio), 1)
        radio = at.sidebar.radio[0]
        self.assertIn("Overview", radio.options)
        self.assertIn("Ideas", radio.options)
        self.assertIn("Projects", radio.options)
        self.assertIn("Tasks", radio.options)
        self.assertIn("Quota", radio.options)

        # Check Ideas button on Overview
        ideas_btn = [b for b in at.button if "Ideas" in b.label or "灵感" in b.label]
        self.assertTrue(len(ideas_btn) >= 1)

        # Verify truth contract: Drive SSOT was mocked as None, so it must display OFFLINE / LOCAL FALLBACK
        # and NOT pretend to be ONLINE green
        markdown_texts = [el.value for el in at.markdown]
        self.assertTrue(any("LOCAL FALLBACK" in m or "UNAVAILABLE" in m for m in markdown_texts))

    @patch("manager.tasks.DriveRecords")
    @patch("manager.quota_reader.read_drive_status")
    @patch("collectors.publish_drive.build_service")
    def test_ideas_page_empty_state_and_four_categories(self, mock_build_service, mock_read_drive_status, mock_drive_records):
        mock_read_drive_status.return_value = {"providers": []}
        mock_store = mock_drive_records.return_value
        mock_store.list_projects.return_value = []

        at = AppTest.from_file("../dashboard.py")
        at.run(timeout=30)
        
        # Switch navigation to Ideas page
        at.sidebar.radio[0].set_value("Ideas")
        at.run(timeout=30)
        self.assertFalse(at.exception, f"App crashed on Ideas page: {at.exception}")

        title_texts = [el.value for el in at.title]
        self.assertTrue(any("Ideas" in t or "灵感" in t for t in title_texts))

        # Check that expanders for the 4 categories exist
        expander_labels = [el.label for el in at.expander]
        self.assertTrue(any("▼ 待立案" in l for l in expander_labels))
        self.assertTrue(any("▼ 已确认" in l for l in expander_labels))
        self.assertTrue(any("▶ 已立案" in l for l in expander_labels))
        self.assertTrue(any("▶ 已放弃" in l for l in expander_labels))

        # Check that empty state message exists (no fake ideas injected)
        info_texts = [el.value for el in at.info]
        self.assertTrue(any("0 Ideas" in i or "暂无" in i for i in info_texts))

    def test_idea_ssot_separation_contract(self):
        """Verify converted idea delegates execution progress solely to Project/Task SSOT."""
        converted_idea = IdeaItem(
            idea_id="IDEA-006",
            title="Windows Native Launcher",
            description="System tray launcher",
            status=STATUS_CONVERTED,
            project_id="ai-development-manager",
            milestone_id="M1",
            task_id="adm-windows-launcher-p0",
            converted_at="2026-08-21T01:37:00Z",
        )
        d = converted_idea.to_dict()
        self.assertNotIn("progress_percent", d)
        self.assertNotIn("execution_progress", d)
        self.assertEqual(d["status"], "converted")
        self.assertEqual(d["project_id"], "ai-development-manager")


if __name__ == "__main__":
    unittest.main()
