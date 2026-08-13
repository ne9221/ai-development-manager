import re
import unittest
from copy import deepcopy
from unittest.mock import patch

from manager.tasks import DriveRecords, TaskError, complete_task, create_handoff, create_project, create_task, logical_record_id, record_storage_id, update_task, validate


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document):
        self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name): return deepcopy(self.records[(area, project, name)])
    def latest(self, area, project, task):
        items = [value for (a, p, _), value in self.records.items() if a == area and p == project and value.get("task_id") == task]
        return max(items, key=lambda item: item["created_at"])


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value() if callable(self.value) else self.value


class FakeDriveFiles:
    def __init__(self): self.items = {}; self.next_id = 1
    def list(self, q, **_kwargs):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        name_match = re.search(r" and name='([^']*)'", q)
        name = name_match.group(1) if name_match else None
        def result():
            values = [deepcopy(item["meta"]) for item in self.items.values() if parent in item["meta"].get("parents", []) and (name is None or item["meta"]["name"] == name)]
            return {"files": values}
        return Request(result)
    def create(self, body, media_body=None, **_kwargs):
        def result():
            file_id = f"file-{self.next_id}"; self.next_id += 1
            meta = dict(body, id=file_id)
            raw = media_body.getbytes(0, media_body.size()) if media_body else b""
            self.items[file_id] = {"meta": meta, "raw": raw}
            return {"id": file_id}
        return Request(result)
    def update(self, fileId, body, media_body, **_kwargs):
        def result():
            self.items[fileId]["meta"].update(body)
            self.items[fileId]["raw"] = media_body.getbytes(0, media_body.size())
            return {"id": fileId}
        return Request(result)
    def get_media(self, fileId): return Request(lambda: self.items[fileId]["raw"])
    def delete(self, fileId): return Request(lambda: self.items.pop(fileId) and {})


class FakeDriveService:
    def __init__(self): self.transport = FakeDriveFiles()
    def files(self): return self.transport


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

    def test_project_governance_fields_match_drive_ssot_and_remain_strict(self):
        project = {
            "project_id": "ai-development-manager", "name": "AI Development Manager",
            "repo": "https://github.com/ne9221/ai-development-manager", "default_branch": "main",
            "runtime_ssot": "Google Drive/AI Development Manager", "project_rules": [],
            "active_tasks": [], "current_phase": "Phase 3C", "important_constraints": [],
            "updated_at": "2026-08-13T00:31:00+08:00",
            "priority_roadmap": [{"priority": "P0", "order": 1, "id": "task-centric-foundation",
                                  "title": "Task-centric foundation", "goal": "Make tasks the primary unit."}],
            "task_management_policy": {
                "relationship": "Project -> Task -> Execution -> Session", "primary_unit": "task",
                "conversation_role": "metadata_only", "default_task_minutes_max": 20,
                "quota_read_required_before_dispatch": True,
                "parallel_write_requires_isolated_worktree": True,
                "execution_loop": ["compile context", "launch provider", "persist terminal state"],
                "human_intervention_only_when": ["requirements conflict"],
            },
            "reference_architecture": [
                {"project": "Agent Orchestrator", "use_for": ["worktree isolation"]},
                {"project": "CAS", "use_for": ["context model"],
                 "storage_policy": "reference only; do not copy local-first SSOT"},
            ],
        }
        validate("project", project)

        malformed = []
        unexpected = deepcopy(project); unexpected["unexpected"] = True; malformed.append(unexpected)
        roadmap = deepcopy(project); roadmap["priority_roadmap"][0]["order"] = "first"; malformed.append(roadmap)
        policy = deepcopy(project); policy["task_management_policy"]["quota_read_required_before_dispatch"] = "yes"; malformed.append(policy)
        architecture = deepcopy(project); architecture["reference_architecture"][0]["unknown"] = True; malformed.append(architecture)
        for record in malformed:
            with self.assertRaises(TaskError):
                validate("project", record)

    def test_drive_records_round_trip_logical_ids_without_filename_collisions(self):
        service = FakeDriveService(); store = DriveRecords(service)
        values = ["abc123", "claude:abc123", "codex:abc123", "abc:def", "abc%3Adef", "Unicode-é", "中文", "a/b", "a\\b", " spaced value "]
        stems = [record_storage_id(value) for value in values]
        self.assertEqual(len(stems), len(set(stems)))
        self.assertEqual(values, [logical_record_id(value) for value in stems])
        self.assertEqual("abc123", stems[0])
        self.assertTrue(all(not re.search(r"[\\/ ]", stem) for stem in stems))
        for index, value in enumerate(values):
            document = {"logical_id": value, "index": index}
            self.assertEqual(document, store.put("sessions", "project-a", value, document))
            self.assertEqual(document, store.get("sessions", "project-a", value))
        self.assertEqual(values, [item["logical_id"] for item in store.list_records("sessions", "project-a")])
        for value in values:
            self.assertTrue(store.delete("sessions", "project-a", value))
        self.assertEqual([], store.list_records("sessions", "project-a"))

    def test_drive_conditional_delete_preserves_replacement(self):
        service = FakeDriveService(); store = DriveRecords(service)
        logical_id = "claude:abc123"
        store.put("sessions", "project-a", logical_id, {"version": 1})
        _, token = store.get_with_token("sessions", "project-a", logical_id)
        store.put("sessions", "project-a", logical_id, {"version": 2})
        self.assertFalse(store.delete("sessions", "project-a", logical_id, expected=token))
        self.assertEqual({"version": 2}, store.get("sessions", "project-a", logical_id))

        _, token = store.get_with_token("sessions", "project-a", logical_id)
        parent = store.project_folder("sessions", "project-a", create=False)
        filename = store.record_filename(logical_id)
        old_id = store.children(parent, filename)[0]["id"]
        service.transport.items.pop(old_id)
        store.put("sessions", "project-a", logical_id, {"version": 3})
        self.assertFalse(store.delete("sessions", "project-a", logical_id, expected=token))
        self.assertEqual({"version": 3}, store.get("sessions", "project-a", logical_id))


if __name__ == "__main__": unittest.main()
