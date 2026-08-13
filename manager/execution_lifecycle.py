"""Authoritative reservation-to-running lifecycle gate."""

from manager.executions import persist_terminal, quota_snapshot, task_snapshot
from manager.quota_reader import read_drive_status
from manager.task_claims import CLAIM_SCHEMA_VERSION, TaskClaimConflict, check_task_execution_claim, claim_task_execution, release_task_execution_claim
from manager.tasks import TaskError, create_handoff, now_iso, validate
from manager.worktree_locks import active, acquire, canonical_baseline, canonical_branch, canonical_repository, canonical_scope, owner_fields, read_registry, release, repository_lock_id, same_owner, validate_local_preflight


def _claimed_task(task, execution_id):
    context = dict(task.get("source_context") or {})
    active = context.get("active_execution_id")
    if active and active != execution_id:
        raise TaskClaimConflict(f"task is already claimed by execution {active}")
    context["active_execution_id"] = execution_id
    claimed = {**task, "source_context": context}
    validate("task", claimed)
    return claimed


def _rollback_task_record(store, project_id, task_id, execution_id, original):
    current = store.get("tasks", project_id, task_id)
    if current == original:
        return True
    if (current.get("source_context") or {}).get("active_execution_id") != execution_id:
        # The GCS claim, not this Drive field, is authoritative. Never overwrite
        # evidence that no longer identifies this execution during recovery.
        return True
    store.put("tasks", project_id, task_id, original)
    return store.get("tasks", project_id, task_id) == original


def _rollback_execution(store, project_id, execution_id, original):
    current = store.get("executions", project_id, execution_id)
    if current == original:
        return True
    if current.get("execution_id") != execution_id or current.get("status") != "running":
        return False
    store.put("executions", project_id, execution_id, original)
    return store.get("executions", project_id, execution_id) == original


