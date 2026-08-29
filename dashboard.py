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
from manager.tasks import DriveRecords
from manager.quota_reader import read_drive_status, summarize
from manager.quota_history import get_default_quota_history_store
from manager.gcs_lock_registry import BUCKET_ENV
from manager.dispatch_requests import (
    list_recent_dispatch_request_ids,
    list_recent_dispatch_rejected_request_ids,
    resolve_dispatch_status_for_request,
    dispatch_request_registry,
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
    parse_scheduled_task_health,
    build_session_center_health,
    UNKNOWN_LABEL,
    DISPATCH_STATE_RUNNING,
    build_dispatch_truth_row,
    build_pretask_dispatch_truth_row,
    build_pretask_listing_truncated_row,
    compute_visible_dispatch_gate,
    parse_task_to_run_path,
    build_provenance_vm,
    compute_provenance_gate,
    compute_overall_visible_dispatch_gate,
    validate_provenance_evidence_document,
    reconcile_watcher_provenance_evidence,
    select_task_command,
    select_task_execution,
    select_task_handoff,
    fetch_project_records,
    fetch_task_handoffs,
    summarize_drive_read_error,
    READ_STATUS_OK,
    READ_STATUS_UNKNOWN,
)

WATCHER_TASK_NAME = "AI Development Manager - Command Watcher"
SUPERVISOR_TASK_NAME = "AI Development Manager - Session Center Supervisor"
SESSION_CENTER_URL = "http://127.0.0.1:8765"
DASHBOARD_PROJECT_ID = os.environ.get("ADM_DASHBOARD_PROJECT_ID", "ai-development-manager")
RECENT_RECORD_LIMIT = 6
# Bounded cap on how many recent dispatch-requests object names are listed
# per project for the pre-Task ("VISIBLE_BEFORE_TASK") Dashboard rows below
# -- a single bounded GCS prefix listing, never a full-history scan. See
# load_pretask_dispatch_requests()'s docstring.
PRETASK_DISPATCH_REQUEST_LIMIT = 20
# One complete Dashboard refresh per active browser session per minute: this
# matches the 60s Drive/GCS caches below, so periodic reruns cannot increase
# their upstream read cadence.
AUTO_REFRESH_INTERVAL_SECONDS = 60
_AUTO_REFRESH_ARMED_KEY = "dashboard_auto_refresh_armed"


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
def load_infra_health(active_executions=None):
    watcher_vm = parse_scheduled_task_health(WATCHER_TASK_NAME, query_scheduled_task_raw(WATCHER_TASK_NAME))
    supervisor_vm = parse_scheduled_task_health(SUPERVISOR_TASK_NAME, query_scheduled_task_raw(SUPERVISOR_TASK_NAME))
    listening, session = query_session_center_raw()
    session_center_vm = build_session_center_health(listening, session, active_executions)
    return watcher_vm, supervisor_vm, session_center_vm

