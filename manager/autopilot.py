"""Minimal Autopilot Slice 1 autonomous continuation engine.

Enforces fail-closed governance for multi-step task continuation:
1. Deterministic dependency progression evaluation (`task.depends_on`).
2. Dirty-tree / baseline transition safety barrier.
3. Post-execution verification & review barrier (read-only progression vs REVIEW_REQUIRED).
4. Runaway circuit breaker (max 1 continuation in Slice 1, time budget, quota check).
5. Generation-CAS persistent idempotency (at-most-once continuation per source execution).
6. Eligible-first candidate selection: already-executing/dispatched tasks are excluded and
   dependency/repository eligibility is evaluated BEFORE ranking, so one ineligible
   high-priority task cannot starve a lower-priority eligible one (or halt Autopilot outright).
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from manager.autopilot_continuations import (
    STATE_ATTENTION_REQUIRED as CONT_STATE_ATTENTION_REQUIRED,
    STATE_COMPLETED as CONT_STATE_COMPLETED,
    STATE_DISPATCHED as CONT_STATE_DISPATCHED,
    autopilot_continuation_registry,
    claim_autopilot_continuation,
    mark_continuation_attention_required,
    mark_continuation_dispatched,
    mark_continuation_dispatching,
    mark_continuation_failed_safe,
)
from manager.dispatch_requests import claim_dispatch_request, dispatch_request_registry
from manager.dispatcher import dispatch as dispatcher_dispatch
from manager.quota_reader import read_drive_status, summarize
from manager.task_claims import check_task_execution_claim, task_claim_registry
from manager.tasks import DriveRecords, TaskError, now_iso, safe_id, update_task, validate
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

# Non-terminal execution/Command statuses: a task with any record in one of
# these states already has work in flight and must never be selected again
# as an Autopilot dispatch candidate (Codex P1-1).
_NONTERMINAL_EXECUTION_STATUSES = {"reserved", "running"}
_NONTERMINAL_COMMAND_STATUSES = {"queued", "claimed", "running", "attention"}

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

    Codex P0-2: when the dependency task names an explicit
    `source_context.active_execution_id`, that execution is authoritative and
    is inspected exactly — it is never silently replaced by some other,
    possibly older, completed execution for the same task. Only when no
    active_execution_id is recorded at all does evaluation fall back to the
    pre-existing "search completed executions for this task_id" contract
    (proven by test_all_dependencies_completed_with_evidence_is_satisfied).
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

        # AG AC-DEP-02 (P2): a task cannot depend on itself. In the common
        # case this is already caught below by the status check (a task
        # cannot be "completed" while it is still the one being evaluated),
        # but that relies on incidental state rather than an explicit
        # invariant -- check it directly so it fails closed unconditionally.
        if dep_task_id == task.get("task_id"):
            return {"satisfied": False, "state": STATE_BLOCKED, "reason": f"self_dependency:{dep_task_id}"}

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

        active_exec_id = (dep_task.get("source_context") or {}).get("active_execution_id")

        if active_exec_id:
            try:
                exec_record = store.get("executions", project_id, active_exec_id)
                validate("execution", exec_record)
            except TaskError:
                return {
                    "satisfied": False,
                    "state": STATE_ATTENTION_REQUIRED,
                    "reason": f"dependency_active_execution_missing:{dep_task_id}",
                }

            if exec_record.get("project_id") != project_id or exec_record.get("task_id") != dep_task_id:
                return {
                    "satisfied": False,
                    "state": STATE_ATTENTION_REQUIRED,
                    "reason": f"dependency_active_execution_identity_mismatch:{dep_task_id}",
                }

            if exec_record.get("status") != "completed":
                return {
                    "satisfied": False,
                    "state": STATE_ATTENTION_REQUIRED,
                    "reason": f"dependency_active_execution_not_completed:{dep_task_id}:{exec_record.get('status')}",
                }

            cleanup = exec_record.get("cleanup_evidence") or {}
            if cleanup.get("persistence") != "complete":
                return {
                    "satisfied": False,
                    "state": STATE_ATTENTION_REQUIRED,
                    "reason": f"dependency_active_execution_persistence_incomplete:{dep_task_id}",
                }
            if cleanup.get("task_claim_release") != "released":
                return {
                    "satisfied": False,
                    "state": STATE_ATTENTION_REQUIRED,
                    "reason": f"dependency_task_claim_not_released:{dep_task_id}",
                }

            writer_release = cleanup.get("writer_release")
            if exec_record.get("access") == "read_only":
                if writer_release not in ("released", "not_required"):
                    return {
                        "satisfied": False,
                        "state": STATE_ATTENTION_REQUIRED,
                        "reason": f"dependency_active_execution_writer_release_invalid:{dep_task_id}",
                    }
            elif writer_release != "released":
                return {
                    "satisfied": False,
                    "state": STATE_ATTENTION_REQUIRED,
                    "reason": f"dependency_active_execution_writer_release_incomplete:{dep_task_id}",
                }

            completed_deps.append(dep_task_id)
            continue

        # No authoritative active_execution_id on the dependency task: fall
        # back to the pre-existing, test-proven contract only in this case.
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

    Codex P1-2: a completed Execution is insufficient by itself — the source
    Task's own lifecycle must independently agree (status == 'completed'),
    and if the Task names a specific active_execution_id, it must match the
    execution actually being inspected here. "Execution completed + Task
    ready" is a lifecycle disagreement and must STOP, not advance.
    """
    try:
        execution = store.get("executions", project_id, source_execution_id)
        validate("execution", execution)
    except TaskError:
        return {"passed": False, "state": STATE_ATTENTION_REQUIRED, "reason": "source_execution_missing_or_invalid"}

    status = execution.get("status")
    if status != "completed":
        if status in ("failed", "interrupted"):
            # Retry invariant: Autopilot itself never performs an automatic
            # retry here — it only reports RETRY_ELIGIBLE/BLOCKED evidence
            # for whatever external recovery path (execution_runner /
            # a human) decides whether a single proven-prelaunch-failure
            # retry is warranted. Autopilot never advances to a next
            # continuation off of a failed/interrupted/ambiguous predecessor.
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

    # P1-2: Task / Execution lifecycle agreement.
    if task.get("status") != "completed":
        return {
            "passed": False,
            "state": STATE_ATTENTION_REQUIRED,
            "reason": f"source_task_execution_lifecycle_mismatch:{task.get('status')}",
        }

    task_active_exec_id = (task.get("source_context") or {}).get("active_execution_id")
    if task_active_exec_id and task_active_exec_id != source_execution_id:
        return {
            "passed": False,
            "state": STATE_ATTENTION_REQUIRED,
            "reason": "source_task_active_execution_id_mismatch",
        }

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
    head_reader: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """AG Finding 2 — Dirty-tree / baseline transition safety gate.

    Verifies working_directory, baseline_head, and git cleanliness.
    Any dirty or untrusted repo state results in BLOCKED / ATTENTION_REQUIRED with zero dispatch.

    Codex P0-1: this must be strictly fail-closed. Positively proves, in
    order: working_directory is present/absolute/exists; the directory is a
    genuine Git worktree/repository (proven by actually running `git status
    --porcelain` and treating ANY command failure — including "this is not a
    git repository at all" — as BLOCKED, never as clean); the working tree is
    clean; and, when a baseline_head is specified, that `git rev-parse HEAD`
    both succeeds and matches it. No branch of this function may interpret a
    command failure as clean.
    """
    working_directory = candidate_task.get("working_directory") or project.get("working_directory")
    if not isinstance(working_directory, str) or not working_directory.strip():
        return {"clean": False, "state": STATE_BLOCKED, "reason": "missing_working_directory"}

    if not os.path.isabs(working_directory):
        return {"clean": False, "state": STATE_BLOCKED, "reason": f"working_directory_not_absolute:{working_directory}"}

    if not os.path.isdir(working_directory):
        return {"clean": False, "state": STATE_BLOCKED, "reason": f"working_directory_not_found:{working_directory}"}

    # Git working tree cleanliness check. Deliberately does NOT special-case
    # a missing `.git` -- running the real command and treating its failure
    # as not-clean is what proves "valid Git worktree/repository" (a plain
    # directory, a normal `.git` directory, and a worktree `.git` FILE are
    # all handled correctly by letting git itself decide) without a separate
    # existence check that could itself be fooled or skipped.
    def _default_git_check(cwd: str) -> tuple[bool, str]:
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
        details = details or ""
        reason = "git_status_command_failed" if details.startswith("git_error:") else "working_tree_dirty"
        return {
            "clean": False,
            "state": STATE_BLOCKED,
            "reason": reason,
            "details": details[:300],
        }

    # Check baseline HEAD if candidate task specifies one. A rev-parse
    # failure must BLOCK, never be swallowed into an implicit pass.
    expected_baseline = candidate_task.get("baseline_head")
    if expected_baseline:
        def _default_head_reader(cwd: str) -> str:
            res = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=5, check=True)
            return res.stdout.strip()

        reader = head_reader or _default_head_reader
        try:
            current_head = reader(working_directory)
        except Exception as exc:
            return {
                "clean": False,
                "state": STATE_BLOCKED,
                "reason": f"rev_parse_head_failed:{exc}"[:300],
            }

        if current_head != expected_baseline:
            return {
                "clean": False,
                "state": STATE_BLOCKED,
                "reason": f"baseline_head_mismatch:expected_{expected_baseline}:actual_{current_head}",
            }

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
    """AG Finding 4 — Runaway circuit breaker & budget guard.

    Codex P0-3: missing, malformed, or empty-provider quota evidence must
    fail closed exactly like "all providers unreliable" — never silently
    allowed just because the caller passed None. No minimum percentage
    threshold is invented; the only bar is "at least one provider/account
    has has_reliable_quota == True", exactly as already computed upstream by
    manager.quota_reader.summarize().
    """
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

    if quota_summary is None:
        return {"allowed": False, "state": STATE_BLOCKED, "reason": "quota_missing"}
    if not isinstance(quota_summary, dict):
        return {"allowed": False, "state": STATE_BLOCKED, "reason": "quota_malformed"}
    providers = quota_summary.get("providers")
    if not isinstance(providers, list) or not providers:
        return {"allowed": False, "state": STATE_BLOCKED, "reason": "quota_providers_empty"}
    has_reliable = any(isinstance(p, dict) and p.get("has_reliable_quota") for p in providers)
    if not has_reliable:
        return {"allowed": False, "state": STATE_BLOCKED, "reason": "quota_unreliable"}

    return {"allowed": True, "state": STATE_ELIGIBLE, "reason": None}


