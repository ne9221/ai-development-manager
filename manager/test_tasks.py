import unittest
from copy import deepcopy
from unittest.mock import patch

from manager.tasks import TaskError, complete_task, create_handoff, create_project, create_task, update_task, validate


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document):
        self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name): return deepcopy(self.records[(area, project, name)])
    def latest(self, area, project, task):
        items = [value for (a, p, _), value in self.records.items() if a == area and p == project and value.get("task_id") == task]
        return max(items, key=lambda item: item["created_at"])


def task_input():
    return {
        "task_id": "phase-5", "project_id": "ai-development-manager", "title": "Phase 5",
        "task_type": "implementation", "expected_minutes": 20,
        "scope": ["TASKS and HANDOFFS"], "constraints": ["No automatic AI execution"],
        "acceptance_criteria": ["Drive round trip passes"], "source_context": {"phase": 5},
    }


def handoff_input(reason="provider switch"):
    return {
        "handoff_id": "phase-5-codex-claude", "task_id": "phase-5", "project_id": "ai-development-manager",
        "from_provider": "codex", "to_provider": "claude", "from_session": "session-a", "reason": reason,
        "completed_work": ["Schemas implemented"], "current_state": "Tests pending", "files_changed": ["schema/task.schema.json"],
        "commits": [], "tests": ["unit tests pending"], "known_issues": [], "do_not_touch": ["Phase 1-4.5 collectors"],
        "next_action": "Run Drive round trip", "acceptance_criteria": ["Drive round trip passes"],
        "minimal_context": "Phase 5 schemas and manager are implemented; validate Drive persistence next."
    }


class TaskTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore()

    def create(self):
        decision = {"recommended_provider": "codex", "recommended_mode": "code", "recommended_effort": "medium", "quota_evidence": {"codex": {"freshness": "fresh"}}}
        with patch("manager.tasks.read_drive_status", return_value={}), patch("manager.tasks.summarize", return_value={}), patch("manager.tasks.decide", return_value=decision):
            return create_task(self.store, task_input(), service=object())

    def test_create_assignment_update_and_block(self):
        task = self.create()
        self.assertEqual("codex", task["recommended_provider"])
        task = update_task(self.store, task["project_id"], task["task_id"], status="in_progress", current_progress="Schemas done", assigned_provider="codex")
        self.assertEqual("in_progress", task["status"])
        task = update_task(self.store, task["project_id"], task["task_id"], status="blocked", blocked_reason="Needs handoff")
        self.assertEqual("Needs handoff", task["blocked_reason"])

    def test_provider_and_session_handoffs_are_minimal(self):
        first_data = handoff_input(); first_data["created_at"] = "2026-08-09T00:00:00Z"
        first = create_handoff(self.store, first_data)
        second = handoff_input("session switch"); second.update(handoff_id="phase-5-session-b", from_provider="claude", to_provider="claude", from_session="session-a", created_at="2026-08-09T00:01:00Z")
        create_handoff(self.store, second)
        self.assertEqual("session switch", self.store.latest("handoffs", "ai-development-manager", "phase-5")["reason"])
        self.assertLess(len(first["minimal_context"]), 4000)
        self.assertNotIn("README", first["minimal_context"])

    def test_complete_preserves_task_history_and_final_handoff(self):
        self.create()
        task, handoff = complete_task(self.store, "ai-development-manager", "phase-5", "All acceptance criteria passed", "codex", "session-b")
        self.assertEqual("completed", task["status"])
        self.assertTrue(any(key[0] == "history" for key in self.store.records))
        self.assertEqual("completed", handoff["reason"])

    def test_malformed_records_rejected(self):
        with self.assertRaises(TaskError): validate("task", {"task_id": "broken"})
        broken = handoff_input(); broken["minimal_context"] = "x" * 4001
        with self.assertRaises(TaskError): create_handoff(self.store, broken)

    def test_project_record(self):
        project = {"project_id": "ai-development-manager", "name": "AI Development Manager", "repo": "https://github.com/ne9221/ai-development-manager", "default_branch": "main", "runtime_ssot": "Google Drive/AI Development Manager", "project_rules": ["Drive is runtime SSOT"], "active_tasks": ["phase-5"], "current_phase": "Phase 5", "important_constraints": ["Do not auto-start AI"]}
        self.assertEqual(project, create_project(self.store, project))


if __name__ == "__main__": unittest.main()
