import streamlit as st
import os
import json
import html
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords
from manager.overview import read_overview
from manager.quota_reader import read_drive_status, read_local_status, summarize
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
    TERMINAL_EXECUTION_STATUSES,
    determine_execution_state,
    is_execution_stale,
    get_global_summary,
    map_task_board,
    build_daily_brief_vm,
    is_live_execution,
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
DASHBOARD_DRIVE_REQUEST_TIMEOUT_SECONDS = 8
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
    page_title="AI 開發管理器｜工作台",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Traditional Chinese operations-console styling. The data and truth builders
# below remain unchanged; this layer only decides hierarchy, density and how
# fast a state can be read. Three primitives carry the whole live UI: the
# status band (what is happening now), the row (one real record), and the
# chip (one real state, always spelled out in words -- colour reinforces the
# word, it is never the only signal).
st.markdown("""
<style>
    /* Tokens. One cool-green neutral family, one accent, one shadow.
       Text tiers are contrast-checked on --adm-panel: text 13.4:1,
       text-2 7.4:1, muted 5.3:1 -- all above the 4.5:1 body floor. */
    :root {
        --adm-bg: #f1f4f2; --adm-panel: #ffffff; --adm-panel-2: #e8eeea;
        --adm-line: #d3ddd7; --adm-line-soft: #e4eae6;
        --adm-text: #16231d; --adm-text-2: #44544b; --adm-muted: #5d6d64;
        --adm-accent: #1f6b53; --adm-accent-soft: #e6f0eb;
        --adm-green: #1f6b53; --adm-amber: #8a5a12; --adm-red: #a32c2c;
        --adm-shadow: 0 1px 2px rgba(22,44,34,.05), 0 6px 16px rgba(22,44,34,.045);
        --adm-r: 8px; --adm-r-sm: 5px;
        /* Legacy alias so the disabled pre-st.stop() block still resolves. */
        --adm-blue: #1f6b53;
    }
    .stApp {
        background: var(--adm-bg);
        color: var(--adm-text);
        font-family: "Aptos", "Segoe UI", "Microsoft JhengHei", sans-serif;
        font-variant-numeric: tabular-nums;
    }
    /* Browser surfaces belong to the design too. */
    ::selection { background: #cfe4d9; color: var(--adm-text); }
    *:focus-visible { outline: 2px solid var(--adm-accent); outline-offset: 2px; border-radius: 3px; }
    ::-webkit-scrollbar { width: 11px; height: 11px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #c4cfc8; border-radius: 999px; border: 3px solid var(--adm-bg); }
    ::-webkit-scrollbar-thumb:hover { background: #aab8b0; }

    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stSidebar"] { background: var(--adm-panel-2); border-right: 1px solid var(--adm-line); }
    [data-testid="stSidebar"] .stMarkdown { color: var(--adm-muted); }
    [data-testid="stSidebar"] h2 { border: 0 !important; padding: 0 !important; margin: 0 0 .6rem !important; font-size: .95rem !important; }
    .block-container { max-width: 1400px; padding: 1.15rem 2rem 3rem; }

    /* Fixed rem scale, ~1.2 ratio: one voice, obvious steps, no fluid type. */
    h1 { font-size: 1.5rem !important; font-weight: 700; letter-spacing: -.02em; margin: 0 0 .15rem !important; }
    h2, h3 {
        font-size: 1rem !important; font-weight: 700; letter-spacing: -.005em;
        margin: 1.7rem 0 .5rem !important; padding-bottom: .35rem;
        border-bottom: 1px solid var(--adm-line-soft);
    }
    [data-testid="stCaptionContainer"] p { color: var(--adm-muted); font-size: .8125rem; line-height: 1.6; }
    [data-testid="stAlert"] { border-radius: var(--adm-r-sm); font-size: .85rem; }
    [data-testid="stDataFrame"], [data-testid="stTable"] { font-variant-numeric: tabular-nums; }
    [data-testid="stExpander"] details { border-radius: var(--adm-r-sm); border-color: var(--adm-line); }
    .stButton > button {
        border-radius: var(--adm-r-sm); border: 1px solid var(--adm-line);
        background: var(--adm-panel); color: var(--adm-text);
        font-weight: 650; font-size: .85rem;
        transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
    }
    .stButton > button:hover { background: var(--adm-accent-soft); border-color: #b8d1c4; color: var(--adm-accent); }
    .stButton > button:active { transform: translateY(1px); }

    /* Status band: the one thing read first -- state, the three counts that
       decide whether to act, and the next automatic action. Same surface as
       every other panel; no dark slab dropped into a light page. */
    .adm-band {
        background: var(--adm-panel); border: 1px solid var(--adm-line);
        border-radius: var(--adm-r); padding: 14px 16px; margin: .5rem 0 1.15rem;
        box-shadow: var(--adm-shadow);
    }
    .adm-band-grid { display: grid; grid-template-columns: minmax(0, 1.9fr) repeat(3, minmax(94px, .6fr)); gap: 12px 20px; align-items: start; }
    .adm-band-state { font-size: 1.3rem; font-weight: 750; letter-spacing: -.02em; line-height: 1.25; }
    .adm-band-detail { color: var(--adm-text-2); font-size: .845rem; line-height: 1.55; margin-top: .3rem; }
    .adm-stat { border-left: 1px solid var(--adm-line-soft); padding-left: 15px; }
    .adm-stat-k { color: var(--adm-muted); font-size: .75rem; line-height: 1.35; }
    .adm-stat-v { font-size: 1.26rem; font-weight: 700; letter-spacing: -.02em; margin-top: .1rem; }
    .adm-band-next {
        margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--adm-line-soft);
        display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 12px;
        align-items: baseline; font-size: .855rem; color: var(--adm-text-2);
    }
    .adm-band-next b { color: var(--adm-muted); font-weight: 650; font-size: .75rem; white-space: nowrap; }

    /* State words. */
    .state-running { color: var(--adm-green) !important; }
    .state-attention { color: var(--adm-red) !important; }
    .state-waiting { color: var(--adm-amber) !important; }
    .state-idle { color: var(--adm-text-2) !important; }

    .chip {
        display: inline-block; padding: 2px 9px; border-radius: 999px;
        font-size: .735rem; font-weight: 700; line-height: 1.55;
        white-space: nowrap; border: 1px solid transparent;
    }
    .chip-running { background: #e0efe7; color: #14523d; border-color: #bedbce; }
    .chip-attention { background: #f8e1e1; color: #7d2020; border-color: #e7c0c0; }
    .chip-waiting { background: #f8ecd6; color: #6f4509; border-color: #e6d3ae; }
    .chip-done { background: #e8ece9; color: #3d4d44; border-color: #d3ddd7; }
    .chip-idle { background: #eef2f0; color: #52625a; border-color: #dde5e0; }

    /* Row: one real record. The chip rail, the title baseline and the
       right-hand time line up down the list, so the eye scans one column at
       a time instead of re-reading every card. */
    .adm-list { display: flex; flex-direction: column; gap: 6px; }
    .adm-row {
        display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 12px;
        align-items: start; background: var(--adm-panel);
        border: 1px solid var(--adm-line); border-radius: var(--adm-r-sm);
        padding: 9px 12px;
    }
    .adm-row > .chip { min-width: 4.6rem; text-align: center; margin-top: 1px; }
    .adm-row-title { display: flex; justify-content: space-between; gap: 14px; align-items: baseline; font-size: .9rem; font-weight: 650; overflow-wrap: anywhere; }
    .adm-row-right { color: var(--adm-muted); font-size: .775rem; font-weight: 500; white-space: nowrap; flex: 0 0 auto; }
    .adm-row-meta { color: var(--adm-muted); font-size: .8rem; line-height: 1.55; margin-top: 3px; overflow-wrap: anywhere; }
    .adm-row-meta b { color: var(--adm-text-2); font-weight: 650; }
    .adm-row-next { margin-top: 4px; font-size: .8rem; line-height: 1.55; color: var(--adm-text-2); overflow-wrap: anywhere; }
    .adm-row-next b { color: var(--adm-muted); font-weight: 650; }

    .adm-hgrid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .adm-hcard { background: var(--adm-panel); border: 1px solid var(--adm-line); border-radius: var(--adm-r-sm); padding: 11px 13px; }
    .adm-hcard-name { color: var(--adm-muted); font-size: .78rem; }
    .adm-hcard-state { font-size: 1.02rem; font-weight: 700; margin: .22rem 0 .3rem; }
    .adm-hcard-detail { color: var(--adm-text-2); font-size: .8rem; line-height: 1.5; overflow-wrap: anywhere; }

    /* Quota entity cards: one identical card per real entity (Codex / Claude A / Claude B).
       Two window rows (5H, 每週), a freshness state that is text first and color second,
       and the last capture time -- warnings live inside the card, never as page banners.
       The markup is contract-locked by manager/test_dashboard_app.py; only the
       surface treatment below is realigned with the row/band system. */
    .qe-card { background: var(--adm-panel); border: 1px solid var(--adm-line); border-radius: var(--adm-r); padding: 13px 15px 11px; min-height: 172px; display: flex; flex-direction: column; gap: 8px; box-shadow: var(--adm-shadow); }
    .qe-card.qe-stale, .qe-card.qe-error { border-color: #d9b9a0; background: #fffaf5; }
    .qe-card.qe-exhausted { border-color: #e2b8b8; }
    .qe-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
    .qe-title { font-size: 1rem; font-weight: 750; letter-spacing: -.01em; }
    .qe-state { font-size: .735rem; font-weight: 700; padding: 2px 9px; border-radius: 999px; white-space: nowrap; border: 1px solid transparent; }
    .qe-state-fresh { background: #e0efe7; color: #14523d; border-color: #bedbce; }
    .qe-state-stale { background: #f8ecd6; color: #6f4509; border-color: #e6d3ae; }
    .qe-state-unknown { background: #eef2f0; color: #52625a; border-color: #dde5e0; }
    .qe-state-error, .qe-state-exhausted { background: #f8e1e1; color: #7d2020; border-color: #e7c0c0; }
    .qw-row { display: grid; grid-template-columns: 2.6rem 3.2rem 1fr; gap: 7px 10px; align-items: center; font-size: .84rem; }
    .qw-label { color: var(--adm-muted); font-weight: 650; }
    .qw-value { font-weight: 750; font-variant-numeric: tabular-nums; }
    .qw-bar { display: block; height: 6px; border-radius: 999px; background: #e3eae6; overflow: hidden; }
    .qw-bar i { display: block; height: 100%; border-radius: 999px; background: var(--adm-accent); }
    .qe-stale .qw-bar i, .qe-unknown .qw-bar i { background: #bcc7c0; }
    .qe-exhausted .qw-bar i { background: var(--adm-red); }
    .qw-reset { grid-column: 2 / span 2; color: var(--adm-muted); font-size: .775rem; font-variant-numeric: tabular-nums; }
    .qw-missing { color: var(--adm-muted); font-size: .79rem; }
    .qe-foot { margin-top: auto; color: var(--adm-muted); font-size: .755rem; font-variant-numeric: tabular-nums; }
    .qe-note { font-size: .8rem; line-height: 1.5; color: #6f4509; background: #fbf2e4; border-radius: var(--adm-r-sm); padding: 6px 9px; }
    .qe-note.qe-note-error { color: #7d2020; background: #f9e8e8; }
    .qe-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }

    @media (max-width: 1180px) { .adm-band-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } .adm-band-grid > :first-child { grid-column: 1 / -1; } .adm-stat:first-of-type { border-left: 0; padding-left: 0; } }
    @media (max-width: 1100px) { .qe-grid, .adm-hgrid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 768px) { .block-container { padding: 1rem .9rem 3rem; } .adm-row { grid-template-columns: 1fr; } .adm-row > .chip { justify-self: start; } }
    @media (max-width: 720px) { .qe-grid, .adm-hgrid, .adm-band-grid { grid-template-columns: 1fr; } .adm-stat { border-left: 0; padding-left: 0; } }

    /* ---- Legacy styles (disabled pre-st.stop() v2 block only) ----------
       Nothing above this line uses them. They are kept only so the legacy
       block at the bottom of this file still renders if it is ever
       re-enabled; delete them together with that block. */
    .glass-card { background: var(--adm-panel); border: 1px solid var(--adm-line); border-radius: var(--adm-r); padding: 14px 16px; margin-bottom: 10px; }
    .hero-card { background: var(--adm-panel); border: 1px solid var(--adm-line); border-radius: var(--adm-r); padding: 16px 18px; margin: .75rem 0 1rem; }
    .hero-kicker, .metric-label { color: var(--adm-muted); font-size: .75rem; font-weight: 650; }
    .hero-title { font-size: 1.05rem; font-weight: 700; margin: .2rem 0 .3rem; }
    .hero-copy { color: var(--adm-text-2); font-size: .855rem; line-height: 1.55; }
    .hero-next { color: var(--adm-text-2); font-size: .8rem; margin-top: .35rem; }
    .hero-next strong { color: var(--adm-muted); font-weight: 650; }
    .metric-value { font-size: 1.26rem; font-weight: 700; color: var(--adm-accent); }
    .metric-card, .active-card, .recommendation-card, .quota-card, .quota-compact-card { background: var(--adm-panel); border: 1px solid var(--adm-line); border-radius: var(--adm-r-sm); padding: 12px 14px; }
    .active-meta, .quota-meta, .quiet-note { color: var(--adm-muted); font-size: .8rem; line-height: 1.55; }
    .workbench-section { margin: 1.5rem 0 .75rem; }
    .section-kicker { color: var(--adm-muted); font-size: .75rem; font-weight: 650; }
    .section-title { font-size: 1rem; font-weight: 700; margin: .2rem 0 .6rem; }
    .badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: .735rem; font-weight: 700; margin-right: 5px; }
    .badge-ok, .badge-official { background: #e0e9f4; color: #1c4478; }
    .badge-running, .badge-fresh { background: #e0efe7; color: #14523d; }
    .badge-waiting, .badge-attention { background: #f8ecd6; color: #6f4509; }
    .badge-danger, .badge-stale { background: #f8e1e1; color: #7d2020; }
    .badge-unknown, .badge-manual { background: #eef2f0; color: #52625a; }
    .badge-action-consume { background: #e0efe7; color: #14523d; }
    .badge-action-normal { background: #e0e9f4; color: #1c4478; }
    .badge-action-conserve { background: #f8ecd6; color: #6f4509; }
    .badge-action-hold { background: #f8e1e1; color: #7d2020; }
    .priority-urgent { color: var(--adm-red); font-weight: 700; }
    .priority-high { color: var(--adm-amber); font-weight: 700; }
    .priority-normal { color: var(--adm-accent); }
    .priority-low { color: var(--adm-muted); }
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
        service = build_service(timeout=DASHBOARD_DRIVE_REQUEST_TIMEOUT_SECONDS)
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
def load_all_data(include_all_projects=False):
    # This result intentionally contains the live dashboard view-model graph.
    # Streamlit's pickle-backed cache rejects some production Drive/runtime
    # values even though the graph is valid for this render. Keep the live
    # read fail-soft and let the smaller, JSON-shaped loaders below retain
    # their bounded caches.
    load_started = time.perf_counter()
    now = datetime.now(timezone.utc)
    all_warnings = []
    try:
        # Dashboard reads are independently degradable and must never inherit
        # the 45s write transport timeout used by lifecycle writers.
        service = build_service(timeout=DASHBOARD_DRIVE_REQUEST_TIMEOUT_SECONDS)
        store = DriveRecords(service)

        # Load Quota Document
        quota_doc = None
        quota_summary = None
        try:
            quota_doc = read_drive_status(service=service)
            quota_summary = summarize(quota_doc, max_age_minutes=60, now=now)
        except Exception as q_exc:
            all_warnings.append(f"Drive quota status read warning: {q_exc}")
            try:
                quota_doc = read_local_status()
                quota_summary = summarize(quota_doc, max_age_minutes=60, now=now)
                all_warnings.append("Drive quota status unavailable; using validated local runtime status.")
            except Exception as local_exc:
                all_warnings.append(f"Local runtime quota status read warning: {local_exc}")
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
            if include_all_projects:
                projects = store.list_projects()
            else:
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
        all_handoffs = []
        all_sessions = []
        overview = None
        overview_error = None
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

            if include_all_projects:
                for area, target in (("handoffs", all_handoffs), ("sessions", all_sessions)):
                    result = fetch_project_records(store, area, p_id, RECENT_RECORD_LIMIT)
                    target.extend(result["records"])
                    all_warnings.extend(result["warnings"])
                    read_status[(area, p_id)] = (result["status"], result["error"])

            # Historical handoff/session detail is intentionally deferred from
            # the P0 first paint. Session identity is authoritative on Execution.

        try:
            overview = read_overview(store, DASHBOARD_PROJECT_ID)
        except Exception as exc:
            overview_error = summarize_drive_read_error(exc)

        return {
            "success": True,
            "quota_summary": quota_summary,
            "daily_brief_vm": daily_brief_vm,
            "projects": projects,
            "all_tasks": all_tasks,
            "all_commands": all_commands,
            "all_executions": all_executions,
            "all_handoffs": all_handoffs,
            "all_sessions": all_sessions,
            "overview": overview,
            "overview_error": overview_error,
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
            "all_handoffs": [],
            "all_sessions": [],
            "overview": None,
            "overview_error": None,
            "handoffs_dict": {},
            "sessions_dict": {},
            "read_status": {},
            "load_duration_seconds": round(time.perf_counter() - load_started, 3),
            "warnings": all_warnings + [str(e)],
            "error": str(e)
        }

# UI v3: the home screen is a small operational brief. Detail data stays on
# explicit secondary routes so the first paint answers the operator's first
# questions without turning the dashboard into a history table.
# zh-TW information architecture: 總覽 (now) / 專案 / 任務 / 執行 (the real
# Task -> Command -> Execution -> Session chain) / 配額 / 想法 (pre-admission
# ideas, never tasks) / 系統健康 (watcher, scheduled tasks, runtime SHA,
# provenance, recent records). Query keys stay English and stable for links.
NAV_OVERVIEW = "總覽"
NAV_PROJECTS = "專案"
NAV_TASKS = "任務"
NAV_EXECUTIONS = "執行"
NAV_QUOTA = "配額"
NAV_IDEAS = "想法"
NAV_HEALTH = "系統健康"
NAV_OPTIONS = [NAV_OVERVIEW, NAV_PROJECTS, NAV_TASKS, NAV_EXECUTIONS, NAV_QUOTA, NAV_IDEAS, NAV_HEALTH]
NAV_QUERY_VALUES = {
    NAV_OVERVIEW: "overview",
    NAV_PROJECTS: "projects",
    NAV_TASKS: "tasks",
    NAV_EXECUTIONS: "executions",
    NAV_QUOTA: "quota",
    NAV_IDEAS: "ideas",
    NAV_HEALTH: "health",
}
# Older bookmarks keep working: sessions -> 執行, history/logs -> 系統健康.
NAV_QUERY_ALIASES = {"sessions": "executions", "history": "health", "logs": "health"}


def _ui_text(value, fallback="未知"):
    if value is None or value == "":
        return fallback
    return html.escape(str(value))


def _ui_state(value):
    labels = {
        "running": "執行中", "in_progress": "執行中", "reserved": "已保留",
        "queued": "排隊中", "claimed": "已接單", "submitted": "已送出",
        "ready": "待執行", "waiting": "等待中", "blocked": "已阻塞",
        "failed": "失敗", "interrupted": "已中斷", "cancelled": "已取消",
        "completed": "已完成", "finishing": "收尾中", "correlating": "關聯中",
    }
    return labels.get(str(value or "").casefold(), _ui_text(value))


def _ui_time(value):
    parsed = parse_time(value)
    return parsed.astimezone().strftime("%m/%d %H:%M") if parsed else "未知"


def _ui_record_time(record):
    return (record.get("updated_at") or record.get("heartbeat_at") or record.get("completed_at")
            or record.get("started_at") or record.get("created_at") or record.get("reserved_at") or "")


def _ui_active_executions(executions, now):
    # 執行中 only with canonical health AND real process evidence -- see
    # manager.dashboard_core.is_live_execution.
    return [execution for execution in executions if is_live_execution(execution, now)]


def _ui_task_for_execution(execution, tasks):
    return next((task for task in tasks if task.get("project_id") == execution.get("project_id")
                  and task.get("task_id") == execution.get("task_id")), {})


def _ui_entity_title(card):
    """Codex / Claude A / Claude B -- never a nameless provider aggregate."""
    provider = str(card.provider or "").casefold()
    base = "Codex" if provider == "codex" else "Claude" if provider == "claude" else _ui_text(card.display_name)
    account = card.account_id
    if not account:
        return base
    suffix = str(account)
    if suffix.casefold().startswith("account-"):
        suffix = suffix[len("account-"):]
    return f"{base} {suffix.upper() if len(suffix) <= 2 else _ui_text(suffix)}"


def _ui_quota_state(card):
    """(code, label). Text first, colour second: STALE != 0, UNKNOWN != 0,
    STALE != EXHAUSTED -- each is its own word."""
    if card.stale:
        return "stale", "過期"
    status = str(card.status or "unknown").casefold()
    if status == "error":
        return "error", "來源錯誤"
    if status == "exhausted":
        return "exhausted", "已用盡"
    # Schema: status is authoritative for manual providers (abundant / normal
    # / low / exhausted / unknown) and "ok" for automatic ones. Anything
    # else -- including "unknown" with a retained numeric window -- is
    # UNKNOWN, never 最新 (bounded review finding).
    if status not in ("ok", "abundant", "normal", "low") or card.five_hour_remaining_pct is None:
        return "unknown", "未知"
    five = float(card.five_hour_remaining_pct)
    weekly = float(card.weekly_remaining_pct) if card.has_weekly_window and card.weekly_remaining_pct is not None else None
    if five <= 0 or (weekly is not None and weekly <= 0):
        if card.effective_availability == "available_via_credits":
            return "fresh", "額度用盡・可用點數"
        return "exhausted", "已用盡"
    return "fresh", "最新"


def _ui_quota_status(card):
    """Legacy uppercase code kept for existing callers/tests."""
    code, _label = _ui_quota_state(card)
    return {"fresh": "OK", "stale": "STALE", "error": "ERROR", "unknown": "UNKNOWN", "exhausted": "EXHAUSTED"}[code]


def _ui_hours_text(hours):
    if hours is None:
        return None
    total_minutes = max(0, int(round(float(hours) * 60)))
    days, rem = divmod(total_minutes, 24 * 60)
    hrs, mins = divmod(rem, 60)
    if days:
        return f"{days} 天 {hrs} 小時後"
    if hrs:
        return f"{hrs} 小時 {mins} 分後"
    return f"{mins} 分後"


def _ui_reset_text(resets_at, hours_to_reset):
    relative = _ui_hours_text(hours_to_reset)
    absolute = _ui_time(resets_at) if resets_at else None
    if relative and absolute and absolute != "未知":
        return f"重置 {relative}（{absolute}）"
    if absolute and absolute != "未知":
        return f"重置 {absolute}"
    if relative:
        return f"重置 {relative}"
    return "重置時間未知"


def _ui_relative_time(value, now=None):
    parsed = parse_time(value)
    if not parsed:
        return "未知"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        rel = "剛剛"
    elif seconds < 3600:
        rel = f"{seconds // 60} 分鐘前"
    elif seconds < 86400:
        rel = f"{seconds // 3600} 小時前"
    else:
        rel = f"{seconds // 86400} 天前"
    return f"{rel}（{_ui_time(value)}）"


def _ui_refresh_note(card):
    """Why the entry did not refresh, in the user's words; None when nothing to say."""
    error = card.refresh_error
    if not error:
        return None, None
    lowered = str(error).casefold()
    when = _ui_relative_time(card.refresh_attempted_at) if card.refresh_attempted_at else "剛才"
    if "access token missing" in lowered or "credentials file" in lowered or "oauth section" in lowered:
        return f"需要重新登入此帳號（憑證檔沒有有效 token）。上次嘗試：{when}", "error"
    if "401" in lowered or "authstale" in lowered:
        return f"登入已失效，需要重新登入。上次嘗試：{when}", "error"
    if "ratelimited" in lowered or "rate limited" in lowered:
        return f"採集被限流，稍後會自動重試。上次嘗試：{when}", "warn"
    return f"上次更新失敗：{_ui_text(error)}。上次嘗試：{when}", "warn"


def _ui_window_row(label, pct, resets_at, hours_to_reset):
    if pct is None:
        value, width = "未知", 0
    else:
        width = max(0.0, min(100.0, float(pct)))
        value = f"{width:.0f}%"
    return (f'<div class="qw-row"><span class="qw-label">{label}</span>'
            f'<span class="qw-value">{value}</span>'
            f'<span class="qw-bar" aria-hidden="true"><i style="width:{width:.0f}%"></i></span>'
            f'<span class="qw-reset">{_ui_reset_text(resets_at, hours_to_reset)}</span></div>')


def _quota_entity_card_html(card):
    code, label = _ui_quota_state(card)
    rows = [_ui_window_row("5H", card.five_hour_remaining_pct, card.five_hour_resets_at, card.five_hour_hours_to_reset)]
    if card.has_weekly_window:
        rows.append(_ui_window_row("每週", card.weekly_remaining_pct, card.weekly_resets_at, card.weekly_hours_to_reset))
    else:
        rows.append('<div class="qw-missing">每週窗口：來源未提供</div>')
    note, note_kind = _ui_refresh_note(card)
    note_html = f'<div class="qe-note{" qe-note-error" if note_kind == "error" else ""}">{note}</div>' if note else ""
    if code == "stale" and not note:
        note_html = '<div class="qe-note">資料已過期，不能用來派工；等待下一次成功更新。</div>'
    credits = ""
    if card.extra_credits_available is True:
        credits = f'<div class="qw-missing">額外點數：{_ui_text(card.formatted_extra_credits)}</div>'
    return (f'<div class="qe-card qe-{code}" role="group" aria-label="{_ui_entity_title(card)} 配額">'
            f'<div class="qe-head"><span class="qe-title">{_ui_entity_title(card)}</span>'
            f'<span class="qe-state qe-state-{code}">{label}</span></div>'
            + "".join(rows) + credits + note_html +
            f'<div class="qe-foot">最後更新 {_ui_relative_time(card.last_updated)}</div></div>')


def _render_quota_entities(accounts):
    if not accounts:
        st.info("目前沒有可驗證的配額資料。")
        return
    st.markdown('<div class="qe-grid">' + "".join(_quota_entity_card_html(card) for card in accounts) + "</div>",
                unsafe_allow_html=True)


def _render_quota_page_alert(brief):
    """The ONE page-level quota warning: only when nothing at all is dispatchable."""
    if brief is None or not getattr(brief, "accounts", None):
        return
    if brief.recommended_provider is None:
        st.warning(f"目前沒有可派工的 AI 帳戶：{_ui_text(brief.reason, '所有帳戶資料過期、未知或已用盡')}")


def _render_quota_card(card, compact=False):
    """One entity card; `compact` keeps the same card and only skips the technical details."""
    st.markdown(_quota_entity_card_html(card), unsafe_allow_html=True)
    if compact:
        return
    with st.expander(f"{_ui_entity_title(card)} 技術詳情", expanded=False):
        st.caption(f"來源：`{_ui_text(card.source)}`　可信度：`{_ui_text(card.confidence)}`　狀態碼：`{_ui_quota_status(card)}`")
        st.caption(f"最後更新（原始）：`{_ui_text(card.last_updated)}`　5H 重置（原始）：`{_ui_text(card.five_hour_resets_at)}`　每週重置（原始）：`{_ui_text(card.weekly_resets_at)}`")
        st.caption(f"實際可用性：`{_ui_text(card.formatted_effective_availability)}`")
        if card.warning_reason:
            st.caption(f"風險說明：{_ui_text(card.warning_reason)}")
        if card.refresh_error:
            st.caption(f"上次更新錯誤（原始）：`{_ui_text(card.refresh_error)}`　嘗試時間：`{_ui_text(card.refresh_attempted_at)}`")


def _render_execution_snapshot(data):
    executions = [
        execution for execution in data.get("all_executions", [])
        if execution.get("status") not in TERMINAL_EXECUTION_STATUSES
    ][:3]
    if not executions:
        return
    rows = []
    now = datetime.now(timezone.utc)
    for execution in executions:
        snapshot = execution.get("task_snapshot") or {}
        rows.append({
            "任務": execution.get("task_id") or "未知",
            "AI 提供者": execution.get("provider") or "未知",
            "帳戶": execution.get("account_id") or "未知",
            "狀態": determine_execution_state(execution, now).upper(),
            "目前進度": execution.get("last_provider_event") or "—",
            "Provider 工作階段": execution.get("provider_session_id") or execution.get("session_id") or "—",
            "模型 / 模式 / 努力程度": f"{execution.get('model') or snapshot.get('model') or '—'} / {execution.get('mode') or snapshot.get('mode') or '—'} / {execution.get('effort') or snapshot.get('effort') or '—'}",
            "健康度": "⚠️ 需要處理" if is_execution_stale(execution, now) else "✅ 正常",
        })
    st.table(pd.DataFrame(rows))


# ---- Presentation primitives -------------------------------------------
# One chip vocabulary and one row shape for every list on every page, so a
# state means the same thing wherever it is read. Neither adds meaning: they
# render strings the truth builders already resolved, and the state word is
# always present -- colour only reinforces the word it sits behind.
_CHIP_TONES = {
    "running": "running", "in_progress": "running", "correlating": "running",
    "finishing": "running",
    "blocked": "attention", "attention": "attention", "failed": "attention",
    "interrupted": "attention",
    "queued": "waiting", "claimed": "waiting", "submitted": "waiting",
    "reserved": "waiting", "ready": "waiting", "waiting": "waiting",
    "awaiting_validation": "waiting",
    "completed": "done", "cancelled": "done", "merged": "done",
    "deferred": "idle", "pending": "idle",
}
# Dispatch states are their own uppercase vocabulary (see
# manager.dashboard_core.compute_dispatch_state); they are rendered verbatim.
_DISPATCH_TONES = {
    "RUNNING": "running",
    "SUBMITTED": "waiting", "ACCEPTED": "waiting", "QUEUED": "waiting",
    "CLAIMED": "waiting", "WAITING_QUOTA": "waiting",
    "COMPLETED": "done", "CANCELLED": "done",
    "FAILED": "attention", "BLOCKED": "attention", "REJECTED": "attention",
    "UNKNOWN": "idle",
}
# The chip reads in the UI's language; the canonical uppercase token stays on
# the detail line, so a row can still be matched against logs and records.
_DISPATCH_LABELS = {
    "SUBMITTED": "已送出", "ACCEPTED": "已受理", "QUEUED": "排隊中",
    "CLAIMED": "已接單", "RUNNING": "執行中", "COMPLETED": "已完成",
    "FAILED": "失敗", "BLOCKED": "已阻塞", "CANCELLED": "已取消",
    "REJECTED": "已拒絕", "WAITING_QUOTA": "等待配額", "UNKNOWN": "未知",
}


def _chip(status, label=None, tone=None):
    """One state chip. `label` supplies the word when the caller already
    resolved one that differs from the raw status (a record persisted as
    running without live proof reads 需要處理, never 執行中); `tone` then
    supplies the matching colour so the two never disagree."""
    text = label if label is not None else _ui_state(status)
    resolved = tone or _CHIP_TONES.get(str(status or "").casefold(), "idle")
    return f'<span class="chip chip-{resolved}">{_ui_text(text, "未知")}</span>'


def _row(chip_html, title, right=None, meta=None, next_action=None):
    """One record as a row. All text arguments must already be escaped."""
    parts = [f'<div class="adm-row">{chip_html}<div><div class="adm-row-title"><span>{title}</span>']
    if right:
        parts.append(f'<span class="adm-row-right">{right}</span>')
    parts.append("</div>")
    if meta:
        parts.append(f'<div class="adm-row-meta">{meta}</div>')
    if next_action:
        parts.append(f'<div class="adm-row-next"><b>下一步</b>　{next_action}</div>')
    parts.append("</div></div>")
    return "".join(parts)


def _rows(items):
    return '<div class="adm-list">' + "".join(items) + "</div>"


def _ui_elapsed(started_at, now=None):
    """How long a real start timestamp has been running. None when the
    record carries no start time -- an unknown duration is never guessed."""
    parsed = parse_time(started_at)
    if not parsed:
        return None
    now = now or datetime.now(timezone.utc)
    minutes = max(0, int((now - parsed).total_seconds() // 60))
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {mins} 分"
    return f"{mins} 分"


def _render_dispatch_snapshot(data):
    projects = {project.get("project_id"): project for project in data.get("projects", []) if project.get("project_id")}
    commands = {}
    for command in data.get("all_commands", []):
        key = (command.get("project_id"), command.get("task_id"))
        if key not in commands or (command.get("created_at") or "") >= (commands[key].get("created_at") or ""):
            commands[key] = command
    executions = {execution.get("execution_id"): execution for execution in data.get("all_executions", []) if execution.get("execution_id")}
    rows = []
    for task in data.get("all_tasks", []):
        command = commands.get((task.get("project_id"), task.get("task_id")))
        execution = executions.get(command.get("execution_id")) if command else None
        rows.append(build_dispatch_truth_row(
            projects.get(task.get("project_id")), task, command, execution,
            getattr(data.get("daily_brief_vm"), "accounts", []), datetime.now(timezone.utc),
        ))

    pretask = load_pretask_dispatch_requests(tuple(sorted(projects)))
    for project_id, listing in pretask.items():
        for request in listing.get("rows", []):
            rows.append(build_pretask_dispatch_truth_row(
                projects.get(project_id), project_id, request["request_id"],
                request.get("dispatch_request_status"), getattr(data.get("daily_brief_vm"), "accounts", []),
                datetime.now(timezone.utc), dispatch_request_read_failed=request.get("dispatch_request_read_failed", False),
            ))
        if listing.get("truncated"):
            rows.append(build_pretask_listing_truncated_row(
                projects.get(project_id), project_id, getattr(data.get("daily_brief_vm"), "accounts", []),
            ))

    if not rows:
        return
    st.header("派工狀態")
    st.caption("Task／Command／Execution 的實際判定。pre-Task 列來自尚未成為 Task 的 dispatch request；UNKNOWN 表示這次刷新無法證實，不是「沒有」。")
    for row in rows[:6]:
        request_id = row.get("request_id")
        title = row.get("task_title") or row.get("task_id") or "未知任務"
        state = str(row.get("dispatch_state") or "")
        token = state.upper()
        st.markdown(_row(
            _chip(state, label=_DISPATCH_LABELS.get(token, state or "未知"),
                  tone=_DISPATCH_TONES.get(token, "idle")),
            _ui_text(title),
            right=f"request {_ui_text(request_id)}" if request_id else None,
            meta=(f'<b>{_ui_text(state, "UNKNOWN")}</b>　'
                  f'{_ui_text(row.get("dispatch_reason"), "沒有可讀的判定原因")}'),
        ), unsafe_allow_html=True)


def _render_task_detail(data):
    tasks = data.get("all_tasks", [])
    if not tasks:
        return
    task = tasks[0]
    project_id, task_id = task.get("project_id"), task.get("task_id")
    command = select_task_command(data.get("all_commands", []), project_id, task_id)
    execution = select_task_execution(data.get("all_executions", []), project_id, task_id, command=command)
    handoff_result = load_task_handoff_from_store(project_id, task_id)
    handoff = select_task_handoff(
        handoff_result.get("records", []), project_id, task_id,
        execution=execution, command=command,
    )
    with st.expander("Task 詳情", expanded=False):
        st.markdown(f"**{_ui_text(task.get('title') or task_id)}** · 狀態：{_ui_state(task.get('status'))}")
        st.markdown(f"Execution ID：`{_ui_text((execution or {}).get('execution_id'))}`")
        if handoff:
            st.markdown(f"Handoff ID：`{_ui_text(handoff.get('handoff_id'))}`")
            st.markdown(f"已完成工作：{_ui_text(', '.join(handoff.get('completed_work', [])))}")
        elif handoff_result.get("status") == READ_STATUS_UNKNOWN:
            st.warning(f"Handoff UNKNOWN：{_ui_text(handoff_result.get('error'))}")
        else:
            st.caption("此 Task 沒有可驗證的 Handoff 紀錄。")


def _render_overview(data):
    now = datetime.now(timezone.utc)
    tasks = data.get("all_tasks", [])
    commands = data.get("all_commands", [])
    executions = data.get("all_executions", [])
    brief = data.get("daily_brief_vm")
    active = _ui_active_executions(executions, now)
    # A record persisted as "running" without live proof (no host/PID
    # evidence, expired hard timeout, stale progress) is never 執行中 -- it
    # is something a person must look at.
    attention = [
        execution for execution in executions
        if execution.get("status") in {"failed", "interrupted", "cancelled"}
        or is_execution_stale(execution, now)
        or (execution.get("status") == "running" and not is_live_execution(execution, now))
    ]
    attention_tasks = [task for task in tasks if task.get("status") in {"blocked", "attention"}]
    has_attention = bool(attention or attention_tasks)
    queued = [command for command in commands if command.get("status") in {"queued", "claimed", "submitted"}]
    current_task = _ui_task_for_execution(active[0], tasks) if active else {}
    if not current_task and queued:
        current_task = next((task for task in tasks if task.get("task_id") == queued[0].get("task_id")), {})

    if active:
        headline = "執行中"
        headline_detail = f"{_ui_text(current_task.get('title') or active[0].get('task_id'))} 正由 {_ui_text(active[0].get('provider'))} 執行"
        next_action = current_task.get("next_action") or active[0].get("last_provider_event") or "等待下一個 provider 事件"
    elif has_attention:
        headline = "需要處理"
        headline_detail = "有執行紀錄需要人工確認，系統沒有把它算成正常工作中"
        next_action = "先查看異常，再決定是否重試或調整下一步"
    elif queued:
        headline = "排隊中"
        headline_detail = "已有任務或 request 在佇列，但尚未證實 provider 正在執行"
        next_action = current_task.get("next_action") or "查看任務佇列與可用配額"
    else:
        headline = "目前閒置"
        headline_detail = "目前沒有已證實正在執行的 AI 任務"
        next_action = "選擇一個待執行任務，或同步最新資料"

    tone = "running" if active else "attention" if has_attention else "waiting" if queued else "idle"
    attention_count = len(attention) + len(attention_tasks)
    reliable = sum(1 for card in (brief.accounts if brief else []) if card.has_reliable_quota)
    total = len(brief.accounts) if brief else 0

    st.title("ADM 工作台")
    st.caption("先看現在有沒有事在跑、有沒有事卡住、還剩多少配額；細節在左側各分頁下鑽。")
    # One status band instead of a headline block plus a separate metric row:
    # the four facts that decide whether a person has to act are read at one
    # glance, on the same surface as every other panel.
    st.markdown(f"""
    <section class="adm-band" aria-label="目前工作狀態">
      <div class="adm-band-grid">
        <div>
          <div class="adm-band-state"><span class="state-{tone}">{headline}</span></div>
          <div class="adm-band-detail">{headline_detail}</div>
        </div>
        <div class="adm-stat"><div class="adm-stat-k">正在做</div><div class="adm-stat-v">{len(active)}</div></div>
        <div class="adm-stat"><div class="adm-stat-k">需要處理</div><div class="adm-stat-v{' state-attention' if attention_count else ''}">{attention_count}</div></div>
        <div class="adm-stat"><div class="adm-stat-k">可用配額</div><div class="adm-stat-v">{reliable} / {total}</div></div>
      </div>
      <div class="adm-band-next"><b>下一步自動動作</b><span>{_ui_text(next_action)}</span></div>
    </section>
    """, unsafe_allow_html=True)

    st.header("正在做")
    if active:
        shown = active[:3]
        items = []
        for execution in shown:
            task = _ui_task_for_execution(execution, tasks)
            elapsed = _ui_elapsed(execution.get("started_at"), now)
            last_event_at = _ui_record_time(execution)
            items.append(_row(
                _chip("running"),
                _ui_text(task.get("title") or execution.get("task_id")),
                right=f"已執行 {elapsed}" if elapsed else None,
                meta=(f'AI <b>{_ui_text(execution.get("provider") or task.get("assigned_provider"))}'
                      f' / {_ui_text(execution.get("account_id"))}</b>　'
                      f'進度 {_ui_text(execution.get("last_provider_event") or task.get("current_progress"), "尚未回報")}　'
                      f'最後事件 {_ui_relative_time(last_event_at, now) if last_event_at else "未知"}'),
                next_action=_ui_text(task.get("next_action") or "等待最新事件"),
            ))
        st.markdown(_rows(items), unsafe_allow_html=True)
        if len(active) > len(shown):
            st.caption(f"另有 {len(active) - len(shown)} 筆已證實執行中的紀錄，完整清單在「執行」頁。")
    else:
        st.info("目前沒有已證實正在執行的任務。「執行中」需要真實 Execution 紀錄與真實 OS 進程證據兩者同時成立。")

    _render_dispatch_snapshot(data)

    focus = []
    seen = set()
    for task in sorted(tasks, key=lambda item: (item.get("status") not in {"in_progress", "ready", "queued"},
                                                item.get("priority") not in {"urgent", "high"},
                                                _ui_record_time(item))):
        task_id = task.get("task_id")
        if task_id and task_id not in seen and task.get("status") not in {"completed", "cancelled"}:
            focus.append(task)
            seen.add(task_id)
        if len(focus) == 4:
            break
    if len(focus) < 2:
        for item in (data.get("overview") or {}).get("items", []):
            item_id = item.get("item_id")
            if item_id and item_id not in seen and item.get("status") not in {"completed", "cancelled", "merged"}:
                focus.append({
                    "task_id": item_id,
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "current_progress": item.get("current_progress"),
                    "next_action": item.get("next_action"),
                })
                seen.add(item_id)
            if len(focus) == 4:
                break
    st.header("今日重點")
    if focus:
        # Each line carries its own real status, so 待執行 and 執行中 are not
        # read as the same thing just because they sit in the same list.
        st.markdown(_rows([
            _row(
                _chip(task.get("status")),
                _ui_text(task.get("title") or task.get("task_id")),
                right=_ui_text(task.get("task_id"), "") if task.get("title") else None,
                meta=f'進度 {_ui_text(task.get("current_progress"), "尚未回報")}',
                next_action=_ui_text(task.get("next_action")) if task.get("next_action") else None,
            )
            for task in focus
        ]), unsafe_allow_html=True)
    else:
        st.info("目前沒有可驗證的今日任務。")

    if has_attention:
        st.header("需要處理")
        # Same row vocabulary as every other list, with the attention tone:
        # an amber st.warning would have said 等待 in this palette, which is
        # not what any of these records mean.
        shown_executions = attention[:3]
        shown_tasks = attention_tasks[:max(0, 3 - len(shown_executions))]
        items = []
        for execution in shown_executions:
            task = _ui_task_for_execution(execution, tasks)
            items.append(_row(
                _chip(execution.get("status"), label="需要處理", tone="attention"),
                _ui_text(task.get("title") or execution.get("task_id")),
                right="到「執行」頁查看",
                meta=f'執行狀態 <b>{_ui_state(execution.get("status"))}</b>，未證實仍在執行。',
            ))
        for task in shown_tasks:
            items.append(_row(
                _chip(task.get("status"), label="需要處理", tone="attention"),
                _ui_text(task.get("title") or task.get("task_id")),
                right=_ui_text(task.get("task_id"), "") if task.get("title") else None,
                meta=f'任務狀態 <b>{_ui_state(task.get("status"))}</b>。',
                next_action=_ui_text(task.get("next_action")) if task.get("next_action") else None,
            ))
        st.markdown(_rows(items), unsafe_allow_html=True)
        remaining = attention_count - len(items)
        if remaining > 0:
            st.caption(f"另有 {remaining} 筆需要處理的紀錄未列出；完整清單在「執行」與「任務」頁。")

    st.header("配額與重置")
    _render_quota_page_alert(brief)
    _render_quota_entities(brief.accounts if brief else [])
    _render_sync_status_line(data)
    _render_task_detail(data)
    if data.get("warnings"):
        with st.expander(f"資料警告（{len(data['warnings'])}）", expanded=False):
            for warning in data["warnings"][:12]:
                st.warning(warning)


def _render_projects(data):
    projects = data.get("projects", [])
    tasks = data.get("all_tasks", [])
    st.title("專案")
    st.caption("每個專案的里程碑、進度、目前任務與阻塞；首頁只保留目前工作的摘要。")
    if not projects:
        st.info("目前沒有可驗證的專案資料。")
        return
    items = []
    for project in projects:
        project_tasks = [task for task in tasks if task.get("project_id") == project.get("project_id")]
        active = sum(task.get("status") in {"in_progress", "running", "queued", "ready"} for task in project_tasks)
        blocked = sum(task.get("status") in {"blocked", "attention"} for task in project_tasks)
        # The chip summarises this project's own task counts -- nothing more.
        label, tone = ("需要處理", "attention") if blocked else ("進行中", "running") if active else ("無進行中", "idle")
        items.append(_row(
            _chip(None, label=label, tone=tone),
            _ui_text(project.get("name") or project.get("project_id")),
            right=_ui_text(project.get("project_id"), "") if project.get("name") else None,
            meta=f"進行中 <b>{active}</b>　需要處理 <b>{blocked}</b>　近期任務 <b>{len(project_tasks)}</b>",
        ))
    st.markdown(_rows(items), unsafe_allow_html=True)


def _render_tasks(data):
    tasks = data.get("all_tasks", [])
    st.title("任務")
    st.caption("待處理／進行中／等待驗收／已完成的任務明細；首頁不承擔歷史 backlog。")
    if not tasks:
        st.info("目前沒有可驗證的任務資料。")
        return
    items = []
    for task in tasks[:12]:
        updated_at = _ui_record_time(task)
        items.append(_row(
            _chip(task.get("status")),
            _ui_text(task.get("title") or task.get("task_id")),
            right=_ui_relative_time(updated_at) if updated_at else None,
            meta=(f'{_ui_text(task.get("task_id"))}　'
                  f'目前進度 {_ui_text(task.get("current_progress"), "尚未回報")}'),
            next_action=_ui_text(task.get("next_action")) if task.get("next_action") else None,
        ))
    st.markdown(_rows(items), unsafe_allow_html=True)
    if len(tasks) > 12:
        st.caption(f"另有 {len(tasks) - 12} 筆近期任務未列出。")


def _render_sync_status_line(data):
    """Last GitHub / Drive / runtime sync facts, one line, human words."""
    evidence = read_provenance_evidence_file() or {}
    running = str(evidence.get("running_sha") or "")[:7] or "未知"
    consistent = bool(evidence.get("running_sha")) and evidence.get("running_sha") == evidence.get("tested_sha") == evidence.get("activated_sha")
    captured = _ui_relative_time(evidence.get("captured_at")) if evidence.get("captured_at") else "未知"
    read_status = data.get("read_status") or {}
    drive_ok = all((value[0] if isinstance(value, tuple) else value) == READ_STATUS_OK for value in read_status.values()) if read_status else bool(data.get("success"))
    st.caption(f"同步狀態：Drive 讀取 {'正常' if drive_ok else '部分失敗'}　·　Runtime {running}（{'TESTED／ACTIVATED／RUNNING 一致' if consistent else '版本一致性未證實'}，證據更新 {captured}）")


def _render_executions(data):
    executions = data.get("all_executions", [])
    sessions = data.get("all_sessions", [])
    commands = {c.get("execution_id"): c for c in data.get("all_commands", []) if c.get("execution_id")}
    tasks = data.get("all_tasks", [])
    now = datetime.now(timezone.utc)
    st.title("執行")
    st.caption("真實執行鏈：Task → Command → Execution → Provider Session。只有真實 OS 進程與真實 Execution 才會顯示「執行中」。")
    rows = []
    for execution in executions[:24]:
        snapshot = execution.get("task_snapshot") or {}
        command = commands.get(execution.get("execution_id")) or {}
        evidence = execution.get("provider_evidence") or {}
        terminal = execution.get("status") in TERMINAL_EXECUTION_STATUSES
        if terminal:
            state = _ui_state(execution.get("status"))
        elif is_live_execution(execution, now):
            state = "執行中"
        elif execution.get("status") == "running":
            # Persisted as running but without live proof (no host/PID
            # evidence, expired hard timeout, stale progress): never 執行中.
            state = "需要處理"
        else:
            state = _ui_state(execution.get("status"))
        task = _ui_task_for_execution(execution, tasks)
        # Column order follows the reading order of the question this page
        # answers: what state, whose task, which AI, what happened last, when,
        # and only then the lifecycle identifiers and process evidence.
        rows.append({
            "狀態": state,
            "任務": task.get("title") or execution.get("task_id") or "未知",
            "AI／帳戶": f"{execution.get('provider') or '未知'} / {execution.get('account_id') or '—'}",
            "最後事件": execution.get("last_provider_event") or "—",
            "最近心跳": _ui_time(execution.get("heartbeat_at")) if execution.get("heartbeat_at") else "—",
            "結果": execution.get("terminal_reason") or ((execution.get("cleanup_evidence") or {}).get("provider_outcome")) or "—",
            "Command": command.get("command_id") or "—",
            "Execution": execution.get("execution_id") or "—",
            "Session": execution.get("provider_session_id") or execution.get("session_id") or "—",
            "分支／基準": f"{(snapshot.get('branch') or '—').replace('refs/heads/', '')} @ {str(snapshot.get('baseline_head') or '')[:7] or '—'}",
            "主機／PID": f"{evidence.get('host') or '—'} / {evidence.get('pid') or '—'}",
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("目前沒有可驗證的 Execution。")
    if sessions:
        with st.expander(f"Provider Session（{len(sessions)}）", expanded=False):
            st.dataframe(pd.DataFrame([{
                "狀態": _ui_state(session.get("status")), "Provider": session.get("provider") or "未知",
                "帳戶": session.get("account_id") or "—", "Session": session.get("provider_session_id") or session.get("session_id") or "—",
                "Task": session.get("task_id") or "—", "最後更新": _ui_time(_ui_record_time(session)),
            } for session in sessions[:24]]), use_container_width=True, hide_index=True)


def _render_history(data, embedded=False):
    records = []
    for kind in ("all_tasks", "all_commands", "all_executions", "all_handoffs"):
        for record in data.get(kind, []):
            records.append({
                "時間": _ui_time(_ui_record_time(record)),
                "類型": kind.removeprefix("all_").rstrip("s").upper(),
                "狀態": _ui_state(record.get("status")),
                "Task": record.get("task_id") or "UNKNOWN",
                "事件": record.get("last_provider_event") or record.get("current_progress") or record.get("next_action") or record.get("reason") or "UNKNOWN",
                "排序": _ui_record_time(record),
            })
    records.sort(key=lambda row: row["排序"], reverse=True)
    if not embedded:
        st.title("紀錄")
    st.caption("最近的 lifecycle 紀錄。原始 record 只在需要時展開查看。")
    if records:
        st.dataframe(pd.DataFrame([{key: row[key] for key in ("時間", "類型", "狀態", "Task", "事件")} for row in records[:24]]), use_container_width=True, hide_index=True)
    else:
        st.info("目前沒有可驗證的歷史紀錄。")


def _render_quota(data):
    brief = data.get("daily_brief_vm")
    st.title("配額與重置")
    st.caption("三個獨立實體：Codex、Claude A、Claude B。每張卡同時顯示 5H 與每週；UNKNOWN、STALE、已用盡各自是不同狀態，不會偽裝成 0%。")
    _render_quota_page_alert(brief)
    accounts = getattr(brief, "accounts", [])
    if not accounts:
        st.info("目前沒有可驗證的配額資料。")
        return
    for card in accounts:
        _render_quota_card(card)
    hidden = getattr(brief, "hidden_legacy_accounts", [])
    if hidden:
        st.caption(f"已隱藏 {len(hidden)} 筆沒有帳號 ID 的舊版彙總紀錄（{'、'.join(hidden)}）；它們不參與派工，也不算第三個帳號。")


def _render_ideas(data):
    st.title("想法")
    st.caption("想法在正式 admission 之前不是開發任務；這裡只讀取本機 ideas.json。")
    home = os.environ.get("AI_MANAGER_HOME") or os.path.expanduser("~/.ai-development-manager")
    try:
        ideas = json.loads((Path(home) / "ideas.json").read_text(encoding="utf-8"))
    except Exception:
        ideas = []
    if isinstance(ideas, dict):
        ideas = ideas.get("ideas") or []
    if not ideas:
        st.info("目前沒有想法紀錄。新增想法後，需經 ADM admission 才會成為任務。")
        return
    st.markdown(_rows([
        _row(
            _chip(idea.get("status"), label=idea.get("status") or "未分類"),
            _ui_text(idea.get("title") or idea.get("idea_id")),
            right=_ui_time(idea.get("created_at")) if idea.get("created_at") else None,
            meta=_ui_text(idea.get("summary") or idea.get("description"), "") or None,
        )
        for idea in ideas[:50] if isinstance(idea, dict)
    ]), unsafe_allow_html=True)


def _render_health(data):
    st.title("系統健康")
    st.caption("Watcher／Supervisor／Scheduled Task、Drive／GitHub 讀取、Runtime SHA 與 provenance 一致性，以及最近紀錄。")
    now = datetime.now(timezone.utc)
    active = _ui_active_executions(data.get("all_executions", []), now)
    try:
        watcher_vm, supervisor_vm, session_vm = load_infra_health(active if data.get("success") else None)
        labels = {"Online": "正常", "Offline": "離線", "Unknown": "未知"}
        tones = {"Online": "running", "Offline": "attention", "Unknown": "idle"}
        st.markdown('<div class="adm-hgrid">' + "".join(
            f'<div class="adm-hcard"><div class="adm-hcard-name">{_ui_text(vm.name)}</div>'
            f'<div class="adm-hcard-state state-{tones.get(vm.status_label, "idle")}">'
            f'{labels.get(vm.status_label, _ui_text(vm.status_label))}</div>'
            f'<div class="adm-hcard-detail">{_ui_text(vm.detail)}</div></div>'
            for vm in (watcher_vm, supervisor_vm, session_vm)
        ) + "</div>", unsafe_allow_html=True)
    except Exception as exc:
        st.warning(f"無法評估 Watcher 或 Session Center 健康狀態：{_ui_text(exc)}")
    evidence = read_provenance_evidence_file() or {}
    st.subheader("Runtime 與 provenance")
    if evidence:
        consistent = evidence.get("running_sha") == evidence.get("tested_sha") == evidence.get("activated_sha")
        st.markdown(f"- RUNNING `{_ui_text(evidence.get('running_sha'))}`\n- TESTED `{_ui_text(evidence.get('tested_sha'))}`\n- ACTIVATED `{_ui_text(evidence.get('activated_sha'))}`\n- 一致性：**{'一致' if consistent else '不一致'}**　證據更新：{_ui_relative_time(evidence.get('captured_at'))}\n- 位置：`{_ui_text(evidence.get('repository_path'))}`（{_ui_text(evidence.get('branch'))}）")
    else:
        st.info("找不到 provenance runtime 證據檔；無法證實 TESTED／ACTIVATED／RUNNING 一致。")
    _render_sync_status_line(data)
    warnings = data.get("warnings", [])
    if warnings:
        with st.expander(f"資料讀取警告（{len(warnings)}）", expanded=False):
            for warning in warnings[:24]:
                st.warning(warning)
    st.subheader("最近紀錄")
    _render_history(data, embedded=True)


def _render_ui_v3():
    query = getattr(st, "query_params", {})
    requested = query.get("view") if hasattr(query, "get") else None
    requested = NAV_QUERY_ALIASES.get(requested, requested)
    requested_route = next((label for label, value in NAV_QUERY_VALUES.items() if value == requested), NAV_OVERVIEW)

    with st.sidebar:
        st.header("ADM")
        if st.button("立即同步資料", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption("資料來源：Google Drive runtime SSOT。這個 UI 只讀取，不修改 lifecycle 或 credential。")
        selected = st.radio("工作區", NAV_OPTIONS, index=NAV_OPTIONS.index(requested_route), key="adm_ui_route")
        if hasattr(st, "query_params") and st.query_params.get("view") != NAV_QUERY_VALUES[selected]:
            st.query_params["view"] = NAV_QUERY_VALUES[selected]
        st.caption(f"每 {AUTO_REFRESH_INTERVAL_SECONDS} 秒自動更新")

    data = load_all_data(include_all_projects=selected != NAV_OVERVIEW)
    if not data.get("success") and not data.get("daily_brief_vm"):
        st.error(f"無法取得資料：{_ui_text(data.get('error'))}")
        return

    if selected == NAV_OVERVIEW:
        _render_overview(data)
    elif selected == NAV_PROJECTS:
        _render_projects(data)
    elif selected == NAV_TASKS:
        _render_tasks(data)
    elif selected == NAV_EXECUTIONS:
        _render_executions(data)
    elif selected == NAV_QUOTA:
        _render_quota(data)
    elif selected == NAV_IDEAS:
        _render_ideas(data)
    else:
        _render_health(data)


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
        clear_live_data = getattr(load_all_data, "clear", None)
        if clear_live_data:
            clear_live_data()
        load_infra_health.clear()
        load_pretask_dispatch_requests.clear()
        st.rerun()
    st.session_state[_AUTO_REFRESH_ARMED_KEY] = True


_refresh_dashboard_automatically()

_render_ui_v3()
st.stop()

st.title("AI 開發管理器")
st.caption("營運控制台 · 即時任務、執行狀態與配額")

# Sidebar Refresh & Status
with st.sidebar:
    st.header("控制台")
    _view = st.radio("檢視", ["首頁工作台", "完整資料"], label_visibility="visible")
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

# Workbench home: the first paint is intentionally a decision surface. The
# existing lifecycle tables and technical inspectors remain below this point
# for the secondary views, while HOME shows only proven active work and quota.
st.markdown('<div class="workbench-section"><div class="section-kicker">現在正在做</div><div class="section-title">活動中的真實任務</div></div>', unsafe_allow_html=True)
if not _quick_active:
    st.markdown('<div class="active-card"><h3>目前沒有已證實執行中的任務</h3><div class="active-meta">系統會在取得 provider session evidence 後才標記為執行中。</div></div>', unsafe_allow_html=True)
else:
    _task_by_id = {t.get("task_id"): t for t in all_tasks}
    _active_cards = []
    for _execution in _quick_active[:3]:
        _task = _task_by_id.get(_execution.get("task_id"), {})
        _title = _task.get("title") or (_execution.get("task_snapshot") or {}).get("title") or "未命名任務"
        _provider = _execution.get("provider") or "未知 provider"
        _account = _execution.get("account_id") or "未標示帳戶"
        _phase = _execution.get("last_provider_event") or "執行中，等待下一個進度訊號"
        _blocker = _execution.get("blocker") or _task.get("blocker") or "目前沒有已知阻塞"
        _active_cards.append(f'''<div class="active-card"><h3>{_title}</h3><div class="active-meta"><b>{_provider}</b> · {_account}<br>階段：{_phase}<br>阻塞：{_blocker}</div></div>''')
    st.markdown('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px">' + ''.join(_active_cards) + '</div>', unsafe_allow_html=True)

st.markdown('<div class="workbench-section"><div class="section-kicker">資源餘額</div><div class="section-title">Quota · 剩餘與重置</div></div>', unsafe_allow_html=True)
if not daily_brief_vm.accounts:
    st.markdown('<div class="active-card"><h3>尚無可用的 quota 資料</h3><div class="active-meta">目前沒有已接入且可驗證的 AI provider。</div></div>', unsafe_allow_html=True)
else:
    _quota_cards = []
    for _card in daily_brief_vm.accounts:
        _outer = max(0, min(100, float(_card.five_hour_remaining_pct))) if _card.five_hour_remaining_pct is not None else 0
        _inner = max(0, min(100, float(_card.weekly_remaining_pct))) if _card.weekly_remaining_pct is not None else 0
        _has_week = _card.has_weekly_window and _card.weekly_remaining_pct is not None
        _ring = (f'<div class="quota-ring double" style="--outer:{_outer};--inner:{_inner}"><div class="quota-ring-inner"><span>{_inner:.0f}%<br>每週</span></div></div>'
                 if _has_week else f'<div class="quota-ring single" style="--pct:{_outer}"><div class="quota-ring-inner">{_outer:.0f}%<br>5H</div></div>')
        _fresh = "已過期" if _card.stale else "最新"
        _fresh_class = "state-attention" if _card.stale else "state-running"
        _weekly_line = f'每週 {_card.formatted_weekly_remaining} · 重置 {_card.weekly_resets_at or "—"}<br>' if _has_week else ''
        _quota_cards.append(f'''<div class="quota-card">{_ring}<div><div class="quota-number">{_card.card_title}</div><div class="quota-meta"><b>5H {_card.formatted_five_hour_remaining}</b> · 重置 {_card.five_hour_resets_at or "—"}<br>{_weekly_line}<span class="{_fresh_class}">{_fresh}</span></div></div></div>''')
    st.markdown('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:14px">' + ''.join(_quota_cards) + '</div>', unsafe_allow_html=True)
    st.caption("外圈為 5H；有真實 weekly window 時，內圈為每週。未有資料的窗口不會被推算。")

st.markdown('<div class="workbench-section"><div class="section-kicker">下一步</div><div class="section-title">今日重點</div></div>', unsafe_allow_html=True)
_next_steps = []
for _task in all_tasks:
    if _task.get("task_id") in {e.get("task_id") for e in _quick_active}:
        continue
    _next = _task.get("next_action")
    if _next and _next not in _next_steps:
        _next_steps.append(_next)
for _warning in (daily_brief_vm.telemetry_warnings or []):
    if _warning not in _next_steps:
        _next_steps.append(_warning)
if _next_steps:
    st.markdown("\n".join(f"- {item}" for item in _next_steps[:4]))
else:
    st.markdown("目前沒有需要立即處理的事項。")

# ponytail: keep the dense legacy inspectors available to the existing
# Streamlit route while stopping the HOME first paint here.
if _view == "首頁工作台":
    st.stop()

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


def _localize_quota_value(value):
    """Translate display-only quota labels without changing raw truth values."""
    text = str(value)
    for source, label in (
        ("Unknown / Stale", "未知／已過期"),
        ("Available via credits", "可透過額外額度使用"),
        ("Not available", "不可用"),
        ("Unavailable", "不可用"),
        ("Available", "可用"),
    ):
        text = text.replace(source, label)
    return text.replace("balance:", "餘額：")

action_badge_class = {
    "consume": "badge-action-consume",
    "normal": "badge-action-normal",
    "conserve": "badge-action-conserve",
    "hold": "badge-action-hold"
}.get(daily_brief_vm.recommended_action, "badge-unknown")

action_label = {
    "consume": "立即消耗",
    "normal": "正常使用",
    "conserve": "節省使用",
    "hold": "暫緩派工",
}.get(daily_brief_vm.recommended_action, "未知")

recommended_card = next(
    (
        c for c in daily_brief_vm.accounts
        if c.provider == daily_brief_vm.recommended_provider and c.account_id == daily_brief_vm.recommended_account
    ),
    None,
)

truth_line_html = ""
if recommended_card is not None:
    recommendation_availability = _localize_quota_value(recommended_card.formatted_effective_availability)
    truth_line_html = f"""
    <div style="font-size: 0.85rem; color: #8b949e; margin-bottom: 10px;">
        主要配額：<b>{recommended_card.formatted_five_hour_remaining}</b> &nbsp;|&nbsp;
        額外額度：<b>{recommended_card.formatted_extra_credits}</b> &nbsp;|&nbsp;
        實際可用：<b>{recommendation_availability}</b>
    </div>
    """

st.markdown(f"""
<div class="recommendation-card">
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <span style="font-size: 1.25rem; font-weight: 700; color: #ffffff;">
            建議使用：<span style="color: #58a6ff;">{daily_brief_vm.recommended_display_name}</span>
        </span>
        <span class="badge {action_badge_class}" style="font-size: 0.85rem; padding: 6px 14px;">
            建議動作：{action_label}
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
                    st.write(f"• **額外額度**：`{_localize_quota_value(card.formatted_extra_credits)}`")
                st.write(f"• **實際可用性**：`{_localize_quota_value(card.formatted_effective_availability)}`")

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