def build_default_task_claim_reader(bucket: str, project_id: str) -> Callable[[str], bool]:
    """AG AC-SELECT-01: construct the real GCS-backed task_claim_reader for
    callers that want manager.task_claims consulted during candidate
    selection. Not wired in automatically by step_autopilot (see the comment
    at its call site) -- pass this explicitly when the caller's deployment
    already treats task_claims as authoritative. A backend error is treated
    as "unknown, assume active" (fail closed): excludes the candidate rather
    than risking a double-dispatch onto a task some other authority may
    still be holding.
    """
    def _reader(task_id: str) -> bool:
        try:
            registry = task_claim_registry(bucket, project_id, task_id)
            return check_task_execution_claim(registry, project_id, task_id) is not None
        except Exception:
            return True
    return _reader


def _task_active_evidence(
    store: DriveRecords,
    project_id: str,
    task: dict[str, Any],
    task_claim_reader: Callable[[str], bool] | None = None,
) -> str | None:
    """Codex P1-1 / AG AC-SELECT-01: is there already an execution, Command,
    or task claim in flight for this task? Any non-terminal record —
    reserved/running execution, a queued/claimed/running/attention Command,
    an authoritative nonterminal source_context.active_execution_id, or an
    active manager.task_claims lease — makes the task ineligible for a fresh
    Autopilot dispatch. Returns a short evidence string, or None if the task
    has no active work."""
    task_id = task.get("task_id")
    for execution in store.list_records("executions", project_id):
        if execution.get("task_id") == task_id and execution.get("status") in _NONTERMINAL_EXECUTION_STATUSES:
            return f"active_execution:{execution.get('execution_id')}:{execution.get('status')}"
    for command in store.list_records("commands", project_id):
        if command.get("task_id") == task_id and command.get("status") in _NONTERMINAL_COMMAND_STATUSES:
            return f"active_command:{command.get('command_id')}:{command.get('status')}"

    active_exec_id = (task.get("source_context") or {}).get("active_execution_id")
    if active_exec_id:
        try:
            named_execution = store.get("executions", project_id, active_exec_id)
        except TaskError:
            named_execution = None
        if named_execution is not None and named_execution.get("status") in _NONTERMINAL_EXECUTION_STATUSES:
            return f"active_execution_id:{active_exec_id}:{named_execution.get('status')}"

    if task_claim_reader is not None:
        try:
            has_claim = task_claim_reader(task_id)
        except Exception:
            has_claim = True  # unknown -> treat as active, fail closed
        if has_claim:
            return f"active_task_claim:{task_id}"

    return None


