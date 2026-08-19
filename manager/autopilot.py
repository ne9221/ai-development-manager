"""Minimal Autopilot Slice 1 autonomous continuation engine.

Enforces fail-closed governance for multi-step task continuation:
1. Deterministic dependency progression evaluation (`task.depends_on`).
2. Dirty-tree / baseline transition safety barrier.
3. Post-execution verification & review barrier (read-only progression vs REVIEW_REQUIRED).
4. Runaway circuit breaker (max 1 continuation in Slice 1, time budget, quota check).
5. Generation-CAS persistent idempotency (at-most-once continuation per source execution).
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from manager.autopilot_continuations import (
    autopilot_continuation_registry,
    claim_autopilot_continuation,
)
from manager.dispatcher import dispatch as dispatcher_dispatch
from manager.quota_reader import read_drive_status, summarize
from manager.tasks import DriveRecords, TaskError, now_iso, update_task, validate
from manager.trusted_ingress import (
    ADMISSION_VERSION,
    REQUIRED_TASK_POLICIES,
    TRUSTED_INGRESS_ORIGIN,
    task_policy_satisfied,
)


# Slice 1 governance constants
MAX_CONTINUATION_STEPS = 1
MAX_CONSECUTIVE_FAILURES = 1
MAX_AUTOPILOT_MINUTES = 60
AUTOPILOT_ORIGIN = "autopilot_continuation"
AUTOPILOT_ADMISSION_VERSION = "v1"

# Explicit Autopilot state model
STATE_READY = "READY"
STATE_ELIGIBLE = "ELIGIBLE"
STATE_DISPATCHED = "DISPATCHED"
STATE_RUNNING = "RUNNING"
STATE_COMPLETED = "COMPLETED"
STATE_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATE_NEXT_READY = "NEXT_READY"
STATE_DONE = "DONE"
STATE_BLOCKED = "BLOCKED"
STATE_ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
STATE_RETRY_ELIGIBLE = "RETRY_ELIGIBLE"

PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


class AutopilotError(TaskError):
    def __init__(self, code: str, message: str, state: str = STATE_BLOCKED):
        super().__init__(message)
        self.code = code
        self.state = state


def parse_utc_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def evaluate_dependencies(store: DriveRecords, project_id: str, task: dict[str, Any]) -> dict[str, Any]:
    """AG Finding 1 — Deterministic dependency readiness evaluation.

    A candidate task is eligible only if EVERY task listed in `depends_on`:
    - exists in the project
    - is terminal
    - has status == 'completed'
    - has durable persistent execution evidence with confirmed cleanup

    Missing, blocked, cancelled, failed, stale, or unknown dependencies fail closed.
    """
    depends_on = task.get("depends_on", [])
    if not isinstance(depends_on, list):
        return {"satisfied": False, "state": STATE_BLOCKED, "reason": "malformed_depends_on"}

    if not depends_on:
        return {"satisfied": True, "state": STATE_ELIGIBLE, "reason": None, "completed_dependencies": []}

    completed_deps = []
    for dep_task_id in depends_on:
        if not isinstance(dep_task_id, str) or not dep_task_id.strip():
            return {"satisfied": False, "state": STATE_BLOCKED, "reason": "invalid_dependency_id"}

        try:
            dep_task = store.get("tasks", project_id, dep_task_id)
            validate("task", dep_task)
        except TaskError:
            return {"satisfied": False, "state": STATE_BLOCKED, "reason": f"dependency_missing:{dep_task_id}"}

        status = dep_task.get("status")
        if status != "completed":
            return {
                "satisfied": False,
                "state": STATE_BLOCKED,
                "reason": f"dependency_incomplete:{dep_task_id}:{status}",
            }

        # Verify durable execution persistence and cleanup evidence
        active_exec_id = (dep_task.get("source_context") or {}).get("active_execution_id")
        executions = store.list_records("executions", project_id)
        matching_execs = [e for e in executions if e.get("task_id") == dep_task_id and e.get("status") == "completed"]

        if not matching_execs:
            return {
                "satisfied": False,
                "state": STATE_BLOCKED,
                "reason": f"dependency_execution_evidence_missing:{dep_task_id}",
            }

        latest_exec = max(matching_execs, key=lambda e: e.get("completed_at") or "")
        cleanup = latest_exec.get("cleanup_evidence") or {}
        if cleanup.get("task_claim_release") != "released":
            return {
                "satisfied": False,
                "state": STATE_ATTENTION_REQUIRED,
                "reason": f"dependency_task_claim_not_released:{dep_task_id}",
            }

        completed_deps.append(dep_task_id)

    return {"satisfied": True, "state": STATE_ELIGIBLE, "reason": None, "completed_dependencies": completed_deps}


def verify_predecessor_barrier(store: DriveRecords, project_id: str, source_execution_id: str) -> dict[str, Any]:
    """AG Finding 3 — Post-execution verification/review barrier.

    A completed execution does NOT automatically imply NEXT_READY.
    - Read-only predecessor: advances only when fully completed, persisted,
      claims released, and no review/test gate is declared.
    - Tasks requiring tests/review or repo modification: stop at REVIEW_REQUIRED.
    """
    try:
        execution = store.get("executions", project_id, source_execution_id)
        validate("execution", execution)
    except TaskError:
        return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": "source_execution_missing_or_invalid"}

    status = execution.get("status")
    if status != "completed":
        if status in ("failed", "interrupted"):
            retry_count = int(execution.get("retry_count", 0))
            state = STATE_RETRY_ELIGIBLE if retry_count < 2 else STATE_BLOCKED
            return {"passed": False, "state": state, "reason": f"source_execution_{status}"}
        if status == "cancelled":
            return {"passed": False, "state": STATE_BLOCKED, "reason": "source_execution_cancelled"}
        return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": f"source_execution_non_terminal:{status}"}

    # Verify durable persistence and release evidence
    cleanup = execution.get("cleanup_evidence") or {}
    if cleanup.get("persistence") != "complete":
        return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": "source_execution_persistence_incomplete"}
    if cleanup.get("task_claim_release") != "released":
        return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": "source_execution_task_claim_not_released"}

    writer_release = cleanup.get("writer_release")
    if execution.get("access") == "read_only":
        if writer_release not in ("released", "not_required"):
            return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": "source_execution_writer_release_invalid"}
    elif writer_release != "released":
        return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": "source_execution_writer_release_incomplete"}

    # Inspect source task metadata
    task_id = execution.get("task_id")
    try:
        task = store.get("tasks", project_id, task_id)
        validate("task", task)
    except TaskError:
        return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": "source_task_missing_or_invalid"}

    # In Slice 1: if task had write access, required repo edits, or explicit review -> REVIEW_REQUIRED
    if task.get("needs_repo_edit") is True or task.get("read_only") is not True or not task_policy_satisfied(task):
        return {
            "passed": False,
            "state": STATE_REVIEW_REQUIRED,
            "reason": "source_task_requires_manual_review_or_test_barrier",
            "execution": execution,
            "task": task,
        }

    return {
        "passed": True,
        "state": STATE_NEXT_READY,
        "reason": None,
        "execution": execution,
        "task": task,
    }


def evaluate_repository_transition(
    store: DriveRecords,
    project: dict[str, Any],
    candidate_task: dict[str, Any],
    writer_registry_reader: Callable[[], dict[str, Any] | None] | None = None,
    git_checker: Callable[[str], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """AG Finding 2 — Dirty-tree / baseline transition safety gate.

    Verifies working_directory, baseline_head, and git cleanliness.
    Any dirty or untrusted repo state results in BLOCKED / ATTENTION_REQUIRED with zero dispatch.
    """
    working_directory = candidate_task.get("working_directory") or project.get("working_directory")
    if not isinstance(working_directory, str) or not working_directory.strip():
        return {"clean": False, "state": STATE_BLOCKED, "reason": "missing_working_directory"}

    if not os.path.isabs(working_directory):
        return {"clean": False, "state": STATE_BLOCKED, "reason": f"working_directory_not_absolute:{working_directory}"}

    if not os.path.isdir(working_directory):
        return {"clean": False, "state": STATE_BLOCKED, "reason": f"working_directory_not_found:{working_directory}"}

    # Git working tree cleanliness check
    def _default_git_check(cwd: str) -> tuple[bool, str]:
        if not (Path(cwd) / ".git").exists():
            return True, ""
        try:
            res = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                                 capture_output=True, text=True, timeout=5, check=True)
            output = res.stdout.strip()
            return (len(output) == 0), output
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"git_error:{exc}"

    checker = git_checker or _default_git_check
    is_clean, details = checker(working_directory)
    if not is_clean:
        return {
            "clean": False,
            "state": STATE_BLOCKED,
            "reason": "working_tree_dirty",
            "details": details[:300],
        }

    # Check baseline HEAD if candidate task specifies one
    expected_baseline = candidate_task.get("baseline_head")
    if expected_baseline:
        try:
            head_res = subprocess.run(["git", "-C", working_directory, "rev-parse", "HEAD"],
                                      capture_output=True, text=True, timeout=5, check=True)
            current_head = head_res.stdout.strip()
            if current_head != expected_baseline:
                return {
                    "clean": False,
                    "state": STATE_BLOCKED,
                    "reason": f"baseline_head_mismatch:expected_{expected_baseline}:actual_{current_head}",
                }
        except (subprocess.SubprocessError, OSError):
            pass

    # Check for active unresolved writer leases if reader is provided
    if writer_registry_reader:
        try:
            reg_doc = writer_registry_reader()
            if reg_doc and reg_doc.get("locks"):
                for lock_id, lock_entry in reg_doc["locks"].items():
                    if lock_entry.get("effective_status") == "active":
                        return {
                            "clean": False,
                            "state": STATE_ATTENTION_REQUIRED,
                            "reason": f"active_writer_lease_conflict:{lock_id}",
                        }
        except Exception as exc:
            return {"clean": False, "state": STATE_ATTENTION_REQUIRED, "reason": f"writer_registry_unavailable:{exc}"}

    return {"clean": True, "state": STATE_ELIGIBLE, "reason": None}


def evaluate_circuit_breaker(
    session_start_time: datetime,
    continuation_count: int,
    quota_summary: dict[str, Any] | None,
    now: datetime | None = None,
    max_steps: int = MAX_CONTINUATION_STEPS,
    max_minutes: int = MAX_AUTOPILOT_MINUTES,
) -> dict[str, Any]:
    """AG Finding 4 — Runaway circuit breaker & budget guard."""
    if continuation_count >= max_steps:
        return {
            "allowed": False,
            "state": STATE_DONE,
            "reason": f"max_continuation_steps_reached:{max_steps}",
        }

    current_time = now or datetime.now(timezone.utc)
    elapsed_minutes = (current_time - session_start_time).total_seconds() / 60.0
    if elapsed_minutes > max_minutes:
        return {
            "allowed": False,
            "state": STATE_ATTENTION_REQUIRED,
            "reason": f"autopilot_time_budget_exceeded:{elapsed_minutes:.1f}m > {max_minutes}m",
        }

    # Quota check: fail closed on stale or missing quota data
    if quota_summary is not None:
        has_reliable = any(p.get("has_reliable_quota") for p in quota_summary.get("providers", []))
        if not has_reliable:
            return {
                "allowed": False,
                "state": STATE_BLOCKED,
                "reason": "quota_unreliable",
            }

    return {"allowed": True, "state": STATE_ELIGIBLE, "reason": None}


def find_next_candidate_task(
    store: DriveRecords,
    project_id: str,
    completed_task_ids: set[str],
) -> dict[str, Any] | None:
    """Find the next eligible, unexecuted task in the project backlog."""
    try:
        tasks = store.list_records("tasks", project_id)
    except TaskError:
        return None

    candidates = []
    for t in tasks:
        task_id = t.get("task_id")
        if not task_id or task_id in completed_task_ids:
            continue
        status = t.get("status")
        if status not in ("ready", "queued"):
            continue
        candidates.append(t)

    if not candidates:
        return None

    def _sort_key(t: dict[str, Any]):
        p_val = PRIORITY_ORDER.get(t.get("priority", "normal"), 2)
        created = t.get("created_at") or ""
        return (p_val, created)

    candidates.sort(key=_sort_key)
    return candidates[0]


def step_autopilot(
    store: DriveRecords,
    service: Any,
    project_id: str,
    source_execution_id: str,
    session_start_time: datetime,
    continuation_count: int = 0,
    bucket: str | None = None,
    continuation_registry_factory: Callable[..., Any] = autopilot_continuation_registry,
    quota_document: dict[str, Any] | None = None,
    git_checker: Callable[[str], tuple[bool, str]] | None = None,
    writer_registry_reader: Callable[[], dict[str, Any] | None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute one bounded, fail-closed Autopilot continuation step.

    Workflow:
    1. Verify predecessor completion & barrier (AG Finding 3).
    2. Check circuit breaker & budget (AG Finding 4).
    3. Find next candidate task.
    4. Enforce Slice 1 read-only scope constraint.
    5. Evaluate dependency progression (AG Finding 1).
    6. Evaluate dirty-tree / baseline transition (AG Finding 2).
    7. Atomically claim continuation idempotency via GCS CAS.
    8. Dispatch next Task + Command.
    """
    current_time = now or datetime.now(timezone.utc)

    # 1. Predecessor barrier
    barrier_res = verify_predecessor_barrier(store, project_id, source_execution_id)
    if not barrier_res["passed"]:
        return {
            "status": "halted",
            "state": barrier_res["state"],
            "reason": barrier_res["reason"],
            "continuation_count": continuation_count,
        }

    source_task = barrier_res["task"]
    source_task_id = source_task["task_id"]

    # 2. Circuit breaker & Quota
    quota_doc = quota_document or (read_drive_status(service=service) if service else None)
    quota_sum = summarize(quota_doc, now=current_time, max_age_minutes=60) if quota_doc else None
    circuit_res = evaluate_circuit_breaker(session_start_time, continuation_count, quota_sum, now=current_time)
    if not circuit_res["allowed"]:
        return {
            "status": "halted",
            "state": circuit_res["state"],
            "reason": circuit_res["reason"],
            "continuation_count": continuation_count,
        }

    # 3. Find next candidate task
    try:
        project = store.get("projects", project_id, project_id)
        validate("project", project)
    except TaskError as exc:
        return {"status": "halted", "state": STATE_BLOCKED, "reason": f"project_not_found:{exc}"}

    completed_ids = {source_task_id}
    # Collect all existing completed tasks
    for t in store.list_records("tasks", project_id):
        if t.get("status") == "completed":
            completed_ids.add(t.get("task_id"))

    next_task = find_next_candidate_task(store, project_id, completed_ids)
    if next_task is None:
        return {
            "status": "done",
            "state": STATE_DONE,
            "reason": "all_tasks_completed",
            "continuation_count": continuation_count,
        }

    next_task_id = next_task["task_id"]

    # 4. Scope constraint for Slice 1: read-only only
    if next_task.get("read_only") is not True or next_task.get("needs_repo_edit") is True:
        return {
            "status": "halted",
            "state": STATE_REVIEW_REQUIRED,
            "reason": "slice1_write_task_requires_slice2_activation",
            "next_task_id": next_task_id,
            "continuation_count": continuation_count,
        }

    # 5. Dependency progression
    dep_res = evaluate_dependencies(store, project_id, next_task)
    if not dep_res["satisfied"]:
        return {
            "status": "halted",
            "state": dep_res["state"],
            "reason": dep_res["reason"],
            "next_task_id": next_task_id,
            "continuation_count": continuation_count,
        }

    # 6. Dirty-tree / baseline transition
    trans_res = evaluate_repository_transition(store, project, next_task,
                                               writer_registry_reader=writer_registry_reader,
                                               git_checker=git_checker)
    if not trans_res["clean"]:
        return {
            "status": "halted",
            "state": trans_res["state"],
            "reason": trans_res["reason"],
            "next_task_id": next_task_id,
            "continuation_count": continuation_count,
        }

    # 7. Persistent Idempotency Claim via GCS CAS
    next_command_id = f"autopilot-{next_task_id}-{now_iso()[:10]}"
    decided_at = now_iso()
    new_continuation_count = continuation_count + 1

    if bucket:
        try:
            reg = continuation_registry_factory(bucket, project_id, source_execution_id)
            claim = claim_autopilot_continuation(
                reg, project_id, source_execution_id, source_task_id,
                next_task_id, next_command_id, new_continuation_count, decided_at,
            )
            if not claim["claimed"]:
                return {
                    "status": "already_claimed",
                    "state": STATE_DISPATCHED,
                    "existing_claim": claim,
                    "continuation_count": continuation_count,
                }
        except Exception as exc:
            return {
                "status": "halted",
                "state": STATE_ATTENTION_REQUIRED,
                "reason": f"continuation_idempotency_backend_unavailable:{exc}",
            }

    # 8. Dispatch next task under trusted-ingress contract
    dispatch_request = {
        "project_id": project_id,
        "task_id": next_task_id,
        "title": next_task["title"],
        "task_type": next_task.get("task_type", "general"),
        "complexity": next_task.get("complexity", "medium"),
        "expected_minutes": next_task.get("expected_minutes", 20),
        "needs_repo_edit": False,
        "read_only": True,
        "source_context": {
            "origin": TRUSTED_INGRESS_ORIGIN,
            "admission_version": ADMISSION_VERSION,
            "source_execution_id": source_execution_id,
            "source_task_id": source_task_id,
            "continuation_count": new_continuation_count,
            "decided_at": decided_at,
            "goal": next_task.get("source_context", {}).get("goal") or next_task["title"],
        },
    }

    result = dispatcher_dispatch(store, service, dispatch_request, quota_document=quota_doc)

    update_task(store, project_id, next_task_id, priority=next_task.get("priority", "normal"),
                read_only=True, execution_policies=sorted(REQUIRED_TASK_POLICIES))

    command = {
        "command_id": next_command_id,
        "project_id": project_id,
        "task_id": next_task_id,
        "provider": result["provider"],
        "account_id": result.get("account_id"),
        "requested_provider": None,
        "requested_account_id": None,
        "model": result.get("model"),
        "fallback_model": result.get("fallback_model"),
        "mode": result.get("mode"),
        "effort": result.get("effort"),
        "selection_reason": result.get("selection_reason", []),
        "quota_evidence": result.get("quota_evidence"),
        "created_at": decided_at,
        "status": "queued",
        "execution_id": None,
        "claimed_at": None,
        "completed_at": None,
        "result": None,
        "created_via": TRUSTED_INGRESS_ORIGIN,
        "admission_version": ADMISSION_VERSION,
        "request_id": f"ap-{next_task_id}",
    }
    validate("command", command)
    store.put("commands", project_id, next_command_id, command)

    return {
        "status": "dispatched",
        "state": STATE_DISPATCHED,
        "source_execution_id": source_execution_id,
        "next_task_id": next_task_id,
        "next_command_id": next_command_id,
        "continuation_count": new_continuation_count,
        "provider": result["provider"],
    }
