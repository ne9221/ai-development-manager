"""Canonical, locally durable health/remediation evidence for the P1-G
Global Self-Heal effort.

One shared JSON store (health_evidence_path) covers Dashboard, Session
Center, Command Watcher, Drive, and Fleet/quota freshness with the same
schema: state, last successful health check, degraded reason, last
remediation attempt, remediation result, unresolved human blocker,
observed pid/port, and a timestamp -- so any component's degradation
looks the same to a reader instead of five bespoke shapes.

This module never performs the underlying health check or remediation
action itself for Session Center / Command Watcher / Drive / quota --
those already have real, tested detection logic elsewhere
(manager.session_center_supervisor's own evidence log,
manager.dashboard_core.parse_scheduled_task_health /
build_session_center_health, manager.quota_reader.summarize). The
`*_evidence_from_*` adapters here only normalize that existing,
already-observed truth into the shared schema -- they read, they never
probe or act. manager.runtime_supervisor is what actually performs the
checks and calls `record()` on a live schedule (see that module for the
live recovery path); Dashboard is recorded the same way via its own
`record-dashboard` CLI subcommand.

Self-heal classification (classify_remediation) is fail-closed by
design: only a remediation reason explicitly listed in
SAFE_AUTO_REMEDIATIONS is ever "auto" -- anything else, including a
reason this module has never seen before, is "human_required". A
scheduled task existing, or a process having been started, is never by
itself evidence of "healthy" -- callers must pass a state derived from an
actual observed check.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Deliberately not `from manager.refresh_status import write_atomic`: that
# module's other top-level imports transitively pull in jsonschema and the
# full collectors package just for a 4-line atomic-write helper. This
# module is meant to be cheap to invoke on every tick, so it keeps its own
# copy (identical behavior: temp file + os.replace) instead of taking on
# that dependency weight.


def write_atomic(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

COMPONENTS = ("dashboard", "session_center", "command_watcher", "drive", "quota",
              "drive_dispatch_ingress", "github_dispatch_ingress")
STATES = ("healthy", "degraded", "unknown")
MAX_EVIDENCE_HISTORY = 50

# Only ever auto-remediated -- see module docstring's fail-closed contract.
SAFE_AUTO_REMEDIATIONS = frozenset({
    "transient_http_read_retry",
    "dashboard_process_missing",
    "verified_adm_session_center_orphan",
    "stale_telemetry_recollect",
    "oauth_access_token_refresh",
    "scheduled_task_heartbeat_stale_restart",
})

# Explicitly never auto-remediated -- always surfaced as human_required,
# even though this set is not exhaustive of every possible human-required
# reason (classify_remediation's default already fails closed to
# human_required for anything not in SAFE_AUTO_REMEDIATIONS).
HUMAN_REQUIRED_REMEDIATIONS = frozenset({
    "oauth_refresh_token_invalid",
    "missing_provider_login",
    "destructive_git_worktree_conflict",
    "unknown_port_occupant",
    "requires_user_consent",
    # A Disabled Scheduled Task is deliberately NEVER auto-re-enabled --
    # see manager.session_center_supervisor.maintain_command_watcher()'s
    # own docstring for the P0 popup/focus-steal incident this precedent
    # closes: a task only becomes Disabled through some deliberate action
    # (Stop-ADM, the Tray, or a user disabling it directly), and there is
    # no reliable way to distinguish that from an "unexpected" disable
    # worth auto-recovering. manager.runtime_supervisor applies this same
    # policy to every Scheduled-Task-backed component, not only Command
    # Watcher.
    "scheduled_task_disabled",
})


def utc_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp if timestamp is not None else time.time(), tz=timezone.utc).isoformat()


def classify_remediation(reason: str | None) -> str:
    """"auto" only for an explicitly allow-listed reason; "human_required"
    for everything else, including None, an explicitly deny-listed reason,
    and any reason this module does not recognize at all. Never guesses
    "auto" for an unrecognized reason."""
    if reason in SAFE_AUTO_REMEDIATIONS:
        return "auto"
    return "human_required"


def evidence_store_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "health-evidence.json"


def _validate_component(component: str) -> None:
    if component not in COMPONENTS:
        raise ValueError(f"unknown health-evidence component: {component!r}")


def read_all(path: str | Path) -> dict:
    """Fail closed: any missing/malformed store means "nothing known yet",
    never an error -- this file is diagnostic visibility, not SSOT."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("components"), dict):
            return data
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return {"components": {}}