# Page Configuration
st.set_page_config(
    page_title="ADM Unified Operations Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Traditional Chinese operations-console styling. The data and truth builders
# below remain unchanged; this layer only improves hierarchy and scan speed.
st.markdown("""
<style>
    :root { --adm-bg: #0b1220; --adm-panel: #111b2e; --adm-panel-2: #16243b; --adm-line: #263955; --adm-text: #e6edf7; --adm-muted: #91a4bd; --adm-blue: #62a0ff; --adm-green: #42d392; --adm-amber: #f2b84b; --adm-red: #ff6b78; }
    .stApp {
        background: var(--adm-bg);
        color: var(--adm-text);
        font-family: "Segoe UI", "Microsoft JhengHei", sans-serif;
    }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stSidebar"] { background: #0d1728; border-right: 1px solid var(--adm-line); }
    [data-testid="stSidebar"] .stMarkdown { color: var(--adm-muted); }
    h1, h2, h3 { letter-spacing: -0.025em; }
    h1 { font-size: 2.35rem !important; margin-bottom: .2rem !important; }
    h2 { margin-top: 2rem !important; }
    .block-container { max-width: 1480px; padding-top: 2.5rem; padding-bottom: 4rem; }
    .glass-card {
        background: var(--adm-panel);
        border: 1px solid var(--adm-line);
        border-radius: 10px;
        padding: 18px 20px;
        margin-bottom: 14px;
        box-shadow: 0 10px 30px rgba(0,0,0,.12);
    }
    .hero-card { background: linear-gradient(115deg, #13284a 0%, #102037 58%, #142238 100%); border: 1px solid #315580; border-radius: 12px; padding: 24px 26px; margin: 1rem 0 1.25rem; }
    .hero-kicker, .metric-label { color: var(--adm-muted); font-size: .72rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
    .hero-title { font-size: 1.8rem; font-weight: 750; margin: .45rem 0 .35rem; }
    .hero-copy { color: #b8c8dc; font-size: .98rem; }
    .hero-next { border-left: 2px solid var(--adm-blue); padding-left: 14px; color: #dbe7f7; }
    .hero-next strong { color: white; }
    .metric-value { font-size: 2rem; font-weight: 750; color: var(--adm-blue); line-height: 1.1; margin-top: .35rem; }
    .metric-card { min-height: 96px; }
    .state-running { color: var(--adm-green) !important; }
    .state-attention { color: var(--adm-red) !important; }
    .state-waiting { color: var(--adm-amber) !important; }
    .state-idle { color: var(--adm-muted) !important; }
    .recommendation-card {
        background: var(--adm-panel);
        border: 1px solid var(--adm-line);
        border-radius: 10px;
        padding: 20px 24px;
        margin-bottom: 18px;
    }
    .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        margin-right: 5px;
        margin-bottom: 4px;
    }
    .badge-ok { background-color: #245ca8; color: #ffffff; }
    .badge-running, .badge-fresh { background-color: #176b4e; color: #ffffff; }
    .badge-waiting, .badge-attention { background-color: #8a5a16; color: #ffffff; }
    .badge-danger, .badge-stale { background-color: #8d2938; color: #ffffff; }
    .badge-unknown, .badge-manual { background-color: #43536b; color: #ffffff; }
    .badge-official { background-color: #245ca8; color: #ffffff; }

    /* Action Badges */
    .badge-action-consume { background-color: #2ea043; color: #ffffff; font-weight: bold; }
    .badge-action-normal { background-color: #1f6feb; color: #ffffff; }
    .badge-action-conserve { background-color: #bb8009; color: #ffffff; font-weight: bold; }
    .badge-action-hold { background-color: #da3633; color: #ffffff; font-weight: bold; }

    /* Priorities */
    .priority-urgent { color: var(--adm-red); font-weight: bold; }
    .priority-high { color: var(--adm-amber); font-weight: bold; }
    .priority-normal { color: var(--adm-blue); }
    .priority-low { color: var(--adm-muted); }
    @media (max-width: 768px) { .block-container { padding: 1.25rem .9rem 3rem; } h1 { font-size: 1.8rem !important; } .hero-card { padding: 18px; } }
</style>
""", unsafe_allow_html=True)


def list_records_isolated(store, area, project_id, limit=RECENT_RECORD_LIMIT, include_ids=None):
    """Thin wrapper: delegates to the pure, directly-testable
    fetch_project_records() and keeps this function's prior (records,
    warnings) return shape for existing callers. See load_all_data() for
    the additional folder-level status/error this now also exposes."""
    result = fetch_project_records(store, area, project_id, limit, include_ids=include_ids)
    return result["records"], result["warnings"]


@st.cache_data(ttl=30)
def load_task_handoff_from_store(project_id: str, task_id: str):
    """Targeted, on-demand read of handoff records for one specific task.

    Avoids hydrating the historical handoff backlog during HOME first paint
    (load_all_data() intentionally leaves handoffs_dict empty for that
    reason). Only queries the project's HANDOFFS folder when a task is
    actually inspected in the Task Detail panel, so the canonical handoff /
    completion-report truth for that task is genuinely fetched instead of
    being permanently unavailable.

    Returns {"status", "records", "error"} (see fetch_task_handoffs()) --
    never a bare list, so a Drive read failure can never be silently
    displayed the same way as "confirmed no handoff for this task".
    """
    if not project_id or not task_id:
        return {"status": READ_STATUS_OK, "records": [], "error": None}
    try:
        service = build_service()
        store = DriveRecords(service)
    except Exception as exc:
        return {"status": READ_STATUS_UNKNOWN, "records": [], "error": summarize_drive_read_error(exc)}
    return fetch_task_handoffs(store, project_id, task_id)


@st.cache_data(ttl=60)
def load_pretask_dispatch_requests(project_ids):
    """Bounded pre-Task ingress-truth lookup (closes VISIBLE_BEFORE_TASK): a
    dispatch request ingress has durably ACCEPTED/REJECTED/FAILED before
    any Task/Command/Execution record was ever created for it must still be
    user-visible in this Dashboard -- previously it was completely
    invisible, since every render loop only ever iterated existing Task/
    Command/Execution records.

    For each project_id: list at most PRETASK_DISPATCH_REQUEST_LIMIT recent
    dispatch-requests object names (manager.dispatch_requests.
    list_recent_dispatch_request_ids() -- a single bounded, recency-sorted
    GCS prefix scan, never a full-history scan; see that function's own
    docstring for why a raw single-page listing is not recency-correct and
    how the `truncated` flag it also returns must be handled -- see
    `truncated` below), then resolve each request_id's canonical truth via
    resolve_dispatch_status_for_request(). Only request_ids that resolve to
    task=None (no real Task record exists for them, checked fresh on every
    call) are returned -- once a Task exists, Task/Command/Execution truth
    is the sole source of dispatch state (STATE_PROMOTION contract: this
    function must never also surface a duplicate ingress-only row for a
    request that already has a Task).

    Each project_id's value is {"rows": [...], "truncated": bool}.
    `truncated=True` means list_recent_dispatch_request_ids() could not
    prove its scan for this project was complete this refresh -- the caller
    (dashboard.py's render loop) MUST surface this as an explicit UNKNOWN/
    TRUNCATED row rather than silently treating a short/empty `rows` as a
    confirmed "no pending pre-Task requests".

    Fails soft to {} (no pre-Task rows shown this refresh, never a crash)
    when ADM_LOCK_GCS_BUCKET is not configured or GCS/Drive is unreachable
    -- matches this Dashboard's existing fail-soft pattern for every other
    optional data source (see load_infra_health, the quota/history
    try/excepts in load_all_data below). Cached at the same 60s TTL as
    load_all_data() and cleared by the same "Sync with Google Drive" button.
    """
    bucket = os.environ.get(BUCKET_ENV)
    if not bucket:
        return {}
    try:
        resolve_store = DriveRecords(build_service())
    except Exception:
        return {}
    out = {}
    for project_id in project_ids:
        try:
            listing = list_recent_dispatch_request_ids(
                bucket, project_id, max_results=PRETASK_DISPATCH_REQUEST_LIMIT,
            )
        except Exception:
            listing = {"request_ids": [], "truncated": True}
        # Also discover requests rejected BEFORE they ever reached the claim
        # registry (malformed JSON, schema-invalid payload, unverifiable
        # provenance, ...) -- these never appear under list_recent_dispatch_
        # request_ids()'s own claim-record namespace at all, so without this
        # a rejected-before-claim request stayed invisible here even though
        # resolve_dispatch_status_for_request(..., bucket=bucket) below can
        # now resolve its real REJECTED status once its request_id is known.
        try:
            rejected_listing = list_recent_dispatch_rejected_request_ids(
                bucket, project_id, max_results=PRETASK_DISPATCH_REQUEST_LIMIT,
            )
        except Exception:
            rejected_listing = {"request_ids": [], "truncated": True}
        request_ids = list(dict.fromkeys(
            list(listing.get("request_ids", [])) + list(rejected_listing.get("request_ids", []))
        ))
        truncated = bool(listing.get("truncated", False)) or bool(rejected_listing.get("truncated", False))
        rows = []
        for request_id in request_ids:
            try:
                registry = dispatch_request_registry(bucket, project_id, request_id)
                resolved = resolve_dispatch_status_for_request(
                    resolve_store, registry, project_id, request_id, bucket=bucket)
            except Exception:
                continue
            if resolved.get("task") is not None:
                continue
            rows.append({
                "request_id": request_id,
                "dispatch_request_status": resolved.get("dispatch_request_status"),
                "dispatch_request_read_failed": resolved.get("dispatch_request_read_failed", False),
            })
        out[project_id] = {"rows": rows, "truncated": truncated}
    return out


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
        # Per-(area, project_id) folder-level read status/error, so a real
        # Drive read failure for one project's Commands/Executions can be
        # told apart from a confirmed-empty folder when Task Detail later
        # asks "was this project's read even trustworthy?" (see
        # fetch_project_records()'s READ_STATUS_UNKNOWN contract).
        read_status = {}

        for project in projects:
            p_id = project.get("project_id")
            if not p_id:
                continue

            # Read Commands
            c_result = fetch_project_records(store, "commands", p_id, RECENT_RECORD_LIMIT)
            commands = c_result["records"]
            all_commands.extend(commands)
            all_warnings.extend(c_result["warnings"])
            read_status[("commands", p_id)] = (c_result["status"], c_result["error"])

            # Read Executions
            e_result = fetch_project_records(store, "executions", p_id, RECENT_RECORD_LIMIT)
            executions = e_result["records"]
            all_executions.extend(executions)
            all_warnings.extend(e_result["warnings"])
            read_status[("executions", p_id)] = (e_result["status"], e_result["error"])

            active_task_ids = {
                record.get("task_id") for record in [*commands, *executions]
                if record.get("task_id") and record.get("status") not in {"completed", "failed", "interrupted", "cancelled", "rejected"}
            }
            # Read recent Tasks plus any Task owning a current lifecycle record.
            t_result = fetch_project_records(store, "tasks", p_id, RECENT_RECORD_LIMIT, include_ids=active_task_ids)
            all_tasks.extend(t_result["records"])
            all_warnings.extend(t_result["warnings"])
            read_status[("tasks", p_id)] = (t_result["status"], t_result["error"])

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
            "read_status": read_status,
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
            "read_status": {},
            "load_duration_seconds": round(time.perf_counter() - load_started, 3),
            "warnings": all_warnings + [str(e)],
            "error": str(e)
        }

