"""Deterministic 3-consecutive-fresh-dispatch acceptance harness.

Hands-off is only accepted when 3 CONSECUTIVE FRESH dispatch requests each
fully PASS an explicit, machine-checked contract -- not "historical
successes", not cherry-picked passes after retries, not an SLA-breaching-
but-eventually-successful request, not a manually-triggered success, and not
one built on borrowed/reused execution or session records.

This module is DEVELOPMENT-ONLY tooling: it *evaluates* evidence, it never
triggers a dispatch, never calls `process_command`, never starts a provider,
and never touches the Windows Scheduled Task. All of that stays entirely on
the caller's side; `collect_evidence()` here only reads existing store state.

Design note: mirrors the evidence/evaluate split used by the (unmerged)
`manager/handsoff_reliability_acceptance.py` prior art on
`test/handsoff-three-dispatch-acceptance-20260824` -- a pure, dict-in
`evaluate_dispatch()` with zero I/O (fully unit-testable against every
required failure scenario) and a separate `collect_evidence()` that does the
real store walk. This module diverges from that prior art where the task
spec requires more: the SLA clock starts at `ingress_first_observed_at` (not
`request_created_at`), visibility is split into independent
BACKEND_VISIBLE/USER_VISIBLE checks, linkage is reported per-stage
(TASK/COMMAND/EXECUTION/SESSION/HANDOFF) with an honest N/A for stages that
never occurred, cross-task ID borrowing is detected across an entire batch
(not just within one request), and a `RunLedger` makes it structurally
impossible to silently drop a failed sample and recombine passes from
different runs into a false 3/3.

Freshness: FRESHNESS is computed, never caller-asserted. It compares each
request's own `ingress_first_observed_at` against an explicit
`acceptance_run_started_at` cutoff supplied to `evaluate_dispatch()` /
`evaluate_three_consecutive()` -- never against request_id lexical order,
which `RunLedger` uses only as a same-process secondary guard against
reusing/recombining ids across evaluation runs, not as freshness truth. The
FRESHNESS check is unconditionally appended to every evaluation (unlike a
caller-supplied-optional check), so a missing cutoff, a missing
`ingress_first_observed_at`, or an unparseable timestamp all degrade to an
honest UNKNOWN (folded to FAIL) rather than silently omitting the check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NOT_APPLICABLE = "N/A"
STATUS_UNKNOWN = "UNKNOWN"

# Worst-wins ranking for folding many CheckResults into one RESULT. UNKNOWN
# is deliberately never dominated by PASS: evidence we could not fully
# observe from a real store must never be reported as an honest PASS.
_RANK = {
    STATUS_PASS: 0,
    STATUS_NOT_APPLICABLE: 0,
    STATUS_UNKNOWN: 1,
    STATUS_FAIL: 2,
}

# Statuses the spec accepts as "a recognized status" for the visibility SLA.
RECOGNIZED_VISIBLE_STATUSES = {"ACCEPTED", "QUEUED", "RUNNING", "BLOCKED", "REJECTED", "FAILED"}

TERMINAL_STATES_REQUIRING_REASON = {"BLOCKED", "REJECTED", "FAILED"}

TIMESTAMP_FIELDS = (
    "request_created_at",
    "ingress_first_observed_at",
    "task_created_at",
    "command_created_at",
    "claimed_at",
    "reserved_at",
    "running_at",
    "terminal_at",
    "handoff_at",
)

LINKAGE_STAGES = ("task", "command", "execution", "session", "handoff")
LINKAGE_CHECK_NAMES = {
    "task": "TASK_LINKAGE",
    "command": "COMMAND_LINKAGE",
    "execution": "EXECUTION_LINKAGE",
    "session": "SESSION_LINKAGE",
    "handoff": "HANDOFF_LINKAGE",
}

ID_FIELDS = ("task_id", "command_id", "execution_id", "session_id", "handoff_id")

# Fields the spec requires every evidence record to carry (informational --
# collect_evidence() degrades absent ones to None/UNKNOWN rather than
# fabricating them).
REQUIRED_EVIDENCE_FIELDS = (
    "request_id", "project_id", "request_created_at", "ingress_first_observed_at",
    "first_user_visible_at", "task_created_at", "command_created_at", "claimed_at",
    "reserved_at", "running_at", "terminal_at", "handoff_at", "provider", "account_id",
    "task_id", "command_id", "execution_id", "session_id", "handoff_id",
    "duplicate_counts", "manual_trigger_evidence",
)


class FreshnessViolation(ValueError):
    """Raised when a declared batch of request_ids is not fresh/consecutive.

    This is intentionally a hard error (not a FAIL result) so that reusing,
    reordering, or recombining request_ids across separate evaluation runs
    is structurally impossible rather than merely discouraged.
    """


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}

    @property
    def display_status(self) -> str:
        """PASS/FAIL/N/A only -- UNKNOWN is never surfaced as a bare pass."""
        if self.status == STATUS_UNKNOWN:
            return STATUS_FAIL
        return self.status


def _fold(statuses: Sequence[str]) -> str:
    if not statuses:
        return STATUS_UNKNOWN
    worst = max(statuses, key=lambda s: _RANK.get(s, _RANK[STATUS_UNKNOWN]))
    return worst


def _parse_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        # A naive timestamp carries no fixed instant -- interpreting it as
        # local machine time would make freshness/SLA comparisons depend on
        # the evaluating host's timezone, which is not deterministic. Treat
        # it as malformed rather than silently guessing an offset.
        return None
    return parsed.timestamp()


def _compute_freshness(
    ingress_first_observed_at: Optional[str],
    acceptance_run_started_at: Optional[str],
) -> Dict[str, Any]:
    """Single source of truth for the freshness determination.

    Pure and deterministic: PASS iff `ingress_first_observed_at` is at or
    after `acceptance_run_started_at` (instant comparison, timezone-aware,
    never request_id lexical order). A missing cutoff, a missing
    `ingress_first_observed_at`, or an unparseable/naive timestamp all
    degrade to UNKNOWN rather than defaulting to PASS.

    Used by both `evaluate_dispatch()` (as the authority for the FRESHNESS
    CheckResult -- always recomputed there from its own
    `acceptance_run_started_at` argument, never trusted from a caller- or
    collector-supplied `evidence["freshness"]`) and `collect_evidence()`
    (to attach an observability record onto the evidence it returns). Using
    one function in both places means the two can never silently disagree.
    """
    if acceptance_run_started_at is None:
        return {
            "status": STATUS_UNKNOWN,
            "ingress_first_observed_at": ingress_first_observed_at,
            "acceptance_run_started_at": None,
            "reason": "no acceptance_run_started_at cutoff supplied",
        }
    if ingress_first_observed_at is None:
        return {
            "status": STATUS_UNKNOWN,
            "ingress_first_observed_at": None,
            "acceptance_run_started_at": acceptance_run_started_at,
            "reason": "no ingress_first_observed_at in evidence",
        }
    ingress_ts = _parse_iso(ingress_first_observed_at)
    cutoff_ts = _parse_iso(acceptance_run_started_at)
    if ingress_ts is None or cutoff_ts is None:
        return {
            "status": STATUS_UNKNOWN,
            "ingress_first_observed_at": ingress_first_observed_at,
            "acceptance_run_started_at": acceptance_run_started_at,
            "reason": "ingress_first_observed_at or acceptance_run_started_at not parseable",
        }
    if ingress_ts >= cutoff_ts:
        return {
            "status": STATUS_PASS,
            "ingress_first_observed_at": ingress_first_observed_at,
            "acceptance_run_started_at": acceptance_run_started_at,
            "reason": f"ingress_first_observed_at {ingress_first_observed_at!r} >= acceptance_run_started_at {acceptance_run_started_at!r}",
        }
    return {
        "status": STATUS_FAIL,
        "ingress_first_observed_at": ingress_first_observed_at,
        "acceptance_run_started_at": acceptance_run_started_at,
        "reason": (
            f"ingress_first_observed_at {ingress_first_observed_at!r} precedes acceptance_run_started_at "
            f"{acceptance_run_started_at!r} (historical/reused record, cannot count toward this run)"
        ),
    }


@dataclass
class DispatchResult:
    request_id: str
    sla_start: Optional[str]
    first_visible: Optional[str]
    visibility_ticks: Optional[float]
    checks: List[CheckResult]
    scheduler_pickup_latency_seconds: Optional[float]

    def _check(self, name: str) -> Optional[CheckResult]:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    @property
    def result(self) -> str:
        """PASS/FAIL only. UNKNOWN is folded into FAIL: evidence we could
        not fully observe must never be reported as an honest PASS."""
        folded = _fold([c.status for c in self.checks])
        return STATUS_PASS if folded == STATUS_PASS else STATUS_FAIL

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "sla_start": self.sla_start,
            "first_visible": self.first_visible,
            "visibility_ticks": self.visibility_ticks,
            "scheduler_pickup_latency_seconds": self.scheduler_pickup_latency_seconds,
            "checks": [c.as_dict() for c in self.checks],
            "RESULT": self.result,
        }


@dataclass
class AcceptanceReport:
    results: List[DispatchResult]
    required_count: int
    reason: str = ""

    @property
    def consecutive_pass_count(self) -> int:
        count = 0
        for r in self.results:
            if r.result == STATUS_PASS:
                count += 1
            else:
                break
        return count

    @property
    def overall(self) -> str:
        return STATUS_PASS if self.consecutive_pass_count >= self.required_count else STATUS_FAIL

    def as_dict(self) -> Dict[str, Any]:
        return {
            "results": [r.as_dict() for r in self.results],
            "required_count": self.required_count,
            "CONSECUTIVE_PASS_COUNT": self.consecutive_pass_count,
            "HANDSOFF_DAILY_USABLE": self.overall,
            "reason": self.reason,
        }

    def to_json(self) -> Dict[str, Any]:
        return self.as_dict()

    def to_human_summary(self) -> str:
        blocks: List[str] = []
        for idx, r in enumerate(self.results, start=1):
            def line(name: str) -> str:
                c = r._check(name)
                return f"{name}: {c.display_status if c else STATUS_FAIL}"

            blocks.append("\n".join([
                f"REQUEST_{idx}:",
                f"REQUEST_ID: {r.request_id}",
                f"SLA_START: {r.sla_start}",
                f"FIRST_VISIBLE: {r.first_visible}",
                f"VISIBILITY_TICKS: {r.visibility_ticks}",
                line("BACKEND_VISIBLE"),
                line("USER_VISIBLE"),
                line("FRESHNESS"),
                line("TASK_LINKAGE"),
                line("COMMAND_LINKAGE"),
                line("EXECUTION_LINKAGE"),
                line("SESSION_LINKAGE"),
                line("HANDOFF_LINKAGE"),
                line("IDEMPOTENCY"),
                line("REAL_PROVIDER"),
                line("NO_MANUAL_TRIGGER"),
                line("DASHBOARD_TRUTH"),
                f"RESULT: {r.result}",
            ]))
        blocks.append("\n".join([
            f"CONSECUTIVE_PASS_COUNT: {self.consecutive_pass_count}",
            f"HANDSOFF_DAILY_USABLE: {self.overall}",
        ]))
        return "\n\n".join(blocks)


def detect_cross_task_borrowing(evidences: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Pure, I/O-free scan for IDs shared across different requests in a batch.

    Task A's Execution/Session/Handoff/Command must never belong to Task B.
    Returns {request_id: {"found": bool, "detail": str}} for every request_id
    present in `evidences` (found=False when no conflict).
    """
    owners: Dict[str, Dict[str, str]] = {field: {} for field in ID_FIELDS}
    conflicts: Dict[str, List[str]] = {}

    for ev in evidences:
        rid = ev.get("request_id")
        ids = ev.get("ids") or {}
        for field_name in ID_FIELDS:
            value = ids.get(field_name)
            if not value:
                continue
            owner_map = owners[field_name]
            if value in owner_map and owner_map[value] != rid:
                other = owner_map[value]
                conflicts.setdefault(rid, []).append(f"{field_name}={value!r} also owned by request {other!r}")
                conflicts.setdefault(other, []).append(f"{field_name}={value!r} also owned by request {rid!r}")
            else:
                owner_map[value] = rid

    result: Dict[str, Dict[str, Any]] = {}
    for ev in evidences:
        rid = ev.get("request_id")
        detail_list = conflicts.get(rid, [])
        result[rid] = {"found": bool(detail_list), "detail": "; ".join(detail_list)}
    return result


