"""Manual, fail-closed cleanup for an already-terminal execution's task claim,
and operator break-glass recovery for a legacy running execution stuck
because its provider_evidence is missing/unverifiable."""

import argparse
import json
import os
import socket

from collectors.publish_drive import build_service
from manager.codex_launcher import process_identity_state
from manager.execution_lifecycle import terminalize_execution
from manager.gcs_lock_registry import GCSLockRegistry
from manager.task_claims import check_task_execution_claim, release_task_execution_claim, task_claim_registry
from manager.tasks import DriveRecords, TaskError, now_iso, validate


TERMINAL = {"completed", "failed", "interrupted"}
RECOVERABLE_STATUSES = ("running", "interrupted")

_LIVENESS_DETAIL = {
    "stopped": "provider_process_stopped",
    "replaced": "provider_pid_reused_by_different_process",
    "unknown": "provider_liveness_unverifiable",
}


def _refused(reason, claim=None):
    result = {"status": "refused", "released": False, "reason": reason}
    if claim:
        result["execution_id"] = claim["execution_id"]
    return result


def recover_task_claim(store, claim_registry, project_id, task_id):
    """Release only a terminal execution's exact stale claim generation.

    This command deliberately cannot declare a running provider dead. A running
    execution must be terminalized by its original recovery flow after external
    provider-stop evidence is available.
    """
    claim = check_task_execution_claim(claim_registry, project_id, task_id)
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
    if execution.get("access") == "production_write" and cleanup.get("writer_release") != "released":
        return _refused("writer_authority_not_confirmed_released", claim)
    released = release_task_execution_claim(claim_registry, project_id, task_id,
                                            claim["execution_id"], claim["generation"])
    if not released.get("released"):
        return _refused("claim_changed_or_not_owned", claim)
    return {"status": "released", "released": True, "execution_id": claim["execution_id"],
            "generation": claim["generation"],
            "confirmed_after_ambiguous_delete": bool(released.get("confirmed_after_ambiguous_delete"))}


def _classify_provider_liveness(execution):
    """Classify whether the provider process a running execution's
    provider_evidence points at can be proven dead.

    Returns {"state": ..., "detail": ...} where state is one of:
    - "unknown": no provider_evidence at all (the legacy gap this recovery
      path exists for), evidence recorded on a different host than this one,
      or process_identity_state itself could not prove anything. Liveness is
      unproven either way -- never a safe substitute for "dead".
    - "stopped": process_identity_state proves the exact PID no longer runs
      on this (matching) host.
    - "replaced": the PID exists but now belongs to a different OS process
      (creation identity differs) -- the *original* process is proven gone,
      but this is deliberately kept distinct from "stopped" so a caller can
      never conflate ordinary PID reuse with a direct liveness check.
    - "live": the PID exists on this host with the exact recorded creation
      identity -- the original process may still be running.
    """
    evidence = execution.get("provider_evidence") or {}
    if not evidence:
        return {"state": "unknown", "detail": "provider_evidence_missing"}
    if evidence.get("host") != socket.gethostname()[:100]:
        return {"state": "unknown", "detail": "provider_evidence_host_mismatch"}
    state = process_identity_state(evidence.get("pid"), evidence.get("creation_identity"))
    return {"state": state, "detail": _LIVENESS_DETAIL.get(state, "provider_liveness_unverifiable")}


