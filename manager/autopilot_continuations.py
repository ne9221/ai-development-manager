"""GCS generation-CAS primitive for Autopilot continuation decision idempotency.

Mirrors manager/dispatch_requests.py and manager/task_claims.py's proven
create-if-absent + resolve pattern, scoped to one automatic continuation
decision per (project_id, source_execution_id). This guarantees that a completed
source execution can trigger at most ONE downstream continuation across polling
cycles, process restarts, or concurrent evaluators.
"""

from manager.gcs_lock_registry import GCSLockRegistry, RegistryConflict
from manager.tasks import TaskError, safe_id


CONTINUATION_SCHEMA_VERSION = "0.1.0"
DEFAULT_AMBIGUOUS_ATTEMPTS = 3


def autopilot_continuation_object_name(project_id, source_execution_id):
    """Canonical, collision-free object key. safe_id() rejects '/', '..', and
    any character outside [A-Za-z0-9._-], so concatenation cannot let one
    identifier's content escape into the other's segment."""
    return f"autopilot-continuations/{safe_id(project_id)}/{safe_id(source_execution_id)}.json"


def autopilot_continuation_registry(bucket, project_id, source_execution_id, session=None):
    return GCSLockRegistry(bucket, autopilot_continuation_object_name(project_id, source_execution_id), session=session)


def _new_record(project_id, source_execution_id, source_task_id, next_task_id, next_command_id, continuation_count, decided_at):
    for name, value in (("project_id", project_id), ("source_execution_id", source_execution_id),
                        ("source_task_id", source_task_id), ("next_task_id", next_task_id),
                        ("next_command_id", next_command_id), ("decided_at", decided_at)):
        if not isinstance(value, str) or not value.strip():
            raise TaskError(f"continuation claim requires a non-empty {name}")
    if not isinstance(continuation_count, int) or continuation_count < 1:
        raise TaskError("continuation_count must be a positive integer")
    return {
        "schema_version": CONTINUATION_SCHEMA_VERSION,
        "project_id": project_id,
        "source_execution_id": source_execution_id,
        "source_task_id": source_task_id,
        "next_task_id": next_task_id,
        "next_command_id": next_command_id,
        "continuation_count": continuation_count,
        "decided_at": decided_at,
    }


def _validate_record(document, project_id, source_execution_id):
    if not isinstance(document, dict) or document.get("schema_version") != CONTINUATION_SCHEMA_VERSION:
        raise TaskError("malformed continuation record: unexpected schema_version")
    for key in ("project_id", "source_execution_id", "source_task_id", "next_task_id", "next_command_id", "decided_at"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise TaskError(f"malformed continuation record: missing {key}")
    if not isinstance(document.get("continuation_count"), int) or document["continuation_count"] < 1:
        raise TaskError("malformed continuation record: invalid continuation_count")
    if document["project_id"] != project_id or document["source_execution_id"] != source_execution_id:
        raise TaskError("malformed continuation record: identity does not match the claim key")
    return document


def _resolve_conflict(registry, project_id, source_execution_id):
    """A definite 412 on create means the object exists -- read back the
    identity the winning submission already claimed."""
    try:
        current, generation, _ = registry.read()
    except Exception as exc:
        raise TaskError("continuation claim backend unavailable while resolving contention") from exc
    return {**_validate_record(current, project_id, source_execution_id), "generation": generation}


def _resolve_ambiguous(registry, record, attempts):
    """create_if_absent raised something other than a definite 412 (timeout,
    connection error, 5xx mid-write): the server-side outcome of *this very
    call* is unknown. Re-read and self-recognize instead of assuming either
    outcome."""
    for _ in range(attempts):
        try:
            existing = registry.read_if_exists()
        except Exception as exc:
            raise TaskError("continuation claim backend unavailable; ambiguous create outcome could not be resolved") from exc
        if existing is None:
            try:
                generation = registry.create_if_absent(record)
                return {**record, "generation": generation, "claimed": True}
            except (RegistryConflict, TaskError):
                continue
        else:
            current, generation, _ = existing
            return {**_validate_record(current, record["project_id"], record["source_execution_id"]), "generation": generation, "claimed": True}
    raise TaskError("continuation claim ambiguous create outcome did not resolve after retries; failing closed")


def claim_autopilot_continuation(registry, project_id, source_execution_id, source_task_id,
                                 next_task_id, next_command_id, continuation_count, decided_at,
                                 attempts=DEFAULT_AMBIGUOUS_ATTEMPTS):
    """Atomically claim exactly one continuation decision for one source execution.

    - First submission: create-if-absent (ifGenerationMatch=0) is the sole
      winner (`claimed: True`) -- only the winner may proceed with the continuation.
    - Retry / duplicate observation: resolves by re-reading and returning the
      already-claimed record (`claimed: False`) -- the caller must not create
      a duplicate Task/Command.
    - Backend unavailable / malformed record: fails closed (TaskError).
    """
    record = _new_record(project_id, source_execution_id, source_task_id, next_task_id,
                         next_command_id, continuation_count, decided_at)
    try:
        generation = registry.create_if_absent(record)
        return {**record, "generation": generation, "claimed": True}
    except RegistryConflict:
        return {**_resolve_conflict(registry, project_id, source_execution_id), "claimed": False}
    except TaskError:
        return _resolve_ambiguous(registry, record, attempts)


def check_autopilot_continuation(registry, project_id, source_execution_id):
    """Read-only check if a continuation decision has already been claimed for
    a source execution. Returns the validated dict or None."""
    try:
        existing = registry.read_if_exists()
    except Exception as exc:
        raise TaskError("continuation claim backend unavailable") from exc
    if existing is None:
        return None
    document, generation, _ = existing
    return {**_validate_record(document, project_id, source_execution_id), "generation": generation}
