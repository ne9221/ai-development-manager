"""Canonical mandatory-rule inheritance and fail-closed validation."""

import hashlib
import json
from pathlib import Path

from manager.tasks import TaskError


RULES_PATH = Path(__file__).parents[1] / "governance-rules.json"


def _load_rules(path=RULES_PATH):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        rules = document["mandatory_rules"]
        status_fields = document["mandatory_status_fields"]
        status_labels = document["status_field_labels"]
        rule_ids = [rule["id"] for rule in rules]
        if (not isinstance(document["schema_version"], str) or not document["schema_version"]
                or not rules or len(rule_ids) != len(set(rule_ids))
                or any(not isinstance(rule.get("instruction"), str) or not rule["instruction"] for rule in rules)
                or not status_fields or len(status_fields) != len(set(status_fields))
                or set(status_labels) != set(status_fields)
                or any(not isinstance(label, str) or not label for label in status_labels.values())):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid canonical governance source: {path}") from exc
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return document, hashlib.sha256(canonical).hexdigest()


RULES, RULES_DIGEST = _load_rules()
RULES_VERSION = RULES["schema_version"]
MANDATORY_RULE_IDS = tuple(rule["id"] for rule in RULES["mandatory_rules"])
MANDATORY_STATUS_FIELDS = tuple(RULES["mandatory_status_fields"])
STATUS_FIELD_LABELS = RULES["status_field_labels"]


def task_enforcement():
    return {
        "rules_version": RULES_VERSION,
        "rules_digest": RULES_DIGEST,
        "mandatory_rule_ids": list(MANDATORY_RULE_IDS),
        "mandatory_status_fields": list(MANDATORY_STATUS_FIELDS),
    }


def inject_task_enforcement(task):
    task["governance"] = task_enforcement()
    return task


def validate_task_enforcement(task):
    if task.get("governance") != task_enforcement():
        raise TaskError("mandatory governance enforcement metadata is missing or stale; refusing dispatch")


def rendered_rules():
    return [f"{rule['id']}: {rule['instruction']}" for rule in RULES["mandatory_rules"]]


def validate_completion_report(report, task, store=None, provider=None, session=None):
    if not isinstance(report, dict):
        raise TaskError("completion report must be an object")
    for field in MANDATORY_STATUS_FIELDS:
        if not isinstance(report.get(field), str) or not report[field].strip():
            raise TaskError(f"completion report requires non-empty {field}")
    if report["project"] != task["project_id"] or report["task"] != task["task_id"]:
        raise TaskError("completion report task identity does not match the canonical task")
    if provider and report["ai"].strip().lower() != provider.replace("_", " ").lower():
        raise TaskError("completion report AI identity does not match the completing provider")
    if session and report["session"] != session:
        raise TaskError("completion report session identity does not match the completing session")

    evidence = report.get("rule_evidence")
    if not isinstance(evidence, dict):
        raise TaskError("completion report requires rule_evidence")
    if task.get("needs_research"):
        research = evidence.get("research_before_build")
        if (not isinstance(research, dict) or research.get("outcome") not in ("poc", "rejected")
                or not isinstance(research.get("evidence"), str) or not research["evidence"].strip()):
            raise TaskError("research_before_build requires PoC or explicit rejection evidence")

    if report["actual_ai_provider_running_now"].strip().lower() != "none":
        running = report.get("running_evidence")
        required = ("provider", "execution_id", "status", "observed_at")
        if (not isinstance(running, dict) or running.get("status") != "running"
                or any(not isinstance(running.get(key), str) or not running[key].strip() for key in required)):
            raise TaskError("a running claim requires real running_evidence")
        if store is None:
            raise TaskError("a running claim requires a store-backed real execution record")
        try:
            execution = store.get("executions", task["project_id"], running["execution_id"])
        except (KeyError, TaskError) as exc:
            raise TaskError("running_evidence does not resolve to a real execution") from exc
        observed = {execution.get("started_at"), execution.get("heartbeat_at"), execution.get("progress_updated_at")}
        if (execution.get("status") != "running" or execution.get("provider") != running["provider"]
                or running["observed_at"] not in observed):
            raise TaskError("running_evidence does not match an authoritative running execution")
    return report


def execution_completion_report(task, execution, summary):
    """Build the truthful minimum report for a verified terminal execution."""
    session = execution.get("session_id") or execution["execution_id"]
    return {
        "ai": execution["provider"].replace("_", " ").title(),
        "project": task["project_id"], "task": task["task_id"],
        "conversation": session, "session": session,
        "current_progress": summary,
        "overall_project_progress": "Task completed; project total was not independently measured",
        "milestone_progress": "Execution completed",
        "estimated_remaining": "0 minutes for this task",
        "waiting_blocker": "None",
        "actual_ai_provider_running_now": "None",
        "rule_evidence": {},
    }
