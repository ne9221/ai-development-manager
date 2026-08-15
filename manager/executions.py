#!/usr/bin/env python3
"""Drive-backed execution lifecycle and provider-neutral quota deltas."""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from collectors.publish_drive import build_service
from manager.quota_reader import read_drive_status
from manager.session_identity import manager_session_key, parse_manager_session_key, session_provider_identity
from manager.task_claims import check_task_execution_claim
from manager.tasks import DriveRecords, TaskError, complete_task, now_iso, update_task, validate


MAX_SNAPSHOT_AGE_MINUTES = 60
MAX_CANCELLATION_REASON_CHARS = 300
STALE_AFTER_SECONDS = 15 * 60
MIN_HARD_TIMEOUT_SECONDS = 30 * 60
MAX_HARD_TIMEOUT_SECONDS = 2 * 60 * 60
MAX_RETRY_COUNT = 2


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def hard_timeout_seconds(expected_minutes):
    """Planning estimates never become authority; hard runtime remains bounded."""
    return min(MAX_HARD_TIMEOUT_SECONDS, max(MIN_HARD_TIMEOUT_SECONDS, float(expected_minutes) * 3 * 60))


def execution_health(execution, now=None):
    """Classify persisted lifecycle evidence without changing execution authority."""
    now = now or datetime.now(timezone.utc)
    if execution.get("status") != "running":
        return {"state": execution.get("status"), "reason": None, "over_expected": False}
    started = parse_time(execution["started_at"])
    heartbeat = parse_time(execution.get("heartbeat_at") or execution["started_at"])
    progress = parse_time(execution.get("progress_updated_at") or execution.get("heartbeat_at") or execution["started_at"])
    expected = float((execution.get("task_snapshot") or {}).get("expected_minutes") or 20) * 60
    elapsed = (now - started).total_seconds()
    idle = (now - progress).total_seconds()
    hard = execution.get("hard_timeout_at")
    if any(value < started or value > now + timedelta(minutes=5) for value in (heartbeat, progress)):
        return {"state": "attention", "reason": "activity_timestamp_inconsistent", "over_expected": elapsed > expected}
    if execution.get("session_id") and not execution.get("provider_evidence"):
        return {"state": "attention", "reason": "provider_evidence_missing", "over_expected": elapsed > expected}
    if hard and now >= parse_time(hard):
        return {"state": "attention", "reason": "hard_timeout_exceeded", "over_expected": elapsed > expected}
    if idle > STALE_AFTER_SECONDS:
        return {"state": "attention", "reason": "provider_progress_stale", "over_expected": elapsed > expected}
    return {"state": "healthy", "reason": None, "over_expected": elapsed > expected}


def heartbeat_execution(store, project_id, execution_id, event, at=None, provider_evidence=None, progress=True):
    """Persist orchestrator liveness and, only when true, meaningful provider progress."""
    execution = store.get("executions", project_id, execution_id)
    if execution.get("status") != "running":
        raise TaskError("heartbeat requires a running execution")
    timestamp = at or now_iso()
    execution["heartbeat_at"] = timestamp
    if progress:
        execution["progress_updated_at"] = timestamp
    execution["last_provider_event"] = str(event)[:100]
    if provider_evidence is not None:
        execution["provider_evidence"] = provider_evidence
    validate("execution", execution)
    return store.put("executions", project_id, execution_id, execution)


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


def _snapshot_reason(snapshot, boundary, side):
    if not snapshot:
        return f"unknown_due_to_missing_{side}"
    if snapshot.get("status") != "known":
        return "unknown_due_to_unreliable_snapshot"
    try:
        age = abs((parse_time(boundary) - parse_time(snapshot.get("captured_at"))).total_seconds() / 60)
    except (TypeError, ValueError):
        return "unknown_due_to_snapshot_timestamp"
    return "unknown_due_to_stale_snapshot" if age > MAX_SNAPSHOT_AGE_MINUTES else None


def _window_identity(window):
    value = window.get("window_id") or window.get("name")
    return str(value) if value is not None else None


