"""Small, local audit trail for Scheduled Task wrappers."""

import json
import os
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manager.codex_launcher import process_creation_identity, process_identity_state

ENV_ID = "ADM_SCHEDULER_INVOCATION_ID"
ENV_TASK = "ADM_SCHEDULER_TASK_NAME"
ENV_WRAPPER_PID = "ADM_SCHEDULER_WRAPPER_PID"
ENV_WRAPPER_PARENT_PID = "ADM_SCHEDULER_WRAPPER_PARENT_PID"
ENV_TRIGGER = "ADM_SCHEDULER_TRIGGER_ORIGIN"
ORIGINS = frozenset(("scheduled", "manual", "unknown"))
EVENT_LOG = "Microsoft-Windows-TaskScheduler/Operational"
EVENT_TIMEOUT_SECONDS = 5
EVENT_WINDOW_SECONDS = 120
PARENT_CHILD_START_MAX_SECONDS = 5
EVENT_TIMESTAMP_TOLERANCE_SECONDS = 1
EVENT129_INSTANCE_LINK_MAX_SECONDS = 1
MAX_EVENTS = 200
RETAIN_DAYS = 7
MAX_RECORDS = 10000


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


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _task_name(value):
    """Canonical Task Scheduler identity; only its root separator is optional."""
    return value.lstrip("\\") if isinstance(value, str) else None


def _data_value(data, *names):
    return next((data.get(name.casefold()) for name in names if data.get(name.casefold())), None)


def _event(raw):
    """Keep only the small fields needed for correlation; malformed XML is ignored."""
    try:
        root = ET.fromstring(raw["xml"])
        system = next(node for node in root if node.tag.endswith("System"))
        data = {node.attrib.get("Name", "").casefold(): (node.text or "") for node in root.iter()
                if node.tag.endswith("Data")}
        record_id = int(raw.get("record_id") or next(node.text for node in system if node.tag.endswith("EventRecordID")))
        event_id = int(raw.get("event_id") or next(node.text for node in system if node.tag.endswith("EventID")))
        created = _parse_time(raw.get("time_created") or next(node.attrib.get("SystemTime") for node in system.iter()
                                                                  if node.tag.endswith("TimeCreated")))
    except (KeyError, TypeError, ValueError, ET.ParseError, StopIteration):
        return None
    task_name = _data_value(data, "TaskName", "Task")
    instance_id = _data_value(data, "InstanceId")
    try:
        process_id = int(_data_value(data, "ProcessID", "ProcessId", "PID") or "")
    except ValueError:
        process_id = None
    text = " ".join(str(value) for value in (*data.values(), raw.get("message", "")))
    return {"record_id": record_id, "event_id": event_id, "time_created": created,
            "task_name": task_name, "instance_id": instance_id, "process_id": process_id,
            "text": text[:1000], "action_executable": _data_value(data, "ActionName", "Path")}


def _powershell_events(start, end, max_events):
    # Get-WinEvent is read-only. ConvertTo-Json keeps the subprocess boundary small.
    script = (
        "$ErrorActionPreference='Stop';"
        f"$f=@{{LogName='{EVENT_LOG}';StartTime=[datetime]::Parse('{start.isoformat()}');EndTime=[datetime]::Parse('{end.isoformat()}')}};"
        f"Get-WinEvent -FilterHashtable $f -MaxEvents {max_events}|%{{[pscustomobject]@{{record_id=$_.RecordId;event_id=$_.Id;time_created=$_.TimeCreated.ToUniversalTime().ToString('o');message=$_.Message;xml=$_.ToXml()}}}}|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=EVENT_TIMEOUT_SECONDS, check=True)
    value = json.loads(completed.stdout or "[]")
    return value if isinstance(value, list) else [value]


def read_os_events(started_at, *, reader=None):
    """Read a bounded window, returning UNKNOWN on unavailable/access-denied paths."""
    start = _parse_time(started_at) if isinstance(started_at, str) else started_at
    if os.name != "nt" or start is None:
        return "UNKNOWN", [], "windows_operational_log_unavailable"
    try:
        raw = (reader or _powershell_events)(start - timedelta(seconds=EVENT_WINDOW_SECONDS),
                                             start + timedelta(seconds=EVENT_WINDOW_SECONDS), MAX_EVENTS)
        lower, upper = start - timedelta(seconds=EVENT_WINDOW_SECONDS), start + timedelta(seconds=EVENT_WINDOW_SECONDS)
        events = [event for item in raw if isinstance(item, dict) and (event := _event(item))
                  and event["time_created"] and lower <= event["time_created"] <= upper]
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError, TimeoutError):
        return "UNKNOWN", [], "windows_operational_log_unavailable"
    return "PASS", events, None