def read_component(path: str | Path, component: str) -> dict | None:
    _validate_component(component)
    return read_all(path)["components"].get(component)


def record(path: str | Path, component: str, *, state: str, checked_at: str | None = None,
           degraded_reason: str | None = None, last_remediation: str | None = None,
           remediation_result: str | None = None, unresolved_blocker: str | None = None,
           observed_pid: int | None = None, observed_port: int | None = None) -> dict:
    """Append-only per component: a past entry's original evidence is never
    mutated, only added to (bounded to MAX_EVIDENCE_HISTORY most-recent
    entries per component). `state` must reflect an actually observed
    check -- this function does not itself verify anything.
    """
    _validate_component(component)
    if state not in STATES:
        raise ValueError(f"unknown health-evidence state: {state!r}")
    checked_at = checked_at or utc_iso()
    entry = {
        "component": component,
        "state": state,
        "last_health_check": checked_at,
        "degraded_reason": degraded_reason,
        "last_remediation": last_remediation,
        "remediation_result": remediation_result,
        "remediation_classification": classify_remediation(last_remediation) if last_remediation else None,
        "unresolved_blocker": unresolved_blocker,
        "observed_pid": observed_pid,
        "observed_port": observed_port,
        "timestamp": checked_at,
    }

    data = read_all(path)
    components = data.setdefault("components", {})
    prior = components.get(component, {"history": []})
    history = (prior.get("history", []) + [entry])[-MAX_EVIDENCE_HISTORY:]
    components[component] = {"latest": entry, "history": history}
    write_atomic(Path(path), data)
    return entry


def session_center_evidence_from_supervisor(evidence: dict | None) -> dict:
    """Read-only normalization of manager.session_center_supervisor's own
    evidence file (see evidence_path_for/read_evidence there) into the
    shared schema. Never writes back to that file -- it remains that
    module's own SSOT for its own decisions."""
    evidence = evidence or {}
    degraded_reason = evidence.get("degraded_reason")
    state = "unknown" if evidence.get("last_health_check") is None else ("degraded" if degraded_reason else "healthy")
    return {
        "component": "session_center",
        "state": state,
        "last_health_check": evidence.get("last_health_check"),
        "degraded_reason": degraded_reason,
        "last_remediation": (evidence.get("last_remediation") or {}).get("event"),
        "remediation_result": evidence.get("recovery_result"),
        "unresolved_blocker": evidence.get("unresolved_blocker"),
        "observed_pid": None,
        "observed_port": None,
        "timestamp": evidence.get("last_health_check"),
    }


def task_health_evidence(component: str, health, *, last_remediation: str | None = None,
                          remediation_result: str | None = None) -> dict:
    """Read-only normalization of
    manager.dashboard_core.parse_scheduled_task_health's ViewModel for any
    Scheduled-Task-backed component (command_watcher, drive_dispatch_ingress,
    github_dispatch_ingress, session_center, quota-refresh's own task).
    `last_remediation`/`remediation_result` let a caller that has ALREADY
    attempted an auto-recovery action (see manager.runtime_supervisor)
    attach that outcome to the same evidence entry, rather than needing a
    second record() call -- both default to None (no remediation attempted
    this check), matching every existing adapter's own default posture."""
    _validate_component(component)
    state = {"Online": "healthy", "Offline": "degraded", "Unknown": "unknown"}.get(health.status_label, "unknown")
    return {
        "component": component,
        "state": state,
        "last_health_check": utc_iso(),
        "degraded_reason": None if state != "degraded" else health.detail,
        "last_remediation": last_remediation,
        "remediation_result": remediation_result,
        "unresolved_blocker": None if state != "degraded" else f"{health.name} Scheduled Task is disabled or not found",
        "observed_pid": None,
        "observed_port": None,
        "timestamp": utc_iso(),
    }


