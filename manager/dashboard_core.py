"""Core logic and ViewModels for ADM Operations Dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    running_tasks = sum(1 for t in all_tasks if t.get("status") == "in_progress")
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