def evaluate_dispatch(
    evidence: Dict[str, Any],
    *,
    expected_project_id: str,
    tick_seconds: float,
    max_visibility_ticks: float = 2,
    acceptance_run_started_at: Optional[str] = None,
) -> DispatchResult:
    """Pure evaluator: judges one request's evidence dict. No I/O.

    See module docstring / REQUIRED_EVIDENCE_FIELDS for the expected shape;
    every key is optional and missing data degrades the relevant check to
    UNKNOWN (which is never displayed as PASS) rather than a fabricated PASS.

    `acceptance_run_started_at` is the explicit cutoff a real 3/3 run must
    declare (see FRESHNESS below); omitting it is itself a fail-closed
    UNKNOWN, not silent success.
    """
    request_id = evidence.get("request_id", "")
    ts = dict(evidence.get("timestamps") or {})
    for f_ in TIMESTAMP_FIELDS:
        ts.setdefault(f_, None)

    checks: List[CheckResult] = []

    # -- project scoping ---------------------------------------------------
    project_id = evidence.get("project_id")
    if project_id is None:
        checks.append(CheckResult("PROJECT_SCOPE", STATUS_UNKNOWN, "no project_id in evidence"))
    elif project_id != expected_project_id:
        checks.append(CheckResult("PROJECT_SCOPE", STATUS_FAIL, f"project_id={project_id!r} != expected {expected_project_id!r}"))
    else:
        checks.append(CheckResult("PROJECT_SCOPE", STATUS_PASS))

    # -- SLA start point: ingress_first_observed_at, NOT request_created_at
    sla_start_raw = ts.get("ingress_first_observed_at")
    sla_start = _parse_iso(sla_start_raw)
    request_created_raw = ts.get("request_created_at")
    request_created = _parse_iso(request_created_raw)

    scheduler_pickup_latency: Optional[float] = None
    if sla_start is not None and request_created is not None:
        scheduler_pickup_latency = sla_start - request_created
        if scheduler_pickup_latency < 0:
            checks.append(CheckResult(
                "SCHEDULER_PICKUP_LATENCY", STATUS_FAIL,
                "ingress_first_observed_at precedes request_created_at",
            ))

    # -- visibility: BACKEND_VISIBLE and USER_VISIBLE, independently, both
    #    measured against the 2-tick window from SLA_START -----------------
    backend_vis = evidence.get("backend_visibility")
    user_vis = evidence.get("user_visibility")

    def _visibility_check(name: str, vis: Optional[Dict[str, Any]]) -> Optional[float]:
        if sla_start_raw is None:
            checks.append(CheckResult(name, STATUS_UNKNOWN, "no ingress_first_observed_at (SLA_START) in evidence"))
            return None
        if vis is None:
            checks.append(CheckResult(name, STATUS_UNKNOWN, "visibility not observed"))
            return None
        status = vis.get("status")
        observed_at_raw = vis.get("observed_at")
        observed_at = _parse_iso(observed_at_raw)
        if status is None or observed_at is None:
            checks.append(CheckResult(name, STATUS_FAIL, "no recognized status ever observed (silent request)"))
            return None
        if status not in RECOGNIZED_VISIBLE_STATUSES:
            checks.append(CheckResult(name, STATUS_FAIL, f"status {status!r} is not a recognized visible status"))
            return None
        if sla_start is None:
            checks.append(CheckResult(name, STATUS_UNKNOWN, "ingress_first_observed_at not parseable"))
            return None
        elapsed = observed_at - sla_start
        if elapsed < 0:
            checks.append(CheckResult(name, STATUS_FAIL, "observed_at precedes SLA_START"))
            return None
        ticks = elapsed / tick_seconds if tick_seconds else math.inf
        if ticks > max_visibility_ticks:
            checks.append(CheckResult(name, STATUS_FAIL, f"{status} visible after {ticks:.2f} ticks (max {max_visibility_ticks})"))
        else:
            checks.append(CheckResult(name, STATUS_PASS, f"{status} visible within {ticks:.2f} ticks (max {max_visibility_ticks})"))
        return ticks

    backend_ticks = _visibility_check("BACKEND_VISIBLE", backend_vis)
    user_ticks = _visibility_check("USER_VISIBLE", user_vis)

    tick_candidates = [t for t in (backend_ticks, user_ticks) if t is not None]
    visibility_ticks = max(tick_candidates) if tick_candidates else None

    observed_candidates = []
    for vis in (backend_vis, user_vis):
        if vis and vis.get("observed_at"):
            observed_candidates.append(vis["observed_at"])
    first_visible = min(observed_candidates) if observed_candidates else evidence.get("first_user_visible_at")

    # -- linkage: per stage, honest PASS/FAIL/N/A --------------------------
    linkage = evidence.get("linkage") or {}
    ids = evidence.get("ids") or {}
    for stage in LINKAGE_STAGES:
        check_name = LINKAGE_CHECK_NAMES[stage]
        stage_info = linkage.get(stage)
        if stage_info is None:
            # No linkage evidence supplied at all for this stage. If the
            # stage's ID is also absent, treat it as "never happened" (N/A,
            # e.g. legitimately blocked before a Command existed). If the ID
            # IS present but linkage was never checked, that's UNKNOWN, not
            # a free pass.
            if not ids.get(f"{stage}_id") and stage != "task":
                checks.append(CheckResult(check_name, STATUS_NOT_APPLICABLE, f"{stage} stage never reached"))
            else:
                checks.append(CheckResult(check_name, STATUS_UNKNOWN, "linkage not observed"))
            continue
        occurred = stage_info.get("occurred", True)
        if not occurred:
            checks.append(CheckResult(check_name, STATUS_NOT_APPLICABLE, f"{stage} stage never reached"))
            continue
        matches = stage_info.get("task_id_matches")
        if matches is None:
            checks.append(CheckResult(check_name, STATUS_UNKNOWN, "task_id_matches not observed"))
        elif matches is False:
            checks.append(CheckResult(check_name, STATUS_FAIL, f"{stage} record belongs to a different task_id (borrowed/mismatched)"))
        else:
            checks.append(CheckResult(check_name, STATUS_PASS))

    # -- cross-task borrowing (batch-level; may be pre-populated by the
    #    run orchestrator via detect_cross_task_borrowing) ----------------
    conflict = evidence.get("cross_task_conflict")
    if conflict is not None:
        if conflict.get("found"):
            checks.append(CheckResult("CROSS_TASK_BORROWING", STATUS_FAIL, conflict.get("detail", "")))
        else:
            checks.append(CheckResult("CROSS_TASK_BORROWING", STATUS_PASS))

    # -- freshness: reject a historical/pre-existing record mistakenly
    #    picked up as if it were fresh. ALWAYS recomputed here via the same
    #    _compute_freshness() used by collect_evidence() -- this function is
    #    the authority regardless of whether evidence["freshness"] happens
    #    to already carry a (possibly stale, possibly built against a
    #    different cutoff) precomputed record; never from request_id
    #    lexical order (that is RunLedger's job, and only as a same-process
    #    secondary guard). The check is ALWAYS appended, so a missing
    #    cutoff or a missing/unparseable ingress_first_observed_at cannot
    #    silently vanish from the result; it degrades to UNKNOWN (folded to
    #    FAIL), never to an absent check.
    freshness = _compute_freshness(sla_start_raw, acceptance_run_started_at)
    checks.append(CheckResult("FRESHNESS", freshness["status"], freshness["reason"]))

    # -- idempotency: exactly one canonical Task and one canonical Command;
    #    duplicate_counts >1 for any canonical entity is a FAIL ------------
    dup = evidence.get("duplicate_counts")
    if dup is None:
        checks.append(CheckResult("IDEMPOTENCY", STATUS_UNKNOWN, "duplicate_counts not observed"))
    else:
        offenders = {k: v for k, v in dup.items() if isinstance(v, (int, float)) and v > 1}
        if offenders:
            checks.append(CheckResult("IDEMPOTENCY", STATUS_FAIL, f"duplicate lifecycle(s) detected: {offenders}"))
        else:
            checks.append(CheckResult("IDEMPOTENCY", STATUS_PASS))

    # -- real provider evidence (only meaningful once RUNNING was reached) -
    reached_running = evidence.get("reached_running")
    provider_evidence = evidence.get("real_provider_evidence")
    if reached_running is None:
        checks.append(CheckResult("REAL_PROVIDER", STATUS_UNKNOWN, "reached_running not observed"))
    elif reached_running is False:
        checks.append(CheckResult("REAL_PROVIDER", STATUS_NOT_APPLICABLE, "request never reached RUNNING"))
    elif not provider_evidence or not provider_evidence.get("present"):
        checks.append(CheckResult("REAL_PROVIDER", STATUS_FAIL, "reached RUNNING but no real provider process/session evidence (fake/borrowed/manual)"))
    else:
        checks.append(CheckResult("REAL_PROVIDER", STATUS_PASS, f"pid={provider_evidence.get('pid')} host={provider_evidence.get('host')}"))

    # -- no manual intervention --------------------------------------------
    manual = evidence.get("manual_trigger_evidence")
    if manual is None:
        checks.append(CheckResult("NO_MANUAL_TRIGGER", STATUS_UNKNOWN, "manual-trigger evidence not observed"))
    elif manual.get("found"):
        checks.append(CheckResult("NO_MANUAL_TRIGGER", STATUS_FAIL, f"manual trigger evidence found: {manual.get('source', 'unspecified source')}"))
    else:
        checks.append(CheckResult("NO_MANUAL_TRIGGER", STATUS_PASS))

    # -- dashboard truth: user-visible surface must equal canonical truth --
    backend_status = evidence.get("backend_status")
    dashboard_status = evidence.get("dashboard_status")
    if backend_status is None and dashboard_status is None:
        checks.append(CheckResult("DASHBOARD_TRUTH", STATUS_UNKNOWN, "neither backend_status nor dashboard_status observed"))
    elif backend_status is None or dashboard_status is None:
        checks.append(CheckResult("DASHBOARD_TRUTH", STATUS_UNKNOWN, "only one side of backend/dashboard status observed"))
    elif dashboard_status != backend_status:
        checks.append(CheckResult("DASHBOARD_TRUTH", STATUS_FAIL, f"backend={backend_status!r} but dashboard={dashboard_status!r}"))
    else:
        checks.append(CheckResult("DASHBOARD_TRUTH", STATUS_PASS))

    # -- terminal truth: BLOCKED/REJECTED/FAILED must carry a machine-
    #    readable reason code, not merely exist ----------------------------
    terminal = evidence.get("terminal")
    if terminal is not None:
        state = terminal.get("state")
        if state in TERMINAL_STATES_REQUIRING_REASON:
            reason_code = terminal.get("reason_code")
            if not reason_code:
                checks.append(CheckResult("TERMINAL_REASON_CODE", STATUS_FAIL, f"{state} with no machine-readable reason_code"))
            else:
                checks.append(CheckResult("TERMINAL_REASON_CODE", STATUS_PASS, f"{state}: {reason_code}"))

    return DispatchResult(
        request_id=request_id,
        sla_start=sla_start_raw,
        first_visible=first_visible,
        visibility_ticks=visibility_ticks,
        checks=checks,
        scheduler_pickup_latency_seconds=scheduler_pickup_latency,
    )


