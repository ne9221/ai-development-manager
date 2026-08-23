"""Deterministic acceptance harness for the hands-off dispatch lifecycle.

Purpose: prevent "one historical E2E happened to PASS -> declared done -> the
next fresh dispatch silently fails" by requiring N (default 3) *independent*
fresh request -> task -> command -> claim -> [provider run] -> session ->
handoff lifecycles to each satisfy an explicit, machine-checked contract
before HANDSOFF_DAILY_USABLE may be reported as PASS.

Design mirrors the evidence/evaluate split used by the (unmerged) rule44 and
global-hands-off-acceptance verifiers on sibling branches: a pure, dict-in
`evaluate_dispatch()` with zero I/O (fully unit-testable, used for every
FAIL-scenario test below) and a separate `collect_evidence()` that does the
real store reads. The two are independent so tests can construct exact
failure evidence without needing a live store, GitHub, or Windows Scheduled
Task state.

This module does not fix dispatch latency, quota routing, or drift-guard
issues; it only *measures and verifies* the lifecycle other in-flight work
produces. It reads the existing `tasks`/`commands`/`executions`/`sessions`/
`handoffs` store areas and schema fields (schema/*.schema.json on main) and
does not require any not-yet-merged schema/entity (e.g. a dedicated
dispatch-request record) to exist -- where such infrastructure is still on
other branches, `collect_evidence` degrades to UNKNOWN for the fields it
can't yet source, rather than fabricating a PASS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_UNKNOWN = "UNKNOWN"

# Worst-wins ranking used to fold many CheckResults into one verdict.
# UNKNOWN must never be dominated by PASS: a lifecycle we cannot fully
# observe is not an acceptable "PASS", even if every observed field is fine.
_RANK = {
    STATUS_PASS: 0,
    STATUS_NOT_APPLICABLE: 0,
    STATUS_BLOCKED: 1,
    STATUS_UNKNOWN: 2,
    STATUS_FAIL: 3,
}

TICKPOINTS = (
    "REQUEST_CREATED_AT",
    "TASK_CREATED_AT",
    "COMMAND_CREATED_AT",
    "CLAIMED_AT",
    "RUNNING_AT",
    "TERMINAL_AT",
    "HANDOFF_AT",
)


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class DispatchResult:
    request_id: str
    timestamps: Dict[str, Optional[str]]
    latency_seconds: Dict[str, Optional[float]]
    visibility_ticks: Optional[float]
    checks: List[CheckResult]
    visibility_verdict: str
    provider_verdict: str

    @property
    def verdict(self) -> str:
        # The 3/3 acceptance gate is driven by lifecycle *visibility*
        # (claimed-or-honestly-blocked within SLA, no borrowed/duplicate
        # state, dashboard agreement). Whether the provider actually got to
        # run is a resource-dependent, separately reported signal
        # ("若资源允许") and must not sink an otherwise-honest BLOCKED result.
        return self.visibility_verdict

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "timestamps": self.timestamps,
            "latency_seconds": self.latency_seconds,
            "visibility_ticks": self.visibility_ticks,
            "checks": [c.as_dict() for c in self.checks],
            "visibility_verdict": self.visibility_verdict,
            "provider_verdict": self.provider_verdict,
            "verdict": self.verdict,
        }


@dataclass
class AcceptanceReport:
    results: List[DispatchResult]
    required_count: int
    overall: str
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "results": [r.as_dict() for r in self.results],
            "required_count": self.required_count,
            "overall": self.overall,
            "reason": self.reason,
            "HANDSOFF_DAILY_USABLE": "PASS" if self.overall == STATUS_PASS else "FAIL",
        }


def _fold(statuses: List[str]) -> str:
    if not statuses:
        return STATUS_UNKNOWN
    return max(statuses, key=lambda s: _RANK.get(s, _RANK[STATUS_UNKNOWN]))


def _parse_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime

        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def evaluate_dispatch(
    evidence: Dict[str, Any],
    *,
    expected_project_id: str,
    tick_seconds: float,
    max_visibility_ticks: float,
    provider_optional: bool = True,
) -> DispatchResult:
    """Pure evaluator: judges one request's evidence dict. No I/O.

    Expected `evidence` shape (all keys optional; missing => UNKNOWN for the
    checks that depend on them):
        request_id: str
        project_id: str
        timestamps: {REQUEST_CREATED_AT..HANDOFF_AT: iso8601 str or None}
        linkage: {"task_found", "command_task_id_matches",
                  "execution_task_id_matches", "session_task_id_matches",
                  "handoff_task_id_matches": bool}
        provider_evidence: {"present": bool, "pid": Any, "host": Any} or None
        manual_trigger_evidence: {"found": bool, "source": str} or None
        duplicate_count: int or None
        dashboard_visibility: {"visible": bool, "matches_store": bool} or None
        blocked: {"is_blocked": bool, "blocked_reason": str, "reason_truthful": bool} or None
        provider_started: bool or None  # None == not evaluated / N/A
    """
    request_id = evidence.get("request_id", "")
    timestamps = dict(evidence.get("timestamps") or {})
    for point in TICKPOINTS:
        timestamps.setdefault(point, None)

    checks: List[CheckResult] = []

    # -- project scoping ---------------------------------------------------
    project_id = evidence.get("project_id")
    if project_id is None:
        checks.append(CheckResult("project_scope", STATUS_UNKNOWN, "no project_id in evidence"))
    elif project_id != expected_project_id:
        checks.append(CheckResult(
            "project_scope", STATUS_FAIL,
            f"evidence project_id={project_id!r} != expected {expected_project_id!r}",
        ))
    else:
        checks.append(CheckResult("project_scope", STATUS_PASS))

    # -- linkage: task/command/execution/session/handoff all point at the
    #    same task_id; nothing borrowed from a different task ------------
    linkage = evidence.get("linkage")
    if linkage is None:
        checks.append(CheckResult("linkage", STATUS_UNKNOWN, "no linkage evidence supplied"))
    else:
        if not linkage.get("task_found"):
            checks.append(CheckResult("linkage.task_found", STATUS_FAIL, "no Task record found for this request"))
        else:
            checks.append(CheckResult("linkage.task_found", STATUS_PASS))
        for edge in ("command_task_id_matches", "execution_task_id_matches", "session_task_id_matches", "handoff_task_id_matches"):
            value = linkage.get(edge)
            if value is None:
                checks.append(CheckResult(f"linkage.{edge}", STATUS_UNKNOWN, "not observed"))
            elif value is False:
                checks.append(CheckResult(f"linkage.{edge}", STATUS_FAIL, "record belongs to a different task_id (borrowed)"))
            else:
                checks.append(CheckResult(f"linkage.{edge}", STATUS_PASS))

    # -- duplicate executions/commands for this task ----------------------
    duplicate_count = evidence.get("duplicate_count")
    if duplicate_count is None:
        checks.append(CheckResult("no_duplicates", STATUS_UNKNOWN, "duplicate_count not observed"))
    elif duplicate_count > 0:
        checks.append(CheckResult("no_duplicates", STATUS_FAIL, f"duplicate_count={duplicate_count}"))
    else:
        checks.append(CheckResult("no_duplicates", STATUS_PASS))

    # -- manual trigger evidence must be absent ---------------------------
    manual = evidence.get("manual_trigger_evidence")
    if manual is None:
        checks.append(CheckResult("no_manual_trigger", STATUS_UNKNOWN, "manual-trigger evidence not observed"))
    elif manual.get("found"):
        checks.append(CheckResult(
            "no_manual_trigger", STATUS_FAIL,
            f"manual trigger evidence found: {manual.get('source', 'unspecified source')}",
        ))
    else:
        checks.append(CheckResult("no_manual_trigger", STATUS_PASS))

    # -- dashboard/store visibility agreement -----------------------------
    dash = evidence.get("dashboard_visibility")
    if dash is None:
        checks.append(CheckResult("dashboard_visibility", STATUS_UNKNOWN, "dashboard visibility not observed"))
    elif not dash.get("visible"):
        checks.append(CheckResult("dashboard_visibility", STATUS_FAIL, "task not visible in Dashboard/store"))
    elif not dash.get("matches_store", True):
        checks.append(CheckResult("dashboard_visibility", STATUS_FAIL, "Dashboard truth disagrees with store record"))
    else:
        checks.append(CheckResult("dashboard_visibility", STATUS_PASS))

    # -- blocked-reason truth ----------------------------------------------
    blocked = evidence.get("blocked")
    is_blocked = bool(blocked and blocked.get("is_blocked"))
    if blocked is not None and is_blocked:
        reason = blocked.get("blocked_reason")
        if not reason:
            checks.append(CheckResult("blocked_reason_truth", STATUS_FAIL, "blocked with no blocked_reason recorded"))
        elif blocked.get("reason_truthful") is False:
            checks.append(CheckResult("blocked_reason_truth", STATUS_FAIL, f"blocked_reason not truthful: {reason!r}"))
        elif blocked.get("reason_truthful") is None:
            checks.append(CheckResult("blocked_reason_truth", STATUS_UNKNOWN, "blocked_reason truthfulness not verified"))
        else:
            checks.append(CheckResult("blocked_reason_truth", STATUS_PASS, f"blocked_reason={reason!r}"))

    # -- visibility SLA: time to a definite, non-silent state -------------
    request_ts = _parse_iso(timestamps.get("REQUEST_CREATED_AT"))
    if is_blocked:
        definite_ts = _parse_iso(timestamps.get("TASK_CREATED_AT"))
        sla_label = "blocked-visible"
    else:
        definite_ts = _parse_iso(timestamps.get("CLAIMED_AT"))
        sla_label = "claimed"
    visibility_ticks: Optional[float] = None
    if request_ts is None or definite_ts is None:
        checks.append(CheckResult("visibility_sla", STATUS_UNKNOWN, "missing REQUEST_CREATED_AT or definite-state timestamp"))
    else:
        elapsed = definite_ts - request_ts
        if elapsed < 0:
            checks.append(CheckResult("visibility_sla", STATUS_FAIL, f"{sla_label} timestamp precedes request_created_at"))
        else:
            visibility_ticks = elapsed / tick_seconds if tick_seconds else math.inf
            if visibility_ticks > max_visibility_ticks:
                checks.append(CheckResult(
                    "visibility_sla", STATUS_FAIL,
                    f"{sla_label} after {visibility_ticks:.2f} ticks (max {max_visibility_ticks})",
                ))
            else:
                checks.append(CheckResult(
                    "visibility_sla", STATUS_PASS,
                    f"{sla_label} within {visibility_ticks:.2f} ticks (max {max_visibility_ticks})",
                ))

    if not is_blocked and definite_ts is None and request_ts is not None:
        checks.append(CheckResult("not_silent", STATUS_FAIL, "no CLAIMED_AT and not blocked: request went silent"))

    visibility_checks = [
        c for c in checks
        if c.name not in ("provider_evidence", "provider_started")
    ]
    visibility_verdict = _fold([c.status for c in visibility_checks])

    # -- provider running (only meaningful when not blocked / not skipped) -
    provider_started = evidence.get("provider_started")
    provider_evidence = evidence.get("provider_evidence")
    if is_blocked:
        provider_verdict = STATUS_BLOCKED
        checks.append(CheckResult("provider_started", STATUS_BLOCKED, "task blocked; provider run not expected"))
    elif provider_started is None:
        provider_verdict = STATUS_NOT_APPLICABLE if provider_optional else STATUS_UNKNOWN
        checks.append(CheckResult("provider_started", provider_verdict, "provider_started not observed"))
    elif provider_started is False:
        provider_verdict = STATUS_FAIL
        checks.append(CheckResult("provider_started", STATUS_FAIL, "provider did not start"))
    elif not provider_evidence or not provider_evidence.get("present"):
        provider_verdict = STATUS_FAIL
        checks.append(CheckResult("provider_started", STATUS_FAIL, "no PID/provider_evidence for a running provider"))
    else:
        provider_verdict = STATUS_PASS
        checks.append(CheckResult(
            "provider_started", STATUS_PASS,
            f"pid={provider_evidence.get('pid')} host={provider_evidence.get('host')}",
        ))

    latency_seconds: Dict[str, Optional[float]] = {}
    ordered_ts = [(point, _parse_iso(timestamps.get(point))) for point in TICKPOINTS]
    prev_point, prev_val = None, None
    for point, val in ordered_ts:
        if prev_val is not None and val is not None:
            latency_seconds[f"{prev_point}->{point}"] = val - prev_val
        prev_point, prev_val = point, val

    return DispatchResult(
        request_id=request_id,
        timestamps=timestamps,
        latency_seconds=latency_seconds,
        visibility_ticks=visibility_ticks,
        checks=checks,
        visibility_verdict=visibility_verdict,
        provider_verdict=provider_verdict,
    )


def run_acceptance(
    evidences: List[Dict[str, Any]],
    *,
    expected_project_id: str,
    tick_seconds: float,
    max_visibility_ticks: float,
    required_count: int = 3,
    provider_optional: bool = True,
) -> AcceptanceReport:
    """Evaluate a batch of fresh dispatches and decide HANDSOFF_DAILY_USABLE.

    PASS requires at least `required_count` evidences, each with
    visibility_verdict == PASS. A single UNKNOWN or FAIL anywhere sinks the
    whole batch -- there is no partial credit, matching the "3/3 or nothing"
    contract in the task spec.
    """
    results = [
        evaluate_dispatch(
            evidence,
            expected_project_id=expected_project_id,
            tick_seconds=tick_seconds,
            max_visibility_ticks=max_visibility_ticks,
            provider_optional=provider_optional,
        )
        for evidence in evidences
    ]

    if len(results) < required_count:
        return AcceptanceReport(
            results=results, required_count=required_count, overall=STATUS_FAIL,
            reason=f"only {len(results)} dispatch(es) supplied, need {required_count}",
        )

    considered = results[:required_count] if len(results) > required_count else results
    verdicts = [r.verdict for r in considered]
    overall = _fold(verdicts)
    # _fold never returns PASS unless every input was PASS/NOT_APPLICABLE;
    # NOT_APPLICABLE never occurs in visibility_verdict, so PASS here means
    # every one of the required dispatches independently PASSed.
    if overall == STATUS_PASS:
        reason = f"{required_count}/{required_count} fresh dispatches PASS"
    else:
        failing = [r.request_id for r in considered if r.verdict != STATUS_PASS]
        reason = f"not all {required_count} fresh dispatches PASS (failing: {failing})"

    return AcceptanceReport(results=considered, required_count=required_count, overall=overall, reason=reason)


# ---------------------------------------------------------------------------
# Evidence collection against a real store. Kept separate from evaluate_dispatch
# so tests never need a store; only wired up here for the real acceptance run.
# ---------------------------------------------------------------------------

def collect_evidence(
    store: Any,
    project_id: str,
    request_id: str,
    *,
    dashboard_visibility_probe: Optional[Any] = None,
) -> Dict[str, Any]:
    """Best-effort real-store evidence collection for one request_id.

    Resolution path: scan the `tasks` area of `store` for a Task whose
    `source_context` dict carries a matching `request_id` (the convention
    used by the dispatch-ingress work landing on other branches); then walk
    task -> command(s) -> execution -> session -> handoff via the task_id
    foreign keys already required by schema/{command,execution,session,
    handoff}.schema.json.

    Any leg of this walk that a still-unmerged branch's infrastructure would
    normally populate (e.g. a dedicated dispatch-request record, or a
    manual-trigger marker field) degrades to `None`/absent rather than being
    guessed at, so `evaluate_dispatch` reports UNKNOWN instead of a false
    PASS. `dashboard_visibility_probe`, if supplied, is a callable
    `(project_id, task_id) -> {"visible": bool, "matches_store": bool}`
    wired up by the caller to whatever the real Dashboard/store health check
    is; this module does not assume a specific Dashboard API exists.
    """
    records = getattr(store, "records", None)
    tasks = []
    if records is not None:
        for (area, proj, _name), doc in records.items():
            if area == "tasks" and proj == project_id:
                source_context = doc.get("source_context") or {}
                if source_context.get("request_id") == request_id:
                    tasks.append(doc)

    if not tasks:
        return {"request_id": request_id, "project_id": project_id, "linkage": {"task_found": False}}

    task = max(tasks, key=lambda t: t.get("created_at") or "")
    task_id = task.get("task_id")

    commands = [doc for (area, proj, _n), doc in records.items() if area == "commands" and proj == project_id and doc.get("task_id") == task_id]
    executions = [doc for (area, proj, _n), doc in records.items() if area == "executions" and proj == project_id and doc.get("task_id") == task_id]
    sessions = [doc for (area, proj, _n), doc in records.items() if area == "sessions" and proj == project_id and doc.get("task_id") == task_id]
    handoffs = [doc for (area, proj, _n), doc in records.items() if area == "handoffs" and proj == project_id and doc.get("task_id") == task_id]

    command = max(commands, key=lambda c: c.get("created_at") or "") if commands else None
    execution = None
    if command and command.get("execution_id"):
        execution = next((e for e in executions if e.get("execution_id") == command.get("execution_id")), None)
    elif executions:
        execution = max(executions, key=lambda e: e.get("started_at") or "")

    session = None
    if execution and execution.get("session_id"):
        session = next((s for s in sessions if s.get("session_id") == execution.get("session_id")), None)

    handoff = max(handoffs, key=lambda h: h.get("created_at") or "") if handoffs else None

    is_blocked = task.get("status") == "blocked"

    timestamps = {
        "REQUEST_CREATED_AT": (task.get("source_context") or {}).get("request_created_at") or task.get("created_at"),
        "TASK_CREATED_AT": task.get("created_at"),
        "COMMAND_CREATED_AT": command.get("created_at") if command else None,
        "CLAIMED_AT": command.get("claimed_at") if command else None,
        "RUNNING_AT": execution.get("started_at") if execution else None,
        "TERMINAL_AT": (execution.get("completed_at") or execution.get("finished_at")) if execution else None,
        "HANDOFF_AT": handoff.get("created_at") if handoff else None,
    }

    linkage = {
        "task_found": True,
        "command_task_id_matches": (command.get("task_id") == task_id) if command else None,
        "execution_task_id_matches": (execution.get("task_id") == task_id) if execution else None,
        "session_task_id_matches": (session.get("task_id") == task_id) if session else None,
        "handoff_task_id_matches": (handoff.get("task_id") == task_id) if handoff else None,
    }

    duplicate_count = max(0, len(commands) - 1) + max(0, len(executions) - 1)

    provider_evidence = None
    if execution and execution.get("provider_evidence"):
        pe = execution["provider_evidence"]
        provider_evidence = {"present": bool(pe.get("pid")), "pid": pe.get("pid"), "host": pe.get("host")}

    manual_trigger_evidence = None
    if command is not None:
        marker_text = " ".join(str(command.get(field, "")) for field in ("selection_reason",)).lower()
        manual_trigger_evidence = {"found": "manual" in marker_text, "source": "command.selection_reason" if "manual" in marker_text else None}

    dashboard_visibility = None
    if dashboard_visibility_probe is not None:
        dashboard_visibility = dashboard_visibility_probe(project_id, task_id)

    blocked_reason = task.get("blocked_reason") if is_blocked else None

    return {
        "request_id": request_id,
        "project_id": project_id,
        "timestamps": timestamps,
        "linkage": linkage,
        "provider_evidence": provider_evidence,
        "manual_trigger_evidence": manual_trigger_evidence,
        "duplicate_count": duplicate_count,
        "dashboard_visibility": dashboard_visibility,
        "blocked": {"is_blocked": is_blocked, "blocked_reason": blocked_reason, "reason_truthful": None} if is_blocked else None,
        "provider_started": bool(execution) if not is_blocked else None,
    }