def enter_running_gate(store, service, registry, project_id, task_id, execution_id, provider, access, baseline_head=None, started_at=None, task_claim_registry=None):
    """Validate and authorize one reservation; never launches a provider."""
    try:
        execution = store.get("executions", project_id, execution_id)
    except KeyError as exc:
        raise TaskError("running gate requires an existing reserved execution") from exc
    except TaskError as exc:
        if "found 0" not in str(exc) and "not found" not in str(exc):
            raise
        raise TaskError("running gate requires an existing reserved execution") from exc
    validate("execution", execution)
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(execution.get(key) != value for key, value in identity.items()):
        raise TaskError("requested running identity does not match the reservation")
    if execution["status"] != "reserved":
        raise TaskError("running gate requires an existing reserved execution")
    task = store.get("tasks", project_id, task_id)
    validate("task", task)
    if task["status"] != "ready":
        raise TaskError("running gate requires the task to remain ready")
    snapshot = execution["task_snapshot"]
    if task_snapshot(task) != snapshot:
        raise TaskError("current task contract does not match the reservation snapshot")
    if task_claim_registry is None:
        raise TaskError("authoritative task claim registry is required")

    lease = None
    expected_lock_id = None
    if access == "read_only":
        if snapshot.get("read_only") is not True or snapshot.get("needs_repo_edit") is True:
            raise TaskError("read-only gate requires an explicitly read-only reservation")
        lease_evidence = None
    elif access == "production_write":
        if snapshot.get("read_only") is True:
            raise TaskError("read-only execution cannot upgrade in place; create a production-write reservation")
        project = store.get("projects", project_id, project_id)
        validate("project", project)
        branch = snapshot.get("branch")
        if not isinstance(branch, str) or not branch.startswith("refs/heads/"):
            raise TaskError("production running gate requires a full branch ref")
        repository_request = project["repo"]
        repository = canonical_repository(repository_request)
        expected_lock_id = repository_lock_id(repository)
        branch = canonical_branch(branch)
        scope = canonical_scope(snapshot.get("allowed_paths"))
        reserved_baseline = canonical_baseline(snapshot.get("baseline_head"))
        if baseline_head is not None and canonical_baseline(baseline_head) != reserved_baseline:
            raise TaskError("requested baseline_head does not match the reservation")
        baseline_head = reserved_baseline
        validate_local_preflight(snapshot.get("working_directory"), repository, branch, baseline_head)
        if registry is None:
            raise TaskError("writer lock registry is required")
        acquired = acquire(registry, project_id, task_id, execution_id, provider, repository=repository_request, branch=branch, scope=scope, baseline_head=baseline_head, working_directory=snapshot.get("working_directory"), preflight_func=validate_local_preflight)
        token = acquired.get("lease_token") if isinstance(acquired, dict) else None
        expected_lease = {**identity, "lock_id": expected_lock_id, "repository": repository, "branch": branch, "scope": scope, "baseline_head": baseline_head}
        try:
            if not isinstance(acquired, dict) or acquired.get("authority") != "acquired" or acquired.get("effective_status") != "active" or not isinstance(token, str) or len(token) < 32 or not isinstance(acquired.get("generation"), int) or any(acquired.get(key) != value for key, value in expected_lease.items()):
                raise TaskError("authoritative writer acquire did not return an active owned lease")
        except Exception as exc:
            if isinstance(token, str) and len(token) >= 32:
                try:
                    release(registry, expected_lock_id, project_id, task_id, execution_id, provider, lease_token=token)
                except Exception as cleanup_exc:
                    raise TaskError(f"acquire validation failed and lease cleanup failed: {cleanup_exc}") from exc
            elif isinstance(acquired, dict) and acquired.get("authority") == "acquired":
                raise TaskError("acquire validation failed; cleanup could not be proven without a valid lease token") from exc
            raise
        lease = acquired
        lease_evidence = {
            "authority": "acquired", "lock_id": lease["lock_id"], "generation": lease["generation"],
            "repository": lease["repository"], "branch": lease["branch"], "scope": lease["scope"],
            "baseline_head": lease["baseline_head"],
        }
    else:
        raise TaskError("access must be production_write or read_only")

    claim = None
    claim_time = started_at or now_iso()
    try:
        claim = claim_task_execution(task_claim_registry, project_id, task_id, execution_id, provider, claim_time)
        expected_claim = {
            "schema_version": CLAIM_SCHEMA_VERSION, "project_id": project_id, "task_id": task_id,
            "execution_id": execution_id, "provider": provider,
        }
        if (not isinstance(claim, dict) or not isinstance(claim.get("generation"), int)
                or claim["generation"] < 1 or any(claim.get(key) != value for key, value in expected_claim.items())):
            raise TaskError("authoritative task claim did not return owned generation evidence")
    except TaskClaimConflict as exc:
        if lease:
            try:
                release(registry, lease["lock_id"], project_id, task_id, execution_id, provider, lease_token=lease["lease_token"])
            except Exception as cleanup_exc:
                raise TaskError(f"task claim conflict and writer lease release failed: {cleanup_exc}") from exc
        raise
    except Exception as exc:
        generation = claim.get("generation") if isinstance(claim, dict) else None
        if isinstance(generation, int) and generation >= 1:
            try:
                release_task_execution_claim(task_claim_registry, project_id, task_id, execution_id, generation)
            except Exception as cleanup_exc:
                raise TaskError(f"task claim validation failed and claim cleanup failed; writer lease retained when present: {cleanup_exc}") from exc
            if lease:
                try:
                    release(registry, lease["lock_id"], project_id, task_id, execution_id, provider, lease_token=lease["lease_token"])
                except Exception as cleanup_exc:
                    raise TaskError(f"task claim validation failed and writer lease release failed: {cleanup_exc}") from exc
        elif lease:
            raise TaskError("task claim acquisition could not be proven; writer lease retained for recovery") from exc
        raise

    running = None
    try:
        current_task = store.get("tasks", project_id, task_id)
        validate("task", current_task)
        if current_task != task:
            raise TaskError("task changed after authoritative claim; running transition blocked")
        before = quota_snapshot(read_drive_status(service=service), provider)
        claimed = _claimed_task(current_task, execution_id)
        in_progress = {
            **claimed, "status": "in_progress", "assigned_provider": provider,
            "blocked_reason": None, "updated_at": now_iso(),
            "current_progress": f"Execution {execution_id} running",
            "next_action": "Launch provider after the running gate",
        }
        validate("task", in_progress)
        store.put("tasks", project_id, task_id, in_progress)
        if store.get("tasks", project_id, task_id) != in_progress:
            raise TaskError("in-progress task persistence verification failed")
        running = {
            **execution, "access": access, "lease_evidence": lease_evidence,
            "started_at": started_at or now_iso(), "status": "running",
            "quota_before": before, "source_confidence": before.get("confidence", "unknown"),
        }
        validate("execution", running)
        store.put("executions", project_id, execution_id, running)
        if store.get("executions", project_id, execution_id) != running:
            raise TaskError("running execution persistence verification failed")
    except Exception as exc:
        recovery_errors = []
        try:
            execution_recovered = _rollback_execution(store, project_id, execution_id, execution)
        except Exception as recovery_exc:
            execution_recovered = False
            recovery_errors.append(f"execution rollback failed: {recovery_exc}")
        task_recovered = False
        if execution_recovered:
            try:
                task_recovered = _rollback_task_record(store, project_id, task_id, execution_id, task)
            except Exception as recovery_exc:
                recovery_errors.append(f"task record rollback failed: {recovery_exc}")
        else:
            recovery_errors.append("task record and authoritative claim retained because execution rollback was not confirmed")
        if not execution_recovered or not task_recovered:
            details = "; ".join(recovery_errors) or "persistent state could not be confirmed"
            raise TaskError(f"running gate recovery required; claims and lease retained when present: {details}") from exc
        try:
            release_task_execution_claim(task_claim_registry, project_id, task_id, execution_id, claim["generation"])
        except Exception as cleanup_exc:
            raise TaskError(f"running gate failed and task claim release failed; writer lease retained when present: {cleanup_exc}") from exc
        if lease:
            try:
                release(registry, lease["lock_id"], project_id, task_id, execution_id, provider, lease_token=lease["lease_token"])
            except Exception as cleanup_exc:
                raise TaskError(f"running gate failed and lease release failed: {cleanup_exc}") from exc
        raise
    return {"execution": running, "lease": lease, "task_claim": claim}


