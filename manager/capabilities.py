#!/usr/bin/env python3
"""Named-capability registry and resolver for Agent-facing-instruction skills.

This module lets the dispatcher recognize, resolve, and record usage of a
provider-side skill (currently only ``writing-for-agents``) as a tracked
capability, without vendoring the skill's content into this repo. Each
registry entry only records *where* the authoritative source lives and *how*
each provider was given a copy of it - never the skill text itself.

Governance precedence (see AI-DEVELOPMENT-RULES.md rule 18): a capability
such as ``writing-for-agents`` has no governance authority. It may shape
phrasing of Agent-facing instructions; it must never remove, weaken, or
reinterpret a mandatory rule. ``assert_capability_is_advisory`` is the
fail-closed regression guard for that invariant, covered by
``manager/test_capabilities.py``. It is deliberately a single check against
one sentinel value rather than a general precedence-ranking system: nothing
elsewhere in this codebase consults a capability's "layer" to make a
decision, so a full ordered hierarchy would be unused abstraction (see
AI-DEVELOPMENT-RULES.md rule 11, minimal-change/YAGNI). The real backstop is
structural: this module never imports or writes governance-rules.json,
rules_manifest.json, or a task's ``governance`` field, and
``manager.governance.validate_task_enforcement`` (already required by every
dispatch) is untouched by anything here.
"""

import re


class CapabilityError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Governance precedence
# --------------------------------------------------------------------------

# The only governance layer a capability registry entry may ever declare.
# assert_capability_is_advisory fails closed if an entry ever claims anything
# else (e.g. a future edit that tried to grant a capability override power).
ADVISORY_ONLY = "advisory_no_governance_authority"


def assert_capability_is_advisory(capability_id):
    """Fail closed if a registered capability ever claims governance authority."""
    entry = CAPABILITY_REGISTRY.get(capability_id)
    if entry is None:
        raise CapabilityError(f"unknown capability: {capability_id!r}")
    layer = entry.get("governance_layer")
    if layer != ADVISORY_ONLY:
        raise CapabilityError(
            f"capability {capability_id!r} declares governance layer {layer!r}, "
            f"not {ADVISORY_ONLY!r}; refused"
        )
    return True


# --------------------------------------------------------------------------
# Capability registry
# --------------------------------------------------------------------------

# Task categories where an Agent-facing-instruction skill like
# writing-for-agents is applicable. Kept in one place so the classifier
# (below) and the registry entry describe the same scope.
AGENT_FACING_TASK_TYPES = (
    "agents_md",
    "claude_md",
    "gemini_md",
    "skill",
    "common_governance",
    "project_rules",
    "spec",
    "task_brief",
    "handoff",
    "agent_prompt",
    "agent_instructions",
)

