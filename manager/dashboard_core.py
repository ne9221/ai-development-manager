"""Core logic for ADM Operations Dashboard."""

from __future__ import annotations
from datetime import datetime, timezone

# Terminal execution statuses in ADM
TERMINAL_EXECUTION_STATUSES = {"completed", "failed", "interrupted", "cancelled"}

def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None

def is_cleanup_confirmed(execution: dict) -> bool:
    """True only when cleanup evidence proves the task claim and writer lease are released."""
    evidence = execution.get("cleanup_evidence")
    if not isinstance(evidence, dict) or evidence.get("task_claim_release") != "released":
        return False
    writer_release = evidence.get("writer_release")
    if execution.get("access") == "read_only":
        return writer_release in ("released", "not_required")
    return writer_release == "released"

def determine_execution_state(execution: dict, now: datetime) -> str:
    """Map execution record to the UI state:
    running, waiting, correlating, finishing, completed, failed, interrupted, cancelled.
    """
    status = execution.get("status")
    if not isinstance(status, str):
        return "unknown"

    if status in TERMINAL_EXECUTION_STATUSES:
        if is_cleanup_confirmed(execution):
            return status
        return "finishing"

    if status == "running":
        # Check if it has provider_session_id. If not, it is correlating
        if not execution.get("provider_session_id"):
            return "correlating"
        
        # Check heartbeat for waiting status
        provider = execution.get("provider", "").lower()
        if provider != "claude":
            heartbeat_str = execution.get("heartbeat_at")
            heartbeat = parse_time(heartbeat_str)
            if heartbeat:
                age_seconds = (now - heartbeat).total_seconds()
                if age_seconds >= 900:  # 15 minutes
                    return "waiting"
        return "running"

    return status

def is_execution_stale(execution: dict, now: datetime) -> bool:
    """An execution is stale if it is running/reserved and has no heartbeat for 15 minutes,
    or has exceeded its hard timeout.
    """
    status = execution.get("status")
    if status in TERMINAL_EXECUTION_STATUSES:
        return False

    # Check hard timeout
    hard_timeout_str = execution.get("hard_timeout_at")
    if hard_timeout_str:
        hard_timeout = parse_time(hard_timeout_str)
        if hard_timeout and now > hard_timeout:
            return True

    provider = execution.get("provider", "").lower()
    if provider == "claude":
        return False

    # Check heartbeat
    heartbeat_str = execution.get("heartbeat_at")
    if heartbeat_str:
        heartbeat = parse_time(heartbeat_str)
        if heartbeat:
            age_seconds = (now - heartbeat).total_seconds()
            if age_seconds >= 900:  # 15 minutes
                return True
    else:
        # If running/reserved but has no heartbeat at all, check start time or reserve time
        start_str = execution.get("started_at") or execution.get("reserved_at")
        start = parse_time(start_str)
        if start:
            age_seconds = (now - start).total_seconds()
            if age_seconds >= 900:
                return True

    return False

def get_global_summary(
    providers_summary: list,
    all_tasks: list,
    active_executions: list
) -> dict:
    """Compute dashboard global metrics."""
    running_tasks = sum(1 for t in all_tasks if t.get("status") == "in_progress")
    blocked_tasks = sum(1 for t in all_tasks if t.get("status") == "blocked")
    active_sessions = len(active_executions)
    
    reliable_providers = sum(1 for p in providers_summary if p.get("has_reliable_quota"))
    
    return {
        "running_tasks_count": running_tasks,
        "blocked_tasks_count": blocked_tasks,
        "active_sessions_count": active_sessions,
        "reliable_providers_count": reliable_providers
    }

def map_task_board(all_tasks: list, active_executions_dict: dict, now: datetime) -> dict:
    """Group tasks into Ready, In progress, Blocked / Attention, Completed."""
    board = {
        "Ready": [],
        "In progress": [],
        "Blocked / Attention": [],
        "Completed": []
    }
    
    for task in all_tasks:
        status = task.get("status")
        task_id = task.get("task_id")
        project_id = task.get("project_id")
        key = (project_id, task_id)
        
        # Check if linked to a stale execution
        linked_execution = active_executions_dict.get(key)
        execution_stale = False
        if linked_execution:
            execution_stale = is_execution_stale(linked_execution, now)
            
        if status == "blocked" or execution_stale:
            board["Blocked / Attention"].append(task)
        elif status in ("in_progress", "ready", "queued"):
            if status == "in_progress":
                board["In progress"].append(task)
            else:
                board["Ready"].append(task)
        elif status in ("completed", "cancelled"):
            board["Completed"].append(task)
        else:
            board["Ready"].append(task)
            
    return board
