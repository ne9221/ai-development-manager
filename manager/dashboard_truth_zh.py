"""Traditional Chinese (zh-TW) HOME Dashboard Truth Layer view-models.

Pure, Streamlit-free builders that translate the existing English-language
truth primitives in manager.dashboard_core (and raw Task/Command/Execution/
Session/Handoff/Provenance records) into Traditional Chinese labels for the
HOME-visible Dashboard.

This module adds no new evidence source and performs no I/O: it only
re-labels values that manager.dashboard_core (or the raw Drive record) has
already proven. Any field this module cannot prove from a real record stays
the literal UNKNOWN_LABEL ("UNKNOWN") from manager.dashboard_core, or its
zh-TW equivalent UNKNOWN_ZH ("未知") for values only ever shown in zh-TW --
never fabricated, inferred, or borrowed from another record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from manager.dashboard_core import (
    UNKNOWN_LABEL,
    AccountQuotaCardViewModel,
    ProvenanceViewModel,
    build_quota_truth,
    determine_execution_state,
)

UNKNOWN_ZH = "未知"
NOT_CREATED_ZH = "尚未建立"
AUTO_SELECT_ZH = "自動選擇"

TASK_STATUS_ZH = {
    "queued": "排隊中",
    "ready": "就緒",
    "in_progress": "進行中",
    "blocked": "已阻擋",
    "completed": "已完成",
    "cancelled": "已取消",
}

DISPATCH_STATE_ZH = {
    "SUBMITTED": "已提交",
    "ACCEPTED": "已受理",
    "QUEUED": "排隊中",
    "CLAIMED": "已認領",
    "RUNNING": "執行中",
    "COMPLETED": "已完成",
    "FAILED": "失敗",
    "BLOCKED": "已阻擋",
    "CANCELLED": "已取消",
    "UNKNOWN": UNKNOWN_ZH,
}

EXECUTION_STATE_ZH = {
    "reserved": "已保留",
    "running": "執行中",
    "waiting": "等待回應",
    "correlating": "關聯中",
    "finishing": "收尾中",
    "completed": "已完成",
    "failed": "失敗",
    "interrupted": "已中斷",
    "cancelled": "已取消",
    "unknown": UNKNOWN_ZH,
}

SESSION_STATUS_ZH = {
    "active": "進行中",
    "completed": "已完成",
    "research": "研究中",
    "obsolete": "已過時",
    "unknown": UNKNOWN_ZH,
}

FRESHNESS_ZH = {
    "fresh": "最新",
    "STALE": "過時",
    UNKNOWN_LABEL: UNKNOWN_ZH,
}


def _zh(mapping: Dict[str, str], key: Optional[str]) -> str:
    if not isinstance(key, str) or not key:
        return UNKNOWN_ZH
    return mapping.get(key, key)


def _text(source: Any, key: str) -> Optional[str]:
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, str) and value.strip() else None


# =====================================================================
# Task truth (任務真相)
# =====================================================================


@dataclass
class TaskTruthViewModel:
    """任務真相: identity, status, and the task's own self-reported progress
    fields -- never inferred from Command/Execution state."""

    project_id: str
    task_id: str
    command_id: str
    status_raw: str
    status_zh: str
    current_progress: str
    next_action: str
    blocked_reason: str


def build_task_truth_zh(
    task: Optional[Dict[str, Any]], command: Optional[Dict[str, Any]] = None
) -> TaskTruthViewModel:
    """command_id is NOT_CREATED_ZH (尚未建立) -- not an error -- when no
    Command has been dispatched for this Task yet. blocked_reason is "—"
    when the task is not blocked (there is truthfully nothing to report),
    and UNKNOWN_LABEL only when the task IS blocked but carries no reason."""
    task = task if isinstance(task, dict) else {}
    status = _text(task, "status")
    command_id = _text(command, "command_id")

    if status == "blocked":
        blocked_reason = _text(task, "blocked_reason") or UNKNOWN_LABEL
    else:
        blocked_reason = "—"

    return TaskTruthViewModel(
        project_id=_text(task, "project_id") or UNKNOWN_LABEL,
        task_id=_text(task, "task_id") or UNKNOWN_LABEL,
        command_id=command_id or NOT_CREATED_ZH,
        status_raw=status or UNKNOWN_LABEL,
        status_zh=_zh(TASK_STATUS_ZH, status),
        current_progress=_text(task, "current_progress") or UNKNOWN_LABEL,
        next_action=_text(task, "next_action") or UNKNOWN_LABEL,
        blocked_reason=blocked_reason,
    )


def dispatch_state_zh(dispatch_state_raw: Optional[str]) -> str:
    if not isinstance(dispatch_state_raw, str):
        return UNKNOWN_ZH
    return DISPATCH_STATE_ZH.get(dispatch_state_raw, dispatch_state_raw)


# =====================================================================
# Routing truth (派工真相): requested vs. actual
# =====================================================================


@dataclass
class RoutingTruthViewModel:
    """requested_* preserves the caller's original ask verbatim (or
    AUTO_SELECT_ZH when the caller left selection to automatic
    quota-aware assignment -- that is a real, distinct state, not
    UNKNOWN). provider_matches_request is None when there was no explicit
    request to compare against; it is never fabricated as True."""

    requested_provider: str
    requested_account_id: str
    actual_provider: str
    actual_account_id: str
    provider_matches_request: Optional[bool]
    selection_reason: List[str]


def build_routing_truth_zh(command: Optional[Dict[str, Any]]) -> RoutingTruthViewModel:
    command = command if isinstance(command, dict) else {}
    requested_provider = _text(command, "requested_provider")
    requested_account = _text(command, "requested_account_id")
    actual_provider = _text(command, "provider")
    actual_account = _text(command, "account_id")

    if requested_provider is None:
        matches: Optional[bool] = None
    else:
        matches = requested_provider == actual_provider and requested_account == actual_account

    raw_reasons = command.get("selection_reason")
    reasons = [r for r in raw_reasons if isinstance(r, str)] if isinstance(raw_reasons, list) else []

    return RoutingTruthViewModel(
        requested_provider=requested_provider or AUTO_SELECT_ZH,
        requested_account_id=requested_account or AUTO_SELECT_ZH,
        actual_provider=actual_provider or UNKNOWN_LABEL,
        actual_account_id=actual_account or UNKNOWN_LABEL,
        provider_matches_request=matches,
        selection_reason=reasons,
    )


# =====================================================================
# Quota truth (額度真相)
# =====================================================================


@dataclass
class QuotaTruthViewModel:
    """usable is the literal truth question "can this account actually be
    dispatched to right now": 可用 (usable), 不可用（額度為 0）(zero quota),
    or 未知 (UNKNOWN) whenever freshness/telemetry can't prove either way --
    a stale record is never rendered as usable, and 0% is never rendered as
    merely "low"."""

    provider: str
    account_id: str
    found: bool
    freshness_zh: str
    usable: str
    five_hour_used: str
    five_hour_remaining: str
    five_hour_reset_at: str
    weekly_used: str
    weekly_remaining: str
    weekly_reset_at: str
    last_updated: str


def build_quota_truth_zh(
    account_vms: Sequence[AccountQuotaCardViewModel], provider: str, account_id: str
) -> QuotaTruthViewModel:
    raw = build_quota_truth(account_vms, provider, account_id)
    freshness = raw["freshness"]

    if not raw["found"] or freshness != "fresh":
        usable = UNKNOWN_ZH
    else:
        remaining = raw["five_hour_remaining_pct"]
        if remaining is None:
            usable = UNKNOWN_ZH
        elif remaining <= 0:
            usable = "不可用（額度為 0）"
        else:
            usable = "可用"

    return QuotaTruthViewModel(
        provider=provider,
        account_id=account_id,
        found=raw["found"],
        freshness_zh=_zh(FRESHNESS_ZH, freshness),
        usable=usable,
        five_hour_used=raw["formatted_five_hour_used"],
        five_hour_remaining=raw["formatted_five_hour_remaining"],
        five_hour_reset_at=raw["formatted_five_hour_reset_at"],
        weekly_used=raw["formatted_weekly_used"],
        weekly_remaining=raw["formatted_weekly_remaining"],
        weekly_reset_at=raw["formatted_weekly_reset_at"],
        last_updated=raw["formatted_captured_at"],
    )


# =====================================================================
# Execution truth (執行真相)
# =====================================================================


@dataclass
class ExecutionTruthViewModel:
    execution_id: str
    status_zh: str
    provider: str
    account_id: str
    started_at: str
    ended_at: str
    provider_evidence_available: str


def build_execution_truth_zh(
    execution: Optional[Dict[str, Any]], now: datetime
) -> ExecutionTruthViewModel:
    """No Execution ID -> NOT_CREATED_ZH (尚未建立), never an error label:
    a Task with no Execution yet is a normal, expected state (invariant #7).
    """
    if not isinstance(execution, dict):
        return ExecutionTruthViewModel(
            execution_id=NOT_CREATED_ZH,
            status_zh=NOT_CREATED_ZH,
            provider=UNKNOWN_LABEL,
            account_id=UNKNOWN_LABEL,
            started_at=UNKNOWN_LABEL,
            ended_at="—",
            provider_evidence_available=UNKNOWN_ZH,
        )

    state = determine_execution_state(execution, now)
    evidence = execution.get("provider_evidence")
    if isinstance(evidence, dict) and evidence:
        evidence_available = "有"
    elif evidence is None:
        evidence_available = UNKNOWN_ZH
    else:
        evidence_available = "無"

    return ExecutionTruthViewModel(
        execution_id=_text(execution, "execution_id") or UNKNOWN_LABEL,
        status_zh=_zh(EXECUTION_STATE_ZH, state),
        provider=_text(execution, "provider") or UNKNOWN_LABEL,
        account_id=_text(execution, "account_id") or UNKNOWN_LABEL,
        started_at=_text(execution, "started_at") or UNKNOWN_LABEL,
        ended_at=_text(execution, "completed_at") or "—",
        provider_evidence_available=evidence_available,
    )


# =====================================================================
# Session truth (工作階段真相)
# =====================================================================


@dataclass
class SessionTruthViewModel:
    session_id: str
    status_zh: str
    provider: str
    started_at: str
    updated_at: str
    summary: str


def build_session_truth_zh(session: Optional[Dict[str, Any]]) -> SessionTruthViewModel:
    if not isinstance(session, dict):
        return SessionTruthViewModel(
            session_id=NOT_CREATED_ZH,
            status_zh=NOT_CREATED_ZH,
            provider=UNKNOWN_LABEL,
            started_at=UNKNOWN_LABEL,
            updated_at=UNKNOWN_LABEL,
            summary="—",
        )
    status = _text(session, "status")
    return SessionTruthViewModel(
        session_id=_text(session, "session_id") or UNKNOWN_LABEL,
        status_zh=_zh(SESSION_STATUS_ZH, status),
        provider=_text(session, "provider") or UNKNOWN_LABEL,
        started_at=_text(session, "started_at") or UNKNOWN_LABEL,
        updated_at=_text(session, "updated_at") or UNKNOWN_LABEL,
        summary=_text(session, "summary") or "—",
    )


# =====================================================================
# Handoff truth (交接真相)
# =====================================================================


@dataclass
class HandoffTruthViewModel:
    handoff_id: str
    from_provider: str
    to_provider: str
    reason: str
    next_action: str


def build_handoff_truth_zh(handoff: Optional[Dict[str, Any]]) -> HandoffTruthViewModel:
    if not isinstance(handoff, dict):
        return HandoffTruthViewModel(
            handoff_id=NOT_CREATED_ZH,
            from_provider=UNKNOWN_LABEL,
            to_provider=UNKNOWN_LABEL,
            reason="—",
            next_action="—",
        )
    return HandoffTruthViewModel(
        handoff_id=_text(handoff, "handoff_id") or UNKNOWN_LABEL,
        from_provider=_text(handoff, "from_provider") or UNKNOWN_LABEL,
        to_provider=_text(handoff, "to_provider") or UNKNOWN_LABEL,
        reason=_text(handoff, "reason") or UNKNOWN_LABEL,
        next_action=_text(handoff, "next_action") or UNKNOWN_LABEL,
    )


def latest_handoff(handoffs: Optional[Sequence[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The most recent handoff by created_at; None (not a fabricated one)
    when the list is empty or no record carries a usable timestamp."""
    valid = [
        h for h in (handoffs or [])
        if isinstance(h, dict) and isinstance(h.get("created_at"), str) and h["created_at"].strip()
    ]
    if not valid:
        return None
    return max(valid, key=lambda h: h["created_at"])


# =====================================================================
# Chain truth (鏈結真相): execution_unreadable / session_unreadable
# =====================================================================


@dataclass
class ChainTruthViewModel:
    execution_link_zh: str
    session_link_zh: str
    chain_state_zh: str


def build_chain_truth_zh(
    execution_id_referenced: bool,
    execution: Optional[Dict[str, Any]],
    session_id_referenced: bool,
    session: Optional[Dict[str, Any]],
    task_status: Optional[str],
) -> ChainTruthViewModel:
    """A referenced-but-unfetchable Execution/Session record must visibly
    read as broken linkage (execution_unreadable / session_unreadable),
    never silently as "no execution"/"no session" -- that would hide a real
    data problem behind the normal not-yet-created state."""
    if not execution_id_referenced:
        execution_link_zh = NOT_CREATED_ZH
    elif execution is None:
        execution_link_zh = "無法讀取（execution_unreadable）"
    else:
        execution_link_zh = "已讀取"

    if not session_id_referenced:
        session_link_zh = NOT_CREATED_ZH
    elif session is None:
        session_link_zh = "無法讀取（session_unreadable）"
    else:
        session_link_zh = "已讀取"

    broken = execution_link_zh.startswith("無法讀取") or session_link_zh.startswith("無法讀取")

    if broken:
        chain_state_zh = "鏈結中斷"
    elif task_status == "blocked":
        chain_state_zh = "已阻擋"
    elif task_status == "completed":
        chain_state_zh = "已完成"
    elif task_status in ("in_progress", "ready", "queued"):
        chain_state_zh = "進行中"
    else:
        chain_state_zh = UNKNOWN_ZH

    return ChainTruthViewModel(
        execution_link_zh=execution_link_zh,
        session_link_zh=session_link_zh,
        chain_state_zh=chain_state_zh,
    )


# =====================================================================
# Provenance truth (production identity / 版本真相)
# =====================================================================


@dataclass
class ProvenanceTruthViewModel:
    tested_sha: str
    activated_sha: str
    running_sha: str
    dashboard_sha: str
    all_match_zh: str
    detail: str


def build_provenance_truth_zh(vm: ProvenanceViewModel) -> ProvenanceTruthViewModel:
    """Never claims 一致 (aligned) unless manager.dashboard_core's own
    all_match already proved every SHA is known AND identical."""
    return ProvenanceTruthViewModel(
        tested_sha=vm.watcher_tested_sha,
        activated_sha=vm.watcher_activated_sha,
        running_sha=vm.watcher_running_sha,
        dashboard_sha=vm.dashboard_reviewed_sha,
        all_match_zh="一致" if vm.all_match else "不一致",
        detail=vm.match_detail,
    )


# =====================================================================
# Dispatch availability (no eligible provider)
# =====================================================================


def dispatch_availability_zh(recommended_provider: Optional[str], recommended_display_name: str) -> str:
    """Invariant #9: when no eligible provider exists, this must clearly
    read as "automatic dispatch is currently unavailable" -- never a blank
    or silently-omitted recommendation."""
    if recommended_provider is None:
        return "自動派工目前不可用（沒有可派工的 AI 帳號）"
    return f"可派工：{recommended_display_name}"
