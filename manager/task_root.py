"""Strengthened Design A -- Checkpoint A: the single GCS Task Root Object
acquisition path, with a synchronous legacy-migration gate.

One task has exactly one durable GCS authority object -- the same physical
object task_claims.py has always used at task-claims/<project_id>/<task_id>.json.
There is no second "terminal commit" object. This module REPLACES
task_claims.claim_task_execution()/release_task_execution_claim() as the
acquisition/release path for any task migrated to Strengthened Design A:

  * acquire_task_root() is the ONLY way to establish runtime claim
    authority. It performs exactly one CAS decision per attempt that
    covers every case in a single read-then-write round trip: a fresh
    claim, an idempotent same-execution retry, a genuinely different
    active owner (conflict), a new epoch opening after a prior terminal
    epoch's cleanup has released, AND a synchronous legacy-migration
    decision for a pre-Design-A document. Migration and a new claim are
    never decided by two separate CAS calls that could race past each
    other -- a legacy document is always migrated (or left alone on
    conflict) by itself first, and the caller's own claim is then decided
    against the now-migrated document on the same retry loop.

  * release_runtime_claim() marks runtime authority inactive on the
    CURRENT epoch. It NEVER deletes the object -- Checkpoint A's whole
    point is fixing the regression where a preserved-but-inactive object
    made every subsequent retry fail forever against the old
    create_if_absent-based claim function. A legacy (not yet migrated)
    document falls back to reporting "not_strengthened" so a caller still
    on the old path can fall back to task_claims.release_task_execution_claim
    for that one task, but acquisition is what performs the actual
    migration -- release is never responsible for it.

Each epoch is its OWN bind slot: a superseded epoch's terminal facts,
once Checkpoint B adds real terminal-commit binding, are archived into
`epoch_history` and never touched again -- "immutable once bound" means
immutable *within that epoch*, not that the whole object can never change.
Checkpoint A itself does not yet write real terminal binds (that is
Checkpoint B's `proposal_hash`/fence work); it only needs to know whether
the CURRENT epoch's `terminal`/`cleanup` facets look done enough to let a
new epoch open, and archives whatever is there when one does.
"""

from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError
from manager.task_claims import TaskClaimConflict, release_task_execution_claim


SCHEMA_VERSION = "1.0.0"
DEFAULT_ATTEMPTS = 5


def _is_strengthened(document):
    return isinstance(document, dict) and "epoch" in document


def _fresh_epoch_document(project_id, task_id, execution_id, provider, claimed_at, epoch, claim_token=None):
    document = {
        "schema_version": SCHEMA_VERSION, "project_id": project_id, "task_id": task_id,
        "epoch": epoch, "execution_id": execution_id, "provider": provider, "claimed_at": claimed_at,
        "authority_active": True, "terminal": None,
        "materialization": {"status": "absent"}, "cleanup": {"status": "retained"},
        "epoch_history": [],
    }
    if claim_token is not None:
        document["claim_token"] = claim_token
    return document


def _archive_current_epoch(document):
    """Freeze the CURRENT epoch's slot into epoch_history before a new
    epoch opens. Once archived here, Checkpoint B's bind immutability
    guarantee means this entry is never rewritten again."""
    entry = {
        "epoch": document["epoch"], "execution_id": document["execution_id"],
        "provider": document.get("provider"), "claimed_at": document.get("claimed_at"),
        "terminal": document.get("terminal"), "materialization": document.get("materialization"),
        "cleanup": document.get("cleanup"),
    }
    return [*document.get("epoch_history", []), entry]


def _migrate_legacy_document(document, project_id, task_id, linked_execution):
    """Synchronously upgrade one legacy (pre-Design-A) claim document into
    the strengthened shape, on the same CAS write that will later decide
    a claim attempt against it (never migration and claim-grant on one
    write -- see module docstring).

    `linked_execution` is None when no proof of a terminal outcome exists
    for the legacy document's own execution_id (an ordinary in-flight
    legacy claim -- migrated as still actively held by its original
    owner, conservatively, so a differing new claimant is correctly
    refused rather than allowed to race an owner that may still be
    running). When it IS a terminal Execution record, this is the real
    R17 shape: migrated with a minimal honest bind recording that a
    terminal outcome exists, and a cleanup facet derived from whatever
    the legacy cleanup_evidence already proved -- full hash/fence bind
    machinery is Checkpoint B's job; Checkpoint A only needs enough here
    to gate whether a new epoch may legally open."""
    migrated = {
        "schema_version": SCHEMA_VERSION, "project_id": project_id, "task_id": task_id,
        "epoch": 1, "execution_id": document["execution_id"], "provider": document.get("provider"),
        "claimed_at": document.get("claimed_at"), "authority_active": linked_execution is None,
        "terminal": None, "materialization": {"status": "absent"}, "cleanup": {"status": "retained"},
        "epoch_history": [],
    }
    if document.get("claim_token") is not None:
        migrated["claim_token"] = document["claim_token"]
    if linked_execution is not None:
        migrated["terminal"] = {
            "execution_id": linked_execution.get("execution_id"), "outcome": linked_execution.get("status"),
            "legacy_migrated": True,
        }
        cleanup_evidence = linked_execution.get("cleanup_evidence") or {}
        migrated["cleanup"] = {"status": "released" if cleanup_evidence.get("task_claim_release") == "released" else "retained"}
    return migrated


