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

# Permanent materialization failure watchdog: how many consecutive failed
# materialization attempts one view (task/handoff) durably tolerates before
# escalating from "pending" straight to "attention" and releasing runtime
# claim authority (see record_materialization_failure/release_runtime_claim).
# Durable on the Task Root object itself, not process memory, so the count
# survives a watcher restart between ticks -- no new scheduler/service is
# introduced; this only changes what the EXISTING retry-on-next-tick call
# already does with each failure.
DEFAULT_MATERIALIZATION_ATTEMPTS = 3

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
    "expected_task_projection_digest", "expected_handoff_projection_digest",
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


def projection_digest(payload):
    """Deterministic digest of a Drive projection payload (a Task or
    Handoff document, or the authoritative-fields subset of one). Callers
    own what exactly goes into `payload` -- this function only owns making
    the same payload always hash the same way."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def projection_of(bind):
    """The authority tuple one Drive Task/Handoff projection must carry to
    be verifiable later against verify_projection_matches_commit."""
    if not isinstance(bind, dict):
        return None
    return {"execution_id": bind.get("execution_id"), "epoch": bind.get("epoch"),
            "proposal_hash": bind.get("proposal_hash")}


def verify_projection_matches_commit(bind, projection):
    """Fail-closed authority check: a Drive-materialized projection is
    authoritative only when execution_id, epoch, and proposal_hash all
    agree with the current GCS Task Root bind. A stale writer can still
    physically produce a projection after a newer commit exists -- this
    predicate is what lets a reader ignore/reject it and trigger repair
    instead of trusting Drive on its own.
    STALE_WRITE_CAN_PHYSICALLY_EXIST=YES, STALE_WRITE_CAN_BECOME_AUTHORITATIVE=NO."""
    if not isinstance(bind, dict) or not isinstance(projection, dict):
        return False
    return (bind.get("execution_id") == projection.get("execution_id")
            and bind.get("epoch") == projection.get("epoch")
            and bind.get("proposal_hash") == projection.get("proposal_hash"))


def verify_projection_digest(bind, view, actual_payload):
    """Fail-closed content check, ORTHOGONAL to verify_projection_matches_commit:
    even when the authority tuple (execution_id/epoch/proposal_hash) matches
    the current winner, a stale writer overwriting the SAME winner's Drive
    projection with different content must still be rejected. Recomputes
    the digest from the actual payload read off Drive -- never trusts a
    self-reported hash embedded in the Drive document itself. `view` is
    "task" or "handoff". Returns True (nothing to validate against yet) if
    the bind has no expected digest recorded for that view."""
    if view not in ("task", "handoff"):
        raise TaskError(f"unknown projection view: {view}")
    expected = bind.get(f"expected_{view}_projection_digest") if isinstance(bind, dict) else None
    if expected is None:
        return True
    return projection_digest(actual_payload) == expected


def _is_strengthened(document):
    return isinstance(document, dict) and "epoch" in document


# Orthogonal truth domains (Checkpoint C). Terminal authority (bound/unbound)
# already lives in `terminal`. These two are independent of it and of each
# other: a task can legally be terminal=bound + materialization=attention +
# cleanup=released all at once -- exactly the permanent-Drive-failure shape
# that must not hold a runtime claim hostage forever. Neither domain is
# collapsed into a single linear phase.

# Materialization is a recoverable KNOWLEDGE state about one Drive view
# (task projection or handoff), NOT a rank -- "attention" is not a ceiling
# above "verified"; it can transition back to "verified" once whatever
# failed is retried and confirmed. Views are tracked independently because a
# permanently-broken Handoff write must never block a healthy Task
# projection (or vice versa) from reaching "verified".
MATERIALIZATION_STATUSES = ("absent", "pending", "verified", "attention")
_MATERIALIZATION_TRANSITIONS = {
    "absent": {"pending"},
    "pending": {"pending", "verified", "attention"},
    "verified": {"verified", "pending"},   # re-materializing an already-good view (e.g. repair) is legal
    "attention": {"pending", "verified", "attention"},  # recoverable, never a dead end
}

# Cleanup (the RUNTIME claim/writer/provider resource lattice, distinct from
# Execution.cleanup_evidence's own lattice) IS a monotonic rank: once
# resources are released they are never un-released. "released" is sticky.
CLEANUP_STATUSES = ("retained", "release_pending", "released")
_CLEANUP_RANK = {"retained": 0, "release_pending": 1, "released": 2}


def _fresh_materialization():
    return {"task": {"status": "absent"}, "handoff": {"status": "absent"}}


def _fresh_epoch_document(project_id, task_id, execution_id, provider, claimed_at, epoch, claim_token=None):
    document = {
        "schema_version": SCHEMA_VERSION, "project_id": project_id, "task_id": task_id,
        "epoch": epoch, "execution_id": execution_id, "provider": provider, "claimed_at": claimed_at,
        "authority_active": True, "terminal": None,
        "materialization": _fresh_materialization(), "cleanup": {"status": "retained"},
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
        "terminal": None, "materialization": _fresh_materialization(), "cleanup": {"status": "retained"},
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
                "materialization": _fresh_materialization(), "cleanup": {"status": "retained"},
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
                         handoff_drive_id_factory=None, expected_task_projection=None,
                         expected_handoff_projection=None, attempts=DEFAULT_ATTEMPTS):
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
            # R17 legacy recovery gate: this Execution is ALREADY terminal
            # and we hold no live acquire_task_root() caller to migrate it
            # first -- migrate it here, in the same single-CAS-decision
            # style acquire_task_root uses (migrate-then-loop, never
            # migration and bind on one write). A legacy claim for a
            # DIFFERENT execution_id is a real conflict: something else
            # physically holds it and this execution cannot prove it is
            # the unique terminal proposal.
            if document.get("execution_id") != execution_id:
                raise TerminalProposalLost(
                    f"task root's legacy claim is owned by execution {document.get('execution_id')}, not {execution_id}",
                    winner=None)
            migrated = _migrate_legacy_document(document, project_id, task_id, None)
            migrated["authority_active"] = True
            try:
                registry.compare_and_swap(generation, migrated)
            except RegistryConflict:
                continue
            except Exception as exc:
                raise TaskError("task root backend unavailable during legacy migration") from exc
            continue
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
                "expected_task_projection_digest": projection_digest(expected_task_projection) if expected_task_projection is not None else None,
                "expected_handoff_projection_digest": projection_digest(expected_handoff_projection) if expected_handoff_projection is not None else None,
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
        if expected_task_projection is not None and existing_bind.get("expected_task_projection_digest") is None:
            updates["expected_task_projection_digest"] = projection_digest(expected_task_projection)
        if expected_handoff_projection is not None and existing_bind.get("expected_handoff_projection_digest") is None:
            updates["expected_handoff_projection_digest"] = projection_digest(expected_handoff_projection)
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


def _current_epoch_owner_document(registry, project_id, task_id, execution_id):
    try:
        existing = registry.read_if_exists()
    except Exception as exc:
        raise TaskError("task root backend unavailable") from exc
    if existing is None:
        raise TaskError("task root object does not exist")
    document, generation, _server_time = existing
    if document.get("project_id") != project_id or document.get("task_id") != task_id:
        raise TaskError("malformed task root record: identity does not match the claim key")
    if not _is_strengthened(document):
        raise TaskError("facet advance requires a strengthened task root")
    if document.get("execution_id") != execution_id:
        raise TaskError("only the current epoch's owning execution may advance its facets")
    return document, generation


def advance_materialization_view(registry, project_id, task_id, execution_id, view, new_status, note=None, attempts=DEFAULT_ATTEMPTS):
    """CAS-advance ONE materialization view ("task" or "handoff")
    independently of the other and independently of the cleanup facet.
    Validated against _MATERIALIZATION_TRANSITIONS -- a knowledge state,
    not a rank: "attention" can legally advance back to "verified" once
    whatever failed is retried and confirmed, so a permanently-broken
    Handoff can never trap a healthy Task view (or vice versa) below
    "verified". Never touches the terminal bind (guarded by
    _reject_bind_mutation defensively).

    Only ever called on confirmed PROGRESS (a materialization attempt that
    actually succeeded, or a durable "about to attempt" pending marker) --
    never on a failed attempt, which is record_materialization_failure()'s
    job. Every call here therefore resets the durable retry_count the
    watchdog uses: real progress clears whatever failure history preceded
    it, so a view that eventually succeeds after N failed attempts starts
    counting from zero again if it ever regresses in a later epoch."""
    if view not in ("task", "handoff"):
        raise TaskError(f"unknown materialization view: {view}")
    if new_status not in MATERIALIZATION_STATUSES:
        raise TaskError(f"unknown materialization status: {new_status}")
    for _ in range(attempts):
        document, generation = _current_epoch_owner_document(registry, project_id, task_id, execution_id)
        materialization = dict(document.get("materialization") or _fresh_materialization())
        current = (materialization.get(view) or {}).get("status", "absent")
        if new_status not in _MATERIALIZATION_TRANSITIONS.get(current, set()) and new_status != current:
            raise TaskError(f"illegal materialization transition for '{view}': {current} -> {new_status}")
        view_state = {"status": new_status, "retry_count": 0}
        if note is not None:
            view_state["note"] = str(note)[:500]
        materialization[view] = view_state
        new_document = {**document, "materialization": materialization}
        _reject_bind_mutation(document, new_document)
        try:
            new_generation = registry.compare_and_swap(generation, new_document)
        except RegistryConflict:
            continue
        except Exception as exc:
            raise TaskError("task root backend unavailable while advancing materialization") from exc
        return new_document, new_generation
    raise TaskError("materialization advance ambiguous after retries; failing closed")


def record_materialization_failure(registry, project_id, task_id, execution_id, view, note,
                                   threshold=DEFAULT_MATERIALIZATION_ATTEMPTS, attempts=DEFAULT_ATTEMPTS):
    """Permanent materialization failure watchdog: durably record ONE failed
    materialization attempt for a view, incrementing its retry_count on the
    Task Root object itself (survives a watcher restart between ticks --
    no new scheduler/service). While retry_count stays under `threshold`
    the view is left/set "pending" so the existing retry-on-next-tick call
    keeps trying it exactly as before. Once retry_count reaches `threshold`
    the view escalates to "attention" -- the caller (retry_incomplete_
    terminal_persistence) is then expected to release runtime claim
    authority via release_runtime_claim(), since holding a running claim
    hostage to a permanently-broken Drive write serves nothing: the
    terminal winner (`terminal` bind) stays immutable either way, and
    Execution.cleanup_evidence.persistence is never marked complete while
    any view is outside {"verified"} -- see retry_incomplete_terminal_
    persistence's own docstring. release_runtime_claim's cleanup facet is
    independent of materialization (module docstring), so releasing here
    never fabricates completion.

    "attention" is not a dead end (_MATERIALIZATION_TRANSITIONS): a later
    successful retry calls advance_materialization_view(..., "verified"),
    which resets retry_count to 0 and lets a genuinely-recovered view
    (e.g. a transient Drive outage that later clears) reach "verified"
    even after previously escalating -- repair never needs to reacquire a
    running execution claim, since _current_epoch_owner_document() (which
    every facet-advance function goes through) only checks execution_id
    ownership, never authority_active."""
    if view not in ("task", "handoff"):
        raise TaskError(f"unknown materialization view: {view}")
    for _ in range(attempts):
        document, generation = _current_epoch_owner_document(registry, project_id, task_id, execution_id)
        materialization = dict(document.get("materialization") or _fresh_materialization())
        current_state = materialization.get(view) or {"status": "absent"}
        current = current_state.get("status", "absent")
        if current == "verified":
            raise TaskError(f"cannot record a materialization failure against an already-verified '{view}' view")
        retry_count = current_state.get("retry_count", 0) + 1
        new_status = "attention" if retry_count >= threshold else "pending"
        # A recorded failure means an attempt was made, so for transition
        # purposes "absent" (never even attempted) is treated as "pending"
        # here -- otherwise a low `threshold` could try to jump straight
        # from "absent" to "attention", which _MATERIALIZATION_TRANSITIONS
        # correctly forbids (only "pending" legally reaches "attention").
        transition_from = "pending" if current == "absent" else current
        if new_status not in _MATERIALIZATION_TRANSITIONS.get(transition_from, set()) and new_status != transition_from:
            raise TaskError(f"illegal materialization transition for '{view}': {current} -> {new_status}")
        materialization[view] = {"status": new_status, "note": str(note)[:500], "retry_count": retry_count}
        new_document = {**document, "materialization": materialization}
        _reject_bind_mutation(document, new_document)
        try:
            new_generation = registry.compare_and_swap(generation, new_document)
        except RegistryConflict:
            continue
        except Exception as exc:
            raise TaskError("task root backend unavailable while recording materialization failure") from exc
        return new_document, new_generation
    raise TaskError("materialization failure record ambiguous after retries; failing closed")


def advance_cleanup_facet(registry, project_id, task_id, execution_id, new_status, attempts=DEFAULT_ATTEMPTS):
    """CAS-advance the RUNTIME cleanup facet (retained -> release_pending ->
    released), monotonically -- "released" is sticky and this never
    regresses it, independent of what materialization currently shows.
    This is the facet acquire_task_root() checks before opening a new
    epoch; it is deliberately independent of `terminal`/`materialization`
    so a permanently-attention materialization can still let cleanup
    reach "released" and free up the task for a legitimate retry -- see
    module docstring's permanent-Drive-failure shape."""
    if new_status not in CLEANUP_STATUSES:
        raise TaskError(f"unknown cleanup status: {new_status}")
    for _ in range(attempts):
        document, generation = _current_epoch_owner_document(registry, project_id, task_id, execution_id)
        current = (document.get("cleanup") or {}).get("status", "retained")
        if _CLEANUP_RANK[new_status] <= _CLEANUP_RANK.get(current, 0):
            return document, generation  # monotonic no-op: never regress, no-op if already there or ahead
        new_document = {**document, "cleanup": {"status": new_status}}
        _reject_bind_mutation(document, new_document)
        try:
            new_generation = registry.compare_and_swap(generation, new_document)
        except RegistryConflict:
            continue
        except Exception as exc:
            raise TaskError("task root backend unavailable while advancing cleanup facet") from exc
        return new_document, new_generation
    raise TaskError("cleanup facet advance ambiguous after retries; failing closed")


