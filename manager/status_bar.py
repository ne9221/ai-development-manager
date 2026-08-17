#!/usr/bin/env python3
"""SB-1: read-only Status Bar truth-projection snapshot.

Composes already-authoritative evidence -- the Execution/Task Drive records,
the quota SSOT, and local git state -- into one fail-closed display snapshot.
Never writes anything and never infers past what its inputs actually prove:
a field with no trustworthy evidence is UNKNOWN/null, not a guess.

``build_snapshot`` is pure (no I/O) so it is fully unit-testable; the
``fetch_*`` helpers and ``main`` do the actual reads and turn any read
failure into an explicit unreachable/unknown marker instead of raising.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

from manager.quota_reader import read_drive_status, summarize
from manager.session_center import _authoritative_state
from manager.tasks import DriveRecords


UNKNOWN = "UNKNOWN"

# Matches the staleness threshold executions.py (STALE_AFTER_SECONDS) and
# dashboard_core.py already use for the identical "is this execution still
# actually alive" question -- not a new, independently-invented constant.
# Always pass explicitly where the caller cares; this is only the default.
DEFAULT_ACTIVE_EVIDENCE_MAX_AGE_SECONDS = 15 * 60


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def _active_evidence_at(execution):
    """The most recent timestamp that actually names live/current activity,
    or None if there isn't one. Deliberately narrower than
    _last_trustworthy_evidence(): reserved_at/started_at/completed_at, a
    record merely existing, and process/PID liveness are all excluded on
    purpose -- none of them prove the run is still doing anything *now*,
    only that it once began, once ended, or that some process is alive
    (which is not the same claim). Only heartbeat_at and
    progress_updated_at qualify. A malformed timestamp is treated the same
    as a missing one -- it is not evidence."""
    parsed = [_parse_timestamp(execution.get("heartbeat_at")), _parse_timestamp(execution.get("progress_updated_at"))]
    valid = [value for value in parsed if value is not None]
    return max(valid) if valid else None


def _authoritative_status(execution, now=None, active_evidence_max_age_seconds=DEFAULT_ACTIVE_EVIDENCE_MAX_AGE_SECONDS):
    """UNKNOWN with no execution evidence at all; reuses Session Center's
    fail-closed terminal/cleanup rule so a terminal execution whose
    cleanup_evidence still shows a retained task claim or writer lease never
    displays as fully completed -- it degrades to "finishing" instead.

    A "running" record additionally requires trustworthy active evidence
    (see _active_evidence_at): a status field alone -- however old the
    record, however fresh its reserved_at/started_at, however alive its
    process -- is never sufficient proof the run is still active. No
    evidence, stale evidence, or unparseable evidence all fail closed to
    UNKNOWN rather than defaulting to a guessed "running"."""
    if not isinstance(execution, dict):
        return UNKNOWN
    state = _authoritative_state(execution)
    if not isinstance(state, str):
        return UNKNOWN
    if state != "running":
        return state
    evidence_at = _active_evidence_at(execution)
    if evidence_at is None:
        return UNKNOWN
    age_seconds = ((now or datetime.now(timezone.utc)) - evidence_at).total_seconds()
    if age_seconds < 0 or age_seconds > active_evidence_max_age_seconds:
        return UNKNOWN
    return "running"


def _last_trustworthy_evidence(execution):
    """The most recent authoritative Execution timestamp actually present,
    with the field it came from -- never a wall-clock guess."""
    if not isinstance(execution, dict):
        return {"source": None, "at": None}
    candidates = (
        ("execution_completed_at", execution.get("completed_at")),
        ("execution_progress_updated_at", execution.get("progress_updated_at")),
        ("execution_heartbeat_at", execution.get("heartbeat_at")),
        ("execution_started_at", execution.get("started_at")),
        ("execution_reserved_at", execution.get("reserved_at")),
    )
    timestamped = [(source, at) for source, at in candidates if isinstance(at, str) and at]
    if not timestamped:
        return {"source": None, "at": None}
    source, at = max(timestamped, key=lambda pair: pair[1])
    return {"source": source, "at": at}


def _quota_projection(quota_document, provider, now=None, max_age_minutes=60):
    """Reuses quota_reader's own stale/reliable determination rather than
    re-deriving it, so freshness rules never drift between consumers."""
    if provider is None or quota_document is None:
        return {"remaining_percent": None, "freshness": "unknown", "reason": "no_provider_or_quota_document"}
    try:
        report = summarize(quota_document, max_age_minutes=max_age_minutes, now=now)
    except Exception:
        return {"remaining_percent": None, "freshness": "unknown", "reason": "quota_summary_failed"}
    entry = next((item for item in report["providers"] if item["provider"] == provider), None)
    if entry is None:
        return {"remaining_percent": None, "freshness": "unknown", "reason": "provider_not_reported"}
    freshness = entry["freshness"]
    if not entry["has_reliable_quota"]:
        return {"remaining_percent": None, "freshness": freshness, "reason": "unreliable_or_incomplete_source"}
    remaining = [window["remaining_percent"] for window in entry["windows"] if window.get("remaining_percent") is not None]
    if not remaining:
        return {"remaining_percent": None, "freshness": freshness, "reason": "no_remaining_percent_reported"}
    # Most conservative (lowest-remaining) window governs when several exist.
    return {"remaining_percent": min(remaining), "freshness": freshness, "reason": None}


def _blocker(task):
    if not isinstance(task, dict) or task.get("status") != "blocked":
        return None
    reason = task.get("blocked_reason")
    return reason if isinstance(reason, str) and reason else None


def _account_alias(execution):
    """No authoritative per-account identity evidence exists anywhere in the
    current baseline (Claude two-account routing has not merged to main; see
    P0.1/P0.1.5 branches). Always UNKNOWN -- never fabricate an alias, and
    never gate this on execution content, so this cannot silently start
    lying the moment an execution happens to carry an unrelated field."""
    return UNKNOWN


def _needs_user_action():
    """No field in the current baseline schemas is an authoritative 'needs
    user action' signal. Returns null rather than inventing one (e.g. from
    blocked/attention heuristics) -- see task instructions: no fake field
    sources."""
    return None


def _run_git(cwd, *arguments, runner=subprocess.run):
    try:
        result = runner(["git", "-C", cwd, *arguments], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def github_sync_status(repo_dir, runner=subprocess.run):
    """Local branch vs. its upstream -- ahead/behind/diverged/synced, or an
    explicit unknown with a reason. Never raises: no repo, no upstream, or
    git itself being unavailable all degrade to unknown instead of a guess.
    ``runner`` follows the same injection contract as worktree_locks._git."""
    if not repo_dir:
        return {"state": "unknown", "ahead": None, "behind": None, "reason": "no_repo_dir"}
    upstream = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", runner=runner)
    if not upstream:
        return {"state": "unknown", "ahead": None, "behind": None, "reason": "no_upstream_or_not_a_git_repo"}
    counts = _run_git(repo_dir, "rev-list", "--left-right", "--count", f"{upstream}...HEAD", runner=runner)
    if not counts:
        return {"state": "unknown", "ahead": None, "behind": None, "reason": "rev_list_failed"}
    parts = counts.split()
    if len(parts) != 2:
        return {"state": "unknown", "ahead": None, "behind": None, "reason": "unparseable_rev_list_output"}
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return {"state": "unknown", "ahead": None, "behind": None, "reason": "unparseable_rev_list_output"}
    if ahead and behind:
        state = "diverged"
    elif ahead:
        state = "ahead"
    elif behind:
        state = "behind"
    else:
        state = "synced"
    return {"state": state, "ahead": ahead, "behind": behind, "reason": None}


def _drive_sync_status(read_error):
    """Whether this snapshot's own live Drive reads actually reached the
    SSOT. A prior local cache is never treated as proof of a synced cloud
    state -- absence of a read error is the only thing that counts."""
    if read_error is None:
        return {"state": "reachable", "reason": None}
    return {"state": "unreachable", "reason": str(read_error)[:200] or type(read_error).__name__}


def build_snapshot(execution=None, task=None, quota_document=None, github_repo_dir=None,
                    drive_error=None, quota_max_age_minutes=60, now=None, git_runner=subprocess.run,
                    active_evidence_max_age_seconds=DEFAULT_ACTIVE_EVIDENCE_MAX_AGE_SECONDS):
    """Pure aside from the injected git_runner call: assembles a display
    snapshot from already-fetched evidence and never mutates anything it is
    given."""
    now = now or datetime.now(timezone.utc)
    provider = execution.get("provider") if isinstance(execution, dict) else None
    project_id = (execution or {}).get("project_id") or (task or {}).get("project_id")
    task_id = (execution or {}).get("task_id") or (task or {}).get("task_id")
    session_id = (execution or {}).get("session_id")
    execution_id = (execution or {}).get("execution_id")
    return {
        "generated_at": now_iso(),
        "provider": provider or UNKNOWN,
        "account_alias": _account_alias(execution),
        "project_id": project_id or UNKNOWN,
        "task_id": task_id or UNKNOWN,
        "session_id": session_id or UNKNOWN,
        "execution_id": execution_id or UNKNOWN,
        "status": _authoritative_status(execution, now, active_evidence_max_age_seconds),
        "last_trustworthy_evidence": _last_trustworthy_evidence(execution),
        "quota": _quota_projection(quota_document, provider, now, quota_max_age_minutes),
        "blocker": _blocker(task),
        "needs_user_action": _needs_user_action(),
        "github_sync": github_sync_status(github_repo_dir, runner=git_runner),
        "drive_sync": _drive_sync_status(drive_error),
    }


def fetch_execution(store, project_id, execution_id):
    """Read-only; any failure (not found, Drive unreachable, malformed
    response) degrades to (None, error) instead of propagating."""
    if not project_id or not execution_id:
        return None, None
    try:
        return store.get("executions", project_id, execution_id), None
    except Exception as exc:
        return None, exc


def fetch_task(store, project_id, task_id):
    if not project_id or not task_id:
        return None, None
    try:
        return store.get("tasks", project_id, task_id), None
    except Exception as exc:
        return None, exc


def fetch_quota_document(service):
    try:
        return read_drive_status(service=service), None
    except Exception as exc:
        return None, exc


def fetch_snapshot(store, service, project_id=None, execution_id=None, task_id=None,
                    github_repo_dir=None, quota_max_age_minutes=60, git_runner=subprocess.run,
                    active_evidence_max_age_seconds=DEFAULT_ACTIVE_EVIDENCE_MAX_AGE_SECONDS, now=None):
    """Read-only end-to-end build: reads Drive live, never writes, and never
    raises -- any read failure surfaces as drive_sync.state=='unreachable'
    plus the affected fields falling back to UNKNOWN/null."""
    execution, execution_error = fetch_execution(store, project_id, execution_id)
    task, task_error = fetch_task(store, project_id, task_id)
    quota_document, quota_error = fetch_quota_document(service)
    drive_error = execution_error or task_error or quota_error
    return build_snapshot(
        execution=execution, task=task, quota_document=quota_document,
        github_repo_dir=github_repo_dir, drive_error=drive_error,
        quota_max_age_minutes=quota_max_age_minutes, git_runner=git_runner,
        active_evidence_max_age_seconds=active_evidence_max_age_seconds, now=now,
    )


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project-id")
    result.add_argument("--execution-id")
    result.add_argument("--task-id")
    result.add_argument("--repo-dir", help="Local git working tree to report GitHub sync state for")
    result.add_argument("--quota-max-age-minutes", type=float, default=60)
    result.add_argument("--active-evidence-max-age-seconds", type=float, default=DEFAULT_ACTIVE_EVIDENCE_MAX_AGE_SECONDS)
    return result


def main():
    args = parser().parse_args()
    from collectors.publish_drive import build_service
    try:
        service = build_service()
        store = DriveRecords(service)
    except Exception as exc:
        print(json.dumps(build_snapshot(drive_error=exc, github_repo_dir=args.repo_dir)))
        return 0
    snapshot = fetch_snapshot(
        store, service, args.project_id, args.execution_id, args.task_id,
        args.repo_dir, args.quota_max_age_minutes,
        active_evidence_max_age_seconds=args.active_evidence_max_age_seconds,
    )
    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
