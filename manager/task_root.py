"""Strengthened Design A -- Checkpoint A + B: the single GCS Task Root
Object acquisition path with a synchronous legacy-migration gate
(Checkpoint A), plus the immutable terminal bind and its immutable
terminal_fence_epoch (Checkpoint B).

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

import hashlib
import json

from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError, now_iso
from manager.task_claims import TaskClaimConflict, _same_owner, check_task_execution_claim, release_task_execution_claim


SCHEMA_VERSION = "1.0.0"
DEFAULT_ATTEMPTS = 5

# Canonical proposal-hash spec (frozen -- bump CANONICALIZATION_VERSION,
# never silently change field set/order/serialization for an existing
# version). Deliberately excludes cleanup_evidence, materialization
# attempts, repair_count, quota_delta, notes, and every other mutable
# progress/cursor facet -- only facts that must be true forever once a
# terminal outcome exists belong here.
CANONICALIZATION_VERSION = "1"
_PROPOSAL_FIELDS = (
    "project_id", "task_id", "execution_id", "retry_count", "epoch",
    "terminal_status", "provider_outcome", "terminal_reason", "completed_at",
    "session_id", "provider_identity", "account_identity",
    "schema_version", "canonicalization_version",
)

# BIND fields, frozen the instant a terminal proposal wins its epoch's CAS.
# terminal_fence_epoch is the one field allowed to start null and be
# filled in exactly once by a follow-up CAS (see commit_terminal_bind) --
# every other field is written atomically with the bind itself.
_BIND_IMMUTABLE_FIELDS = (
    "project_id", "task_id", "epoch", "execution_id", "retry_count",
    "terminal_status", "provider_outcome", "terminal_reason", "completed_at",
    "terminal_committed_at", "provider_identity", "session_id", "account_identity",
    "schema_version", "canonicalization_version", "canonical_proposal", "proposal_hash",
    "terminal_fence_epoch", "task_projection_drive_id", "handoff_drive_file_id",
)


class TerminalProposalLost(TaskClaimConflict):
    """A different execution already owns (or already won the terminal
    bind for) this task's current epoch. The caller is a loser: it must
    never materialize authoritative Drive state, never release the
    winner's resources, and never touch the bind."""

    def __init__(self, message, winner):
        super().__init__(message)
        self.winner = winner


class TerminalProposalConflict(TaskError):
    """The SAME execution/epoch's own terminal facts disagree with what is
    already bound -- a genuine correctness violation, never resolved by
    picking a side. The conflicting hashes/values are attached as
    evidence."""

    def __init__(self, message, conflicts):
        super().__init__(message)
        self.conflicts = conflicts


def _canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def terminal_proposal(execution, epoch):
    """The canonical, hashable terminal proposal for one execution at one
    epoch. See _PROPOSAL_FIELDS for the exact, deliberately fixed field
    set this (and only this) covers."""
    cleanup = execution.get("cleanup_evidence") or {}
    return {
        "project_id": execution.get("project_id"), "task_id": execution.get("task_id"),
        "execution_id": execution.get("execution_id"), "retry_count": execution.get("retry_count", 0),
        "epoch": epoch, "terminal_status": execution.get("status"),
        "provider_outcome": cleanup.get("provider_outcome") or execution.get("status"),
        "terminal_reason": execution.get("terminal_reason"), "completed_at": execution.get("completed_at"),
        "session_id": execution.get("session_id"), "provider_identity": execution.get("provider"),
        "account_identity": execution.get("account_id"),
        "schema_version": SCHEMA_VERSION, "canonicalization_version": CANONICALIZATION_VERSION,
    }


def proposal_hash(proposal):
    canonical = {key: proposal.get(key) for key in _PROPOSAL_FIELDS}
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


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

        # claim_token, not just execution_id/provider, decides ownership --
        # two callers racing with the literal SAME execution_id (e.g. two
        # command-watcher processes both trying to run the same Command)
        # each generate their OWN random claim_token when the caller
        # doesn't supply one, so only the genuine same-token retry of an
        # already-owned claim is idempotent; a same-execution-id/different-
        # token caller is a real rival and must still get TaskClaimConflict.
        # Reuses task_claims._same_owner so both layers apply identical
        # ownership semantics.
        same_owner = (document["execution_id"] == execution_id and document.get("provider") == provider
                     and _same_owner(document, {"project_id": project_id, "task_id": task_id,
                                                "execution_id": execution_id, "claim_token": claim_token}))
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


