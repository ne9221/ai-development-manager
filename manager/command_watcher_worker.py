"""Detached continuation for one Command Watcher provider launch.

The Scheduled Task watcher claims work and exits its short poll promptly;
this worker owns the existing claimed-command lifecycle until terminal.
"""

from __future__ import annotations

import argparse

from collectors.publish_drive import build_service
from manager.command_watcher import (
    _claude_account_registry,
    _explicit_account_id,
    _run_claimed_command,
    resolve_provider_runtime,
)
from manager.gcs_lock_registry import GCSLockRegistry
from manager.task_claims import task_claim_registry
from manager.tasks import DriveRecords, TaskError, validate


def run_claimed(project_id: str, task_id: str, execution_id: str) -> int:
    service = build_service()
    store = DriveRecords(service)
    command = store.get("commands", project_id, task_id)
    validate("command", command)
    if command.get("status") != "claimed" or command.get("execution_id") != execution_id:
        return 0
    task = store.get("tasks", project_id, task_id)
    validate("task", task)
    runtime = resolve_provider_runtime(command["provider"])
    if runtime is None:
        raise TaskError("unsupported provider")
    claude_accounts = _claude_account_registry()
    explicit_account_id = _explicit_account_id(command, task) if command["provider"] == "claude" else None
    origin = command.get("process_provenance") or {
        "caller_origin": "command_watcher_worker",
        "scheduler_invocation_id": None,
    }
    result = _run_claimed_command(
        store, service, command, runtime["launcher_factory"],
        GCSLockRegistry.from_environment, task_claim_registry,
        explicit_account_id, claude_accounts, origin,
        retry_count=command.get("retry_count", 0),
        retry_of=command.get("retry_of_execution_id"),
    )
    return 0 if result.get("status") == "completed" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("task_id")
    parser.add_argument("execution_id")
    args = parser.parse_args(argv)
    try:
        return run_claimed(args.project_id, args.task_id, args.execution_id)
    except (TaskError, OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
