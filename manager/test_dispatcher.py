import unittest
from copy import deepcopy

from manager.dispatcher import dispatch, request_ok
from manager.sessions import parse_identity_header
from manager.tasks import TaskError, create_handoff, create_project, create_task


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


def project(active=None):
    return {"project_id": "p1", "name": "Project One", "repo": "https://github.com/example/project", "default_branch": "main", "working_directory": "C:/work/project", "baseline_commit": "abc123", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": active or [], "current_phase": "Phase 7", "important_constraints": ["Do not touch other repos"]}


def quota(codex=80, claude=None, updated="2026-08-09T05:00:00Z"):
    providers = []
    for name, remaining in (("codex", codex), ("claude", claude), ("antigravity", None), ("gemini_app", None)):
        windows = [] if remaining is None else [{"name": "primary", "remaining_percent": remaining, "used_percent": 100-remaining, "resets_at": None}]
        providers.append({"provider": name, "display_name": name, "collection_mode": "automatic" if name in ("codex", "claude") else "manual", "source": "test", "source_type": "official" if name in ("codex", "claude") else "manual", "confidence": "official" if windows else "unknown", "last_updated": updated, "status": "ok" if windows else "unknown", "windows": windows})
    return {"schema_version": "0.1.0", "generated_at": updated, "providers": providers}


def request(**changes):
    value = {"project_id": "p1", "title": "Fix regression", "task_type": "implementation", "complexity": "medium", "expected_minutes": 20, "scope": ["Fix parser", "Add regression test"], "constraints": [], "acceptance_criteria": ["Tests pass"], "needs_repo_edit": True}
    value.update(changes); return value


def history(minutes=14):
    return [{"provider": "codex", "mode": "code", "effort": "medium", "status": "completed", "elapsed_minutes": minutes, "task_snapshot": {"task_type": "implementation", "complexity": "medium", "needs_repo_edit": True}, "quota_delta": {"status": "known", "windows": [{"name": "primary", "status": "known", "used_percent_delta": 2}]}}]


class DispatcherTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore(); create_project(self.store, project())

    def dispatch_case(self, req=None, q=None, records=None): return dispatch(self.store, object(), req or request(), q or quota(), [] if records is None else records)

    def test_new_task_codex_and_drive_consistency(self):
        result = self.dispatch_case(request(task_id="new-explicit-id"), records=history())
        self.assertEqual("codex", result["recommended_provider"]); self.assertEqual(14, result["estimated_minutes"])
        self.assertEqual("codex", result["provider"]); self.assertIsNone(result["model"])
        self.assertIn("selection_reason", result); self.assertIn("quota_evidence", result)
        self.assertEqual("Fix regression", self.store.get("tasks", "p1", "new-explicit-id")["title"])
        self.assertNotIn('"providers"', result["generated_prompt"])

    def test_future_model_contract_is_optional_and_validated(self):
        result = self.dispatch_case(request(title="Model contract", model="gpt-test", fallback_model="gpt-fallback"))
        self.assertEqual("gpt-test", result["model"]); self.assertEqual("gpt-fallback", result["fallback_model"])
        with self.assertRaises(TaskError): request_ok(request(model=""))

    def test_identity_header_round_trips_canonical_task_for_codex_and_claude(self):
        for provider, task_id in (("codex", "canonical-task-id"), ("claude", "canonical_task_id")):
            with self.subTest(provider=provider):
                result = self.dispatch_case(request(task_id=task_id, title="Conversation label differs", preferred_provider=provider), quota(80, 80))
                prompt = result["generated_prompt"]
                expected = {"ai": provider.title(), "project": "p1", "task": task_id}
                self.assertEqual(expected, parse_identity_header(prompt))
                self.assertTrue(prompt.startswith(f"AI: {provider.title()}\nProject: p1\nTask: {task_id}\n\n"))
                self.assertIn("Project name: Project One", prompt)
                self.assertIn("Task goal: Conversation label differs", prompt)
                self.assertNotIn("Conversation:", prompt)

    def test_existing_continuation_with_and_without_handoff(self):
        task = create_task(self.store, request(task_id="t1", current_progress="Parser fixed", next_action="Run tests"), assign=False)
        self.store.records[("projects", "p1", "p1")]["active_tasks"] = ["t1"]
        no_handoff = self.dispatch_case(request()); self.assertIn("Parser fixed", no_handoff["generated_prompt"]); self.assertIn("Latest handoff: none", no_handoff["generated_prompt"])
        create_handoff(self.store, {"handoff_id": "h1", "task_id": "t1", "project_id": "p1", "from_provider": "codex", "to_provider": "claude", "from_session": "a", "reason": "switch", "completed_work": [], "current_state": "Regression reproduced", "files_changed": [], "commits": [], "tests": [], "known_issues": [], "do_not_touch": ["billing.py"], "next_action": "Patch parser", "acceptance_criteria": [], "minimal_context": "Only parser.py is in scope."})
        continued = self.dispatch_case(request()); self.assertIn("Only parser.py is in scope", continued["generated_prompt"]); self.assertIn("billing.py", continued["generated_prompt"])

    def test_claude_preferred_excluded_and_unknown(self):
        claude = self.dispatch_case(request(task_type="architecture", needs_repo_edit=False, needs_research=True), quota(50, 80))
        self.assertEqual("claude", claude["recommended_provider"])
        preferred = self.dispatch_case(request(title="Manual review", preferred_provider="antigravity"))
        self.assertEqual("antigravity", preferred["recommended_provider"]); self.assertTrue(any("unknown" in item for item in preferred["warnings"]))
        excluded = self.dispatch_case(request(title="No Codex", excluded_provider="codex"), quota(90, 80))
        self.assertNotEqual("codex", excluded["recommended_provider"])

    def test_split_and_no_split(self):
        split = self.dispatch_case(request(title="Large task", expected_minutes=45))
        self.assertTrue(split["split_recommended"]); self.assertEqual(3, split["phase_count"]); self.assertIn("Execute Phase 1 only", split["generated_prompt"])
        small = self.dispatch_case(request(title="Small task", expected_minutes=20), records=history(14))
        self.assertFalse(small["split_recommended"]); self.assertEqual(1, small["phase_count"])

    def test_zero_samples_stale_quota_and_secret_redaction(self):
        stale = self.dispatch_case(request(title="Secret task", constraints=["token=sensitive-value", "credential:private"]), quota(updated="2020-01-01T00:00:00Z"))
        self.assertNotIn("sensitive-value", stale["generated_prompt"]); self.assertNotIn("private", stale["generated_prompt"]); self.assertTrue(stale["warnings"])
        self.assertEqual(20, stale["estimated_minutes"])

    def test_malformed_input_rejected(self):
        with self.assertRaises(TaskError): request_ok({"project_id": "p1"})
        with self.assertRaises(TaskError): request_ok(request(preferred_provider="codex", excluded_provider="codex"))

    def test_ordinary_task_carries_no_required_capability(self):
        result = self.dispatch_case(request())
        self.assertEqual([], result["required_capabilities"])
        self.assertEqual("not_required", result["capability_resolution_status"])
        self.assertNotIn("Agent-facing-instruction authoring aid", result["generated_prompt"])

    def test_agent_facing_task_resolves_writing_for_agents_on_a_supported_provider(self):
        result = self.dispatch_case(request(title="Update AGENTS.md with the new dispatch rule", scope=["AGENTS.md"], preferred_provider="claude"))
        self.assertEqual(["writing-for-agents"], result["required_capabilities"])
        self.assertEqual(["writing-for-agents"], result["resolved_capabilities"])
        self.assertEqual("resolved", result["capability_resolution_status"])
        self.assertEqual("0ab1b63a410a03d3627979a109c8695de27af954", result["actual_capability_source_version"])
        self.assertIn("Agent-facing-instruction authoring aid", result["generated_prompt"])
        self.assertIn("writing-for-agents", result["generated_prompt"])

    def test_agent_facing_task_on_unsupported_provider_reports_truthful_fallback_not_fake_success(self):
        result = self.dispatch_case(request(title="Update AGENTS.md with the new dispatch rule", scope=["AGENTS.md"], preferred_provider="gemini_app"))
        self.assertEqual(["writing-for-agents"], result["required_capabilities"])
        self.assertEqual([], result["resolved_capabilities"])
        self.assertEqual("unsupported_provider", result["capability_resolution_status"])
        self.assertIsNotNone(result["capability_fallback_reason"])
        self.assertNotIn("Agent-facing-instruction authoring aid", result["generated_prompt"])


if __name__ == "__main__": unittest.main()