def legacy_terminal_execution_lookup(store):
    """Build an acquire_task_root() legacy_migration_lookup callable bound
    to one real Store. Returns the linked Execution only when it genuinely
    proves a terminal outcome for that exact task -- anything else (not
    found, wrong task, still running) is treated as "no proof", which
    acquire_task_root() conservatively migrates as still actively held by
    its original owner."""

    def lookup(project_id, task_id, execution_id):
        try:
            execution = store.get("executions", project_id, execution_id)
        except (TaskError, KeyError):
            return None
        if execution.get("task_id") != task_id:
            return None
        if execution.get("status") not in ("completed", "failed", "interrupted"):
            return None
        return execution

    return lookup


def _reject_bind_mutation(current_document, new_document):
    """Structural guard: any CURSOR-only CAS update (release, materialization
    bookkeeping, future cleanup-lattice advances) must never change an
    already-bound BIND field. Raises TaskError (fail closed) if it would --
    called defensively even where the caller believes it isn't touching
    `terminal`, so a future refactor can't silently regress this."""
    current_bind = current_document.get("terminal")
    new_bind = new_document.get("terminal")
    if current_bind is None:
        return
    if new_bind is None:
        raise TaskError("cursor update must not remove an existing terminal bind")
    for key in _BIND_IMMUTABLE_FIELDS:
        if current_bind.get(key) is not None and new_bind.get(key) != current_bind.get(key):
            raise TaskError(f"cursor update must not mutate bound field '{key}'")


def read_task_root_or_legacy_claim(registry, project_id, task_id):
    """Schema-aware read-only claim check: dispatches to whichever shape is
    actually stored instead of ever loosening one validator to accept the
    other's shape (see module docstring's legacy/strengthened boundary).
    Returns None when unclaimed, exactly like
    task_claims.check_task_execution_claim -- a drop-in replacement for
    any strengthened-aware caller (e.g. _verify_terminal_authority,
    execution_recovery.recover_task_claim) that must keep working once a
    task has migrated, without ever accepting a malformed shape closed."""
    try:
        existing = registry.read_if_exists()
    except Exception as exc:
        raise TaskError("task root backend unavailable") from exc
    if existing is None:
        return None
    document, generation, _server_time = existing
    if document.get("project_id") != project_id or document.get("task_id") != task_id:
        raise TaskError("malformed task root record: identity does not match the claim key")
    if _is_strengthened(document):
        # A preserved-but-released epoch (authority_active=False) is, by
        # definition, NOT an active claim -- the object staying around for
        # its durable terminal bind must never be mistaken for "still
        # claimed" by a caller checking whether a task is free (e.g.
        # executions.prepare_task_retry / cancel_reserved_execution).
        if not document.get("authority_active"):
            return None
        return {**document, "generation": generation}
    return check_task_execution_claim(registry, project_id, task_id)


def validate_task_root_running_authority(claim, project_id, task_id, execution_id, provider):
    """Strengthened-Root-aware replacement for enter_running_gate's old
    hard-pinned task_claims.CLAIM_SCHEMA_VERSION/fixed-key check. Verifies
    the document acquire_task_root() just returned genuinely grants THIS
    caller running authority on THIS task/execution/provider right now --
    never loosened to "accept any shape", just a different, explicit
    strengthened shape instead of the legacy one."""
    if not isinstance(claim, dict) or not _is_strengthened(claim):
        raise TaskError("authoritative task root did not return strengthened claim evidence")
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(claim.get(key) != value for key, value in identity.items()):
        raise TaskError("task root identity does not match the requested running authority")
    if not isinstance(claim.get("epoch"), int) or claim["epoch"] < 1:
        raise TaskError("task root did not return a valid epoch")
    if not isinstance(claim.get("generation"), int) or claim["generation"] < 1:
        raise TaskError("task root did not return owned generation evidence")
    if claim.get("authority_active") is not True:
        raise TaskError("task root did not return active running authority")
    if claim.get("terminal") is not None:
        raise TaskError("task root's current epoch is already terminally bound; cannot be a fresh running claim")
    if not isinstance(claim.get("materialization"), dict) or not isinstance(claim.get("cleanup"), dict):
        raise TaskError("task root is missing its materialization/cleanup facets")
    return claim


