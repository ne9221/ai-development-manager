"""Core logic and ViewModels for ADM Operations Dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

from manager.quota_forecast import (
    AccountQuotaForecast,
    ActionRecommendation,
    DailyBriefForecast,
    QuotaWindowForecast,
    RiskStatus,
    WarningLevel,
    forecast_account,
    forecast_daily_brief,
    score_account_forecast,
)

# Terminal execution statuses in ADM
TERMINAL_EXECUTION_STATUSES = {"completed", "failed", "interrupted", "cancelled"}
NORMAL_MAX_AGE_SECONDS = 300
EXTENDED_RUNNING_MAX_AGE_SECONDS = 7800
FUTURE_CLOCK_SKEW_SECONDS = 15


@dataclass
class ServiceHealthViewModel:
    """UI ViewModel for a single infrastructure health check (Scheduled Task or HTTP service)."""
    name: str
    found: bool
    detail: str
    status_label: str  # "Online" | "Offline" | "Unknown"


def parse_scheduled_task_health(task_name: str, raw_schtasks_output: str | None) -> ServiceHealthViewModel:
    """Parse `schtasks /Query /TN <name> /FO LIST` stdout into a health viewmodel.

    A missing/unparseable result (task query failed, schtasks unavailable) is
    reported as Unknown rather than Offline -- we could not observe the real
    state, so we must not claim a definite answer either way.
    """
    if not raw_schtasks_output:
        return ServiceHealthViewModel(name=task_name, found=False, detail="query failed", status_label="Unknown")

    status = None
    task_state = None
    for line in raw_schtasks_output.splitlines():
        line = line.strip()
        if line.startswith("Status:") and status is None:
            status = line.split(":", 1)[1].strip()
        elif line.startswith("Scheduled Task State:") and task_state is None:
            task_state = line.split(":", 1)[1].strip()

    if status is None:
        return ServiceHealthViewModel(name=task_name, found=False, detail="not found", status_label="Unknown")

    enabled = task_state != "Disabled" if task_state is not None else True
    detail = f"{status}" + (f" / {task_state}" if task_state else "")
    status_label = "Offline" if not enabled else "Online"
    return ServiceHealthViewModel(name=task_name, found=True, detail=detail, status_label=status_label)


def build_session_center_health(listening: bool, session: dict | None) -> ServiceHealthViewModel:
    """Build a health viewmodel from a Session Center /health + /api/session probe result."""
    if not listening:
        return ServiceHealthViewModel(
            name="Session Center (HTTP :8765)",
            found=False,
            detail="not listening -- normal when no AI execution is currently active",
            status_label="Offline",
        )
    if session:
        detail = f"provider={session.get('provider', '—')}, state={session.get('current_state', '—')}"
    else:
        detail = "listening, but /api/session did not respond"
    return ServiceHealthViewModel(name="Session Center (HTTP :8765)", found=True, detail=detail, status_label="Online")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def is_cleanup_confirmed(execution: dict) -> bool:
    """True only when cleanup evidence proves the task claim and writer lease are released."""
    evidence = execution.get("cleanup_evidence")
    if not isinstance(evidence, dict) or evidence.get("task_claim_release") != "released":
        return False
    writer_release = evidence.get("writer_release")
    if execution.get("access") == "read_only":
        return writer_release in ("released", "not_required")
    return writer_release == "released"


def determine_execution_state(execution: dict, now: datetime) -> str:
    """Map execution record to the UI state:
    running, waiting, correlating, finishing, completed, failed, interrupted, cancelled.
    """
    status = execution.get("status")
    if not isinstance(status, str):
        return "unknown"

    if status in TERMINAL_EXECUTION_STATUSES:
        if is_cleanup_confirmed(execution):
            return status
        return "finishing"

    if status == "running":
        # Check if it has provider_session_id. If not, it is correlating
        if not execution.get("provider_session_id"):
            return "correlating"

        # Check heartbeat for waiting status
        provider = execution.get("provider", "").lower()
        if provider != "claude":
            heartbeat_str = execution.get("heartbeat_at")
            heartbeat = parse_time(heartbeat_str)
            if heartbeat:
                age_seconds = (now - heartbeat).total_seconds()
                if age_seconds >= 900:  # 15 minutes
                    return "waiting"
        return "running"

    return status


def is_execution_stale(execution: dict, now: datetime) -> bool:
    """An execution is stale if it is running/reserved and has no heartbeat for 15 minutes,
    or has exceeded its hard timeout.
    """
    status = execution.get("status")
    if status in TERMINAL_EXECUTION_STATUSES:
        return False

    # Check hard timeout
    hard_timeout_str = execution.get("hard_timeout_at")
    if hard_timeout_str:
        hard_timeout = parse_time(hard_timeout_str)
        if hard_timeout and now > hard_timeout:
            return True

    provider = execution.get("provider", "").lower()
    if provider == "claude":
        return False

    # Check heartbeat
    heartbeat_str = execution.get("heartbeat_at")
    if heartbeat_str:
        heartbeat = parse_time(heartbeat_str)
        if heartbeat:
            age_seconds = (now - heartbeat).total_seconds()
            if age_seconds >= 900:  # 15 minutes
                return True
    else:
        # If running/reserved but has no heartbeat at all, check start time or reserve time
        start_str = execution.get("started_at") or execution.get("reserved_at")
        start = parse_time(start_str)
        if start:
            age_seconds = (now - start).total_seconds()
            if age_seconds >= 900:
                return True

    return False


def get_global_summary(
    providers_summary: list,
    all_tasks: list,
    active_executions: list
) -> dict:
    """Compute dashboard global metrics."""
    # An in_progress Task without an active Execution is stale projection, not
    # proof that an AI is running now.
    running_tasks = len({(e.get("project_id"), e.get("task_id")) for e in active_executions})
    blocked_tasks = sum(1 for t in all_tasks if t.get("status") == "blocked")
    active_sessions = len(active_executions)

    reliable_providers = sum(1 for p in providers_summary if p.get("has_reliable_quota"))

    return {
        "running_tasks_count": running_tasks,
        "blocked_tasks_count": blocked_tasks,
        "active_sessions_count": active_sessions,
        "reliable_providers_count": reliable_providers
    }


def map_task_board(all_tasks: list, active_executions_dict: dict, now: datetime) -> dict:
    """Group tasks into Ready, In progress, Blocked / Attention, Completed."""
    board = {
        "Ready": [],
        "In progress": [],
        "Blocked / Attention": [],
        "Completed": []
    }

    for task in all_tasks:
        status = task.get("status")
        task_id = task.get("task_id")
        project_id = task.get("project_id")
        key = (project_id, task_id)

        # Check if linked to a stale execution
        linked_execution = active_executions_dict.get(key)
        execution_stale = False
        if linked_execution:
            execution_stale = is_execution_stale(linked_execution, now)

        if status == "blocked" or execution_stale:
            board["Blocked / Attention"].append(task)
        elif status in ("in_progress", "ready", "queued"):
            if status == "in_progress":
                board["In progress"].append(task)
            else:
                board["Ready"].append(task)
        elif status in ("completed", "cancelled"):
            board["Completed"].append(task)
        else:
            board["Ready"].append(task)

    return board


# =====================================================================
# Dashboard ViewModels for Quota & Daily Brief (Slice 4A)
# =====================================================================


def format_countdown(hours: Optional[float]) -> str:
    """Format hours to reset into human-friendly string."""
    if hours is None:
        return "—"
    if hours < 0:
        return "Past due"
    if hours < 1.0:
        mins = max(1, int(round(hours * 60)))
        return f"in {mins}m"
    if hours < 24.0:
        return f"in {hours:.1f}h"
    days = int(hours // 24)
    rem_h = int(round(hours % 24))
    if rem_h == 0:
        return f"in {days}d"
    return f"in {days}d {rem_h}h"


def format_percent(val: Optional[float]) -> str:
    """Format percentage value, strictly maintaining Unknown for None."""
    if val is None:
        return "Unknown"
    return f"{val:.1f}%"


def format_burn_rate(val: Optional[float]) -> str:
    """Format burn rate value."""
    if val is None:
        return "—"
    return f"{val:.1f}%/hr"


@dataclass
class AccountQuotaCardViewModel:
    """UI ViewModel for a single provider/account quota card."""
    provider: str
    account_id: Optional[str]
    display_name: str
    card_title: str
    status: str
    freshness: str
    stale: bool
    confidence: str
    source: str
    source_type: str
    has_reliable_quota: bool
    source_reliable: bool
    source_verified: bool
    dispatchable: bool
    last_updated: Optional[str]

    # Primary / 5-hour window
    five_hour_remaining_pct: Optional[float] = None
    five_hour_used_pct: Optional[float] = None
    five_hour_resets_at: Optional[str] = None
    five_hour_hours_to_reset: Optional[float] = None
    five_hour_burn_rate: Optional[float] = None
    five_hour_burn_samples: int = 0
    five_hour_projected_remaining: Optional[float] = None
    five_hour_warning_level: str = "UNKNOWN"
    five_hour_risk_status: str = "unknown"
    five_hour_action_recommendation: str = "hold"
    five_hour_warning_reason: str = ""

    # Secondary / Weekly window
    has_weekly_window: bool = False
    weekly_remaining_pct: Optional[float] = None
    weekly_used_pct: Optional[float] = None
    weekly_resets_at: Optional[str] = None
    weekly_hours_to_reset: Optional[float] = None
    weekly_burn_rate: Optional[float] = None
    weekly_projected_remaining: Optional[float] = None
    weekly_warning_level: str = "UNKNOWN"
    weekly_risk_status: str = "unknown"
    weekly_action_recommendation: str = "hold"
    weekly_warning_reason: str = ""

    # Overall assessments
    overall_warning: str = "UNKNOWN"
    overall_risk: str = "unknown"
    action_recommendation: str = "hold"
    warning_reason: str = ""

    # Extra credits (e.g. Codex pay-as-you-go balance) -- a pool distinct from
    # the subscription quota windows above.
    extra_credits_available: Optional[bool] = None
    extra_credits_balance: Optional[str] = None

    # Truthful summary of whether the provider can actually be dispatched to
    # right now: "available" | "available_via_credits" | "unavailable" | "unknown"
    effective_availability: str = "unknown"

    # Formatted display strings
    formatted_five_hour_remaining: str = "Unknown"
    formatted_weekly_remaining: str = "—"
    formatted_five_hour_countdown: str = "—"
    formatted_weekly_countdown: str = "—"
    formatted_five_hour_burn_rate: str = "—"
    formatted_five_hour_projected: str = "—"
    formatted_extra_credits: str = "—"
    formatted_effective_availability: str = "Unknown"


@dataclass
class DailyBriefViewModel:
    """UI ViewModel for top Daily Brief & Recommendation banner."""
    generated_at: str
    recommended_provider: Optional[str] = None
    recommended_account: Optional[str] = None
    recommended_display_name: str = "None"
    recommended_action: str = "hold"  # "consume", "normal", "conserve", "hold"
    reason: str = ""
    unsafe_accounts: List[AccountQuotaCardViewModel] = field(default_factory=list)
    nearest_reset: Optional[str] = None
    nearest_reset_countdown: str = "—"
    telemetry_warnings: List[str] = field(default_factory=list)
    summary_counts: Dict[str, int] = field(default_factory=dict)
    accounts: List[AccountQuotaCardViewModel] = field(default_factory=list)


def build_account_quota_card_vm(fc: AccountQuotaForecast) -> AccountQuotaCardViewModel:
    """Build a UI ViewModel for a single account from its AccountQuotaForecast."""
    account_label = f" (Account {fc.account_id})" if fc.account_id else ""
    card_title = f"{fc.display_name}{account_label}" if fc.account_id and fc.account_id not in fc.display_name else fc.display_name

    # Find 5-hour / primary window
    w5 = None
    for name in ("five_hour", "primary"):
        for w in fc.windows:
            if w.window_name == name:
                w5 = w
                break
        if w5:
            break
    if w5 is None and fc.primary_window:
        w5 = fc.primary_window
    elif w5 is None and fc.windows:
        w5 = fc.windows[0]

    # Find weekly / 7-day window
    w_week = None
    for name in ("seven_day", "weekly"):
        for w in fc.windows:
            if w.window_name == name:
                w_week = w
                break
        if w_week:
            break

    # 5-hour metrics
    f5_rem = w5.remaining_percent if w5 else None
    f5_used = w5.used_percent if w5 else None
    f5_resets = w5.resets_at if w5 else None
    f5_h_reset = w5.hours_to_reset if w5 else None
    f5_burn = w5.burn_rate_pct_per_hour if w5 else None
    f5_samples = w5.burn_rate_samples if w5 else 0
    f5_proj = w5.estimated_remaining_at_reset if w5 else None
    f5_warn = w5.warning_level.value if w5 and hasattr(w5.warning_level, "value") else str(w5.warning_level if w5 else "UNKNOWN")
    f5_risk = w5.risk_status.value if w5 and hasattr(w5.risk_status, "value") else str(w5.risk_status if w5 else "unknown")
    f5_act = w5.action_recommendation.value if w5 and hasattr(w5.action_recommendation, "value") else str(w5.action_recommendation if w5 else "hold")
    f5_reason = w5.warning_reason if w5 else ""

    # Weekly metrics
    has_week = w_week is not None
    w_rem = w_week.remaining_percent if w_week else None
    w_used = w_week.used_percent if w_week else None
    w_resets = w_week.resets_at if w_week else None
    w_h_reset = w_week.hours_to_reset if w_week else None
    w_burn = w_week.burn_rate_pct_per_hour if w_week else None
    w_proj = w_week.estimated_remaining_at_reset if w_week else None
    w_warn = w_week.warning_level.value if w_week and hasattr(w_week.warning_level, "value") else str(w_week.warning_level if w_week else "UNKNOWN")
    w_risk = w_week.risk_status.value if w_week and hasattr(w_week.risk_status, "value") else str(w_week.risk_status if w_week else "unknown")
    w_act = w_week.action_recommendation.value if w_week and hasattr(w_week.action_recommendation, "value") else str(w_week.action_recommendation if w_week else "hold")
    w_reason = w_week.warning_reason if w_week else ""

    overall_warn_str = fc.overall_warning_level.value if hasattr(fc.overall_warning_level, "value") else str(fc.overall_warning_level)
    overall_risk_str = fc.overall_risk_status.value if hasattr(fc.overall_risk_status, "value") else str(fc.overall_risk_status)
    overall_act_str = fc.overall_action_recommendation.value if hasattr(fc.overall_action_recommendation, "value") else str(fc.overall_action_recommendation)

    # Truthful effective availability: never collapse "primary quota exhausted"
    # into "unavailable" when extra credits make the provider still usable,
    # and never invent an answer when telemetry is stale/unknown.
    if fc.stale:
        effective_availability = "unknown"
    elif overall_risk_str == "available_via_credits":
        effective_availability = "available_via_credits"
    elif fc.dispatchable:
        effective_availability = "available"
    else:
        effective_availability = "unavailable"

    formatted_effective_availability = {
        "unknown": "Unknown / Stale",
        "available_via_credits": "Available via credits",
        "available": "Available",
        "unavailable": "Unavailable",
    }[effective_availability]

    if fc.extra_credits_available is True:
        formatted_extra_credits = (
            f"Available (balance: {fc.extra_credits_balance})" if fc.extra_credits_balance else "Available"
        )
    elif fc.extra_credits_available is False:
        formatted_extra_credits = "Not available"
    else:
        formatted_extra_credits = "—"

    return AccountQuotaCardViewModel(
        provider=fc.provider,
        account_id=fc.account_id,
        display_name=fc.display_name,
        card_title=card_title,
        status=fc.status,
        freshness=fc.freshness,
        stale=fc.stale,
        confidence=fc.confidence,
        source=fc.source,
        source_type=fc.source_type,
        has_reliable_quota=fc.has_reliable_quota,
        source_reliable=fc.source_reliable,
        source_verified=fc.source_verified,
        dispatchable=fc.dispatchable,
        last_updated=fc.last_updated,
        five_hour_remaining_pct=f5_rem,
        five_hour_used_pct=f5_used,
        five_hour_resets_at=f5_resets,
        five_hour_hours_to_reset=f5_h_reset,
        five_hour_burn_rate=f5_burn,
        five_hour_burn_samples=f5_samples,
        five_hour_projected_remaining=f5_proj,
        five_hour_warning_level=f5_warn,
        five_hour_risk_status=f5_risk,
        five_hour_action_recommendation=f5_act,
        five_hour_warning_reason=f5_reason,
        has_weekly_window=has_week,
        weekly_remaining_pct=w_rem,
        weekly_used_pct=w_used,
        weekly_resets_at=w_resets,
        weekly_hours_to_reset=w_h_reset,
        weekly_burn_rate=w_burn,
        weekly_projected_remaining=w_proj,
        weekly_warning_level=w_warn,
        weekly_risk_status=w_risk,
        weekly_action_recommendation=w_act,
        weekly_warning_reason=w_reason,
        overall_warning=overall_warn_str,
        overall_risk=overall_risk_str,
        action_recommendation=overall_act_str,
        warning_reason=fc.overall_warning_reason,
        extra_credits_available=fc.extra_credits_available,
        extra_credits_balance=fc.extra_credits_balance,
        effective_availability=effective_availability,
        formatted_five_hour_remaining=format_percent(f5_rem),
        formatted_weekly_remaining=format_percent(w_rem) if has_week else "—",
        formatted_five_hour_countdown=format_countdown(f5_h_reset),
        formatted_weekly_countdown=format_countdown(w_h_reset) if has_week else "—",
        formatted_five_hour_burn_rate=format_burn_rate(f5_burn),
        formatted_five_hour_projected=format_percent(f5_proj) if f5_proj is not None else "—",
        formatted_extra_credits=formatted_extra_credits,
        formatted_effective_availability=formatted_effective_availability,
    )


def build_daily_brief_vm(
    current_doc_or_summary: Any,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    max_age_minutes: float = 60.0,
) -> DailyBriefViewModel:
    """Build the complete Daily Brief ViewModel from status document and history."""
    now = now or datetime.now(timezone.utc)

    # Normalize input candidates
    normalized_input = current_doc_or_summary or {}
    if isinstance(normalized_input, dict):
        if not normalized_input.get("accounts") and normalized_input.get("providers"):
            normalized_input = normalized_input.get("providers")
        elif normalized_input.get("accounts"):
            normalized_input = normalized_input.get("accounts")

    # Fail-safe extraction
    try:
        brief_fc: DailyBriefForecast = forecast_daily_brief(
            normalized_input,
            history=history,
            now=now,
            max_age_minutes=max_age_minutes,
        )
    except Exception as exc:
        # Fallback closed on forecast exception
        return DailyBriefViewModel(
            generated_at=now.isoformat(),
            recommended_provider=None,
            recommended_account=None,
            recommended_display_name="Unavailable",
            recommended_action="hold",
            reason=f"Quota forecast calculation error: {exc}",
            telemetry_warnings=[f"Forecast exception: {exc}"],
        )

    account_vms = [build_account_quota_card_vm(fc) for fc in brief_fc.accounts]

    # Find unsafe accounts (stale, hold, conserve, exhausted)
    unsafe = [
        vm for vm in account_vms
        if vm.stale
        or not vm.dispatchable
        or vm.action_recommendation in ("hold", "conserve")
        or vm.overall_risk in ("exhausted", "likely_exhaust_before_reset")
    ]

    # Identify nearest reset across all accounts and windows
    min_reset_hours: Optional[float] = None
    min_reset_ts: Optional[str] = None
    for fc in brief_fc.accounts:
        for w in fc.windows:
            if w.hours_to_reset is not None and w.hours_to_reset > 0:
                if min_reset_hours is None or w.hours_to_reset < min_reset_hours:
                    min_reset_hours = w.hours_to_reset
                    min_reset_ts = w.resets_at

    nearest_countdown = format_countdown(min_reset_hours)

    # Collect telemetry warnings
    warnings: List[str] = []
    for vm in account_vms:
        if vm.stale:
            warnings.append(f"{vm.card_title} telemetry is STALE (last updated: {vm.last_updated or 'never'})")
        elif not vm.source_reliable:
            warnings.append(f"{vm.card_title} quota source is unverified / manual ({vm.source})")
        elif vm.five_hour_remaining_pct is None:
            warnings.append(f"{vm.card_title} has no remaining percentage telemetry reported")

    # Score and determine top recommendation
    scored_candidates = []
    for fc, vm in zip(brief_fc.accounts, account_vms):
        score_tuple = score_account_forecast(fc)
        scored_candidates.append((score_tuple, fc, vm))

    eligible = [c for c in scored_candidates if c[0][0]]  # is_eligible is True

    if not eligible:
        rec_provider = None
        rec_account = None
        rec_display = "No AI Available"
        rec_action = "hold"
        rec_reason = "No dispatchable AI accounts available (all accounts are stale, unconfigured, or quota-exhausted)."
    else:
        # Sort descending by score tuple: (is_eligible, action_tier, remaining_percent, reset_urgency, account_id)
        eligible.sort(key=lambda c: c[0], reverse=True)
        best_score, best_fc, best_vm = eligible[0]

        rec_provider = best_vm.provider
        rec_account = best_vm.account_id
        rec_display = best_vm.card_title

        # Map action
        act = best_fc.overall_action_recommendation
        if act in (ActionRecommendation.URGENT_CONSUME, ActionRecommendation.SUGGEST_CONSUME):
            rec_action = "consume"
        elif act == ActionRecommendation.CONSERVE:
            rec_action = "conserve"
        elif act == ActionRecommendation.NORMAL_USE:
            rec_action = "normal"
        else:
            rec_action = "hold"

        # Build comprehensive reason
        reason_parts = []
        rem_str = f"{best_vm.five_hour_remaining_pct:.0f}%" if best_vm.five_hour_remaining_pct is not None else "healthy"
        reset_str = best_vm.formatted_five_hour_countdown

        if rec_action == "consume":
            reason_parts.append(f"fresh surplus quota ({rem_str} remaining, resets {reset_str})")
        elif rec_action == "normal" and best_vm.effective_availability == "available_via_credits":
            balance_note = f" (balance: {best_vm.extra_credits_balance})" if best_vm.extra_credits_balance else ""
            reason_parts.append(f"primary quota exhausted (0% remaining) but extra credits available{balance_note}")
        elif rec_action == "normal":
            reason_parts.append(f"fresh quota ({rem_str} remaining, resets {reset_str})")
        elif rec_action == "conserve":
            reason_parts.append(f"quota conservation required ({rem_str} remaining)")
        else:
            reason_parts.append("eligible for general tasks")

        # Contextual notes about other accounts (e.g. Claude account comparison or secondary window protection)
        conserve_others = [
            vm for vm in account_vms
            if vm is not best_vm
            and vm.provider == best_vm.provider
            and (vm.action_recommendation == "conserve" or vm.overall_risk == "likely_exhaust_before_reset")
        ]
        if conserve_others:
            other_names = ", ".join(o.card_title for o in conserve_others)
            reason_parts.append(f"{other_names} weekly quota should be conserved")
        elif best_vm.has_weekly_window and best_vm.weekly_remaining_pct is not None:
            reason_parts.append(f"weekly quota at {best_vm.formatted_weekly_remaining}")

        rec_reason = "; ".join(reason_parts)
        if best_vm.warning_reason and rec_action != "normal":
            rec_reason += f" ({best_vm.warning_reason})"

    return DailyBriefViewModel(
        generated_at=brief_fc.generated_at,
        recommended_provider=rec_provider,
        recommended_account=rec_account,
        recommended_display_name=rec_display,
        recommended_action=rec_action,
        reason=rec_reason,
        unsafe_accounts=unsafe,
        nearest_reset=min_reset_ts,
        nearest_reset_countdown=nearest_countdown,
        telemetry_warnings=warnings,
        summary_counts=brief_fc.summary_counts,
        accounts=account_vms,
    )


# =====================================================================
# Visible Dispatch Truth Gate (Task / Provider / Account / Quota truth)
# =====================================================================
#
# This section builds a single, honest, per-task row combining Task +
# Command + Execution + per-account Quota truth for the Dashboard's main
# view. It never fabricates a value: anything not provable from a real
# SSOT record is surfaced as the literal string "UNKNOWN" (or "STALE" for
# quota freshness), never inferred or borrowed from another record.

UNKNOWN_LABEL = "UNKNOWN"

DISPATCH_STATE_SUBMITTED = "SUBMITTED"
DISPATCH_STATE_ACCEPTED = "ACCEPTED"
DISPATCH_STATE_QUEUED = "QUEUED"
DISPATCH_STATE_CLAIMED = "CLAIMED"
DISPATCH_STATE_RUNNING = "RUNNING"
DISPATCH_STATE_COMPLETED = "COMPLETED"
DISPATCH_STATE_FAILED = "FAILED"
DISPATCH_STATE_BLOCKED = "BLOCKED"
DISPATCH_STATE_CANCELLED = "CANCELLED"
DISPATCH_STATE_REJECTED = "REJECTED"
DISPATCH_STATE_UNKNOWN = "UNKNOWN"

# Maps manager.dashboard_core.determine_execution_state()'s execution-level
# vocabulary onto the Dispatch Truth state a *terminal* execution proves,
# for the (unusual) case where an execution has already reached a terminal
# outcome while its parent command record still says "running". Only
# reached from compute_dispatch_state() below; "interrupted" is truthfully
# a failure outcome from the dispatch-visibility standpoint, not its own
# 10th state the task spec did not ask for.
_TERMINAL_EXECUTION_STATE_TO_DISPATCH_STATE = {
    "completed": DISPATCH_STATE_COMPLETED,
    "failed": DISPATCH_STATE_FAILED,
    "interrupted": DISPATCH_STATE_FAILED,
    "cancelled": DISPATCH_STATE_CANCELLED,
}


def _dispatch_request_state(dispatch_request_status: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Maps a manager.dispatch_requests.read_dispatch_request_status()/
    read_dispatch_rejection_status() result onto a Dispatch Truth
    state+reason, or None if no such evidence is available at all (the
    caller then falls back to `has_dispatch_request`/SUBMITTED/UNKNOWN
    exactly as before this existed). "accepted"/"dispatched" both mean
    ACCEPTED here -- both are real evidence the request was received before
    any Task/Command was observed; "dispatched" additionally means Task/
    Command creation itself already succeeded once, so if this is still
    being consulted (no Task/Command found), something else must have
    removed them, which is out of scope to second-guess here. "failed" and
    "rejected" are kept as distinct terminal states (FAILED = a claimed
    ingress request whose Task/Command creation itself failed; REJECTED = a
    request that never even reached the claim stage, e.g. malformed/
    unverifiable) so a caller can tell which stage the request died at.
    """
    if not isinstance(dispatch_request_status, dict):
        return None
    status = dispatch_request_status.get("status")
    reason = dispatch_request_status.get("failure_reason") or dispatch_request_status.get("reason_code")
    if status in ("accepted", "dispatched"):
        return {"state": DISPATCH_STATE_ACCEPTED, "reason": "dispatch request accepted; no task record found yet"}
    if status == "failed":
        return {"state": DISPATCH_STATE_FAILED, "reason": reason or "ingress request failed before a task was created"}
    if status == "rejected":
        return {"state": DISPATCH_STATE_REJECTED, "reason": reason or "ingress request rejected before it was claimed"}
    return None