# Main App Loop
st.session_state[_AUTO_REFRESH_ARMED_KEY] = False
_periodic_fragment = getattr(st, "fragment", None) or st.experimental_fragment


@_periodic_fragment(run_every=AUTO_REFRESH_INTERVAL_SECONDS)
def _refresh_dashboard_automatically():
    """Turn a bounded fragment tick into one full Dashboard refresh.

    The initial full-script call only arms the next tick. A tick reruns the
    app, which disarms and re-arms it, preventing an immediate rerun loop.
    """
    if st.session_state.get(_AUTO_REFRESH_ARMED_KEY):
        load_all_data.clear()
        load_infra_health.clear()
        load_pretask_dispatch_requests.clear()
        st.rerun()
    st.session_state[_AUTO_REFRESH_ARMED_KEY] = True


_refresh_dashboard_automatically()

st.title("AI 開發管理器")
st.caption("營運控制台 · 即時任務、執行狀態與配額")

# Sidebar Refresh & Status
with st.sidebar:
    st.header("控制台")
    if st.button("立即同步資料", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("### 執行環境")
    st.info("唯讀監控中。資料來源為 Google Drive runtime SSOT。")

    st.markdown("---")
    st.caption("每 60 秒自動更新。手動同步會立即清除快取。")

# Load Data
data = load_all_data()

if not data["success"] and not data.get("all_tasks") and not data.get("daily_brief_vm"):
    st.error(f"無法從 Google Drive 取得資料：{data['error']}")
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
read_status = data.get("read_status", {})
all_warnings = data.get("warnings", [])
load_duration_seconds = data.get("load_duration_seconds")

now = datetime.now(timezone.utc)

# 首屏摘要：只使用已載入的真實 lifecycle records，不創造執行中的假象。
_quick_active = [e for e in all_executions if e.get("status") not in {"completed", "failed", "interrupted", "cancelled"}
                 and determine_execution_state(e, now) == "running" and not is_execution_stale(e, now)]
_quick_attention = [e for e in all_executions if e.get("status") in {"failed", "interrupted", "cancelled"}
                    or is_execution_stale(e, now)]
_quick_queued = [c for c in all_commands if c.get("status") in {"queued", "claimed", "submitted"}]
_quick_task = next((t for t in all_tasks if t.get("task_id") in {e.get("task_id") for e in _quick_active}), None)
_quick_state = "執行中" if _quick_active else ("需要處理" if _quick_attention else ("等待接單" if _quick_queued else "目前閒置"))
_quick_state_class = "state-running" if _quick_active else ("state-attention" if _quick_attention else ("state-waiting" if _quick_queued else "state-idle"))
_quick_owner = (_quick_active[0].get("provider") if _quick_active else None) or ("尚未分派" if _quick_queued else "無")
_quick_title = (_quick_task or {}).get("title") or ("已有 request，尚未建立 Task" if _quick_queued else "沒有已證實執行中的任務")
_quick_progress = (_quick_active[0].get("last_provider_event") if _quick_active else None) or ("等待 lifecycle promotion" if _quick_queued else "下一步：檢查最新任務或手動同步")

st.markdown(f"""
<section class="hero-card" aria-label="目前營運摘要">
  <div class="hero-kicker">目前營運狀態 · 最後讀取 {load_duration_seconds}s</div>
  <div class="hero-title"><span class="{_quick_state_class}">{_quick_state}</span></div>
  <div class="hero-copy"><strong>誰在做：</strong>{_quick_owner}　 <strong>Task：</strong>{_quick_title}</div>
  <div class="hero-next"><strong>目前進度 / 下一步</strong><br>{_quick_progress}</div>
</section>
""", unsafe_allow_html=True)

st.markdown("### 一眼掌握")
m0, m1, m2, m3 = st.columns(4)
for col, label, value, klass in (
    (m0, "正在執行", len(_quick_active), "state-running"),
    (m1, "需要處理", len(_quick_attention), "state-attention"),
    (m2, "等待接單", len(_quick_queued), "state-waiting"),
    (m3, "可用配額帳戶", f"{sum(1 for a in daily_brief_vm.accounts if a.has_reliable_quota)} / {len(daily_brief_vm.accounts)}", "state-running"),
):
    with col:
        st.markdown(f'<div class="glass-card metric-card"><div class="metric-label">{label}</div><div class="metric-value {klass}">{value}</div></div>', unsafe_allow_html=True)

# =====================================================================
# P0: User-visible lifecycle truth (Task + Command + Execution)
# =====================================================================
st.header("即時任務可視性")
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
        "類別": {"Active": "執行中", "Queued": "等待接單", "Attention": "需要處理", "Completed": "已完成"}[category],
        "AI": execution.get("provider") or command.get("provider") or task.get("assigned_provider") or task.get("recommended_provider") or "—",
        "任務名稱": task.get("title") or snapshot.get("title") or "未知",
        "任務狀態": task.get("status") or "未知",
        "指令狀態": command_status or "未知",
        "執行狀態": execution_status or "未知",
        "Session ID": execution.get("provider_session_id") or execution.get("session_id") or (command.get("result") or {}).get("session_id") or "未知",
        "Task ID": task_id or "未知",
        "模型 / 模式": f"{execution.get('model') or command.get('model') or snapshot.get('model') or '未知'} / {execution.get('mode') or command.get('mode') or snapshot.get('mode') or task.get('mode') or '未知'}",
        "工作目錄": task.get("working_directory") or snapshot.get("working_directory") or "未知",
        "開始時間": execution.get("started_at") or execution.get("reserved_at") or command.get("claimed_at") or "未知",
        "最後更新": execution.get("heartbeat_at") or execution.get("completed_at") or task.get("updated_at") or command.get("completed_at") or command.get("created_at") or "未知",
    })