def _origin(event):
    text = event["text"].lower()
    if "time trigger" in text or "scheduled time" in text:
        return "scheduled_time"
    return "unknown"


def _within_parent_child_causality(event, identity):
    """Accept only the short Event129 action-parent -> wrapper-child interval."""
    if not identity.startswith("windows-filetime:"):
        return True  # Test/non-Windows identity; live identity was still verified above.
    try:
        created = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(identity.split(":", 1)[1]) / 10)
    except (ValueError, OverflowError):
        return False
    delta = event["time_created"] - created
    return (-timedelta(seconds=PARENT_CHILD_START_MAX_SECONDS)
            <= delta <= timedelta(seconds=EVENT_TIMESTAMP_TOLERANCE_SECONDS))


def _action_matches_trigger(action, trigger):
    """Link Event129 to its Event107 instance when Windows omits InstanceId."""
    if action["instance_id"]:
        return action["instance_id"] == trigger["instance_id"]
    delta = action["time_created"] - trigger["time_created"]
    return timedelta() <= delta <= timedelta(seconds=EVENT129_INSTANCE_LINK_MAX_SECONDS)


def correlate_os_evidence(context, started_at, *, reader=None):
    """Correlate only a unique Event 129 PID link plus its 107/100 instance."""
    status, events, reason = read_os_events(started_at, reader=reader)
    if status != "PASS":
        return {"status": "UNKNOWN", "reason": reason}
    if not context.get("wrapper_parent_pid"):
        return {"status": "UNKNOWN", "reason": "missing_wrapper_parent_pid"}
    if not context.get("wrapper_pid") or not context.get("wrapper_creation_identity"):
        return {"status": "UNKNOWN", "reason": "missing_wrapper_pid_identity"}
    if process_identity_state(context["wrapper_pid"], context["wrapper_creation_identity"]) != "live":
        return {"status": "FAIL", "reason": "wrapper_creation_identity_mismatch"}
    task_name = _task_name(context["task_name"])
    matching = [event for event in events if _task_name(event["task_name"]) == task_name]
    actions = [event for event in matching if event["event_id"] == 129]
    linked = [event for event in actions if event["process_id"] == context["wrapper_parent_pid"]
              and _within_parent_child_causality(event, context["wrapper_creation_identity"])]
    ignored = [{"task_name": event["task_name"], "instance_id": event["instance_id"],
                "event_record_id": event["record_id"], "timestamp": event["time_created"].isoformat(),
                "reason": "already_running"} for event in matching if event["event_id"] == 322]
    if actions and not linked:
        return {"status": "FAIL", "reason": "event_129_process_id_mismatch", "ignore_new_events": ignored}
    triggers = [event for event in matching if event["event_id"] == 107 and _origin(event) == "scheduled_time"]
    candidates = []
    for action in linked:
        trigger = [event for event in triggers if _action_matches_trigger(action, event)]
        if len(trigger) == 1:
            candidates.append((action, trigger[0]))
    if len(candidates) != 1:
        return {"status": "UNKNOWN", "reason": "missing_or_ambiguous_os_instance", "ignore_new_events": ignored}
    action, trigger = candidates[0]
    return {"status": "PASS", "task_name": task_name, "instance_id": trigger["instance_id"],
            "trigger_event_record_id": trigger["record_id"], "trigger_event_id": trigger["event_id"],
            "trigger_time": trigger["time_created"].isoformat(), "action_event_record_id": action["record_id"],
            "action_process_id": action["process_id"], "action_executable": action["action_executable"],
            "trigger_origin": _origin(trigger), "reason": "event_129_pid_and_instance_link", "ignore_new_events": ignored}


