"""Authoritative GCS generation-CAS primitive for one Task's active-execution claim.

This is a new, independent authority: `task-claims/<project_id>/<task_id>.json`,
one dedicated GCS object per task. It is not the existing worktree writer-lock
registry, and it does not make the Drive Task JSON itself CAS-authoritative.
Atomicity comes entirely from the GCS `ifGenerationMatch` precondition on this
one object; nothing here proves cross-machine correctness on its own.

Slice 3A scope: this primitive only. It is not wired into
`execution_lifecycle.enter_running_gate()` yet.
"""

from manager.gcs_lock_registry import GCSLockRegistry, RegistryConflict
from manager.tasks import TaskError, safe_id


CLAIM_SCHEMA_VERSION = "0.1.0"
DEFAULT_AMBIGUOUS_ATTEMPTS = 3


class TaskClaimConflict(TaskError):
    pass


def task_claim_object_name(project_id, task_id):
    """Canonical, collision-free object key. safe_id() rejects '/', '..', and
    any character outside [A-Za-z0-9._-], so concatenation below cannot let
    one identifier's content escape into the other's segment, and no
    case-folding is applied beyond the project's existing safe_id contract."""
    return f"task-claims/{safe_id(project_id)}/{safe_id(task_id)}.json"


def task_claim_registry(bucket, project_id, task_id, session=None):
    return GCSLockRegistry(bucket, task_claim_object_name(project_id, task_id), session=session)


def _new_claim_record(project_id, task_id, execution_id, provider, claimed_at):
    for name, value in (("project_id", project_id), ("task_id", task_id), ("execution_id", execution_id), ("provider", provider), ("claimed_at", claimed_at)):
        if not isinstance(value, str) or not value.strip():
            raise TaskError(f"task claim requires a non-empty {name}")
    return {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "project_id": project_id, "task_id": task_id,
        "execution_id": execution_id, "provider": provider,
        "claimed_at": claimed_at,
    }


def _validate_claim_record(document, project_id, task_id):
    if not isinstance(document, dict) or document.get("schema_version") != CLAIM_SCHEMA_VERSION:
        raise TaskError("malformed task claim record: unexpected schema_version")
    for key in ("project_id", "task_id", "execution_id", "provider", "claimed_at"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise TaskError(f"malformed task claim record: missing {key}")
    if document["project_id"] != project_id or document["task_id"] != task_id:
        raise TaskError("malformed task claim record: identity does not match the claim key")
    return document


def _same_owner(document, record):
    return document.get("project_id") == record["project_id"] and document.get("task_id") == record["task_id"] and document.get("execution_id") == record["execution_id"]


def _resolve_conflict(registry, record):
    """A definite 412 on create means the object exists; resolve ownership."""
    try:
        current, generation, _ = registry.read()
    except Exception as exc:
        raise TaskError("task claim backend unavailable while resolving contention") from exc
    _validate_claim_record(current, record["project_id"], record["task_id"])
    if _same_owner(current, record):
        return {**current, "generation": generation}
    raise TaskClaimConflict(f"task is already claimed by execution {current['execution_id']}")


def _resolve_ambiguous(registry, record, attempts):
    """create_if_absent raised something other than a definite 412 (timeout,
    connection error, 5xx mid-write): we cannot tell whether the server-side
    create actually landed. Re-read and self-recognize instead of assuming
    either outcome."""
    for _ in range(attempts):
        try:
            existing = registry.read_if_exists()
        except Exception as exc:
            raise TaskError("task claim backend unavailable; ambiguous create outcome could not be resolved") from exc
        if existing is None:
            # Provably absent: the earlier attempt did not persist. Safe to retry.
            try:
                generation = registry.create_if_absent(record)
                return {**record, "generation": generation}
            except (RegistryConflict, TaskError):
                continue
        else:
            current, generation, _ = existing
            _validate_claim_record(current, record["project_id"], record["task_id"])
            if _same_owner(current, record):
                return {**current, "generation": generation}
            raise TaskClaimConflict(f"task is already claimed by execution {current['execution_id']}")
    raise TaskError("task claim ambiguous create outcome did not resolve after retries; failing closed")


def claim_task_execution(registry, project_id, task_id, execution_id, provider, claimed_at, attempts=DEFAULT_AMBIGUOUS_ATTEMPTS):
    """Atomically claim exclusive active-execution authority for one task.

    - First claim: create-if-absent (ifGenerationMatch=0) is the sole winner.
    - Same execution retry: a definite or ambiguous "already exists" resolves
      by re-reading and recognizing our own execution_id as idempotent success.
    - Different execution: re-read finds another owner -> TaskClaimConflict;
      the loser never writes the object.
    - Backend unavailable / malformed record: fails closed (TaskError).
    """
    record = _new_claim_record(project_id, task_id, execution_id, provider, claimed_at)
    try:
        generation = registry.create_if_absent(record)
        return {**record, "generation": generation}
    except RegistryConflict:
        return _resolve_conflict(registry, record)
    except TaskError:
        return _resolve_ambiguous(registry, record, attempts)


def check_task_execution_claim(registry, project_id, task_id):
    """Read-only inspection. Returns None if unclaimed, never authorizes a write."""
    try:
        existing = registry.read_if_exists()
    except Exception as exc:
        raise TaskError("task claim backend unavailable") from exc
    if existing is None:
        return None
    document, generation, _ = existing
    return {**_validate_claim_record(document, project_id, task_id), "generation": generation}


def release_task_execution_claim(registry, project_id, task_id, execution_id, generation):
    """Generation-matched release. Never a blind delete: verifies the caller's
    execution_id still owns the claim and that the generation the caller holds
    is still current before deleting, so a stale rollback can never remove a
    later execution's claim (ABA-safe)."""
    try:
        existing = registry.read_if_exists()
    except Exception as exc:
        raise TaskError("task claim backend unavailable during release") from exc
    if existing is None:
        return {"released": False, "reason": "no active claim"}
    document, current_generation, _ = existing
    _validate_claim_record(document, project_id, task_id)
    if document["execution_id"] != execution_id:
        return {"released": False, "reason": "claim is owned by a different execution"}
    if current_generation != generation:
        raise TaskClaimConflict("task claim generation changed; refusing to release under a stale generation")
    try:
        registry.delete_if_generation_matches(current_generation)
    except RegistryConflict as exc:
        raise TaskClaimConflict("task claim changed concurrently; release aborted") from exc
    except Exception as exc:
        # A delete timeout may mean that GCS committed the conditional delete
        # before the client lost the response. Re-read once: absence proves our
        # exact generation is gone; any present or unreadable state stays closed.
        try:
            confirmed = check_task_execution_claim(registry, project_id, task_id)
        except Exception as reread_exc:
            raise TaskError("task claim release outcome is ambiguous") from reread_exc
        if confirmed is None:
            return {"released": True, "generation": current_generation, "confirmed_after_ambiguous_delete": True}
        raise TaskError("task claim release outcome is ambiguous") from exc
    return {"released": True, "generation": current_generation}