def find_next_candidate_task(
    store: DriveRecords,
    project_id: str,
    completed_task_ids: set[str],
    project: dict[str, Any] | None = None,
    git_checker: Callable[[str], tuple[bool, str]] | None = None,
    writer_registry_reader: Callable[[], dict[str, Any] | None] | None = None,
    head_reader: Callable[[str], str] | None = None,
    task_claim_reader: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Find the next eligible, unexecuted task in the project backlog.

    Codex P1-1 / P1-3, AG AC-SELECT-01: eligibility is evaluated BEFORE
    ranking, not after.
    1. Enumerate ready/queued candidate tasks.
    2. Exclude any candidate with an existing non-terminal execution,
       Command, authoritative active_execution_id, or active task claim.
    3. Evaluate dependency + (when `project` is given) repository-transition
       eligibility for each remaining candidate.
    4. Collect eligible candidates, rank by priority, return the highest.

    An ineligible high-priority candidate is skipped in favor of a lower-
    priority eligible one instead of halting Autopilot outright. Returns
    `{"task": <dict or None>, "blocked": [...]}` — `blocked` preserves the
    reason/evidence for every candidate that was excluded or found
    ineligible, so callers can distinguish "genuinely nothing left" from
    "candidates exist but none are currently eligible" instead of
    mislabeling the latter as completion.
    """
    try:
        tasks = store.list_records("tasks", project_id)
    except TaskError:
        return {"task": None, "blocked": []}

    blocked: list[dict[str, Any]] = []
    candidates = []
    for t in tasks:
        task_id = t.get("task_id")
        if not task_id or task_id in completed_task_ids:
            continue
        status = t.get("status")
        if status not in ("ready", "queued"):
            continue
        active_evidence = _task_active_evidence(store, project_id, t, task_claim_reader=task_claim_reader)
        if active_evidence is not None:
            blocked.append({"task_id": task_id, "reason": f"already_active:{active_evidence}"})
            continue
        candidates.append(t)

    if not candidates:
        return {"task": None, "blocked": blocked}

    def _sort_key(t: dict[str, Any]):
        p_val = PRIORITY_ORDER.get(t.get("priority", "normal"), 2)
        created = t.get("created_at") or ""
        return (p_val, created)

    candidates.sort(key=_sort_key)

    for candidate in candidates:
        task_id = candidate["task_id"]
        dep_res = evaluate_dependencies(store, project_id, candidate)
        if not dep_res["satisfied"]:
            blocked.append({"task_id": task_id, "reason": dep_res["reason"], "state": dep_res["state"]})
            continue
        if project is not None:
            trans_res = evaluate_repository_transition(
                store, project, candidate,
                writer_registry_reader=writer_registry_reader,
                git_checker=git_checker,
                head_reader=head_reader,
            )
            if not trans_res["clean"]:
                blocked.append({"task_id": task_id, "reason": trans_res["reason"], "state": trans_res["state"]})
                continue
        return {"task": candidate, "blocked": blocked}

    return {"task": None, "blocked": blocked}


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
    head_reader: Callable[[str], str] | None = None,
    task_claim_reader: Callable[[str], bool] | None = None,
    dispatch_request_registry_factory: Callable[..., Any] = dispatch_request_registry,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute one bounded, fail-closed Autopilot continuation step.

    Workflow:
    1. Verify predecessor completion & barrier (AG Finding 3 / Codex P1-2).
    2. Derive a restart-safe continuation depth/session-start floor from
       durable Task evidence (Codex P0-5), then check circuit breaker & budget.
    3. Find next eligible candidate task (Codex P1-1 / P1-3 / AG AC-SELECT-01).
    4. Enforce Slice 1 read-only scope constraint.
    5. Re-verify dependency progression (AG Finding 1 / Codex P0-2 / AG AC-DEP-01).
    6. Re-verify dirty-tree / baseline transition (AG Finding 2 / Codex P0-1).
    7. Atomically claim continuation idempotency via GCS CAS (Codex P0-4 /
       AG AC-CAS-01: deterministic identity, durable-evidence-based recovery).
    8. Dispatch next Task + Command under the existing Direct Dispatch
       trusted-ingress contract (AG AC-INGRESS-01) so Command Watcher's Safe
       Auto-Admission actually accepts it, advancing the CAS state machine.
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

    # Codex P0-5: derive the true continuation depth and session-start floor
    # from durable evidence on the source task's own source_context, rather
    # than trusting the caller-supplied continuation_count/session_start_time
    # alone. A restarted caller passing continuation_count=0 must not be able
    # to escape MAX_CONTINUATION_STEPS when the predecessor task was itself
    # created by a prior automatic continuation (i.e. is already at depth 1).
    source_context = source_task.get("source_context") or {}
    durable_continuation_count = 0
    durable_decided_at = None
    if source_context.get("origin") == TRUSTED_INGRESS_ORIGIN:
        raw_count = source_context.get("continuation_count")
        if isinstance(raw_count, int) and raw_count >= 0:
            durable_continuation_count = raw_count
        durable_decided_at = source_context.get("decided_at")

    effective_continuation_count = max(continuation_count, durable_continuation_count)

    effective_session_start = session_start_time
    if isinstance(durable_decided_at, str):
        try:
            durable_start = parse_utc_time(durable_decided_at)
            effective_session_start = min(session_start_time, durable_start)
        except (ValueError, AttributeError):
            pass

    # AG AC-CHAIN-01: thread the durable chain root through unchanged once
    # established, rather than re-rooting at every hop, so the full lineage
    # can be reconstructed from any single link after a restart.
    durable_root_execution_id = None
    if source_context.get("origin") == TRUSTED_INGRESS_ORIGIN:
        candidate_root = source_context.get("root_execution_id")
        if isinstance(candidate_root, str) and candidate_root.strip():
            durable_root_execution_id = candidate_root
    root_execution_id = durable_root_execution_id or source_execution_id

    # 2. Circuit breaker & Quota
    quota_doc = quota_document
    if quota_doc is None and service is not None:
        try:
            quota_doc = read_drive_status(service=service)
        except Exception as exc:
            return {
                "status": "halted",
                "state": STATE_ATTENTION_REQUIRED,
                "reason": f"quota_read_failed:{exc}",
                "continuation_count": effective_continuation_count,
            }
    quota_sum = summarize(quota_doc, now=current_time, max_age_minutes=60) if quota_doc else None
    circuit_res = evaluate_circuit_breaker(effective_session_start, effective_continuation_count, quota_sum, now=current_time)
    if not circuit_res["allowed"]:
        return {
            "status": "halted",
            "state": circuit_res["state"],
            "reason": circuit_res["reason"],
            "continuation_count": effective_continuation_count,
        }

    # 3. Find next eligible candidate task
    try:
        project = store.get("projects", project_id, project_id)
        validate("project", project)
    except TaskError as exc:
        return {"status": "halted", "state": STATE_BLOCKED, "reason": f"project_not_found:{exc}",
                "continuation_count": effective_continuation_count}

    completed_ids = {source_task_id}
    for t in store.list_records("tasks", project_id):
        if t.get("status") == "completed":
            completed_ids.add(t.get("task_id"))

    # AG AC-SELECT-01: exclude candidates already covered by an active
    # manager.task_claims lease, in addition to executions/Commands. This is
    # deliberately opt-in (via the `task_claim_reader` parameter) rather than
    # auto-constructed from `bucket`: manager.task_claims is explicitly not
    # yet wired into execution_lifecycle.enter_running_gate() as an
    # authoritative gate elsewhere in this codebase (Slice 3A primitive), so
    # silently making every bucket-configured Autopilot call depend on that
    # backend being reachable would be a new hard dependency, not a
    # narrowly-scoped safety fix. A caller that already treats task_claims as
    # authoritative can pass its own reader; build_default_task_claim_reader()
    # below is provided for that purpose.
    selection = find_next_candidate_task(
        store, project_id, completed_ids, project=project,
        git_checker=git_checker, writer_registry_reader=writer_registry_reader, head_reader=head_reader,
        task_claim_reader=task_claim_reader,
    )
    next_task = selection["task"]
    if next_task is None:
        if selection["blocked"]:
            return {
                "status": "halted",
                "state": STATE_BLOCKED,
                "reason": "no_eligible_candidate",
                "blocked_candidates": selection["blocked"],
                "continuation_count": effective_continuation_count,
            }
        return {
            "status": "done",
            "state": STATE_DONE,
            "reason": "all_tasks_completed",
            "continuation_count": effective_continuation_count,
        }

    next_task_id = next_task["task_id"]

    # 4. Scope constraint for Slice 1: read-only only
    if next_task.get("read_only") is not True or next_task.get("needs_repo_edit") is True:
        return {
            "status": "halted",
            "state": STATE_REVIEW_REQUIRED,
            "reason": "slice1_write_task_requires_slice2_activation",
            "next_task_id": next_task_id,
            "continuation_count": effective_continuation_count,
        }

    # 5. Dependency progression (defense-in-depth re-check; find_next_candidate_task
    # already filtered on this, but re-verifying against the exact selected
    # candidate keeps this guarantee independent of selection internals).
    dep_res = evaluate_dependencies(store, project_id, next_task)
    if not dep_res["satisfied"]:
        return {
            "status": "halted",
            "state": dep_res["state"],
            "reason": dep_res["reason"],
            "next_task_id": next_task_id,
            "continuation_count": effective_continuation_count,
        }

    # 6. Dirty-tree / baseline transition (defense-in-depth re-check; see above).
    trans_res = evaluate_repository_transition(store, project, next_task,
                                               writer_registry_reader=writer_registry_reader,
                                               git_checker=git_checker,
                                               head_reader=head_reader)
    if not trans_res["clean"]:
        return {
            "status": "halted",
            "state": trans_res["state"],
            "reason": trans_res["reason"],
            "next_task_id": next_task_id,
            "continuation_count": effective_continuation_count,
        }

    # 7. Persistent Idempotency Claim via GCS CAS (recoverable state machine)
    #
    # AG AC-CAS-01: the Command/request identity is derived purely from
    # stable chain inputs (project_id is implicit in the registry scope;
    # source_execution_id + next_task_id are the stable identifiers) -- NOT
    # from wall-clock time -- so the exact same continuation always resolves
    # to the exact same command_id/request_id after a restart, and that
    # identity is what recovery logic below re-derives and looks up rather
    # than trusting a stored value.
    next_command_id = f"autopilot-{safe_id(source_execution_id)}-{safe_id(next_task_id)}"
    request_id = f"ap-{safe_id(source_execution_id)}-{safe_id(next_task_id)}"
    decided_at = now_iso()
    new_continuation_count = effective_continuation_count + 1
    autopilot_session_id = f"{project_id}:{root_execution_id}"

    reg = None
    claim = None
    if bucket:
        try:
            reg = continuation_registry_factory(bucket, project_id, source_execution_id)
            claim = claim_autopilot_continuation(
                reg, project_id, source_execution_id, source_task_id,
                next_task_id, next_command_id, new_continuation_count, decided_at,
            )
        except Exception as exc:
            return {
                "status": "halted",
                "state": STATE_ATTENTION_REQUIRED,
                "reason": f"continuation_idempotency_backend_unavailable:{exc}",
                "continuation_count": effective_continuation_count,
            }

        if not claim["claimed"]:
            existing_state = claim.get("state")
            if existing_state in (CONT_STATE_DISPATCHED, CONT_STATE_COMPLETED):
                # AG AC-CAS-01: the CAS record alone is not proof of
                # dispatch -- re-derive the deterministic Command identity
                # and inspect durable Command evidence before trusting it.
                expected_command_id = f"autopilot-{safe_id(source_execution_id)}-{safe_id(claim['next_task_id'])}"
                existing_command = None
                try:
                    existing_command = store.get("commands", project_id, expected_command_id)
                    validate("command", existing_command)
                except TaskError:
                    existing_command = None
                if (existing_command is not None
                        and existing_command.get("project_id") == project_id
                        and existing_command.get("task_id") == claim.get("next_task_id")):
                    return {
                        "status": "already_claimed",
                        "state": STATE_DISPATCHED,
                        "existing_claim": claim,
                        "existing_command_id": expected_command_id,
                        "continuation_count": effective_continuation_count,
                    }
                # The claim record says DISPATCHED/COMPLETED but no matching
                # Command exists. A single missing read is not durable proof
                # of absence (could race an eventually-consistent store), so
                # this is never silently reclassified as either dispatched
                # or safe-to-retry -- surface for recovery instead.
                return {
                    "status": "halted",
                    "state": STATE_ATTENTION_REQUIRED,
                    "reason": "continuation_claimed_dispatched_but_command_missing",
                    "existing_claim": claim,
                    "continuation_count": effective_continuation_count,
                }
            # CLAIMED / DISPATCHING / ATTENTION_REQUIRED found on a fresh
            # read: the dispatch outcome is unproven (could be a live
            # in-flight attempt, or a crash) -- never blindly retried.
            return {
                "status": "halted",
                "state": STATE_ATTENTION_REQUIRED,
                "reason": f"continuation_claim_unresolved:{existing_state}",
                "existing_claim": claim,
                "continuation_count": effective_continuation_count,
            }

        try:
            claim = mark_continuation_dispatching(reg, claim)
        except TaskError as exc:
            return {
                "status": "halted",
                "state": STATE_ATTENTION_REQUIRED,
                "reason": f"continuation_state_transition_failed:{exc}",
                "continuation_count": effective_continuation_count,
            }

    # 8. Dispatch next task under the EXISTING Direct Dispatch trusted-ingress
    # contract (AG AC-INGRESS-01). Creating a Drive Command alone is not
    # sufficient: manager.command_watcher.process_command only auto-admits a
    # non-allowlisted Command when manager.trusted_ingress.
    # verify_trusted_ingress_admission proves BOTH (a) the Task's own
    # source_context carries matching origin/admission_version/
    # external_request_id evidence, AND (b) a dispatch-requests idempotency
    # record (manager.dispatch_requests, the same store Direct Dispatch's
    # cloud.dispatch_ingress writes to) corroborates that exact
    # project/task/command/request identity. This reuses that existing,
    # already-proven authority rather than inventing a second one, and
    # rather than letting the Autopilot origin string alone grant trust.
    task_source_context = {
        "origin": TRUSTED_INGRESS_ORIGIN,
        "admission_version": ADMISSION_VERSION,
        "external_request_id": request_id,
        "source_execution_id": source_execution_id,
        "source_task_id": source_task_id,
        "continuation_count": new_continuation_count,
        "continuation_depth": new_continuation_count,
        "root_execution_id": root_execution_id,
        "parent_execution_id": source_execution_id,
        "autopilot_session_id": autopilot_session_id,
        "decided_at": decided_at,
        "goal": next_task.get("source_context", {}).get("goal") or next_task["title"],
    }

    dispatch_request = {
        "project_id": project_id,
        "task_id": next_task_id,
        "title": next_task["title"],
        "task_type": next_task.get("task_type", "general"),
        "complexity": next_task.get("complexity", "medium"),
        "expected_minutes": next_task.get("expected_minutes", 20),
        "needs_repo_edit": False,
        "read_only": True,
        "source_context": task_source_context,
    }

    try:
        result = dispatcher_dispatch(store, service, dispatch_request, quota_document=quota_doc)
    except Exception as exc:
        # Known pre-dispatch failure: dispatcher_dispatch() raised before any
        # Command was created, so nothing was actually dispatched. Safe to
        # recover on a future retry.
        if reg is not None and claim is not None:
            try:
                mark_continuation_failed_safe(reg, claim, reason=str(exc))
            except TaskError:
                pass
        return {
            "status": "halted",
            "state": STATE_RETRY_ELIGIBLE,
            "reason": f"continuation_dispatch_prelaunch_failure:{exc}",
            "continuation_count": effective_continuation_count,
        }

    try:
        # Reuse the existing Direct Dispatch trusted-ingress idempotency
        # authority instead of inventing a second one -- this is exactly
        # what Command Watcher's verify_trusted_ingress_admission
        # cross-checks the Task/Command evidence against. Deterministic
        # (project_id, request_id, next_task_id, next_command_id) makes this
        # safe to call again on any retry of the same continuation.
        if bucket:
            dispatch_reg = dispatch_request_registry_factory(bucket, project_id, request_id)
            claim_dispatch_request(dispatch_reg, project_id, request_id, next_task_id, next_command_id, decided_at)

        update_task(store, project_id, next_task_id, priority=next_task.get("priority", "normal"),
                    read_only=True, execution_policies=sorted(REQUIRED_TASK_POLICIES),
                    source_context=task_source_context)

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
            "request_id": request_id,
        }
        validate("command", command)
        store.put("commands", project_id, next_command_id, command)
    except Exception as exc:
        # Ambiguous outcome: dispatcher_dispatch() already succeeded, but we
        # cannot prove the Task/Command evidence write landed. Must not
        # blindly retry -- that would risk a duplicate dispatch.
        if reg is not None and claim is not None:
            try:
                mark_continuation_attention_required(reg, claim, reason=str(exc))
            except TaskError:
                pass
        return {
            "status": "halted",
            "state": STATE_ATTENTION_REQUIRED,
            "reason": f"continuation_dispatch_evidence_write_failed:{exc}",
            "continuation_count": effective_continuation_count,
        }

    if reg is not None and claim is not None:
        try:
            mark_continuation_dispatched(reg, claim)
        except TaskError as exc:
            # The Command is genuinely persisted at this point -- only the
            # claim record's own final transition failed. Surface this
            # rather than silently leaving the claim stuck at DISPATCHING
            # forever, but do not pretend the real dispatch did not happen.
            return {
                "status": "dispatched",
                "state": STATE_DISPATCHED,
                "source_execution_id": source_execution_id,
                "next_task_id": next_task_id,
                "next_command_id": next_command_id,
                "continuation_count": new_continuation_count,
                "provider": result["provider"],
                "warning": f"continuation_claim_finalize_failed:{exc}",
            }

    return {
        "status": "dispatched",
        "state": STATE_DISPATCHED,
        "source_execution_id": source_execution_id,
        "next_task_id": next_task_id,
        "next_command_id": next_command_id,
        "continuation_count": new_continuation_count,
        "provider": result["provider"],
    }
