#!/usr/bin/env python3
"""Small Drive-backed Development Overview management layer."""

import argparse
import json
import sys

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords, TaskError, now_iso, validate


OVERVIEW_VERSION = "1.0"
OVERVIEW_NAME = "overview"
STATUSES = ("pending", "in_progress", "awaiting_validation", "completed", "deferred", "cancelled", "merged")
PRIORITIES = ("high", "medium", "low")


def empty_overview(project_id):
    return {"project_id": project_id, "version": OVERVIEW_VERSION, "updated_at": now_iso(), "items": []}


def _item(item_id, title, status="pending", priority="medium", current_progress="Not started", next_action="Define next action", task_ids=None, merged_into=None, notes=None):
    return {
        "item_id": item_id, "title": title, "status": status, "priority": priority,
        "current_progress": current_progress, "next_action": next_action,
        "task_ids": task_ids or [], "merged_into": merged_into, "notes": notes or [],
    }


def initial_overview(project_id):
    overview = empty_overview(project_id)
    if project_id == "ai-development-manager":
        overview["items"] = [
            _item("P03", "Codex Session Organizer", "awaiting_validation", "high", "Implementation complete", "Run Drive production verification when OAuth is available"),
            _item("P04", "Global AI Development Rules", "completed", "medium", "Version 0.1.3 complete", "No action"),
            _item("P05", "Development Overview Runtime", "in_progress", "high", "Implementing the first Drive-backed overview runtime", "Run affected tests and verify Drive when OAuth is available"),
        ]
    validate("overview", overview)
    return overview


def read_overview(store, project_id):
    overview = store.get("overviews", project_id, OVERVIEW_NAME)
    validate("overview", overview)
    return overview


def initialize_overview(store, project_id):
    try:
        return read_overview(store, project_id)
    except (TaskError, KeyError):
        overview = initial_overview(project_id)
        return store.put("overviews", project_id, OVERVIEW_NAME, overview)


def _find(overview, item_id):
    for item in overview["items"]:
        if item["item_id"] == item_id:
            return item
    raise TaskError(f"overview item not found: {item_id}")


def _save(store, overview):
    overview["updated_at"] = now_iso()
    validate("overview", overview)
    return store.put("overviews", overview["project_id"], OVERVIEW_NAME, overview)


def add_item(store, project_id, item_id, title, priority="medium", current_progress="Not started", next_action="Define next action", task_ids=None, notes=None):
    overview = read_overview(store, project_id)
    if any(item["item_id"] == item_id for item in overview["items"]):
        raise TaskError(f"overview item already exists: {item_id}")
    overview["items"].append(_item(item_id, title, priority=priority, current_progress=current_progress, next_action=next_action, task_ids=task_ids, notes=notes))
    return _save(store, overview)


def update_item(store, project_id, item_id, **changes):
    overview = read_overview(store, project_id)
    item = _find(overview, item_id)
    allowed = {"title", "status", "priority", "current_progress", "next_action", "task_ids", "merged_into", "notes"}
    item.update({key: value for key, value in changes.items() if key in allowed and value is not None})
    return _save(store, overview)


def summary(overview):
    groups = {status: [] for status in STATUSES}
    for item in overview["items"]:
        groups[item["status"]].append({key: item[key] for key in ("item_id", "title", "priority", "current_progress", "next_action")})
    return {"project_id": overview["project_id"], "updated_at": overview["updated_at"], "by_status": groups}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("read", "summary", "init"):
        sub.add_parser(command).add_argument("project_id")
    add = sub.add_parser("item-add")
    add.add_argument("project_id"); add.add_argument("item_id"); add.add_argument("--title", required=True)
    add.add_argument("--priority", choices=PRIORITIES, default="medium"); add.add_argument("--progress", default="Not started"); add.add_argument("--next-action", default="Define next action")
    add.add_argument("--task-id", action="append", dest="task_ids"); add.add_argument("--note", action="append", dest="notes")
    update = sub.add_parser("item-update")
    update.add_argument("project_id"); update.add_argument("item_id"); update.add_argument("--title"); update.add_argument("--status", choices=STATUSES); update.add_argument("--priority", choices=PRIORITIES)
    update.add_argument("--progress", dest="current_progress"); update.add_argument("--next-action", dest="next_action"); update.add_argument("--merged-into"); update.add_argument("--task-id", action="append", dest="task_ids"); update.add_argument("--note", action="append", dest="notes")
    args = parser.parse_args()
    try:
        store = DriveRecords(build_service())
        if args.command == "read": result = read_overview(store, args.project_id)
        elif args.command == "summary": result = summary(read_overview(store, args.project_id))
        elif args.command == "init": result = initialize_overview(store, args.project_id)
        elif args.command == "item-add": result = add_item(store, args.project_id, args.item_id, args.title, args.priority, args.progress, args.next_action, args.task_ids, args.notes)
        else: result = update_item(store, args.project_id, args.item_id, **{key: value for key, value in vars(args).items() if key not in {"command", "project_id", "item_id"}})
        print(json.dumps(result, indent=2, ensure_ascii=False)); return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