visibility_rows.sort(key=lambda row: row["最後更新"], reverse=True)
visibility_rows.sort(key=lambda row: {"執行中": 0, "等待接單": 1, "需要處理": 2, "已完成": 3}[row["類別"]])
if visibility_rows:
    st.dataframe(pd.DataFrame(visibility_rows), use_container_width=True, hide_index=True)
else:
    st.info("目前沒有最近的 Task、Command 或 Execution 紀錄。")
st.caption(f"顯示專案 `{DASHBOARD_PROJECT_ID}` 各 lifecycle 類型最近 {RECENT_RECORD_LIMIT} 筆；每 {AUTO_REFRESH_INTERVAL_SECONDS} 秒自動更新。")

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
st.header("派工真實狀態")

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

# VISIBLE_BEFORE_TASK: a dispatch request ingress has durably ACCEPTED/
# REJECTED/FAILED before any Task/Command/Execution record exists for it
# must still be shown here -- see load_pretask_dispatch_requests()'s
# docstring for the bounded-listing + Task-truth-priority contract. Each
# entry it returns has ALREADY been confirmed (fresh, this same render) to
# have no Task record, so appending these never duplicates a row already
# built from all_tasks above.
_pretask_by_project = load_pretask_dispatch_requests(tuple(sorted(_dispatch_projects_by_id.keys())))
for _pretask_project_id, _pretask_entry in _pretask_by_project.items():
    _pretask_project = _dispatch_projects_by_id.get(_pretask_project_id)
    for _pretask in _pretask_entry["rows"]:
        _dispatch_rows.append(build_pretask_dispatch_truth_row(
            _pretask_project, _pretask_project_id, _pretask["request_id"],
            _pretask["dispatch_request_status"], daily_brief_vm.accounts, now,
            dispatch_request_read_failed=_pretask["dispatch_request_read_failed"],
        ))
    if _pretask_entry.get("truncated"):
        # Blocker 1 fix: an incomplete recent-request scan must never be
        # rendered as a silent, confirmed "nothing pending" -- see
        # load_pretask_dispatch_requests()'s docstring.
        _dispatch_rows.append(build_pretask_listing_truncated_row(
            _pretask_project, _pretask_project_id, daily_brief_vm.accounts,
        ))

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
    st.success("派工真實狀態：通過")
