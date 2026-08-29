"""C-line unattended ten-round Direct Dispatch acceptance harness.

This module is acceptance-only.  It never imports or calls the Command
Watcher, dispatcher, execution runner, provider launcher, or a real ingress.
Those actions are supplied by caller-owned callbacks for the future live run.

Each run evaluates exactly ten declared rounds in order.  A failed round is
recorded and the harness continues, but it can never be omitted, retried, or
replaced by a later pass.  The JSONL recorder emits bounded evidence only:
identifiers, timestamps, statuses, PID/host provenance, output-match metadata,
terminal cleanup, and Dashboard truth.  Raw prompts, transcripts, provider
output, stderr, and credentials are deliberately never persisted here.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from manager.dispatch_3of3_acceptance import (
    STATUS_FAIL,
    STATUS_NOT_APPLICABLE,
    STATUS_PASS,
    STATUS_UNKNOWN,
    CheckResult,
    FreshnessViolation,
    _parse_iso,
    detect_cross_task_borrowing,
    evaluate_dispatch,
)

ROUND_COUNT = 10
HARNESS_VERSION = "c-stability-gate-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_METHODS = {"provider_result_summary", "exit_code_and_result_summary", "digest_only"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pass(status: str) -> bool:
    return status in (STATUS_PASS, STATUS_NOT_APPLICABLE)


def _timestamp(evidence: Dict[str, Any], name: str) -> Optional[str]:
    return (evidence.get("timestamps") or {}).get(name)


def claim_latency_seconds(evidence: Dict[str, Any]) -> Optional[float]:
    """Return claimed_at - ingress_first_observed_at, or None if unavailable."""
    ingress = _parse_iso(_timestamp(evidence, "ingress_first_observed_at"))
    claimed = _parse_iso(_timestamp(evidence, "claimed_at"))
    if ingress is None or claimed is None:
        return None
    return claimed - ingress


def _claim_check(evidence: Dict[str, Any], max_claim_latency_seconds: float) -> tuple[CheckResult, Optional[float]]:
    latency = claim_latency_seconds(evidence)
    if latency is None:
        return CheckResult("CLAIM_LATENCY", STATUS_UNKNOWN, "ingress_first_observed_at or claimed_at not observed"), None
    if latency < 0:
        return CheckResult("CLAIM_LATENCY", STATUS_FAIL, "claimed_at precedes ingress_first_observed_at"), latency
    if latency > max_claim_latency_seconds:
        return CheckResult(
            "CLAIM_LATENCY", STATUS_FAIL,
            f"claim latency {latency:.3f}s exceeds {max_claim_latency_seconds:.3f}s",
        ), latency
    return CheckResult("CLAIM_LATENCY", STATUS_PASS, f"claim latency {latency:.3f}s"), latency


def _execution_check(evidence: Dict[str, Any]) -> CheckResult:
    ids = evidence.get("ids") or {}
    execution = evidence.get("execution") or {}
    execution_id = execution.get("execution_id") or ids.get("execution_id")
    status = execution.get("status") or evidence.get("execution_status")
    missing = [name for name, value in (
        ("execution_id", execution_id),
        ("reserved_at", _timestamp(evidence, "reserved_at")),
        ("running_at", _timestamp(evidence, "running_at")),
        ("terminal_at", _timestamp(evidence, "terminal_at")),
    ) if not value]
    if missing:
        return CheckResult("EXECUTION_EVIDENCE", STATUS_FAIL, f"missing {', '.join(missing)}")
    if status != "completed":
        return CheckResult("EXECUTION_EVIDENCE", STATUS_FAIL, f"execution status is {status!r}, expected 'completed'")
    if execution.get("task_id") and execution.get("task_id") != ids.get("task_id"):
        return CheckResult("EXECUTION_EVIDENCE", STATUS_FAIL, "Execution task_id does not match Task")
    return CheckResult("EXECUTION_EVIDENCE", STATUS_PASS, f"execution_id={execution_id}")


def _session_check(evidence: Dict[str, Any]) -> CheckResult:
    ids = evidence.get("ids") or {}
    execution = evidence.get("execution") or {}
    session = evidence.get("session") or {}
    session_id = session.get("session_id") or ids.get("session_id")
    execution_session_id = execution.get("session_id")
    if not session_id:
        return CheckResult("SESSION_EVIDENCE", STATUS_FAIL, "matching Session was not observed")
    if execution_session_id and execution_session_id != session_id:
        return CheckResult("SESSION_EVIDENCE", STATUS_FAIL, "Execution.session_id does not match Session")
    if session.get("task_id") and session.get("task_id") != ids.get("task_id"):
        return CheckResult("SESSION_EVIDENCE", STATUS_FAIL, "Session task_id does not match Task")
    if session.get("execution_id") and session.get("execution_id") != ids.get("execution_id"):
        return CheckResult("SESSION_EVIDENCE", STATUS_FAIL, "Session execution_id does not match Execution")
    if session.get("provider") and session.get("provider") != evidence.get("provider"):
        return CheckResult("SESSION_EVIDENCE", STATUS_FAIL, "Session provider does not match selected provider")
    return CheckResult("SESSION_EVIDENCE", STATUS_PASS, f"session_id={session_id}")


def _provider_output_check(evidence: Dict[str, Any]) -> CheckResult:
    output = evidence.get("provider_output")
    if not isinstance(output, dict):
        return CheckResult("PROVIDER_OUTPUT", STATUS_FAIL, "bounded provider-output verdict was not observed")
    if output.get("raw_output") is not None or output.get("transcript") is not None:
        return CheckResult("PROVIDER_OUTPUT", STATUS_FAIL, "raw provider output must not enter acceptance evidence")
    if output.get("observed") is not True:
        return CheckResult("PROVIDER_OUTPUT", STATUS_FAIL, "provider output was not observed")
    if output.get("matched_expected") is not True:
        return CheckResult("PROVIDER_OUTPUT", STATUS_FAIL, "provider output did not match the round expectation")
    if not _parse_iso(output.get("observed_at")):
        return CheckResult("PROVIDER_OUTPUT", STATUS_FAIL, "provider output observed_at is missing or invalid")
    method = output.get("verification_method")
    if method not in _OUTPUT_METHODS:
        return CheckResult("PROVIDER_OUTPUT", STATUS_FAIL, "provider output verification method is not bounded/recognized")
    digest = output.get("sha256")
    if digest is not None and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        return CheckResult("PROVIDER_OUTPUT", STATUS_FAIL, "provider output digest is not a SHA-256 digest")
    return CheckResult("PROVIDER_OUTPUT", STATUS_PASS, f"verified via {method}")


def _terminal_check(evidence: Dict[str, Any]) -> CheckResult:
    terminal = evidence.get("terminal") or {}
    if terminal.get("state") != "COMPLETED":
        return CheckResult("TERMINAL_TRUTH", STATUS_FAIL, f"terminal state is {terminal.get('state')!r}, expected 'COMPLETED'")
    if terminal.get("command_status") != "completed":
        return CheckResult("TERMINAL_TRUTH", STATUS_FAIL, "Command terminal status is not completed")
    if terminal.get("execution_status") != "completed":
        return CheckResult("TERMINAL_TRUTH", STATUS_FAIL, "Execution terminal status is not completed")
    cleanup = terminal.get("cleanup_evidence") or evidence.get("cleanup_evidence") or {}
    if cleanup.get("task_claim_release") != "released":
        return CheckResult("TERMINAL_TRUTH", STATUS_FAIL, "task claim release is not proven")
    if cleanup.get("writer_release") not in ("not_required", "released"):
        return CheckResult("TERMINAL_TRUTH", STATUS_FAIL, "writer release is not proven for read-only execution")
    return CheckResult("TERMINAL_TRUTH", STATUS_PASS, "completed with cleanup release evidence")


def _dashboard_check(evidence: Dict[str, Any]) -> CheckResult:
    truth = evidence.get("dashboard_truth") or {}
    if truth.get("observed") is not True:
        return CheckResult("DASHBOARD_TRUTH_EVIDENCE", STATUS_FAIL, "independent Dashboard observation was not recorded")
    if truth.get("backend_status") != "COMPLETED" or truth.get("dashboard_status") != "COMPLETED":
        return CheckResult("DASHBOARD_TRUTH_EVIDENCE", STATUS_FAIL, "Dashboard/backend terminal statuses are not both COMPLETED")
    if truth.get("matches") is not True:
        return CheckResult("DASHBOARD_TRUTH_EVIDENCE", STATUS_FAIL, "Dashboard truth does not match canonical backend truth")
    if not _parse_iso(truth.get("observed_at")):
        return CheckResult("DASHBOARD_TRUTH_EVIDENCE", STATUS_FAIL, "Dashboard observation timestamp is missing or invalid")
    return CheckResult("DASHBOARD_TRUTH_EVIDENCE", STATUS_PASS, "Dashboard terminal truth matches backend")


@dataclass
class TenRoundResult:
    round_number: int
    request_id: str
    checks: List[CheckResult]
    claim_latency: Optional[float]
    evidence_record: Dict[str, Any]

    @property
    def result(self) -> str:
        return STATUS_PASS if all(_pass(check.status) for check in self.checks) else STATUS_FAIL

    def as_dict(self) -> Dict[str, Any]:
        return {
            "round": self.round_number,
            "request_id": self.request_id,
            "claim_latency_seconds": self.claim_latency,
            "checks": [check.as_dict() for check in self.checks],
            "RESULT": self.result,
            "evidence": self.evidence_record,
        }


@dataclass
class TenRoundReport:
    run_id: str
    run_started_at: str
    results: List[TenRoundResult]
    required_count: int = ROUND_COUNT
    reason: str = ""

    @property
    def passed_count(self) -> int:
        return sum(result.result == STATUS_PASS for result in self.results)

    @property
    def overall(self) -> str:
        return STATUS_PASS if len(self.results) == self.required_count and self.passed_count == self.required_count else STATUS_FAIL

    def as_dict(self) -> Dict[str, Any]:
        return {
            "harness_version": HARNESS_VERSION,
            "run_id": self.run_id,
            "run_started_at": self.run_started_at,
            "required_count": self.required_count,
            "passed_count": self.passed_count,
            "results": [result.as_dict() for result in self.results],
            "HANDSOFF_TEN_ROUND_ACCEPTANCE": self.overall,
            "reason": self.reason,
        }


def _safe_provider_output(output: Any) -> Dict[str, Any]:
    """Copy only bounded provider-output metadata; never copy raw text."""
    if not isinstance(output, dict):
        return {"observed": False, "matched_expected": False}
    record = {
        "observed": output.get("observed") is True,
        "matched_expected": output.get("matched_expected") is True,
        "observed_at": output.get("observed_at"),
        "verification_method": output.get("verification_method"),
    }
    digest = output.get("sha256")
    if isinstance(digest, str) and _SHA256.fullmatch(digest):
        record["sha256"] = digest
    return record


def _evidence_record(evidence: Dict[str, Any], captured_at: str) -> Dict[str, Any]:
    ids = evidence.get("ids") or {}
    timestamps = evidence.get("timestamps") or {}
    execution = evidence.get("execution") or {}
    session = evidence.get("session") or {}
    provider_evidence = evidence.get("real_provider_evidence") or {}
    terminal = evidence.get("terminal") or {}
    cleanup = terminal.get("cleanup_evidence") or evidence.get("cleanup_evidence") or {}
    dashboard = evidence.get("dashboard_truth") or {}
    return {
        "captured_at": captured_at,
        "project_id": evidence.get("project_id"),
        "request_id": evidence.get("request_id"),
        "ids": {name: ids.get(name) for name in ("task_id", "command_id", "execution_id", "session_id", "handoff_id")},
        "claim": {
            "ingress_first_observed_at": timestamps.get("ingress_first_observed_at"),
            "claimed_at": timestamps.get("claimed_at"),
            "latency_seconds": claim_latency_seconds(evidence),
        },
        "execution": {
            "execution_id": execution.get("execution_id") or ids.get("execution_id"),
            "status": execution.get("status") or evidence.get("execution_status"),
            "reserved_at": timestamps.get("reserved_at"),
            "running_at": timestamps.get("running_at"),
            "terminal_at": timestamps.get("terminal_at"),
            "provider_evidence": {
                "present": provider_evidence.get("present") is True,
                "pid": provider_evidence.get("pid"),
                "host": provider_evidence.get("host"),
            },
        },
        "session": {
            "session_id": session.get("session_id") or ids.get("session_id"),
            "provider_session_id": session.get("provider_session_id"),
            "provider": session.get("provider") or evidence.get("provider"),
            "task_id": session.get("task_id"),
            "matching_execution_session_id": (execution.get("session_id") in (None, session.get("session_id") or ids.get("session_id"))),
        },
        "provider": {
            "provider": evidence.get("provider"),
            "account_id": evidence.get("account_id"),
            "provider_evidence_present": provider_evidence.get("present") is True,
            "output": _safe_provider_output(evidence.get("provider_output")),
        },
        "terminal": {
            "state": terminal.get("state"),
            "command_status": terminal.get("command_status"),
            "execution_status": terminal.get("execution_status"),
            "task_claim_release": cleanup.get("task_claim_release"),
            "writer_release": cleanup.get("writer_release"),
        },
        "dashboard_truth": {
            "observed": dashboard.get("observed") is True,
            "backend_status": dashboard.get("backend_status"),
            "dashboard_status": dashboard.get("dashboard_status"),
            "matches": dashboard.get("matches") is True,
            "observed_at": dashboard.get("observed_at"),
        },
    }


def evaluate_ten_rounds(
    evidences: Sequence[Dict[str, Any]],
    *,
    run_id: str,
    run_started_at: str,
    expected_project_id: str,
    tick_seconds: float,
    max_visibility_ticks: float = 2,
    max_claim_latency_seconds: Optional[float] = None,
) -> TenRoundReport:
    """Purely evaluate exactly ten evidence snapshots, without dispatch/I/O."""
    if len(evidences) != ROUND_COUNT:
        raise ValueError(f"exactly {ROUND_COUNT} evidence snapshots are required, got {len(evidences)}")
    max_claim_latency_seconds = max_claim_latency_seconds if max_claim_latency_seconds is not None else tick_seconds * max_visibility_ticks
    copied = [dict(evidence) for evidence in evidences]
    conflicts = detect_cross_task_borrowing(copied)
    results: List[TenRoundResult] = []
    for round_number, evidence in enumerate(copied, start=1):
        request_id = evidence.get("request_id", "")
        evidence.setdefault("cross_task_conflict", conflicts.get(request_id, {"found": False, "detail": ""}))
        base = evaluate_dispatch(
            evidence,
            expected_project_id=expected_project_id,
            tick_seconds=tick_seconds,
            max_visibility_ticks=max_visibility_ticks,
            acceptance_run_started_at=run_started_at,
        )
        extra = [
            _claim_check(evidence, max_claim_latency_seconds)[0],
            _execution_check(evidence),
            _session_check(evidence),
            _provider_output_check(evidence),
            _terminal_check(evidence),
            _dashboard_check(evidence),
        ]
        if evidence.get("harness_error"):
            extra.append(CheckResult("HARNESS_CALL", STATUS_FAIL, "dispatch/collection adapter did not complete"))
        results.append(TenRoundResult(
            round_number=round_number,
            request_id=request_id,
            checks=base.checks + extra,
            claim_latency=claim_latency_seconds(evidence),
            evidence_record=_evidence_record(evidence, _utc_iso()),
        ))
    passed = sum(result.result == STATUS_PASS for result in results)
    reason = f"{passed}/{ROUND_COUNT} rounds PASS" if passed == ROUND_COUNT else f"{passed}/{ROUND_COUNT} rounds PASS; every round is required"
    return TenRoundReport(run_id=run_id, run_started_at=run_started_at, results=results, reason=reason)


class JsonlEvidenceRecorder:
    """Append-only recorder for bounded run/round/summary evidence."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def emit(self, event: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")

    def consumed_request_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        consumed: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"acceptance evidence log is malformed: {self.path}") from exc
            if event.get("event") == "round" and event.get("request_id"):
                consumed.add(event["request_id"])
        return consumed


