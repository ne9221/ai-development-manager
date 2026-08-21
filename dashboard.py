import os
import json
import queue
import threading
import time
import urllib.request
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import pandas as pd
import streamlit as st

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords, logical_record_id
from manager.quota_reader import read_drive_status, summarize
from manager.quota_history import get_default_quota_history_store
from manager.ideas import (
    STATUS_CONFIRMED,
    STATUS_CONFLICTED,
    STATUS_CONVERTED,
    STATUS_DROPPED,
    STATUS_PENDING,
    STATUS_DISPLAY_NAMES,
    STATUS_ICONS,
    IdeaItem,
    IdeasStore,
    get_default_ideas_store,
    get_ideas_summary,
    group_ideas_by_status,
)
from manager.actions import (
    STATUS_OPEN,
    STATUS_ACKNOWLEDGED,
    STATUS_RESOLVED,
    STATUS_DISMISSED,
    TYPE_REVIEW_REQUIRED,
    TYPE_ACTION_NEEDED,
    TYPE_BLOCKED,
    TYPE_MILESTONE_REACHED,
    TYPE_INFO,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_LOW,
    ActionItem,
    ActionsStore,
    get_default_actions_store,
    derive_automatic_actions,
    get_actions_summary,
    format_waiting_duration,
)
from manager.runtime_visibility import (
    STATE_AUTO_RUNNING,
    STATE_WAITING_USER,
    STATE_BLOCKED,
    STATE_IDLE,
    STATE_AUTO_STALLED,
    STATE_UNKNOWN,
    format_elapsed_duration,
    format_activity_timestamp_and_age,
    format_duration_and_remaining_eta,
    get_latest_activity_timestamp,
    determine_ai_runtime_activity,
    compute_global_runtime_state,
    compute_next_auto_action,
)
from manager.dashboard_core import (
    parse_time,
    determine_execution_state,
    is_execution_stale,
    get_global_summary,
    map_task_board,
    build_project_detail_vm,
    build_sessions_vm,
    build_review_evidence_vm,
    build_operational_events,
    build_daily_brief_vm,
    DailyBriefViewModel,
    AccountQuotaCardViewModel,
    ServiceHealthViewModel,
    parse_scheduled_task_health,
    build_session_center_health,
    UNKNOWN_LABEL,
    DISPATCH_STATE_RUNNING,
    build_dispatch_truth_row,
    compute_visible_dispatch_gate,
)