else:
    st.error("派工真實狀態：未通過")
    with st.expander(f"判定原因（{len(overall_gate['reasons'])}）", expanded=True):
        for _reason in overall_gate["reasons"]:
            st.write(f"- {_reason}")

with st.expander("🧬 Production Provenance（Dashboard 與 Watcher runtime identity）", expanded=(provenance_gate["result"] == "FAIL")):
    _col1, _col2 = st.columns(2)
    with _col1:
        st.markdown("**Dashboard（目前頁面）**")
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
    st.info("目前沒有可顯示的任務。")
else:
    for _row in _dispatch_rows:
        _state = _row["dispatch_state"]
        if _row.get("pretask"):
            _badge = "🕓"  # pre-Task ingress-only truth -- no Task/Command/Execution exists yet.
        else:
            _badge = "🟢" if _state == DISPATCH_STATE_RUNNING else ("🔴" if _state in ("FAILED", "BLOCKED") else "⚪")
        with st.container():
            st.markdown(f"""<div class="glass-card">
                <b>{_badge} {_row['project_name']} → {_row['task_title']}</b><br>
                <b>派工狀態：</b> <code>{_state}</code> — {_row['dispatch_reason']}<br>
                <b>AI / 帳戶：</b> {_row['provider']} / {_row['account_id']} · <b>模型 / 模式：</b> {_row['model']} / {_row['mode']}<br>
                <b>五小時剩餘：</b> {_row['quota']['formatted_five_hour_remaining']}（重置 {_row['quota']['formatted_five_hour_reset_at']}）
                · <b>每週剩餘：</b> {_row['quota']['formatted_weekly_remaining']}（重置 {_row['quota']['formatted_weekly_reset_at']}）<br>
                <b>配額擷取時間：</b> {_row['quota']['formatted_captured_at']} · <b>新鮮度：</b> {_row['quota']['freshness']}
                </div>""", unsafe_allow_html=True)
            with st.expander("技術識別碼"):
                st.write(f"project_id=`{_row['project_id']}` · task_id=`{_row['task_id']}`")
                st.write(f"execution_id=`{_row['execution_id']}` · session_id=`{_row['session_id']}`")