def command_watcher_evidence_from_task_health(health) -> dict:
    """Thin, backward-compatible wrapper of task_health_evidence() fixed to
    the "command_watcher" component -- kept as its own name since it
    predates task_health_evidence() and existing tests/callers use it."""
    return task_health_evidence("command_watcher", health)


def drive_evidence_from_read_attempt(reachable: bool, detail: str | None = None) -> dict:
    """Read-only normalization of a Drive connectivity probe the caller
    already performed (e.g. manager.quota_reader.read_drive_status
    succeeding or raising)."""
    return {
        "component": "drive",
        "state": "healthy" if reachable else "degraded",
        "last_health_check": utc_iso(),
        "degraded_reason": None if reachable else (detail or "Drive read failed"),
        "last_remediation": None,
        "remediation_result": None,
        "unresolved_blocker": None if reachable else "Drive is unreachable or credentials are invalid",
        "observed_pid": None,
        "observed_port": None,
        "timestamp": utc_iso(),
    }


def quota_evidence_from_summary(summary: dict) -> list[dict]:
    """Read-only normalization of manager.quota_reader.summarize()'s
    per-provider output. Stale (but otherwise present) telemetry is
    classified as auto-recoverable (recollect); a provider with no
    telemetry at all, or with a source that isn't official/confident, is
    reported degraded without asserting an auto-safe remediation -- this
    module does not know from staleness alone whether credentials are
    even valid."""
    results = []
    for provider in summary.get("providers", []):
        stale = provider.get("stale", True)
        reliable = provider.get("has_reliable_quota", False)
        if reliable:
            state, reason, remediation = "healthy", None, None
        elif stale:
            state, reason, remediation = "degraded", "stale_telemetry", "stale_telemetry_recollect"
        else:
            state, reason, remediation = "degraded", "quota_source_unreliable", None
        results.append({
            "component": "quota",
            "provider": provider.get("provider"),
            "state": state,
            "last_health_check": utc_iso(),
            "degraded_reason": reason,
            "last_remediation": remediation,
            "remediation_result": None,
            "unresolved_blocker": None if state == "healthy" else f"{provider.get('provider')} quota telemetry is not reliable",
            "observed_pid": None,
            "observed_port": None,
            "timestamp": utc_iso(),
        })
    return results


def _cli_record_dashboard(args: argparse.Namespace) -> dict:
    return record(
        args.store_path, "dashboard", state=args.state, degraded_reason=args.degraded_reason,
        last_remediation=args.last_remediation, remediation_result=args.remediation_result,
        unresolved_blocker=args.unresolved_blocker,
        observed_pid=args.observed_pid, observed_port=args.observed_port,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    record_dash = sub.add_parser("record-dashboard", help="Record an observed Dashboard health-check/remediation result")
    record_dash.add_argument("--store-path", required=True)
    record_dash.add_argument("--state", required=True, choices=STATES)
    record_dash.add_argument("--degraded-reason")
    record_dash.add_argument("--last-remediation")
    record_dash.add_argument("--remediation-result")
    record_dash.add_argument("--unresolved-blocker")
    record_dash.add_argument("--observed-pid", type=int)
    record_dash.add_argument("--observed-port", type=int)

    args = parser.parse_args(argv)
    try:
        if args.command == "record-dashboard":
            entry = _cli_record_dashboard(args)
        else:  # pragma: no cover -- argparse `required=True` already rejects this
            raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"state": "unknown", "error": str(exc)}), file=sys.stdout)
        return 1
    print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
