#!/usr/bin/env python3
"""Per-account quota forecast and daily brief core.

Pure computation module for forecasting quota burn rate, reset horizon,
waste risk, and account prioritization across providers and accounts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


FUTURE_SKEW_MINUTES = 5
MIN_SAMPLE_INTERVAL_SECONDS = 60
DEFAULT_MAX_AGE_MINUTES = 60
RELIABLE_SOURCES = {
    "codex": {"codex_app_server", "official_app_server"},
    "claude": {"claude_code_statusline_rate_limits", "official_statusline"},
}


class WarningLevel(str, Enum):
    UNKNOWN = "UNKNOWN"
    NORMAL = "NORMAL"
    ATTENTION = "ATTENTION"
    WARNING = "WARNING"
    URGENT = "URGENT"


class RiskStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    CONSERVE = "conserve"
    CONSUME_FASTER = "consume_faster"
    LIKELY_EXHAUST_BEFORE_RESET = "likely_exhaust_before_reset"
    EXHAUSTED = "exhausted"


class ActionRecommendation(str, Enum):
    HOLD = "hold"
    CONSERVE = "conserve"
    NORMAL_USE = "normal_use"
    SUGGEST_CONSUME = "suggest_consume"
    URGENT_CONSUME = "urgent_consume"


_WARNING_SEVERITY = {
    WarningLevel.NORMAL: 1,
    WarningLevel.ATTENTION: 2,
    WarningLevel.WARNING: 3,
    WarningLevel.URGENT: 4,
}


def parse_iso_time(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime.

    Fails safe to None for null, invalid, or malformed timestamps.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


@dataclass
class QuotaWindowForecast:
    window_name: str
    duration_minutes: Optional[int] = None
    remaining_percent: Optional[float] = None
    used_percent: Optional[float] = None
    resets_at: Optional[str] = None
    hours_to_reset: Optional[float] = None
    stale: bool = False
    freshness: str = "fresh"
    source: str = "not_reported"
    source_type: str = "manual"
    confidence: str = "unknown"
    burn_rate_pct_per_hour: Optional[float] = None
    burn_rate_samples: int = 0
    estimated_hours_to_exhaustion: Optional[float] = None
    estimated_remaining_at_reset: Optional[float] = None
    warning_level: WarningLevel = WarningLevel.UNKNOWN
    risk_status: RiskStatus = RiskStatus.UNKNOWN
    action_recommendation: ActionRecommendation = ActionRecommendation.HOLD
    warning_reason: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return forecast_to_dict(self)


@dataclass
class AccountQuotaForecast:
    provider: str
    account_id: Optional[str] = None
    display_name: str = ""
    status: str = "unknown"
    last_updated: Optional[str] = None
    stale: bool = False
    freshness: str = "fresh"
    source: str = "not_reported"
    source_type: str = "manual"
    confidence: str = "unknown"
    has_reliable_quota: bool = False
    source_reliable: bool = False
    source_verified: bool = False
    windows: List[QuotaWindowForecast] = field(default_factory=list)
    primary_window: Optional[QuotaWindowForecast] = None
    overall_warning_level: WarningLevel = WarningLevel.UNKNOWN
    overall_risk_status: RiskStatus = RiskStatus.UNKNOWN
    overall_action_recommendation: ActionRecommendation = ActionRecommendation.HOLD
    overall_warning_reason: str = ""
    dispatchable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return forecast_to_dict(self)


@dataclass
class DailyBriefForecast:
    generated_at: str
    accounts: List[AccountQuotaForecast] = field(default_factory=list)
    summary_counts: Dict[str, int] = field(default_factory=dict)
    highest_remaining_accounts: List[AccountQuotaForecast] = field(default_factory=list)
    soonest_reset_accounts: List[AccountQuotaForecast] = field(default_factory=list)
    recommended_consume_accounts: List[AccountQuotaForecast] = field(default_factory=list)
    conserve_accounts: List[AccountQuotaForecast] = field(default_factory=list)
    hold_or_unreliable_accounts: List[AccountQuotaForecast] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return forecast_to_dict(self)


def _extract_window(snapshot: Dict[str, Any], window_name: str) -> Optional[Dict[str, Any]]:
    for w in snapshot.get("windows", []):
        if isinstance(w, dict) and w.get("name") == window_name:
            return w
    return None


def calculate_window_burn_rate(
    window_name: str,
    current_item: Dict[str, Any],
    history: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[float], int, List[Dict[str, Any]]]:
    """Calculate the observed burn rate (% per hour) for a specific window.

    Strict rules applied:
    - Independent per window (no cross-window math).
    - Deduplicates snapshots sharing the exact same timestamp.
    - Sorts samples chronologically.
    - Detects reset/replenishment boundaries (quota increases, reset timestamp change,
      or reset timestamp passing). Never calculates burn rate across a reset boundary.
    - Never fabricates negative burn rates.
    - Requires >= 2 samples within the active cycle with >= 60s span.
    """
    now = now or datetime.now(timezone.utc)
    all_snapshots = ([s for s in history if isinstance(s, dict)] if history else []) + [current_item]

    # Extract valid points (timestamp, remaining_percent, resets_at_str, resets_at_dt)
    extracted = []
    for snap in all_snapshots:
        ts = parse_iso_time(snap.get("last_updated"))
        if ts is None:
            continue
        w = _extract_window(snap, window_name)
        if w is None:
            continue
        rem = w.get("remaining_percent")
        if rem is None or not isinstance(rem, (int, float)):
            continue
        rem = float(rem)
        resets_str = w.get("resets_at")
        resets_dt = parse_iso_time(resets_str) if resets_str else None
        extracted.append((ts, rem, resets_str, resets_dt))

    if not extracted:
        return (None, 0, [])

    # Deduplicate snapshots with identical timestamps (keep last in input order)
    deduped_by_time: Dict[datetime, Tuple[datetime, float, Optional[str], Optional[datetime]]] = {}
    for pt in extracted:
        deduped_by_time[pt[0]] = pt

    sorted_points = sorted(deduped_by_time.values(), key=lambda p: p[0])

    # Scan chronologically for reset/replenishment boundaries
    # Any boundary resets the active cycle starting index
    active_start_idx = 0
    for i in range(len(sorted_points) - 1):
        t_curr, rem_curr, resets_curr, resets_dt_curr = sorted_points[i]
        t_next, rem_next, resets_next, resets_dt_next = sorted_points[i + 1]

        # 1. Quota increased (replenishment or reset occurred)
        if rem_next > rem_curr:
            active_start_idx = i + 1
            continue

        # 2. Reset timestamp changed between valid values
        if resets_curr and resets_next and resets_curr != resets_next:
            active_start_idx = i + 1
            continue

        # 3. Reset timestamp passed between samples
        if resets_dt_curr and t_next > resets_dt_curr:
            active_start_idx = i + 1
            continue

    active_points = sorted_points[active_start_idx:]
    sample_count = len(active_points)

    evidence = [
        {
            "timestamp": pt[0].isoformat(),
            "remaining_percent": pt[1],
            "resets_at": pt[2],
        }
        for pt in active_points
    ]

    if sample_count < 2:
        return (None, sample_count, evidence)

    first_t, first_rem, _, _ = active_points[0]
    last_t, last_rem, _, _ = active_points[-1]

    delta_seconds = (last_t - first_t).total_seconds()
    if delta_seconds < MIN_SAMPLE_INTERVAL_SECONDS:
        return (None, sample_count, evidence)

    delta_quota = first_rem - last_rem
    delta_hours = delta_seconds / 3600.0

    if delta_hours <= 0:
        return (None, sample_count, evidence)

    burn_rate = max(0.0, delta_quota / delta_hours)
    return (round(burn_rate, 4), sample_count, evidence)


def forecast_window(
    window_name: str,
    current_item: Dict[str, Any],
    history: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
) -> QuotaWindowForecast:
    """Compute forecast, burn rate, and warning level for a single quota window."""
    now = now or datetime.now(timezone.utc)
    w = _extract_window(current_item, window_name)

    # Base metadata from current item
    source = current_item.get("source", "not_reported")
    source_type = current_item.get("source_type", "manual")
    confidence = current_item.get("confidence", "unknown")

    # Evaluate item staleness
    updated_dt = parse_iso_time(current_item.get("last_updated"))
    age_minutes = None if updated_dt is None else (now - updated_dt).total_seconds() / 60.0
    future_skewed = age_minutes is not None and age_minutes < -FUTURE_SKEW_MINUTES
    stale = (
        bool(current_item.get("stale"))
        or age_minutes is None
        or age_minutes > max_age_minutes
        or future_skewed
    )
    freshness = "stale" if stale else "fresh"

    if w is None:
        return QuotaWindowForecast(
            window_name=window_name,
            stale=stale,
            freshness=freshness,
            source=source,
            source_type=source_type,
            confidence=confidence,
            warning_level=WarningLevel.UNKNOWN,
            risk_status=RiskStatus.UNKNOWN,
            action_recommendation=ActionRecommendation.HOLD,
            warning_reason=f"Window '{window_name}' not present in current snapshot",
        )

    rem = w.get("remaining_percent")
    used = w.get("used_percent")
    duration = w.get("duration_minutes")
    resets_str = w.get("resets_at")
    resets_dt = parse_iso_time(resets_str) if resets_str else None

    # Unknown remaining percent
    if rem is None or not isinstance(rem, (int, float)):
        return QuotaWindowForecast(
            window_name=window_name,
            duration_minutes=duration,
            remaining_percent=None,
            used_percent=used,
            resets_at=resets_str,
            stale=stale,
            freshness=freshness,
            source=source,
            source_type=source_type,
            confidence=confidence,
            warning_level=WarningLevel.UNKNOWN,
            risk_status=RiskStatus.UNKNOWN,
            action_recommendation=ActionRecommendation.HOLD,
            warning_reason="Quota remaining percent is unknown",
        )

    rem = float(rem)
    hours_to_reset = (
        (resets_dt - now).total_seconds() / 3600.0 if resets_dt is not None else None
    )

    # Compute burn rate from valid samples
    burn_rate, samples, evidence = calculate_window_burn_rate(window_name, current_item, history, now)

    base_evidence = {
        "samples_used": samples,
        "history_points": evidence,
        "age_minutes": None if age_minutes is None else round(max(0.0, age_minutes), 1),
    }

    # Stale telemetry
    if stale:
        age_str = f"{age_minutes:.1f}m ago" if age_minutes is not None else "unknown"
        return QuotaWindowForecast(
            window_name=window_name,
            duration_minutes=duration,
            remaining_percent=rem,
            used_percent=used,
            resets_at=resets_str,
            hours_to_reset=hours_to_reset,
            stale=True,
            freshness="stale",
            source=source,
            source_type=source_type,
            confidence=confidence,
            burn_rate_pct_per_hour=burn_rate,
            burn_rate_samples=samples,
            warning_level=WarningLevel.UNKNOWN,
            risk_status=RiskStatus.UNKNOWN,
            action_recommendation=ActionRecommendation.HOLD,
            warning_reason=f"Quota telemetry is stale (last updated {age_str})",
            evidence=base_evidence,
        )

    # Resets_at is unknown
    if resets_dt is None:
        return QuotaWindowForecast(
            window_name=window_name,
            duration_minutes=duration,
            remaining_percent=rem,
            used_percent=used,
            resets_at=None,
            hours_to_reset=None,
            stale=False,
            freshness="fresh",
            source=source,
            source_type=source_type,
            confidence=confidence,
            burn_rate_pct_per_hour=burn_rate,
            burn_rate_samples=samples,
            warning_level=WarningLevel.UNKNOWN,
            risk_status=RiskStatus.UNKNOWN,
            action_recommendation=ActionRecommendation.HOLD,
            warning_reason="Reset timestamp is unknown",
            evidence=base_evidence,
        )

    # Reset already passed
    if hours_to_reset is not None and hours_to_reset <= 0:
        return QuotaWindowForecast(
            window_name=window_name,
            duration_minutes=duration,
            remaining_percent=rem,
            used_percent=used,
            resets_at=resets_str,
            hours_to_reset=round(hours_to_reset, 2),
            stale=False,
            freshness="fresh",
            source=source,
            source_type=source_type,
            confidence=confidence,
            burn_rate_pct_per_hour=burn_rate,
            burn_rate_samples=samples,
            warning_level=WarningLevel.UNKNOWN,
            risk_status=RiskStatus.UNKNOWN,
            action_recommendation=ActionRecommendation.HOLD,
            warning_reason="Window reset time is in the past; awaiting fresh telemetry",
            evidence=base_evidence,
        )

    # Insufficient history for burn rate
    if burn_rate is None:
        return QuotaWindowForecast(
            window_name=window_name,
            duration_minutes=duration,
            remaining_percent=rem,
            used_percent=used,
            resets_at=resets_str,
            hours_to_reset=round(hours_to_reset, 2) if hours_to_reset is not None else None,
            stale=False,
            freshness="fresh",
            source=source,
            source_type=source_type,
            confidence=confidence,
            burn_rate_pct_per_hour=None,
            burn_rate_samples=samples,
            warning_level=WarningLevel.UNKNOWN,
            risk_status=RiskStatus.UNKNOWN,
            action_recommendation=ActionRecommendation.HOLD,
            warning_reason=f"Insufficient history to compute burn rate ({samples} sample(s))",
            evidence=base_evidence,
        )

    # Already exhausted (0% remaining)
    if rem == 0.0:
        return QuotaWindowForecast(
            window_name=window_name,
            duration_minutes=duration,
            remaining_percent=0.0,
            used_percent=100.0,
            resets_at=resets_str,
            hours_to_reset=round(hours_to_reset, 2) if hours_to_reset is not None else None,
            stale=False,
            freshness="fresh",
            source=source,
            source_type=source_type,
            confidence=confidence,
            burn_rate_pct_per_hour=burn_rate,
            burn_rate_samples=samples,
            estimated_hours_to_exhaustion=0.0,
            estimated_remaining_at_reset=0.0,
            warning_level=WarningLevel.NORMAL,
            risk_status=RiskStatus.EXHAUSTED,
            action_recommendation=ActionRecommendation.HOLD,
            warning_reason="Quota exhausted (0% remaining)",
            evidence=base_evidence,
        )

    # Calculate projections
    est_hours_exhaustion = (rem / burn_rate) if burn_rate > 0 else None
    projected_consumption = burn_rate * hours_to_reset if hours_to_reset is not None else 0.0
    est_remaining_at_reset = max(0.0, rem - projected_consumption)

    # Case A: Burn rate is fast enough that quota will exhaust before reset
    if (
        est_hours_exhaustion is not None
        and hours_to_reset is not None
        and est_hours_exhaustion < hours_to_reset
    ):
        return QuotaWindowForecast(
            window_name=window_name,
            duration_minutes=duration,
            remaining_percent=rem,
            used_percent=used,
            resets_at=resets_str,
            hours_to_reset=round(hours_to_reset, 2),
            stale=False,
            freshness="fresh",
            source=source,
            source_type=source_type,
            confidence=confidence,
            burn_rate_pct_per_hour=burn_rate,
            burn_rate_samples=samples,
            estimated_hours_to_exhaustion=round(est_hours_exhaustion, 2),
            estimated_remaining_at_reset=0.0,
            warning_level=WarningLevel.NORMAL,
            risk_status=RiskStatus.LIKELY_EXHAUST_BEFORE_RESET,
            action_recommendation=ActionRecommendation.CONSERVE,
            warning_reason=(
                f"Quota will exhaust in ~{est_hours_exhaustion:.1f}h before reset "
                f"in {hours_to_reset:.1f}h (burn rate: {burn_rate:.1f}%/h)"
            ),
            evidence=base_evidence,
        )

    # Case B: Waste warning heuristics for unspent quota at reset
    if hours_to_reset is not None and hours_to_reset <= 2.0 and est_remaining_at_reset > 10.0:
        warning_lvl = WarningLevel.URGENT
        risk = RiskStatus.CONSUME_FASTER
        action = ActionRecommendation.URGENT_CONSUME
        reason = (
            f"Urgent waste risk: projected {est_remaining_at_reset:.1f}% unspent with "
            f"only {hours_to_reset:.1f}h until reset"
        )
    elif est_remaining_at_reset > 20.0:
        warning_lvl = WarningLevel.WARNING
        risk = RiskStatus.CONSUME_FASTER
        action = ActionRecommendation.SUGGEST_CONSUME
        reason = (
            f"High waste risk: projected {est_remaining_at_reset:.1f}% unspent at reset "
            f"in {hours_to_reset:.1f}h"
        )
    elif est_remaining_at_reset > 10.0:
        warning_lvl = WarningLevel.ATTENTION
        risk = RiskStatus.CONSERVE
        action = ActionRecommendation.SUGGEST_CONSUME
        reason = (
            f"Moderate leftover: projected {est_remaining_at_reset:.1f}% unspent at reset "
            f"in {hours_to_reset:.1f}h"
        )
    else:
        warning_lvl = WarningLevel.NORMAL
        risk = RiskStatus.HEALTHY
        action = ActionRecommendation.NORMAL_USE
        reason = (
            f"Healthy utilization: projected {est_remaining_at_reset:.1f}% remaining at reset "
            f"in {hours_to_reset:.1f}h"
        )

    return QuotaWindowForecast(
        window_name=window_name,
        duration_minutes=duration,
        remaining_percent=rem,
        used_percent=used,
        resets_at=resets_str,
        hours_to_reset=round(hours_to_reset, 2) if hours_to_reset is not None else None,
        stale=False,
        freshness="fresh",
        source=source,
        source_type=source_type,
        confidence=confidence,
        burn_rate_pct_per_hour=burn_rate,
        burn_rate_samples=samples,
        estimated_hours_to_exhaustion=(
            round(est_hours_exhaustion, 2) if est_hours_exhaustion is not None else None
        ),
        estimated_remaining_at_reset=round(est_remaining_at_reset, 2),
        warning_level=warning_lvl,
        risk_status=risk,
        action_recommendation=action,
        warning_reason=reason,
        evidence=base_evidence,
    )


def _pick_primary_window(windows: Sequence[QuotaWindowForecast]) -> Optional[QuotaWindowForecast]:
    if not windows:
        return None
    # Preference: five_hour -> primary -> seven_day -> first
    for name in ("five_hour", "primary", "seven_day"):
        for w in windows:
            if w.window_name == name:
                return w
    return windows[0]


def forecast_account(
    current_account_item: Dict[str, Any],
    history: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
) -> AccountQuotaForecast:
    """Forecast quota standing and risks for a single (provider, account_id)."""
    now = now or datetime.now(timezone.utc)
    provider_id = current_account_item.get("provider", "unknown")
    account_id = current_account_item.get("account_id")
    display_name = current_account_item.get("display_name", provider_id)
    status = current_account_item.get("status", "unknown")
    source = current_account_item.get("source", "not_reported")
    source_type = current_account_item.get("source_type", "manual")
    confidence = current_account_item.get("confidence", "unknown")

    # Evaluate staleness
    updated_dt = parse_iso_time(current_account_item.get("last_updated"))
    age_minutes = None if updated_dt is None else (now - updated_dt).total_seconds() / 60.0
    future_skewed = age_minutes is not None and age_minutes < -FUTURE_SKEW_MINUTES
    stale = (
        bool(current_account_item.get("stale"))
        or age_minutes is None
        or age_minutes > max_age_minutes
        or future_skewed
    )
    freshness = "stale" if stale else "fresh"

    source_reliable = (source_type == "official" and confidence == "official")
    source_verified = source_reliable and source in RELIABLE_SOURCES.get(provider_id, set())

    # Filter history strictly to this account
    account_history = []
    if history:
        for item in history:
            if (
                isinstance(item, dict)
                and item.get("provider") == provider_id
                and item.get("account_id") == account_id
            ):
                account_history.append(item)

    # Forecast each window strictly independently
    raw_windows = current_account_item.get("windows", [])
    window_forecasts: List[QuotaWindowForecast] = []
    for raw_w in raw_windows:
        if isinstance(raw_w, dict) and "name" in raw_w:
            w_fc = forecast_window(
                raw_w["name"],
                current_account_item,
                history=account_history,
                now=now,
                max_age_minutes=max_age_minutes,
            )
            window_forecasts.append(w_fc)

    primary = _pick_primary_window(window_forecasts)

    has_reliable_quota = (
        not stale
        and source_reliable
        and bool(window_forecasts)
        and any(w.remaining_percent is not None for w in window_forecasts)
    )

    # Determine overall warning level
    evaluated_warnings = [
        w.warning_level for w in window_forecasts if w.warning_level != WarningLevel.UNKNOWN
    ]
    if not evaluated_warnings:
        overall_warning = WarningLevel.UNKNOWN
        overall_reason = (
            primary.warning_reason if primary else "No quota windows available to forecast"
        )
    else:
        overall_warning = max(
            evaluated_warnings, key=lambda lvl: _WARNING_SEVERITY.get(lvl, 0)
        )
        worst_window = next(w for w in window_forecasts if w.warning_level == overall_warning)
        overall_reason = worst_window.warning_reason

    overall_risk = primary.risk_status if primary else RiskStatus.UNKNOWN
    overall_action = primary.action_recommendation if primary else ActionRecommendation.HOLD

    # Dispatchability
    has_positive_remaining = (
        primary is not None
        and primary.remaining_percent is not None
        and primary.remaining_percent > 0
    )
    dispatchable = (
        not stale
        and has_reliable_quota
        and has_positive_remaining
    )

    return AccountQuotaForecast(
        provider=provider_id,
        account_id=account_id,
        display_name=display_name,
        status=status,
        last_updated=current_account_item.get("last_updated"),
        stale=stale,
        freshness=freshness,
        source=source,
        source_type=source_type,
        confidence=confidence,
        has_reliable_quota=has_reliable_quota,
        source_reliable=source_reliable,
        source_verified=source_verified,
        windows=window_forecasts,
        primary_window=primary,
        overall_warning_level=overall_warning,
        overall_risk_status=overall_risk,
        overall_action_recommendation=overall_action,
        overall_warning_reason=overall_reason,
        dispatchable=dispatchable,
    )


def _normalize_account_items(current_doc_or_accounts: Union[Dict[str, Any], Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if isinstance(current_doc_or_accounts, list):
        return [item for item in current_doc_or_accounts if isinstance(item, dict)]
    if isinstance(current_doc_or_accounts, dict):
        if "accounts" in current_doc_or_accounts and isinstance(current_doc_or_accounts["accounts"], list):
            return [item for item in current_doc_or_accounts["accounts"] if isinstance(item, dict)]
        if "providers" in current_doc_or_accounts and isinstance(current_doc_or_accounts["providers"], list):
            return [item for item in current_doc_or_accounts["providers"] if isinstance(item, dict)]
    return []


def forecast_daily_brief(
    current_doc_or_accounts: Union[Dict[str, Any], Sequence[Dict[str, Any]]],
    history: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
) -> DailyBriefForecast:
    """Generate multi-account daily brief summary and prioritization."""
    now = now or datetime.now(timezone.utc)
    items = _normalize_account_items(current_doc_or_accounts)

    accounts: List[AccountQuotaForecast] = []
    for item in items:
        fc = forecast_account(item, history=history, now=now, max_age_minutes=max_age_minutes)
        accounts.append(fc)

    summary_counts = {
        WarningLevel.UNKNOWN.value: 0,
        WarningLevel.NORMAL.value: 0,
        WarningLevel.ATTENTION.value: 0,
        WarningLevel.WARNING.value: 0,
        WarningLevel.URGENT.value: 0,
    }
    for acc in accounts:
        summary_counts[acc.overall_warning_level.value] = (
            summary_counts.get(acc.overall_warning_level.value, 0) + 1
        )

    # 1. Highest remaining accounts (among dispatchable / reliable accounts with known remaining)
    dispatchable_accounts = [a for a in accounts if a.dispatchable and a.primary_window is not None]
    highest_remaining = sorted(
        dispatchable_accounts,
        key=lambda a: (a.primary_window.remaining_percent or 0.0),
        reverse=True,
    )

    # 2. Soonest reset accounts (among dispatchable with known future reset)
    soonest_reset = [
        a for a in dispatchable_accounts
        if a.primary_window.hours_to_reset is not None and a.primary_window.hours_to_reset > 0
    ]
    soonest_reset.sort(key=lambda a: a.primary_window.hours_to_reset)

    # 3. Recommended consume accounts (urgent or suggest consume)
    recommended_consume = [
        a for a in accounts
        if a.overall_action_recommendation in (ActionRecommendation.URGENT_CONSUME, ActionRecommendation.SUGGEST_CONSUME)
    ]
    recommended_consume.sort(
        key=lambda a: (
            1 if a.overall_action_recommendation == ActionRecommendation.URGENT_CONSUME else 2,
            -(a.primary_window.remaining_percent or 0.0) if a.primary_window else 0.0,
        )
    )

    # 4. Conserve accounts
    conserve_accounts = [
        a for a in accounts
        if a.overall_action_recommendation == ActionRecommendation.CONSERVE
        or a.overall_risk_status == RiskStatus.LIKELY_EXHAUST_BEFORE_RESET
    ]

    # 5. Hold or unreliable accounts
    hold_accounts = [a for a in accounts if not a.dispatchable]

    return DailyBriefForecast(
        generated_at=now.isoformat(),
        accounts=accounts,
        summary_counts=summary_counts,
        highest_remaining_accounts=highest_remaining,
        soonest_reset_accounts=soonest_reset,
        recommended_consume_accounts=recommended_consume,
        conserve_accounts=conserve_accounts,
        hold_or_unreliable_accounts=hold_accounts,
    )


def forecast_to_dict(obj: Any) -> Any:
    """Convert dataclasses, Enums, and datetimes into standard JSON-serializable types."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        result = {}
        for k in obj.__dataclass_fields__:
            v = getattr(obj, k)
            result[k] = forecast_to_dict(v)
        return result
    if isinstance(obj, dict):
        return {k: forecast_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [forecast_to_dict(item) for item in obj]
    return obj