CAPABILITY_REGISTRY = {
    "writing-for-agents": {
        "capability_id": "writing-for-agents",
        "description": (
            "Structural/expression guidance for writing clear, actionable "
            "instructions consumed by AI agents (structure, trigger "
            "conditions, completion criteria, context efficiency). No "
            "governance authority."
        ),
        "governance_layer": ADVISORY_ONLY,
        "source": {
            "type": "github",
            "repo": "mattpocock/skills",
            "path": "skills/productivity/writing-for-agents",
            "ref": "0ab1b63a410a03d3627979a109c8695de27af954",
        },
        "supported_providers": ("claude", "codex", "antigravity"),
        "providers": {
            "claude": {
                "installed": True,
                "resolution_mechanism": "claude_plugin_marketplace",
                "package": "mattpocock-skills",
                "package_version": "1.2.3",
                "scope": "user",
                "source_ref": "0ab1b63a410a03d3627979a109c8695de27af954",
                "verification": "manually_verified_not_reproducible_2026-08-26",
                "verification_note": (
                    "One-time manual/operator-driven check, not an automated "
                    "or CI-reproducible one (the `claude` CLI and a live "
                    "account are not available in CI): on 2026-08-26 a fresh "
                    "`claude -p` process listed mattpocock-skills:"
                    "writing-for-agents with the correct description."
                ),
            },
            "codex": {
                "installed": True,
                "resolution_mechanism": "codex_skill_installer_github_fetch",
                "package": None,
                "package_version": None,
                "scope": "user ($CODEX_HOME/skills)",
                "source_ref": "0ab1b63a410a03d3627979a109c8695de27af954",
                "verification": "manually_verified_not_reproducible_2026-08-26",
                "verification_note": (
                    "One-time manual/operator-driven check, not an automated "
                    "or CI-reproducible one (the `codex` CLI and a live "
                    "account are not available in CI): on 2026-08-26 "
                    "`codex exec -s read-only` enumerated writing-for-agents "
                    "among its discovered skills with the correct "
                    "description."
                ),
            },
            "antigravity": {
                "installed": True,
                "resolution_mechanism": "manual_fetch_into_gemini_config_skills_dir",
                "package": None,
                "package_version": None,
                "scope": "global (~/.gemini/config/skills)",
                "source_ref": "0ab1b63a410a03d3627979a109c8695de27af954",
                "verification": "structural_documented_contract_only",
                "verification_note": (
                    "No CLI exists for Antigravity, so there is no way to "
                    "drive a live AG chat session from a terminal to prove "
                    "runtime pickup. Verified only against AG's own "
                    "documented global-skill-directory contract."
                ),
            },
        },
        "last_verified": "2026-08-26",
        "drift_status": "no_drift_all_pinned_same_commit",
        "applicable_task_types": AGENT_FACING_TASK_TYPES,
    },
}


# --------------------------------------------------------------------------
# Task classification
# --------------------------------------------------------------------------

# Explicit filename / document-category markers. Matching is deliberately
# narrow (filenames and named document categories, not bare English words
# like "spec" or "handoff" in isolation) so ordinary source-code, UI, or
# business-logic tasks that merely mention such a word in passing are not
# misclassified. See manager/test_capabilities.py for the ordinary-task and
# agent-facing-task classification tests.
#
# Every phrase-based (non-filename) alternative below accepts an optional
# trailing "s" so that e.g. "task briefs", "handoff documents", or "agent
# prompts" match the same as their singular forms - the previous version
# only pluralized "governance ...s?", which was a false-negative bug.
#
# Known, deliberately-accepted false negatives (do NOT expand this regex to
# chase them - see manager/test_capabilities.py and the task history for
# why): subagent-first/inverted phrasing generally, e.g. "write prompt for
# the agent" or "write instructions for the AI assistant". Catching those
# would require matching on a bare context word co-occurring with a bare
# authoring verb across the whole sentence, which is exactly the
# over-broad-matching failure mode fixed below for "skill(s)" and for
# "governance .../system prompt" - so it is intentionally left unfixed
# rather than reintroduced here.
_AGENT_FACING_PATTERN = re.compile(
    r"(?i)(?:"
    r"\bAGENTS\.md\b"
    r"|\bCLAUDE\.md\b"
    r"|\bGEMINI\.md\b"
    r"|\bSKILL\.md\b"
    r"|\bAI-DEVELOPMENT-RULES(?:\.md)?\b"
    r"|\bPROJECT[- ]RULES(?:\.md)?\b"
    r"|\bcommon governance\b"
    r"|\bagent[- ]facing instructions?\b"
    r"|\bagent prompts?\b"
    r"|\bsystem instructions?\b"
    r"|\btask briefs?\b"
    r"|\bhandoff (?:document|template|schema|format)s?\b"
    r"|\b(?:new |a |an |the )?skill(?:'s)? (?:SKILL\.md|definition|authoring|creation)\b"
    r"|\bwriting-for-agents\b"
    r")"
)