def _unknown_window(name, reason):
    return {"name": name, "window_id": name, "status": "unknown", "attribution_status": "unknown", "used_percent_delta": None, "reason": reason, "attribution_reason": reason}


def quota_delta(before, after, started_at, completed_at):
    """Return conservative execution attribution evidence, never inferred usage."""
    before_reason = _snapshot_reason(before, started_at, "before")
    after_reason = _snapshot_reason(after, completed_at, "after")
    before_windows = {_window_identity(item): item for item in (before or {}).get("windows", []) if _window_identity(item)}
    after_windows = {_window_identity(item): item for item in (after or {}).get("windows", []) if _window_identity(item)}
    results = []
    for window_id in sorted(before_windows.keys() | after_windows.keys()):
        old, new = before_windows.get(window_id), after_windows.get(window_id)
        name = (new or old).get("name")
        if before_reason:
            result = _unknown_window(name, before_reason)
        elif after_reason:
            result = _unknown_window(name, after_reason)
        elif not old or not new:
            result = _unknown_window(name, "unknown_due_to_window_missing_before" if not old else "unknown_due_to_window_missing_after")
        elif old.get("used_percent") is None or new.get("used_percent") is None:
            result = _unknown_window(name, "unknown_due_to_usage_unavailable")
        else:
            old_reset = old.get("resets_at")
            try:
                crossed = bool(old_reset and parse_time(started_at) < parse_time(old_reset) <= parse_time(completed_at))
            except (TypeError, ValueError):
                crossed = True
            changed_reset = old_reset and new.get("resets_at") and old_reset != new.get("resets_at")
            delta = new["used_percent"] - old["used_percent"]
            if crossed or changed_reset:
                result = _unknown_window(name, "unknown_due_to_reset")
            elif delta < 0:
                result = _unknown_window(name, "unknown_due_to_usage_decreased")
            else:
                result = {"name": name, "window_id": window_id, "status": "known", "attribution_status": "known", "used_percent_delta": round(delta, 6), "reason": None, "attribution_reason": None}
        results.append(result)
    known = sum(item["status"] == "known" for item in results)
    status = "known" if results and known == len(results) else "partial" if known else "unknown"
    reason = None if status == "known" else before_reason or after_reason or "unknown_due_to_incomparable_windows"
    return {"status": status, "attribution_status": status, "attribution_reason": reason, "windows": results}


def task_snapshot(task):
    keys = (
        "title", "task_type", "complexity", "expected_minutes", "needs_repo_edit",
        "needs_research", "needs_browser", "parallelizable", "read_only", "scope",
        "constraints", "acceptance_criteria", "working_directory", "branch",
        "baseline_head", "allowed_paths", "execution_policies",
    )
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
        "account_id": session.get("account_id"),
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


def reserve_execution(store, project_id, task_id, execution_id, provider, quota_evidence, mode=None, effort=None, reserved_at=None, notes=None,
                      retry_count=0, retry_of_execution_id=None):
    """Create an idempotent execution reservation without starting work."""
    if not isinstance(quota_evidence, dict) or not quota_evidence:
        raise TaskError("quota_evidence must be a non-empty object")
    task = store.get("tasks", project_id, task_id)
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or not 0 <= retry_count <= MAX_RETRY_COUNT:
        raise TaskError(f"retry_count must be from 0 to {MAX_RETRY_COUNT}")
    if (retry_count == 0) != (retry_of_execution_id is None):
        raise TaskError("retry metadata must link every retry to its prior execution")
    expected = {
        "execution_id": execution_id, "task_id": task_id, "project_id": project_id,
        "provider": provider, "mode": mode or task.get("mode"), "effort": effort or task.get("effort"),
        "notes": list(notes or []), "task_snapshot": task_snapshot(task),
        "quota_evidence": quota_evidence,
        "retry_count": retry_count, "retry_of_execution_id": retry_of_execution_id,
    }
    try:
        existing = store.get("executions", project_id, execution_id)
    except KeyError:
        existing = None
    except TaskError as exc:
        if "found 0" not in str(exc) and "not found" not in str(exc):
            raise
        existing = None
    if existing is not None:
        validate("execution", existing)
        if existing.get("status") == "reserved" and all(existing.get(key) == value for key, value in expected.items()):
            return existing
        raise TaskError(f"execution_id already exists with a different reservation: {execution_id}")

    reserved_at = reserved_at or now_iso()
    execution = {
        **expected, "reserved_at": reserved_at, "started_at": None,
        "completed_at": None, "elapsed_minutes": None, "status": "reserved",
        "finished_at": None, "session_id": None, "provider_session_id": None, "account_id": None,
        "quota_before": None, "quota_after": None, "quota_delta": None,
        "source_confidence": None,
        "heartbeat_at": None, "progress_updated_at": None, "hard_timeout_at": None, "last_provider_event": None,
        "provider_evidence": None, "stale_at": None, "recovery_reason": None,
        "terminal_reason": None,
    }
    validate("execution", execution)
    return store.put("executions", project_id, execution_id, execution)