@dataclass
class RunLedger:
    """Tracks request_ids already consumed by prior evaluation runs.

    Passing the same RunLedger instance into successive
    `evaluate_three_consecutive()` calls makes it structurally impossible to
    reuse a request_id from a previous run (whether that run passed or
    failed), and impossible to submit an out-of-order/non-monotonic id
    relative to what has already been consumed -- i.e. you cannot drop a
    failed sample from one run and cherry-pick 3 passing ones by recombining
    ids across runs.

    This is a SECONDARY, same-process-only guard, not the freshness source
    of truth: it has no memory across separate script invocations (nothing
    here persists it to disk), and its ordering check trusts request_id
    lexical order. Actual freshness -- whether a request counts as having
    happened at or after this acceptance run -- is decided independently by
    the FRESHNESS check in `evaluate_dispatch()`, which compares each
    request's own `ingress_first_observed_at` against the explicit
    `acceptance_run_started_at` cutoff. A brand-new RunLedger (a fresh
    process with no reuse history at all) still cannot let a historical
    request pass, because FRESHNESS does not consult the ledger.
    """

    seen_request_ids: set = field(default_factory=set)
    max_consumed_request_id: Optional[str] = None

    def record(self, request_ids: Sequence[str]) -> None:
        self.seen_request_ids.update(request_ids)
        batch_max = max(request_ids)
        if self.max_consumed_request_id is None or batch_max > self.max_consumed_request_id:
            self.max_consumed_request_id = batch_max