def acquire_task_root(registry, project_id, task_id, execution_id, provider, claimed_at,
                      claim_token=None, legacy_migration_lookup=None, attempts=DEFAULT_ATTEMPTS):
    """The single entry point for establishing runtime claim authority on
    a task. See module docstring for the full case table. Raises
    TaskClaimConflict when a different execution genuinely holds (or, for
    a still-cleaning-up terminal epoch, has not yet released) authority;
    raises TaskError on backend failure or after ambiguous retries."""
    for _ in range(attempts):
        try:
            existing = registry.read_if_exists()
        except Exception as exc:
            raise TaskError("task root backend unavailable during acquisition") from exc

        if existing is None:
            document = _fresh_epoch_document(project_id, task_id, execution_id, provider, claimed_at, 1, claim_token)
            try:
                generation = registry.create_if_absent(document)
            except RegistryConflict:
                continue
            except TaskError:
                continue
            return {**document, "generation": generation}

        document, generation, _server_time = existing
        if document.get("project_id") != project_id or document.get("task_id") != task_id:
            raise TaskError("malformed task root record: identity does not match the claim key")

        if not _is_strengthened(document):
            linked_execution = (legacy_migration_lookup(project_id, task_id, document["execution_id"])
                               if legacy_migration_lookup else None)
            migrated = _migrate_legacy_document(document, project_id, task_id, linked_execution)
            try:
                registry.compare_and_swap(generation, migrated)
            except RegistryConflict:
                continue
            except Exception as exc:
                raise TaskError("task root backend unavailable during legacy migration") from exc
            # Migration alone -- never also grants this caller's claim on
            # the same write. Loop: the claim is decided against the now-
            # migrated document next iteration, so a concurrent migrator
            # and a concurrent new-claimant can never each believe a
            # different single write settled both questions.
            continue

        same_owner = document["execution_id"] == execution_id and document.get("provider") == provider
        if document.get("authority_active"):
            if same_owner:
                return {**document, "generation": generation}
            raise TaskClaimConflict(f"task is already claimed by execution {document['execution_id']}")

        if document.get("terminal") is not None and (document.get("cleanup") or {}).get("status") != "released":
            raise TaskClaimConflict("prior terminal epoch's cleanup has not yet released; retry not yet safe")

        if document.get("terminal") is not None:
            new_document = {
                **document, "epoch": document["epoch"] + 1, "execution_id": execution_id, "provider": provider,
                "claimed_at": claimed_at, "authority_active": True, "terminal": None,
                "materialization": {"status": "absent"}, "cleanup": {"status": "retained"},
                "epoch_history": _archive_current_epoch(document),
            }
        else:
            # Fresh strengthened object (or one whose current epoch never
            # reached terminal, e.g. a prelaunch rollback) with no active
            # owner: reopen the SAME epoch rather than manufacturing an
            # unearned new one.
            new_document = {**document, "execution_id": execution_id, "provider": provider,
                            "claimed_at": claimed_at, "authority_active": True}
        if claim_token is not None:
            new_document["claim_token"] = claim_token
        try:
            new_generation = registry.compare_and_swap(generation, new_document)
        except RegistryConflict:
            continue
        except Exception as exc:
            raise TaskError("task root backend unavailable during acquisition") from exc
        return {**new_document, "generation": new_generation}
    raise TaskError("task root acquisition ambiguous after retries; failing closed")


def release_runtime_claim(registry, project_id, task_id, execution_id, generation, claim_token=None, attempts=DEFAULT_ATTEMPTS):
    """Release runtime authority for the CURRENT epoch. NEVER deletes a
    strengthened Task Root object -- see module docstring. A legacy (not
    yet migrated) document has no bind to preserve and no epoch history to
    protect, so this transparently falls back to the exact pre-Design-A
    task_claims.release_task_execution_claim() physical delete for that
    one call -- acquire_task_root() is solely responsible for migrating a
    task going forward; release never needs to (and must not: a release
    call has no legacy_migration_lookup to decide it correctly)."""
    try:
        existing = registry.read_if_exists()
    except Exception as exc:
        raise TaskError("task root backend unavailable during release") from exc
    if existing is None:
        return {"released": False, "reason": "no active claim"}
    document, current_generation, _server_time = existing
    if document.get("project_id") != project_id or document.get("task_id") != task_id:
        raise TaskError("malformed task root record: identity does not match the claim key")
    if not _is_strengthened(document):
        return release_task_execution_claim(registry, project_id, task_id, execution_id, generation, claim_token=claim_token)
    if document.get("execution_id") != execution_id:
        return {"released": False, "reason": "claim is owned by a different execution/epoch"}
    if current_generation != generation:
        raise TaskClaimConflict("task root generation changed; refusing to release under a stale generation")
    for _ in range(attempts):
        # Checkpoint A owns only the authority facet transition. Once
        # Checkpoint B/C land real terminal-bind and cleanup-lattice
        # semantics, a bound epoch's cleanup facet advances through its
        # own retained/release_pending/released states instead of being
        # force-set here; for an epoch with no bind yet, releasing simply
        # frees authority and there is nothing durable to preserve.
        new_cleanup = document.get("cleanup") if document.get("terminal") is not None else {"status": "released"}
        new_document = {**document, "authority_active": False, "cleanup": new_cleanup}
        try:
            new_generation = registry.compare_and_swap(current_generation, new_document)
        except RegistryConflict:
            try:
                existing = registry.read_if_exists()
            except Exception as exc:
                raise TaskError("task root backend unavailable during release") from exc
            if existing is None:
                return {"released": False, "reason": "task root vanished during release"}
            document, current_generation, _server_time = existing
            if document.get("execution_id") != execution_id:
                return {"released": False, "reason": "claim changed during release"}
            continue
        except Exception as exc:
            raise TaskError("task root backend unavailable during release") from exc
        return {"released": True, "generation": new_generation}
    raise TaskError("runtime claim release ambiguous after retries; failing closed")
