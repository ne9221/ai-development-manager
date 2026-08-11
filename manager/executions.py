#!/usr/bin/env python3
"""Drive-backed execution lifecycle and provider-neutral quota deltas."""

import argparse
import json
import sys
from datetime import datetime, timezone

from collectors.publish_drive import build_service
from manager.quota_reader import read_drive_status
from manager.session_identity import manager_session_key, parse_manager_session_key, session_provider_identity
from manager.tasks import DriveRecords, TaskError, complete_task, now_iso, update_task, validate


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def quota_snapshot(document, provider_id):
    provider = next((item for item in document.get("providers", []) if item.get("provider") == provider_id), None)
    if not provider:
        return {"status": "unknown", "captured_at": document.get("generated_at"), "windows": []}
    return {
        "status": "known" if provider.get("source_type") == "official" and provider.get("windows") else "unknown",
        "captured_at": provider.get("last_updated") or document.get("generated_at"),
        "source_type": provider.get("source_type"),
        "confidence": provider.get("confidence", "unknown"),
        "windows": provider.get("windows", []),
    }


def quota_delta(before, after, started_at, completed_at):
    before_windows = {item["name"]: item for item in before.get("windows", [])}
    after_windows = {item["name"]: item for item in after.get("windows", [])}
    results = []
    for name in sorted(before_windows.keys() | after_windows.keys()):
        old, new = before_windows.get(name), after_windows.get(name)
        result = {"name": name, "status": "unknown", "used_percent_delta": None}
        if not old or not new:
            result["reason"] = "window_missing_before" if not old else "window_missing_after"
        elif old.get("used_percent") is None or new.get("used_percent") is None:
            result["reason"] = "usage_unknown"
        else:
            old_reset = old.get("resets_at")
            crossed = bool(old_reset and parse_time(started_at) < parse_time(old_reset) <= parse_time(completed_at))
            changed_reset = old_reset and new.get("resets_at") and old_reset != new.get("resets_at")
            delta = new["used_percent"] - old["used_percent"]
            if crossed or (delta < 0 and changed_reset):
                result["reason"] = "quota_reset_crossed"
            elif delta < 0:
                result["reason"] = "usage_decreased_unknown"
            else:
                result.update(status="known", used_percent_delta=round(delta, 6), reason=None)
        results.append(result)
    known = sum(item["status"] == "known" for item in results)
    status = "known" if results and known == len(results) else "partial" if known else "unknown"
    return {"status": status, "windows": results}


def task_snapshot(task):
    keys = ("task_type", "complexity", "needs_repo_edit", "needs_research", "needs_browser", "parallelizable")
    return {key: task.get(key) for key in keys if key in task}


def session_link_fields(execution, session):
    """Validate and return the one primary provider conversation for a run."""
    provider, provider_session_id = session_provider_identity(session)
    if execution["provider"] != provider:
        raise TaskError("execution provider does not match session provider")
    if session.get("project_id") != execution["project_id"]:
        raise TaskError("execution project does not match session project")
    return {
        "session_id": manager_session_key(provider, provider_session_id),
        "provider_session_id": provider_session_id,
    }


def _linked_session_key(execution):
    value = execution.get("session_id")
    if not value:
        return None
    if parse_manager_session_key(value):
        return value
    return manager_session_key(execution["provider"], execution.get("provider_session_id") or value)


def link_execution_session(store, project_id, execution_id, session):
    """Attach one primary session after a run starts; repeat links are idempotent."""
    execution = store.get("executions", project_id, execution_id)
    fields = session_link_fields(execution, session)
    existing = _linked_session_key(execution)
    if existing:
        if existing == fields["session_id"]:
            return execution
        raise TaskError("execution is already linked to another session")
    execution.update(fields)
    validate("execution", execution)
    return store.put("executions", project_id, execution_id, execution)


def read_session_for_link(store, project_id, session_ref, provider=None):
    """Read canonical registry records and fall back to legacy Codex raw keys."""
    parsed = parse_manager_session_key(session_ref)
    if parsed:
        provider, provider_session_id = parsed
        key = session_ref
    else:
        provider_session_id = session_ref
        key = manager_session_key(provider or "codex", provider_session_id)
    for location, name in ((project_id, key), (project_id, provider_session_id), ("_unclassified", key), ("_unclassified", provider_session_id)):
        try:
            session = store.get("sessions", location, name)
            if session_provider_identity(session) == (provider, provider_session_id):
                return session
        except (TaskError, KeyError):
            pass
    raise TaskError(f"session not found for link: {session_ref}")


