import streamlit as st
import os
from datetime import datetime, timezone
import pandas as pd

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords, logical_record_id
from manager.quota_reader import read_drive_status, summarize
from manager.quota_history import get_default_quota_history_store
from manager.dashboard_core import (
    parse_time,
    determine_execution_state,
    is_execution_stale,
    get_global_summary,
    map_task_board,
    build_daily_brief_vm,
    DailyBriefViewModel,
    AccountQuotaCardViewModel
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

    /* Sleek glass card */
    .glass-card {
        background: rgba(22, 27, 34, 0.85);
        border: 1px solid #30363d;
        border-radius: 12px;
        padding: 18px 22px;
        margin-bottom: 18px;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25);
    }

    /* Highlight recommendation card */
    .recommendation-card {
        background: linear-gradient(135deg, rgba(22, 27, 34, 0.95), rgba(30, 41, 59, 0.9));
        border: 1px solid #388bfd;
        border-radius: 12px;
        padding: 20px 24px;
        margin-bottom: 22px;
        box-shadow: 0 4px 18px rgba(56, 139, 253, 0.15);
    }

    /* Metrics section */
    .metric-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #58a6ff;
    }
    .metric-label {
        font-size: 0.85rem;
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
        margin-bottom: 4px;
    }
    .badge-ok { background-color: #1f6feb; color: #ffffff; }
    .badge-running { background-color: #238636; color: #ffffff; }
    .badge-waiting { background-color: #d29922; color: #ffffff; }
    .badge-attention { background-color: #d29922; color: #ffffff; }
    .badge-danger { background-color: #f85149; color: #ffffff; }
    .badge-stale { background-color: #da3633; color: #ffffff; }
    .badge-fresh { background-color: #238636; color: #ffffff; }
    .badge-unknown { background-color: #484f58; color: #ffffff; }
    .badge-official { background-color: #388bfd; color: #ffffff; }
    .badge-manual { background-color: #6e7681; color: #ffffff; }

    /* Action Badges */
    .badge-action-consume { background-color: #2ea043; color: #ffffff; font-weight: bold; }
    .badge-action-normal { background-color: #1f6feb; color: #ffffff; }
    .badge-action-conserve { background-color: #bb8009; color: #ffffff; font-weight: bold; }
    .badge-action-hold { background-color: #da3633; color: #ffffff; font-weight: bold; }

    /* Priorities */
    .priority-urgent { color: #f85149; font-weight: bold; }
    .priority-high { color: #db6d28; font-weight: bold; }
    .priority-normal { color: #58a6ff; }
    .priority-low { color: #8b949e; }
</style>
""", unsafe_allow_html=True)


def list_records_isolated(store, area, project_id):
    records = []
    warnings = []
    try:
        parent = store.project_folder(area, project_id, create=False)
        items = store.children(parent)
        for item in items:
            name = item.get("name", "")
            if name.endswith(".json"):
                storage_id = name[:-5]
                try:
                    logical_id = logical_record_id(storage_id)
                    doc = store.get(area, project_id, logical_id)
                    records.append(doc)
                except Exception as exc:
                    warnings.append(f"Malformed record '{name}' in '{area}' for project '{project_id}': {exc}")
    except Exception:
        # Ignore normal missing folder cases
        pass
    return records, warnings


# Cache data loading to prevent unnecessary Drive API calls on interaction
@st.cache_data(ttl=60)
def load_all_data():
    now = datetime.now(timezone.utc)
    all_warnings = []
    try:
        service = build_service()
        store = DriveRecords(service)

        # Load Quota Document
        quota_doc = None
        quota_summary = None
        try:
            quota_doc = read_drive_status(service=service)
            quota_summary = summarize(quota_doc, max_age_minutes=60, now=now)
        except Exception as q_exc:
            all_warnings.append(f"Drive quota status read warning: {q_exc}")
            quota_summary = summarize({}, max_age_minutes=60, now=now)

        # Load Quota Telemetry History (Fail-safe)
        history_snapshots = []
        try:
            history_store = get_default_quota_history_store()
            history_doc = history_store.load()
            history_snapshots = history_doc.get("snapshots", [])
        except Exception as h_exc:
            all_warnings.append(f"Quota history store load warning: {h_exc}")

        # Build Daily Brief ViewModel
        daily_brief_vm = build_daily_brief_vm(
            quota_summary,
            history=history_snapshots,
            now=now,
            max_age_minutes=60.0
        )

        # Load Projects
        projects = []
        try:
            projects = store.list_projects()
        except Exception as p_exc:
            all_warnings.append(f"Drive projects list warning: {p_exc}")

        all_tasks = []
        all_executions = []
        handoffs_dict = {}
        sessions_dict = {}

        for project in projects:
            p_id = project.get("project_id")
            if not p_id:
                continue

            # Read Tasks
            tasks, t_warns = list_records_isolated(store, "tasks", p_id)
            all_tasks.extend(tasks)
            all_warnings.extend(t_warns)

            # Read Executions
            executions, e_warns = list_records_isolated(store, "executions", p_id)
            all_executions.extend(executions)
            all_warnings.extend(e_warns)

            # Read Handoffs
            handoffs, h_warns = list_records_isolated(store, "handoffs", p_id)
            all_warnings.extend(h_warns)
            for ho in handoffs:
                t_id = ho.get("task_id")
                if t_id:
                    key = (p_id, t_id)
                    existing = handoffs_dict.get(key)
                    if not existing or ho.get("created_at", "") > existing.get("created_at", ""):
                        handoffs_dict[key] = ho

            # Read Sessions
            sessions, s_warns = list_records_isolated(store, "sessions", p_id)
            all_warnings.extend(s_warns)
            for s in sessions:
                s_id = s.get("id") or s.get("provider_session_id")
                if s_id:
                    sessions_dict[(p_id, s_id)] = s

        return {
            "success": True,
            "quota_summary": quota_summary,
            "daily_brief_vm": daily_brief_vm,
            "projects": projects,
            "all_tasks": all_tasks,
            "all_executions": all_executions,
            "handoffs_dict": handoffs_dict,
            "sessions_dict": sessions_dict,
            "warnings": all_warnings,
            "error": None
        }
    except Exception as e:
        now = datetime.now(timezone.utc)
        fallback_brief = build_daily_brief_vm({}, now=now)
        return {
            "success": False,
            "quota_summary": summarize({}, max_age_minutes=60, now=now),
            "daily_brief_vm": fallback_brief,
            "projects": [],
            "all_tasks": [],
            "all_executions": [],
            "handoffs_dict": {},
            "sessions_dict": {},
            "warnings": all_warnings + [str(e)],
            "error": str(e)
        }

# Main App Loop
st.title("🤖 ADM Unified Operations Dashboard")
st.caption("AI Operations Command Center — Multi-Account Telemetry, Forecast & Execution Monitor")

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

if not data["success"] and not data.get("all_tasks") and not data.get("daily_brief_vm"):
    st.error(f"Failed to fetch data from Google Drive: {data['error']}")
    st.stop()

# Prepare Data Variables
quota_summary = data["quota_summary"]
daily_brief_vm: DailyBriefViewModel = data["daily_brief_vm"]
projects = data["projects"]
all_tasks = data["all_tasks"]
all_executions = data["all_executions"]
handoffs_dict = data["handoffs_dict"]
sessions_dict = data["sessions_dict"]
all_warnings = data.get("warnings", [])

now = datetime.now(timezone.utc)

# Active Executions mappings
active_executions = [e for e in all_executions if e.get("status") not in {"completed", "failed", "interrupted", "cancelled"}]
active_executions_dict = {(e.get("project_id"), e.get("task_id")): e for e in active_executions}

# Global Metrics
summary_metrics = get_global_summary(quota_summary.get("providers", []), all_tasks, active_executions)
reliable_count = sum(1 for a in daily_brief_vm.accounts if a.has_reliable_quota)
total_accounts_count = len(daily_brief_vm.accounts)

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
        <div class="metric-value">{reliable_count} / {total_accounts_count}</div>
    </div>
    """, unsafe_allow_html=True)

# =====================================================================
# Section A: Today's AI Recommendation (Daily Brief)
# =====================================================================
st.header("🎯 Today's AI Recommendation")

action_badge_class = {
    "consume": "badge-action-consume",
    "normal": "badge-action-normal",
    "conserve": "badge-action-conserve",
    "hold": "badge-action-hold"
}.get(daily_brief_vm.recommended_action, "badge-unknown")

action_label = daily_brief_vm.recommended_action.upper()

st.markdown(f"""
<div class="recommendation-card">
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <span style="font-size: 1.25rem; font-weight: 700; color: #ffffff;">
            Recommended: <span style="color: #58a6ff;">{daily_brief_vm.recommended_display_name}</span>
        </span>
        <span class="badge {action_badge_class}" style="font-size: 0.85rem; padding: 6px 14px;">
            ACTION: {action_label}
        </span>
    </div>
    <div style="font-size: 0.95rem; line-height: 1.5; color: #e6edf3; margin-bottom: 10px;">
        <b>Reason:</b> {daily_brief_vm.reason}
    </div>
    <div style="font-size: 0.85rem; color: #8b949e;">
        ⏳ <b>Nearest Cycle Reset:</b> {daily_brief_vm.nearest_reset_countdown} &nbsp;|&nbsp;
        Generated at: <code>{daily_brief_vm.generated_at}</code>
    </div>
</div>
""", unsafe_allow_html=True)

# Display telemetry warnings if present
if daily_brief_vm.telemetry_warnings:
    with st.expander(f"⚠️ Telemetry & Quota Alerts ({len(daily_brief_vm.telemetry_warnings)})"):
        for tw in daily_brief_vm.telemetry_warnings:
            st.warning(tw)

st.markdown("---")

# =====================================================================
# Section B: Provider / Account Quota Center (Multi-Account)
# =====================================================================
st.header("⚡ Provider & Account Quota Center")
accounts_list = daily_brief_vm.accounts

if not accounts_list:
    st.info("No AI providers configured in quota status.")
else:
    # Render in columns (max 3 or 4 per row)
    num_cols = min(len(accounts_list), 3)
    quota_cols = st.columns(num_cols)

    for idx, card in enumerate(accounts_list):
        col_idx = idx % num_cols
        with quota_cols[col_idx]:
            try:
                st.markdown(f"### {card.card_title}")

                # Freshness badge
                if card.stale:
                    fresh_class = "badge-stale"
                    freshness_text = "STALE"
                else:
                    fresh_class = "badge-fresh"
                    freshness_text = "FRESH"

                # Status badge
                status_class = "badge-ok" if card.status.lower() == "ok" else "badge-attention"

                # Action recommendation badge
                action_bg = {
                    "urgent_consume": "badge-action-consume",
                    "suggest_consume": "badge-action-consume",
                    "normal_use": "badge-action-normal",
                    "conserve": "badge-action-conserve",
                    "hold": "badge-action-hold"
                }.get(card.action_recommendation.lower(), "badge-unknown")

                action_text = card.action_recommendation.replace("_", " ").upper()

                st.markdown(f"""
                <div>
                    <span class="badge {status_class}">STATUS: {card.status.upper()}</span>
                    <span class="badge {fresh_class}">{freshness_text}</span>
                    <span class="badge {action_bg}">{action_text}</span>
                </div>
                """, unsafe_allow_html=True)

                # 5-Hour Quota Display
                st.markdown("#### 5-Hour Window")
                if card.stale:
                    st.warning(f"Stale Quota (Remaining: {card.formatted_five_hour_remaining})")
                elif card.five_hour_remaining_pct is not None:
                    try:
                        rem_val = float(card.five_hour_remaining_pct)
                        st.progress(rem_val / 100.0)
                        used_text = f"{card.five_hour_used_pct:.1f}%" if card.five_hour_used_pct is not None else "—"
                        st.write(f"Remaining: **{card.formatted_five_hour_remaining}** (Used: {used_text})")
                    except (ValueError, TypeError):
                        st.write(f"Remaining: **Unknown**")
                else:
                    used_text = f"{card.five_hour_used_pct:.1f}%" if card.five_hour_used_pct is not None else "—"
                    st.info(f"Percentage not reported (Used: {used_text})")

                # 5-Hour Forecast Details
                st.write(f"• **Reset Clock**: `{card.formatted_five_hour_countdown}`")
                st.write(f"• **Burn Rate**: `{card.formatted_five_hour_burn_rate}`")
                st.write(f"• **Projected at Reset**: `{card.formatted_five_hour_projected}`")

                # Weekly Quota Display (if exists)
                if card.has_weekly_window:
                    st.markdown("#### Weekly Window")
                    w_used_text = f"{card.weekly_used_pct:.1f}%" if card.weekly_used_pct is not None else "—"
                    st.write(f"• **Remaining**: **{card.formatted_weekly_remaining}** (Used: {w_used_text})")
                    st.write(f"• **Reset**: `{card.formatted_weekly_countdown}`")
                    if card.weekly_action_recommendation in ("conserve", "hold"):
                        st.caption(f"⚠️ Weekly status: {card.weekly_action_recommendation.upper()}")

                # Metadata / Telemetry
                st.caption(f"Source: `{card.source}` ({card.source_type}) | Confidence: `{card.confidence}`")
                st.caption(f"Last updated: {card.last_updated or 'never'}")

                if card.warning_reason:
                    st.caption(f"ℹ️ *{card.warning_reason}*")

                st.markdown("---")
            except Exception as e:
                st.error(f"Error rendering account {card.card_title}: {e}")

st.markdown("---")

# =====================================================================
# Section C: Running & Active Executions Table
# =====================================================================
st.header("🔄 Running & Active Executions")
if not active_executions:
    st.info("No active AI executions running currently.")
else:
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

st.markdown("---")

# =====================================================================
# Section D: Task Board (Tabs for columns)
# =====================================================================
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

# =====================================================================
# Section E: Session & Handoff Inspector
# =====================================================================
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

if all_warnings:
    st.markdown("---")
    with st.expander(f"⚠️ Partial Data Warnings ({len(all_warnings)})"):
        for w in all_warnings:
            st.warning(w)
