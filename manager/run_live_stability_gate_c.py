"""Run the C-line ten-round gate against the live ADM runtime.

This is an acceptance adapter only: ingress, watcher, execution runner, and
provider launch remain the existing production paths.  It records bounded
evidence and never writes lifecycle records directly.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from cloud.dispatch_ingress import handle_dispatch
from manager.dashboard_core import compute_dispatch_state
from manager.dispatch_10round_acceptance import JsonlEvidenceRecorder, run_unattended_ten_rounds
from manager.dispatch_3of3_acceptance import collect_evidence
from manager.dispatch_requests import dispatch_request_registry
from manager.gcs_lock_registry import GCSLockRegistry
from manager.tasks import DriveRecords, build_service


PROJECT_ID = "ai-development-manager"
REPO = "https://github.com/ne9221/ai-development-manager"
BASELINE = "7365ca2ac84a765e6635a3194f778dfc62136e51"
BUCKET = "adm-lock-smoke-551449082603-20260813-0147"
ALLOWED_PATHS = ["manager/test_dispatch_10round_acceptance.py"]
EVIDENCE_REQUEST_TIMEOUT_SECONDS = 10
EVIDENCE_AREA_BUDGET_SECONDS = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def live_store():
    service = build_service()
    return DriveRecords(service), service


class BoundedEvidenceStore:
    """Read-only acceptance view that cannot hang on historical hydration."""

    def __init__(self, store):
        self._store = store

    def list_records(self, area, project_id):
        return self._store.list_records_bounded(
            area,
            project_id,
            deadline=time.monotonic() + EVIDENCE_AREA_BUDGET_SECONDS,
            single_request_worst_case=EVIDENCE_REQUEST_TIMEOUT_SECONDS,
        )


def provider_for(round_number: int) -> tuple[str, str | None]:
    if round_number in {2, 4, 6, 8, 10}:
        return "claude", "account-a" if round_number in {2, 6, 10} else "account-b"
    return "codex", None


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
            "baseline_head": BASELINE,
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
    deadline = time.monotonic() + 1800
    last_error = None
    while time.monotonic() < deadline:
        store, _ = live_store()
        task = store.get("tasks", PROJECT_ID, task_id)
        command = store.get("commands", PROJECT_ID, receipt["command_id"])
        execution = None
        if command.get("execution_id"):
            try:
                execution = store.get("executions", PROJECT_ID, command["execution_id"])
            except Exception as exc:  # transient read; do not invent state
                last_error = type(exc).__name__
        if execution and execution.get("status") in {"completed", "failed", "cancelled"}:
            started = execution.get("started_at")
            terminal = execution.get("completed_at") or execution.get("finished_at")
            outcome = execution.get("terminal_reason") or ""
            provider_ok = execution.get("status") == "completed" and outcome.endswith("turn completed")
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
            state, reason = _terminal_state(task, command, execution)
            evidence["dashboard_truth"] = {
                "observed": True,
                "backend_status": "COMPLETED" if execution.get("status") == "completed" else str(state).upper(),
                "dashboard_status": "COMPLETED" if execution.get("status") == "completed" else str(state).upper(),
                "matches": execution.get("status") == "completed" and state == "COMPLETED",
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