# A bare "(verb) ... skill(s)" phrase (e.g. "create ... skills", "build ...
# skill") is ordinary English across countless non-agent domains - HR ("soft
# skills filter"), games ("combat skill system"), business ("employee
# leadership skills matrix"). Unlike the filename/category markers above, it
# only counts as Agent-Skill authoring when the same text field also names
# actual AI-agent-skill vocabulary: an agent platform (agent/AI/Claude/
# Codex/Antigravity), an explicit SKILL.md/"skill authoring"/"agent skill"
# reference, or a skill being authored *for* the Agent Skill marketplace
# (see _SKILL_MARKETPLACE_PATTERN below) - not merely because the sentence
# happens to contain the word "skill(s)" next to some authoring verb. This
# deliberately does NOT widen the verb-to-noun word window (that was the
# original bug); it gates the existing phrase with a co-occurrence check,
# the same shape as the governance/system-prompt gate below. See
# manager/test_capabilities.py for the business/game-domain regression cases
# this must exclude, and the marketplace regression case it must still
# catch.
_SKILL_VERB_PATTERN = re.compile(
    r"(?i)\b(?:write|writing|wrote|written|author|authoring|authored|create|creating|created|"
    r"draft|drafting|drafted|revise|revising|revised|rewrite|rewriting|rewrote|"
    r"add|adding|added|build|building|built)\s+(?:\w+\s+){0,3}?skills?(?:'s)?\b"
)

_AGENT_SKILL_CONTEXT_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:agents?|AI|Claude|Codex|Antigravity)\b"
    r"|\bSKILL\.md\b"
    r"|\bskill authoring\b"
    r"|\bagent skill\b"
    r")"
)

# ADM is a cross-project global classifier, so a bare "marketplace" token
# co-occurring anywhere in the text is too wide a signal: an ordinary
# e-commerce/HR "marketplace" task can easily contain both "skill(s)" and
# "marketplace" without being about Agent Skill authoring at all (e.g.
# "Create seller skills matrix for marketplace users", "Build marketplace
# employee skills dashboard"). What actually distinguishes genuine Agent
# Skill marketplace phrasing is a skill being authored *for* the
# marketplace - "a new skill for the marketplace" - not "marketplace"
# merely appearing somewhere in the sentence. This mirrors the same
# "<phrase> + connector + optional article + context noun" proximity idiom
# used by _CONTEXT_GATED_PATTERN below for the governance/system-prompt
# gate, requiring "marketplace" to sit directly as the object of "for"
# relative to "skill(s)" itself. Deliberately "for" only, not "for|to":
# "Add customer service skills to marketplace profile" has "skills"
# directly followed by "to marketplace" with zero word gap too - the same
# shape the "for" idiom would otherwise match - but that sentence is adding
# a skills listing to a marketplace profile, not authoring a skill for the
# marketplace, so allowing "to" here would reintroduce a real false
# positive. Only "for" reliably distinguishes the two in the required
# regression cases (see manager/test_capabilities.py).
_SKILL_MARKETPLACE_PATTERN = re.compile(
    r"(?i)\bskills?(?:'s)?\s+for\s+(?:the\s+|an?\s+)?marketplace\b"
)

# "governance <file|rule|document>" and "system prompt" are ordinary English
# in plenty of ungoverned, non-agent contexts ("data governance rule",
# "governance document viewer", "system prompt caching in the LLM client").
# They only count as agent-facing when an actual AI-agent-instruction context
# word (agent/AI/assistant/chatbot) directly modifies the phrase - either
# immediately before it ("AI assistant governance rule") or immediately
# after it via "for"/"of" ("system prompt for the agent") - not merely
# present *somewhere* in the same sentence. A plain same-field co-occurrence
# check (the previous version of this gate) was too permissive: "add a data
# governance rule for user privacy in the AI assistant module" and "add a
# governance document for insurance agents compliance" both contain a
# context word and the gated phrase, but the context word describes an
# unrelated noun several words away (the module the rule applies to; the
# business/compliance domain of the document) rather than the
# governance/prompt artifact itself, so they must not match. Note "LLM"
# deliberately does NOT count as qualifying context: mentioning an LLM
# client is not the same as writing agent instructions.
_CONTEXT_GATED_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:agents?|AI|assistants?|chatbots?)(?:'s)?\s+(?:\w+\s+){0,1}?"
    r"(?:governance (?:file|rule|document)s?|system prompts?)\b"
    r"|\b(?:governance (?:file|rule|document)s?|system prompts?)\b\s+(?:for|of)\s+"
    r"(?:the\s+|an?\s+)?(?:agents?|AI|assistants?|chatbots?)\b"
    r")"
)