st.markdown("---")

# Active means provider execution is proven running now. A reserved, stale, or
# session-less Execution remains visible in lifecycle/attention views but must
# not inflate Running Tasks or Active Sessions.
execution_candidates = [
    e for e in all_executions
    if e.get("status") not in {"completed", "failed", "interrupted", "cancelled"}
]
active_executions = [
    e for e in execution_candidates
    if determine_execution_state(e, now) == "running" and not is_execution_stale(e, now)
]
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
        <div class="metric-label">正在執行</div>
        <div class="metric-value">{summary_metrics['running_tasks_count']}</div>
    </div>
    """, unsafe_allow_html=True)
with m2:
    st.markdown(f"""
    <div class="glass-card">
        <div class="metric-label">需要處理</div>
        <div class="metric-value">{summary_metrics['blocked_tasks_count']}</div>
    </div>
    """, unsafe_allow_html=True)
with m3:
    st.markdown(f"""
    <div class="glass-card">
        <div class="metric-label">活躍 Session</div>
        <div class="metric-value">{summary_metrics['active_sessions_count']}</div>
    </div>
    """, unsafe_allow_html=True)
with m4:
    st.markdown(f"""
    <div class="glass-card">
        <div class="metric-label">可靠配額</div>
        <div class="metric-value">{reliable_count} / {total_accounts_count}</div>
    </div>
    """, unsafe_allow_html=True)

# =====================================================================
# Section: Watcher & Session Center Health
# =====================================================================
st.header("系統健康狀態")
health_status_badge = {
    "Online": "badge-fresh",
    "Offline": "badge-danger",
    "Unknown": "badge-unknown",
}
try:
    execution_read_status = read_status.get(
        ("executions", DASHBOARD_PROJECT_ID), (READ_STATUS_UNKNOWN, None)
    )[0]
    session_truth = active_executions if execution_read_status == READ_STATUS_OK else None
    watcher_health, supervisor_health, session_center_health = load_infra_health(session_truth)
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
    st.warning(f"無法評估 Watcher 或 Session Center 健康狀態：{health_exc}")

st.markdown("---")

# =====================================================================
# Section A: Today's AI Recommendation (Daily Brief)
# =====================================================================
st.header("今日派工建議")

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
        主要配額：<b>{recommended_card.formatted_five_hour_remaining}</b> &nbsp;|&nbsp;
        額外額度：<b>{recommended_card.formatted_extra_credits}</b> &nbsp;|&nbsp;
        實際可用：<b>{recommended_card.formatted_effective_availability}</b>
    </div>
    """

