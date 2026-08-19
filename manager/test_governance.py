import unittest
from copy import deepcopy

from manager.dispatcher import dispatch
from manager.governance import MANDATORY_RULE_IDS, validate_completion_report
from manager.tasks import TaskError, create_handoff, create_project, create_task


class MemoryStore:
    def __init__(self):
        self.records = {}

    def put(self, area, project, name, document):
        self.records[(area, project, name)] = deepcopy(document)
        return deepcopy(document)

    def get(self, area, project, name):
        try:
            return deepcopy(self.records[(area, project, name)])
        except KeyError as exc:
            raise TaskError("not found") from exc

    def latest(self, area, project, task_id):
        raise TaskError("not found")


def project():
    return {
        "project_id": "p1", "name": "Project One", "repo": "https://example.test/repo",
        "default_branch": "main", "runtime_ssot": "Drive", "project_rules": [],
        "active_tasks": [], "current_phase": "P0", "important_constraints": [],
    }


def task_input(**changes):
    value = {
        "task_id": "governance-proof", "project_id": "p1", "title": "Prove governance",
        "task_type": "implementation", "complexity": "medium", "expected_minutes": 20,
        "needs_repo_edit": True, "needs_research": True, "scope": ["Implement the proof"],
        "constraints": [], "acceptance_criteria": ["Tests pass"], "source_context": {},
    }
    value.update(changes)
    return value


def quota():
    providers = []
    for name in ("codex", "claude", "antigravity", "gemini_app"):
        providers.append({
            "provider": name, "display_name": name, "collection_mode": "automatic",
            "source": "test", "source_type": "official", "confidence": "official",
            "last_updated": "2026-08-19T00:00:00Z", "status": "ok",
            "windows": [{"name": "primary", "remaining_percent": 80,
                         "used_percent": 20, "resets_at": None}],
        })
    return {"schema_version": "0.1.0", "generated_at": "2026-08-19T00:00:00Z", "providers": providers}


def completion_report(**changes):
    value = {
        "ai": "Codex", "project": "p1", "task": "governance-proof",
        "conversation": "Codex-Rule-Enforcement-P0-20260819",
        "session": "codex-rule-enforcement-p0-20260819",
        "current_progress": "Complete", "overall_project_progress": "P0 complete",
        "milestone_progress": "Governance gate complete", "estimated_remaining": "0 minutes",
        "waiting_blocker": "None", "actual_ai_provider_running_now": "None",
        "rule_evidence": {
            "research_before_build": {"outcome": "poc", "evidence": "Focused failing test and passing PoC"}
        },
    }
    value.update(changes)
    return value


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        create_project(self.store, project())

    def test_mandatory_rules_are_injected_and_rendered_for_all_dispatch_providers(self):
        task = create_task(self.store, task_input(), assign=False)
        self.assertEqual(list(MANDATORY_RULE_IDS), task["governance"]["mandatory_rule_ids"])
        for provider in ("codex", "claude", "antigravity"):
            with self.subTest(provider=provider):
                result = dispatch(self.store, object(), {
                    "project_id": "p1", "task_id": task["task_id"], "title": task["title"],
                    "task_type": task["task_type"], "complexity": task["complexity"],
                    "preferred_provider": provider,
                }, quota_document=quota(), executions=[])
                for rule_id in MANDATORY_RULE_IDS:
                    self.assertIn(rule_id, result["generated_prompt"])
                self.assertIn("Overall project progress:", result["generated_prompt"])

    def test_missing_injection_blocks_dispatch(self):
        task = create_task(self.store, task_input(), assign=False)
        del self.store.records[("tasks", "p1", task["task_id"])]["governance"]
        with self.assertRaisesRegex(TaskError, "mandatory governance"):
            dispatch(self.store, object(), {
                "project_id": "p1", "task_id": task["task_id"], "title": task["title"],
                "task_type": task["task_type"], "complexity": task["complexity"],
            }, quota_document=quota(), executions=[])

    def test_research_before_build_requires_poc_or_explicit_rejection_evidence(self):
        task = create_task(self.store, task_input(), assign=False)
        report = completion_report(rule_evidence={})
        with self.assertRaisesRegex(TaskError, "research_before_build"):
            validate_completion_report(report, task)
        report["rule_evidence"] = {
            "research_before_build": {"outcome": "rejected", "evidence": "Existing library rejected: incompatible license"}
        }
        validate_completion_report(report, task)

    def test_running_claim_requires_real_execution_evidence(self):
        task = create_task(self.store, task_input(needs_research=False), assign=False)
        report = completion_report(actual_ai_provider_running_now="Codex running", rule_evidence={})
        with self.assertRaisesRegex(TaskError, "running_evidence"):
            validate_completion_report(report, task)
        report["running_evidence"] = {
            "provider": "codex", "execution_id": "exec-1", "status": "running",
            "observed_at": "2026-08-19T00:00:00Z",
        }
        with self.assertRaisesRegex(TaskError, "store-backed"):
            validate_completion_report(report, task)
        self.store.put("executions", "p1", "exec-1", {
            "execution_id": "exec-1", "provider": "codex", "status": "running",
            "heartbeat_at": "2026-08-19T00:00:00Z",
        })
        validate_completion_report(report, task, self.store)

    def test_completed_handoff_requires_mandatory_adm_status_fields(self):
        create_task(self.store, task_input(needs_research=False), assign=False)
        report = completion_report(rule_evidence={})
        del report["overall_project_progress"]
        with self.assertRaisesRegex(TaskError, "overall_project_progress"):
            create_handoff(self.store, {
                "handoff_id": "h1", "task_id": "governance-proof", "project_id": "p1",
                "from_provider": "codex", "to_provider": None, "from_session": "s1",
                "reason": "completed", "completed_work": ["Done"], "current_state": "completed",
                "files_changed": [], "commits": [], "tests": [], "known_issues": [],
                "do_not_touch": [], "next_action": "", "acceptance_criteria": ["Tests pass"],
                "minimal_context": "Done", "completion_report": report,
            })


if __name__ == "__main__":
    unittest.main()