def start_execution(*_args, **_kwargs):
    raise TaskError("legacy start is retired; reserve first and use the authoritative running gate")


def cancel_reserved_execution(store, claim_registry, project_id, execution_id, reason, cancelled_at=None):
    """Cancel only a reservation that provably never acquired running authority."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_CANCELLATION_REASON_CHARS:
        raise TaskError("cancellation reason must be non-empty and at most 300 characters")
    execution = store.get("executions", project_id, execution_id)
    validate("execution", execution)
    if execution.get("status") == "cancelled":
        if execution.get("notes", [])[-1:] == [reason]:
            return execution
        raise TaskError("execution reservation is already cancelled with a different reason")
    if execution.get("status") != "reserved":
        raise TaskError("only a reserved execution can be cancelled before start")
    for field in ("started_at", "session_id", "provider_session_id", "access", "lease_evidence"):
        if execution.get(field) is not None:
            raise TaskError(f"reservation has running authority evidence: {field}")
    if check_task_execution_claim(claim_registry, project_id, execution["task_id"]) is not None:
        raise TaskError("reservation cannot be cancelled while a task claim exists")
    cancelled = {**execution, "status": "cancelled", "finished_at": cancelled_at or now_iso(),
                 "elapsed_minutes": 0, "access": None, "lease_evidence": None,
                 "cleanup_evidence": None, "notes": [*execution.get("notes", []), reason]}
    validate("execution", cancelled)
    store.put("executions", project_id, execution_id, cancelled)
    if store.get("executions", project_id, execution_id) != cancelled:
        raise TaskError("cancelled reservation persistence verification failed")
    return cancelled


def prepare_task_retry(store, claim_registry, project_id, task_id, prior_execution_id, retry_count=1):
    """Return one safely cleaned-up failed/interrupted task to ready."""
    task = store.get("tasks", project_id, task_id)
    validate("task", task)
    prior = store.get("executions", project_id, prior_execution_id)
    validate("execution", prior)
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or not 1 <= retry_count <= MAX_RETRY_COUNT:
        raise TaskError(f"retry_count must be from 1 to {MAX_RETRY_COUNT}")
    if retry_count != int(prior.get("retry_count", 0)) + 1:
        raise TaskError("retry_count must increment the prior execution exactly once")
    if prior.get("task_id") != task_id or prior.get("status") not in ("failed", "interrupted"):
        raise TaskError("retry requires the task's failed or interrupted prior execution")
    cleanup = prior.get("cleanup_evidence") or {}
    if cleanup.get("persistence") != "complete" or cleanup.get("task_claim_release") != "released":
        raise TaskError("retry requires complete persistence and released task claim")
    if prior.get("access") == "production_write" and cleanup.get("writer_release") != "released":
        raise TaskError("retry requires released writer authority")
    if check_task_execution_claim(claim_registry, project_id, task_id) is not None:
        raise TaskError("retry requires no active task claim")
    executions = store.list_records("executions", project_id)
    if any(item.get("task_id") == task_id and item.get("status") in ("running", "reserved") for item in executions):
        raise TaskError("retry requires all running or reserved executions to be resolved")
    context = dict(task.get("source_context") or {})
    active = context.get("active_execution_id")
    if task.get("status") == "ready" and active is None and task.get("blocked_reason") is None:
        if context.get("retry_count") == retry_count and context.get("retry_of_execution_id") == prior_execution_id:
            return task
        raise TaskError("ready task retry linkage does not match the prior execution")
    if task.get("status") != "blocked" or active != prior_execution_id:
        raise TaskError("retry requires a blocked task linked to the prior execution")
    context.pop("active_execution_id", None)
    context.update(retry_count=retry_count, retry_of_execution_id=prior_execution_id)
    ready = {**task, "status": "ready", "blocked_reason": None, "source_context": context,
             "updated_at": now_iso(), "current_progress": "Retry ready", "next_action": "Retry execution"}
    validate("task", ready)
    store.put("tasks", project_id, task_id, ready)
    if store.get("tasks", project_id, task_id) != ready:
        raise TaskError("retry task persistence verification failed")
    return ready


def persist_terminal(store, service, project_id, execution_id, status="completed", completed_at=None, note=None):
    """Persist only the provider's terminal execution outcome.

    Task, handoff, and authority cleanup are lifecycle orchestration concerns.
    """
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
        heartbeat_at=completed_at, progress_updated_at=completed_at,
        last_provider_event="terminal", terminal_reason=(note or status)[:300],
    )
    if note:
        execution["notes"].append(note)
    validate("execution", execution)
    store.put("executions", project_id, execution_id, execution)
    if store.get("executions", project_id, execution_id) != execution:
        raise TaskError("terminal execution persistence verification failed")
    return execution


def finish_execution(store, service, project_id, execution_id, status="completed", completed_at=None, note=None):
    execution = persist_terminal(store, service, project_id, execution_id, status, completed_at, note)
    if status == "completed":
        complete_task(store, project_id, execution["task_id"], note or f"Execution {execution_id} completed", execution["provider"], execution.get("session_id"))
    else:
        update_task(store, project_id, execution["task_id"], status="blocked", blocked_reason=f"Execution {status}: {note or 'no details'}", current_progress=f"Execution {execution_id} {status}", next_action="Review failure and decide whether to resume")
    return execution


def read_execution(store, project_id, execution_id):
    execution = store.get("executions", project_id, execution_id)
    validate("execution", execution)
    return execution


def list_executions(store, project_id):
    try:
        store.project_folder("executions", project_id, create=False)
    except TaskError:
        return []
    records = store.list_records("executions", project_id)
    for record in records:
        validate("execution", record)
    return records


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    reserve = sub.add_parser("reserve"); reserve.add_argument("project_id"); reserve.add_argument("task_id"); reserve.add_argument("execution_id"); reserve.add_argument("--provider", required=True); reserve.add_argument("--quota-evidence-json", required=True); reserve.add_argument("--mode"); reserve.add_argument("--effort"); reserve.add_argument("--note", action="append")
    start = sub.add_parser("start"); start.add_argument("project_id"); start.add_argument("task_id"); start.add_argument("execution_id"); start.add_argument("--provider", required=True); start.add_argument("--mode"); start.add_argument("--effort"); start.add_argument("--note", action="append")
    start.add_argument("--session-id", help="Canonical session key or legacy raw Codex session ID")
    for command in ("finish", "fail", "interrupt"):
        item = sub.add_parser(command); item.add_argument("project_id"); item.add_argument("execution_id"); item.add_argument("--note")
    read = sub.add_parser("read"); read.add_argument("project_id"); read.add_argument("execution_id")
    link = sub.add_parser("link-session"); link.add_argument("project_id"); link.add_argument("execution_id"); link.add_argument("session_id"); link.add_argument("--provider")
    args = parser.parse_args()
    try:
        if args.command == "start":
            return start_execution()
        service = build_service(); store = DriveRecords(service)
        if args.command == "reserve":
            result = reserve_execution(store, args.project_id, args.task_id, args.execution_id, args.provider, json.loads(args.quota_evidence_json), args.mode, args.effort, notes=args.note)
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