def _cleanup(directory):
    if not directory.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETAIN_DAYS)
    records = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8")); started = _parse_time(record.get("started_at"))
            running = record.get("status") == "running"
        except (OSError, ValueError, AttributeError):
            started = None; running = True
        records.append((started or datetime.min.replace(tzinfo=timezone.utc), path, running))
    removable = [(started, path) for started, path, running in records if not running]
    for _, path in sorted(removable)[:-MAX_RECORDS]:
        path.unlink(missing_ok=True)
    for started, path in removable:
        if started < cutoff:
            path.unlink(missing_ok=True)


def context_from_environment(environ=None):
    environ = os.environ if environ is None else environ
    invocation_id = _safe_id(environ.get(ENV_ID)); task_name = environ.get(ENV_TASK)
    try:
        wrapper_pid = int(environ.get(ENV_WRAPPER_PID, ""))
        wrapper_parent_pid = int(environ.get(ENV_WRAPPER_PARENT_PID, ""))
    except ValueError:
        wrapper_pid = wrapper_parent_pid = 0
    if (not invocation_id or not isinstance(task_name, str) or not task_name or wrapper_pid != os.getppid()
            or wrapper_parent_pid < 1):
        return None
    wrapper_identity = process_creation_identity(wrapper_pid)
    if not wrapper_identity:
        return None
    return {"scheduler_invocation_id": invocation_id, "task_name": task_name[:200], "trigger_origin": "unknown",
            "wrapper_pid": wrapper_pid, "wrapper_parent_pid": wrapper_parent_pid,
            "wrapper_creation_identity": wrapper_identity, "python_pid": os.getpid(),
            "python_creation_identity": process_creation_identity(os.getpid()), "caller_origin": "watcher_poll"}


def start(manager_home, component, environ=None, *, reader=None):
    context = context_from_environment(environ)
    if context is None:
        return None
    started_at = now_iso()
    os_evidence = correlate_os_evidence(context, started_at, reader=reader)
    record = {**context, "component": component, "started_at": started_at, "ended_at": None, "status": "running",
              "trigger_origin": os_evidence.get("trigger_origin", "unknown"), "os_scheduler_evidence": os_evidence}
    path = _path(manager_home, context["scheduler_invocation_id"])
    _write(path, record)
    try:
        _cleanup(path.parent)
    except OSError:
        pass
    return {**context, "os_scheduler_evidence": os_evidence}


def finish(manager_home, context, status):
    if not context:
        return
    path = _path(manager_home, context["scheduler_invocation_id"])
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    record.update(ended_at=now_iso(), status=status[:40]); _write(path, record)


def command_origin(context=None):
    if not context:
        return {"caller_origin": "direct_or_unknown", "scheduler_invocation_id": None}
    origin = {"caller_origin": "watcher_poll", "scheduler_invocation_id": context.get("scheduler_invocation_id")}
    for key in ("wrapper_pid", "wrapper_parent_pid", "wrapper_creation_identity", "os_scheduler_evidence"):
        if key in context:
            origin[key] = context[key]
    return origin


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
    if not all(provider.get(key) for key in required):
        return "UNKNOWN"
    os_evidence = command_evidence.get("os_scheduler_evidence") or {}
    os_required = ("instance_id", "trigger_event_record_id", "action_event_record_id", "action_process_id")
    if os_evidence.get("status") == "FAIL":
        return "FAIL"
    if (os_evidence.get("status") != "PASS" or os_evidence.get("trigger_origin") != "scheduled_time"
            or not all(os_evidence.get(key) for key in os_required)):
        return "UNKNOWN"
    if not command_evidence.get("wrapper_pid") or not command_evidence.get("wrapper_creation_identity"):
        return "UNKNOWN"
    if not command_evidence.get("wrapper_parent_pid"):
        return "UNKNOWN"
    return "PASS" if os_evidence["action_process_id"] == command_evidence["wrapper_parent_pid"] else "FAIL"