def compute_dispatch_state(
    task: Optional[Dict[str, Any]],
    command: Optional[Dict[str, Any]],
    execution: Optional[Dict[str, Any]],
    now: datetime,
    has_dispatch_request: bool = False,
    dispatch_request_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Truthfully classify one task's dispatch state.

    =====================================================================
    Two-Tick Visibility SLA -- formal contract (SLA_START_POINT)
    =====================================================================
    SLA_START_POINT = the moment ingress first successfully observes a
    valid request file -- NOT the file's own created-at timestamp, since
    the system cannot guarantee file-created -> observed-by-scheduler
    ordering before the scheduler has actually run (a file can be created
    long before any scheduler tick looks at it; measuring from creation
    would make the SLA unmeetable by construction on a cold/backlogged
    queue, and would blame ingress for OS/Drive propagation delay that
    ingress has no control over).

    Formal definition: from the first successful ingress observation of
    the request, within at most 2 normal scheduler ticks, the ADM
    user-visible interface (this Dashboard, via the functions in this
    module) must show one of: ACCEPTED, QUEUED, RUNNING, BLOCKED,
    REJECTED, FAILED (i.e. never nothing at all, and never a state that
    understates progress -- see compute_dispatch_state()'s state-priority
    rules below for what "never nothing at all" requires: a request with
    NO Task record yet must still resolve to a real state via
    `dispatch_request_status`/`has_dispatch_request`, not silently vanish).

    `request_created_at` (the request file's own created_at field, as
    written by whatever produced it) should still be recorded/available
    wherever a request's Drive/GCS record already carries it (e.g.
    manager.dispatch_requests's claim record `created_at` field) -- it is
    useful for measuring file-created -> first-observed latency as a
    SEPARATE diagnostic metric. It is explicitly NOT the 2-tick execution
    SLA's starting point; do not conflate the two when reasoning about
    whether the 2-tick SLA was met.
    =====================================================================

    Never reports SUBMITTED/ACCEPTED/QUEUED/CLAIMED as RUNNING: RUNNING is
    only reached when the *command* says running AND
    determine_execution_state() independently proves the execution is
    running (status == "running" with provider session evidence) --
    reusing that existing, already-tested distinction rather than
    duplicating it. `has_dispatch_request` is real evidence (a
    dispatch-requests/*.json idempotency record exists at all) that ingress
    accepted the request before a Task/Command was observed; without it,
    ACCEPTED is never guessed as a phase distinct from SUBMITTED, because
    the current schema has no independent signal for it. `dispatch_request_
    status` (manager.dispatch_requests.read_dispatch_request_status()'s own
    result, or an equivalent rejection-record lookup) is strictly richer
    evidence when available -- it additionally distinguishes FAILED/REJECTED
    with an exact reason, not just "was accepted, yes/no" -- and takes
    priority over the plain boolean when both are given.

    Critically, this evidence is now consulted even when NO Task record
    exists at all (see the `not isinstance(task, dict)` branch below): a
    request that was durably accepted/failed/rejected by ingress before any
    Task was ever created must never report UNKNOWN just because the Task
    lookup itself came back empty -- that was this function's own gap in
    the request -> Task visibility window this whole task exists to close.
    """
    if not isinstance(task, dict):
        request_state = _dispatch_request_state(dispatch_request_status)
        if request_state is not None:
            return request_state
        if has_dispatch_request:
            return {"state": DISPATCH_STATE_ACCEPTED, "reason": "dispatch request accepted; no task record found yet"}
        return {"state": DISPATCH_STATE_UNKNOWN, "reason": "no task record found"}

    task_status = task.get("status")
    if task_status == "blocked":
        return {"state": DISPATCH_STATE_BLOCKED, "reason": task.get("blocked_reason") or "task marked blocked"}

    if not isinstance(command, dict):
        if task_status == "completed":
            return {"state": DISPATCH_STATE_COMPLETED, "reason": "task completed with no command record"}
        if task_status == "cancelled":
            return {"state": DISPATCH_STATE_CANCELLED, "reason": "task cancelled with no command record"}
        request_state = _dispatch_request_state(dispatch_request_status)
        if request_state is not None and request_state["state"] in (DISPATCH_STATE_ACCEPTED, DISPATCH_STATE_FAILED):
            return request_state
        if has_dispatch_request:
            return {"state": DISPATCH_STATE_ACCEPTED, "reason": "dispatch request accepted; no command observed yet"}
        return {"state": DISPATCH_STATE_SUBMITTED, "reason": "task created, not yet dispatched to a command"}

    cmd_status = command.get("status")
    if cmd_status == "queued":
        return {"state": DISPATCH_STATE_QUEUED, "reason": "command queued, not yet claimed"}
    if cmd_status == "claimed":
        return {"state": DISPATCH_STATE_CLAIMED, "reason": "command claimed, launch not yet proven running"}
    if cmd_status == "attention":
        return {"state": DISPATCH_STATE_BLOCKED, "reason": command.get("recovery_reason") or "command requires attention"}
    if cmd_status == "completed":
        return {"state": DISPATCH_STATE_COMPLETED, "reason": "command completed"}
    if cmd_status == "failed":
        return {"state": DISPATCH_STATE_FAILED, "reason": "command failed"}
    if cmd_status == "running":
        if not isinstance(execution, dict):
            return {"state": DISPATCH_STATE_CLAIMED, "reason": "command reports running but no execution record found (not proven running)"}
        if execution.get("task_id") != task.get("task_id") or execution.get("project_id") != task.get("project_id"):
            return {"state": DISPATCH_STATE_CLAIMED, "reason": "execution record does not match this task's (project_id, task_id) -- linkage mismatch, not proven running"}
        exec_state = determine_execution_state(execution, now)
        if exec_state == "running":
            evidence = execution.get("provider_session_id") or "provider session evidence present"
            return {"state": DISPATCH_STATE_RUNNING, "reason": f"execution running with provider session evidence ({evidence})"}
        if exec_state in ("correlating", "waiting", "finishing"):
            return {"state": DISPATCH_STATE_CLAIMED, "reason": f"command running but execution state={exec_state} (provider session evidence not yet proven)"}
        mapped = _TERMINAL_EXECUTION_STATE_TO_DISPATCH_STATE.get(exec_state)
        if mapped:
            return {"state": mapped, "reason": f"execution reached terminal state '{exec_state}'"}
        return {"state": DISPATCH_STATE_UNKNOWN, "reason": f"command running but execution state unrecognized ({exec_state!r})"}
    return {"state": DISPATCH_STATE_UNKNOWN, "reason": f"unrecognized command status: {cmd_status!r}"}


# Real, callable implementation of the Two-Tick Visibility SLA formally
# documented on compute_dispatch_state() above (SLA_START_POINT) -- prior to
# this, that contract existed only as a docstring; nothing computed
# visibility_elapsed or a PASS/FAIL verdict from it. See
# evaluate_two_tick_visibility_sla()'s own docstring.
TWO_TICK_SLA_TICK_COUNT = 2


def _default_scheduler_tick_seconds() -> float:
    """The real scheduler cadence contract (manager.command_watcher.
    POLL_SECONDS), read live rather than duplicated as a separate,
    potentially-inconsistent constant here. Deferred import: manager.
    command_watcher pulls in the full provider-launcher stack (AgRunner,
    ClaudeLauncher, CodexLauncher, execution_runner, ...) -- this module is
    imported at Streamlit dashboard.py startup and must not be forced to
    load that entire stack just to read one integer."""
    from manager.command_watcher import POLL_SECONDS
    return float(POLL_SECONDS)


def evaluate_two_tick_visibility_sla(
    dispatch_request_status: Optional[Dict[str, Any]],
    now: datetime,
    tick_seconds: Optional[float] = None,
    ticks: int = TWO_TICK_SLA_TICK_COUNT,
) -> Dict[str, Any]:
    """Real evaluator for the Two-Tick Visibility SLA (see
    compute_dispatch_state()'s SLA_START_POINT contract above) -- computes
    `visibility_elapsed_seconds` from the durable `ingress_first_observed_at`
    evidence (manager.dispatch_requests.claim_dispatch_request()/
    read_dispatch_request_status()) and reports PASS/FAIL against `ticks`
    normal scheduler ticks (default `tick_seconds` is the real
    manager.command_watcher.POLL_SECONDS cadence, never a separately
    hardcoded duplicate -- pass `tick_seconds` explicitly only to test
    against a different cadence).

    Deliberately reads `ingress_first_observed_at`, never
    `request_created_at`/`created_at` from the request's own body -- per the
    SLA_START_POINT contract, measuring from file-created time would blame
    ingress for OS/Drive propagation delay it has no control over.

    Returns {"result": "PASS"|"FAIL"|"UNKNOWN", "visibility_elapsed_seconds":
    float|None, "sla_seconds": float, "reason": str}.

    UNKNOWN (never a guessed PASS or FAIL) when `dispatch_request_status`
    itself is missing, or its `ingress_first_observed_at` is missing/
    malformed/future-dated -- an acceptance harness reading this directly
    must never fabricate a verdict for a request ingress never durably
    proved it observed.
    """
    if tick_seconds is None:
        tick_seconds = _default_scheduler_tick_seconds()
    sla_seconds = float(tick_seconds) * int(ticks)
    if not isinstance(dispatch_request_status, dict):
        return {"result": "UNKNOWN", "visibility_elapsed_seconds": None, "sla_seconds": sla_seconds,
                "reason": "no dispatch request status evidence available"}
    observed_raw = dispatch_request_status.get("ingress_first_observed_at")
    if not isinstance(observed_raw, str) or not observed_raw.strip():
        return {"result": "UNKNOWN", "visibility_elapsed_seconds": None, "sla_seconds": sla_seconds,
                "reason": "ingress_first_observed_at is missing; visibility elapsed cannot be truthfully computed"}
    try:
        observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return {"result": "UNKNOWN", "visibility_elapsed_seconds": None, "sla_seconds": sla_seconds,
                "reason": "ingress_first_observed_at is not a valid ISO-8601 timestamp"}
    elapsed = (now.astimezone(timezone.utc) - observed).total_seconds()
    if elapsed < 0:
        return {"result": "UNKNOWN", "visibility_elapsed_seconds": elapsed, "sla_seconds": sla_seconds,
                "reason": "ingress_first_observed_at is in the future relative to now"}
    result = "PASS" if elapsed <= sla_seconds else "FAIL"
    return {"result": result, "visibility_elapsed_seconds": elapsed, "sla_seconds": sla_seconds,
            "reason": f"visible {elapsed:.1f}s after first ingress observation (SLA {sla_seconds:.1f}s)"}


def build_provider_truth(command: Optional[Dict[str, Any]], execution: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """provider/account_id/model/mode from the most authoritative record
    available (command, then execution); any missing/blank field is the
    literal "UNKNOWN" string, never a guess or another record's value."""
    source = command if isinstance(command, dict) else (execution if isinstance(execution, dict) else {})

    def _val(key: str) -> str:
        value = source.get(key)
        return value if isinstance(value, str) and value.strip() else UNKNOWN_LABEL

    return {
        "provider": _val("provider"),
        "account_id": _val("account_id"),
        "model": _val("model"),
        "mode": _val("mode"),
    }


def find_account_quota_vm(
    account_vms: Sequence[AccountQuotaCardViewModel], provider: Optional[str], account_id: Optional[str]
) -> Optional[AccountQuotaCardViewModel]:
    """Exact (provider, account_id) match only -- never falls back to a
    provider-level representative, so Claude A and Claude B never share a
    quota card by accident."""
    for vm in account_vms:
        if vm.provider == provider and vm.account_id == account_id:
            return vm
    return None


def build_quota_truth(
    account_vms: Sequence[AccountQuotaCardViewModel], provider: str, account_id: str
) -> Dict[str, Any]:
    """5h + weekly used/remaining/reset_at, captured_at, and freshness for
    one exact (provider, account_id). No captured entry -> every field is
    explicitly "UNKNOWN", never borrowed from another account or provider."""
    unknown_row = {
        "found": False,
        "five_hour_used_pct": None, "five_hour_remaining_pct": None, "five_hour_reset_at": None,
        "weekly_used_pct": None, "weekly_remaining_pct": None, "weekly_reset_at": None,
        "captured_at": None, "freshness": UNKNOWN_LABEL,
        "formatted_five_hour_used": UNKNOWN_LABEL, "formatted_five_hour_remaining": UNKNOWN_LABEL,
        "formatted_five_hour_reset_at": UNKNOWN_LABEL,
        "formatted_weekly_used": UNKNOWN_LABEL, "formatted_weekly_remaining": UNKNOWN_LABEL,
        "formatted_weekly_reset_at": UNKNOWN_LABEL,
        "formatted_captured_at": UNKNOWN_LABEL,
    }
    if provider == UNKNOWN_LABEL or account_id == UNKNOWN_LABEL:
        return unknown_row

    vm = find_account_quota_vm(account_vms, provider, account_id)
    if vm is None:
        return unknown_row

    freshness = "STALE" if vm.stale else "fresh"
    return {
        "found": True,
        "five_hour_used_pct": vm.five_hour_used_pct,
        "five_hour_remaining_pct": vm.five_hour_remaining_pct,
        "five_hour_reset_at": vm.five_hour_resets_at,
        "weekly_used_pct": vm.weekly_used_pct if vm.has_weekly_window else None,
        "weekly_remaining_pct": vm.weekly_remaining_pct if vm.has_weekly_window else None,
        "weekly_reset_at": vm.weekly_resets_at if vm.has_weekly_window else None,
        "captured_at": vm.last_updated,
        "freshness": freshness,
        "formatted_five_hour_used": format_percent(vm.five_hour_used_pct),
        "formatted_five_hour_remaining": vm.formatted_five_hour_remaining,
        "formatted_five_hour_reset_at": vm.five_hour_resets_at or UNKNOWN_LABEL,
        "formatted_weekly_used": format_percent(vm.weekly_used_pct) if vm.has_weekly_window else UNKNOWN_LABEL,
        "formatted_weekly_remaining": vm.formatted_weekly_remaining if vm.has_weekly_window else UNKNOWN_LABEL,
        "formatted_weekly_reset_at": (vm.weekly_resets_at or UNKNOWN_LABEL) if vm.has_weekly_window else UNKNOWN_LABEL,
        "formatted_captured_at": vm.last_updated or UNKNOWN_LABEL,
    }


def build_dispatch_truth_row(
    project: Optional[Dict[str, Any]],
    task: Dict[str, Any],
    command: Optional[Dict[str, Any]],
    execution: Optional[Dict[str, Any]],
    account_vms: Sequence[AccountQuotaCardViewModel],
    now: datetime,
    has_dispatch_request: bool = False,
    dispatch_request_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One user-visible truth row: Project/Task/Provider/Account/Model/Mode/
    Dispatch State/Execution/Session, plus that account's Quota truth."""
    dispatch = compute_dispatch_state(task, command, execution, now, has_dispatch_request=has_dispatch_request,
                                      dispatch_request_status=dispatch_request_status)
    provider_truth = build_provider_truth(command, execution)
    quota = build_quota_truth(account_vms, provider_truth["provider"], provider_truth["account_id"])

    execution_id = execution.get("execution_id") if isinstance(execution, dict) else None
    session_id = None
    if isinstance(execution, dict):
        session_id = execution.get("provider_session_id") or execution.get("session_id") or execution.get("conversation_id")

    project_id = task.get("project_id") or UNKNOWN_LABEL
    project_name = (project.get("name") if isinstance(project, dict) else None) or project_id

    return {
        "project_id": project_id,
        "project_name": project_name,
        "task_id": task.get("task_id") or UNKNOWN_LABEL,
        "task_title": task.get("title") or UNKNOWN_LABEL,
        "provider": provider_truth["provider"],
        "account_id": provider_truth["account_id"],
        "model": provider_truth["model"],
        "mode": provider_truth["mode"],
        "dispatch_state": dispatch["state"],
        "dispatch_reason": dispatch["reason"],
        "execution_id": execution_id or UNKNOWN_LABEL,
        "session_id": session_id or UNKNOWN_LABEL,
        "quota": quota,
    }


def build_pretask_dispatch_truth_row(
    project: Optional[Dict[str, Any]],
    project_id: str,
    request_id: str,
    dispatch_request_status: Optional[Dict[str, Any]],
    account_vms: Sequence[AccountQuotaCardViewModel],
    now: datetime,
    dispatch_request_read_failed: bool = False,
) -> Dict[str, Any]:
    """Same output shape/contract as build_dispatch_truth_row(), for a
    dispatch request ingress has observed BEFORE any Task record exists yet
    (VISIBLE_BEFORE_TASK: this is the P0 gap that made an ACCEPTED/
    REJECTED/FAILED request invisible in the real Dashboard when no Task
    had been created for it yet).

    task_id here is the deterministic f"dispatch-{request_id}" identity
    (manager.dispatch_requests.resolve_dispatch_status_for_request()'s own
    scheme) -- a caller MUST only call this once it has confirmed no real
    Task record exists for that id (see resolve_dispatch_status_for_request
    returning task=None), so this row is never a duplicate of a Task-truth
    row: once a Task exists, Task/Command/Execution truth is the sole
    source (see compute_dispatch_state()'s task-first branching), and no
    caller here should still be building a pre-Task row for it.

    Provider/account/model/mode are always the literal UNKNOWN at this
    stage -- no Command/Execution exists yet to carry them, so this is an
    honest UNKNOWN, not a guess. compute_visible_dispatch_gate() already
    treats a literal UNKNOWN value as a truthful PASS for a required field
    (see its docstring), so this row still passes the Gate.

    `dispatch_request_read_failed=True` (see
    manager.dispatch_requests.resolve_dispatch_status_for_request()'s own
    `dispatch_request_read_failed` field) means the underlying status read
    itself definitively failed (backend/network/malformed-record error) --
    NOT "no record was ever received". compute_dispatch_state() itself is
    never told about this distinction (its own has_dispatch_request/
    dispatch_request_status contract is intentionally unchanged -- see its
    docstring), so a genuine read failure is special-cased here, in this
    view-model layer, to an honest UNKNOWN with a reason that says so --
    never silently reported as ACCEPTED (which would be a guess) and never
    as "no row at all" (which is exactly the failure mode
    VISIBLE_BEFORE_TASK/dashboard_gate case 4 exists to close).
    """
    if dispatch_request_read_failed:
        dispatch = {
            "state": DISPATCH_STATE_UNKNOWN,
            "reason": "dispatch request status read failed (backend/network error); "
                      "state cannot be truthfully determined this refresh",
        }
    else:
        dispatch = compute_dispatch_state(
            None, None, None, now, has_dispatch_request=True,
            dispatch_request_status=dispatch_request_status,
        )
    project_name = (project.get("name") if isinstance(project, dict) else None) or project_id or UNKNOWN_LABEL

    return {
        "project_id": project_id or UNKNOWN_LABEL,
        "project_name": project_name,
        "task_id": f"dispatch-{request_id}",
        # No Task record exists yet -- there is no real title to show -- but
        # the request_id itself is real, durable evidence (this is why this
        # row exists at all), so it is surfaced here rather than the bare
        # literal UNKNOWN_LABEL: a blank/UNKNOWN title would leave a user
        # unable to tell which pre-Task row is which at a glance.
        "task_title": f"(pre-task) request {request_id}" if request_id else UNKNOWN_LABEL,
        "provider": UNKNOWN_LABEL,
        "account_id": UNKNOWN_LABEL,
        "model": UNKNOWN_LABEL,
        "mode": UNKNOWN_LABEL,
        "dispatch_state": dispatch["state"],
        "dispatch_reason": dispatch["reason"],
        "execution_id": UNKNOWN_LABEL,
        "session_id": UNKNOWN_LABEL,
        "quota": build_quota_truth(account_vms, UNKNOWN_LABEL, UNKNOWN_LABEL),
        "request_id": request_id,
        "pretask": True,
    }


def build_pretask_listing_truncated_row(
    project: Optional[Dict[str, Any]],
    project_id: str,
    account_vms: Sequence[AccountQuotaCardViewModel],
) -> Dict[str, Any]:
    """One synthetic pre-Task row surfaced when manager.dispatch_requests.
    list_recent_dispatch_request_ids() could not prove its scan of
    dispatch-requests/ for this project was complete this refresh (its own
    `truncated` field) -- e.g. the bounded page budget was exhausted while
    more objects remained, a page/metadata read failed mid-scan, or an
    object was missing usable recency metadata.

    This exists specifically to close the PRETASK_FALSE_NEGATIVE_RISK gap:
    an incomplete scan must never be rendered as a silent, confirmed "no
    pending pre-Task requests" -- even when the scan happened to find zero
    or few request_ids on the pages it did manage to read, a truly recent
    pending request may still exist beyond what was scanned.

    Same row shape/contract as build_dispatch_truth_row()/
    build_pretask_dispatch_truth_row() so it fits unchanged into the same
    Dashboard render loop and Visible Dispatch Gate. dispatch_state is the
    literal UNKNOWN -- this is an honest "cannot prove completeness", never
    a guess at any individual request's real status.
    """
    project_name = (project.get("name") if isinstance(project, dict) else None) or project_id or UNKNOWN_LABEL
    return {
        "project_id": project_id or UNKNOWN_LABEL,
        "project_name": project_name,
        "task_id": f"pretask-listing-truncated-{project_id or UNKNOWN_LABEL}",
        "task_title": "(pre-task) recent request listing incomplete -- a pending request may not be shown",
        "provider": UNKNOWN_LABEL,
        "account_id": UNKNOWN_LABEL,
        "model": UNKNOWN_LABEL,
        "mode": UNKNOWN_LABEL,
        "dispatch_state": DISPATCH_STATE_UNKNOWN,
        "dispatch_reason": "recent dispatch-request listing could not prove completeness this refresh "
                           "(bounded page budget exhausted, a page/metadata read failed, or an object had no "
                           "usable recency metadata); a truly recent pending request may exist but not be shown",
        "execution_id": UNKNOWN_LABEL,
        "session_id": UNKNOWN_LABEL,
        "quota": build_quota_truth(account_vms, UNKNOWN_LABEL, UNKNOWN_LABEL),
        "request_id": None,
        "pretask": True,
        "pretask_listing_truncated": True,
    }


# Every key a Dispatch Truth row must carry for the Visible Dispatch Gate to
# PASS. "quota" itself is always present (build_quota_truth() never omits
# it, even as the all-UNKNOWN row) -- the gate additionally checks that its
# "freshness" sub-field is populated, since freshness must be visible even
# when quota is otherwise unknown.
_REQUIRED_DISPATCH_TRUTH_FIELDS = (
    "project_id", "task_id", "provider", "account_id", "model", "mode",
    "dispatch_state", "execution_id", "session_id", "quota",
)


def compute_visible_dispatch_gate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """PASS/FAIL for the Visible Dispatch Gate banner.

    A row carrying the literal "UNKNOWN"/"STALE" marker for a field is a
    truthful PASS for that field -- the gate only FAILs when a required
    field is outright missing from the row (a read/build failure), or when
    there are no rows to show at all.
    """
    if not rows:
        return {"result": "FAIL", "reasons": ["no dispatch-visible task rows available"]}

    reasons: List[str] = []
    for row in rows:
        label = row.get("task_id", "unknown task")
        for key in _REQUIRED_DISPATCH_TRUTH_FIELDS:
            if key not in row or row[key] in (None, ""):
                reasons.append(f"{label}: missing required truth field '{key}'")
        quota = row.get("quota")
        if not isinstance(quota, dict) or "freshness" not in quota or quota["freshness"] in (None, ""):
            reasons.append(f"{label}: quota freshness not visible")

    if reasons:
        return {"result": "FAIL", "reasons": reasons}
    return {"result": "PASS", "reasons": []}


# =====================================================================
# Production Provenance (Dashboard self-identity vs. Watcher runtime)
# =====================================================================
#
# INTERIM, HONEST evidence source: as of this slice, no real Production
# Provenance Contract exists yet anywhere in this repo (verified against
# origin: no such branch/file, confirmed with the session -- "A2" --
# reportedly building it). Until that lands, watcher_tested_sha and
# watcher_activated_sha have no real evidence to report and MUST stay
# UNKNOWN rather than being guessed, copied from running_sha, or borrowed
# from a test fixture -- an UNKNOWN there correctly fails the gate below,
# which is the honest outcome, not a bug. repository_path/branch/
# running_sha ARE real: they come from actual `git` introspection of the
# actual on-disk checkouts (see dashboard.py's query_git_head_raw() /
# discover_repository_root_from_script_path(), which read the real
# Scheduled Task configuration and the real git HEAD -- never mocked).
# When the real Provenance Contract exists, only dashboard.py's I/O layer
# needs to change to source watcher_tested_sha/watcher_activated_sha from
# it; build_provenance_vm() below already accepts them as plain parameters.


def parse_task_to_run_path(verbose_schtasks_output: Optional[str]) -> Optional[str]:
    """Extract the launcher script path from `schtasks /FO LIST /V` output's
    "Task To Run:" line (may be quoted and may carry a leading executable
    like wscript.exe, or trailing arguments)."""
    if not verbose_schtasks_output:
        return None
    for line in verbose_schtasks_output.splitlines():
        line = line.strip()
        if not line.lower().startswith("task to run:"):
            continue
        raw = line.split(":", 1)[1].strip()
        if not raw:
            return None
        quote_start = raw.find('"')
        if quote_start != -1:
            quote_end = raw.find('"', quote_start + 1)
            if quote_end != -1:
                return raw[quote_start + 1:quote_end]
        return raw.split(" ")[0]
    return None


@dataclass
class ProvenanceViewModel:
    """User-visible Dashboard-vs-Watcher runtime identity truth."""
    dashboard_repository_path: str
    dashboard_branch: str
    dashboard_reviewed_sha: str
    watcher_repository_path: str
    watcher_branch: str
    watcher_running_sha: str
    watcher_tested_sha: str
    watcher_activated_sha: str
    captured_at: str
    all_match: bool
    match_detail: str
    evidence_source: str


def build_provenance_vm(
    dashboard_repository_path: Optional[str],
    dashboard_branch: Optional[str],
    dashboard_sha: Optional[str],
    watcher_repository_path: Optional[str],
    watcher_branch: Optional[str],
    watcher_running_sha: Optional[str],
    watcher_tested_sha: Optional[str] = None,
    watcher_activated_sha: Optional[str] = None,
    now: Optional[datetime] = None,
    evidence_source: str = "git introspection (interim; Production Provenance Contract pending)",
) -> ProvenanceViewModel:
    """Pure builder: every missing/blank input becomes the literal
    "UNKNOWN" string. The gate requires all four SHAs (Dashboard's own
    reviewed release SHA, and the Watcher's running/tested/activated SHAs)
    to be known AND identical -- any UNKNOWN or any mismatch is reported
    truthfully, never hidden and never silently passed."""
    now = now or datetime.now(timezone.utc)

    def _s(value: Optional[str]) -> str:
        return value if isinstance(value, str) and value.strip() else UNKNOWN_LABEL

    d_path, d_branch, d_sha = _s(dashboard_repository_path), _s(dashboard_branch), _s(dashboard_sha)
    w_path, w_branch = _s(watcher_repository_path), _s(watcher_branch)
    w_running, w_tested, w_activated = _s(watcher_running_sha), _s(watcher_tested_sha), _s(watcher_activated_sha)

    shas = {
        "dashboard_reviewed_sha": d_sha,
        "watcher_running_sha": w_running,
        "watcher_tested_sha": w_tested,
        "watcher_activated_sha": w_activated,
    }
    known = {k: v for k, v in shas.items() if v != UNKNOWN_LABEL}
    all_known = len(known) == len(shas)
    all_match = all_known and len(set(shas.values())) == 1

    if not all_known:
        missing = ", ".join(k for k, v in shas.items() if v == UNKNOWN_LABEL)
        detail = f"cannot verify: no real evidence for {missing}"
    elif all_match:
        detail = f"all four SHAs match ({d_sha})"
    else:
        detail = "SHA mismatch: " + ", ".join(f"{k}={v}" for k, v in shas.items())

    return ProvenanceViewModel(
        dashboard_repository_path=d_path,
        dashboard_branch=d_branch,
        dashboard_reviewed_sha=d_sha,
        watcher_repository_path=w_path,
        watcher_branch=w_branch,
        watcher_running_sha=w_running,
        watcher_tested_sha=w_tested,
        watcher_activated_sha=w_activated,
        captured_at=now.isoformat(),
        all_match=all_match,
        match_detail=detail,
        evidence_source=evidence_source,
    )


def compute_provenance_gate(vm: ProvenanceViewModel) -> Dict[str, Any]:
    """PASS only when Dashboard reviewed SHA == Watcher running_sha ==
    tested_sha == activated_sha, all real (non-UNKNOWN). This never falls
    back to a mock/test fixture and is never overridden by any other gate
    or test suite passing."""
    if vm.all_match:
        return {"result": "PASS", "reasons": []}
    return {"result": "FAIL", "reasons": [vm.match_detail]}


def compute_overall_visible_dispatch_gate(
    dispatch_gate: Dict[str, Any], provenance_gate: Dict[str, Any]
) -> Dict[str, Any]:
    """The Dashboard's single top-level PASS/FAIL banner: requires both the
    per-task dispatch-truth gate AND the Dashboard/Watcher provenance gate
    to pass. Either one failing fails the whole banner."""
    if dispatch_gate["result"] == "PASS" and provenance_gate["result"] == "PASS":
        return {"result": "PASS", "reasons": []}
    return {"result": "FAIL", "reasons": [*dispatch_gate["reasons"], *provenance_gate["reasons"]]}


_PROVENANCE_EVIDENCE_REQUIRED_FIELDS = (
    "running_sha", "tested_sha", "activated_sha", "repository_path", "branch", "captured_at",
)


def validate_provenance_evidence_document(doc: Any) -> Optional[Dict[str, str]]:
    """Validate a persisted Production Provenance Contract evidence
    document (e.g. <AI_MANAGER_HOME>/provenance/runtime_evidence.json).
    Returns a normalized dict on success, None on any structural problem --
    a malformed or incomplete file is never partially trusted."""
    if not isinstance(doc, dict):
        return None
    for key in _PROVENANCE_EVIDENCE_REQUIRED_FIELDS:
        value = doc.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    if parse_time(doc["captured_at"]) is None:
        return None
    return {key: doc[key] for key in _PROVENANCE_EVIDENCE_REQUIRED_FIELDS}


def _has_extended_running_proof(
    watcher_running: bool, active_task: Optional[Dict[str, Any]],
    active_command: Optional[Dict[str, Any]], active_execution: Optional[Dict[str, Any]],
) -> bool:
    return bool(
        watcher_running and isinstance(active_task, dict) and isinstance(active_command, dict)
        and isinstance(active_execution, dict) and active_execution.get("status") == "running"
        and isinstance(active_execution.get("provider_session_id"), str)
        and active_execution["provider_session_id"].strip()
        and active_command.get("status") == "running"
        and active_command.get("execution_id") == active_execution.get("execution_id")
        and active_task.get("project_id") == active_command.get("project_id") == active_execution.get("project_id")
        and active_task.get("task_id") == active_command.get("task_id") == active_execution.get("task_id")
    )


def reconcile_watcher_provenance_evidence(
    independently_observed_repository_path: Optional[str],
    independently_observed_running_sha: Optional[str],
    evidence_document: Optional[Dict[str, str]],
    now: Optional[datetime] = None,
    watcher_running: bool = False,
    active_task: Optional[Dict[str, Any]] = None,
    active_command: Optional[Dict[str, Any]] = None,
    active_execution: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[str]]:
    """Only trust a persisted evidence document's tested_sha/activated_sha
    when its own repository_path AND running_sha agree with what this
    Dashboard independently observed via real git introspection of the
    Watcher's actual on-disk checkout -- a stale or mismatched evidence
    file must never silently override live ground truth. Always returns
    UNKNOWN-safe fallbacks (None) rather than guessing."""
    now = now or datetime.now(timezone.utc)
    if evidence_document is None:
        return {"tested_sha": None, "activated_sha": None, "captured_at": None,
                "note": "no persisted Production Provenance Contract evidence file found"}
    if evidence_document["repository_path"] != independently_observed_repository_path:
        return {"tested_sha": None, "activated_sha": None, "captured_at": None,
                "note": "evidence file repository_path does not match the live Watcher checkout -- ignored"}
    if evidence_document["running_sha"] != independently_observed_running_sha:
        return {"tested_sha": None, "activated_sha": None, "captured_at": None,
                "note": "evidence file running_sha does not match the live Watcher HEAD -- ignored (stale evidence)"}
    captured_at = parse_time(evidence_document["captured_at"])
    if captured_at is None or captured_at > now + timedelta(seconds=FUTURE_CLOCK_SKEW_SECONDS):
        return {"tested_sha": None, "activated_sha": None, "captured_at": None,
                "note": "evidence file captured_at is invalid or too far in the future -- ignored"}
    age = (now - captured_at).total_seconds()
    if age > NORMAL_MAX_AGE_SECONDS and not (
        age <= EXTENDED_RUNNING_MAX_AGE_SECONDS and
        _has_extended_running_proof(watcher_running, active_task, active_command, active_execution)
    ):
        return {"tested_sha": None, "activated_sha": None, "captured_at": None,
                "note": "evidence file freshness is not proven by a live linked provider session -- ignored"}
    return {
        "tested_sha": evidence_document["tested_sha"],
        "activated_sha": evidence_document["activated_sha"],
        "captured_at": evidence_document["captured_at"],
        "note": "evidence file matches the live Watcher checkout",
    }
