import os
import json
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
    build_daily_brief_vm,
    DailyBriefViewModel,
    AccountQuotaCardViewModel,
    ServiceHealthViewModel,
    parse_scheduled_task_health,
    build_session_center_health,
)

st.set_page_config(
    page_title="ADM Operations Dashboard",
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

if store:
    try:
        all_projects = store.list_projects()
    except Exception as e:
        all_warnings.append(f"Failed to list projects: {e}")

    for p in all_projects:
        p_id = p.get("project_id")
        if not p_id:
            continue

        try:
            tasks_folder = store.project_folder("tasks", p_id, create=False)
            task_files = store.children(tasks_folder)
            for tf in task_files:
                fname = tf.get("name", "")
                if fname.endswith(".json"):
                    t_id = fname[:-5]
                    try:
                        task_data = store.get("tasks", p_id, t_id)
                        all_tasks.append(task_data)
                    except Exception as e:
                        all_warnings.append(f"Malformed record in tasks for project {p_id}, file {fname}: {e}")
        except Exception:
            pass

        try:
            commands_folder = store.project_folder("commands", p_id, create=False)
            for cf in store.children(commands_folder):
                fname = cf.get("name", "")
                if fname.endswith(".json"):
                    all_commands.append(store.get("commands", p_id, fname[:-5]))
        except Exception:
            pass

        try:
            execs_folder = store.project_folder("executions", p_id, create=False)
            exec_files = store.children(execs_folder)
            for ef in exec_files:
                fname = ef.get("name", "")
                if fname.endswith(".json"):
                    e_id = fname[:-5]
                    try:
                        exec_data = store.get("executions", p_id, e_id)
                        all_executions.append(exec_data)
                        if exec_data.get("status") in ["running", "reserved"]:
                            active_executions.append(exec_data)
                            active_executions_dict[(p_id, exec_data.get("task_id"))] = exec_data
                    except Exception as e:
                        all_warnings.append(f"Malformed record in executions for project {p_id}, file {fname}: {e}")
        except Exception:
            pass

        try:
            ho_folder = store.project_folder("handoffs", p_id, create=False)
            ho_files = store.children(ho_folder)
            for hf in ho_files:
                fname = hf.get("name", "")
                if fname.endswith(".json"):
                    h_id = fname[:-5]
                    try:
                        ho_data = store.get("handoffs", p_id, h_id)
                        all_handoffs.append(ho_data)
                        t_id = ho_data.get("task_id")
                        if t_id:
                            handoffs_dict[(p_id, t_id)] = ho_data
                    except Exception as e:
                        all_warnings.append(f"Malformed record in handoffs for project {p_id}, file {fname}: {e}")
        except Exception:
            pass

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
next_auto_action_str = compute_next_auto_action(all_tasks, active_executions, all_actions, daily_brief_vm)

NAV_OVERVIEW = "Overview"
NAV_ACTION_CENTER = "Action Center"
NAV_PROJECTS = "Projects"
NAV_TASKS = "Tasks"
NAV_IDEAS = "Ideas"
NAV_AI_SESSIONS = "AI Sessions"
NAV_REVIEWS = "Reviews"
NAV_QUOTA = "Quota"
NAV_LOGS = "Logs"
NAV_SETTINGS = "Settings"

NAV_PAGES = [
    NAV_OVERVIEW,
    NAV_ACTION_CENTER,
    NAV_PROJECTS,
    NAV_TASKS,
    NAV_IDEAS,
    NAV_AI_SESSIONS,
    NAV_REVIEWS,
    NAV_QUOTA,
    NAV_LOGS,
    NAV_SETTINGS,
]

if "nav_selection" not in st.session_state:
    st.session_state["nav_selection"] = NAV_OVERVIEW

st.sidebar.title("🤖 AI Development Manager")
selected_nav = st.sidebar.radio("Navigation", NAV_PAGES, key="nav_selection")

st.sidebar.markdown("---")
if actions_summary["need_user_action"] > 0:
    st.sidebar.error(f"🚨 待你处理: {actions_summary['need_user_action']} 项")
elif actions_summary["open"] > 0:
    st.sidebar.warning(f"⚡ Action Center: {actions_summary['open']} open")
else:
    st.sidebar.caption(f"✅ Action Center: All clear")

conflict_tag = f" · ⚠️ {ideas_summary['conflicted']} 冲突" if ideas_summary.get('conflicted', 0) > 0 else ""
st.sidebar.caption(f"💡 Ideas: {ideas_summary['pending']} 待立案 · {ideas_summary['confirmed']} 已确认{conflict_tag}")
st.sidebar.caption(f"⚡ Active Tasks: {len(active_executions)}")

def render_overview_page():
    st.title("🎯 Operations Overview")

    # Section 1: Global Runtime State Banner
    st.markdown(f"""
    <div class="runtime-state-banner">
        <div>
            <span style="font-size:13px; color:#8b949e; margin-right:8px;">GLOBAL ADM RUNTIME STATE:</span>
            <span class="{global_badge_class}" style="font-size:14px;">{global_runtime_state}</span>
            <span style="font-size:13px; margin-left:10px; color:#c9d1d9;">{global_state_desc}</span>
        </div>
        <div style="font-size:13px; color:#8b949e;">
            <b>Next Auto Action:</b> <span style="color:#58a6ff;">{next_auto_action_str}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Section 2: Action Center Quick Alert Bar
    col_act1, col_act2, col_act3, col_act4 = st.columns([1.5, 1.5, 1.5, 2.5])
    with col_act1:
        st.metric("🚨 Need User Action", actions_summary["need_user_action"])
    with col_act2:
        st.metric("📝 Review Required", actions_summary["review_required"])
    with col_act3:
        st.metric("⚠️ Blocked Items", actions_summary["blocked"])
    with col_act4:
        if actions_summary["need_user_action"] > 0:
            if st.button("👉 Open Action Center (前往处理)", key="btn_goto_actions_top", use_container_width=True):
                st.session_state["nav_selection"] = NAV_ACTION_CENTER
                st.rerun()
        else:
            st.caption("✅ No critical user interventions pending.")

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
        st.subheader("📁 Active Projects & Milestones")
        if not all_projects:
            st.info("No active projects found. Create a project to begin tracking.")
        else:
            for proj in all_projects:
                p_id = proj.get("project_id", "—")
                p_title = proj.get("title", p_id)
                proj_tasks = [t for t in all_tasks if t.get("project_id") == p_id]
                completed_tasks = [t for t in proj_tasks if t.get("status") == "completed"]
                total_count = len(proj_tasks) or 1
                prog_pct = int((len(completed_tasks) / total_count) * 100)

                active_task_in_proj = next((t for t in proj_tasks if (p_id, t.get("task_id")) in active_executions_dict), None)
                curr_status = "In Progress" if active_task_in_proj else ("Ready" if proj_tasks else "Planning")
                next_step = active_task_in_proj.get("next_action") if active_task_in_proj else (proj_tasks[0].get("next_action") if proj_tasks else "Define next milestone")

                st.markdown(f"""
                <div class="glass-card">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <h4 style="margin:0;">{p_title} <small style="color:#8b949e;">({p_id})</small></h4>
                        <span class="badge-ok">{curr_status}</span>
                    </div>
                    <p style="margin:8px 0 4px 0; font-size:14px;"><b>Overall Progress:</b> {prog_pct}% ({len(completed_tasks)}/{len(proj_tasks)} tasks)</p>
                    <div style="background:#21262d; border-radius:4px; height:6px; width:100%; margin-bottom:8px;">
                        <div style="background:#238636; width:{prog_pct}%; height:6px; border-radius:4px;"></div>
                    </div>
                    <p style="margin:0; font-size:13px; color:#8b949e;"><b>Next Step / ETA:</b> {next_step or '—'}</p>
                </div>
                """, unsafe_allow_html=True)

        st.subheader("⚠️ Blockers & Attention Required")
        stale_execs = [exe for exe in active_executions if is_execution_stale(exe, now)]
        blocked_tasks = [t for t in all_tasks if t.get("status") in ["blocked", "attention"]]

        if not stale_execs and not blocked_tasks and not conflicted_ideas and actions_summary["blocked"] == 0:
            st.success("✅ No active blockers. All pipelines and tasks are operating normally.")
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
        st.subheader("⚡ Live AI Fleet & Runtime Visibility")
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
                            <p style="margin:6px 0 2px 0; font-size:13px;"><b>Task:</b> {curr_task_str}</p>
                            <p style="margin:0 0 2px 0; font-size:12px; color:#8b949e;"><b>Model/Mode:</b> {model_str} ({mode_str}/{effort_str}) | <b>Session:</b> <code>{sess_id}</code></p>
                            <p style="margin:0 0 2px 0; font-size:12px;"><b>Started:</b> {started_display} · <b>Elapsed:</b> {elapsed_display}</p>
                            <p style="margin:0 0 2px 0; font-size:12px;"><b>Last Activity ({act_src}):</b> {activity_display}{eta_line}</p>
                            <p style="margin:2px 0 0 0; font-size:12px; color:#58a6ff;"><i>Current Step: {event_str}</i></p>
                        </div>
                        """, unsafe_allow_html=True)
                    else:
                        fleet_state = "IDLE"
                        badge_class = "badge-warn"
                        curr_task_str = "No active task assigned"
                        event_str = "Awaiting next dispatch cycle"

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
                        st.warning(f"⚠️ STALE: No recent status updates received for {acc_title}.")

                    rem_pct = acc.get("remaining_percent")
                    if rem_pct is None:
                        w = next((w for w in acc.get("windows", []) if w.get("remaining_percent") is not None), None)
                        if w:
                            rem_pct = w.get("remaining_percent")

                    if rem_pct is not None:
                        st.progress(max(0.0, min(1.0, float(rem_pct) / 100.0)))
                    else:
                        st.info("Percentage not reported")
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
                        <p style="margin:6px 0 2px 0; font-size:13px;"><b>Task:</b> {curr_task_str}</p>
                        <p style="margin:0 0 2px 0; font-size:12px; color:#8b949e;"><b>Model/Mode:</b> {model_str} ({mode_str}/{effort_str}) | <b>Session:</b> <code>{sess_id}</code></p>
                        <p style="margin:0 0 2px 0; font-size:12px;"><b>Started:</b> {started_display} · <b>Elapsed:</b> {elapsed_display}</p>
                        <p style="margin:0 0 2px 0; font-size:12px;"><b>Last Activity ({act_src}):</b> {activity_display}{eta_line}</p>
                        <p style="margin:2px 0 0 0; font-size:12px; color:#58a6ff;"><i>Current Step: {event_str}</i></p>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    fleet_state = "IDLE"
                    badge_class = "badge-warn"
                    curr_task_str = "No active task assigned"
                    event_str = "Awaiting next dispatch cycle"

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
                    st.warning(f"⚠️ STALE: No recent status updates received for {prov_name}.")

                rem_pct = prov.get("remaining_percent")
                if rem_pct is None:
                    w = next((w for w in prov.get("windows", []) if w.get("remaining_percent") is not None), None)
                    if w:
                        rem_pct = w.get("remaining_percent")

                if rem_pct is not None:
                    st.progress(max(0.0, min(1.0, float(rem_pct) / 100.0)))
                else:
                    st.info("Percentage not reported")

        st.subheader("💡 Ideas Backlog")
        st.markdown(f"""
        <div class="recommendation-card">
            <h4 style="margin:0 0 6px 0;">Ideas ({ideas_summary['total']} total)</h4>
            <p style="margin:0 0 10px 0; font-size:14px; color:#c9d1d9;">
                <b>待立案 {ideas_summary['pending']}</b> · <b>已确认 {ideas_summary['confirmed']}</b>
                <br><small style="color:#8b949e;">(已立案 {ideas_summary['converted']} · 已放弃 {ideas_summary['dropped']})</small>
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("👉 Open Ideas Page (前往灵感中心)", key="btn_goto_ideas", use_container_width=True):
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
    st.title("🚨 Action Center (待你处理)")
    st.caption("当 AI Fleet 无法自动继续或需要人工决策验收时，集中在此处记录和响应，确保任务不发生无声停滞。")

    if actions_store.is_degraded:
        st.warning("⚠️ Action Center: Running in Local Cache / Degraded Mode (Google Drive SSOT disconnected). Action updates are read-only.")
    else:
        st.caption("✅ Action Center: Google Drive SSOT Connected")

    if actions_store.last_error:
        st.error(f"{actions_store.last_error}")

    # Summary Metrics Row
    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
    with m_col1:
        st.metric("🚨 Need User Action", actions_summary["need_user_action"])
    with m_col2:
        st.metric("📝 Review Required", actions_summary["review_required"])
    with m_col3:
        st.metric("⚠️ Blocked Items", actions_summary["blocked"])
    with m_col4:
        st.metric("📦 Action History", actions_summary["history"])

    st.markdown("---")

    open_actions_list = [a for a in all_actions if a.status == STATUS_OPEN]
    ack_actions_list = [a for a in all_actions if a.status == STATUS_ACKNOWLEDGED]
    history_actions_list = [a for a in all_actions if a.status in [STATUS_RESOLVED, STATUS_DISMISSED]]

    # Category 1: Needs Attention (Open Items)
    with st.expander(f"🚨 Needs Attention / 待处理 ({len(open_actions_list)})", expanded=True):
        if not open_actions_list:
            st.info("✅ 当前没有任何待处理事项，所有任务均在自主运行或已就绪。")
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
                if st.button("👀 知晓 (Acknowledge)", key=f"btn_ack_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.acknowledge_action(item.action_id, note=f"Acknowledged by user at {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Acknowledge failed: {e}")
            with col_b2:
                if st.button("✅ 标记已解决 (Resolve)", key=f"btn_res_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.resolve_action(item.action_id, note="Resolved by user via Action Center")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Resolve failed: {e}")
            with col_b3:
                if st.button("✖ 忽略 (Dismiss)", key=f"btn_dsm_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.dismiss_action(item.action_id, note="Dismissed by user")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Dismiss failed: {e}")

    # Category 2: Acknowledged Items
    with st.expander(f"👀 Acknowledged / 已知晓待完成 ({len(ack_actions_list)})", expanded=bool(ack_actions_list)):
        if not ack_actions_list:
            st.write("暂无已知晓事项。")
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
                if st.button("✅ 标记已解决 (Resolve)", key=f"btn_res_ack_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.resolve_action(item.action_id, note="Resolved after acknowledgment")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Resolve failed: {e}")
            with col_b2:
                if st.button("✖ 忽略 (Dismiss)", key=f"btn_dsm_ack_{item.action_id}", disabled=actions_store.is_degraded):
                    try:
                        actions_store.dismiss_action(item.action_id, note="Dismissed after acknowledgment")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Dismiss failed: {e}")

    # Category 3: History (Resolved & Dismissed) - Default Collapsed
    with st.expander(f"📜 Action History / 历史归档 ({len(history_actions_list)})", expanded=False):
        st.caption("ℹ️ *已解决与已忽略的历史事项保留完整审计记录。*")
        if not history_actions_list:
            st.write("暂无历史归档事项。")
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
    st.title("💡 Ideas Backlog & Triage (灵感中心)")
    st.caption("保存平时零散提出的想法（‘以后要做 / 之后加 / 先记着’），在正式进入 Project 执行前完成确认与立案。")

    if ideas_store.is_degraded:
        st.warning("⚠️ Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")
    else:
        st.caption("✅ Ideas Store: Google Drive SSOT Connected")

    if ideas_store.last_error:
        st.error(f"{ideas_store.last_error}")

    # Quick Add Idea Expander
    with st.expander("➕ Capture New Idea (快速记录想法)", expanded=False):
        with st.form("form_add_idea", clear_on_submit=True):
            col_t1, col_t2 = st.columns([3, 1])
            with col_t1:
                new_title = st.text_input("Title (想法简述)", placeholder="例如: 增加微信/Webhook 告警机器人")
            with col_t2:
                new_priority = st.selectbox("Priority", ["high", "medium", "low"], index=1)

            new_desc = st.text_area("Description / Context", placeholder="详细背景、为什么要做、可能的方案...")
            new_proj = st.text_input("Proposed Project ID (可选)", value="ai-development-manager")
            new_source = st.text_input("Source / 来源", value="User Chat")

            if st.form_submit_button("Save Idea (存入待立案)"):
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
                        st.success(f"Idea '{new_title}' added to 待立案!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to add idea: {e}")
                else:
                    st.error("Title cannot be empty.")

    grouped = group_ideas_by_status(ideas_store.list_ideas())

    # Conflicted Ideas Warning Section (Fail Closed)
    conflicted_list = grouped.get(STATUS_CONFLICTED, [])
    if conflicted_list:
        with st.expander(f"⚠️ 冲突锁定中的 Ideas ({len(conflicted_list)})", expanded=True):
            st.error("检测到以下 Idea 在 Drive SSOT 中存在多份冲突记录。为确保真实性，禁止自动合并或选择胜出者，已锁定所有变更操作。请在云端排查修复后刷新。")
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
            st.info("暂无待立案想法 (0 Ideas)。")
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
                if st.button("✨ 确认 (Confirm)", key=f"btn_conf_{item.idea_id}"):
                    try:
                        ideas_store.confirm_idea(item.idea_id, note=f"Confirmed on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Confirm failed: {e}")
            with col_b2:
                show_drop_form = st.session_state.get(f"show_drop_{item.idea_id}", False)
                if st.button("📦 放弃 (Drop)...", key=f"btn_drop_toggle_{item.idea_id}"):
                    st.session_state[f"show_drop_{item.idea_id}"] = not show_drop_form
                    st.rerun()

            if st.session_state.get(f"show_drop_{item.idea_id}", False):
                with st.form(f"form_drop_{item.idea_id}"):
                    st.markdown(f"**放弃想法: {item.title}**")
                    drop_r = st.text_input("Drop Reason (放弃原因，必填)*", key=f"dr_{item.idea_id}")
                    drop_p = st.text_input("Drop Problem / Blocker (当时问题/阻碍，必填)*", key=f"dp_{item.idea_id}")
                    drop_n = st.text_input("Decision Note (说明)", value="Dropped after review", key=f"dn_{item.idea_id}")
                    if st.form_submit_button("Confirm Drop (确认放弃)"):
                        if drop_r.strip() and drop_p.strip():
                            try:
                                ideas_store.drop_idea(item.idea_id, drop_reason=drop_r, drop_problem=drop_p, note=drop_n)
                                st.session_state[f"show_drop_{item.idea_id}"] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Drop failed: {e}")
                        else:
                            st.error("放弃原因 (Drop Reason) 与 当时问题 (Drop Problem) 均为必填项，不得留空。")

    # Category 2: 已确认 (Default Expanded)
    confirmed_list = grouped[STATUS_CONFIRMED]
    with st.expander(f"▼ 已确认 ({len(confirmed_list)})", expanded=True):
        if not confirmed_list:
            st.info("暂无已确认待立案想法 (0 Ideas)。")
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
                if st.button("📦 放弃 (Drop)...", key=f"btn_drop2_toggle_{item.idea_id}"):
                    st.session_state[f"show_drop2_{item.idea_id}"] = not show_drop_form2
                    st.rerun()

            if st.session_state.get(f"show_conv_{item.idea_id}", False):
                with st.form(f"form_conv_{item.idea_id}"):
                    st.markdown(f"**正式立案: {item.title}**")
                    conv_proj = st.text_input("Target Project ID (必填)*", value=item.project_id if item.project_id != "Unassigned" else "ai-development-manager")
                    conv_ms = st.text_input("Milestone ID (可选)", value=item.milestone_id or "")
                    conv_t = st.text_input("Task ID (可选)", value=item.task_id or "")
                    conv_n = st.text_input("Decision Note", value=item.decision_note or "Converted to project task")
                    if st.form_submit_button("Confirm Conversion (确认立案)"):
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
                    st.markdown(f"**放弃想法: {item.title}**")
                    drop_r2 = st.text_input("Drop Reason (放弃原因，必填)*", key=f"dr2_{item.idea_id}")
                    drop_p2 = st.text_input("Drop Problem / Blocker (当时问题/阻碍，必填)*", key=f"dp2_{item.idea_id}")
                    drop_n2 = st.text_input("Decision Note (说明)", value="Dropped from confirmed queue", key=f"dn2_{item.idea_id}")
                    if st.form_submit_button("Confirm Drop (确认放弃)"):
                        if drop_r2.strip() and drop_p2.strip():
                            try:
                                ideas_store.drop_idea(item.idea_id, drop_reason=drop_r2, drop_problem=drop_p2, note=drop_n2)
                                st.session_state[f"show_drop2_{item.idea_id}"] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Drop failed: {e}")
                        else:
                            st.error("放弃原因 (Drop Reason) 与 当时问题 (Drop Problem) 均为必填项，不得留空。")

    # Category 3: 已立案 (Default Collapsed / Folded)
    converted_list = grouped[STATUS_CONVERTED]
    with st.expander(f"▶ 已立案 ({len(converted_list)})", expanded=False):
        st.caption("ℹ️ *已立案想法之执行进度唯一来源于正式 Project / Milestone / Task SSOT，本页仅保留立案历史档案。*")
        if not converted_list:
            st.write("暂无已立案历史 (0 Ideas)。")
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
                    <b>来源:</b> {item.source}
                </p>
                <p style="margin:4px 0 0 0; font-size:12px; color:#58a6ff;"><b>Decision Note:</b> {item.decision_note or 'Converted to active roadmap'}</p>
            </div>
            """, unsafe_allow_html=True)
            if st.button("🔍 查看专案进度 (View Project)", key=f"btn_view_proj_{item.idea_id}"):
                st.session_state["selected_project_id"] = item.project_id
                st.session_state["nav_selection"] = NAV_PROJECTS
                st.rerun()

    # Category 4: 已放弃 (Default Collapsed / Folded)
    dropped_list = grouped[STATUS_DROPPED]
    with st.expander(f"▶ 已放弃 ({len(dropped_list)})", expanded=False):
        st.caption("ℹ️ *已放弃想法的历史记录完整保留，支持随时恢复。*")
        if not dropped_list:
            st.write("暂无已放弃历史 (0 Ideas)。")
        for item in dropped_list:
            st.markdown(f"""
            <div class="glass-card-dimmed">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h4 style="margin:0; color:#8b949e;">📦 {item.title} <small>({item.idea_id})</small></h4>
                    <span class="badge-err">DROPPED</span>
                </div>
                <p style="margin:6px 0 4px 0; color:#8b949e;">{item.description or '—'}</p>
                <p style="margin:0; font-size:12px; color:#ff7b72;">
                    <b>放弃日期:</b> {item.dropped_at or '—'} |
                    <b>放弃原因:</b> {item.drop_reason or '—'}
                </p>
                {f'<p style="margin:2px 0 0 0; font-size:12px; color:#8b949e;"><b>当时问题/阻碍:</b> {item.drop_problem}</p>' if item.drop_problem else ''}
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
    st.title("📁 Projects & Roadmaps")
    selected_p_id = st.session_state.get("selected_project_id")
    if selected_p_id:
        st.info(f"Targeting Project: **{selected_p_id}**")

    if not all_projects:
        st.info("No projects found in Drive SSOT.")
    else:
        for proj in all_projects:
            p_id = proj.get("project_id", "—")
            p_title = proj.get("title", p_id)
            tasks = [t for t in all_tasks if t.get("project_id") == p_id]
            is_highlighted = (p_id == selected_p_id)

            st.markdown(f"""
            <div class="glass-card" {'style="border: 2px solid #388bfd;"' if is_highlighted else ''}>
                <h3 style="margin:0;">{p_title} <small style="color:#8b949e;">({p_id})</small></h3>
                <p style="margin:6px 0;">Total Tasks: {len(tasks)}</p>
            </div>
            """, unsafe_allow_html=True)
            with st.expander(f"View Tasks for {p_title}", expanded=is_highlighted):
                st.json(tasks)


def render_tasks_page():
    st.title("📋 Tasks Board & Execution")
    board = map_task_board(all_tasks, active_executions_dict, now)

    tab_in_progress, tab_ready, tab_blocked, tab_completed = st.tabs([
        f"🚀 In Progress ({len(board['In progress'])})",
        f"📥 Ready ({len(board['Ready'])})",
        f"⚠️ Blocked / Attention ({len(board['Blocked / Attention'])})",
        f"✅ Completed ({len(board['Completed'])})"
    ])

    def render_task_cards(tasks):
        if not tasks:
            st.write("No tasks in this category.")
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
    st.title("🔍 AI Sessions & Handoff Inspector")
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
                linked_exe = active_executions_dict.get((p_id, t_id))
                if linked_exe:
                    st.info("Active running execution found.")
                    st.write(f"Execution ID: `{linked_exe.get('execution_id')}`")
                    st.write(f"Provider Session: `{linked_exe.get('provider_session_id')}`")
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
    st.title("⚡ Quota & Fleet Forecast")
    st.markdown(f"""
    <div class="recommendation-card">
        <h4 style="margin:0 0 8px 0; color:#58a6ff;">🚀 Primary Recommendation: {daily_brief_vm.recommended_action}</h4>
        <p style="margin:0 0 6px 0;"><b>Target Provider:</b> {daily_brief_vm.recommended_provider or 'None'}</p>
        <p style="margin:0; font-size:14px;"><b>Rationale:</b> {daily_brief_vm.reason}</p>
    </div>
    """, unsafe_allow_html=True)

    cols = st.columns(min(len(daily_brief_vm.accounts), 3) or 1)
    for idx, card in enumerate(daily_brief_vm.accounts):
        with cols[idx % len(cols)]:
            try:
                st.subheader(card.card_title)
                st.metric(
                    label="Primary Quota Remaining",
                    value=card.formatted_five_hour_remaining,
                    delta=f"-{card.formatted_five_hour_burn_rate}" if card.formatted_five_hour_burn_rate != "—" else None,
                    delta_color="inverse"
                )
                if card.formatted_five_hour_countdown != "—":
                    st.write(f"• **Resets In**: `{card.formatted_five_hour_countdown}`")
                if card.extra_credits_available is not None:
                    st.write(f"• **Extra Credits**: `{card.formatted_extra_credits}`")
                st.write(f"• **Effective Availability**: `{card.formatted_effective_availability}`")
                st.caption(f"Source: `{card.source}` ({card.source_type}) | Confidence: `{card.confidence}`")
                st.caption(f"Last updated: {card.last_updated or 'never'}")
                st.markdown("---")
            except Exception as e:
                st.error(f"Error rendering {card.card_title}: {e}")


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
elif selected_nav == NAV_AI_SESSIONS:
    render_sessions_page()
elif selected_nav == NAV_QUOTA:
    render_quota_page()
elif selected_nav == NAV_REVIEWS:
    render_placeholder_page("📝 Session Reviews", "View code reviews, reviewer decisions, and task acceptance archives.")
elif selected_nav == NAV_LOGS:
    render_placeholder_page("📄 System & Task Logs", "Inspect aggregated runtime logs from Watcher and Session Center.")
elif selected_nav == NAV_SETTINGS:
    render_placeholder_page("⚙️ Settings & Configuration", "Manage credentials, account mapping, and refresh intervals.")

if all_warnings:
    st.markdown("---")
    with st.expander(f"⚠️ System Notices ({len(all_warnings)})"):
        for w in all_warnings:
            st.warning(w)
