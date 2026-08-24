"""Small, local audit trail for Scheduled Task wrappers.

This is deliberately evidence, not a scheduling authority: without an OS task
event the wrapper can only say ``unknown`` trigger origin.
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from manager.codex_launcher import process_creation_identity

ENV_ID = "ADM_SCHEDULER_INVOCATION_ID"
ENV_TASK = "ADM_SCHEDULER_TASK_NAME"
ENV_WRAPPER_PID = "ADM_SCHEDULER_WRAPPER_PID"
ENV_TRIGGER = "ADM_SCHEDULER_TRIGGER_ORIGIN"
ORIGINS = frozenset(("scheduled", "manual", "unknown"))


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_id(value):
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return None


def _path(manager_home, invocation_id):
    return Path(manager_home) / "runtime" / "scheduler-invocations" / f"{invocation_id}.json"


def _write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(record, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def context_from_environment(environ=None):
    environ = os.environ if environ is None else environ
    invocation_id = _safe_id(environ.get(ENV_ID))
    task_name = environ.get(ENV_TASK)
    try:
        wrapper_pid = int(environ.get(ENV_WRAPPER_PID, ""))
    except ValueError:
        wrapper_pid = 0
    # The wrapper must be this Python process's live parent.  An arbitrary
    # environment value alone never establishes a scheduler context.
    if not invocation_id or not isinstance(task_name, str) or not task_name or wrapper_pid != os.getppid():
        return None
    wrapper_identity = process_creation_identity(wrapper_pid)
    if not wrapper_identity:
        return None
    return {
        "scheduler_invocation_id": invocation_id,
        "task_name": task_name[:200],
        # No wrapper environment variable is OS trigger evidence.  Until a
        # Task Scheduler event is correlated, this must remain unknown.
        "trigger_origin": "unknown",
        "wrapper_pid": wrapper_pid,
        "wrapper_creation_identity": wrapper_identity,
        "python_pid": os.getpid(),
        "python_creation_identity": process_creation_identity(os.getpid()),
        "caller_origin": "watcher_poll",
    }


def start(manager_home, component, environ=None):
    context = context_from_environment(environ)
    if context is None:
        return None
    record = {**context, "component": component, "started_at": now_iso(), "ended_at": None, "status": "running"}
    _write(_path(manager_home, context["scheduler_invocation_id"]), record)
    return context


def finish(manager_home, context, status):
    if not context:
        return
    path = _path(manager_home, context["scheduler_invocation_id"])
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    record.update(ended_at=now_iso(), status=status[:40])
    _write(path, record)


def command_origin(context=None):
    if not context:
        return {"caller_origin": "direct_or_unknown", "scheduler_invocation_id": None}
    return {"caller_origin": "watcher_poll", "scheduler_invocation_id": context["scheduler_invocation_id"]}


def evidence_status(command, execution):
    """Return PASS/FAIL/UNKNOWN; legacy or incomplete records never pass."""
    command_evidence = (command or {}).get("process_provenance") or {}
    provider = (execution or {}).get("provider_evidence") or {}
    invocation_id = command_evidence.get("scheduler_invocation_id")
    if not invocation_id or not provider.get("scheduler_invocation_id"):
        return "UNKNOWN"
    if invocation_id != provider.get("scheduler_invocation_id"):
        return "FAIL"
    required = ("launcher_pid", "launcher_creation_identity", "provider_pid", "provider_creation_identity", "provider_parent_identity")
    return "PASS" if all(provider.get(key) for key in required) else "UNKNOWN"
