"""Run the C-line ten-round gate against the live ADM runtime.

This is an acceptance adapter only: ingress, watcher, execution runner, and
provider launch remain the existing production paths.  It records bounded
evidence and never writes lifecycle records directly.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from cloud.dispatch_ingress import handle_dispatch
from manager.dashboard_core import compute_dispatch_state
from manager.dispatch_10round_acceptance import JsonlEvidenceRecorder, run_unattended_ten_rounds
from manager.dispatch_3of3_acceptance import collect_evidence
from manager.dispatch_requests import dispatch_request_registry
from manager.gcs_lock_registry import GCSLockRegistry
from manager.provenance import ProvenanceError, _activated_evidence_path
from manager.tasks import DriveRecords, MIME_FOLDER, MIME_JSON, ROOT_FOLDER_ID, ROOT_FOLDERS, TaskError, build_service


PROJECT_ID = "ai-development-manager"
REPO = "https://github.com/ne9221/ai-development-manager"
BUCKET = "adm-lock-smoke-551449082603-20260813-0147"
ALLOWED_PATHS = ["manager/test_dispatch_10round_acceptance.py"]
EVIDENCE_REQUEST_TIMEOUT_SECONDS = 10
EVIDENCE_AREA_BUDGET_SECONDS = 30
ROUND_TIMEOUT_SECONDS = 1800
WAITING_QUOTA_TIMEOUT_SECONDS = 8 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def live_store():
    service = build_service()
    return DriveRecords(service), service


def current_baseline() -> str:
    """The repo-write worktree each round dispatches against must be
    materialized at whatever SHA production actually has activated right
    now -- never a value frozen at some earlier point. A hardcoded
    constant here is wrong on principle, not just inconvenient: the very
    commit that would embed a fixed SHA is itself merged onto production
    main, which immediately advances HEAD past that SHA, so the worktree
    materializer would request a baseline the running production
    checkout no longer matches and require_runtime_guard() would reject
    the round with a PROVENANCE_MISMATCH RuntimeGuardError before a
    provider is ever launched (live-reproduced during this harness's own
    recovery/activation, 2026-08-30). Reads the same activated_sha.json
    manager.provenance.activate() writes and manager.production_guard's
    require_runtime_guard() itself trusts, so this can never drift from
    what the rest of the system considers "currently activated".
    """
    manager_home = Path(os.environ.get("AI_MANAGER_HOME") or Path(__file__).resolve().parents[1] / ".ai-development-manager")
    path = _activated_evidence_path(manager_home)
    try:
        activated_sha = json.loads(path.read_text(encoding="utf-8")).get("activated_sha")
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"PROVENANCE_MISMATCH: cannot read activated baseline at {path}") from exc
    if not isinstance(activated_sha, str) or not activated_sha:
        raise ProvenanceError(f"PROVENANCE_MISMATCH: activated evidence at {path} has no activated_sha")
    return activated_sha


class BoundedEvidenceStore:
    """Read-only acceptance view that cannot hang on historical hydration."""

    def __init__(self, store):
        self._store = store

    def _direct_drive_list_records(self, area, project_id, deadline):
        """List real Drive records without DriveRecords' unbounded folder hop.

        DriveRecords.list_records_bounded() bounds record hydration, but its
        initial project_folder() lookup still uses the legacy unbounded
        children() path.  The C gate must remain bounded even after a
        terminal provider event, so use the same DriveRecords primitives with
        the deadline forwarded through every listing page.  This adapter is
        acceptance-only; production lifecycle code remains untouched.
        """
        root_matches = [item for item in self._store.children(
            ROOT_FOLDER_ID, ROOT_FOLDERS[area], deadline=deadline
        ) if item.get("mimeType") == MIME_FOLDER]
        if len(root_matches) > 1:
            raise TaskError(f"duplicate Drive folder: {ROOT_FOLDERS[area]}")
        if not root_matches:
            if time.monotonic() >= deadline:
                raise TaskError("bounded evidence read deadline expired while locating area folder")
            raise TaskError(f"Drive folder not found: {ROOT_FOLDERS[area]}")
        project_matches = [item for item in self._store.children(
            root_matches[0]["id"], project_id, deadline=deadline
        ) if item.get("mimeType") == MIME_FOLDER]
        if len(project_matches) > 1:
            raise TaskError(f"duplicate Drive folder: {project_id}")
        if not project_matches:
            if time.monotonic() >= deadline:
                raise TaskError("bounded evidence read deadline expired while locating project folder")
            raise TaskError(f"Drive folder not found: {project_id}")

        records = []
        for item in self._store.children(project_matches[0]["id"], deadline=deadline):
            if not item.get("name", "").endswith(".json"):
                continue
            if time.monotonic() + EVIDENCE_REQUEST_TIMEOUT_SECONDS >= deadline:
                break
            try:
                raw = self._store.files.get_media(fileId=item["id"]).execute()
                records.append(json.loads(raw.decode("utf-8")))
            except Exception:
                continue
        return records

    def list_records(self, area, project_id):
        deadline = time.monotonic() + EVIDENCE_AREA_BUDGET_SECONDS
        if hasattr(self._store, "files") and hasattr(self._store, "children"):
            return self._direct_drive_list_records(area, project_id, deadline)
        return self._store.list_records_bounded(
            area,
            project_id,
            deadline=deadline,
            single_request_worst_case=EVIDENCE_REQUEST_TIMEOUT_SECONDS,
        )


def provider_for(round_number: int) -> tuple[str, str | None]:
    if round_number in {2, 4, 6, 8, 10}:
        # Leave account routing to the production Command Watcher.  The live
        # harness does not own the machine's Claude registry, and hardcoding
        # fixture ids here makes direct ingress fail with unknown_account
        # before the natural watcher path can select a real account.
        return "claude", None
    return "codex", None


def provider_output_matches(execution):
    """Accept either a normal completed turn or an independently validated
    no-change success produced by the repo-write enforcement path."""
    if execution.get("status") != "completed":
        return False
    if (execution.get("terminal_reason") or "").endswith("turn completed"):
        return True
    evidence = execution.get("repo_write_evidence") or {}
    return evidence.get("push_status") == "not_applicable" and evidence.get("tests_status") == "passed"


def dispatch_round(round_number: int, request_id: str):
    provider, account_id = provider_for(round_number)
    payload = {
        "request_id": request_id,
        "project_id": PROJECT_ID,
        "title": f"C stability gate round {round_number:02d}",
        "goal": (
            "在隔离 worktree 中完成一轮最小真实 provider 验证；只检查既有测试入口与运行契约，"
            "不得修改 production checkout、credential、token、governance 或 lifecycle core。"
        ),
        "priority": "normal",
        "constraints": {"read_only": False},
        "provider": provider,
        "account_id": account_id,
        "repo_write": {
            "allowed_paths": ALLOWED_PATHS,
            "baseline_head": current_baseline(),
            "repo": REPO,
            "validation_command": "git diff --check",
            "allow_no_change_success": True,
        },
    }
    store, service = live_store()
    return handle_dispatch(
        store,
        service,
        lambda project_id, rid: dispatch_request_registry(BUCKET, project_id, rid),
        payload,
    )


def _terminal_state(task, command, execution):
    state = compute_dispatch_state(
        task,
        command,
        execution,
        datetime.now(timezone.utc),
        has_dispatch_request=True,
    )
    return state["state"], state["reason"]


def collect_round(round_number: int, request_id: str, receipt):
    task_id = receipt["task_id"]
    waiting_quota = receipt.get("status") == "waiting_quota" and not receipt.get("command_id")
    command_id = receipt.get("command_id") or task_id
    deadline = time.monotonic() + (WAITING_QUOTA_TIMEOUT_SECONDS if waiting_quota else ROUND_TIMEOUT_SECONDS)
    last_error = None
    while time.monotonic() < deadline:
        store, _ = live_store()
        task = store.get("tasks", PROJECT_ID, task_id)
        try:
            command = store.get("commands", PROJECT_ID, command_id)
        except Exception as exc:
            last_error = type(exc).__name__
            # The watcher owns quota re-evaluation and Command creation. Keep
            # observing this same admitted Task until natural promotion.
            if waiting_quota and task.get("recommended_provider") is None and task.get("quota_evidence") is not None:
                time.sleep(15)
                continue
            raise
        execution = None
        if command.get("execution_id"):
            try:
                execution = store.get("executions", PROJECT_ID, command["execution_id"])
            except Exception as exc:  # transient read; do not invent state
                last_error = type(exc).__name__
        # A prelaunch failure can truthfully terminalize Command/Task while
        # rolling a reserved Execution back to cancelled (or without ever
        # reserving one).  The acceptance harness must record that terminal
        # lifecycle outcome instead of waiting 30 minutes for an Execution
        # that governance correctly never allowed to become running.
        if execution is None and command.get("status") in {"completed", "failed", "cancelled"}:
            evidence_service = build_service(timeout=EVIDENCE_REQUEST_TIMEOUT_SECONDS)
            evidence_store = BoundedEvidenceStore(DriveRecords(evidence_service))
            evidence_registry = dispatch_request_registry(BUCKET, PROJECT_ID, request_id)
            evidence = collect_evidence(
                evidence_store,
                PROJECT_ID,
                request_id,
                dispatch_request_registry=evidence_registry,
                acceptance_run_started_at=RUN_STARTED_AT,
            )
            evidence["provider_output"] = {
                "observed": False,
                "matched_expected": False,
                "observed_at": command.get("completed_at"),
                "verification_method": "provider_result_summary",
            }
            evidence["execution"] = {
                "execution_id": command.get("execution_id"),
                "status": None,
                "reserved_at": None,
                "running_at": None,
                "terminal_at": None,
                "provider_evidence": {"present": False, "pid": None, "host": None},
            }
            evidence["session"] = {
                "session_id": None,
                "provider_session_id": None,
                "provider": command.get("provider"),
                "task_id": task_id,
                "execution_id": command.get("execution_id"),
            }
            state, reason = _terminal_state(task, command, None)
            evidence["terminal"] = {
                "state": state,
                "command_status": command.get("status"),
                "execution_status": None,
                "task_claim_release": None,
                "writer_release": None,
            }
            evidence["dashboard_truth"] = {
                "observed": True,
                "backend_status": str(state).upper(),
                "dashboard_status": str(state).upper(),
                "matches": False,
                "observed_at": utc_now(),
                "state_reason": reason,
            }
            return evidence
        if execution and execution.get("status") in {"completed", "failed", "cancelled"}:
            started = execution.get("started_at")
            terminal = execution.get("completed_at") or execution.get("finished_at")
            outcome = execution.get("terminal_reason") or ""
            provider_ok = provider_output_matches(execution)
            evidence_service = build_service(timeout=EVIDENCE_REQUEST_TIMEOUT_SECONDS)
            evidence_store = BoundedEvidenceStore(DriveRecords(evidence_service))
            evidence_registry = dispatch_request_registry(BUCKET, PROJECT_ID, request_id)
            evidence = collect_evidence(
                evidence_store,
                PROJECT_ID,
                request_id,
                dispatch_request_registry=evidence_registry,
                acceptance_run_started_at=RUN_STARTED_AT,
            )
            evidence["provider_output"] = {
                "observed": bool(execution.get("provider_evidence")) and bool(terminal),
                "matched_expected": provider_ok,
                "observed_at": terminal,
                "verification_method": "provider_result_summary",
            }
            # collect_evidence() is intentionally a bounded independent read,
            # but the Task/Command snapshot above may predate their terminal
            # updates. Refresh the canonical records before deriving the
            # terminal state; otherwise a real completed lifecycle is recorded
            # as UNKNOWN/false by the acceptance adapter.
            try:
                task = store.get("tasks", PROJECT_ID, task_id)
                command = store.get("commands", PROJECT_ID, command_id)
                if command.get("execution_id"):
                    execution = store.get("executions", PROJECT_ID, command["execution_id"])
            except Exception as exc:  # transient read; retain the observed terminal execution
                last_error = type(exc).__name__
            state, reason = _terminal_state(task, command, execution)
            cleanup = execution.get("cleanup_evidence") or {}
            evidence["terminal"] = {
                "state": state,
                "command_status": command.get("status"),
                "execution_status": execution.get("status"),
                "cleanup_evidence": cleanup,
                "task_claim_release": cleanup.get("task_claim_release"),
                "writer_release": cleanup.get("writer_release"),
            }
            evidence["dashboard_truth"] = {
                "observed": True,
                "backend_status": "COMPLETED" if execution.get("status") == "completed" else str(state).upper(),
                "dashboard_status": "COMPLETED" if execution.get("status") == "completed" else str(state).upper(),
                "matches": (
                    task.get("status") == "completed"
                    and command.get("status") == "completed"
                    and execution.get("status") == "completed"
                    and state == "COMPLETED"
                ),
                "observed_at": utc_now(),
                "state_reason": reason,
            }
            evidence["execution"] = execution
            evidence["session"] = {
                "session_id": execution.get("session_id") or execution.get("provider_session_id"),
                "provider_session_id": execution.get("provider_session_id"),
                "provider": execution.get("provider"),
                "task_id": execution.get("task_id"),
                "execution_id": execution.get("execution_id"),
            }
            return evidence
        time.sleep(15)
    raise TimeoutError(f"round {round_number} did not reach terminal; last_error={last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    global RUN_STARTED_AT
    RUN_STARTED_AT = utc_now()
    run_id = f"c-live-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    request_ids = [f"{run_id}-r{n:02d}" for n in range(1, 11)]
    report = run_unattended_ten_rounds(
        project_id=PROJECT_ID,
        dispatch_round=dispatch_round,
        collect_round=collect_round,
        tick_seconds=60,
        request_ids=request_ids,
        run_id=run_id,
        recorder=JsonlEvidenceRecorder(Path(args.output)),
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
