#!/usr/bin/env python3
"""Named-capability registry and resolver for Agent-facing-instruction skills.

This module lets the dispatcher recognize, resolve, and record usage of a
provider-side skill (currently only ``writing-for-agents``) as a tracked
capability, without vendoring the skill's content into this repo. Each
registry entry only records *where* the authoritative source lives and *how*
each provider was given a copy of it - never the skill text itself.

Governance precedence (see AI-DEVELOPMENT-RULES.md rule 17): a capability
such as ``writing-for-agents`` sits below every governance and task layer.
It may shape phrasing of Agent-facing instructions; it must never be able to
remove, weaken, or reinterpret a mandatory rule. ``PRECEDENCE_ORDER`` below
is the authoritative ordering and is asserted by
``assert_capability_is_subordinate`` and covered by
``manager/test_capabilities.py``.
"""

import re


class CapabilityError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Governance precedence (Task: Governance precedence)
# --------------------------------------------------------------------------

# Highest precedence first. Nothing below "writing_for_agents" may override
# anything above it; a capability entry is only ever allowed to occupy the
# "writing_for_agents" slot or lower.
PRECEDENCE_ORDER = (
    "ai_development_rules_common_governance",
    "project_specific_authoritative_rules",
    "task_specific_mandatory_constraints",
    "writing_for_agents",
    "provider_defaults",
)

# Layers a capability entry is allowed to claim. A capability that declared
# any higher layer would be claiming governance authority it must not have.
_SUBORDINATE_LAYERS = frozenset(PRECEDENCE_ORDER[PRECEDENCE_ORDER.index("writing_for_agents"):])


def precedence_rank(layer):
    """Lower rank = higher precedence. Raises for an unknown layer."""
    if layer not in PRECEDENCE_ORDER:
        raise CapabilityError(f"unknown governance layer: {layer!r}")
    return PRECEDENCE_ORDER.index(layer)


def higher_precedence(layer_a, layer_b):
    """Return the layer with higher precedence (lower rank) between the two."""
    return layer_a if precedence_rank(layer_a) <= precedence_rank(layer_b) else layer_b


def assert_capability_is_subordinate(capability_id):
    """Fail closed if a registered capability ever claims governance authority.

    This is a regression guard: it is meaningless for it to ever raise given
    the registry below, but it exists so that a future entry cannot silently
    grant a capability a governance-overriding layer without a test noticing.
    """
    entry = CAPABILITY_REGISTRY.get(capability_id)
    if entry is None:
        raise CapabilityError(f"unknown capability: {capability_id!r}")
    layer = entry.get("governance_layer")
    if layer not in _SUBORDINATE_LAYERS:
        raise CapabilityError(
            f"capability {capability_id!r} declares governance layer {layer!r}, "
            "which would outrank mandatory governance/task constraints; refused"
        )
    return True


# --------------------------------------------------------------------------
# Capability registry (Task: Capability registry)
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
        "governance_layer": "writing_for_agents",
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
                "verification": "live_session_verified",
                "verification_note": (
                    "A fresh `claude -p` process listed "
                    "mattpocock-skills:writing-for-agents with the correct "
                    "description."
                ),
            },
            "codex": {
                "installed": True,
                "resolution_mechanism": "codex_skill_installer_github_fetch",
                "package": None,
                "package_version": None,
                "scope": "user ($CODEX_HOME/skills)",
                "source_ref": "0ab1b63a410a03d3627979a109c8695de27af954",
                "verification": "live_session_verified",
                "verification_note": (
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
# Task classification (Task: Auto-trigger scope at task-parse time)
# --------------------------------------------------------------------------

# Explicit filename / document-category markers. Matching is deliberately
# narrow (filenames and named document categories, not bare English words
# like "spec" or "handoff" in isolation) so ordinary source-code, UI, or
# business-logic tasks that merely mention such a word in passing are not
# misclassified. See manager/test_capabilities.py for the ordinary-task and
# agent-facing-task classification tests.
_AGENT_FACING_PATTERN = re.compile(
    r"(?i)(?:"
    r"\bAGENTS\.md\b"
    r"|\bCLAUDE\.md\b"
    r"|\bGEMINI\.md\b"
    r"|\bSKILL\.md\b"
    r"|\bAI-DEVELOPMENT-RULES(?:\.md)?\b"
    r"|\bPROJECT[- ]RULES(?:\.md)?\b"
    r"|\bcommon governance\b"
    r"|\bgovernance (?:file|rule|document)s?\b"
    r"|\bagent[- ]facing instruction\b"
    r"|\bagent prompt\b"
    r"|\bsystem instruction\b"
    r"|\bsystem prompt\b"
    r"|\btask brief\b"
    r"|\bhandoff (?:document|template|schema|format)\b"
    r"|\b(?:new |a |an |the )?skill(?:'s)? (?:SKILL\.md|definition|authoring|creation)\b"
    r"|\bwriting-for-agents\b"
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
    """
    if not isinstance(task, dict):
        return False
    if _normalized_task_type(task) in AGENT_FACING_TASK_TYPES:
        return True
    for text in _task_text_fields(task):
        if _AGENT_FACING_PATTERN.search(text):
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
# Provider resolution + dispatch integration (Task: Dispatch integration)
# --------------------------------------------------------------------------

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
    mechanism = {
        "claude_plugin_marketplace": "installed plugin skill (mattpocock-skills:writing-for-agents)",
        "codex_skill_installer_github_fetch": "installed skill ($CODEX_HOME/skills/writing-for-agents)",
        "manual_fetch_into_gemini_config_skills_dir": "global skill (~/.gemini/config/skills/writing-for-agents)",
    }.get(resolution["resolution_mechanism"], "installed skill")
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
    assert_capability_is_subordinate(required[0])
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
