"""GCS generation-CAS primitive for Autopilot continuation decision idempotency.

Mirrors manager/dispatch_requests.py and manager/task_claims.py's proven
create-if-absent + resolve pattern, scoped to one automatic continuation
decision per (project_id, source_execution_id). This guarantees that a completed
source execution can trigger at most ONE downstream continuation across polling
cycles, process restarts, or concurrent evaluators.

The claim record is a small recoverable state machine (Codex P0-4), not a
single irreversible CAS flag:

    CLAIMED -> DISPATCHING -> DISPATCHED
                     \\-> FAILED_SAFE (recoverable: retry allowed)
                     \\-> ATTENTION_REQUIRED (ambiguous outcome: never auto-retried)

CLAIMED means the winner of the create-if-absent race has been decided but no
dispatch attempt has started. DISPATCHING means an attempt is in flight -- a
process that crashes here leaves the record observably stuck in DISPATCHING
(or CLAIMED, if it crashed before even starting), and a fresh reader must
treat both as ATTENTION_REQUIRED rather than guessing. FAILED_SAFE is only
ever reached when the failure is *proven* to have happened before any
Command was created (nothing was actually dispatched), so it is the one
state a later caller may safely retry from. ATTENTION_REQUIRED covers every
outcome where dispatch may or may not have actually happened; it is a dead
end for automatic retry by design.
"""

from manager.gcs_lock_registry import GCSLockRegistry, RegistryConflict
from manager.tasks import TaskError, safe_id


CONTINUATION_SCHEMA_VERSION = "0.2.0"
DEFAULT_AMBIGUOUS_ATTEMPTS = 3

STATE_CLAIMED = "CLAIMED"
STATE_DISPATCHING = "DISPATCHING"
STATE_DISPATCHED = "DISPATCHED"
STATE_FAILED_SAFE = "FAILED_SAFE"
STATE_ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
STATE_COMPLETED = "COMPLETED"

CONTINUATION_STATES = {
    STATE_CLAIMED, STATE_DISPATCHING, STATE_DISPATCHED,
    STATE_FAILED_SAFE, STATE_ATTENTION_REQUIRED, STATE_COMPLETED,
}

# States a fresh (non-winning) reader may safely self-retry from -- proven to
# have made zero external dispatch progress.
_RETRYABLE_STATES = {STATE_FAILED_SAFE}

# Terminal-for-idempotency states: an existing claim in one of these means a
# real dispatch has already happened (or is settled), so callers must never
# create a second Command for the same source_execution_id.
_ALREADY_DISPATCHED_STATES = {STATE_DISPATCHED, STATE_COMPLETED}


def autopilot_continuation_object_name(project_id, source_execution_id):
    """Canonical, collision-free object key. safe_id() rejects '/', '..', and
    any character outside [A-Za-z0-9._-], so concatenation cannot let one
    identifier's content escape into the other's segment."""
    return f"autopilot-continuations/{safe_id(project_id)}/{safe_id(source_execution_id)}.json"


def autopilot_continuation_registry(bucket, project_id, source_execution_id, session=None):
    return GCSLockRegistry(bucket, autopilot_continuation_object_name(project_id, source_execution_id), session=session)


def _new_record(project_id, source_execution_id, source_task_id, next_task_id, next_command_id,
                continuation_count, decided_at, state=STATE_CLAIMED):
    for name, value in (("project_id", project_id), ("source_execution_id", source_execution_id),
                        ("source_task_id", source_task_id), ("next_task_id", next_task_id),
                        ("next_command_id", next_command_id), ("decided_at", decided_at)):
        if not isinstance(value, str) or not value.strip():
            raise TaskError(f"continuation claim requires a non-empty {name}")
    if not isinstance(continuation_count, int) or continuation_count < 1:
        raise TaskError("continuation_count must be a positive integer")
    if state not in CONTINUATION_STATES:
        raise TaskError(f"continuation claim requires a valid state, got {state!r}")
    return {
        "schema_version": CONTINUATION_SCHEMA_VERSION,
        "project_id": project_id,
        "source_execution_id": source_execution_id,
        "source_task_id": source_task_id,
        "next_task_id": next_task_id,
        "next_command_id": next_command_id,
        "continuation_count": continuation_count,
        "decided_at": decided_at,
        "state": state,
    }


