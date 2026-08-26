import unittest

from manager.capabilities import (
    CAPABILITY_REGISTRY,
    CapabilityError,
    PRECEDENCE_ORDER,
    assert_capability_is_subordinate,
    higher_precedence,
    is_agent_facing_task,
    precedence_rank,
    required_capabilities_for_task,
    resolve_provider_capability,
    resolve_task_capabilities,
)


def agent_facing_task(**changes):
    value = {"task_id": "t1", "project_id": "p1", "title": "Rewrite AGENTS.md for the ingest service", "task_type": "documentation", "scope": ["AGENTS.md"]}
    value.update(changes)
    return value


def ordinary_task(**changes):
    value = {"task_id": "t2", "project_id": "p1", "title": "Fix off-by-one in the parser", "task_type": "implementation", "scope": ["manager/executions.py"]}
    value.update(changes)
    return value


class GovernancePrecedenceTests(unittest.TestCase):
    """A governance-precedence regression test: nothing about capability
    resolution or the writing-for-agents registry entry can outrank a
    mandatory governance constraint."""

    def test_precedence_order_places_governance_above_capability_and_provider_defaults(self):
        self.assertEqual(
            (
                "ai_development_rules_common_governance",
                "project_specific_authoritative_rules",
                "task_specific_mandatory_constraints",
                "writing_for_agents",
                "provider_defaults",
            ),
            PRECEDENCE_ORDER,
        )
        # writing_for_agents must never rank at or above any governance/task layer.
        for layer in ("ai_development_rules_common_governance", "project_specific_authoritative_rules", "task_specific_mandatory_constraints"):
            self.assertLess(precedence_rank(layer), precedence_rank("writing_for_agents"))
        self.assertLess(precedence_rank("writing_for_agents"), precedence_rank("provider_defaults"))

    def test_higher_precedence_always_prefers_governance_over_capability(self):
        for layer in ("ai_development_rules_common_governance", "project_specific_authoritative_rules", "task_specific_mandatory_constraints"):
            self.assertEqual(layer, higher_precedence(layer, "writing_for_agents"))
            self.assertEqual(layer, higher_precedence("writing_for_agents", layer))

    def test_writing_for_agents_registry_entry_declares_a_subordinate_layer(self):
        self.assertTrue(assert_capability_is_subordinate("writing-for-agents"))
        self.assertEqual("writing_for_agents", CAPABILITY_REGISTRY["writing-for-agents"]["governance_layer"])

    def test_a_capability_claiming_governance_authority_is_refused(self):
        # Simulate a future/incorrect registry entry that tried to claim
        # authority over mandatory governance; the guard must fail closed
        # rather than silently allowing it to outrank governance.
        CAPABILITY_REGISTRY["_rogue-test-capability"] = {
            "governance_layer": "ai_development_rules_common_governance",
            "providers": {}, "supported_providers": (),
        }
        try:
            with self.assertRaises(CapabilityError):
                assert_capability_is_subordinate("_rogue-test-capability")
        finally:
            del CAPABILITY_REGISTRY["_rogue-test-capability"]

    def test_unknown_capability_is_refused_not_silently_allowed(self):
        with self.assertRaises(CapabilityError):
            assert_capability_is_subordinate("does-not-exist")

    def test_resolving_capabilities_never_mutates_the_registry(self):
        resolve_task_capabilities(agent_facing_task(), "claude")
        # The registry itself is untouched by resolution.
        self.assertIn("writing-for-agents", CAPABILITY_REGISTRY)
        self.assertEqual("writing_for_agents", CAPABILITY_REGISTRY["writing-for-agents"]["governance_layer"])


