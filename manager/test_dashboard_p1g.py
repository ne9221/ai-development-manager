import unittest
from pathlib import Path

from manager.dashboard_core import build_session_center_health
from manager.runtime_visibility import determine_ai_runtime_activity


class DashboardP1GTruthAndLayoutTests(unittest.TestCase):
    def test_correlation_failed_is_error_not_healthy(self):
        state, badge, _ = determine_ai_runtime_activity({"status": "CORRELATION_FAILED"})
        self.assertEqual((state, badge), ("FAILED", "badge-err"))
        self.assertEqual(build_session_center_health(True, {"current_state": "CORRELATION_FAILED"}).status_label, "Offline")

    def test_dashboard_uses_zhtw_layout_and_does_not_infer_project_progress(self):
        source = Path(__file__).parents[1].joinpath("dashboard.py").read_text(encoding="utf-8")
        self.assertIn('NAV_OVERVIEW = "總覽"', source)
        self.assertIn('[data-testid="stSidebar"] { background: #101722', source)
        self.assertIn('.fleet-anchor', source)
        self.assertIn("未記錄 canonical 百分比；不以任務數推算", source)
        self.assertNotIn('prog_pct = int((len(completed_tasks)', source)


if __name__ == "__main__":
    unittest.main()
