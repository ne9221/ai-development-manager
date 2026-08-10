import unittest

from manager.context_pack import context_pack
from manager.tasks import TaskError


PROJECT = {"project_id": "ai-development-manager", "name": "AI Development Manager", "aliases": ["adm"], "repo": "https://github.com/ne9221/ai-development-manager", "default_branch": "main", "working_directory": "C:/adm", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": ["task-1"], "current_phase": "4", "important_constraints": []}
TASK = {"task_id": "task-1", "project_id": "ai-development-manager", "title": "Session organizer", "status": "in_progress", "priority": "normal", "created_at": "2026-08-01T00:00:00Z", "updated_at": "2026-08-10T00:00:00Z", "task_type": "implementation", "expected_minutes": 20, "scope": ["manager"], "constraints": [], "acceptance_criteria": ["Tests pass"], "recommended_provider": "codex", "assigned_provider": "codex", "mode": "code", "effort": "medium", "depends_on": [], "blocked_reason": None, "source_context": {}, "current_progress": "Review queue complete", "next_action": "Run the context pack"}
HANDOFF = {"handoff_id": "handoff-1", "from_provider": "codex", "from_session": "s1", "reason": "continue", "current_state": "Tests ready", "next_action": "Run tests", "minimal_context": "Keep changes small.", "tests": ["unit"], "known_issues": []}


def session(session_id, project_id, updated_at):
    return {"session_id": session_id, "provider": "codex", "project_id": project_id, "task_id": None, "title": "Title", "conversation_label": None, "first_user_prompt": "Short prompt", "classification_method": "unclassified", "classification_status": "needs_review", "started_at": "2026-08-01T00:00:00Z", "updated_at": updated_at, "source_identifier": f"sessions/{session_id}.jsonl"}


class Store:
    def __init__(self):
        self.direct = [session("direct", "ai-development-manager", "2026-08-09T00:00:00Z")]
        self.unclassified = [session("manual", None, "2026-08-10T00:00:00Z"), session("direct", None, "2026-08-09T00:00:00Z")]
        self.reviews = [{"session_id": "manual", "provider": "codex", "project_id": "ai-development-manager", "classification_method": "manual_review", "classification_status": "classified", "source_identifier": "sessions/manual.jsonl", "assigned_at": "2026-08-10T00:00:00Z", "assignment_history": [{"previous_project_id": None, "new_project_id": "ai-development-manager", "assigned_at": "2026-08-10T00:00:00Z"}]}]
        self.overview = {"project_id": "ai-development-manager", "version": "1.0", "updated_at": "2026-08-10T00:00:00Z", "items": [{"item_id": f"P{index:02d}", "title": f"Item {index}", "status": "in_progress", "priority": "high", "current_progress": "Progress", "next_action": "Next", "task_ids": [], "merged_into": None, "notes": []} for index in range(6)]}
    def list_projects(self): return [PROJECT]
    def get(self, area, project_id, name):
        if area == "projects" and name == "ai-development-manager": return PROJECT
        if area == "tasks" and name == "task-1": return TASK
        if area == "overviews" and name == "overview": return self.overview
        raise TaskError(name)
    def latest(self, area, project_id, task_id): return HANDOFF
    def list_records(self, area, project_id):
        if area == "sessions" and project_id == "ai-development-manager": return self.direct
        if area == "sessions" and project_id == "_unclassified": return self.unclassified
        if area == "session_reviews": return self.reviews
        return []


class ContextPackTests(unittest.TestCase):
    def test_resumes_task_handoff_and_all_rules(self):
        pack = context_pack(Store(), "adm", "task-1", "continue")
        self.assertEqual("Review queue complete", pack["active_task"]["current_progress"])
        self.assertEqual("Run tests", pack["latest_handoff"]["next_action"])
        self.assertGreaterEqual(len(pack["shared_rules"]), 12)
        self.assertIn("Resume from", pack["continuation_instruction"])

    def test_manual_overlay_dedupes_and_bounds_sessions(self):
        store = Store()
        for index in range(8): store.direct.append(session(f"extra-{index}", "ai-development-manager", f"2026-08-{index + 1:02d}T00:00:00Z"))
        pack = context_pack(store, "ai-development-manager")
        ids = [item["session_id"] for item in pack["recent_sessions"]]
        self.assertEqual(5, len(ids))
        self.assertIn("manual", ids)
        self.assertEqual(1, ids.count("direct"))
        self.assertEqual("manual_review", next(item for item in pack["recent_sessions"] if item["session_id"] == "manual")["classification_method"])

    def test_overview_focus_is_bounded(self):
        pack = context_pack(Store(), "ai-development-manager")
        self.assertEqual(5, len(pack["overview_focus"]))
        self.assertEqual("P00", pack["overview_focus"][0]["item_id"])

    def test_missing_overview_is_backward_compatible(self):
        store = Store()
        store.overview = None
        original_get = store.get
        def missing_overview(area, project_id, name):
            if area == "overviews": raise TaskError(name)
            return original_get(area, project_id, name)
        store.get = missing_overview
        self.assertEqual([], context_pack(store, "ai-development-manager")["overview_focus"])


if __name__ == "__main__": unittest.main()
