#!/usr/bin/env python3
"""Bounded, read-only continuation context from the Drive runtime SSOT."""

import argparse
import json
import re

from collectors.publish_drive import build_service
from manager.runtime_bridge import active_task, resolve_project
from manager.sessions import apply_manual_reviews, list_review_records, prompt_snippet
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
        records[record["session_id"]] = record
    for record in _records(store, "sessions", "_unclassified"):
        records.setdefault(record["session_id"], record)
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
        "contract_version": "1.0",
        "project": {key: project.get(key) for key in ("project_id", "name", "repo", "default_branch", "working_directory")},
        "active_task": ({key: task.get(key) for key in ("task_id", "title", "status", "current_progress", "next_action", "scope", "acceptance_criteria")} if task else None),
        "latest_handoff": ({key: handoff.get(key) for key in ("handoff_id", "from_provider", "from_session", "reason", "current_state", "next_action", "minimal_context", "tests", "known_issues")} if handoff else None),
        "recent_sessions": recent_sessions(store, project["project_id"], reviews, session_limit),
        "overview_focus": overview_focus(store, project["project_id"]),
        "shared_rules": shared_rules(),
        "user_request": user_request or None,
        "continuation_instruction": "Resume from active_task.current_progress, active_task.next_action, and latest_handoff before exploring new work. Do not re-explore completed work or read full session transcripts.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--request", default="")
    parser.add_argument("--max-sessions", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = context_pack(DriveRecords(build_service()), args.project_id, args.task_id, args.request, session_limit=args.max_sessions)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