def start_execution(store, service, project_id, task_id, execution_id, provider, mode=None, effort=None, started_at=None, notes=None, session=None):
    task = store.get("tasks", project_id, task_id)
    started_at = started_at or now_iso()
    before = quota_snapshot(read_drive_status(service=service), provider)
    execution = {
        "execution_id": execution_id, "task_id": task_id, "project_id": project_id,
        "provider": provider, "mode": mode or task.get("mode"), "effort": effort or task.get("effort"),
        "started_at": started_at, "completed_at": None, "elapsed_minutes": None, "status": "running",
        "finished_at": None, "session_id": None, "provider_session_id": None,
        "quota_before": before, "quota_after": None, "quota_delta": None,
        "source_confidence": before.get("confidence", "unknown"), "notes": notes or [],
        "task_snapshot": task_snapshot(task),
    }
    if session:
        execution.update(session_link_fields(execution, session))
    validate("execution", execution)
    store.put("executions", project_id, execution_id, execution)
    update_task(store, project_id, task_id, status="in_progress", assigned_provider=provider, blocked_reason=None, current_progress=f"Execution {execution_id} running", next_action="Finish or interrupt execution")
    return execution


def finish_execution(store, service, project_id, execution_id, status="completed", completed_at=None, note=None):
    if status not in ("completed", "failed", "interrupted"):
        raise TaskError(f"invalid terminal execution status: {status}")
    execution = store.get("executions", project_id, execution_id)
    if execution["status"] != "running":
        raise TaskError("execution is not running")
    completed_at = completed_at or now_iso()
    elapsed = max(0, (parse_time(completed_at) - parse_time(execution["started_at"])).total_seconds() / 60)
    after = quota_snapshot(read_drive_status(service=service), execution["provider"])
    execution.update(
        completed_at=completed_at, elapsed_minutes=round(elapsed, 6), status=status,
        finished_at=completed_at,
        quota_after=after, quota_delta=quota_delta(execution["quota_before"], after, execution["started_at"], completed_at),
    )
    if note:
        execution["notes"].append(note)
    validate("execution", execution)
    store.put("executions", project_id, execution_id, execution)
    if status == "completed":
        complete_task(store, project_id, execution["task_id"], note or f"Execution {execution_id} completed", execution["provider"], execution_id)
    else:
        update_task(store, project_id, execution["task_id"], status="blocked", blocked_reason=f"Execution {status}: {note or 'no details'}", current_progress=f"Execution {execution_id} {status}", next_action="Review failure and decide whether to resume")
    return execution


def read_execution(store, project_id, execution_id):
    execution = store.get("executions", project_id, execution_id)
    validate("execution", execution)
    return execution


def list_executions(store, project_id):
    try:
        parent = store.project_folder("executions", project_id, create=False)
    except TaskError:
        return []
    records = []
    for item in store.children(parent):
        if item["name"].endswith(".json"):
            records.append(read_execution(store, project_id, item["name"][:-5]))
    return records


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start"); start.add_argument("project_id"); start.add_argument("task_id"); start.add_argument("execution_id"); start.add_argument("--provider", required=True); start.add_argument("--mode"); start.add_argument("--effort"); start.add_argument("--note", action="append")
    start.add_argument("--session-id", help="Canonical session key or legacy raw Codex session ID")
    for command in ("finish", "fail", "interrupt"):
        item = sub.add_parser(command); item.add_argument("project_id"); item.add_argument("execution_id"); item.add_argument("--note")
    read = sub.add_parser("read"); read.add_argument("project_id"); read.add_argument("execution_id")
    link = sub.add_parser("link-session"); link.add_argument("project_id"); link.add_argument("execution_id"); link.add_argument("session_id"); link.add_argument("--provider")
    args = parser.parse_args()
    try:
        service = build_service(); store = DriveRecords(service)
        if args.command == "start":
            session = read_session_for_link(store, args.project_id, args.session_id, args.provider) if args.session_id else None
            result = start_execution(store, service, args.project_id, args.task_id, args.execution_id, args.provider, args.mode, args.effort, notes=args.note, session=session)
        elif args.command == "read": result = read_execution(store, args.project_id, args.execution_id)
        elif args.command == "link-session": result = link_execution_session(store, args.project_id, args.execution_id, read_session_for_link(store, args.project_id, args.session_id, args.provider))
        else:
            terminal = {"finish": "completed", "fail": "failed", "interrupt": "interrupted"}[args.command]
            result = finish_execution(store, service, args.project_id, args.execution_id, terminal, note=args.note)
        print(json.dumps(result, indent=2)); return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
