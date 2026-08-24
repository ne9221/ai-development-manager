"""GCS generation-CAS primitive for direct-dispatch-ingress request_id idempotency.

Mirrors manager/task_claims.py's proven create-if-absent + resolve pattern,
scoped to one Task+Command creation per (project_id, request_id). This is a
separate, independent authority from task_claims.py: it dedupes *creation* of
a Task/Command from an external request, never launch/execution authority --
that remains solely governed by task_claims.py and the existing Command
Watcher.
"""

from urllib.parse import quote

from manager.gcs_lock_registry import GCS_SCOPE, GCSLockRegistry, RegistryConflict
from manager.tasks import TaskError, safe_id

# Bounded prefix-listing default for list_recent_dispatch_request_ids(): a
# small fixed cap, never an unbounded/full-history scan. See that function's
# docstring for why this exists and how it must be used.
DEFAULT_RECENT_REQUEST_LIST_LIMIT = 20


DISPATCH_REQUEST_SCHEMA_VERSION = "0.1.0"
DEFAULT_AMBIGUOUS_ATTEMPTS = 3
# Additive, optional fields (see _new_record/_validate_record): a record
# written before this change has neither, and .get(..., default) treats that
# exactly like a freshly-claimed, not-yet-resolved request -- so this stays
# fully backward compatible with every record already live in GCS, without a
# schema_version bump (see module docstring update below for why one is not
# needed: DISPATCH_REQUEST_SCHEMA_VERSION is compared for exact equality by
# _validate_record, so bumping it would make every already-live record
# suddenly "malformed" the instant it is next read).
DISPATCH_REQUEST_STATUSES = ("accepted", "dispatched", "failed")
DEFAULT_DISPATCH_REQUEST_STATUS = "accepted"

# A malformed/rejected-before-claim request (bad JSON, schema-invalid,
# unverifiable provenance, oversized, wrong MIME, ...) never reaches the
# (project_id, request_id)-scoped claim registry above -- the payload/
# request_id itself may not even be trustworthy or safely usable as a GCS
# object-path key. Rejection records are instead keyed by the Drive file's
# own id (always present and safe -- see manager.drive_dispatch_ingress's
# metadata contract), a completely separate GCS namespace from
# dispatch-requests/. See record_dispatch_rejection()/
# read_dispatch_rejection_status().
DISPATCH_REJECTION_SCHEMA_VERSION = "0.1.0"
MAX_REJECTION_MESSAGE_LENGTH = 500


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
        # Durable, queryable "request received" truth -- persisted in this
        # SAME create-if-absent write, i.e. strictly before any slow
        # provider/quota/execution-history resolution ever runs. See
        # mark_dispatch_request_status()/read_dispatch_request_status().
        "status": DEFAULT_DISPATCH_REQUEST_STATUS, "failure_reason": None,
    }


