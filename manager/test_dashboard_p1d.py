import unittest
from datetime import datetime, timezone

from manager.actions import ActionItem, STATUS_OPEN
from manager.dashboard_core import build_project_detail_vm
from manager.runtime_visibility import format_duration_and_remaining_eta, format_elapsed_duration


class ProjectDetailTruthTests(unittest.TestCase):
    def test_truthful_project_task_execution_model(self):
        project = {"project_id": "P1", "title": "Project", "current_phase": "Build", "priority_roadmap": ["P1-D"]}
        tasks = [
            {"project_id": "P1", "task_id": "ready-low", "status": "ready", "priority": "low", "created_at": "2026-08-21T02:00:00Z"},
            {"project_id": "P1", "task_id": "queued-high", "status": "queued", "priority": "high"},
            {"project_id": "P1", "task_id": "current", "status": "in_progress"},
            {"project_id": "P1", "task_id": "blocked", "status": "blocked"},
            {"project_id": "P1", "task_id": "done-old", "status": "completed", "completed_at": "2026-08-20T00:00:00Z"},
            {"project_id": "P1", "task_id": "done-new", "status": "completed", "completed_at": "2026-08-21T00:00:00Z"},
        ]
        exe = {"project_id": "P1", "task_id": "current", "execution_id": "E1", "provider": "codex", "provider_session_id": "S1", "status": "running"}
        action = ActionItem(action_id="A1", title="Blocked", project_id="P1", task_id="blocked", status=STATUS_OPEN)
        vm = build_project_detail_vm(project, tasks, [exe], [action], [{"project_id": "P1", "idea_id": "I1"}])
        self.assertEqual(vm["current_phase"], "Build")
        self.assertEqual(vm["priority_roadmap"], ["P1-D"])
        self.assertEqual(vm["task_completion"], (2, 6))
        self.assertEqual(vm["milestone_progress"], "Unavailable / Not recorded")
        self.assertEqual([t["task_id"] for t in vm["next"]], ["ready-low", "queued-high"])
        self.assertEqual(vm["completed"][0]["task_id"], "done-new")
        self.assertEqual(vm["executions"][0]["provider_session_id"], "S1")
        self.assertEqual(vm["actions"][0].action_id, "A1")
        self.assertEqual(len(vm["ideas"]), 1)

    def test_missing_truth_stays_unavailable(self):
        vm = build_project_detail_vm({"project_id": "P0"}, [], [], [], [{"project_id": "missing"}])
        self.assertIsNone(vm["task_completion"])
        self.assertEqual(vm["current_phase"], "Unavailable / Not recorded")
        self.assertEqual(vm["milestone_progress"], "Unavailable / Not recorded")
        self.assertEqual(len(vm["orphan_ideas"]), 1)
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        self.assertEqual(format_elapsed_duration(None, now), "Unknown")
        self.assertEqual(format_duration_and_remaining_eta(None, None, now), ("—", "—"))


if __name__ == "__main__":
    unittest.main()
