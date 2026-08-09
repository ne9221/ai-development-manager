import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from manager.runtime_bridge import human_summary, resolve_project, runtime_bridge
from manager.tasks import TaskError, create_handoff, create_project, create_task


NOW = datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document): self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name):
        if (area, project, name) not in self.records: raise TaskError("not found")
        return deepcopy(self.records[(area, project, name)])
    def latest(self, area, project, task_id):
        items = [value for (a, p, _), value in self.records.items() if a == area and p == project and value.get("task_id") == task_id]
        if not items: raise TaskError("no handoff")
        return max(items, key=lambda item: item["created_at"])
    def list_projects(self): return [deepcopy(value) for (area, _, _), value in self.records.items() if area == "projects"]


def project(active=None):
    return {"project_id": "adm", "name": "AI Development Manager", "aliases": ["ADM", "开发管理器", "development manager"], "repo": "https://github.com/example/adm", "default_branch": "main", "working_directory": "C:/adm", "baseline_commit": "abc", "runtime_ssot": "Drive", "project_rules": ["Project facts win within project scope"], "active_tasks": active or [], "current_phase": "9", "important_constraints": ["Do not touch other repos"], "execution_policies": ["ponytail"]}


def task(task_id="t1", **changes):
    value = {"task_id": task_id, "project_id": "adm", "title": "Continue bridge", "status": "ready", "task_type": "implementation", "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True, "scope": ["manager/runtime_bridge.py"], "constraints": ["Task scope is narrow"], "acceptance_criteria": ["Tests pass"], "depends_on": [], "source_context": {}, "current_progress": "Bridge skeleton done", "next_action": "Add contract tests"}
    value.update(changes); return value


def quota(codex=80, claude=None, updated=NOW):
    providers = []
    for name, remaining in (("codex", codex), ("claude", claude), ("antigravity", None), ("gemini_app", None)):
        windows = [] if remaining is None else [{"name": "primary", "remaining_percent": remaining, "used_percent": 100-remaining, "resets_at": None}]
        providers.append({"provider": name, "display_name": name, "collection_mode": "automatic" if name in ("codex", "claude") else "manual", "source": "test", "source_type": "official" if name in ("codex", "claude") else "manual", "confidence": "official" if windows else "unknown", "last_updated": updated.isoformat(), "status": "ok" if windows else "unknown", "windows": windows})
    return {"schema_version": "0.1.0", "generated_at": updated.isoformat(), "providers": providers}


class RuntimeBridgeTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore(); create_project(self.store, project())

    def call(self, request, q=None): return runtime_bridge(self.store, object(), request, q or quota(), [])

    def test_alias_and_explicit_project_resolution(self):
        self.assertEqual("adm", resolve_project(self.store, "adm")["project_id"])
        self.assertEqual("adm", resolve_project(self.store, "开发管理器")["project_id"])
        self.assertEqual("adm", resolve_project(self.store, None, "请让 ADM 处理") ["project_id"])

    def test_new_task_current_quota_provider_alternatives_and_json(self):
        result = self.call({"project_id": "ADM", "user_request": "Implement runtime bridge", "task_type": "implementation", "complexity": "medium"})
        self.assertEqual("new_task", result["request_type"]); self.assertEqual("codex", result["recommended_provider"])
        self.assertTrue(result["alternatives"]); self.assertEqual("fresh", result["quota_freshness"]); self.assertIn("80% remaining", result["quota_summary"])
        self.assertIsInstance(json.loads(json.dumps(result)), dict); self.assertIn("推荐：codex", human_summary(result))

    def test_continuation_task_handoff_and_no_handoff(self):
        create_task(self.store, task(), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = ["t1"]
        no_handoff = self.call({"project_id": "adm", "task_id": "t1", "user_request": "继续"})
        self.assertEqual("continuation", no_handoff["request_type"]); self.assertIsNone(no_handoff["latest_handoff_summary"]); self.assertEqual("Add contract tests", no_handoff["next_action"])
        create_handoff(self.store, {"handoff_id": "h1", "task_id": "t1", "project_id": "adm", "from_provider": "codex", "to_provider": "claude", "from_session": "a", "reason": "switch", "completed_work": [], "current_state": "Parser done", "files_changed": [], "commits": [], "tests": [], "known_issues": [], "do_not_touch": ["quota collectors"], "next_action": "Add tests", "acceptance_criteria": [], "minimal_context": "Continue from bridge skeleton. token=sensitive"})
        continued = self.call({"project_id": "adm", "user_request": "Continue bridge"})
        self.assertEqual("h1", continued["latest_handoff_summary"]["handoff_id"]); self.assertIn("Continue from bridge skeleton", continued["generated_prompt"])
        self.assertNotIn("sensitive", json.dumps(continued))

    def test_status_split_stale_unknown_and_safety(self):
        create_task(self.store, task(expected_minutes=45, constraints=["token=sensitive", "Task scope is narrow"]), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = ["t1"]
        result = self.call({"project_id": "adm", "task_id": "t1", "user_request": "进度状态"}, quota(updated=NOW-timedelta(hours=2)))
        self.assertEqual("status", result["request_type"]); self.assertTrue(result["split_recommended"]); self.assertEqual("stale", result["quota_freshness"]); self.assertTrue(result["warnings"])
        serialized = json.dumps(result); self.assertNotIn("sensitive", serialized); self.assertNotIn('"providers"', serialized)

    def test_shared_project_task_rule_priority_in_prompt(self):
        create_task(self.store, task(), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = ["t1"]
        prompt = self.call({"project_id": "adm", "task_id": "t1", "user_request": "继续"})["generated_prompt"]
        self.assertLess(prompt.index("Project business / acceptance:"), prompt.index("AI Development Manager scope / protection:")); self.assertLess(prompt.index("AI Development Manager scope / protection:"), prompt.index("Ponytail minimal-change preference"))
        self.assertIn("20 minutes", prompt); self.assertIn("Task scope is narrow", prompt); self.assertIn("smallest safe change", prompt)
        self.assertNotIn("## Persistence", prompt)

    def test_ponytail_research_and_unavailable_fallback(self):
        coding = self.call({"project_id": "adm", "user_request": "Fix regression safely", "task_type": "regression", "complexity": "medium", "ponytail_available": False})["generated_prompt"]
        self.assertIn("Ponytail skill is unavailable", coding); self.assertIn("smallest safe change", coding)
        self.store.records[("projects", "adm", "adm")]["active_tasks"] = []
        research = self.call({"project_id": "adm", "user_request": "Research architecture options", "task_type": "research", "complexity": "medium"})["generated_prompt"]
        self.assertNotIn("Ponytail minimal-change preference", research)

    def test_preferred_and_excluded(self):
        preferred = self.call({"project_id": "adm", "user_request": "Research design", "task_type": "research", "complexity": "medium", "preferred_provider": "claude"}, quota(50, None))
        self.assertEqual("claude", preferred["recommended_provider"]); self.assertTrue(preferred["warnings"])
        excluded = self.call({"project_id": "adm", "user_request": "Implement second bridge", "task_type": "implementation", "complexity": "medium", "excluded_provider": "codex"}, quota(90, 80))
        self.assertNotEqual("codex", excluded["recommended_provider"])

    def test_multi_task_reuses_scheduler(self):
        ready = create_task(self.store, task(), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = [ready["task_id"]]
        called = []
        def fake_schedule(store, service, project_id, tasks, raw, history, dispatch_func=None):
            called.append((project_id, len(tasks), bool(dispatch_func)))
            dispatcher_result = {"recommended_provider": "codex", "mode": "code", "effort": "medium", "estimated_minutes": 10, "split_recommended": False, "alternatives": ["claude"], "quota_summary": "primary: 80% remaining; official; fresh", "warnings": [], "generated_prompt": "scheduler reused prompt"}
            return {"execution_batches": [{"batch": 1, "tasks": [{"task_id": "t1", "recommended_provider": "codex", "dispatcher_result": dispatcher_result}]}], "deferred_tasks": [], "warnings": []}
        result = runtime_bridge(self.store, object(), {"project_id": "adm", "user_request": "安排全部任务", "multi_task": True}, quota(), [], schedule_func=fake_schedule)
        self.assertEqual("scheduling", result["request_type"]); self.assertEqual([("adm", 1, True)], called); self.assertEqual("scheduler reused prompt", result["generated_prompt"])

    def test_malformed_input(self):
        with self.assertRaises(TaskError): runtime_bridge(self.store, object(), {}, quota(), [])
        with self.assertRaises(TaskError): runtime_bridge(self.store, object(), {"project_id": "missing", "user_request": "work"}, quota(), [])


if __name__ == "__main__": unittest.main()