def _verify_terminal_authority(store, writer_registry, claim_registry, project_id, task_id, execution_id, provider, claim_generation, lease_token):
    execution = store.get("executions", project_id, execution_id)
    validate("execution", execution)
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(execution.get(key) != value for key, value in identity.items()):
        raise TaskError("terminal callback identity does not match execution")
    claim = check_task_execution_claim(claim_registry, project_id, task_id)
    if not claim or claim.get("execution_id") != execution_id or claim.get("provider") != provider or claim.get("generation") != claim_generation:
        raise TaskError("terminal callback does not hold the exact task claim generation")
    if execution.get("access") == "production_write":
        lease = execution.get("lease_evidence") or {}
        document, _, server_time = read_registry(writer_registry)
        current = document["locks"].get(lease.get("lock_id"))
        session_id = current.get("session_id") if current else None
        if session_id is not None and session_id != execution.get("session_id"):
            raise TaskError("terminal callback session does not match the writer lease")
        owner = owner_fields(project_id, task_id, execution_id, provider, session_id)
        if not current or current.get("generation") != lease.get("generation") or not active(current, server_time) or not same_owner(current, owner, lease_token):
            raise TaskError("terminal callback does not own the active writer lease generation")
    return execution


def cleanup_execution(writer_registry, claim_registry, execution, claim_generation, lease_token=None):
    """Release writer authority first; release only the supplied claim generation last."""
    evidence = {"writer_release": "not_required", "task_claim_release": "not_attempted", "errors": []}
    if execution.get("access") == "production_write":
        try:
            release(writer_registry, execution["lease_evidence"]["lock_id"], execution["project_id"], execution["task_id"], execution["execution_id"], execution["provider"], lease_token=lease_token)
            evidence["writer_release"] = "released"
        except Exception as exc:
            evidence["writer_release"] = "failed"
            evidence["errors"].append(f"writer release failed: {exc}")
            return evidence
    try:
        result = release_task_execution_claim(claim_registry, execution["project_id"], execution["task_id"], execution["execution_id"], claim_generation)
        evidence["task_claim_release"] = "released" if result.get("released") else result.get("reason", "not_released")
    except Exception as exc:
        evidence["task_claim_release"] = "failed"
        evidence["errors"].append(f"task claim release failed: {exc}")
    return evidence