class TaskClassificationTests(unittest.TestCase):
    def test_ordinary_source_code_task_does_not_trigger(self):
        self.assertFalse(is_agent_facing_task(ordinary_task()))
        self.assertEqual([], required_capabilities_for_task(ordinary_task()))

    def test_ordinary_ui_and_business_logic_tasks_do_not_trigger(self):
        ui_task = ordinary_task(task_type="ui", title="Restyle the dashboard status card", scope=["dashboard/status_card.tsx"])
        business_task = ordinary_task(task_type="business_logic", title="Add proration rule to the billing calculator", scope=["manager/billing.py"])
        self.assertFalse(is_agent_facing_task(ui_task))
        self.assertFalse(is_agent_facing_task(business_task))

    def test_task_that_merely_mentions_an_ordinary_word_does_not_false_positive(self):
        # "spec" and "handoff" appear as ordinary English words here, not as
        # a named Agent-facing-instruction document category.
        task = ordinary_task(title="Implement the OpenAPI spec client and handoff response codes", scope=["manager/api_client.py"])
        self.assertFalse(is_agent_facing_task(task))

    def test_agent_facing_document_targets_trigger(self):
        cases = [
            agent_facing_task(title="Update AGENTS.md with the new dispatch rule", scope=["AGENTS.md"]),
            agent_facing_task(title="Revise CLAUDE.md for the new provider", scope=["CLAUDE.md"]),
            agent_facing_task(title="Author a new skill", scope=["skills/my-skill/SKILL.md"]),
            agent_facing_task(title="Add rule 18 to the common governance file", scope=["AI-DEVELOPMENT-RULES.md"]),
            agent_facing_task(title="Write the task brief for the ingest migration", scope=[]),
            agent_facing_task(title="Draft the handoff document format", scope=[]),
            agent_facing_task(title="Tighten the agent prompt used for dispatch", scope=[]),
        ]
        for task in cases:
            with self.subTest(title=task["title"]):
                self.assertTrue(is_agent_facing_task(task))
                self.assertEqual(["writing-for-agents"], required_capabilities_for_task(task))

    def test_ambiguous_scope_text_alone_does_not_trigger(self):
        # "spec" and "handoff" used loosely (not as a named document
        # category like "task brief" or "handoff document") must not
        # false-positive, regardless of task_type.
        task = ordinary_task(task_type="testing", title="Add tests for the spec parser handoff path")
        self.assertFalse(is_agent_facing_task(task))

    def test_structured_task_type_alone_triggers_even_with_ordinary_title(self):
        task = ordinary_task(task_type="common_governance", title="Quarterly cleanup", scope=[])
        self.assertTrue(is_agent_facing_task(task))
        self.assertEqual(["writing-for-agents"], required_capabilities_for_task(task))

    def test_installed_but_unused_capability_never_force_triggers(self):
        # Merely having the skill installed for every provider must not make
        # an ordinary task require it.
        self.assertIn("claude", CAPABILITY_REGISTRY["writing-for-agents"]["providers"])
        self.assertEqual([], required_capabilities_for_task(ordinary_task()))


class ProviderResolutionTests(unittest.TestCase):
    def test_supported_provider_resolves_available_with_pinned_version(self):
        for provider in ("claude", "codex", "antigravity"):
            with self.subTest(provider=provider):
                resolution = resolve_provider_capability("writing-for-agents", provider)
                self.assertTrue(resolution["available"])
                self.assertEqual("0ab1b63a410a03d3627979a109c8695de27af954", resolution["source_version"])
                self.assertIsNone(resolution["reason"])

    def test_antigravity_verification_is_marked_structural_only(self):
        resolution = resolve_provider_capability("writing-for-agents", "antigravity")
        self.assertEqual("structural_documented_contract_only", resolution["verification"])

    def test_unsupported_provider_reports_truthful_fallback_not_fabricated_success(self):
        resolution = resolve_provider_capability("writing-for-agents", "gemini_app")
        self.assertFalse(resolution["available"])
        self.assertIsNone(resolution["source_version"])
        self.assertIsNotNone(resolution["reason"])
        self.assertIn("gemini_app", resolution["reason"])

    def test_unknown_provider_string_reports_truthful_fallback(self):
        resolution = resolve_provider_capability("writing-for-agents", "some-future-provider")
        self.assertFalse(resolution["available"])
        self.assertIsNotNone(resolution["reason"])

    def test_unknown_capability_id_reports_truthful_fallback(self):
        resolution = resolve_provider_capability("does-not-exist", "claude")
        self.assertFalse(resolution["available"])
        self.assertIn("unknown capability", resolution["reason"])


class ResolveTaskCapabilitiesTests(unittest.TestCase):
    def test_ordinary_task_is_not_required_regardless_of_provider(self):
        result = resolve_task_capabilities(ordinary_task(), "claude")
        self.assertEqual([], result["required_capabilities"])
        self.assertEqual("not_required", result["resolution_status"])
        self.assertIsNone(result["fallback_reason"])

    def test_agent_facing_task_on_supported_provider_resolves(self):
        result = resolve_task_capabilities(agent_facing_task(), "codex")
        self.assertEqual(["writing-for-agents"], result["required_capabilities"])
        self.assertEqual(["writing-for-agents"], result["resolved_capabilities"])
        self.assertEqual("resolved", result["resolution_status"])
        self.assertEqual("0ab1b63a410a03d3627979a109c8695de27af954", result["actual_capability_source_version"])
        self.assertIsNone(result["fallback_reason"])

    def test_agent_facing_task_on_unsupported_provider_reports_truthful_fallback(self):
        result = resolve_task_capabilities(agent_facing_task(), "gemini_app")
        self.assertEqual(["writing-for-agents"], result["required_capabilities"])
        self.assertEqual([], result["resolved_capabilities"])
        self.assertEqual("unsupported_provider", result["resolution_status"])
        self.assertIsNotNone(result["fallback_reason"])
        self.assertIsNone(result["actual_capability_source_version"])


if __name__ == "__main__":
    unittest.main()