def _normalized_task_type(task):
    return re.sub(r"[\s-]+", "_", str(task.get("task_type") or "").strip().lower())


def _task_text_fields(task):
    fields = [task.get("title") or ""]
    fields.extend(str(item) for item in task.get("scope", []) or [])
    return fields


def is_agent_facing_task(task):
    """True when a task's target is Agent-facing-instruction authoring/editing.

    Triggers on either of two independent, narrow signals:
    1. An explicit structured ``task_type`` matching one of the
       ``AGENT_FACING_TASK_TYPES`` categories (e.g. ``"agents_md"``,
       ``"skill"``, ``"common_governance"``).
    2. An explicit document-category marker (filenames like
       ``AGENTS.md``/``CLAUDE.md``/``SKILL.md``, or named categories such as
       "task brief", "handoff document", "agent prompt") in the task's title
       or scope.

    Note that ``task_type`` in this codebase's dispatcher (see
    ``manager/assignment.py``) is normally a coarse work-mode label
    ("implementation", "debugging", "testing", ...) rather than a content
    domain, so it is intentionally *not* used to suppress a match found in
    title/scope text - ordinary source code, UI, and business-logic tasks
    are excluded instead by requiring the title/scope regex to name an
    actual Agent-facing document category, not a bare English word like
    "spec" or "handoff" used in its everyday sense.

    Two further, narrower signals are gated on additional context rather
    than firing on the bare phrase alone:

    3. A "(verb) ... skill(s)" phrase (e.g. "create ... skills", "build a
       skill") only counts when the same text field also names actual
       AI-agent-skill vocabulary (see ``_AGENT_SKILL_CONTEXT_PATTERN``) or
       the skill is authored *for* the Agent Skill marketplace specifically
       (see ``_SKILL_MARKETPLACE_PATTERN``), so "create user profile with
       skills" or "build employee leadership skills matrix" are correctly
       excluded while "write a new skill for the marketplace" still
       matches - and a bare co-occurrence like "skills ... marketplace"
       elsewhere in an unrelated e-commerce/HR sentence ("add customer
       service skills to marketplace profile") does not.
    4. "governance file/rule/document" or "system prompt" only count when an
       agent-instruction context word directly modifies that phrase (see
       ``_CONTEXT_GATED_PATTERN``), so "add a data governance rule to the
       compliance module" or "add system prompt caching to the LLM client"
       are correctly excluded - and so is a context word merely present
       *elsewhere* in the sentence, e.g. "add a data governance rule for
       user privacy in the AI assistant module".
    """
    if not isinstance(task, dict):
        return False
    if _normalized_task_type(task) in AGENT_FACING_TASK_TYPES:
        return True
    for text in _task_text_fields(task):
        if _AGENT_FACING_PATTERN.search(text):
            return True
        if _CONTEXT_GATED_PATTERN.search(text):
            return True
        if _SKILL_VERB_PATTERN.search(text) and (
            _AGENT_SKILL_CONTEXT_PATTERN.search(text) or _SKILL_MARKETPLACE_PATTERN.search(text)
        ):
            return True
    return False


def required_capabilities_for_task(task):
    """Capabilities that MUST be resolved for this task, or [] if none apply.

    Merely having a capability installed never forces this; only a task
    actually classified as Agent-facing-instruction work (see
    ``is_agent_facing_task``) requires it.
    """
    if not is_agent_facing_task(task):
        return []
    return ["writing-for-agents"]


# --------------------------------------------------------------------------
# Provider resolution + dispatch integration
# --------------------------------------------------------------------------

