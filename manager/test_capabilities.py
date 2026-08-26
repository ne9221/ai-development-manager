import unittest

from manager.capabilities import (
    ADVISORY_ONLY,
    CAPABILITY_REGISTRY,
    CapabilityError,
    assert_capability_is_advisory,
    is_agent_facing_task,
    required_capabilities_for_task,
    resolve_provider_capability,
    resolve_task_capabilities,
)
from manager.governance import validate_task_enforcement
from manager.rules_manifest import mandatory_rules


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

    def test_writing_for_agents_registry_entry_declares_the_advisory_layer(self):
        self.assertTrue(assert_capability_is_advisory("writing-for-agents"))
        self.assertEqual(ADVISORY_ONLY, CAPABILITY_REGISTRY["writing-for-agents"]["governance_layer"])

    def test_a_capability_claiming_governance_authority_is_refused(self):
        # Simulate a future/incorrect registry entry that tried to claim
        # authority it must not have; the guard must fail closed rather than
        # silently allowing it.
        CAPABILITY_REGISTRY["_rogue-test-capability"] = {
            "governance_layer": "ai_development_rules_common_governance",
            "providers": {}, "supported_providers": (),
        }
        try:
            with self.assertRaises(CapabilityError):
                assert_capability_is_advisory("_rogue-test-capability")
        finally:
            del CAPABILITY_REGISTRY["_rogue-test-capability"]

    def test_unknown_capability_is_refused_not_silently_allowed(self):
        with self.assertRaises(CapabilityError):
            assert_capability_is_advisory("does-not-exist")

    def test_resolving_capabilities_never_mutates_the_registry(self):
        resolve_task_capabilities(agent_facing_task(), "claude")
        self.assertIn("writing-for-agents", CAPABILITY_REGISTRY)
        self.assertEqual(ADVISORY_ONLY, CAPABILITY_REGISTRY["writing-for-agents"]["governance_layer"])

    def test_capability_module_never_touches_task_governance_stamp(self):
        # Structural backstop: capability resolution must not be able to
        # forge or clear the digest-bound governance stamp that
        # manager.governance.validate_task_enforcement requires on every
        # dispatched task. A task carrying no governance stamp is still
        # rejected exactly as it would be with this module never imported.
        task = agent_facing_task()
        resolve_task_capabilities(task, "claude")
        self.assertNotIn("governance", task)
        with self.assertRaises(Exception):
            validate_task_enforcement(task)

    def test_capability_advisory_note_is_lower_priority_than_mandatory_rules(self):
        # The mandatory-rule injection lines (see manager/rules_manifest.py,
        # used by manager/dispatcher.py) are unrelated to and unaffected by
        # this module -- capability resolution has no path to remove or
        # reorder them.
        rules = mandatory_rules("dispatch")
        self.assertTrue(rules)


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
            agent_facing_task(title="Add a rule to the common governance file", scope=["AI-DEVELOPMENT-RULES.md"]),
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

    def test_plural_phrasing_of_agent_facing_categories_triggers(self):
        # Regression coverage for a real false-negative bug found by an
        # independent adversarial review: most alternatives in
        # _AGENT_FACING_PATTERN lacked the "s?" pluralization that only the
        # "governance" branch originally had, so plural phrasing of the same
        # document categories silently fell through as ordinary tasks.
        cases = [
            agent_facing_task(title="Update the Codex system instructions", scope=[]),
            agent_facing_task(title="Write the task briefs for phase 2 and phase 3", scope=[]),
            agent_facing_task(title="Revise the handoff documents for the migration", scope=[]),
            agent_facing_task(title="Rewrite all of the agent prompts used by the dispatcher", scope=[]),
            agent_facing_task(title="Tighten the agent-facing instructions across the repo", scope=[]),
            agent_facing_task(title="Write a new skill for the marketplace covering PDF export", scope=[]),
        ]
        for task in cases:
            with self.subTest(title=task["title"]):
                self.assertTrue(is_agent_facing_task(task))
                self.assertEqual(["writing-for-agents"], required_capabilities_for_task(task))

    def test_generic_governance_and_system_prompt_usage_does_not_false_positive(self):
        # Regression coverage for a real false-positive bug found by the same
        # review: bare "governance rule/document" and "system prompt" phrases
        # matched even in ordinary, non-agent-instruction business/technical
        # usage. These now require an agent-instruction context word to
        # directly modify the phrase (see _CONTEXT_GATED_PATTERN).
        cases = [
            ordinary_task(
                task_type="business_logic",
                title="Add a data governance rule to the compliance module",
                scope=["manager/compliance.py"],
            ),
            ordinary_task(
                task_type="implementation",
                title="Implement the corporate governance document viewer feature",
                scope=["manager/documents_viewer.py"],
            ),
            ordinary_task(
                task_type="implementation",
                title="Add system prompt caching to reduce latency in the LLM client",
                scope=["manager/llm_client.py"],
            ),
        ]
        for task in cases:
            with self.subTest(title=task["title"]):
                self.assertFalse(is_agent_facing_task(task))
                self.assertEqual([], required_capabilities_for_task(task))

    def test_bare_skill_word_in_business_or_game_content_does_not_false_positive(self):
        # Regression coverage for a second real false-positive bug found by a
        # further adversarial review: the "(verb) ... skill(s)" branch added
        # to fix "write a new skill for the marketplace" was too loose and
        # matched ordinary business/game-domain content that merely contains
        # the word "skill(s)", with no actual AI-agent-skill signal anywhere
        # in the text. These now require co-occurrence with real
        # AI-agent-skill vocabulary (see _AGENT_SKILL_CONTEXT_PATTERN).
        cases = [
            ordinary_task(
                task_type="implementation",
                title="Create user profile with skills",
                scope=["manager/profile.py"],
            ),
            ordinary_task(
                task_type="ui",
                title="Add soft skills filter to HR dashboard",
                scope=["hr/dashboard.tsx"],
            ),
            ordinary_task(
                task_type="business_logic",
                title="Build employee leadership skills matrix",
                scope=["hr/leadership.py"],
            ),
            ordinary_task(
                task_type="implementation",
                title="Create combat skills tree for RPG character",
                scope=["game/skills_tree.py"],
            ),
            ordinary_task(
                task_type="implementation",
                title="Build the player combat skill system in game client",
                scope=["game/client/combat.py"],
            ),
        ]
        for task in cases:
            with self.subTest(title=task["title"]):
                self.assertFalse(is_agent_facing_task(task))
                self.assertEqual([], required_capabilities_for_task(task))

    def test_skill_verb_phrase_still_triggers_with_genuine_agent_skill_signal(self):
        # Must not regress: the marketplace-Agent-Skill phrasing that
        # motivated the "(verb) ... skill(s)" branch in the first place must
        # keep matching once it is gated on real AI-agent-skill vocabulary.
        task = agent_facing_task(title="Write a new skill for the marketplace", scope=[])
        self.assertTrue(is_agent_facing_task(task))
        self.assertEqual(["writing-for-agents"], required_capabilities_for_task(task))

    def test_context_word_must_modify_the_gated_phrase_not_merely_co_occur(self):
        # Regression coverage for a third real bug found by the same review:
        # _CONTEXT_GATED_PATTERN used to fire whenever an agent/AI/assistant/
        # chatbot word appeared *anywhere* in the same text field as
        # "governance file/rule/document" or "system prompt", even when the
        # context word was describing an unrelated noun several words away
        # (a database schema field, a compliance/business-role domain, the
        # module a rule applies to) rather than the governance/prompt
        # artifact itself. The gate is now tightened to require the context
        # word to directly modify the phrase.
        cases = [
            ordinary_task(
                task_type="implementation",
                title="Add a data governance rule for user privacy in the AI assistant module",
                scope=["manager/privacy.py"],
            ),
            ordinary_task(
                task_type="implementation",
                title="Add system prompt field to the database schema of AI assistant app",
                scope=["manager/schema.py"],
            ),
            ordinary_task(
                task_type="business_logic",
                title="Add a governance document for insurance agents compliance",
                scope=["manager/compliance.py"],
            ),
        ]
        for task in cases:
            with self.subTest(title=task["title"]):
                self.assertFalse(is_agent_facing_task(task))
                self.assertEqual([], required_capabilities_for_task(task))


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

    def test_claude_and_codex_verification_is_an_honest_non_reproducible_claim(self):
        # Regression coverage for a real issue found by an independent
        # adversarial review: the claude/codex entries used to hardcode the
        # bare, unfalsifiable string "live_session_verified" with zero
        # checked-in evidence anywhere in the repo, inconsistent with
        # Antigravity's honestly-caveated entry. The value must now say
        # plainly *how* it was established (manual, one-time, not CI
        # reproducible) rather than reading as an automated per-execution
        # check.
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                resolution = resolve_provider_capability("writing-for-agents", provider)
                verification = resolution["verification"]
                self.assertNotEqual("live_session_verified", verification)
                self.assertIn("manually_verified", verification)
                self.assertIn("not_reproducible", verification)
                entry = CAPABILITY_REGISTRY["writing-for-agents"]["providers"][provider]
                self.assertIn("One-time manual/operator-driven check", entry["verification_note"])
                self.assertIn("not an automated or CI-reproducible one", entry["verification_note"])

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