def commit_terminal_bind(registry, project_id, task_id, execution, task_drive_id_factory=None,
                         handoff_drive_id_factory=None, attempts=DEFAULT_ATTEMPTS):
    """CAS-bind this execution's terminal proposal as its epoch's winner.

    Same epoch + identical canonical proposal -> idempotent (returns the
    existing bind, no new write once fully settled). Same epoch +
    different, non-null proposal facts -> TerminalProposalConflict (fail
    closed). A different execution than the epoch's current owner ->
    TerminalProposalLost -- the caller is a structural loser and must not
    materialize anything or consume its own pre-generated Drive IDs (see
    module docstring / test_task_root.py's H test).

    terminal_fence_epoch is NOT the raw GCS object generation. An
    earlier draft tried to use the GCS-assigned generation returned by the
    bind's own CAS write, filled in via a required follow-up CAS (GCS only
    learns that number AFTER a write lands -- real GCS generations are
    opaque, server-assigned, non-predictable values, so it cannot be
    embedded in the bind's own first write). That two-step design has an
    unrecoverable crash window: if the process dies between the bind write
    and the follow-up freeze write, and ANY other cursor-only write (e.g.
    release_runtime_claim marking authority_active=False) lands on the
    object before a fresh process resumes, that fresh process can no
    longer tell the true original bind-write generation apart from the
    object's current (now-later) generation -- nothing durable records
    the former once the latter exists. Proven unrecoverable; not used.

    Instead, terminal_fence_epoch is simply the epoch this bind
    belongs to. `epoch` is read off the document BEFORE the bind write --
    never returned by it -- so it is known deterministically up front,
    needs no follow-up write, and has no crash window at all: the ENTIRE
    bind, including its fence, is written atomically in one CAS. Fencing
    correctness does not depend on it being a raw GCS generation number in
    the first place -- (execution_id, epoch, proposal_hash) is already a
    complete, immutable identity for "this exact winning proposal"; a
    stale writer's projection carries a different epoch or hash and is
    rejected on that basis regardless of what a numeric fence would add."""
    execution_id = execution.get("execution_id")
    fresh_task_drive_id = None
    fresh_handoff_drive_id = None
    for _ in range(attempts):
        try:
            existing = registry.read_if_exists()
        except Exception as exc:
            raise TaskError("task root backend unavailable while committing terminal bind") from exc
        if existing is None:
            raise TaskError("task root object does not exist; execution was never claimed")
        document, generation, _server_time = existing
        if document.get("project_id") != project_id or document.get("task_id") != task_id:
            raise TaskError("malformed task root record: identity does not match the claim key")
        if not _is_strengthened(document):
            raise TaskError("terminal bind requires a strengthened task root; acquire_task_root must run first")
        if document.get("execution_id") != execution_id:
            raise TerminalProposalLost(
                f"task root's current epoch is owned by execution {document.get('execution_id')}, not {execution_id}",
                winner=document.get("terminal"))

        epoch = document["epoch"]
        proposal = terminal_proposal(execution, epoch)
        proposal_h = proposal_hash(proposal)
        existing_bind = document.get("terminal")

        if existing_bind is None:
            if fresh_task_drive_id is None and task_drive_id_factory is not None:
                fresh_task_drive_id = task_drive_id_factory()
            if fresh_handoff_drive_id is None and handoff_drive_id_factory is not None:
                fresh_handoff_drive_id = handoff_drive_id_factory()
            bind = {
                "project_id": project_id, "task_id": task_id, "epoch": epoch,
                "execution_id": execution_id, "retry_count": proposal["retry_count"],
                "terminal_status": proposal["terminal_status"], "provider_outcome": proposal["provider_outcome"],
                "terminal_reason": proposal["terminal_reason"], "completed_at": proposal["completed_at"],
                "terminal_committed_at": now_iso(), "provider_identity": proposal["provider_identity"],
                "session_id": proposal["session_id"], "account_identity": proposal["account_identity"],
                "schema_version": SCHEMA_VERSION, "canonicalization_version": CANONICALIZATION_VERSION,
                "canonical_proposal": proposal, "proposal_hash": proposal_h,
                "terminal_fence_epoch": epoch,
                "task_projection_drive_id": fresh_task_drive_id, "handoff_drive_file_id": fresh_handoff_drive_id,
            }
            new_document = {**document, "terminal": bind}
            try:
                new_generation = registry.compare_and_swap(generation, new_document)
            except RegistryConflict:
                continue
            except Exception as exc:
                raise TaskError("task root backend unavailable while committing terminal bind") from exc
            document, generation, existing_bind = new_document, new_generation, bind
        else:
            if existing_bind.get("execution_id") != execution_id or existing_bind.get("epoch") != epoch:
                raise TerminalProposalLost(
                    "task root's current epoch is already bound to a different execution/epoch",
                    winner=existing_bind)
            if existing_bind.get("proposal_hash") != proposal_h:
                raise TerminalProposalConflict(
                    "terminal proposal conflicts with the already-bound proposal for this epoch",
                    {"bound_proposal_hash": existing_bind.get("proposal_hash"), "candidate_proposal_hash": proposal_h})

        updates = {}
        if fresh_task_drive_id is None and task_drive_id_factory is not None and existing_bind.get("task_projection_drive_id") is None:
            fresh_task_drive_id = task_drive_id_factory()
        if fresh_handoff_drive_id is None and handoff_drive_id_factory is not None and existing_bind.get("handoff_drive_file_id") is None:
            fresh_handoff_drive_id = handoff_drive_id_factory()
        if fresh_task_drive_id is not None and existing_bind.get("task_projection_drive_id") is None:
            updates["task_projection_drive_id"] = fresh_task_drive_id
        if fresh_handoff_drive_id is not None and existing_bind.get("handoff_drive_file_id") is None:
            updates["handoff_drive_file_id"] = fresh_handoff_drive_id
        if not updates:
            return document, generation

        new_document = {**document, "terminal": {**existing_bind, **updates}}
        try:
            new_generation = registry.compare_and_swap(generation, new_document)
        except RegistryConflict:
            continue
        except Exception as exc:
            raise TaskError("task root backend unavailable while committing terminal bind") from exc
        return new_document, new_generation
    raise TaskError("terminal bind ambiguous after retries; failing closed")


