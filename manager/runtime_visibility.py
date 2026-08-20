"""Runtime Visibility and Fleet Activity Helpers for ADM Dashboard P1-B."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from manager.dashboard_core import parse_time, is_execution_stale


STATE_AUTO_RUNNING = "AUTO RUNNING"
STATE_WAITING_USER = "WAITING USER"
STATE_BLOCKED = "BLOCKED"
STATE_IDLE = "IDLE"
STATE_AUTO_STALLED = "AUTO STALLED"
STATE_UNKNOWN = "UNKNOWN"

STALE_ACTIVITY_MINUTES = 10.0
INITIALIZING_GRACE_MINUTES = 2.0


def format_elapsed_duration(start_val: Any, now: Optional[datetime] = None) -> str:
    """Format runtime elapsed duration. Return 'Unknown' if start is missing/invalid."""
    if not start_val:
        return "Unknown"
    start_dt = parse_time(start_val) if isinstance(start_val, str) else start_val
    if not start_dt:
        return "Unknown"
    now_dt = now or datetime.now(timezone.utc)
    seconds = max(0.0, (now_dt - start_dt).total_seconds())
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    hours = minutes // 60
    rem_mins = minutes % 60

    if hours > 0:
        return f"{hours}h {rem_mins:02d}m"
    return f"{minutes}m {secs:02d}s"


def format_activity_timestamp_and_age(activity_val: Any, now: Optional[datetime] = None) -> str:
    """Format exact time and human age (e.g. '02:31:08 · 1m ago'). Return 'Unknown' if missing."""
    if not activity_val:
        return "Unknown"
    act_dt = parse_time(activity_val) if isinstance(activity_val, str) else activity_val
    if not act_dt:
        return "Unknown"
    now_dt = now or datetime.now(timezone.utc)
    seconds = max(0.0, (now_dt - act_dt).total_seconds())
    mins = int(seconds // 60)
    exact_time_str = act_dt.strftime("%H:%M:%S")

    if seconds < 60:
        age_str = f"{int(seconds)}s ago"
    elif mins < 60:
        age_str = f"{mins}m ago"
    else:
        hours = mins // 60
        age_str = f"{hours}h ago"

    return f"{exact_time_str} · {age_str}"


def format_duration_and_remaining_eta(
    expected_minutes: Optional[float | int],
    start_val: Any,
    now: Optional[datetime] = None
) -> Tuple[str, str]:
    """Calculate expected total duration and remaining duration safely.

    Truth Contract: Never label total expected duration as remaining ETA.
    Returns: (expected_total_display, est_remaining_display)
    """
    if expected_minutes is None:
        return "—", "—"

    expected_total_str = f"~{int(expected_minutes)}m"

    if not start_val:
        return expected_total_str, "—"

    start_dt = parse_time(start_val) if isinstance(start_val, str) else start_val
    if not start_dt:
        return expected_total_str, "—"

    now_dt = now or datetime.now(timezone.utc)
    elapsed_minutes = max(0.0, (now_dt - start_dt).total_seconds() / 60.0)
    remaining_minutes = max(0.0, float(expected_minutes) - elapsed_minutes)

    if remaining_minutes <= 0.0:
        est_remaining_str = "Overdue / finishing"
    else:
        est_remaining_str = f"~{int(remaining_minutes)}m"

    return expected_total_str, est_remaining_str


def get_latest_activity_timestamp(exe: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Retrieve the most trustworthy live activity timestamp and its source label."""
    # Priority 1: true last provider event timestamp if recorded
    if exe.get("last_provider_event_at"):
        return exe["last_provider_event_at"], "provider event"
    # Priority 2: heartbeat timestamp
    if exe.get("heartbeat_at"):
        return exe["heartbeat_at"], "heartbeat"
    # Priority 3: session updated timestamp
    if exe.get("session_updated_at"):
        return exe["session_updated_at"], "session update"
    return None, "none"


