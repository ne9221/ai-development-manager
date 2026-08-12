#!/usr/bin/env python3
"""Bounded, read-only continuation context from the Drive runtime SSOT."""

import argparse
import json
import re

from collectors.publish_drive import build_service
from manager.runtime_bridge import active_task, resolve_project
from manager.sessions import apply_manual_reviews, list_review_records, prompt_snippet, session_manager_key
from manager.tasks import DriveRecords, TaskError
from manager.overview import read_overview


RULES_PATH = __import__("pathlib").Path(__file__).parents[1] / "AI-DEVELOPMENT-RULES.md"


def shared_rules(path=RULES_PATH):
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"(\d+)\.\s+(.+)", line)
        if match:
            rules.append({"rule_id": int(match.group(1)), "text": match.group(2)})
    return rules


def _records(store, area, project_id):
    try:
        return store.list_records(area, project_id)
    except (TaskError, KeyError):
        return []


def recent_sessions(store, project_id, reviews=None, limit=5):
    if not isinstance(limit, int) or not 1 <= limit <= 5:
        raise TaskError("session limit must be between 1 and 5")
    records = {}
    for record in _records(store, "sessions", project_id):
        records[session_manager_key(record)] = record
    for record in _records(store, "sessions", "_unclassified"):
        records.setdefault(session_manager_key(record), record)
    effective = apply_manual_reviews(records.values(), list_review_records(store) if reviews is None else reviews)
    selected = [record for record in effective if record.get("project_id") == project_id]
    selected.sort(key=lambda record: record.get("updated_at") or record.get("started_at") or "", reverse=True)
    return [{
        "session_id": record["session_id"], "provider": record["provider"],
        "started_at": record.get("started_at"), "updated_at": record.get("updated_at"),
        "title": record.get("title"), "task_id": record.get("task_id"),
        "conversation_label": record.get("conversation_label"),
        "classification_method": record.get("classification_method"),
        "classification_status": record.get("classification_status"),
        "source_identifier": record.get("source_identifier"),
        "prompt_snippet": prompt_snippet(record),
    } for record in selected[:limit]]


def overview_focus(store, project_id, limit=5):
    """Return only actionable overview items; missing overviews remain compatible."""
    try:
        overview = read_overview(store, project_id)
    except (TaskError, KeyError):
        return []
    included = {"in_progress", "awaiting_validation"}
    items = [item for item in overview["items"] if item["status"] in included or (item["status"] == "pending" and item["priority"] == "high")]
    order = {"in_progress": 0, "awaiting_validation": 1, "pending": 2}
    items.sort(key=lambda item: (order[item["status"]], item["item_id"]))
    return [{key: item[key] for key in ("item_id", "title", "status", "priority", "current_progress", "next_action")} for item in items[:limit]]


def latest_execution(store, project_id, task_id):
    """Bounded execution summary for the active task; never includes quota_before/after/delta."""
    if not task_id:
        return None
    candidates = [item for item in _records(store, "executions", project_id) if item.get("task_id") == task_id]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda item: item.get("started_at") or "")
    return {key: chosen.get(key) for key in ("execution_id", "provider", "status", "started_at", "completed_at", "finished_at", "notes", "session_id")}


def context_pack(store, project_ref, task_id=None, user_request="", reviews=None, session_limit=5):
    project = resolve_project(store, project_ref, user_request)
    task = active_task(store, project, task_id, user_request)
    handoff = None
    if task:
        try:
            handoff = store.latest("handoffs", project["project_id"], task["task_id"])
        except (TaskError, KeyError):
            pass
    return {
        "contract_version": "1.1",
        "project": {key: project.get(key) for key in ("project_id", "name", "repo", "default_branch", "working_directory")},
        "active_task": ({key: task.get(key) for key in ("task_id", "title", "status", "current_progress", "next_action", "scope", "acceptance_criteria", "mode", "effort")} if task else None),
        "latest_handoff": ({key: handoff.get(key) for key in ("handoff_id", "from_provider", "from_session", "reason", "current_state", "next_action", "minimal_context", "tests", "known_issues", "commits")} if handoff else None),
        "latest_execution": latest_execution(store, project["project_id"], task["task_id"]) if task else None,
        "recent_sessions": recent_sessions(store, project["project_id"], reviews, session_limit),
        "overview_focus": overview_focus(store, project["project_id"]),
        "shared_rules": shared_rules(),
        "user_request": user_request or None,
        "continuation_instruction": "Resume from active_task.current_progress, active_task.next_action, and latest_handoff before exploring new work. Do not re-explore completed work or read full session transcripts.",
    }


def _value_or_unknown(value):
    return value if value not in (None, "") else "(unknown)"


def render_for_claude(pack):
    """Claude continuation prompt with an explicit identity header; no field is invented."""
    project = pack.get("project") or {}
    task = pack.get("active_task") or {}
    sessions = pack.get("recent_sessions") or []
    latest_session = sessions[0] if sessions else {}
    lines = [
        "AI: Claude",
        f"Mode: {_value_or_unknown(task.get('mode'))}",
        f"Project: {_value_or_unknown(project.get('name') or project.get('project_id'))}",
        f"Task: {_value_or_unknown(task.get('title') or task.get('task_id'))}",
        f"Conversation: {_value_or_unknown(latest_session.get('conversation_label'))}",
        f"Run/Session: {_value_or_unknown(latest_session.get('session_id'))}",
        "",
        f"Current progress: {_value_or_unknown(task.get('current_progress'))}",
        f"Next action: {_value_or_unknown(task.get('next_action'))}",
    ]
    handoff = pack.get("latest_handoff")
    if handoff:
        lines.append(f"Latest handoff next action: {_value_or_unknown(handoff.get('next_action'))}")
    execution = pack.get("latest_execution")
    if execution:
        lines.append(f"Latest execution: {execution.get('execution_id')} [{execution.get('status')}] via {execution.get('provider')}")
    lines.append("")
    lines.append(pack.get("continuation_instruction") or "")
    return "\n".join(lines)


def render_for_codex(pack):
    """Concise execution-oriented Codex continuation prompt; no field is invented."""
    project = pack.get("project") or {}
    task = pack.get("active_task") or {}
    lines = [f"Project: {_value_or_unknown(project.get('project_id'))}"]
    if task:
        lines.append(f"Task: {task.get('task_id')} - {_value_or_unknown(task.get('title'))}")
        lines.append(f"Progress: {_value_or_unknown(task.get('current_progress'))}")
        lines.append(f"Next action: {_value_or_unknown(task.get('next_action'))}")
    else:
        lines.append("Task: (none active)")
    handoff = pack.get("latest_handoff")
    if handoff:
        lines.append(f"Handoff next action: {_value_or_unknown(handoff.get('next_action'))}")
    execution = pack.get("latest_execution")
    if execution:
        lines.append(f"Latest execution: {execution.get('execution_id')} [{execution.get('status')}] via {execution.get('provider')}")
    lines.append(pack.get("continuation_instruction") or "")
    return "\n".join(lines)


RENDERERS = {"claude": render_for_claude, "codex": render_for_codex}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--request", default="")
    parser.add_argument("--max-sessions", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--for", dest="render_for", choices=("claude", "codex"), default=None, help="Render a provider continuation prompt instead of JSON; the JSON contract itself stays provider-neutral")
    args = parser.parse_args()
    try:
        result = context_pack(DriveRecords(build_service()), args.project_id, args.task_id, args.request, session_limit=args.max_sessions)
        if args.render_for:
            print(RENDERERS[args.render_for](result))
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