def _validate_record(document, project_id, request_id):
    if not isinstance(document, dict) or document.get("schema_version") != DISPATCH_REQUEST_SCHEMA_VERSION:
        raise TaskError("malformed dispatch request record: unexpected schema_version")
    for key in ("project_id", "request_id", "task_id", "command_id", "created_at"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise TaskError(f"malformed dispatch request record: missing {key}")
    if document["project_id"] != project_id or document["request_id"] != request_id:
        raise TaskError("malformed dispatch request record: identity does not match the claim key")
    status = document.get("status", DEFAULT_DISPATCH_REQUEST_STATUS)
    if status not in DISPATCH_REQUEST_STATUSES:
        raise TaskError(f"malformed dispatch request record: invalid status {status!r}")
    failure_reason = document.get("failure_reason")
    if failure_reason is not None and not isinstance(failure_reason, str):
        raise TaskError("malformed dispatch request record: failure_reason must be a string or null")
    return {**document, "status": status, "failure_reason": failure_reason}


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


def mark_dispatch_request_status(registry, project_id, request_id, generation, status, failure_reason=None):
    """Best-effort CAS status transition on an already-claimed request's
    durable claim record -- e.g. "accepted" (the default at claim time) ->
    "dispatched" (Task/Command successfully created) or "failed" (a definite,
    sole-owned pre-artifact failure; see cloud.dispatch_ingress's caller for
    when it is safe to call this with status="failed").

    This is observability, not authority: a lost race (the record changed
    concurrently -- `generation` no longer matches) or any backend/validation
    failure returns None rather than raising, so a caller must never let this
    affect whether the surrounding dispatch itself succeeded or failed. On
    success returns the new generation.
    """
    if status not in DISPATCH_REQUEST_STATUSES:
        raise TaskError(f"invalid dispatch request status: {status!r}")
    if failure_reason is not None and not isinstance(failure_reason, str):
        raise TaskError("failure_reason must be a string or null")
    try:
        current, current_generation, _ = registry.read()
    except Exception:
        return None
    if current_generation != generation:
        return None
    try:
        document = _validate_record(current, project_id, request_id)
    except TaskError:
        return None
    updated = {**document, "status": status, "failure_reason": failure_reason}
    try:
        return registry.compare_and_swap(current_generation, updated)
    except Exception:
        return None


def dispatch_rejection_object_name(file_id):
    """Canonical, collision-free object key for a rejection record, keyed by
    the Drive file's own id -- see the DISPATCH_REJECTION_SCHEMA_VERSION
    comment above for why this is a separate namespace from
    dispatch_request_object_name()."""
    return f"dispatch-rejections/{safe_id(file_id)}.json"


def dispatch_rejection_registry(bucket, file_id, session=None):
    return GCSLockRegistry(bucket, dispatch_rejection_object_name(file_id), session=session)


def _validate_rejection_record(document, file_id):
    if (not isinstance(document, dict)
            or document.get("schema_version") != DISPATCH_REJECTION_SCHEMA_VERSION
            or document.get("status") != "rejected"):
        raise TaskError("malformed dispatch rejection record")
    if not isinstance(document.get("file_id"), str) or document["file_id"] != file_id:
        raise TaskError("malformed dispatch rejection record: file_id does not match the lookup key")
    if not isinstance(document.get("reason_code"), str) or not document["reason_code"].strip():
        raise TaskError("malformed dispatch rejection record: missing reason_code")
    if not isinstance(document.get("created_at"), str) or not document["created_at"].strip():
        raise TaskError("malformed dispatch rejection record: missing created_at")
    message = document.get("message")
    if message is not None and not isinstance(message, str):
        raise TaskError("malformed dispatch rejection record: message must be a string or null")
    return document


def record_dispatch_rejection(registry, file_id, reason_code, message, created_at):
    """Idempotently persist durable "rejected" truth for one Drive ingress
    file that never reached (or never could safely reach) the request_id-
    scoped claim registry -- malformed JSON, schema-invalid payload,
    unverifiable provenance, invalid project_id/provider/account, oversized/
    wrong-MIME, or any other rejection manager.drive_dispatch_ingress.
    poll_drive_dispatch_requests()'s per-candidate handler raises. Never
    creates a Task/Command (this whole code path is reached specifically
    because none was ever attempted), so there is no duplicate-Task risk;
    idempotent because the same file_id namespace only ever needs ONE
    current verdict (a file that was rejected and later corrected/re-polled
    successfully just stops being reached from the exception path at all --
    a stale prior rejection record for it is a harmless, honest historical
    artifact, not something later code trusts as current once the file
    itself has since dispatched successfully).

    Best-effort observability, matching manager.dispatch_requests'
    established contract elsewhere in this module: any backend failure here
    is swallowed (returns None) rather than raised, since a caller here is
    already inside the exception-handling path for the REAL rejection --
    this call recording that rejection durably must never itself become a
    second, masking failure.
    """
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise TaskError("record_dispatch_rejection requires a non-empty reason_code")
    document = {
        "schema_version": DISPATCH_REJECTION_SCHEMA_VERSION, "file_id": file_id, "status": "rejected",
        "reason_code": reason_code, "message": (message[:MAX_REJECTION_MESSAGE_LENGTH] if isinstance(message, str) else None),
        "created_at": created_at,
    }
    try:
        existing = registry.read_if_exists()
    except Exception:
        return None
    try:
        if existing is None:
            registry.create_if_absent(document)
        else:
            _, generation, _ = existing
            registry.compare_and_swap(generation, document)
    except Exception:
        return None
    return document


def read_dispatch_rejection_status(registry, file_id):
    """Best-effort read of one Drive file's durable rejection truth (see
    record_dispatch_rejection). Returns None when no rejection record
    exists for this file_id at all (it was never rejected, or was never
    even seen) -- a real, honest "nothing recorded" answer, distinct from a
    malformed record (which fails closed, matching every other reader in
    this module)."""
    existing = registry.read_if_exists()
    if existing is None:
        return None
    document, generation, _ = existing
    document = _validate_rejection_record(document, file_id)
    return {"status": "rejected", "reason_code": document["reason_code"], "message": document["message"],
            "created_at": document["created_at"], "generation": generation}


def read_dispatch_request_status(registry, project_id, request_id):
    """Best-effort read of one request_id's current durable claim-record
    truth -- status/failure_reason/task_id/command_id/created_at -- for any
    caller (Dashboard, CLI, tests, an audit script) that wants to know
    whether a request was ever received, without waiting for its Task/
    Command to exist. Returns None only when no claim record exists at all
    (the request was never received, or -- pre this change -- a legacy
    release-based rollback already deleted it). A malformed record fails
    closed (TaskError), matching every other reader of this same object.
    """
    existing = registry.read_if_exists()
    if existing is None:
        return None
    document, generation, _ = existing
    document = _validate_record(document, project_id, request_id)
    return {
        "status": document["status"], "failure_reason": document["failure_reason"],
        "task_id": document["task_id"], "command_id": document["command_id"],
        "created_at": document["created_at"], "generation": generation,
    }


def resolve_dispatch_status_for_request(store, registry, project_id, request_id):
    """Canonical status resolution for one ingress request_id -- Task/
    Command truth when a Task already exists, otherwise the durable ingress-
    acceptance claim record's own truth. This is the single query surface
    manager.dashboard_core.compute_dispatch_state()'s `dispatch_request_
    status` parameter is designed to consume, and is directly callable by
    any ADM surface (a status-check endpoint, CLI, or Dashboard) for one
    request_id -- it never fabricates NONE for a request that was actually
    received: if no Task exists yet, the claim record's accepted/dispatched/
    failed truth is returned instead of silence.

    Deterministic (task_id, command_id) = f"dispatch-{request_id}" mirrors
    cloud.dispatch_ingress.handle_dispatch()'s own identity scheme exactly,
    so this needs no separate lookup table to find them.
    """
    task_id = command_id = f"dispatch-{request_id}"

    def _fetch(area, name):
        try:
            return store.get(area, project_id, name)
        except TaskError as exc:
            message = str(exc)
            if "found 0" in message or "not found" in message:
                return None
            raise

    task = _fetch("tasks", task_id)
    if task is not None:
        return {"task": task, "command": _fetch("commands", command_id), "task_id": task_id,
                "command_id": command_id, "dispatch_request_status": None,
                "dispatch_request_read_failed": False}
    # A genuine backend/read failure here (network error, malformed record,
    # GCS unavailable) must NEVER be silently collapsed onto "no record
    # exists" -- those are two different truths for a caller like the
    # Dashboard: "this request_id was never received" vs. "this request_id
    # was received but its status could not be read right now". Losing that
    # distinction is exactly what let a real read failure render as nothing
    # at all (a missing row) instead of an honest UNKNOWN. See
    # dispatch_request_read_failed below -- callers that only look at
    # dispatch_request_status keep working unchanged (None still means "no
    # richer evidence, fall back to has_dispatch_request/UNKNOWN"), this is
    # a purely additive signal for the ones that care about the distinction.
    try:
        status = read_dispatch_request_status(registry, project_id, request_id)
        read_failed = False
    except TaskError:
        status = None
        read_failed = True
    return {"task": None, "command": None, "task_id": task_id, "command_id": command_id,
            "dispatch_request_status": status, "dispatch_request_read_failed": read_failed}


def list_recent_dispatch_request_ids(bucket, project_id, session=None, max_results=DEFAULT_RECENT_REQUEST_LIST_LIMIT):
    """Bounded discovery of candidate request_ids for one project -- used
    ONLY to seed which request_ids a pre-Task Dashboard view should look up
    via resolve_dispatch_status_for_request()/read_dispatch_request_status();
    NEVER treated as state truth itself (a name appearing here proves
    nothing about status -- the resolver call is still the sole source of
    truth for ACCEPTED/REJECTED/FAILED/etc).

    `max_results` is a hard, small, fixed cap (default
    DEFAULT_RECENT_REQUEST_LIST_LIMIT) passed straight through to the GCS
    JSON API's own `maxResults` query param -- this is a single bounded
    listing call, never pagination through full history, and callers must
    not loop past the first page. This is the same "bounded, cached,
    no-full-history-scan" discipline as manager/dashboard.py's existing
    list_records_isolated()/RECENT_RECORD_LIMIT for Drive-backed records --
    prior incidents (unbounded list_executions(), full Drive history scans)
    caused multi-minute Dashboard load latency, and this must not repeat
    that class of bug.

    Fails soft: returns [] on any error (missing bucket, no credentials, GCS
    unreachable, malformed response) rather than raising -- a listing
    failure degrades to "no pre-Task rows shown this refresh", never a
    Dashboard crash, matching every other best-effort observability reader
    in this module.
    """
    if not bucket or not project_id:
        return []
    if session is None:
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=[GCS_SCOPE])
            session = AuthorizedSession(credentials)
        except Exception:
            return []
    prefix = f"dispatch-requests/{safe_id(project_id)}/"
    encoded_bucket = quote(bucket, safe="")
    list_url = f"https://storage.googleapis.com/storage/v1/b/{encoded_bucket}/o"
    try:
        response = session.get(
            list_url, params={"prefix": prefix, "maxResults": int(max_results)}, timeout=10,
        )
        if response.status_code != 200:
            return []
        items = response.json().get("items", [])
    except Exception:
        return []
    request_ids = []
    for item in items:
        name = item.get("name", "")
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        request_ids.append(name[len(prefix):-len(".json")])
    return request_ids[:max_results]