def evaluate_three_consecutive(
    declared_request_ids: Sequence[str],
    evidence_by_id: Dict[str, Dict[str, Any]],
    *,
    expected_project_id: str,
    tick_seconds: float,
    max_visibility_ticks: float = 2,
    required_count: int = 3,
    acceptance_run_started_at: Optional[str] = None,
    ledger: Optional[RunLedger] = None,
) -> AcceptanceReport:
    """Evaluate a pre-declared, fixed batch of fresh request_ids.

    `declared_request_ids` must be supplied and evaluated as-is -- this
    function never filters, reorders, or drops entries, so a caller cannot
    silently exclude a failing sample and recompute 3/3 from later
    successes. If a `RunLedger` is supplied, request_ids already consumed by
    a prior run (or out of order relative to them) raise
    `FreshnessViolation` before any evaluation happens.

    `acceptance_run_started_at` is the explicit cutoff every real 3/3 run
    must declare (ISO8601, timezone-aware) -- forwarded unchanged to every
    `evaluate_dispatch()` call in the batch, so each request's own
    FRESHNESS check is judged against the same run-level instant. Omitting
    it does not skip freshness checking: every result's FRESHNESS check
    still fires and degrades to UNKNOWN (folded to FAIL).
    """
    if len(declared_request_ids) != required_count:
        raise ValueError(f"declared_request_ids must have exactly {required_count} entries, got {len(declared_request_ids)}")
    if len(set(declared_request_ids)) != len(declared_request_ids):
        raise FreshnessViolation("declared_request_ids contains duplicate request_id(s)")

    if ledger is not None:
        for rid in declared_request_ids:
            if rid in ledger.seen_request_ids:
                raise FreshnessViolation(f"request_id {rid!r} was already consumed by a prior evaluation run")
        if ledger.max_consumed_request_id is not None:
            for rid in declared_request_ids:
                if rid <= ledger.max_consumed_request_id:
                    raise FreshnessViolation(
                        f"request_id {rid!r} is not fresh/consecutive relative to prior run's "
                        f"max consumed id {ledger.max_consumed_request_id!r}"
                    )

    missing = [rid for rid in declared_request_ids if rid not in evidence_by_id]
    if missing:
        raise ValueError(f"no evidence supplied for declared request_id(s): {missing}")

    evidences = [dict(evidence_by_id[rid]) for rid in declared_request_ids]

    # Auto-populate cross-task-borrowing detection for any evidence that
    # didn't already declare an explicit cross_task_conflict (tests may set
    # one directly to exercise evaluate_dispatch in isolation).
    conflicts = detect_cross_task_borrowing(evidences)
    for ev in evidences:
        if "cross_task_conflict" not in ev:
            ev["cross_task_conflict"] = conflicts.get(ev.get("request_id"), {"found": False, "detail": ""})

    results = [
        evaluate_dispatch(
            ev,
            expected_project_id=expected_project_id,
            tick_seconds=tick_seconds,
            max_visibility_ticks=max_visibility_ticks,
            acceptance_run_started_at=acceptance_run_started_at,
        )
        for ev in evidences
    ]

    if ledger is not None:
        ledger.record(declared_request_ids)

    passing = [r for r in results if r.result == STATUS_PASS]
    if len(passing) == required_count:
        reason = f"{required_count}/{required_count} fresh consecutive dispatches PASS"
    else:
        failing = [r.request_id for r in results if r.result != STATUS_PASS]
        reason = f"not all {required_count} fresh consecutive dispatches PASS (failing: {failing})"

    return AcceptanceReport(results=results, required_count=required_count, reason=reason)


