"""Manual, fail-closed cleanup for an already-terminal execution's task claim."""

import argparse
import json
import os

from collectors.publish_drive import build_service
from manager.task_claims import check_task_execution_claim, release_task_execution_claim, task_claim_registry
from manager.tasks import DriveRecords, TaskError, validate


TERMINAL = {"completed", "failed", "interrupted"}


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
    if execution.get("access") == "production_write" and cleanup.get("writer_release") != "released":
        return _refused("writer_authority_not_confirmed_released", claim)
    released = release_task_execution_claim(claim_registry, project_id, task_id,
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
        result = recover_task_claim(store, registry, args.project_id, args.task_id)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] in {"clean", "released"} else 2
    except (TaskError, OSError, ValueError):
        print(json.dumps({"status": "error", "released": False, "reason": "recovery_failed"}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