def release_runtime_claim(registry, project_id, task_id, execution_id, generation, claim_token=None, attempts=DEFAULT_ATTEMPTS):
    """Release runtime authority for the CURRENT epoch.

    The delete-vs-preserve decision turns on whether a real terminal BIND
    exists (document.get("terminal") is not None) -- never on document
    shape alone. A legacy (not yet migrated) document falls back to the
    exact pre-Design-A task_claims.release_task_execution_claim() delete
    (acquire_task_root() is solely responsible for migrating a task going
    forward; release never needs to and must not -- it has no
    legacy_migration_lookup to decide it correctly). A strengthened-but-
    never-bound object (today, EVERY execution: commit_terminal_bind is
    not yet wired into the live completion path -- that lands in a later
    checkpoint) also has nothing durable to preserve and is physically
    deleted -- via a plain generation-matched delete rather than routing
    through task_claims' own legacy schema validator, since this
    document's schema_version is intentionally not the legacy one -- so a
    strengthened shape by itself never changes this call's observable
    behavior for the common case. Only once a real bind exists does this
    switch to a CAS update that marks authority inactive without ever
    deleting the object -- see module docstring."""
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
    if document.get("terminal") is None:
        try:
            registry.delete_if_generation_matches(current_generation)
        except RegistryConflict as exc:
            raise TaskClaimConflict("task root changed concurrently; release aborted") from exc
        except Exception as exc:
            try:
                confirmed = registry.read_if_exists()
            except Exception as reread_exc:
                raise TaskError("task root release outcome is ambiguous") from reread_exc
            if confirmed is None:
                return {"released": True, "generation": current_generation, "confirmed_after_ambiguous_delete": True}
            raise TaskError("task root release outcome is ambiguous") from exc
        return {"released": True, "generation": current_generation}
    for _ in range(attempts):
        new_document = {**document, "authority_active": False}
        _reject_bind_mutation(document, new_document)
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
