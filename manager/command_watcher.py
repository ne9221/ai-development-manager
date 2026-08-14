"""Bounded, Drive-backed command watcher that delegates every launch to execution_runner."""

import argparse
import json
import os
import socket
import time
from datetime import datetime, timezone

from collectors.publish_drive import build_service
from manager.codex_launcher import CodexLauncher
from manager.execution_runner import launch_task
from manager.gcs_lock_registry import GCSLockRegistry
from manager.runtime_bridge import all_projects
from manager.task_claims import task_claim_registry
from manager.tasks import DriveRecords, TaskError, now_iso, validate


POLL_SECONDS = 60
MAX_POLL_SECONDS = 900
CLAIM_TIMEOUT_SECONDS = 20 * 60
MAX_COMMANDS_PER_POLL = 4


def execution_id(command):
    return f"command-{command['command_id']}"


def _result(status, execution_id_value, session_id=None, error_kind=None):
    return {"status": status, "execution_id": execution_id_value, "session_id": session_id, "error_kind": error_kind}


def _write(store, command):
    validate("command", command)
    return store.put("commands", command["project_id"], command["command_id"], command)


def _claimed(command):
    # ponytail: deterministic claim content lets competing watchers converge; task claim remains launch authority.
    return {**command, "status": "claimed", "execution_id": execution_id(command),
            "claimed_at": now_iso(), "completed_at": None, "result": None}


def _terminal(command, status, result):
    return {**command, "status": status, "completed_at": now_iso(), "result": result}


def _claim_expired(command, now=None):
    try:
        claimed = datetime.fromisoformat(command["claimed_at"].replace("Z", "+00:00"))
        return (now or datetime.now(timezone.utc) - claimed).total_seconds() > CLAIM_TIMEOUT_SECONDS
    except (AttributeError, TypeError, ValueError):
        return True


def _existing_terminal(store, command):
    try:
        execution = store.get("executions", command["project_id"], command["execution_id"])
        validate("execution", execution)
    except TaskError:
        return None
    if execution.get("status") not in ("completed", "failed", "interrupted"):
        return None
    return _terminal(command, "completed" if execution["status"] == "completed" else "failed",
                     _result(execution["status"], command["execution_id"], execution.get("session_id")))


def process_command(store, service, command, launcher_factory=CodexLauncher, writer_factory=GCSLockRegistry.from_environment,
                    claim_factory=task_claim_registry):
    """Claim/reconcile one command; a claimed command is never automatically relaunched."""
    try:
        validate("command", command)
    except TaskError:
        return {"status": "rejected"}
    if command["status"] in ("completed", "failed"):
        return {"status": command["status"], "skipped": True}
    if command["status"] in ("claimed", "running"):
        terminal = _existing_terminal(store, command)
        if terminal:
            _write(store, terminal)
            return {"status": terminal["status"], "reconciled": True}
        if command["status"] == "claimed" and _claim_expired(command):
            failed = _terminal(command, "failed", _result("error", command["execution_id"], error_kind="claim_timeout"))
            _write(store, failed)
            return {"status": "failed", "reconciled": True}
        return {"status": command["status"], "skipped": True}
    if command["status"] != "queued" or command["provider"] != "codex":
        return {"status": "rejected"}

    claimed = _claimed(command)
    _write(store, claimed)
    try:
        task = store.get("tasks", claimed["project_id"], claimed["task_id"])
        validate("task", task)
        claim_registry = claim_factory(os.environ.get("ADM_LOCK_GCS_BUCKET"), claimed["project_id"], claimed["task_id"])
        writer_registry = None if task.get("read_only") else writer_factory()
        running = {**claimed, "status": "running"}
        _write(store, running)
        outcome = launch_task(store, service, writer_registry, claim_registry, launcher_factory(),
                              claimed["project_id"], claimed["task_id"], claimed["execution_id"], claimed["model"])
        terminal = outcome["terminal"]["execution"]
        dispatch = outcome["dispatch"]
        selected = {**running, "provider": dispatch["provider"], "model": dispatch["model"] or claimed["model"],
                    "fallback_model": dispatch["fallback_model"] or claimed["fallback_model"], "mode": dispatch["mode"],
                    "effort": dispatch["effort"], "selection_reason": dispatch["selection_reason"],
                    "quota_evidence": dispatch["quota_evidence"]}
        final = _terminal(selected, "completed" if terminal["status"] == "completed" else "failed",
                          _result(terminal["status"], claimed["execution_id"], outcome["session"].get("session_id")))
    except Exception as exc:
        terminal = _existing_terminal(store, claimed)
        if terminal:
            final = terminal
        else:
            try:
                existing = store.get("executions", claimed["project_id"], claimed["execution_id"])
                if existing.get("status") in ("reserved", "running"):
                    return {"status": "running", "skipped": True}
            except TaskError:
                pass
            kind = getattr(exc, "classification", None) or type(exc).__name__
            final = _terminal(claimed, "failed", _result("error", claimed["execution_id"], error_kind=str(kind)[:100]))
    _write(store, final)
    return {"status": final["status"], "execution_id": claimed["execution_id"]}


def poll_once(store, service, **factories):
    results = []
    for project in all_projects(store):
        try:
            commands = store.list_records("commands", project["project_id"])
        except TaskError:
            continue
        for command in commands:
            if len(results) == MAX_COMMANDS_PER_POLL:
                return results
            results.append(process_command(store, service, command, **factories))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll Drive commands and run Codex through ADM execution_runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=POLL_SECONDS)
    args = parser.parse_args(argv)
    if not 10 <= args.interval_seconds <= MAX_POLL_SECONDS:
        raise SystemExit("interval-seconds must be from 10 to 900")
    while True:
        try:
            service = build_service()
            result = poll_once(DriveRecords(service), service)
            print(json.dumps({"status": "ok", "host": socket.gethostname()[:100], "commands": result}, separators=(",", ":")))
        except Exception:
            print(json.dumps({"status": "unavailable"}, separators=(",", ":")))
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
