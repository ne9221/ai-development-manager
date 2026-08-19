import unittest

from manager.rules_manifest import (
    load_rules, mandatory_rules, injection_lines, validate_prompt_injection,
    validate_research_gate, validate_running_claim, validate_status_report,
)
from manager.tasks import TaskError


EXPECTED_RULE_IDS = {
    "cloud_first", "task_identity", "research_before_build", "copy_ready_ai_dispatch",
    "real_running_truth", "visibility_first", "mandatory_status_report",
}


class RulesManifestTests(unittest.TestCase):
    def test_manifest_defines_all_seven_mandatory_rules(self):
        rules = load_rules()
        self.assertEqual(EXPECTED_RULE_IDS, {rule["rule_id"] for rule in rules})
        for rule in rules:
            self.assertTrue(rule["injection_required"])
            self.assertIn(rule["severity"], ("blocking", "advisory"))

    def test_mandatory_rules_filters_by_scope(self):
        dispatch_rules = mandatory_rules("dispatch")
        self.assertEqual(EXPECTED_RULE_IDS, {rule["rule_id"] for rule in dispatch_rules})
        status_rules = mandatory_rules("status_report")
        self.assertIn("mandatory_status_report", {rule["rule_id"] for rule in status_rules})
        self.assertNotIn("copy_ready_ai_dispatch", {rule["rule_id"] for rule in status_rules})

    def test_validate_prompt_injection_accepts_compliant_prompt_rejects_missing(self):
        rules = mandatory_rules("dispatch")
        compliant = "\n".join(injection_lines(rules))
        self.assertTrue(validate_prompt_injection(compliant, rules))
        stripped = compliant.replace(rules[0]["instruction"], "")
        with self.assertRaises(TaskError) as ctx:
            validate_prompt_injection(stripped, rules)
        self.assertIn(rules[0]["rule_id"], str(ctx.exception))

    def test_research_before_build_requires_poc_or_rejection_evidence(self):
        with self.assertRaises(TaskError):
            validate_research_gate({"report_only": True, "candidates": [{"name": "foo"}]})
        with self.assertRaises(TaskError):
            validate_research_gate({"candidates": []})
        with self.assertRaises(TaskError):
            validate_research_gate({"candidates": [{"name": "foo"}], "poc_attempted": False})
        self.assertTrue(validate_research_gate({
            "candidates": [{"name": "foo", "rejection_reason": "no OAuth support, dead since 2024"}],
            "poc_attempted": False,
        }))
        self.assertTrue(validate_research_gate({"candidates": [{"name": "foo"}], "poc_attempted": True}))

    def test_running_claim_requires_execution_evidence(self):
        with self.assertRaises(TaskError):
            validate_running_claim({"status": "queued"})
        with self.assertRaises(TaskError):
            validate_running_claim({"status": "running", "execution_id": "e1"})
        self.assertTrue(validate_running_claim({
            "status": "running", "execution_id": "e1", "provider": "codex",
            "session_id": "s1", "started_at": "2026-08-19T00:00:00Z",
        }))

    def test_status_report_requires_mandatory_fields(self):
        with self.assertRaises(TaskError) as ctx:
            validate_status_report({"current_progress": "half done"})
        self.assertIn("overall_project_progress", str(ctx.exception))
        self.assertTrue(validate_status_report({
            "current_progress": "half done", "overall_project_progress": "60%",
            "milestone_progress": "M2 in progress", "estimated_remaining": "2 hours",
            "waiting_blocker": "none", "actual_ai_provider_running": "Claude",
        }))


if __name__ == "__main__":
    unittest.main()