_MECHANISM_LABELS = {
    "claude_plugin_marketplace": "installed plugin skill (mattpocock-skills:writing-for-agents)",
    "codex_skill_installer_github_fetch": "installed skill ($CODEX_HOME/skills/writing-for-agents)",
    "manual_fetch_into_gemini_config_skills_dir": "global skill (~/.gemini/config/skills/writing-for-agents)",
}


def resolve_provider_capability(capability_id, provider):
    """Return this provider's resolution info for one capability.

    Never fabricates availability: an unsupported or unknown provider
    reports ``available: False`` with a truthful ``reason`` instead of
    silently succeeding.
    """
    entry = CAPABILITY_REGISTRY.get(capability_id)
    if entry is None:
        return {
            "capability_id": capability_id, "provider": provider, "available": False,
            "resolution_mechanism": None, "source_version": None, "verification": None,
            "reason": f"unknown capability: {capability_id}",
        }
    provider_info = entry["providers"].get(provider)
    if provider not in entry["supported_providers"] or provider_info is None:
        return {
            "capability_id": capability_id, "provider": provider, "available": False,
            "resolution_mechanism": None, "source_version": None, "verification": None,
            "reason": f"{capability_id} has no known installation/resolution mechanism for provider {provider!r}",
        }
    if not provider_info.get("installed"):
        return {
            "capability_id": capability_id, "provider": provider, "available": False,
            "resolution_mechanism": provider_info.get("resolution_mechanism"),
            "source_version": provider_info.get("source_ref"), "verification": provider_info.get("verification"),
            "reason": f"{capability_id} is registered for provider {provider!r} but not marked installed",
        }
    return {
        "capability_id": capability_id, "provider": provider, "available": True,
        "resolution_mechanism": provider_info.get("resolution_mechanism"),
        "source_version": provider_info.get("source_ref"), "verification": provider_info.get("verification"),
        "reason": None,
    }


def capability_prompt_note(capability_id, provider):
    """One advisory prompt line for an available capability, or None.

    Mirrors the existing "ponytail" advisory block in manager/dispatcher.py:
    explicitly lower priority than every requirement listed above it, and
    never itself capable of removing a requirement.
    """
    resolution = resolve_provider_capability(capability_id, provider)
    if not resolution["available"]:
        return None
    mechanism = _MECHANISM_LABELS.get(resolution["resolution_mechanism"], "installed skill")
    return (
        f"Apply the {mechanism} for {capability_id} to improve this document's structure, "
        "actionability, trigger conditions, completion criteria, and context efficiency. "
        "This is phrasing guidance only: it must never remove, weaken, or reinterpret any "
        "governance rule, mandatory gate, or acceptance criterion listed above."
    )


def resolve_task_capabilities(task, provider):
    """Full resolution result for a task against one selected provider.

    Returns the fields the dispatch/execution flow records:
    required_capabilities, resolved_capabilities, provider_capability_availability,
    actual_capability_source_version, resolution_status, fallback_reason.
    """
    required = required_capabilities_for_task(task)
    if not required:
        return {
            "required_capabilities": [], "resolved_capabilities": [],
            "provider_capability_availability": {}, "actual_capability_source_version": None,
            "resolution_status": "not_required", "fallback_reason": None,
        }
    assert_capability_is_advisory(required[0])
    availability = {}
    resolved = []
    versions = []
    reasons = []
    for capability_id in required:
        resolution = resolve_provider_capability(capability_id, provider)
        availability[capability_id] = resolution
        if resolution["available"]:
            resolved.append(capability_id)
            if resolution["source_version"]:
                versions.append(resolution["source_version"])
        else:
            reasons.append(resolution["reason"])
    if len(resolved) == len(required):
        status = "resolved"
    elif resolved:
        status = "partially_resolved"
    else:
        status = "unsupported_provider" if provider not in CAPABILITY_REGISTRY[required[0]]["supported_providers"] else "unavailable"
    return {
        "required_capabilities": required, "resolved_capabilities": resolved,
        "provider_capability_availability": availability,
        "actual_capability_source_version": versions[0] if versions else None,
        "resolution_status": status,
        "fallback_reason": "; ".join(reasons) or None,
    }