def determine_ai_runtime_activity(exe: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[str, str, str]:
    """Determine live execution state distinguishing healthy long runs from stalled runs.

    Truth Contract: If status='running' but no heartbeat or event evidence exists,
    do NOT fabricate green RUNNING. Use UNKNOWN / WAITING FOR EVIDENCE.
    Returns: (state_label, badge_class, explanation)
    """
    now_dt = now or datetime.now(timezone.utc)
    status = exe.get("status", "unknown").lower()

    if status in ["completed", "succeeded"]:
        return "COMPLETED", "badge-ok", "Execution finished successfully."
    if status in ["failed", "crashed"]:
        return "FAILED", "badge-err", "Execution failed with errors."
    if status in ["blocked", "attention"]:
        return "BLOCKED", "badge-err", "Execution is blocked."

    start_time_val = exe.get("started_at") or exe.get("reserved_at")
    start_dt = parse_time(start_time_val) if start_time_val else None

    act_time_val, act_source = get_latest_activity_timestamp(exe)
    act_dt = parse_time(act_time_val) if act_time_val else None

    # Truth Contract: No activity evidence -> Not green RUNNING
    if not act_dt:
        if start_dt:
            elapsed_seconds = max(0.0, (now_dt - start_dt).total_seconds())
            if elapsed_seconds <= (INITIALIZING_GRACE_MINUTES * 60):
                return "INITIALIZING", "badge-warn", "Worker process initializing; awaiting first heartbeat."
        return "UNKNOWN", "badge-warn", "Execution marked running but no activity or heartbeat evidence received."

    idle_seconds = max(0.0, (now_dt - act_dt).total_seconds())
    idle_minutes = idle_seconds / 60.0

    if idle_minutes >= STALE_ACTIVITY_MINUTES:
        return "POSSIBLY STALLED", "badge-err", f"No {act_source} activity for {int(idle_minutes)}m."

    if idle_minutes >= (STALE_ACTIVITY_MINUTES / 2):
        return "WAITING", "badge-warn", f"Awaiting next event ({int(idle_minutes)}m since last {act_source})."

    return "RUNNING", "badge-ok", f"Actively processing ({act_source} received {int(idle_seconds)}s ago)."


def compute_global_runtime_state(
    active_executions: List[Dict[str, Any]],
    all_tasks: List[Dict[str, Any]],
    open_actions: List[Any],
    infra_health_list: Optional[List[Any]] = None,
    now: Optional[datetime] = None
) -> Tuple[str, str, str]:
    """Derive global ADM operational state from real evidence without guessing.

    Returns: (state_constant, badge_class, description)
    """
    now_dt = now or datetime.now(timezone.utc)

    # Check for critical infra blockage
    if infra_health_list:
        drive_health = next((h for h in infra_health_list if "Drive" in h.name), None)
        dash_health = next((h for h in infra_health_list if "Dashboard" in h.name), None)
        if dash_health and dash_health.status_label == "Offline":
            return STATE_UNKNOWN, "badge-warn", "Dashboard communication unconfirmed."
        if drive_health and drive_health.status_label == "Offline":
            high_actions = [a for a in open_actions if getattr(a, "severity", "") == "high" and getattr(a, "status", "") == "open"]
            if high_actions:
                return STATE_BLOCKED, "badge-err", "Google Drive SSOT disconnected with unresolved high-severity actions."

    # Check for active running executions
    if active_executions:
        states = [determine_ai_runtime_activity(e, now_dt)[0] for e in active_executions]
        if all(s in ["POSSIBLY STALLED", "STALE", "BLOCKED", "UNKNOWN"] for s in states):
            return STATE_AUTO_STALLED, "badge-err", "Active executions have stopped emitting progress updates."
        return STATE_AUTO_RUNNING, "badge-ok", f"Autonomous fleet active ({len(active_executions)} running task{'s' if len(active_executions) > 1 else ''})."

    # Check for high severity or user action required items
    unresolved_user_actions = [
        a for a in open_actions
        if getattr(a, "status", "") in ["open", "acknowledged"] and getattr(a, "need_user_action", False)
    ]
    if unresolved_user_actions:
        first_act = unresolved_user_actions[0]
        title = getattr(first_act, "title", "User intervention needed")
        return STATE_WAITING_USER, "badge-warn", f"Waiting for user: {title}"

    # Check for blocked tasks
    blocked_tasks = [t for t in all_tasks if t.get("status") in ["blocked", "attention"]]
    if blocked_tasks:
        return STATE_BLOCKED, "badge-err", f"{len(blocked_tasks)} task(s) currently blocked."

    return STATE_IDLE, "badge-ok", "All tasks complete or awaiting next scheduled trigger. System idle."


def compute_next_auto_action(
    all_tasks: List[Dict[str, Any]],
    active_executions: List[Dict[str, Any]],
    open_actions: List[Any],
    daily_brief_vm: Optional[Any] = None
) -> str:
    """Determine the next concrete automatic or dispatch step based on live state."""
    # 1. If currently executing, next step is completion of active task
    if active_executions:
        first_exe = active_executions[0]
        p_id = first_exe.get("project_id", "—")
        t_id = first_exe.get("task_id", "—")
        prov = first_exe.get("provider", "AI")
        return f"Awaiting completion of {p_id}/{t_id} on {prov}"

    # 2. If user action required, next step is user response
    user_actions = [a for a in open_actions if getattr(a, "status", "") == "open" and getattr(a, "need_user_action", False)]
    if user_actions:
        return f"Awaiting user action: {user_actions[0].title}"

    # 3. If ready tasks exist, next step is dispatch
    ready_tasks = [t for t in all_tasks if t.get("status") == "ready"]
    if ready_tasks:
        first_r = ready_tasks[0]
        rec_p = first_r.get("assigned_provider") or first_r.get("recommended_provider") or "primary provider"
        return f"Dispatch task {first_r.get('task_id')} ({first_r.get('project_id')}) to {rec_p}"

    # 4. If quota recommendations exist
    if daily_brief_vm and getattr(daily_brief_vm, "recommended_action", None):
        rec_act = daily_brief_vm.recommended_action
        if rec_act != "normal":
            return f"Execute fleet policy: {rec_act} ({daily_brief_vm.reason})"

    return "No pending auto actions. Fleet standing by."