# ---------------------------------------------------------------------------
# Evidence collection against a real store. Kept separate from
# evaluate_dispatch so tests never need a store, GitHub, or Windows
# Scheduled Task state; only wired up here for a real (non-test) run.
# ---------------------------------------------------------------------------

def collect_evidence(
    store: Any,
    project_id: str,
    request_id: str,
    *,
    dashboard_probe: Optional[Any] = None,
    ingress_probe: Optional[Any] = None,
    manual_trigger_probe: Optional[Any] = None,
    acceptance_run_started_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Best-effort real-store evidence collection for one request_id.

    Resolution path: scan the `tasks` area of `store` for a Task whose
    `source_context` dict carries a matching `request_id`, then walk
    task -> command(s) -> execution -> session -> handoff via the task_id
    foreign keys already required by schema/{command,execution,session,
    handoff}.schema.json (see schema/*.schema.json on main).

    Any leg of this walk that not-yet-merged infrastructure would normally
    populate (e.g. a dedicated ingress-observed-at timestamp, or a manual-
    trigger marker) degrades to None/absent so `evaluate_dispatch` reports
    UNKNOWN instead of a fabricated PASS. `dashboard_probe`, `ingress_probe`,
    and `manual_trigger_probe`, if supplied, are callables the caller wires
    up to whatever the real Dashboard/ingress-log/audit-trail APIs are; this
    module does not assume a specific implementation of any of them.

    `timestamps["ingress_first_observed_at"]` returned here is the canonical
    freshness anchor `evaluate_dispatch()`'s FRESHNESS check compares against
    the caller's `acceptance_run_started_at` cutoff -- faithfully passed
    through from the store (or `ingress_probe`) rather than fabricated, so a
    genuinely-missing value correctly surfaces as FRESHNESS=UNKNOWN.

    If `acceptance_run_started_at` is supplied, this function also attaches
    an observability record at `evidence["freshness"]` (status/
    ingress_first_observed_at/acceptance_run_started_at/reason), computed by
    the same `_compute_freshness()` `evaluate_dispatch()` uses -- so the two
    can never disagree. `evaluate_dispatch()` does not read this field back
    as an authority; it always recomputes FRESHNESS itself from its own
    `acceptance_run_started_at` argument. This attached record exists so a
    human or downstream tool reading raw evidence JSON can see the
    freshness verdict without separately knowing which cutoff was used.
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
        return {
            "request_id": request_id,
            "project_id": project_id,
            "linkage": {"task": {"occurred": False}},
            "freshness": _compute_freshness(None, acceptance_run_started_at),
        }

    task = max(tasks, key=lambda t: t.get("created_at") or "")
    task_id = task.get("task_id")
    source_context = task.get("source_context") or {}

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

    status = task.get("status")
    is_blocked = status == "blocked"

    ingress_first_observed_at = source_context.get("ingress_first_observed_at")
    if ingress_first_observed_at is None and ingress_probe is not None:
        ingress_first_observed_at = ingress_probe(project_id, request_id)

    timestamps = {
        "request_created_at": source_context.get("request_created_at"),
        "ingress_first_observed_at": ingress_first_observed_at,
        "task_created_at": task.get("created_at"),
        "command_created_at": command.get("created_at") if command else None,
        "claimed_at": command.get("claimed_at") if command else None,
        "reserved_at": execution.get("reserved_at") if execution else None,
        "running_at": execution.get("started_at") if execution else None,
        "terminal_at": (execution.get("completed_at") or execution.get("finished_at")) if execution else None,
        "handoff_at": handoff.get("created_at") if handoff else None,
    }

    def _stage(occurred: bool, record_task_id: Optional[str]):
        if not occurred:
            return {"occurred": False}
        return {"occurred": True, "task_id_matches": (record_task_id == task_id) if record_task_id is not None else None}

    linkage = {
        "task": {"occurred": True, "task_id_matches": task.get("task_id") == task_id},
        "command": _stage(command is not None, command.get("task_id") if command else None),
        "execution": _stage(execution is not None, execution.get("task_id") if execution else None),
        "session": _stage(session is not None, session.get("task_id") if session else None),
        "handoff": _stage(handoff is not None, handoff.get("task_id") if handoff else None),
    }

    ids = {
        "task_id": task_id,
        "command_id": command.get("command_id") if command else None,
        "execution_id": execution.get("execution_id") if execution else None,
        "session_id": session.get("session_id") if session else None,
        "handoff_id": handoff.get("handoff_id") if handoff else None,
    }

    duplicate_counts = {
        "task": len(tasks),
        "command": len(commands),
        "execution": len(executions),
        "session": len(sessions),
        "handoff": len(handoffs),
    }

    reached_running = bool(execution and execution.get("started_at")) if not is_blocked else False
    provider_evidence = None
    if execution and execution.get("provider_evidence"):
        pe = execution["provider_evidence"]
        provider_evidence = {"present": bool(pe.get("pid")), "pid": pe.get("pid"), "host": pe.get("host")}

    manual_trigger_evidence = None
    if manual_trigger_probe is not None:
        manual_trigger_evidence = manual_trigger_probe(project_id, task_id)
    elif command is not None:
        marker_text = str(command.get("selection_reason", "")).lower()
        manual_trigger_evidence = {
            "found": "manual" in marker_text,
            "source": "command.selection_reason" if "manual" in marker_text else None,
        }

    backend_status = (status or "").upper() or None
    dashboard_status = None
    dashboard_observed_at = None
    if dashboard_probe is not None:
        probe_result = dashboard_probe(project_id, task_id) or {}
        dashboard_status = probe_result.get("status")
        dashboard_observed_at = probe_result.get("observed_at")

    backend_visibility = {"status": backend_status, "observed_at": task.get("created_at")} if backend_status else None
    user_visibility = {"status": dashboard_status, "observed_at": dashboard_observed_at} if dashboard_status else None

    terminal_state = None
    reason_code = None
    if is_blocked:
        terminal_state = "BLOCKED"
        reason_code = task.get("blocked_reason")
    elif status in ("cancelled",):
        terminal_state = "REJECTED"
    elif execution and execution.get("status") == "failed":
        terminal_state = "FAILED"
        reason_code = (execution.get("result") or {}).get("error_kind") if isinstance(execution.get("result"), dict) else None

    account_id = None
    if execution and execution.get("quota_evidence"):
        account_id = execution["quota_evidence"].get("account_id")

    return {
        "request_id": request_id,
        "project_id": project_id,
        "provider": command.get("provider") if command else None,
        "account_id": account_id,
        "timestamps": timestamps,
        "ids": ids,
        "backend_visibility": backend_visibility,
        "user_visibility": user_visibility,
        "backend_status": backend_status,
        "dashboard_status": dashboard_status,
        "linkage": linkage,
        "duplicate_counts": duplicate_counts,
        "manual_trigger_evidence": manual_trigger_evidence,
        "reached_running": reached_running,
        "real_provider_evidence": provider_evidence,
        "terminal": {"state": terminal_state, "reason_code": reason_code} if terminal_state else None,
        "freshness": _compute_freshness(ingress_first_observed_at, acceptance_run_started_at),
    }
