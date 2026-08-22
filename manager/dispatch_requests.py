"""GCS generation-CAS primitive for direct-dispatch-ingress request_id idempotency.

Mirrors manager/task_claims.py's proven create-if-absent + resolve pattern,
scoped to one Task+Command creation per (project_id, request_id). This is a
separate, independent authority from task_claims.py: it dedupes *creation* of
a Task/Command from an external request, never launch/execution authority --
that remains solely governed by task_claims.py and the existing Command
Watcher.
"""

from manager.gcs_lock_registry import GCSLockRegistry, RegistryConflict
from manager.tasks import TaskError, safe_id


DISPATCH_REQUEST_SCHEMA_VERSION = "0.1.0"
DEFAULT_AMBIGUOUS_ATTEMPTS = 3


class DispatchRequestClaimConflict(TaskError):
    """The request claim changed while a pre-artifact rollback was pending."""


def dispatch_request_object_name(project_id, request_id):
    """Canonical, collision-free object key; safe_id() rejects '/', '..', and
    any character outside [A-Za-z0-9._-], so concatenation cannot let one
    identifier's content escape into the other's segment."""
    return f"dispatch-requests/{safe_id(project_id)}/{safe_id(request_id)}.json"


def dispatch_request_registry(bucket, project_id, request_id, session=None):
    return GCSLockRegistry(bucket, dispatch_request_object_name(project_id, request_id), session=session)


def _new_record(project_id, request_id, task_id, command_id, created_at):
    for name, value in (("project_id", project_id), ("request_id", request_id), ("task_id", task_id), ("command_id", command_id), ("created_at", created_at)):
        if not isinstance(value, str) or not value.strip():
            raise TaskError(f"dispatch request claim requires a non-empty {name}")
    return {
        "schema_version": DISPATCH_REQUEST_SCHEMA_VERSION,
        "project_id": project_id, "request_id": request_id,
        "task_id": task_id, "command_id": command_id,
        "created_at": created_at,
    }


def _validate_record(document, project_id, request_id):
    if not isinstance(document, dict) or document.get("schema_version") != DISPATCH_REQUEST_SCHEMA_VERSION:
        raise TaskError("malformed dispatch request record: unexpected schema_version")
    for key in ("project_id", "request_id", "task_id", "command_id", "created_at"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise TaskError(f"malformed dispatch request record: missing {key}")
    if document["project_id"] != project_id or document["request_id"] != request_id:
        raise TaskError("malformed dispatch request record: identity does not match the claim key")
    return document


def _resolve_conflict(registry, project_id, request_id):
    """A definite 412 on create means the object exists -- read back the
    identity the winning submission already claimed."""
    try:
        current, generation, _ = registry.read()
    except Exception as exc:
        raise TaskError("dispatch request backend unavailable while resolving contention") from exc
    return {**_validate_record(current, project_id, request_id), "generation": generation}


def _resolve_ambiguous(registry, record, attempts):
    """create_if_absent raised something other than a definite 412 (timeout,
    connection error, 5xx mid-write): the server-side outcome of *this very
    call* is unknown. Re-read and self-recognize instead of assuming either
    outcome.

    Both outcomes here resolve to `claimed: True`: unlike task_claims.py's
    per-execution content, every claimant for the same request_id computes
    the identical deterministic (task_id, command_id) record, so an ambiguous
    write that turns out to have landed is indistinguishable from a distinct
    earlier claimant having landed the same content first. Treating it as
    claimed keeps the caller re-running the (idempotent) Task/Command
    creation rather than risking trusting a claim whose downstream creation
    never actually happened -- re-creation with the same deterministic ids
    only overwrites the same Drive file, it never produces a second one.
    Only a definite precondition failure (_resolve_conflict, a real 412)
    proves a distinct prior winner and returns `claimed: False`.
    """
    for _ in range(attempts):
        try:
            existing = registry.read_if_exists()
        except Exception as exc:
            raise TaskError("dispatch request backend unavailable; ambiguous create outcome could not be resolved") from exc
        if existing is None:
            try:
                generation = registry.create_if_absent(record)
                return {**record, "generation": generation, "claimed": True, "created_by_this_call": True}
            except (RegistryConflict, TaskError):
                continue
        else:
            current, generation, _ = existing
            # A timeout after create is intentionally not rollback-eligible:
            # the matching record could have been created by another caller.
            return {**_validate_record(current, record["project_id"], record["request_id"]), "generation": generation, "claimed": True, "created_by_this_call": False}
    raise TaskError("dispatch request ambiguous create outcome did not resolve after retries; failing closed")


def claim_dispatch_request(registry, project_id, request_id, task_id, command_id, created_at, attempts=DEFAULT_AMBIGUOUS_ATTEMPTS):
    """Atomically claim exactly one (task_id, command_id) identity for one
    external request_id.

    - First submission: create-if-absent (ifGenerationMatch=0) is the sole
      winner (`claimed: True`) -- only the winner may create the Task/Command.
    - Retry (same request_id resubmitted, network timeout, or a simultaneous
      duplicate racing the first): resolves by re-reading and returning the
      already-claimed identity (`claimed: False`) -- the caller must not
      create anything a second time.
    - Backend unavailable / malformed record: fails closed (TaskError).
    """
    record = _new_record(project_id, request_id, task_id, command_id, created_at)
    try:
        generation = registry.create_if_absent(record)
        return {**record, "generation": generation, "claimed": True, "created_by_this_call": True}
    except RegistryConflict:
        return {**_resolve_conflict(registry, project_id, request_id), "claimed": False, "created_by_this_call": False}
    except TaskError:
        return _resolve_ambiguous(registry, record, attempts)


def release_dispatch_request_claim(registry, project_id, request_id, task_id, command_id, generation):
    """Release only this exact generation of a pre-artifact request claim.

    This is deliberately a conditional delete, never a best-effort cleanup:
    a stale caller cannot remove a newer request claimant's authority.
    """
    try:
        existing = registry.read_if_exists()
    except Exception as exc:
        raise TaskError("dispatch request backend unavailable during release") from exc
    if existing is None:
        return {"released": False, "reason": "no active claim"}
    document, current_generation, _ = existing
    _validate_record(document, project_id, request_id)
    if document["task_id"] != task_id or document["command_id"] != command_id:
        return {"released": False, "reason": "claim identity differs"}
    if current_generation != generation:
        raise DispatchRequestClaimConflict("dispatch request generation changed; refusing stale release")
    try:
        registry.delete_if_generation_matches(current_generation)
    except RegistryConflict as exc:
        raise DispatchRequestClaimConflict("dispatch request changed concurrently; release aborted") from exc
    except Exception as exc:
        try:
            confirmed = registry.read_if_exists()
        except Exception as reread_exc:
            raise TaskError("dispatch request release outcome is ambiguous") from reread_exc
        if confirmed is None:
            return {"released": True, "generation": current_generation, "confirmed_after_ambiguous_delete": True}
        raise TaskError("dispatch request release outcome is ambiguous") from exc
    return {"released": True, "generation": current_generation}
