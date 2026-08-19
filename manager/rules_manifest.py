#!/usr/bin/env python3
"""Canonical machine-readable mandatory-rule manifest and enforcement helpers.

Extends the AI-DEVELOPMENT-RULES.md prose SSOT (rather than duplicating it)
with a machine-checkable form: rule_id, scope, severity, injection_required,
completion_check_required, and a short instruction. Dispatch and completion
code call these helpers directly so a rule is inherited mechanically instead
of depending on a caller remembering to include it.
"""

import json
from pathlib import Path

from manager.tasks import TaskError

MANIFEST_PATH = Path(__file__).parent / "rules_manifest.json"
REQUIRED_KEYS = ("rule_id", "scope", "severity", "injection_required", "completion_check_required", "instruction")


def load_rules(path=MANIFEST_PATH):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = manifest["rules"]
    for rule in rules:
        missing = [key for key in REQUIRED_KEYS if key not in rule]
        if missing:
            raise TaskError(f"rules manifest entry {rule.get('rule_id', '?')!r} missing keys: {', '.join(missing)}")
    return rules


def mandatory_rules(scope=None, path=MANIFEST_PATH):
    """Load rules, optionally filtered to those whose scope includes `scope`."""
    rules = load_rules(path)
    return rules if scope is None else [rule for rule in rules if scope in rule["scope"]]


def injection_lines(rules):
    return [f"- [{rule['rule_id']}] {rule['instruction']}" for rule in rules if rule["injection_required"]]


def validate_prompt_injection(prompt, rules):
    """Reject (rather than silently accept) a generated prompt missing a mandatory rule."""
    missing = [rule["rule_id"] for rule in rules if rule["injection_required"] and rule["instruction"] not in prompt]
    if missing:
        raise TaskError(f"mandatory rule injection missing from generated task: {', '.join(missing)}")
    return True


def validate_research_gate(evidence):
    """research_before_build: a comparison/report alone never satisfies the gate."""
    if not isinstance(evidence, dict):
        raise TaskError("research gate requires evidence: {candidates: [...], poc_attempted: bool}")
    if evidence.get("report_only") is True:
        raise TaskError("research gate is not satisfied by a comparison/report alone")
    candidates = evidence.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise TaskError("research gate requires at least one reviewed candidate")
    poc_attempted = evidence.get("poc_attempted")
    if poc_attempted not in (True, False):
        raise TaskError("research gate requires explicit poc_attempted: true/false")
    if poc_attempted is False:
        unrejected = [c.get("name", "unnamed") for c in candidates if not (isinstance(c, dict) and str(c.get("rejection_reason", "")).strip())]
        if unrejected:
            raise TaskError(f"research gate requires concrete rejection evidence for: {', '.join(unrejected)}")
    return True


def validate_running_claim(claim, rules=None):
    """real_running_truth: queued != running; a running claim needs real execution evidence."""
    rule = next((r for r in (rules or mandatory_rules("execution")) if r["rule_id"] == "real_running_truth"), None)
    required = (rule or {}).get("required_evidence_fields", ["execution_id", "provider", "session_id", "started_at"])
    if not isinstance(claim, dict) or claim.get("status") != "running":
        raise TaskError("running claim rejected: status must be 'running', not queued/pending")
    missing = [field for field in required if not claim.get(field)]
    if missing:
        raise TaskError(f"running claim rejected: missing execution/provider evidence: {', '.join(missing)}")
    return True


def validate_status_report(report, rules=None):
    """mandatory_status_report: every ADM work report must carry the required fields."""
    rule = next((r for r in (rules or mandatory_rules("status_report")) if r["rule_id"] == "mandatory_status_report"), None)
    required = (rule or {}).get("required_fields", [])
    if not isinstance(report, dict):
        raise TaskError("status report must be an object")
    missing = [field for field in required if not str(report.get(field, "")).strip()]
    if missing:
        raise TaskError(f"status report missing mandatory fields: {', '.join(missing)}")
    return True
