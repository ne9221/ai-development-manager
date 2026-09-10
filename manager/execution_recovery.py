"""Manual, fail-closed cleanup for an already-terminal execution's task claim."""

import argparse
import json
import os

from collectors.publish_drive import build_service
from manager.gcs_lock_registry import GCSLockRegistry
from manager.task_claims import task_claim_registry
from manager.task_root import read_task_root_or_legacy_claim, release_runtime_claim
from manager.tasks import DriveRecords, TaskError, validate
from manager.worktree_locks import owner_fields, read_registry


TERMINAL = {"completed", "failed", "interrupted"}


def _refused(reason, claim=None):
    result = {"status": "refused", "released": False, "reason": reason}
    if claim:
        result["execution_id"] = claim["execution_id"]
    return result


def _has_verifiable_lease_identity(execution):
    """Does this Execution name a specific lock generation the registry can be
    asked about? Records that do (every one written since worktree leases
    grew lock_id/generation) must be verified there and nowhere else."""
    lease = execution.get("lease_evidence") or {}
    return bool(lease.get("lock_id")) and isinstance(lease.get("generation"), int)


def writer_lease_release_truth(writer_registry, execution):
    """Is this execution's writer lease released, according to the LEASE
    REGISTRY itself?  True / False / None ("cannot tell from here").

    The registry -- not Execution.cleanup_evidence -- is the writer
    authority. cleanup_evidence.writer_release is a Drive-side *copy* of
    that fact, written by whichever code path happened to run last, and the
    two provably diverged in production (2026-09-04: the lease generation
    was genuinely CAS-released while the Execution copy still said
    "retained", and every later recovery pass refused the task claim on the
    strength of the copy).

    Two shapes count as released without the lock literally saying so, both
    for the same structural reason the registry keeps exactly one lock slot
    per repository: a slot now owned by a DIFFERENT execution, or carrying a
    NEWER generation than ours, is positive proof our own generation no
    longer holds anything there. A same-execution owner mismatch on any
    other field is record corruption, not proof, and returns None so the
    caller keeps failing closed."""
    lease = execution.get("lease_evidence") or {}
    lock_id, generation = lease.get("lock_id"), lease.get("generation")
    if writer_registry is None or not lock_id or not isinstance(generation, int):
        return None
    try:
        document, _etag, _server_time = read_registry(writer_registry)
    except Exception:
        return None
    lock = (document.get("locks") or {}).get(lock_id)
    if lock is None:
        return None
    if lock.get("execution_id") != execution.get("execution_id"):
        return True
    owner = owner_fields(execution.get("project_id"), execution.get("task_id"),
                         execution.get("execution_id"), execution.get("provider"),
                         lock.get("session_id"))
    if any(lock.get(key) != value for key, value in owner.items()):
        return None
    if lock.get("generation") != generation:
        return lock.get("generation") > generation if isinstance(lock.get("generation"), int) else None
    return lock.get("status") == "released"


def recover_task_claim(store, claim_registry, project_id, task_id, writer_registry=None):
    """Release only a terminal execution's exact stale claim generation.

    This command deliberately cannot declare a running provider dead. A running
    execution must be terminalized by its original recovery flow after external
    provider-stop evidence is available.
    """
    claim = read_task_root_or_legacy_claim(claim_registry, project_id, task_id)
    if claim is None:
        return {"status": "clean", "released": False, "reason": "no_active_claim"}
    try:
        execution = store.get("executions", project_id, claim["execution_id"])
        task = store.get("tasks", project_id, task_id)
        validate("execution", execution); validate("task", task)
    except (KeyError, TaskError) as exc:
        raise TaskError("recovery cannot confirm matching Drive task and execution") from exc
    identity = {"project_id": project_id, "task_id": task_id,
                "execution_id": claim["execution_id"], "provider": claim["provider"]}
    if any(execution.get(key) != value for key, value in identity.items()):
        return _refused("drive_gcs_identity_mismatch", claim)
    if (task.get("source_context") or {}).get("active_execution_id") != claim["execution_id"]:
        return _refused("drive_task_does_not_identify_claimed_execution", claim)
    if execution.get("status") == "running":
        return _refused("running_execution_requires_provider_stop_and_terminal_recovery", claim)
    if execution.get("status") not in TERMINAL:
        return _refused("execution_is_not_terminal", claim)
    expected_task_status = "completed" if execution["status"] == "completed" else "blocked"
    if task.get("status") != expected_task_status:
        return _refused("terminal_drive_state_is_incomplete", claim)
    cleanup = execution.get("cleanup_evidence") or {}
    if (cleanup.get("provider_outcome") != execution["status"]
            or cleanup.get("persistence") != "complete"
            or cleanup.get("persisted") != ["execution", "handoff", "task"]):
        return _refused("authoritative_terminal_cleanup_not_confirmed", claim)
    if execution.get("access") == "production_write":
        # The lock registry is the writer authority; cleanup_evidence is a
        # Drive-side copy of it. Whenever this execution carries verifiable
        # lease identity, ONLY the registry may answer -- an unreadable
        # registry refuses (fail closed), it never falls through to the copy,
        # because releasing a task claim on the strength of a stale copy is
        # exactly how a still-active production writer gets a second owner.
        # The copy remains the answer only for legacy records that have no
        # lock_id/generation to verify against at all.
        released = writer_lease_release_truth(writer_registry, execution)
        if released is None and _has_verifiable_lease_identity(execution):
            return _refused("writer_authority_not_confirmed_released", claim)
        if released is None:
            released = cleanup.get("writer_release") == "released"
        if not released:
            return _refused("writer_authority_not_confirmed_released", claim)
    released = release_runtime_claim(claim_registry, project_id, task_id,
                                     claim["execution_id"], claim["generation"])
    if not released.get("released"):
        return _refused("claim_changed_or_not_owned", claim)
    return {"status": "released", "released": True, "execution_id": claim["execution_id"],
            "generation": claim["generation"],
            "confirmed_after_ambiguous_delete": bool(released.get("confirmed_after_ambiguous_delete"))}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manually release one verified terminal task claim")
    parser.add_argument("project_id"); parser.add_argument("task_id")
    args = parser.parse_args(argv)
    try:
        service = build_service(); store = DriveRecords(service)
        registry = task_claim_registry(os.environ.get("ADM_LOCK_GCS_BUCKET"), args.project_id, args.task_id)
        try:
            writer_registry = GCSLockRegistry.from_environment()
        except Exception:
            writer_registry = None
        result = recover_task_claim(store, registry, args.project_id, args.task_id, writer_registry=writer_registry)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] in {"clean", "released"} else 2
    except (TaskError, OSError, ValueError):
        print(json.dumps({"status": "error", "released": False, "reason": "recovery_failed"}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