st.markdown(f"""
<div class="recommendation-card">
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <span style="font-size: 1.25rem; font-weight: 700; color: #ffffff;">
            建議使用：<span style="color: #58a6ff;">{daily_brief_vm.recommended_display_name}</span>
        </span>
        <span class="badge {action_badge_class}" style="font-size: 0.85rem; padding: 6px 14px;">
            ACTION: {action_label}
        </span>
    </div>
    <div style="font-size: 0.95rem; line-height: 1.5; color: #e6edf3; margin-bottom: 10px;">
        <b>原因：</b> {daily_brief_vm.reason}
    </div>
    {truth_line_html}
    <div style="font-size: 0.85rem; color: #8b949e;">
        <b>最近重置倒數：</b> {daily_brief_vm.nearest_reset_countdown} &nbsp;|&nbsp;
        產生時間：<code>{daily_brief_vm.generated_at}</code>
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
st.header("Provider 與帳戶配額")
accounts_list = daily_brief_vm.accounts

if not accounts_list:
    st.info("配額資料中目前沒有已設定的 AI provider。")
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
                    freshness_text = "已過期"
                else:
                    fresh_class = "badge-fresh"
                    freshness_text = "新鮮"

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

                action_text = {
                    "urgent_consume": "立即消耗",
                    "suggest_consume": "建議消耗",
                    "normal_use": "正常使用",
                    "conserve": "節省使用",
                    "hold": "暫緩派工",
                }.get(card.action_recommendation.lower(), "未知")

                st.markdown(f"""
                <div>
                    <span class="badge {status_class}">狀態：{card.status.upper()}</span>
                    <span class="badge {fresh_class}">{freshness_text}</span>
                    <span class="badge {action_bg}">{action_text}</span>
                </div>
                """, unsafe_allow_html=True)

                # 5-Hour Quota Display
                st.markdown("#### 五小時配額窗")
                if card.stale:
                    st.warning(f"配額資料已過期（剩餘：{card.formatted_five_hour_remaining}）")
                elif card.five_hour_remaining_pct is not None:
                    try:
                        rem_val = float(card.five_hour_remaining_pct)
                        st.progress(rem_val / 100.0)
                        used_text = f"{card.five_hour_used_pct:.1f}%" if card.five_hour_used_pct is not None else "—"
                        st.write(f"剩餘：**{card.formatted_five_hour_remaining}**（已使用：{used_text}）")
                    except (ValueError, TypeError):
                        st.write("剩餘：**未知**")
                else:
                    used_text = f"{card.five_hour_used_pct:.1f}%" if card.five_hour_used_pct is not None else "—"
                    st.info(f"尚未回報百分比（已使用：{used_text}）")

                # 5-Hour Forecast Details
                st.write(f"• **重置倒數**：`{card.formatted_five_hour_countdown}`")
                st.write(f"• **消耗速度**：`{card.formatted_five_hour_burn_rate}`")
                st.write(f"• **重置時預估**：`{card.formatted_five_hour_projected}`")

                # Weekly Quota Display (if exists)
                if card.has_weekly_window:
                    st.markdown("#### 每週配額窗")
                    w_used_text = f"{card.weekly_used_pct:.1f}%" if card.weekly_used_pct is not None else "—"
                    st.write(f"• **剩餘**：**{card.formatted_weekly_remaining}**（已使用：{w_used_text}）")
                    st.write(f"• **重置**：`{card.formatted_weekly_countdown}`")
                    st.caption(f"每週重置時間：`{card.weekly_resets_at or '未知'}`")
                    if card.weekly_action_recommendation in ("conserve", "hold"):
                        weekly_status = "節省使用" if card.weekly_action_recommendation == "conserve" else "暫緩派工"
                        st.caption(f"⚠️ 每週狀態：{weekly_status}")

                # Truthful availability: primary subscription quota, extra credits,
                # and the effective (actually dispatchable) availability, kept distinct
                # so "primary quota exhausted" is never displayed as "unavailable" when
                # usable extra credits exist.
                if card.extra_credits_available is not None:
                    st.write(f"• **額外額度**：`{card.formatted_extra_credits}`")
                st.write(f"• **實際可用性**：`{card.formatted_effective_availability}`")

                # Metadata / Telemetry
                st.caption(f"來源：`{card.source}`（{card.source_type}）｜可信度：`{card.confidence}`")
                st.caption(f"最後更新：{card.last_updated or '尚未更新'}")
                st.caption(f"重置時間：`{card.five_hour_resets_at or '未知'}`")

                if card.warning_reason:
                    st.caption(f"ℹ️ *{card.warning_reason}*")

                st.markdown("---")
            except Exception as e:
                st.error(f"帳戶 {card.card_title} 顯示失敗：{e}")

st.markdown("---")

# =====================================================================
# Section C: Running & Active Executions Table
# =====================================================================
st.header("執行中的任務")
if not execution_candidates:
    st.info("目前沒有已證實正在執行的 AI 任務。")
else:
    exec_rows = []
    for exe in execution_candidates:
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
            elapsed_str = f"{elapsed_m:.1f} 分鐘"

        expected_str = f"{task_snapshot.get('expected_minutes', '—')} 分鐘"

        is_stale = is_execution_stale(exe, now)
        attention = "⚠️ 需要處理" if is_stale else "✅ 正常"

        exec_rows.append({
            "專案": p_id,
            "任務": t_id,
            "AI 提供者": provider,
            "帳戶": account,
            "模型 / 模式 / 努力程度": f"{model} / {mode} / {effort}",
            "Provider 工作階段": session_id,
            "狀態": ui_state.upper(),
            "目前進度": progress,
            "心跳": hb_at,
            "已耗時": elapsed_str,
            "預估耗時": expected_str,
            "健康度": attention
        })

    st.table(pd.DataFrame(exec_rows))

st.markdown("---")

# =====================================================================
# Section D: Task Board (Tabs for columns)
# =====================================================================
st.header("任務佇列")
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
    f"執行中 ({len(board['In progress'])})",
    f"待接單 ({len(board['Ready'])})",
    f"需要處理 ({len(board['Blocked / Attention'])})",
    f"已完成 ({len(board['Completed'])})"
])


def render_task_cards(tasks):
    if not tasks:
        st.write("這個分類目前沒有任務。")
        return

    for t in tasks:
        project_id = t.get("project_id", "—")
        task_id = t.get("task_id", "—")
        title = t.get("title", "—")
        priority = t.get("priority", "normal")
        provider = t.get("assigned_provider") or t.get("recommended_provider") or "尚未分派"
        progress = t.get("current_progress", "—")
        next_action = t.get("next_action", "—")

        st.markdown(f"""
        <div class="glass-card">
            <h4>{title} <small style="color:#8b949e;">({task_id} in {project_id})</small></h4>
            <p>
                優先級：<span class="priority-{priority}">{priority.upper()}</span>｜
                AI：<b>{provider}</b>
            </p>
            <p>進度：<i>{progress}</i></p>
            <p><b>下一步：</b> {next_action}</p>
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
st.header("Task 詳情與交接")
all_task_ids = [(t.get("project_id"), t.get("task_id"), t.get("title")) for t in all_tasks]

if not all_task_ids:
    st.info("目前沒有可檢視的任務。")
else:
    selected_option = st.selectbox(
        "選擇要檢視的 Task",
        options=all_task_ids,
        format_func=lambda x: f"[{x[0]}] {x[1]} - {x[2]}"
    )

    if selected_option:
        p_id, t_id, _ = selected_option

        # Find the task, and the exact task-scoped command/execution/handoff.
        # Deliberately NOT active_executions_dict / handoffs_dict here:
        # active_executions_dict only covers executions proven running (a
        # terminal-but-still-relevant execution silently reads as "no active
        # execution"), and handoffs_dict is always empty (load_all_data()
        # intentionally defers historical handoff hydration from first
        # paint) -- both previously made a genuinely running/completed task
        # show as "cannot confirm" even though its Drive records exist.
        task = next((t for t in all_tasks if t.get("project_id") == p_id and t.get("task_id") == t_id), {})
        cmd = select_task_command(all_commands, p_id, t_id)
        exe = select_task_execution(all_executions, p_id, t_id, command=cmd)
        handoff_result = load_task_handoff_from_store(p_id, t_id)
        ho = select_task_handoff(handoff_result["records"], p_id, t_id, execution=exe, command=cmd)

        # Folder-level read truth for this project's Commands/Executions --
        # distinguishes "no command/execution record" (confirmed) from "we
        # could not trust this project's Commands/Executions read" (Drive
        # auth/network/timeout/5xx failure). Never collapse the latter into
        # the former.
        cmd_status, cmd_read_error = read_status.get(("commands", p_id), (READ_STATUS_OK, None))
        exe_status, exe_read_error = read_status.get(("executions", p_id), (READ_STATUS_OK, None))

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Task 詳情")
            st.write(f"**狀態**：`{task.get('status')}`")
            st.write(f"**建議 Provider**：`{task.get('recommended_provider')}`")
            st.write(f"**指派 Provider**：`{task.get('assigned_provider')}`")
            st.write(f"**下一步**：{task.get('next_action')}")
            st.write(f"**工作目錄**：`{task.get('working_directory')}`")
            st.write(f"**分支**：`{task.get('branch')}`")

            # Check for the exact task-scoped execution (any status, not just
            # "active") -- this is the record select_task_execution proved
            # belongs to this (project_id, task_id), never borrowed.
            if exe:
                exec_state = determine_execution_state(exe, now)
                st.info(f"已找到 Execution（生命週期狀態：{exec_state.upper()}）。")
                st.write(f"Execution ID：`{exe.get('execution_id') or '未知'}`")
                st.write(f"Execution 狀態：`{exe.get('status') or '未知'}`")
                st.write(f"Provider Session ID：`{exe.get('provider_session_id') or exe.get('session_id') or '未知'}`")
                st.write(f"心跳：`{exe.get('heartbeat_at') or exe.get('completed_at') or '未知'}`")
            elif cmd:
                st.write(f"找不到此 Task 指令對應的 Execution（狀態：`{cmd.get('status') or '未知'}`）。")
            elif cmd_status == READ_STATUS_UNKNOWN or exe_status == READ_STATUS_UNKNOWN:
                st.warning(
                    f"未知：無法確認此專案的 Command / Execution 真實狀態 "
                    f"（讀取失敗：`{cmd_read_error or exe_read_error}`）。不是確認不存在；請重新同步。"
                )
            else:
                st.write("此 Task 沒有 Command 或 Execution 紀錄。")

        with col2:
            st.subheader("最新交接")
            if ho:
                st.write(f"Handoff ID：`{ho.get('handoff_id')}`")
                st.write(f"建立時間：`{ho.get('created_at')}`")
                st.write(f"原因：`{ho.get('reason')}`")
                st.write(f"下一步：{ho.get('next_action')}")

                # Expand completed work & changes
                with st.expander("已完成工作"):
                    st.write(ho.get("completed_work", []))

                files_changed = ho.get("files_changed", [])
                if files_changed:
                    with st.expander("已修改檔案"):
                        st.write(files_changed)

                commits = ho.get("commits", [])
                if commits:
                    with st.expander("提交紀錄"):
                        st.write(commits)
            elif handoff_result["status"] == READ_STATUS_UNKNOWN:
                st.warning(
                    f"未知：無法確認此 Task 的 Handoff 真實狀態 "
                    f"（讀取失敗：`{handoff_result['error']}`）。不是確認不存在；請重新同步。"
                )
            else:
                st.write("此 Task 沒有 Handoff 紀錄。")

if all_warnings:
    st.markdown("---")
    with st.expander(f"⚠️ 部分資料警告（{len(all_warnings)}）"):
        for w in all_warnings:
            st.warning(w)