def _failed_evidence(project_id: str, request_id: str) -> Dict[str, Any]:
    return {"project_id": project_id, "request_id": request_id}


def run_unattended_ten_rounds(
    *,
    project_id: str,
    dispatch_round: Callable[[int, str], Any],
    collect_round: Callable[[int, str, Any], Dict[str, Any]],
    tick_seconds: float,
    request_ids: Optional[Sequence[str]] = None,
    run_id: Optional[str] = None,
    recorder: Optional[JsonlEvidenceRecorder] = None,
    max_visibility_ticks: float = 2,
    max_claim_latency_seconds: Optional[float] = None,
    now: Callable[[], str] = _utc_iso,
) -> TenRoundReport:
    """Run ten sequential adapter-owned rounds and record every outcome.

    `dispatch_round` is intentionally injected and called once per round;
    this harness does not know how to create a Task or start a provider.
    `collect_round` owns polling until a terminal snapshot is available.  A
    callback exception fails that round and does not trigger a redispatch.
    """
    run_id = run_id or f"{HARNESS_VERSION}-{uuid.uuid4().hex}"
    request_ids = list(request_ids or [f"{run_id}-r{number:02d}" for number in range(1, ROUND_COUNT + 1)])
    if len(request_ids) != ROUND_COUNT or len(set(request_ids)) != ROUND_COUNT:
        raise ValueError(f"request_ids must contain exactly {ROUND_COUNT} unique entries")
    if recorder is not None:
        reused = set(request_ids) & recorder.consumed_request_ids()
        if reused:
            raise FreshnessViolation(f"request_id(s) were already consumed by a prior run: {sorted(reused)}")

    run_started_at = now()
    if recorder is not None:
        recorder.emit({"event": "run_started", "harness_version": HARNESS_VERSION, "run_id": run_id,
                       "project_id": project_id, "run_started_at": run_started_at, "round_count": ROUND_COUNT})

    evidences: List[Dict[str, Any]] = []
    for round_number, request_id in enumerate(request_ids, start=1):
        dispatch_error = None
        receipt = None
        try:
            receipt = dispatch_round(round_number, request_id)
            evidence = collect_round(round_number, request_id, receipt)
            if not isinstance(evidence, dict):
                raise TypeError("collect_round must return a dict")
        except Exception as exc:  # one failed round must be recorded, not retried or dropped
            dispatch_error = type(exc).__name__
            evidence = _failed_evidence(project_id, request_id)
        evidence = dict(evidence)
        evidence.setdefault("project_id", project_id)
        evidence.setdefault("request_id", request_id)
        if evidence.get("request_id") != request_id:
            evidence["harness_error"] = "observed_request_id_mismatch"
        if dispatch_error:
            evidence["harness_error"] = dispatch_error
        evidences.append(evidence)
        if recorder is not None:
            # Persist each collected round before starting the next dispatch.
            # The final evaluated record still follows after all ten rounds so
            # cross-task checks remain authoritative; this snapshot is the
            # durable, append-only audit trail for a round that has actually
            # completed (including a dispatch/collection failure).
            snapshot = _evidence_record(evidence, now())
            snapshot["harness_error"] = dispatch_error
            recorder.emit({
                "event": "round_snapshot",
                "harness_version": HARNESS_VERSION,
                "run_id": run_id,
                "round": round_number,
                "request_id": request_id,
                "RESULT": "PENDING_FINAL_EVALUATION",
                "evidence": snapshot,
            })

    report = evaluate_ten_rounds(
        evidences,
        run_id=run_id,
        run_started_at=run_started_at,
        expected_project_id=project_id,
        tick_seconds=tick_seconds,
        max_visibility_ticks=max_visibility_ticks,
        max_claim_latency_seconds=max_claim_latency_seconds,
    )
    if recorder is not None:
        for result in report.results:
            recorder.emit({
                "event": "round",
                "harness_version": HARNESS_VERSION,
                "run_id": run_id,
                "round": result.round_number,
                "request_id": result.request_id,
                "claim_latency_seconds": result.claim_latency,
                "RESULT": result.result,
                "checks": [check.as_dict() for check in result.checks],
                "evidence": result.evidence_record,
            })
        recorder.emit({"event": "run_finished", **report.as_dict(), "finished_at": now()})
    return report