def _validate_record(document, project_id, source_execution_id):
    if not isinstance(document, dict) or document.get("schema_version") != CONTINUATION_SCHEMA_VERSION:
        raise TaskError("malformed continuation record: unexpected schema_version")
    for key in ("project_id", "source_execution_id", "source_task_id", "next_task_id", "next_command_id", "decided_at"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise TaskError(f"malformed continuation record: missing {key}")
    if not isinstance(document.get("continuation_count"), int) or document["continuation_count"] < 1:
        raise TaskError("malformed continuation record: invalid continuation_count")
    if document.get("state") not in CONTINUATION_STATES:
        raise TaskError("malformed continuation record: invalid state")
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


def _retry_failed_safe(registry, existing_record, fresh_record):
    """A FAILED_SAFE record proves the previous attempt never reached Command
    creation -- nothing was actually dispatched -- so reusing this object's
    CAS slot for a brand new attempt is safe. Only ever moves FAILED_SAFE ->
    CLAIMED, and only under a generation-matched compare-and-swap, so a
    concurrent retrier can win at most once. Returns None (never raises) if
    the record is not actually FAILED_SAFE or the CAS loses a race -- the
    caller falls back to treating it as an ordinary already-claimed read."""
    if existing_record.get("state") not in _RETRYABLE_STATES:
        return None
    updated = {**fresh_record, "state": STATE_CLAIMED}
    try:
        new_generation = registry.compare_and_swap(existing_record["generation"], updated)
    except (RegistryConflict, TaskError):
        return None
    return {**updated, "generation": new_generation}


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
            validated = {**_validate_record(current, record["project_id"], record["source_execution_id"]), "generation": generation}
            retried = _retry_failed_safe(registry, validated, record)
            if retried is not None:
                return {**retried, "claimed": True}
            return {**validated, "claimed": True}
    raise TaskError("continuation claim ambiguous create outcome did not resolve after retries; failing closed")


def claim_autopilot_continuation(registry, project_id, source_execution_id, source_task_id,
                                 next_task_id, next_command_id, continuation_count, decided_at,
                                 attempts=DEFAULT_AMBIGUOUS_ATTEMPTS):
    """Atomically claim exactly one continuation decision for one source execution.

    - First submission: create-if-absent (ifGenerationMatch=0) is the sole
      winner (`claimed: True`, `state: CLAIMED`) -- only the winner may
      proceed to attempt a dispatch.
    - Retry / duplicate observation while a real attempt is claimed, in
      flight, dispatched, or requires attention: resolves by re-reading and
      returning the already-claimed record (`claimed: False`) -- the caller
      must not create a duplicate Task/Command and must inspect `state` to
      decide how to report the situation.
    - Retry observation of a FAILED_SAFE record (a previous attempt proven
      to have failed before any Command was created): automatically
      recovers by CAS'ing FAILED_SAFE -> CLAIMED with the caller's fresh
      identity, returned as `claimed: True` so the caller proceeds exactly
      as it would on a first claim.
    - Backend unavailable / malformed record: fails closed (TaskError).
    """
    record = _new_record(project_id, source_execution_id, source_task_id, next_task_id,
                         next_command_id, continuation_count, decided_at, state=STATE_CLAIMED)
    try:
        generation = registry.create_if_absent(record)
        return {**record, "generation": generation, "claimed": True}
    except RegistryConflict:
        existing = _resolve_conflict(registry, project_id, source_execution_id)
        retried = _retry_failed_safe(registry, existing, record)
        if retried is not None:
            return {**retried, "claimed": True}
        return {**existing, "claimed": False}
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


def _transition(registry, record, generation, new_state, allowed_from, extra=None):
    """Generation-matched state transition, enforced purely by the CAS
    precondition (no extra read): if another writer touched the object since
    `generation` was observed, the compare_and_swap is rejected and this
    fails closed rather than silently overwriting a concurrent transition."""
    if record.get("state") not in allowed_from:
        raise TaskError(f"continuation claim in unexpected state for transition: {record.get('state')}")
    updated = {**{k: v for k, v in record.items() if k != "generation"}, "state": new_state, **(extra or {})}
    try:
        new_generation = registry.compare_and_swap(generation, updated)
    except RegistryConflict as exc:
        raise TaskError("continuation claim changed concurrently; transition aborted") from exc
    except TaskError:
        raise
    return {**updated, "generation": new_generation}


def mark_continuation_dispatching(registry, record):
    """CLAIMED -> DISPATCHING, immediately before the external dispatch call.
    A crash after this point is observable: a fresh reader sees DISPATCHING
    and must not assume either success or failure."""
    return _transition(registry, record, record["generation"], STATE_DISPATCHING, {STATE_CLAIMED})


def mark_continuation_dispatched(registry, record):
    """DISPATCHING -> DISPATCHED, only after the Command has been durably
    persisted. This is the sole state that makes the continuation permanently
    idempotent against replays."""
    return _transition(registry, record, record["generation"], STATE_DISPATCHED, {STATE_DISPATCHING})


def mark_continuation_failed_safe(registry, record, reason=None):
    """CLAIMED/DISPATCHING -> FAILED_SAFE. Use only when the failure is
    proven to have happened before any Command was created -- this is the
    one state a later caller is allowed to automatically retry from."""
    extra = {"failure_reason": str(reason)[:500]} if reason else {}
    return _transition(registry, record, record["generation"], STATE_FAILED_SAFE,
                       {STATE_CLAIMED, STATE_DISPATCHING}, extra=extra)


def mark_continuation_attention_required(registry, record, reason=None):
    """CLAIMED/DISPATCHING -> ATTENTION_REQUIRED. Use whenever the dispatch
    outcome is ambiguous (e.g. the Command write itself failed or timed out
    after dispatch computation succeeded) -- never auto-retried."""
    extra = {"failure_reason": str(reason)[:500]} if reason else {}
    return _transition(registry, record, record["generation"], STATE_ATTENTION_REQUIRED,
                       {STATE_CLAIMED, STATE_DISPATCHING}, extra=extra)


def mark_continuation_completed(registry, record):
    """DISPATCHED -> COMPLETED. Provided for symmetry with the documented
    state model; nothing in Slice 1 calls this automatically yet since no
    watcher currently reports the downstream execution's own completion back
    into this registry -- wiring that up is future (Slice 2+) work, not part
    of this fix."""
    return _transition(registry, record, record["generation"], STATE_COMPLETED, {STATE_DISPATCHED})