st.set_page_config(
    page_title="ADM 營運儀表板",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stApp {
        background-color: #0d1117;
        color: #c9d1d9;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    [data-testid="stSidebar"] { background: #101722 !important; border-right: 1px solid #30363d; }
    [data-testid="stSidebar"] * { color: #d8e1ea; }
    [data-testid="stSidebar"] [role="radiogroup"] { gap: .18rem; padding-right: .45rem; }
    [data-testid="stSidebar"] label { min-width: 0; overflow: visible; white-space: normal; }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: #b7c3d0; }
    [data-testid="stHeader"] { background: #0d1117; }
    [data-testid="stMainBlockContainer"] { padding-top: 1.25rem; }
    .fleet-anchor { height: 0; }
    [data-testid="stColumn"]:has(.fleet-anchor) { position: sticky; top: 1rem; align-self: flex-start; }
    .glass-card {
        background: rgba(22, 27, 34, 0.85);
        border: 1px solid #30363d;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 14px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }
    .glass-card-dimmed {
        background: rgba(18, 22, 28, 0.6);
        border: 1px solid #21262d;
        border-radius: 10px;
        padding: 14px 18px;
        margin-bottom: 12px;
        color: #8b949e;
    }
    .recommendation-card {
        background: linear-gradient(135deg, rgba(22, 27, 34, 0.95), rgba(30, 41, 59, 0.9));
        border: 1px solid #388bfd;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 16px;
    }
    .runtime-state-banner {
        background: rgba(22, 27, 34, 0.95);
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 12px 18px;
        margin-bottom: 14px;
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .badge-ok {
        color: #7ee787;
        background: #193c2c;
        padding: 3px 9px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
    }
    .badge-warn {
        color: #ffa657;
        background: #482914;
        padding: 3px 9px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
    }
    .badge-err {
        color: #ff7b72;
        background: #49181d;
        padding: 3px 9px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
    }
    .badge-high {
        color: #ffffff;
        background: #b91c1c;
        padding: 2px 7px;
        border-radius: 5px;
        font-size: 11px;
        font-weight: 700;
    }
    .badge-med {
        color: #ffffff;
        background: #b45309;
        padding: 2px 7px;
        border-radius: 5px;
        font-size: 11px;
        font-weight: 700;
    }
    .badge-low {
        color: #ffffff;
        background: #1e3a8a;
        padding: 2px 7px;
        border-radius: 5px;
        font-size: 11px;
        font-weight: 700;
    }
    .priority-high { color: #ff7b72; font-weight: bold; }
    .priority-medium { color: #d29922; font-weight: bold; }
    .priority-low { color: #7ee787; }
</style>
""", unsafe_allow_html=True)


def query_scheduled_task_raw(task_name: str) -> Optional[str]:
    try:
        cmd = ["schtasks.exe", "/Query", "/TN", task_name, "/FO", "LIST"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            return res.stdout
        return res.stdout if res.stdout else res.stderr
    except Exception:
        return None


def query_session_center_raw(port: int = 8765) -> tuple[bool, Optional[dict]]:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=1) as resp:
            if resp.status == 200:
                try:
                    s_req = urllib.request.Request(f"http://127.0.0.1:{port}/api/session")
                    with urllib.request.urlopen(s_req, timeout=1) as s_resp:
                        if s_resp.status == 200:
                            data = json.loads(s_resp.read().decode("utf-8"))
                            return True, data
                except Exception:
                    pass
                return True, None
    except Exception:
        return False, None
    return False, None


def query_dashboard_raw(port: int = 8501) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/_stcore/health")
        with urllib.request.urlopen(req, timeout=1) as resp:
            if resp.status == 200:
                return True, f"ONLINE :{port}"
    except Exception:
        return False, f"UNAVAILABLE :{port}"
    return False, f"UNAVAILABLE :{port}"


def load_infra_health(drive_service_inst: Any = None) -> List[ServiceHealthViewModel]:
    dash_ok, dash_detail = query_dashboard_raw(8501)
    dash_vm = ServiceHealthViewModel(
        name="Dashboard (:8501)",
        found=dash_ok,
        detail=dash_detail,
        status_label="Online" if dash_ok else "Offline"
    )

    sc_listening, sc_session = query_session_center_raw(8765)
    sc_vm = build_session_center_health(sc_listening, sc_session)

    watcher_raw = query_scheduled_task_raw("AI Development Manager - Command Watcher")
    watcher_vm = parse_scheduled_task_health("Command Watcher", watcher_raw)

    supervisor_raw = query_scheduled_task_raw("AI Development Manager - Session Center Supervisor")
    supervisor_vm = parse_scheduled_task_health("Supervisor", supervisor_raw)

    drive_vm = ServiceHealthViewModel(
        name="Google Drive SSOT",
        found=bool(drive_service_inst),
        detail="Connected" if drive_service_inst else "Unavailable / Local Fallback",
        status_label="Online" if drive_service_inst else "Offline"
    )

    return [dash_vm, sc_vm, watcher_vm, supervisor_vm, drive_vm]


def bounded_read(callback, timeout_seconds: float = 3.0, retries: int = 1):
    """Return a canonical read result without allowing it to block page bootstrap."""
    deadline = time.monotonic() + timeout_seconds
    for _ in range(retries + 1):
        result = queue.Queue(maxsize=1)
        def run():
            try:
                result.put((True, callback()))
            except Exception as exc:
                result.put((False, exc))
        threading.Thread(target=run, daemon=True).start()
        try:
            ok, value = result.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return False, TimeoutError(f"canonical Drive read timed out after {timeout_seconds:g}s")
        if ok or time.monotonic() >= deadline:
            return ok, value
    return False, value

now = datetime.now(timezone.utc)
all_warnings = []

drive_service = None
try:
    drive_service = build_service()
except Exception as e:
    all_warnings.append(f"Google Drive token/service unavailable: {e}. Running in local/degraded mode.")

store = DriveRecords(drive_service) if drive_service else None
ideas_store = get_default_ideas_store(drive_service=drive_service)
actions_store = get_default_actions_store(drive_service=drive_service)

all_projects = []
all_tasks = []
all_executions = []
all_commands = []
all_handoffs = []
active_executions = []
active_executions_dict = {}
handoffs_dict = {}
canonical_records_available = bool(store)
records_are_last_known = False
last_successful_sync = None

if store:
    projects_ok, projects_result = bounded_read(store.dashboard_records)
    if projects_ok:
        all_projects = projects_result["projects"]
        all_tasks = projects_result["tasks"]
        all_commands = projects_result["commands"]
        all_executions = projects_result["executions"]
        all_handoffs = projects_result["handoffs"]
        last_successful_sync = now.isoformat()
        st.session_state["dashboard_last_known"] = (last_successful_sync, projects_result)
    else:
        canonical_records_available = False
        all_warnings.append(f"Failed to list projects: {projects_result}")
        last_known = st.session_state.get("dashboard_last_known")
        if last_known:
            last_successful_sync, records = last_known
            all_projects, all_tasks = records["projects"], records["tasks"]
            all_commands, all_executions = records["commands"], records["executions"]
            all_handoffs, records_are_last_known = records["handoffs"], True

for exec_data in all_executions:
    if exec_data.get("status") in ["running", "reserved"]:
        active_executions.append(exec_data)
        active_executions_dict[(exec_data.get("project_id"), exec_data.get("task_id"))] = exec_data
for ho_data in all_handoffs:
    if ho_data.get("task_id"):
        handoffs_dict[(ho_data.get("project_id"), ho_data["task_id"])] = ho_data
records_available = canonical_records_available or records_are_last_known

drive_status_raw = {"providers": []}
if drive_service:
    try:
        drive_status_raw = read_drive_status(drive_service)
    except Exception as e:
        all_warnings.append(f"Failed to read Drive status payload: {e}")

quota_history_store = None
try:
    quota_history_store = get_default_quota_history_store()
    quota_history_store.load()
except Exception as e:
    all_warnings.append(f"Failed to load quota history store: {e}")

summary = summarize(drive_status_raw, now=now)
daily_brief_vm: DailyBriefViewModel = build_daily_brief_vm(drive_status_raw, quota_history_store, now=now)

infra_health_list = load_infra_health(drive_service)
all_ideas = ideas_store.list_ideas()
ideas_summary = get_ideas_summary(all_ideas)
conflicted_ideas = [i for i in all_ideas if i.is_conflicted or i.status == STATUS_CONFLICTED]

persisted_actions = actions_store.list_actions()
derived_candidates = derive_automatic_actions(
    all_tasks=all_tasks,
    active_executions=all_executions,
    ideas_conflicted=conflicted_ideas,
    infra_health_list=infra_health_list,
    persisted_actions=persisted_actions,
    commands=all_commands,
    now=now,
)
all_actions = actions_store.reconcile_automatic_actions(derived_candidates)
actions_summary = get_actions_summary(all_actions)

global_runtime_state, global_badge_class, global_state_desc = compute_global_runtime_state(
    active_executions=active_executions,
    all_tasks=all_tasks,
    open_actions=all_actions,
    infra_health_list=infra_health_list,
    now=now,
)
if not canonical_records_available:
    global_runtime_state, global_badge_class, global_state_desc = (
        STATE_UNKNOWN, "badge-warn", "Canonical project/task records unavailable; Drive request timed out or failed."
    )
next_auto_action_str = compute_next_auto_action(all_tasks, active_executions, all_actions, daily_brief_vm)

NAV_OVERVIEW = "總覽"
NAV_ACTION_CENTER = "操作中心"
NAV_PROJECTS = "專案"
NAV_TASKS = "任務"
NAV_DISPATCH_TRUTH = "派工真相"
NAV_IDEAS = "構想"
NAV_AI_SESSIONS = "AI 工作階段"
NAV_REVIEWS = "審查"
NAV_QUOTA = "用量與 AI Fleet"
NAV_LOGS = "日誌"
NAV_SETTINGS = "設定"

NAV_PAGES = [
    NAV_OVERVIEW,
    NAV_ACTION_CENTER,
    NAV_PROJECTS,
    NAV_TASKS,
    NAV_DISPATCH_TRUTH,
    NAV_IDEAS,
    NAV_AI_SESSIONS,
    NAV_REVIEWS,
    NAV_QUOTA,
    NAV_LOGS,
    NAV_SETTINGS,
]

if "nav_selection" not in st.session_state:
    st.session_state["nav_selection"] = NAV_OVERVIEW

st.sidebar.title("🤖 AI 開發管理員")
selected_nav = st.sidebar.radio("導覽", NAV_PAGES, key="nav_selection")

st.sidebar.markdown("---")
if actions_summary["need_user_action"] > 0:
    st.sidebar.error(f"🚨 待你處理：{actions_summary['need_user_action']} 項")
elif actions_summary["open"] > 0:
    st.sidebar.warning(f"⚡ 操作中心：{actions_summary['open']} 項待處理")
else:
    st.sidebar.caption("✅ 操作中心：目前無需人工處理（不代表系統健康）")

conflict_tag = f" · ⚠️ {ideas_summary['conflicted']} 衝突" if ideas_summary.get('conflicted', 0) > 0 else ""
st.sidebar.caption(f"💡 構想：{ideas_summary['pending']} 待立案 · {ideas_summary['confirmed']} 已確認{conflict_tag}")
st.sidebar.caption(f"⚡ 執行中任務：{len(active_executions) if canonical_records_available else '無法取得'}")
if records_are_last_known:
    st.sidebar.warning(f"⚠️ 顯示上次成功同步資料（{last_successful_sync}）；非目前狀態。")

def render_overview_page():
    st.title("🎯 營運總覽")

    # Section 1: Global Runtime State Banner
    st.markdown(f"""
    <div class="runtime-state-banner">
        <div>
            <span style="font-size:13px; color:#b7c3d0; margin-right:8px;">ADM 整體執行狀態：</span>
            <span class="{global_badge_class}" style="font-size:14px;">{global_runtime_state}</span>
            <span style="font-size:13px; margin-left:10px; color:#c9d1d9;">{global_state_desc}</span>
        </div>
        <div style="font-size:13px; color:#8b949e;">
            <b>下一個自動動作：</b> <span style="color:#79c0ff;">{next_auto_action_str}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Section 2: Action Center Quick Alert Bar
    col_act1, col_act2, col_act3, col_act4 = st.columns([1.5, 1.5, 1.5, 2.5])
    with col_act1:
        st.metric("🚨 需人工處理", actions_summary["need_user_action"])
    with col_act2:
        st.metric("📝 需審查", actions_summary["review_required"])
    with col_act3:
        st.metric("⚠️ 受阻項目", actions_summary["blocked"])
    with col_act4:
        if actions_summary["need_user_action"] > 0:
            if st.button("👉 前往操作中心", key="btn_goto_actions_top", use_container_width=True):
                st.session_state["nav_selection"] = NAV_ACTION_CENTER
                st.rerun()
        else:
            st.caption("✅ 目前無需人工處理；此訊息不代表系統健康。")

    st.markdown("---")

    # Section 3: Technical Health Status Bar
    health_cols = st.columns(len(infra_health_list))
    for idx, h in enumerate(infra_health_list):
        with health_cols[idx]:
            if h.status_label == "Online":
                badge = f"<span class='badge-ok'>{h.detail.upper() if h.detail else 'ONLINE'}</span>"
            elif h.status_label == "Offline":
                badge = f"<span class='badge-err'>{h.detail.upper() if h.detail else 'OFFLINE'}</span>"
            else:
                badge = f"<span class='badge-warn'>{h.detail.upper() if h.detail else 'UNKNOWN'}</span>"
            st.markdown(f"**{h.name}**: {badge}", unsafe_allow_html=True)

    st.markdown("---")

    col_left, col_right = st.columns([3, 2])

    with col_left:
        st.subheader("📁 專案、里程碑與任務")
        if not records_available:
            st.warning("無法取得 — 無法讀取 Drive 的 canonical 專案／任務記錄；不會推論為空結果。")
        elif not all_projects:
            st.info("目前沒有可用的專案記錄。請建立專案後再追蹤。")
        else:
            for proj in all_projects:
                p_id = proj.get("project_id", "—")
                p_title = proj.get("title", p_id)
                proj_tasks = [t for t in all_tasks if t.get("project_id") == p_id]
                active_task_in_proj = next((t for t in proj_tasks if (p_id, t.get("task_id")) in active_executions_dict), None)
                status_text = active_task_in_proj.get("status", "—") if active_task_in_proj else "無法取得／未記錄"
                status_badge = "badge-warn" if not active_task_in_proj else "badge-ok"
                next_step = active_task_in_proj.get("next_action") if active_task_in_proj else "無法取得／未記錄"

                st.markdown(f"""
                <div class="glass-card">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <h4 style="margin:0;">{p_title} <small style="color:#8b949e;">({p_id})</small></h4>
                        <span class="{status_badge}">{status_text}</span>
                    </div>
                    <p style="margin:8px 0 4px 0; font-size:14px;"><b>專案總進度：</b>無法取得（未記錄 canonical 百分比；不以任務數推算）</p>
                    <p style="margin:0; font-size:13px; color:#b7c3d0;"><b>下一步：</b>{next_step or '無法取得／未記錄'}</p>
                </div>
                """, unsafe_allow_html=True)

        st.subheader("⚠️ 阻礙與待注意事項")
        stale_execs = [exe for exe in active_executions if is_execution_stale(exe, now)]
        blocked_tasks = [t for t in all_tasks if t.get("status") in ["blocked", "attention"]]

        if not records_available:
            st.warning("無法取得 — canonical 專案／任務記錄不可用，無法判定任務阻礙。")
        elif not stale_execs and not blocked_tasks and not conflicted_ideas and actions_summary["blocked"] == 0:
            st.info("✅ 目前未記錄任何阻礙；此訊息不代表系統健康。")
        else:
            for exe in stale_execs:
                st.markdown(f"""
                <div class="glass-card" style="border-color:#ff7b72;">
                    <span class="badge-err">STALE EXECUTION</span> <b>{exe.get('project_id')} / {exe.get('task_id')}</b>
                    <p style="margin:4px 0; font-size:13px;">AI Provider: {exe.get('provider')} | Last event: {exe.get('last_provider_event', 'none')} | Heartbeat timed out.</p>
                </div>
                """, unsafe_allow_html=True)
            for t in blocked_tasks:
                st.markdown(f"""
                <div class="glass-card" style="border-color:#d29922;">
                    <span class="badge-warn">TASK BLOCKED</span> <b>{t.get('project_id')} / {t.get('task_id')}</b> - {t.get('title')}
                    <p style="margin:4px 0; font-size:13px;">Reason: {t.get('next_action', 'Awaiting manual review')}</p>
                </div>
                """, unsafe_allow_html=True)
            for ci in conflicted_ideas:
                st.markdown(f"""
                <div class="glass-card" style="border-color:#ff7b72;">
                    <span class="badge-err">SSOT CONFLICT</span> <b>Idea {ci.idea_id}</b> - {ci.title}
                    <p style="margin:4px 0; font-size:13px;">Conflicting duplicate copies exist in Drive SSOT. Mutations locked.</p>
                </div>
                """, unsafe_allow_html=True)

    with col_right:
        st.markdown('<div class="fleet-anchor"></div>', unsafe_allow_html=True)
        st.subheader("⚡ AI Fleet 與執行狀態")
        providers_data = summary.get("providers", [])
        accounts_data = summary.get("accounts", [])
        
        accounts_by_provider = {}
        for acc in accounts_data:
            accounts_by_provider.setdefault(acc.get("provider"), []).append(acc)

        for prov in providers_data:
            prov_id = prov.get("provider")
            prov_name = prov.get("display_name", "Unknown Provider")
            matched_accounts = accounts_by_provider.get(prov_id) or prov.get("accounts")
            
            if matched_accounts:
                for acc in matched_accounts:
                    acc_id = acc.get("account_id")
                    acc_title = f"{prov_name} (Account {acc_id})"
                    st.markdown(f"#### {acc_title}")
                    
                    matched_exe = next((
                        e for e in active_executions
                        if e.get("provider") == prov_id and e.get("account_id") == acc_id
                    ), None)

                    if matched_exe:
                        fleet_state, badge_class, state_exp = determine_ai_runtime_activity(matched_exe, now)
                        curr_task_str = f"{matched_exe.get('project_id')} / {matched_exe.get('task_id')}"
                        event_str = matched_exe.get("last_provider_event") or "Processing task..."
                        
                        task_snap = matched_exe.get("task_snapshot", {})
                        model_str = task_snap.get("model") or matched_exe.get("model") or "—"
                        mode_str = matched_exe.get("mode") or task_snap.get("mode") or "—"
                        effort_str = matched_exe.get("effort") or task_snap.get("effort") or "—"
                        sess_id = matched_exe.get("provider_session_id") or "—"

                        start_ts = matched_exe.get("started_at") or matched_exe.get("reserved_at")
                        started_display = start_ts[:16].replace("T", " ") if start_ts else "Unknown"
                        elapsed_display = format_elapsed_duration(start_ts, now)

                        act_ts, act_src = get_latest_activity_timestamp(matched_exe)
                        activity_display = format_activity_timestamp_and_age(act_ts, now)

                        exp_mins = task_snap.get("expected_minutes")
                        exp_total_disp, est_rem_disp = format_duration_and_remaining_eta(exp_mins, start_ts, now)
                        eta_line = f" | <b>Expected:</b> {exp_total_disp}" if exp_total_disp != "—" else ""
                        if est_rem_disp != "—":
                            eta_line += f" (<b>Est. remaining:</b> {est_rem_disp})"

                        st.markdown(f"""
                        <div class="glass-card">
                            <div style="display:flex; justify-content:space-between; align-items:center;">
                                <b>{acc_title}</b>
                                <span class="{badge_class}">{fleet_state}</span>
                            </div>
                            <p style="margin:6px 0 2px 0; font-size:13px;"><b>任務：</b> {curr_task_str}</p>
                            <p style="margin:0 0 2px 0; font-size:12px; color:#8b949e;"><b>Model/Mode:</b> {model_str} ({mode_str}/{effort_str}) | <b>Session:</b> <code>{sess_id}</code></p>
                            <p style="margin:0 0 2px 0; font-size:12px;"><b>Started:</b> {started_display} · <b>Elapsed:</b> {elapsed_display}</p>
                            <p style="margin:0 0 2px 0; font-size:12px;"><b>Last Activity ({act_src}):</b> {activity_display}{eta_line}</p>
                            <p style="margin:2px 0 0 0; font-size:12px; color:#58a6ff;"><i>Current Step: {event_str}</i></p>
                        </div>
                        """, unsafe_allow_html=True)
                    else:
                        fleet_state = "UNKNOWN" if not canonical_records_available else "IDLE"
                        badge_class = "badge-warn"
                        curr_task_str = "無法確認執行任務" if not canonical_records_available else "目前未指派執行中任務"
                        event_str = "canonical 專案／任務資料不可用" if not canonical_records_available else "等待下一次派送週期"

                        st.markdown(f"""
                        <div class="glass-card">
                            <div style="display:flex; justify-content:space-between; align-items:center;">
                                <b>{acc_title}</b>
                                <span class="{badge_class}">{fleet_state}</span>
                            </div>
                            <p style="margin:6px 0 2px 0; font-size:13px;"><b>Task:</b> {curr_task_str}</p>
                            <p style="margin:0 0 4px 0; font-size:12px; color:#8b949e;"><i>{event_str}</i></p>
                        </div>
                        """, unsafe_allow_html=True)

                    if acc.get("stale"):
                        st.warning(f"⚠️ 資料過期：{acc_title} 沒有最近的狀態更新。")

                    rem_pct = acc.get("remaining_percent")
                    if rem_pct is None:
                        w = next((w for w in acc.get("windows", []) if w.get("remaining_percent") is not None), None)
                        if w:
                            rem_pct = w.get("remaining_percent")

                    if rem_pct is not None:
                        st.progress(max(0.0, min(1.0, float(rem_pct) / 100.0)))
                    else:
                        st.info("用量百分比無法取得")
            else:
                st.markdown(f"#### {prov_name}")
                matched_exe = next((
                    e for e in active_executions
                    if e.get("provider") == prov_id
                ), None)

                if matched_exe:
                    fleet_state, badge_class, state_exp = determine_ai_runtime_activity(matched_exe, now)
                    curr_task_str = f"{matched_exe.get('project_id')} / {matched_exe.get('task_id')}"
                    event_str = matched_exe.get("last_provider_event") or "Processing task..."

                    task_snap = matched_exe.get("task_snapshot", {})
                    model_str = task_snap.get("model") or matched_exe.get("model") or "—"
                    mode_str = matched_exe.get("mode") or task_snap.get("mode") or "—"
                    effort_str = matched_exe.get("effort") or task_snap.get("effort") or "—"
                    sess_id = matched_exe.get("provider_session_id") or "—"

                    start_ts = matched_exe.get("started_at") or matched_exe.get("reserved_at")
                    started_display = start_ts[:16].replace("T", " ") if start_ts else "Unknown"
                    elapsed_display = format_elapsed_duration(start_ts, now)

                    act_ts, act_src = get_latest_activity_timestamp(matched_exe)
                    activity_display = format_activity_timestamp_and_age(act_ts, now)

                    exp_mins = task_snap.get("expected_minutes")
                    exp_total_disp, est_rem_disp = format_duration_and_remaining_eta(exp_mins, start_ts, now)
                    eta_line = f" | <b>Expected:</b> {exp_total_disp}" if exp_total_disp != "—" else ""
                    if est_rem_disp != "—":
                        eta_line += f" (<b>Est. remaining:</b> {est_rem_disp})"

                    st.markdown(f"""
                    <div class="glass-card">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <b>{prov_name}</b>
                            <span class="{badge_class}">{fleet_state}</span>
                        </div>
                        <p style="margin:6px 0 2px 0; font-size:13px;"><b>任務：</b> {curr_task_str}</p>
                        <p style="margin:0 0 2px 0; font-size:12px; color:#8b949e;"><b>Model/Mode:</b> {model_str} ({mode_str}/{effort_str}) | <b>Session:</b> <code>{sess_id}</code></p>
                        <p style="margin:0 0 2px 0; font-size:12px;"><b>Started:</b> {started_display} · <b>Elapsed:</b> {elapsed_display}</p>
                        <p style="margin:0 0 2px 0; font-size:12px;"><b>Last Activity ({act_src}):</b> {activity_display}{eta_line}</p>
                        <p style="margin:2px 0 0 0; font-size:12px; color:#58a6ff;"><i>Current Step: {event_str}</i></p>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    fleet_state = "UNKNOWN" if not canonical_records_available else "IDLE"
                    badge_class = "badge-warn"
                    curr_task_str = "無法確認執行任務" if not canonical_records_available else "目前未指派執行中任務"
                    event_str = "canonical 專案／任務資料不可用" if not canonical_records_available else "等待下一次派送週期"

                    st.markdown(f"""
                    <div class="glass-card">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <b>{prov_name}</b>
                            <span class="{badge_class}">{fleet_state}</span>
                        </div>
                        <p style="margin:6px 0 2px 0; font-size:13px;"><b>Task:</b> {curr_task_str}</p>
                        <p style="margin:0 0 4px 0; font-size:12px; color:#8b949e;"><i>{event_str}</i></p>
                    </div>
                    """, unsafe_allow_html=True)

                if prov.get("stale"):
                    st.warning(f"⚠️ 資料過期：{prov_name} 沒有最近的狀態更新。")

                rem_pct = prov.get("remaining_percent")
                if rem_pct is None:
                    w = next((w for w in prov.get("windows", []) if w.get("remaining_percent") is not None), None)
                    if w:
                        rem_pct = w.get("remaining_percent")

                if rem_pct is not None:
                    st.progress(max(0.0, min(1.0, float(rem_pct) / 100.0)))
                else:
                    st.info("用量百分比無法取得")

        st.subheader("💡 構想清單")
        st.markdown(f"""
        <div class="recommendation-card">
            <h4 style="margin:0 0 6px 0;">構想（共 {ideas_summary['total']} 項）</h4>
            <p style="margin:0 0 10px 0; font-size:14px; color:#c9d1d9;">
                <b>待立案 {ideas_summary['pending']}</b> · <b>已確認 {ideas_summary['confirmed']}</b>
                <br><small style="color:#b7c3d0;">(已立案 {ideas_summary['converted']} · 已放棄 {ideas_summary['dropped']})</small>
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("👉 前往構想中心", key="btn_goto_ideas", use_container_width=True):
            st.session_state["nav_selection"] = NAV_IDEAS
            st.rerun()

    if active_executions:
        st.markdown("---")
        st.subheader("🔄 Active Executions Table")
        exec_rows = []
        for exe in active_executions:
            p_id = exe.get("project_id", "—")
            t_id = exe.get("task_id", "—")
            provider = exe.get("provider", "—")
            account = exe.get("account_id") or "—"
            task_snapshot = exe.get("task_snapshot", {})
            model = task_snapshot.get("model", "—")
            mode = exe.get("mode") or task_snapshot.get("mode") or "—"
            effort = exe.get("effort") or task_snapshot.get("effort") or "—"
            session_id = exe.get("provider_session_id") or "—"
            ui_state = determine_execution_state(exe, now)
            progress = exe.get("last_provider_event") or "—"
            hb_at = exe.get("heartbeat_at") or "—"
            start_time = parse_time(exe.get("started_at") or exe.get("reserved_at"))
            elapsed_str = format_elapsed_duration(start_time, now)
            expected_str = f"{task_snapshot.get('expected_minutes', '—')} min"
            is_stale = is_execution_stale(exe, now)
            attention = "⚠️ ATTENTION" if is_stale else "✅ OK"
            exec_rows.append({
                "Project": p_id,
                "Task": t_id,
                "AI Provider": provider,
                "Account": account,
                "Model/Mode/Effort": f"{model} / {mode} / {effort}",
                "Provider Session": session_id,
                "State": ui_state.upper(),
                "Current Progress": progress,
                "Heartbeat": hb_at,
                "Elapsed": elapsed_str,
                "Expected": expected_str,
                "Health": attention
            })
        st.table(pd.DataFrame(exec_rows))

def render_action_center_page():
    st.title("🚨 操作中心")
    st.caption("當 AI Fleet 無法自動繼續或需要人工決策／驗收時，集中在此記錄與回應，避免任務無聲停滯。")

    if actions_store.is_degraded:
        st.warning("⚠️ 操作中心：本機快取／降級模式（Google Drive SSOT 未連線），操作項目為唯讀。")
    else:
        st.caption("✅ 操作中心：Google Drive SSOT 已連線。")

    if actions_store.last_error:
        st.error(f"{actions_store.last_error}")

    # Summary Metrics Row
    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
    with m_col1:
        st.metric("🚨 需人工處理", actions_summary["need_user_action"])
    with m_col2:
        st.metric("📝 需審查", actions_summary["review_required"])
    with m_col3:
        st.metric("⚠️ 受阻項目", actions_summary["blocked"])
    with m_col4:
        st.metric("📦 操作紀錄", actions_summary["history"])

    st.markdown("---")

    open_actions_list = [a for a in all_actions if a.status == STATUS_OPEN]
    ack_actions_list = [a for a in all_actions if a.status == STATUS_ACKNOWLEDGED]
    history_actions_list = [a for a in all_actions if a.status in [STATUS_RESOLVED, STATUS_DISMISSED]]

    # Category 1: Needs Attention (Open Items)
    with st.expander(f"🚨 需注意／待處理 ({len(open_actions_list)})", expanded=True):
        if not open_actions_list:
            st.info("✅ 目前沒有待處理事項；這不代表所有任務或系統均健康。")
        for item in open_actions_list:
            sev_badge = f"<span class='badge-{item.severity[:4].lower()}'>{item.severity.upper()}</span>"
            waiting_dur = format_waiting_duration(item.waiting_since, now)
            need_action_str = "YES (需人工介入)" if item.need_user_action else "NO (仅需知情)"

            st.markdown(f"""
            <div class="glass-card" style="border-left: 4px solid {'#ff7b72' if item.severity == 'high' else '#ffa657'};">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0;">{item.title} <small style="color:#8b949e;">({item.action_id})</small></h4>
                    <div>
                        <span class="badge-warn">{item.type}</span>
                        {sev_badge}
                    </div>
                </div>
                <p style="margin:8px 0 4px 0; font-size:14px;"><b>Reason:</b> {item.reason}</p>
                <p style="margin:0 0 4px 0; font-size:13px; color:#ff7b72;"><b>Impact:</b> {item.impact}</p>
                <p style="margin:0 0 6px 0; font-size:13px; color:#58a6ff;"><b>Recommended Next Step:</b> {item.recommended_next_step}</p>
                <p style="margin:0; font-size:12px; color:#8b949e;">
                    <b>Project:</b> <code>{item.project_id}</code> |
                    <b>Task:</b> <code>{item.task_id or '—'}</code> |
                    <b>Waiting Since:</b> {item.waiting_since[:16].replace('T', ' ') if item.waiting_since else '—'} (<b>Duration:</b> {waiting_dur}) |
                    <b>Need User Action:</b> <b>{need_action_str}</b>
                </p>
            </div>
            """, unsafe_allow_html=True)

            col_b1, col_b2, col_b3 = st.columns([1.5, 1.5, 3])
            with col_b1:
                if st.button("👀 已知悉", key=f"btn_ack_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.acknowledge_action(item.action_id, note=f"Acknowledged by user at {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Acknowledge failed: {e}")
            with col_b2:
                if st.button("✅ 標記為已解決", key=f"btn_res_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.resolve_action(item.action_id, note="Resolved by user via Action Center")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Resolve failed: {e}")
            with col_b3:
                if st.button("✖ 忽略", key=f"btn_dsm_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.dismiss_action(item.action_id, note="Dismissed by user")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Dismiss failed: {e}")

    # Category 2: Acknowledged Items
    with st.expander(f"👀 已知悉、待完成 ({len(ack_actions_list)})", expanded=bool(ack_actions_list)):
        if not ack_actions_list:
            st.write("目前沒有已知悉事項。")
        for item in ack_actions_list:
            sev_badge = f"<span class='badge-{item.severity[:4].lower()}'>{item.severity.upper()}</span>"
            waiting_dur = format_waiting_duration(item.waiting_since, now)

            st.markdown(f"""
            <div class="glass-card-dimmed">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0; color:#c9d1d9;">👀 {item.title} <small>({item.action_id})</small></h4>
                    {sev_badge}
                </div>
                <p style="margin:6px 0 2px 0; font-size:13px;"><b>Recommended Step:</b> {item.recommended_next_step}</p>
                <p style="margin:0; font-size:12px; color:#8b949e;">
                    <b>Acknowledged At:</b> {item.acknowledged_at or '—'} |
                    <b>Waiting Duration:</b> {waiting_dur} |
                    <b>Note:</b> {item.resolution_note or '—'}
                </p>
            </div>
            """, unsafe_allow_html=True)

            col_b1, col_b2, _ = st.columns([1.5, 1.5, 3])
            with col_b1:
                if st.button("✅ 標記為已解決", key=f"btn_res_ack_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.resolve_action(item.action_id, note="Resolved after acknowledgment")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Resolve failed: {e}")
            with col_b2:
                if st.button("✖ 忽略", key=f"btn_dsm_ack_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.dismiss_action(item.action_id, note="Dismissed after acknowledgment")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Dismiss failed: {e}")

    # Category 3: History (Resolved & Dismissed) - Default Collapsed
    with st.expander(f"📜 操作紀錄 ({len(history_actions_list)})", expanded=False):
        st.caption("ℹ️ *已解決與已忽略的歷史事項保留完整稽核記錄。*")
        if not history_actions_list:
            st.write("目前沒有歷史歸檔事項。")
        for item in history_actions_list:
            badge_type = "badge-ok" if item.status == STATUS_RESOLVED else "badge-err"
            resolved_ts = item.resolved_at or item.dismissed_at or "—"
            st.markdown(f"""
            <div class="glass-card-dimmed">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0; color:#8b949e;">{item.title} <small>({item.action_id})</small></h4>
                    <span class="{badge_type}">{item.status.upper()}</span>
                </div>
                <p style="margin:4px 0 0 0; font-size:12px; color:#8b949e;">
                    <b>Finished At:</b> {resolved_ts} |
                    <b>Project:</b> {item.project_id} |
                    <b>Note:</b> {item.resolution_note or '—'}
                </p>
            </div>
            """, unsafe_allow_html=True)


def render_ideas_page():
    st.title("💡 構想中心")
    st.caption("保存平時零散提出的構想（「以後要做／之後加／先記著」），在正式進入專案執行前完成確認與立案。")

    if ideas_store.is_degraded:
        st.warning("⚠️ Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")
    else:
        st.caption("✅ Ideas Store: Google Drive SSOT Connected")

    if ideas_store.last_error:
        st.error(f"{ideas_store.last_error}")

    # Quick Add Idea Expander
    with st.expander("➕ 快速記錄構想", expanded=False):
        with st.form("form_add_idea", clear_on_submit=True):
            col_t1, col_t2 = st.columns([3, 1])
            with col_t1:
                new_title = st.text_input("標題（構想簡述）", placeholder="例如：新增 Webhook 警示機器人")
            with col_t2:
                new_priority = st.selectbox("優先級", ["high", "medium", "low"], index=1)

            new_desc = st.text_area("說明／背景", placeholder="詳細背景、為什麼要做、可能方案……")
            new_proj = st.text_input("建議專案 ID（選填）", value="ai-development-manager")
            new_source = st.text_input("來源", value="使用者對話")

            if st.form_submit_button("儲存構想（存入待立案）"):
                if ideas_store.is_degraded:
                    st.error("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")
                elif new_title.strip():
                    item = IdeaItem(
                        idea_id=f"IDEA-{int(datetime.now().timestamp())}",
                        title=new_title.strip(),
                        description=new_desc.strip(),
                        status=STATUS_PENDING,
                        priority=new_priority,
                        project_id=new_proj.strip() or "Unassigned",
                        created_at=datetime.now(timezone.utc).isoformat(),
                        source=new_source.strip(),
                    )
                    try:
                        ideas_store.add_idea(item)
                        st.success(f"構想「{new_title}」已加入待立案。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to add idea: {e}")
                else:
                    st.error("標題不得為空。")

    grouped = group_ideas_by_status(ideas_store.list_ideas())

    # Conflicted Ideas Warning Section (Fail Closed)
    conflicted_list = grouped.get(STATUS_CONFLICTED, [])
    if conflicted_list:
        with st.expander(f"⚠️ 衝突鎖定中的構想 ({len(conflicted_list)})", expanded=True):
            st.error("偵測到下列構想在 Drive SSOT 有多份衝突記錄。為確保真實性，禁止自動合併或選擇版本，所有變更已鎖定；請在雲端排查修復後重新整理。")
            for c_item in conflicted_list:
                st.markdown(f"""
                <div class="glass-card" style="border: 1px solid #ff7b72;">
                    <h4 style="margin:0; color:#ff7b72;">⚠️ {c_item.title} <small>({c_item.idea_id})</small></h4>
                    <p style="margin:6px 0; font-size:13px;">{c_item.description}</p>
                    <span class="badge-err">MUTATIONS LOCKED</span>
                </div>
                """, unsafe_allow_html=True)

    # Category 1: 待立案 (Default Expanded)
    pending_list = grouped[STATUS_PENDING]
    with st.expander(f"▼ 待立案 ({len(pending_list)})", expanded=True):
        if not pending_list:
            st.info("目前沒有待立案構想。")
        for item in pending_list:
            st.markdown(f"""
            <div class="glass-card">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0;">💡 {item.title} <small style="color:#8b949e;">({item.idea_id})</small></h4>
                    <span class="priority-{item.priority}">PRIORITY {item.priority.upper()}</span>
                </div>
                <p style="margin:8px 0 4px 0;">{item.description or '—'}</p>
                <p style="margin:0; font-size:12px; color:#8b949e;">
                    <b>Proposed Project:</b> <code>{item.project_id}</code> |
                    <b>Created:</b> {item.created_at or '—'} |
                    <b>Source:</b> {item.source}
                </p>
                {f'<p style="margin:4px 0 0 0; font-size:12px; color:#58a6ff;"><b>Note:</b> {item.decision_note}</p>' if item.decision_note else ''}
            </div>
            """, unsafe_allow_html=True)
            col_b1, col_b2, _ = st.columns([1.2, 1.2, 3.6])
            with col_b1:
                if st.button("✨ 確認", key=f"btn_conf_{item.idea_id}"):
                    try:
                        ideas_store.confirm_idea(item.idea_id, note=f"Confirmed on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Confirm failed: {e}")
            with col_b2:
                show_drop_form = st.session_state.get(f"show_drop_{item.idea_id}", False)
                if st.button("📦 放棄……", key=f"btn_drop_toggle_{item.idea_id}"):
                    st.session_state[f"show_drop_{item.idea_id}"] = not show_drop_form
                    st.rerun()

            if st.session_state.get(f"show_drop_{item.idea_id}", False):
                with st.form(f"form_drop_{item.idea_id}"):
                    st.markdown(f"**放棄構想：{item.title}**")
                    drop_r = st.text_input("放棄原因（必填）*", key=f"dr_{item.idea_id}")
                    drop_p = st.text_input("當時問題／阻礙（必填）*", key=f"dp_{item.idea_id}")
                    drop_n = st.text_input("Decision Note (说明)", value="Dropped after review", key=f"dn_{item.idea_id}")
                    if st.form_submit_button("確認放棄"):
                        if drop_r.strip() and drop_p.strip():
                            try:
                                ideas_store.drop_idea(item.idea_id, drop_reason=drop_r, drop_problem=drop_p, note=drop_n)
                                st.session_state[f"show_drop_{item.idea_id}"] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Drop failed: {e}")
                        else:
                            st.error("放棄原因與當時問題均為必填，不得留空。")

    # Category 2: 已確認 (Default Expanded)
    confirmed_list = grouped[STATUS_CONFIRMED]
    with st.expander(f"▼ 已確認 ({len(confirmed_list)})", expanded=True):
        if not confirmed_list:
            st.info("目前沒有已確認、待立案的構想。")
        for item in confirmed_list:
            st.markdown(f"""
            <div class="glass-card">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0;">✨ {item.title} <small style="color:#8b949e;">({item.idea_id})</small></h4>
                    <span class="priority-{item.priority}">PRIORITY {item.priority.upper()}</span>
                </div>
                <p style="margin:8px 0 4px 0;">{item.description or '—'}</p>
                <p style="margin:0; font-size:12px; color:#8b949e;">
                    <b>Target Project:</b> <code>{item.project_id}</code> |
                    <b>Milestone:</b> {item.milestone_id or 'TBD'} |
                    <b>Source:</b> {item.source}
                </p>
                <p style="margin:4px 0 0 0; font-size:12px; color:#7ee787;"><b>Decision Note:</b> {item.decision_note or 'Approved for upcoming sprint'}</p>
            </div>
            """, unsafe_allow_html=True)
            col_b1, col_b2, _ = st.columns([1.5, 1.2, 3.3])
            with col_b1:
                show_conv_form = st.session_state.get(f"show_conv_{item.idea_id}", False)
                if st.button("🚀 正式立案 (Convert)...", key=f"btn_conv_toggle_{item.idea_id}"):
                    st.session_state[f"show_conv_{item.idea_id}"] = not show_conv_form
                    st.rerun()
            with col_b2:
                show_drop_form2 = st.session_state.get(f"show_drop2_{item.idea_id}", False)
                if st.button("📦 放棄……", key=f"btn_drop2_toggle_{item.idea_id}"):
                    st.session_state[f"show_drop2_{item.idea_id}"] = not show_drop_form2
                    st.rerun()

            if st.session_state.get(f"show_conv_{item.idea_id}", False):
                with st.form(f"form_conv_{item.idea_id}"):
                    st.markdown(f"**正式立案: {item.title}**")
                    conv_proj = st.text_input("Target Project ID (必填)*", value=item.project_id if item.project_id != "Unassigned" else "ai-development-manager")
                    conv_ms = st.text_input("Milestone ID (可选)", value=item.milestone_id or "")
                    conv_t = st.text_input("Task ID (可选)", value=item.task_id or "")
                    conv_n = st.text_input("Decision Note", value=item.decision_note or "Converted to project task")
                    if st.form_submit_button("確認立案"):
                        if conv_proj.strip() and conv_proj.strip() != "Unassigned":
                            try:
                                ideas_store.convert_idea(item.idea_id, project_id=conv_proj.strip(), milestone_id=conv_ms.strip() or None, task_id=conv_t.strip() or None, note=conv_n)
                                st.session_state[f"show_conv_{item.idea_id}"] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Convert failed: {e}")
                        else:
                            st.error("立案必须绑定明确有效的 Project ID，不能为 Unassigned。")

            if st.session_state.get(f"show_drop2_{item.idea_id}", False):
                with st.form(f"form_drop2_{item.idea_id}"):
                    st.markdown(f"**放棄構想：{item.title}**")
                    drop_r2 = st.text_input("放棄原因（必填）*", key=f"dr2_{item.idea_id}")
                    drop_p2 = st.text_input("當時問題／阻礙（必填）*", key=f"dp2_{item.idea_id}")
                    drop_n2 = st.text_input("Decision Note (说明)", value="Dropped from confirmed queue", key=f"dn2_{item.idea_id}")
                    if st.form_submit_button("確認放棄"):
                        if drop_r2.strip() and drop_p2.strip():
                            try:
                                ideas_store.drop_idea(item.idea_id, drop_reason=drop_r2, drop_problem=drop_p2, note=drop_n2)
                                st.session_state[f"show_drop2_{item.idea_id}"] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Drop failed: {e}")
                        else:
                            st.error("放棄原因與當時問題均為必填，不得留空。")

    # Category 3: 已立案 (Default Collapsed / Folded)
    converted_list = grouped[STATUS_CONVERTED]
    with st.expander(f"▶ 已立案 ({len(converted_list)})", expanded=False):
        st.caption("ℹ️ *已立案構想的執行進度唯一來源為正式 Project／Milestone／Task SSOT，本頁僅保留立案歷史檔案。*")
        if not converted_list:
            st.write("目前沒有已立案歷史。")
        for item in converted_list:
            st.markdown(f"""
            <div class="glass-card-dimmed">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0; color:#c9d1d9;">🚀 {item.title} <small style="color:#8b949e;">({item.idea_id})</small></h4>
                    <span class="badge-ok">ACTIVE IN PROJECT</span>
                </div>
                <p style="margin:6px 0 4px 0; color:#8b949e;">{item.description or '—'}</p>
                <p style="margin:0; font-size:12px;">
                    <b>Linked Project:</b> <code>{item.project_id}</code> |
                    <b>Milestone:</b> {item.milestone_id or '—'} |
                    <b>Task ID:</b> <code>{item.task_id or '—'}</code>
                </p>
                <p style="margin:2px 0 0 0; font-size:12px;">
                    <b>提出日期:</b> {item.created_at or '—'} |
                    <b>立案日期:</b> {item.converted_at or '—'} |
                    <b>來源：</b> {item.source}
                </p>
                <p style="margin:4px 0 0 0; font-size:12px; color:#58a6ff;"><b>Decision Note:</b> {item.decision_note or 'Converted to active roadmap'}</p>
            </div>
            """, unsafe_allow_html=True)
            if st.button("🔍 查看專案進度", key=f"btn_view_proj_{item.idea_id}"):
                st.session_state["selected_project_id"] = item.project_id
                st.session_state["nav_selection"] = NAV_PROJECTS
                st.rerun()

    # Category 4: 已放棄 (Default Collapsed / Folded)
    dropped_list = grouped[STATUS_DROPPED]
    with st.expander(f"▶ 已放棄 ({len(dropped_list)})", expanded=False):
        st.caption("ℹ️ *已放棄構想的歷史記錄完整保留，支援隨時恢復。*")
        if not dropped_list:
            st.write("目前沒有已放棄歷史。")
        for item in dropped_list:
            st.markdown(f"""
            <div class="glass-card-dimmed">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0; color:#8b949e;">📦 {item.title} <small>({item.idea_id})</small></h4>
                    <span class="badge-err">DROPPED</span>
                </div>
                <p style="margin:6px 0 4px 0; color:#8b949e;">{item.description or '—'}</p>
                <p style="margin:0; font-size:12px; color:#ff7b72;">
                    <b>放棄日期：</b> {item.dropped_at or '—'} |
                    <b>放棄原因：</b> {item.drop_reason or '—'}
                </p>
                {f'<p style="margin:2px 0 0 0; font-size:12px; color:#b7c3d0;"><b>當時問題／阻礙：</b> {item.drop_problem}</p>' if item.drop_problem else ''}
                <p style="margin:2px 0 0 0; font-size:12px; color:#8b949e;"><b>Decision Note:</b> {item.decision_note or '—'}</p>
            </div>
            """, unsafe_allow_html=True)
            if st.button("🔄 恢复为待立案 (Restore to Pending)", key=f"btn_restore_{item.idea_id}"):
                try:
                    ideas_store.restore_idea(item.idea_id, note=f"Restored on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Restore failed: {e}")


def render_projects_page():
    st.title("📁 專案與路線圖")
    selected_p_id = st.session_state.get("selected_project_id")
    if selected_p_id:
        st.info(f"目前專案：**{selected_p_id}**")

    if not records_available:
        st.warning("無法取得 — 無法讀取 Drive 的 canonical 專案／任務記錄；不會推論為空結果。")
    elif not all_projects:
        st.info("Drive SSOT 中沒有可用的專案記錄。")
    else:
        for proj in all_projects:
            p_id = proj.get("project_id", "—")
            p_title = proj.get("title", p_id)
            is_highlighted = (p_id == selected_p_id)
            detail = build_project_detail_vm(proj, all_tasks, all_executions, all_actions, all_ideas, now)
            completion = detail["task_completion"]
            completion_text = f"{completion[0]} / {completion[1]}" if completion else "無法取得"

            st.markdown(f"""
            <div class="glass-card" {'style="border: 2px solid #388bfd;"' if is_highlighted else ''}>
                <h3 style="margin:0;">{p_title} <small style="color:#8b949e;">({p_id})</small></h3>
                <p style="margin:6px 0;"><b>目前階段：</b>{detail['current_phase']} | <b>任務完成數：</b>{completion_text} | <b>里程碑進度：</b>{detail['milestone_progress']}</p>
            </div>
            """, unsafe_allow_html=True)
            with st.expander(f"查看專案詳情：{p_title}", expanded=is_highlighted):
                st.caption(f"SSOT：{'已連線' if not actions_store.is_degraded else '無法取得／降級'} · 任務完成數不等於里程碑進度。")
                st.write(f"**優先路線圖：** {detail['priority_roadmap'] or '無法取得／未記錄'}")
                cols = st.columns(4)
                for col, label, items in zip(cols, ("目前", "下一步", "受阻", "最近完成"),
                                             (detail['current'], detail['next'], detail['blocked'], detail['completed'])):
                    with col:
                        st.caption(label)
                        if items:
                            for task in items:
                                st.write(f"`{task.get('task_id', '—')}` {task.get('title', '—')} · {task.get('status', 'Unknown')}")
                        else:
                            st.write("無法取得／無記錄")
                st.write("**執行／工作階段**")
                if detail['executions']:
                    for exe in detail['executions']:
                        st.write(f"`{exe.get('execution_id', '—')}` · {exe.get('provider', 'Unknown')} · session `{exe.get('provider_session_id') or exe.get('session_id') or 'Not recorded'}` · {exe.get('status', 'Unknown')}")
                else:
                    st.write("無法取得／未記錄")
                if detail['actions']:
                    st.write("**Open Actions:** " + "; ".join(f"{a.type}/{a.severity}: {a.reason}" for a in detail['actions']))
                st.write(f"**Relevant Ideas:** {len(detail['ideas'])}")
                if detail['recent_activity']:
                    st.write("**Recent Activity (canonical timestamps):** " + "; ".join(f"{x[0]} {x[1]} {x[2]} → {x[3]}" for x in detail['recent_activity']))


def render_tasks_page():
    st.title("📋 任務看板與執行狀態")
    if not records_available:
        st.warning("無法取得 — canonical 任務記錄不可讀；不會推論為零筆任務。")
        return
    board = map_task_board(all_tasks, active_executions_dict, now)

    tab_in_progress, tab_ready, tab_blocked, tab_completed = st.tabs([
        f"🚀 進行中 ({len(board['In progress'])})",
        f"📥 就緒 ({len(board['Ready'])})",
        f"⚠️ 受阻／待注意 ({len(board['Blocked / Attention'])})",
        f"✅ 已完成 ({len(board['Completed'])})"
    ])

    def render_task_cards(tasks):
        if not tasks:
            st.write("此分類目前沒有任務。")
            return
        for t in tasks:
            project_id = t.get("project_id", "—")
            task_id = t.get("task_id", "—")
            title = t.get("title", "—")
            priority = t.get("priority", "normal")
            provider = t.get("assigned_provider") or t.get("recommended_provider") or "Unassigned"
            progress = t.get("current_progress", "—")
            next_action = t.get("next_action", "—")
            st.markdown(f"""
            <div class="glass-card">
                <h4>{title} <small style="color:#8b949e;">({task_id} in {project_id})</small></h4>
                <p>Priority: <span class="priority-{priority}">{priority.upper()}</span> | AI: <b>{provider}</b></p>
                <p>Progress: <i>{progress}</i></p>
                <p><b>Next Action:</b> {next_action}</p>
            </div>
            """, unsafe_allow_html=True)

    with tab_in_progress:
        render_task_cards(board["In progress"])
    with tab_ready:
        render_task_cards(board["Ready"])
    with tab_blocked:
        render_task_cards(board["Blocked / Attention"])
    with tab_completed:
        render_task_cards(board["Completed"])


def render_sessions_page():
    st.title("🔍 AI 工作階段與交接檢視")
    sessions = build_sessions_vm(all_executions)
    for title, rows in (("目前工作階段", sessions["current"]), ("歷史工作階段", sessions["historical"])):
        st.subheader(f"{title} ({len(rows)})")
        if not rows:
            st.write("無法取得／未記錄")
        for row in rows:
            st.markdown(f"""<div class="glass-card"><b>{row['provider'] or 'Unknown'}</b> · `{row['execution_id'] or '—'}` · session `{row['provider_session_id']}`<br><small>Project `{row['project_id'] or '—'}` → Task `{row['task_id'] or '—'}` · model `{row['model'] or 'Not recorded'}` · mode `{row['mode'] or 'Not recorded'}` · status `{row['status'] or 'Unknown'}` · started {row['started_at'] or 'Unknown'} · last activity {row['last_activity'] or 'Unknown'} · ended {row['completed_at'] or row['finished_at'] or 'Not recorded'}</small></div>""", unsafe_allow_html=True)
    all_task_ids = [(t.get("project_id"), t.get("task_id"), t.get("title")) for t in all_tasks]

    if not all_task_ids:
        st.info("No tasks available to inspect.")
    else:
        selected_option = st.selectbox(
            "Select Task to Inspect",
            options=all_task_ids,
            format_func=lambda x: f"[{x[0]}] {x[1]} - {x[2]}"
        )
        if selected_option:
            p_id, t_id, _ = selected_option
            task = next((t for t in all_tasks if t.get("project_id") == p_id and t.get("task_id") == t_id), {})
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Task Details")
                st.write(f"**Status**: `{task.get('status')}`")
                st.write(f"**Assigned Provider**: `{task.get('assigned_provider')}`")
                st.write(f"**Next Action**: {task.get('next_action')}")
                linked_exe = next((e for e in all_executions if e.get("project_id") == p_id and e.get("task_id") == t_id), None)
                if linked_exe:
                    started = linked_exe.get("started_at") or linked_exe.get("reserved_at")
                    activity, _ = get_latest_activity_timestamp(linked_exe)
                    expected, remaining = format_duration_and_remaining_eta(linked_exe.get("expected_minutes"), started, now)
                    st.write(f"Execution ID: `{linked_exe.get('execution_id')}`")
                    st.write(f"Provider / model: `{linked_exe.get('provider') or 'Unknown'}` / `{linked_exe.get('model') or 'Not recorded'}`")
                    st.write(f"Provider Session: `{linked_exe.get('provider_session_id') or linked_exe.get('session_id') or 'Not recorded'}`")
                    st.write(f"State: `{linked_exe.get('status', 'Unknown')}` · Started: {started or 'Unknown'} · Elapsed: {format_elapsed_duration(started, now)}")
                    st.write(f"Last activity: {format_activity_timestamp_and_age(activity, now)} · Expected total: {expected} · Estimated remaining: {remaining}")
                    st.write(f"Result / reason: {linked_exe.get('result') or linked_exe.get('recovery_reason') or 'Not recorded'}")
                linked_actions = [a for a in all_actions if a.project_id == p_id and a.task_id == t_id and a.status in (STATUS_OPEN, STATUS_ACKNOWLEDGED)]
                if linked_actions:
                    st.write("Action Center: " + "; ".join(f"{a.type}/{a.severity} ({a.status}): {a.reason}" for a in linked_actions))
            with col2:
                st.subheader("Latest Handoff")
                ho = handoffs_dict.get((p_id, t_id))
                if ho:
                    st.write(f"Handoff ID: `{ho.get('handoff_id')}`")
                    st.write(f"Next Action: {ho.get('next_action')}")
                    with st.expander("Completed Work"):
                        st.write(ho.get("completed_work", []))
                else:
                    st.write("No handoff records found.")


def render_quota_page():
    st.title("⚡ 用量與 AI Fleet 預測")
    st.markdown(f"""
    <div class="recommendation-card">
        <h4 style="margin:0 0 8px 0; color:#79c0ff;">🚀 主要建議：{daily_brief_vm.recommended_action}</h4>
        <p style="margin:0 0 6px 0;"><b>目標供應商：</b>{daily_brief_vm.recommended_provider or '無法取得'}</p>
        <p style="margin:0; font-size:14px;"><b>依據：</b>{daily_brief_vm.reason}</p>
    </div>
    """, unsafe_allow_html=True)

    cols = st.columns(min(len(daily_brief_vm.accounts), 3) or 1)
    for idx, card in enumerate(daily_brief_vm.accounts):
        with cols[idx % len(cols)]:
            try:
                st.subheader(card.card_title)
                st.metric(
                    label="主要用量剩餘",
                    value="無法取得" if card.stale else card.formatted_five_hour_remaining,
                    delta=f"-{card.formatted_five_hour_burn_rate}" if card.formatted_five_hour_burn_rate != "—" else None,
                    delta_color="inverse"
                )
                if card.stale:
                    st.warning("用量資料已過期，無法判定可用量或是否耗盡。")
                elif card.formatted_five_hour_countdown != "—":
                    st.write(f"• **重設倒數**：`{card.formatted_five_hour_countdown}`")
                if card.extra_credits_available is not None:
                    st.write(f"• **Extra Credits**: `{card.formatted_extra_credits}`")
                st.write(f"• **Effective Availability**: `{card.formatted_effective_availability}`")
                st.caption(f"Source: `{card.source}` ({card.source_type}) | Confidence: `{card.confidence}`")
                st.caption(f"Last updated: {card.last_updated or 'never'}")
                st.markdown("---")
            except Exception as e:
                st.error(f"Error rendering {card.card_title}: {e}")


def render_dispatch_truth_page():
    st.title("🔎 派工真相 (Visible Dispatch Truth Gate)")
    st.caption("任務／AI／帳號／狀態／額度 一律取自真實 SSOT 紀錄；無法取得的欄位一律顯示 UNKNOWN／STALE，絕不猜測。")

    if not canonical_records_available and not records_are_last_known:
        st.error("VISIBLE DISPATCH GATE: FAIL — 目前無法讀取 Task/Command/Execution 正式紀錄。")
        return

    commands_by_task: Dict[tuple, Dict[str, Any]] = {}
    for cmd in all_commands:
        key = (cmd.get("project_id"), cmd.get("task_id"))
        existing = commands_by_task.get(key)
        if existing is None or (cmd.get("created_at") or "") >= (existing.get("created_at") or ""):
            commands_by_task[key] = cmd

    executions_by_id = {e.get("execution_id"): e for e in all_executions if e.get("execution_id")}
    projects_by_id = {p.get("project_id"): p for p in all_projects}

    visible_tasks = [t for t in all_tasks if t.get("status") != "cancelled"]
    rows = []
    for task in visible_tasks:
        key = (task.get("project_id"), task.get("task_id"))
        command = commands_by_task.get(key)
        execution = executions_by_id.get(command.get("execution_id")) if command else None
        project = projects_by_id.get(task.get("project_id"))
        rows.append(build_dispatch_truth_row(project, task, command, execution, daily_brief_vm.accounts, now))

    gate = compute_visible_dispatch_gate(rows)
    if gate["result"] == "PASS":
        st.success("VISIBLE DISPATCH GATE: PASS")
    else:
        st.error("VISIBLE DISPATCH GATE: FAIL")
        with st.expander(f"原因（{len(gate['reasons'])}）", expanded=True):
            for reason in gate["reasons"]:
                st.write(f"- {reason}")

    if not rows:
        st.info("目前沒有任務可顯示。")
        return

    for row in rows:
        state = row["dispatch_state"]
        badge = "🟢" if state == DISPATCH_STATE_RUNNING else ("🔴" if state in ("FAILED", "BLOCKED") else "⚪")
        with st.container():
            st.markdown(f"""<div class="glass-card">
                <b>{badge} {row['project_name']} → {row['task_title']}</b><br>
                <b>狀態 (Dispatch State):</b> <code>{state}</code> — {row['dispatch_reason']}<br>
                <b>AI／帳號：</b> {row['provider']} / {row['account_id']} · <b>Model/Mode：</b> {row['model']} / {row['mode']}<br>
                <b>5h 剩餘：</b> {row['quota']['formatted_five_hour_remaining']}（重設 {row['quota']['formatted_five_hour_reset_at']}）
                · <b>Weekly 剩餘：</b> {row['quota']['formatted_weekly_remaining']}（重設 {row['quota']['formatted_weekly_reset_at']}）<br>
                <b>額度取得時間 (captured_at)：</b> {row['quota']['formatted_captured_at']} · <b>新鮮度：</b> {row['quota']['freshness']}
                </div>""", unsafe_allow_html=True)
            with st.expander("技術細節 (Technical IDs)"):
                st.write(f"project_id=`{row['project_id']}` · task_id=`{row['task_id']}`")
                st.write(f"execution_id=`{row['execution_id']}` · session_id=`{row['session_id']}`")


def render_reviews_page():
    st.title("📝 審查證據")
    rows = build_review_evidence_vm(all_handoffs)
    if not rows:
        st.write("無法取得／未記錄")
    for row in rows:
        st.markdown(f"""<div class="glass-card"><b>Review Verdict:</b> {row['verdict']}<br><small>Source: {row['source']} · Project `{row['project_id'] or '—'}` → Task `{row['task_id'] or '—'}` · reviewer `{row['reviewer']}` · {row['timestamp'] or 'Not recorded'}</small><br>Tests: {row['tests'] or 'Unavailable'}<br>Commits: {row['commits'] or 'Unavailable'}<br>Known issues: {row['known_issues'] or 'None recorded'}</div>""", unsafe_allow_html=True)


def render_logs_page():
    st.title("📄 營運日誌／最近事件")
    projects = ["All"] + sorted({p.get("project_id") for p in all_projects if p.get("project_id")})
    project_filter = st.selectbox("專案", projects, key="log_project")
    providers = ["All"] + sorted({e.get("provider") for e in all_executions if e.get("provider")})
    provider_filter = st.selectbox("供應商", providers, key="log_provider")
    events = build_operational_events(all_commands, all_executions, all_actions, all_handoffs, limit=30,
                                      project_id=None if project_filter == "All" else project_filter,
                                      provider=None if provider_filter == "All" else provider_filter)
    st.caption("Source: canonical state timestamps; activity is capped to one latest event per execution.")
    if not events:
        st.write("無法取得／未記錄")
    for event in events:
        st.write(f"`{event['timestamp']}` · {event['kind']} · `{event['project_id'] or '—'}`/`{event['task_id'] or '—'}` · {event['event']} · {event['provider']} · {event['source']}")


def render_placeholder_page(title: str, description: str):
    st.title(title)
    st.info(f"{description} (Foundation established in P1-A; full integration scheduled for subsequent slice).")


if selected_nav == NAV_OVERVIEW:
    render_overview_page()
elif selected_nav == NAV_ACTION_CENTER:
    render_action_center_page()
elif selected_nav == NAV_IDEAS:
    render_ideas_page()
elif selected_nav == NAV_PROJECTS:
    render_projects_page()
elif selected_nav == NAV_TASKS:
    render_tasks_page()
elif selected_nav == NAV_DISPATCH_TRUTH:
    render_dispatch_truth_page()
elif selected_nav == NAV_AI_SESSIONS:
    render_sessions_page()
elif selected_nav == NAV_QUOTA:
    render_quota_page()
elif selected_nav == NAV_REVIEWS:
    render_reviews_page()
elif selected_nav == NAV_LOGS:
    render_logs_page()
elif selected_nav == NAV_SETTINGS:
    render_placeholder_page("⚙️ 設定", "管理憑證、帳戶對應與重新整理間隔。")

if all_warnings:
    st.markdown("---")
    with st.expander(f"⚠️ System Notices ({len(all_warnings)})"):
        for w in all_warnings:
            st.warning(w)