def _terminal_handoff(execution, status, summary, timestamp):
    return {
        "handoff_id": f"{execution['task_id']}-{status}-{execution['execution_id']}",
        "task_id": execution["task_id"], "project_id": execution["project_id"],
        "created_at": timestamp, "from_provider": execution["provider"], "to_provider": None,
        "from_session": execution.get("session_id"), "reason": status,
        "completed_work": [summary] if status == "completed" else [], "current_state": status,
        "next_action": "" if status == "completed" else "Review outcome and decide whether to resume",
        "minimal_context": summary,
        "acceptance_criteria": execution["task_snapshot"].get("acceptance_criteria", []),
    }


def terminalize_execution(store, service, writer_registry, claim_registry, project_id, task_id, execution_id, provider, status, claim_generation, provider_stopped, lease_token=None, completed_at=None, summary=None):
    if status not in ("completed", "failed", "interrupted"):
        raise TaskError(f"invalid terminal execution status: {status}")
    if provider_stopped is not True:
        raise TaskError("provider process must be confirmed stopped before terminalization")
    existing = store.get("executions", project_id, execution_id)
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(existing.get(key) != value for key, value in identity.items()):
        raise TaskError("terminal callback identity does not match execution")
    if existing.get("status") in ("completed", "failed", "interrupted"):
        if existing["status"] == status:
            return {"execution": existing, "cleanup": existing.get("cleanup_evidence"), "idempotent": True}
        raise TaskError("conflicting duplicate terminal outcome")
    execution = _verify_terminal_authority(store, writer_registry, claim_registry, project_id, task_id, execution_id, provider, claim_generation, lease_token)
    if execution.get("status") != "running":
        raise TaskError("terminalization requires a running execution")

    timestamp = completed_at or now_iso()
    summary = summary or f"Execution {execution_id} {status}"
    primary_error = None
    terminal = execution
    persisted = []
    try:
        terminal = persist_terminal(store, service, project_id, execution_id, status, timestamp, summary)
        persisted.append("execution")
        create_handoff(store, _terminal_handoff(terminal, status, summary, timestamp))
        persisted.append("handoff")
        task = store.get("tasks", project_id, task_id)
        if (task.get("source_context") or {}).get("active_execution_id") != execution_id:
            raise TaskError("task no longer identifies the terminal execution")
        task.update(status="completed" if status == "completed" else "blocked", updated_at=timestamp,
                    blocked_reason=None if status == "completed" else f"Execution {status}: {summary}",
                    current_progress=summary,
                    next_action="" if status == "completed" else "Review outcome and decide whether to resume")
        if status == "completed":
            task["completed_at"] = timestamp
        validate("task", task)
        store.put("tasks", project_id, task_id, task)
        persisted.append("task")
    except Exception as exc:
        primary_error = exc
    finally:
        cleanup = cleanup_execution(writer_registry, claim_registry, execution, claim_generation, lease_token)
        audit = {**cleanup, "provider_outcome": status, "persisted": persisted}
        try:
            current = store.get("executions", project_id, execution_id)
            if current.get("status") == status:
                current["cleanup_evidence"] = audit
                validate("execution", current)
                store.put("executions", project_id, execution_id, current)
                terminal = current
        except Exception as audit_exc:
            cleanup["errors"].append(f"cleanup audit persistence failed: {audit_exc}")
    if primary_error:
        if cleanup["errors"]:
            primary_error.add_note("cleanup: " + "; ".join(cleanup["errors"]))
        raise primary_error
    return {"execution": terminal, "cleanup": cleanup, "idempotent": False}
