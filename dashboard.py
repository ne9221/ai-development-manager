import streamlit as st
import os
import json
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
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
    AccountQuotaCardViewModel,
    parse_scheduled_task_health,
    build_session_center_health,
    UNKNOWN_LABEL,
    DISPATCH_STATE_RUNNING,
    build_dispatch_truth_row,
    compute_visible_dispatch_gate,
    parse_task_to_run_path,
    build_provenance_vm,
    compute_provenance_gate,
    compute_overall_visible_dispatch_gate,
    validate_provenance_evidence_document,
    reconcile_watcher_provenance_evidence,
)

WATCHER_TASK_NAME = "AI Development Manager - Command Watcher"
SUPERVISOR_TASK_NAME = "AI Development Manager - Session Center Supervisor"
SESSION_CENTER_URL = "http://127.0.0.1:8765"
DASHBOARD_PROJECT_ID = os.environ.get("ADM_DASHBOARD_PROJECT_ID", "ai-development-manager")
RECENT_RECORD_LIMIT = 6


def query_scheduled_task_raw(task_name):
    """Shell out to schtasks for a task's live state. Returns None on any failure
    (schtasks unavailable, non-Windows host, permission error) so the caller can
    report Unknown rather than fabricating a state."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:
        return None


def query_session_center_raw():
    """Probe the local Session Center HTTP endpoints. Returns (listening, session_dict)."""
    try:
        with urllib.request.urlopen(f"{SESSION_CENTER_URL}/health", timeout=2) as resp:
            listening = resp.status == 200
    except Exception:
        listening = False

    session = None
    if listening:
        try:
            with urllib.request.urlopen(f"{SESSION_CENTER_URL}/api/session", timeout=2) as resp:
                session = json.loads(resp.read().decode("utf-8"))
        except Exception:
            session = None
    return listening, session


def query_scheduled_task_verbose_raw(task_name):
    """Like query_scheduled_task_raw, but /V so "Task To Run:" (the real
    launcher script path) is included -- needed to discover which on-disk
    checkout the production Watcher is actually running from."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:
        return None


def discover_repository_root_from_script_path(script_path):
    """Walk up from a launcher script's path to the nearest git repo root
    (a directory containing .git) -- deliberately doesn't hardcode the
    wrapper's internal layout (e.g. manager/generated/*.vbs), so it stays
    correct if that layout ever changes."""
    if not script_path:
        return None
    try:
        current = Path(script_path).resolve().parent
        for _ in range(10):
            if (current / ".git").exists():
                return str(current)
            if current.parent == current:
                break
            current = current.parent
    except Exception:
        pass
    return None


