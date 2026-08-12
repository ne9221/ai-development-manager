"""Authoritative reservation-to-running lifecycle gate."""

from manager.executions import _mark_execution_running, quota_snapshot, task_snapshot
from manager.quota_reader import read_drive_status
from manager.tasks import TaskError, validate
from manager.worktree_locks import acquire, canonical_baseline, canonical_branch, canonical_repository, canonical_scope, release, validate_local_preflight


def enter_running_gate(store, service, registry, project_id, task_id, execution_id, provider, access, baseline_head=None, started_at=None, acquire_func=acquire, release_func=release, preflight_func=validate_local_preflight):
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

    lease = None
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
        branch = canonical_branch(branch)
        scope = canonical_scope(snapshot.get("allowed_paths"))
        reserved_baseline = canonical_baseline(snapshot.get("baseline_head"))
        if baseline_head is not None and canonical_baseline(baseline_head) != reserved_baseline:
            raise TaskError("requested baseline_head does not match the reservation")
        baseline_head = reserved_baseline
        preflight_func(snapshot.get("working_directory"), repository, branch, baseline_head)
        if registry is None:
            raise TaskError("writer lock registry is required")
        acquired = acquire_func(
            registry, project_id, task_id, execution_id, provider,
            repository=repository_request, branch=branch, scope=scope, baseline_head=baseline_head,
            working_directory=snapshot.get("working_directory"), preflight_func=preflight_func,
        )
        expected_lease = {**identity, "repository": repository, "branch": branch, "scope": scope, "baseline_head": baseline_head}
        if not isinstance(acquired, dict) or acquired.get("authority") != "acquired" or acquired.get("effective_status") != "active" or not isinstance(acquired.get("lease_token"), str) or len(acquired["lease_token"]) < 32 or not isinstance(acquired.get("lock_id"), str) or not isinstance(acquired.get("generation"), int) or any(acquired.get(key) != value for key, value in expected_lease.items()):
            raise TaskError("authoritative writer acquire did not return an active owned lease")
        lease = acquired
        lease_evidence = {
            "authority": "acquired", "lock_id": lease["lock_id"], "generation": lease["generation"],
            "repository": lease["repository"], "branch": lease["branch"], "scope": lease["scope"],
            "baseline_head": lease["baseline_head"],
        }
    else:
        raise TaskError("access must be production_write or read_only")

    try:
        before = quota_snapshot(read_drive_status(service=service), provider)
        running = _mark_execution_running(
            store, project_id, task_id, execution_id, provider, access, lease_evidence,
            before, before.get("confidence", "unknown"), started_at,
        )
    except Exception as exc:
        if lease:
            try:
                release_func(registry, lease["lock_id"], project_id, task_id, execution_id, provider, lease_token=lease["lease_token"])
            except Exception as cleanup_exc:
                raise TaskError(f"running gate failed and lease release failed: {cleanup_exc}") from exc
        raise
    return {"execution": running, "lease": lease}