def recover_stale_running_execution(store, service, writer_registry, claim_registry, project_id, task_id,
                                    execution_id, provider, *, actor, reason, break_glass=False, attested_at=None):
    """Operator break-glass recovery for a running execution whose provider
    liveness cannot be automatically proven (typically: legacy provider_evidence
    was never recorded), forcing it to a terminal "interrupted" state so its
    Command can leave "attention" and its GCS task claim can be released.

    Fail-closed guarantees this function enforces (never delegated to the
    caller):
    - `provider_state=unknown` is never treated as stopped; recovering it
      requires the explicit `break_glass=True` flag, which is never defaulted
      or inferred (see `_classify_provider_liveness`).
    - `actor`/`reason` are mandatory, non-empty, keyword-only attestation --
      there is no positional call shape that can invoke this without them.
    - A live PID with a matching creation identity always refuses recovery,
      break_glass or not; this is the one outcome nothing here can override.
    - Attestation evidence (actor, timestamp, reason, prior status, provider
      liveness classification) is persisted onto the execution record itself
      before any terminal transition is attempted, so it survives even if
      terminalization subsequently fails.
    - The GCS task claim is only ever released by delegating to
      `execution_lifecycle.terminalize_execution()` *after* the execution
      itself is confirmed to already hold (or successfully acquires) terminal
      status -- this function never releases a claim directly, and never on
      any refusal or exception path.
    - Idempotent: recovering an already-recovered execution (status already
      "interrupted" via this same mechanism) re-validates the same inputs and
      delegates to `terminalize_execution`'s own idempotent short-circuit,
      rather than re-attempting a claim release that already happened. An
      execution that reached "interrupted" through any other path is refused,
      never silently reinterpreted as this tool's own prior work.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise TaskError("operator attestation requires a non-empty actor")
    if not isinstance(reason, str) or not reason.strip():
        raise TaskError("operator attestation requires a non-empty reason")

    execution = store.get("executions", project_id, execution_id)
    validate("execution", execution)
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(execution.get(key) != value for key, value in identity.items()):
        raise TaskError("recovery identity does not match the execution record")

    prior_status = execution.get("status")
    if prior_status not in RECOVERABLE_STATUSES:
        return {"status": "refused", "reason": f"execution_status_not_recoverable:{prior_status}"}
    if prior_status == "interrupted" and not (execution.get("recovery_attestation") or {}).get("break_glass_recovery"):
        return {"status": "refused", "reason": "execution_already_terminal_not_via_break_glass_recovery"}

    liveness = _classify_provider_liveness(execution)
    if liveness["state"] == "live":
        return {"status": "refused", "reason": "provider_process_is_live", "liveness": liveness}
    if liveness["state"] == "unknown" and not break_glass:
        return {"status": "refused", "reason": "provider_liveness_unknown_break_glass_required", "liveness": liveness}

    if (execution.get("access") == "production_write"
            and (execution.get("cleanup_evidence") or {}).get("writer_release") != "released"
            and writer_registry is None):
        raise TaskError("production_write execution recovery requires a writer lock registry")

    claim = check_task_execution_claim(claim_registry, project_id, task_id)
    if prior_status == "running":
        if claim is None or claim.get("execution_id") != execution_id or claim.get("provider") != provider:
            return {"status": "refused", "reason": "task_claim_missing_or_mismatched"}
        claim_generation = claim["generation"]
    else:
        claim_generation = claim["generation"] if claim and claim.get("execution_id") == execution_id else None

    if prior_status == "running":
        attestation = {
            "actor": actor, "attested_at": attested_at or now_iso(), "reason": reason[:500],
            "break_glass_recovery": True, "break_glass": bool(break_glass),
            "prior_status": prior_status, "provider_liveness": liveness["state"],
            "provider_liveness_detail": liveness["detail"],
        }
        execution = {**execution, "recovery_attestation": attestation}
        validate("execution", execution)
        store.put("executions", project_id, execution_id, execution)
        if store.get("executions", project_id, execution_id) != execution:
            raise TaskError("recovery attestation persistence verification failed")
    else:
        attestation = execution["recovery_attestation"]

    result = terminalize_execution(
        store, service, writer_registry, claim_registry, project_id, task_id, execution_id, provider,
        "interrupted", claim_generation, True,
        summary=f"Operator break-glass recovery ({actor}): {reason}"[:300],
    )
    released = result["cleanup"].get("task_claim_release") in ("released", "not_required")
    return {"status": "recovered" if released else "recovered_claim_not_released",
            "attestation": attestation, **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manually release one verified terminal task claim")
    parser.add_argument("project_id"); parser.add_argument("task_id")
    args = parser.parse_args(argv)
    try:
        service = build_service(); store = DriveRecords(service)
        registry = task_claim_registry(os.environ.get("ADM_LOCK_GCS_BUCKET"), args.project_id, args.task_id)
        result = recover_task_claim(store, registry, args.project_id, args.task_id)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] in {"clean", "released"} else 2
    except (TaskError, OSError, ValueError):
        print(json.dumps({"status": "error", "released": False, "reason": "recovery_failed"}, separators=(",", ":")))
        return 1


def main_break_glass(argv=None):
    parser = argparse.ArgumentParser(
        description="Operator break-glass recovery for a legacy stuck running execution")
    parser.add_argument("project_id"); parser.add_argument("task_id")
    parser.add_argument("execution_id"); parser.add_argument("provider")
    parser.add_argument("--actor", required=True, help="Identity of the operator attesting to this recovery")
    parser.add_argument("--reason", required=True, help="Why this execution is believed safe to recover")
    parser.add_argument("--break-glass", action="store_true",
                        help="Required when provider liveness cannot be automatically proven "
                             "(e.g. legacy execution with no provider_evidence recorded)")
    args = parser.parse_args(argv)
    try:
        service = build_service(); store = DriveRecords(service)
        writer_registry = GCSLockRegistry.from_environment()
        claim_registry = task_claim_registry(os.environ.get("ADM_LOCK_GCS_BUCKET"), args.project_id, args.task_id)
        result = recover_stale_running_execution(
            store, service, writer_registry, claim_registry, args.project_id, args.task_id,
            args.execution_id, args.provider, actor=args.actor, reason=args.reason, break_glass=args.break_glass,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
        return 0 if result["status"] in {"recovered", "recovered_claim_not_released"} else 2
    except (TaskError, OSError, ValueError):
        print(json.dumps({"status": "error", "reason": "recovery_failed"}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