def query_git_head_raw(repository_path):
    """(branch, sha) via real `git` introspection of an on-disk checkout;
    (None, None) on any failure -- never a guessed or cached value."""
    if not repository_path:
        return None, None
    try:
        branch_res = subprocess.run(
            ["git", "-C", repository_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        sha_res = subprocess.run(
            ["git", "-C", repository_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        branch = branch_res.stdout.strip() if branch_res.returncode == 0 else None
        sha = sha_res.stdout.strip() if sha_res.returncode == 0 else None
        return branch or None, sha or None
    except Exception:
        return None, None


def read_provenance_evidence_file():
    """Read the persisted Production Provenance Contract evidence file, if
    one exists, from <AI_MANAGER_HOME>/provenance/runtime_evidence.json --
    the same AI_MANAGER_HOME convention already used by
    manager/quota_history.py, manager/refresh_status.py, and
    manager/claude_config_locks.py. Returns the raw parsed document, or
    None on any failure (missing file, unreadable, malformed JSON) -- the
    caller (validate_provenance_evidence_document) decides whether its
    shape can be trusted."""
    home = os.environ.get("AI_MANAGER_HOME") or os.path.expanduser("~/.ai-development-manager")
    evidence_path = Path(home) / "provenance" / "runtime_evidence.json"
    try:
        return json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception:
        return None


@st.cache_data(ttl=15)
def load_infra_health():
    watcher_vm = parse_scheduled_task_health(WATCHER_TASK_NAME, query_scheduled_task_raw(WATCHER_TASK_NAME))
    supervisor_vm = parse_scheduled_task_health(SUPERVISOR_TASK_NAME, query_scheduled_task_raw(SUPERVISOR_TASK_NAME))
    listening, session = query_session_center_raw()
    session_center_vm = build_session_center_health(listening, session)
    return watcher_vm, supervisor_vm, session_center_vm

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


def list_records_isolated(store, area, project_id, limit=RECENT_RECORD_LIMIT, include_ids=None):
    records = []
    warnings = []
    try:
        parent = store.project_folder(area, project_id, create=False)
        items = sorted(
            store.children(parent),
            key=lambda item: item.get("modifiedTime") or "",
            reverse=True,
        )
        json_items = [item for item in items if item.get("name", "").endswith(".json")]
        selected_items = json_items[:limit]
        if include_ids:
            selected_names = {item.get("name") for item in selected_items}
            selected_items.extend(
                item for item in json_items[limit:]
                if item.get("name") not in selected_names
                and logical_record_id(item["name"][:-5]) in include_ids
            )
        for item in selected_items:
            name = item.get("name", "")
            storage_id = name[:-5]
            try:
                if item.get("id"):
                    raw = store.files.get_media(fileId=item["id"]).execute()
                    doc = json.loads(raw.decode("utf-8"))
                else:  # Test doubles and legacy adapters may not expose Drive file IDs.
                    logical_id = logical_record_id(storage_id)
                    doc = store.get(area, project_id, logical_id)
                records.append(doc)
            except Exception as exc:
                warnings.append(f"Malformed record '{name}' in '{area}' for project '{project_id}': {exc}")
    except Exception:
        # Ignore normal missing folder cases
        pass
    return records, warnings


# Recent-first reads keep the HOME view bounded; the manual sync button clears
# this short cache immediately when the user needs a fresh lifecycle state.
@st.cache_data(ttl=60)
def load_all_data():
    load_started = time.perf_counter()
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

        # HOME is the live ai-development-manager operations view. Historical
        # smoke projects stay in Drive but are not allowed to block first paint.
        projects = []
        try:
            project = store.get("projects", DASHBOARD_PROJECT_ID, DASHBOARD_PROJECT_ID)
            projects = [project] if isinstance(project, dict) and project.get("project_id") else store.list_projects()
        except Exception as p_exc:
            try:
                projects = store.list_projects()
            except Exception:
                all_warnings.append(f"Drive project read warning: {p_exc}")

        all_tasks = []
        all_commands = []
        all_executions = []
        handoffs_dict = {}
        sessions_dict = {}

        for project in projects:
            p_id = project.get("project_id")
            if not p_id:
                continue

            # Read Commands
            commands, c_warns = list_records_isolated(store, "commands", p_id)
            all_commands.extend(commands)
            all_warnings.extend(c_warns)

            # Read Executions
            executions, e_warns = list_records_isolated(store, "executions", p_id)
            all_executions.extend(executions)
            all_warnings.extend(e_warns)

            active_task_ids = {
                record.get("task_id") for record in [*commands, *executions]
                if record.get("task_id") and record.get("status") not in {"completed", "failed", "interrupted", "cancelled", "rejected"}
            }
            # Read recent Tasks plus any Task owning a current lifecycle record.
            tasks, t_warns = list_records_isolated(store, "tasks", p_id, include_ids=active_task_ids)
            all_tasks.extend(tasks)
            all_warnings.extend(t_warns)

            # Historical handoff/session detail is intentionally deferred from
            # the P0 first paint. Session identity is authoritative on Execution.

        return {
            "success": True,
            "quota_summary": quota_summary,
            "daily_brief_vm": daily_brief_vm,
            "projects": projects,
            "all_tasks": all_tasks,
            "all_commands": all_commands,
            "all_executions": all_executions,
            "handoffs_dict": handoffs_dict,
            "sessions_dict": sessions_dict,
            "load_duration_seconds": round(time.perf_counter() - load_started, 3),
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
            "all_commands": [],
            "all_executions": [],
            "handoffs_dict": {},
            "sessions_dict": {},
            "load_duration_seconds": round(time.perf_counter() - load_started, 3),
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
    st.caption("Recent HOME data is cached for 60 seconds. Sync clears the cache immediately.")

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
all_commands = data["all_commands"]
all_executions = data["all_executions"]
handoffs_dict = data["handoffs_dict"]
sessions_dict = data["sessions_dict"]
all_warnings = data.get("warnings", [])
load_duration_seconds = data.get("load_duration_seconds")

now = datetime.now(timezone.utc)

# =====================================================================
# P0: User-visible lifecycle truth (Task + Command + Execution)
# =====================================================================
st.header("📡 Current Execution Visibility")
tasks_by_id = {task.get("task_id"): task for task in all_tasks if task.get("task_id")}
commands_by_task = {}
for command in all_commands:
    task_id = command.get("task_id")
    if task_id and (task_id not in commands_by_task or
                    (command.get("updated_at") or command.get("created_at") or "") >
                    (commands_by_task[task_id].get("updated_at") or commands_by_task[task_id].get("created_at") or "")):
        commands_by_task[task_id] = command
executions_by_task = {}
for execution in all_executions:
    task_id = execution.get("task_id")
    if task_id and (task_id not in executions_by_task or
                    (execution.get("heartbeat_at") or execution.get("completed_at") or execution.get("started_at") or execution.get("reserved_at") or "") >
                    (executions_by_task[task_id].get("heartbeat_at") or executions_by_task[task_id].get("completed_at") or executions_by_task[task_id].get("started_at") or executions_by_task[task_id].get("reserved_at") or "")):
        executions_by_task[task_id] = execution

visibility_rows = []
for task_id in dict.fromkeys([*tasks_by_id, *commands_by_task, *executions_by_task]):
    task = tasks_by_id.get(task_id, {})
    command = commands_by_task.get(task_id, {})
    execution = executions_by_task.get(task_id, {})
    execution_status = execution.get("status")
    command_status = command.get("status")
    if execution_status in {"failed", "interrupted", "cancelled"} or command_status in {"attention", "failed", "rejected"}:
        category = "Attention"
    elif execution_status == "completed" or task.get("status") == "completed":
        category = "Completed"
    elif command_status == "queued" or task.get("status") in {"ready", "queued"}:
        category = "Queued"
    elif execution and is_execution_stale(execution, now):
        category = "Attention"
    else:
        category = "Active"
    snapshot = execution.get("task_snapshot") or {}
    visibility_rows.append({
        "Class": category,
        "AI": execution.get("provider") or command.get("provider") or task.get("assigned_provider") or task.get("recommended_provider") or "—",
        "Task title": task.get("title") or snapshot.get("title") or "—",
        "Task status": task.get("status") or "—",
        "Command status": command_status or "—",
        "Execution status": execution_status or "—",
        "Session ID": execution.get("provider_session_id") or execution.get("session_id") or (command.get("result") or {}).get("session_id") or "—",
        "Task ID": task_id or "—",
        "Model / mode": f"{execution.get('model') or command.get('model') or snapshot.get('model') or '—'} / {execution.get('mode') or command.get('mode') or snapshot.get('mode') or task.get('mode') or '—'}",
        "Working directory": task.get("working_directory") or snapshot.get("working_directory") or "—",
        "Started at": execution.get("started_at") or execution.get("reserved_at") or command.get("claimed_at") or "—",
        "Last updated": execution.get("heartbeat_at") or execution.get("completed_at") or task.get("updated_at") or command.get("completed_at") or command.get("created_at") or "—",
    })

visibility_rows.sort(key=lambda row: row["Last updated"], reverse=True)
visibility_rows.sort(key=lambda row: {"Active": 0, "Queued": 1, "Attention": 2, "Completed": 3}[row["Class"]])
if visibility_rows:
    st.dataframe(pd.DataFrame(visibility_rows), use_container_width=True, hide_index=True)
else:
    st.info("No recent HOME Task / Command / Execution records found.")
st.caption(f"Showing the {RECENT_RECORD_LIMIT} most recently modified records per lifecycle type for `{DASHBOARD_PROJECT_ID}`. Drive load: {load_duration_seconds}s. Use Sync with Google Drive for an immediate refresh.")

# =====================================================================
# Visible Dispatch Truth Gate: stricter, task-provider-account-quota-bound
# truth than the table above. Never displays a pre-authority dispatch
# state (SUBMITTED/ACCEPTED/QUEUED/CLAIMED) as RUNNING -- RUNNING requires
# both Command.status == "running" AND determine_execution_state() to
# independently prove provider session evidence. Any field the SSOT does
# not prove is the literal "UNKNOWN"/"STALE", never guessed. See
# manager/dashboard_core.py's compute_dispatch_state()/build_quota_truth()
# for the full contract.
# =====================================================================
st.header("🔎 Visible Dispatch Truth Gate")

_dispatch_commands_by_task = {}
for _cmd in all_commands:
    _key = (_cmd.get("project_id"), _cmd.get("task_id"))
    _existing = _dispatch_commands_by_task.get(_key)
    if _existing is None or (_cmd.get("created_at") or "") >= (_existing.get("created_at") or ""):
        _dispatch_commands_by_task[_key] = _cmd

_dispatch_executions_by_id = {e.get("execution_id"): e for e in all_executions if e.get("execution_id")}
_dispatch_projects_by_id = {p.get("project_id"): p for p in projects if p.get("project_id")}

_dispatch_rows = []
for _task in all_tasks:
    if _task.get("status") == "cancelled":
        continue
    _key = (_task.get("project_id"), _task.get("task_id"))
    _command = _dispatch_commands_by_task.get(_key)
    _execution = _dispatch_executions_by_id.get(_command.get("execution_id")) if _command else None
    _project = _dispatch_projects_by_id.get(_task.get("project_id"))
    _dispatch_rows.append(build_dispatch_truth_row(_project, _task, _command, _execution, daily_brief_vm.accounts, now))

dispatch_gate = compute_visible_dispatch_gate(_dispatch_rows)

# Production Provenance: this Dashboard's own identity vs. the real running
# Command Watcher's identity. Real git/schtasks introspection only -- no
# cached/mock/demo values. See build_provenance_vm()'s docstring for why
# tested_sha/activated_sha are currently UNKNOWN (no Production Provenance
# Contract evidence exists anywhere in this repo yet).
_dashboard_repo_path = str(Path(__file__).resolve().parent)
_dashboard_branch, _dashboard_sha = query_git_head_raw(_dashboard_repo_path)

_watcher_task_raw = query_scheduled_task_verbose_raw(WATCHER_TASK_NAME)
_watcher_script_path = parse_task_to_run_path(_watcher_task_raw)
_watcher_repo_path = discover_repository_root_from_script_path(_watcher_script_path)
_watcher_branch, _watcher_running_sha = query_git_head_raw(_watcher_repo_path)
_watcher_task_health = parse_scheduled_task_health(WATCHER_TASK_NAME, query_scheduled_task_raw(WATCHER_TASK_NAME))
_watcher_running = _watcher_task_health.found and _watcher_task_health.detail.lower().startswith("running")

_provenance_task = _provenance_command = _provenance_execution = None
for _task in all_tasks:
    _command = _dispatch_commands_by_task.get((_task.get("project_id"), _task.get("task_id")))
    _execution = _dispatch_executions_by_id.get(_command.get("execution_id")) if _command else None
    if _command and _execution and _command.get("status") == "running" and _execution.get("status") == "running":
        _provenance_task, _provenance_command, _provenance_execution = _task, _command, _execution
        break

# Production Provenance Contract evidence: only trusted for tested_sha/
# activated_sha when its own repository_path AND running_sha agree with
# what we just independently observed above via real git introspection --
# a stale or mismatched evidence file is never silently trusted.
_evidence_doc = validate_provenance_evidence_document(read_provenance_evidence_file())
_evidence = reconcile_watcher_provenance_evidence(
    _watcher_repo_path, _watcher_running_sha, _evidence_doc, now=now,
    watcher_running=_watcher_running, active_task=_provenance_task,
    active_command=_provenance_command, active_execution=_provenance_execution,
)

provenance_vm = build_provenance_vm(
    _dashboard_repo_path, _dashboard_branch, _dashboard_sha,
    _watcher_repo_path, _watcher_branch, _watcher_running_sha,
    watcher_tested_sha=_evidence["tested_sha"], watcher_activated_sha=_evidence["activated_sha"], now=now,
    evidence_source=_evidence["note"],
)
provenance_gate = compute_provenance_gate(provenance_vm)
overall_gate = compute_overall_visible_dispatch_gate(dispatch_gate, provenance_gate)

if overall_gate["result"] == "PASS":
    st.success("VISIBLE DISPATCH GATE: PASS")
else:
    st.error("VISIBLE DISPATCH GATE: FAIL")
    with st.expander(f"Reasons ({len(overall_gate['reasons'])})", expanded=True):
        for _reason in overall_gate["reasons"]:
            st.write(f"- {_reason}")

with st.expander("🧬 Production Provenance (Dashboard vs. Watcher runtime identity)", expanded=(provenance_gate["result"] == "FAIL")):
    _col1, _col2 = st.columns(2)
    with _col1:
        st.markdown("**Dashboard (this page)**")
        st.write(f"repository_path=`{provenance_vm.dashboard_repository_path}`")
        st.write(f"branch=`{provenance_vm.dashboard_branch}`")
        st.write(f"reviewed_sha=`{provenance_vm.dashboard_reviewed_sha}`")
    with _col2:
        st.markdown(f"**Watcher ({WATCHER_TASK_NAME})**")
        st.write(f"repository_path=`{provenance_vm.watcher_repository_path}`")
        st.write(f"branch=`{provenance_vm.watcher_branch}`")
        st.write(f"running_sha=`{provenance_vm.watcher_running_sha}`")
        st.write(f"tested_sha=`{provenance_vm.watcher_tested_sha}`")
        st.write(f"activated_sha=`{provenance_vm.watcher_activated_sha}`")
    st.caption(f"captured_at: {provenance_vm.captured_at} · evidence_source: {provenance_vm.evidence_source}")
    if provenance_gate["result"] == "PASS":
        st.success(provenance_vm.match_detail)
    else:
        st.error(provenance_vm.match_detail)

if not _dispatch_rows:
    st.info("No tasks to show.")
else:
    for _row in _dispatch_rows:
        _state = _row["dispatch_state"]
        _badge = "🟢" if _state == DISPATCH_STATE_RUNNING else ("🔴" if _state in ("FAILED", "BLOCKED") else "⚪")
        with st.container():
            st.markdown(f"""<div class="glass-card">
                <b>{_badge} {_row['project_name']} → {_row['task_title']}</b><br>
                <b>Dispatch State:</b> <code>{_state}</code> — {_row['dispatch_reason']}<br>
                <b>AI / Account:</b> {_row['provider']} / {_row['account_id']} · <b>Model/Mode:</b> {_row['model']} / {_row['mode']}<br>
                <b>5h remaining:</b> {_row['quota']['formatted_five_hour_remaining']} (resets {_row['quota']['formatted_five_hour_reset_at']})
                · <b>Weekly remaining:</b> {_row['quota']['formatted_weekly_remaining']} (resets {_row['quota']['formatted_weekly_reset_at']})<br>
                <b>Quota captured_at:</b> {_row['quota']['formatted_captured_at']} · <b>Freshness:</b> {_row['quota']['freshness']}
                </div>""", unsafe_allow_html=True)
            with st.expander("Technical IDs"):
                st.write(f"project_id=`{_row['project_id']}` · task_id=`{_row['task_id']}`")
                st.write(f"execution_id=`{_row['execution_id']}` · session_id=`{_row['session_id']}`")

st.markdown("---")

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
# Section: Watcher & Session Center Health
# =====================================================================
st.header("🩺 Watcher & Session Center Health")
health_status_badge = {
    "Online": "badge-fresh",
    "Offline": "badge-danger",
    "Unknown": "badge-unknown",
}
try:
    watcher_health, supervisor_health, session_center_health = load_infra_health()
    h1, h2, h3 = st.columns(3)
    for col, vm in zip((h1, h2, h3), (watcher_health, supervisor_health, session_center_health)):
        with col:
            badge_class = health_status_badge.get(vm.status_label, "badge-unknown")
            st.markdown(f"""
            <div class="glass-card">
                <div class="metric-label">{vm.name}</div>
                <span class="badge {badge_class}" style="font-size:0.9rem;padding:6px 14px;">{vm.status_label.upper()}</span>
                <p style="margin-top:10px;color:#8b949e;font-size:0.85rem;">{vm.detail}</p>
            </div>
            """, unsafe_allow_html=True)
except Exception as health_exc:
    st.warning(f"Could not evaluate Watcher/Session Center health: {health_exc}")

st.markdown("---")

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

recommended_card = next(
    (
        c for c in daily_brief_vm.accounts
        if c.provider == daily_brief_vm.recommended_provider and c.account_id == daily_brief_vm.recommended_account
    ),
    None,
)

truth_line_html = ""
if recommended_card is not None:
    truth_line_html = f"""
    <div style="font-size: 0.85rem; color: #8b949e; margin-bottom: 10px;">
        Primary quota: <b>{recommended_card.formatted_five_hour_remaining}</b> &nbsp;|&nbsp;
        Extra credits: <b>{recommended_card.formatted_extra_credits}</b> &nbsp;|&nbsp;
        Effective availability: <b>{recommended_card.formatted_effective_availability}</b>
    </div>
    """

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
    {truth_line_html}
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

                # Truthful availability: primary subscription quota, extra credits,
                # and the effective (actually dispatchable) availability, kept distinct
                # so "primary quota exhausted" is never displayed as "unavailable" when
                # usable extra credits exist.
                if card.extra_credits_available is not None:
                    st.write(f"• **Extra Credits**: `{card.formatted_extra_credits}`")
                st.write(f"• **Effective Availability**: `{card.formatted_effective_availability}`")

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
task_board_tasks = []
for task in all_tasks:
    board_task = dict(task)
    command = commands_by_task.get(task.get("task_id"), {})
    if (board_task.get("status") == "in_progress" and not active_executions_dict.get((task.get("project_id"), task.get("task_id")))
            and command.get("status") in {"attention", "failed", "rejected"}):
        board_task["status"] = "blocked"
    task_board_tasks.append(board_task)
board = map_task_board(task_board_tasks, active_executions_dict, now)

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
