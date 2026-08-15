import streamlit as st
import os
from datetime import datetime, timezone
import pandas as pd

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords
from manager.quota_reader import read_drive_status, summarize
from manager.dashboard_core import (
    parse_time,
    determine_execution_state,
    is_execution_stale,
    get_global_summary,
    map_task_board
)

# Page Configuration
st.set_page_config(
    page_title="ADM Unified Operations Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Dark Mode custom CSS Injection for Rich Glassmorphism Aesthetics
st.markdown("""
<style>
    /* Main body background & font family */
    .stApp {
        background-color: #0d1117;
        color: #c9d1d9;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    
    /* Sleek container style */
    .glass-card {
        background: rgba(22, 27, 34, 0.8);
        border: 1px solid #30363d;
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 20px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    }
    
    /* Metrics section */
    .metric-value {
        font-size: 2rem;
        font-weight: 700;
        color: #58a6ff;
    }
    .metric-label {
        font-size: 0.9rem;
        color: #8b949e;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    
    /* Status Badges */
    .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 12px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        margin-right: 5px;
    }
    .badge-ok { background-color: #1f6feb; color: #ffffff; }
    .badge-running { background-color: #238636; color: #ffffff; }
    .badge-waiting { background-color: #d29922; color: #ffffff; }
    .badge-attention { background-color: #f85149; color: #ffffff; }
    .badge-stale { background-color: #f85149; color: #ffffff; }
    .badge-fresh { background-color: #238636; color: #ffffff; }
    .badge-unknown { background-color: #484f58; color: #ffffff; }
    
    /* Priorities */
    .priority-urgent { color: #f85149; font-weight: bold; }
    .priority-high { color: #db6d28; font-weight: bold; }
    .priority-normal { color: #58a6ff; }
    .priority-low { color: #8b949e; }
</style>
""", unsafe_allow_html=True)

# Cache data loading to prevent unnecessary Drive API calls on interaction
@st.cache_data(ttl=60)
def load_all_data():
    try:
        service = build_service()
        store = DriveRecords(service)
        
        # Load Quota Document
        quota_doc = read_drive_status(service=service)
        quota_summary = summarize(quota_doc, max_age_minutes=60)
        
        # Load Projects
        projects = store.list_projects()
        
        all_tasks = []
        all_executions = []
        handoffs_dict = {}
        sessions_dict = {}
        
        for project in projects:
            p_id = project.get("project_id")
            if not p_id:
                continue
            
            # Read Tasks
            try:
                tasks = store.list_records("tasks", p_id)
                all_tasks.extend(tasks)
            except Exception:
                pass
                
            # Read Executions
            try:
                executions = store.list_records("executions", p_id)
                all_executions.extend(executions)
            except Exception:
                pass
                
            # Read Handoffs
            try:
                handoffs = store.list_records("handoffs", p_id)
                for ho in handoffs:
                    t_id = ho.get("task_id")
                    if t_id:
                        key = (p_id, t_id)
                        existing = handoffs_dict.get(key)
                        if not existing or ho.get("created_at", "") > existing.get("created_at", ""):
                            handoffs_dict[key] = ho
            except Exception:
                pass
                
            # Read Sessions
            try:
                sessions = store.list_records("sessions", p_id)
                for s in sessions:
                    s_id = s.get("id") or s.get("provider_session_id")
                    if s_id:
                        sessions_dict[(p_id, s_id)] = s
            except Exception:
                pass
                
        return {
            "success": True,
            "quota_summary": quota_summary,
            "projects": projects,
            "all_tasks": all_tasks,
            "all_executions": all_executions,
            "handoffs_dict": handoffs_dict,
            "sessions_dict": sessions_dict,
            "error": None
        }
    except Exception as e:
        return {
            "success": False,
            "quota_summary": None,
            "projects": [],
            "all_tasks": [],
            "all_executions": [],
            "handoffs_dict": {},
            "sessions_dict": {},
            "error": str(e)
        }

# Main App Loop
st.title("🤖 ADM Unified Operations Dashboard")
st.caption("Single local dashboard to monitor Tasks, Executions, Sessions, and Quotas from Google Drive.")

# Sidebar Refresh & Status
with st.sidebar:
    st.header("Control Panel")
    if st.button("🔄 Sync with Google Drive", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    
    st.markdown("---")
    st.markdown("### Runtime Status")
    st.info("Read-only execution monitoring active. Local Drive token is valid.")
    
    # Auto-refresh check
    st.markdown("---")
    st.caption("Data is cached locally for 60 seconds to respect API rate limits.")

# Load Data
data = load_all_data()

if not data["success"]:
    st.error(f"Failed to fetch data from Google Drive: {data['error']}")
    st.stop()

# Prepare Data Variables
quota_summary = data["quota_summary"]
projects = data["projects"]
all_tasks = data["all_tasks"]
all_executions = data["all_executions"]
handoffs_dict = data["handoffs_dict"]
sessions_dict = data["sessions_dict"]

now = datetime.now(timezone.utc)

# Active Executions mappings
active_executions = [e for e in all_executions if e.get("status") not in {"completed", "failed", "interrupted", "cancelled"}]
active_executions_dict = {(e.get("project_id"), e.get("task_id")): e for e in active_executions}

# Global Metrics
summary_metrics = get_global_summary(quota_summary.get("providers", []), all_tasks, active_executions)

# Display Global Summary
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.markdown(f"""
    <div class="glass-card">
        <div class="metric-label">Running Tasks</div>
        <div class="metric-value">{summary_metrics['running_tasks_count']}</div>
    </div>
    """, unsafe_allow_html=True)
with m2:
    st.markdown(f"""
    <div class="glass-card">
        <div class="metric-label">Blocked Tasks</div>
        <div class="metric-value">{summary_metrics['blocked_tasks_count']}</div>
    </div>
    """, unsafe_allow_html=True)
with m3:
    st.markdown(f"""
    <div class="glass-card">
        <div class="metric-label">Active Sessions</div>
        <div class="metric-value">{summary_metrics['active_sessions_count']}</div>
    </div>
    """, unsafe_allow_html=True)
with m4:
    st.markdown(f"""
    <div class="glass-card">
        <div class="metric-label">Reliable Quotas</div>
        <div class="metric-value">{summary_metrics['reliable_providers_count']} / 4</div>
    </div>
    """, unsafe_allow_html=True)

# Layout: 1. Quota Columns
st.header("⚡ Provider / Quota Center")
quota_cols = st.columns(len(quota_summary["providers"]))

for idx, p in enumerate(quota_summary["providers"]):
    with quota_cols[idx]:
        st.markdown(f"### {p['display_name']}")
        
        status_val = p.get("status", "unknown").upper()
        freshness_val = p.get("freshness", "stale").upper()
        
        status_class = "badge-ok" if status_val == "OK" else "badge-attention"
        fresh_class = "badge-fresh" if freshness_val == "FRESH" else "badge-stale"
        
        st.markdown(f"""
        <span class="badge {status_class}">Status: {status_val}</span>
        <span class="badge {fresh_class}">{freshness_val}</span>
        """, unsafe_allow_html=True)
        
        # Display window remaining percentage if exists
        windows = p.get("windows", [])
        if windows and not p.get("stale"):
            for w in windows:
                rem_pct = w.get("remaining_percent", 0)
                st.progress(rem_pct / 100.0)
                st.write(f"Remaining: **{rem_pct}%** (Used: {w.get('used_percent', 0)}%)")
        else:
            st.warning("No fresh quota window data")
            
        st.write(f"Confidence: `{p.get('confidence', 'unknown')}`")
        st.write(f"Source: `{p.get('source', 'unknown')}`")
        st.write(f"Reset Clock: `{p.get('nearest_reset_at') or '—'}`")
        st.caption(f"Last updated: {p.get('last_updated') or 'never'}")

st.markdown("---")

# Layout: 2. Running Executions Table
st.header("🔄 Running & Active Executions")
if not active_executions:
    st.info("No active AI executions running currently.")
else:
    exec_rows = []
    for exe in active_executions:
        p_id = exe.get("project_id", "—")
        t_id = exe.get("task_id", "—")
        provider = exe.get("provider", "—")
        
        task_snapshot = exe.get("task_snapshot", {})
        model = task_snapshot.get("model", "—")
        mode = exe.get("mode") or task_snapshot.get("mode") or "—"
        effort = exe.get("effort") or task_snapshot.get("effort") or "—"
        
        session_id = exe.get("provider_session_id") or "—"
        ui_state = determine_execution_state(exe, now)
        progress = exe.get("last_provider_event") or "—"
        hb_at = exe.get("heartbeat_at") or "—"
        
        # Calculate elapsed
        start_time = parse_time(exe.get("started_at") or exe.get("reserved_at"))
        elapsed_str = "—"
        if start_time:
            elapsed_m = (now - start_time).total_seconds() / 60
            elapsed_str = f"{elapsed_m:.1f} min"
            
        expected_str = f"{task_snapshot.get('expected_minutes', '—')} min"
        
        is_stale = is_execution_stale(exe, now)
        attention = "⚠️ ATTENTION" if is_stale else "✅ OK"
        
        exec_rows.append({
            "Project": p_id,
            "Task": t_id,
            "AI Provider": provider,
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

st.markdown("---")

# Layout: 3. Task Board (Tabs for columns)
st.header("📋 Task Board")
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
            <p>
                Priority: <span class="priority-{priority}">{priority.upper()}</span> | 
                AI: <b>{provider}</b>
            </p>
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

st.markdown("---")

# Layout: 4. Session & Handoff Inspector
st.header("🔍 Session & Handoff Detail Inspector")
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
        
        # Find the task
        task = next((t for t in all_tasks if t.get("project_id") == p_id and t.get("task_id") == t_id), {})
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Task Details")
            st.write(f"**Status**: `{task.get('status')}`")
            st.write(f"**Recommended Provider**: `{task.get('recommended_provider')}`")
            st.write(f"**Assigned Provider**: `{task.get('assigned_provider')}`")
            st.write(f"**Next Action**: {task.get('next_action')}")
            st.write(f"**CWD**: `{task.get('working_directory')}`")
            st.write(f"**Branch**: `{task.get('branch')}`")
            
            # Check for linked execution
            linked_exe = active_executions_dict.get((p_id, t_id))
            if linked_exe:
                st.info("There is an active running execution for this task.")
                st.write(f"Execution ID: `{linked_exe.get('execution_id')}`")
                st.write(f"Provider Session ID: `{linked_exe.get('provider_session_id')}`")
                st.write(f"Heartbeat: `{linked_exe.get('heartbeat_at')}`")
            else:
                st.write("No active execution.")

        with col2:
            st.subheader("Latest Handoff")
            ho = handoffs_dict.get((p_id, t_id))
            if ho:
                st.write(f"Handoff ID: `{ho.get('handoff_id')}`")
                st.write(f"Created At: `{ho.get('created_at')}`")
                st.write(f"Reason: `{ho.get('reason')}`")
                st.write(f"Next Action: {ho.get('next_action')}")
                
                # Expand completed work & changes
                with st.expander("Completed Work"):
                    st.write(ho.get("completed_work", []))
                    
                files_changed = ho.get("files_changed", [])
                if files_changed:
                    with st.expander("Files Changed"):
                        st.write(files_changed)
                        
                commits = ho.get("commits", [])
                if commits:
                    with st.expander("Commits"):
                        st.write(commits)
            else:
                st.write("No handoff records found for this task.")