def release_runtime_claim(registry, project_id, task_id, execution_id, generation, claim_token=None, attempts=DEFAULT_ATTEMPTS):
    """Release runtime authority for the CURRENT epoch.

    The delete-vs-preserve decision turns on whether a real terminal BIND
    exists (document.get("terminal") is not None) -- never on document
    shape alone. A legacy (not yet migrated) document falls back to the
    exact pre-Design-A task_claims.release_task_execution_claim() delete
    (acquire_task_root() is solely responsible for migrating a task going
    forward; release never needs to and must not -- it has no
    legacy_migration_lookup to decide it correctly). A strengthened-but-
    never-bound object (a prelaunch rollback in enter_running_gate, before
    any terminal proposal exists) also has nothing durable to preserve and
    is physically deleted -- via a plain generation-matched delete rather
    than routing through task_claims' own legacy schema validator, since
    this document's schema_version is intentionally not the legacy one --
    so a strengthened shape by itself never changes this call's observable
    behavior for the common pre-terminal case. Only once a real bind
    exists does this switch to a CAS update that marks authority inactive
    without ever deleting the object -- see module docstring.

    `generation` is an exact-match precondition ONLY for the no-bind
    (delete) branch, where nothing else should legitimately have touched
    the object since the caller's own read (matching the original
    task_claims.py invariant exactly). Once a bind exists, this object's
    generation legitimately keeps advancing from commit_terminal_bind()/
    advance_materialization_view() CAS writes made by the SAME owning
    execution without ownership ever changing hands (see P0-1's normal-path
    bind-before-materialization wiring) -- exact-match would then reject a
    perfectly legitimate release for no real reason. Ownership
    (document.get("execution_id") == execution_id, checked above
    regardless of branch) is what actually matters there; the CAS write
    itself still always uses the freshly re-read generation as its own
    precondition, so it remains fully race-safe either way.

    P0-2 fix: for a terminal-bound Root, this ALSO advances the Root's own
    `cleanup.status` facet to "released" in the SAME CAS write as
    authority_active=False -- never leaving that decision to a separate
    caller. Before this fix, only Execution.cleanup_evidence.task_claim_release
    ever became "released" (via merge_cleanup_evidence elsewhere);
    Root.cleanup.status silently stayed "retained" forever, which
    permanently blocked acquire_task_root()'s next-epoch gate (it requires
    cleanup.status == "released" before a bound epoch may be superseded).
    The two cleanup truths must converge on the same release, not one
    lagging the other indefinitely."""
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
    if document.get("terminal") is None:
        # No bind exists yet, so nothing else should have legitimately
        # advanced this object's generation since the caller's own read --
        # exact match is still a valid, desirable staleness guard here,
        # identical to the pre-Design-A task_claims.py semantics.
        if current_generation != generation:
            raise TaskClaimConflict("task root generation changed; refusing to release under a stale generation")
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
        current_cleanup_status = (document.get("cleanup") or {}).get("status", "retained")
        new_cleanup_status = "released" if _CLEANUP_RANK.get(current_cleanup_status, 0) < _CLEANUP_RANK["released"] else current_cleanup_status
        new_document = {**document, "authority_active": False, "cleanup": {"status": new_cleanup_status}}
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
