"""Minimal, bounded, idempotent runtime self-heal for ADM's Scheduled-Task
components (Command Watcher, Drive Dispatch Ingress, GitHub Dispatch
Ingress, Session Center Supervisor, Quota Refresh).

Design constraints (see the P0 runtime self-heal brief this closes):

- Supervisor authority is independent of ordinary dispatch admission: it
  never calls process_command()/dispatch()/_promote_waiting_quota_task(),
  and a component being degraded has zero effect on whether it runs.
- It must survive any ONE component dying: rather than being its own
  Scheduled Task (a new always-on process is exactly what the brief asks
  us not to add without cause), `try_check_and_recover()` is called from
  the tail of every OTHER Scheduled-Task-backed component's own `main()`
  (command_watcher, drive_dispatch_watcher, github_dispatch_watcher,
  session_center_supervisor) -- as long as at least one of those four
  ticks (they run on independent ~60s Scheduled Tasks), the sweep below
  still runs and can recover the others, including re-enabling/starting
  a fifth, non-Python-hosted task (Quota Refresh) that never calls this
  module itself.
- Bounded and idempotent: `_should_run_sweep()` debounces the actual sweep
  to at most once per SWEEP_MIN_INTERVAL_SECONDS regardless of how many
  callers invoke it concurrently (cheap to call from all four; expensive
  to actually run from all four). `_cooldown_ok()` refuses to re-attempt
  recovery for the same component within RECOVERY_COOLDOWN_SECONDS, so a
  component stuck in a genuine crash loop is retried on a bounded cadence
  instead of thrashed. Recovery itself is a bare `schtasks /Run` (never
  `/Change /ENABLE` -- see the next point), which is safe to call on an
  already-running task: every one of these five Scheduled Tasks is
  configured MultipleInstances=IgnoreNew, so an extra trigger on a task
  already running is a documented no-op, not a duplicate execution.
- A DISABLED task is deliberately NEVER auto-re-enabled, for any of the
  five components -- only reported degraded/human_required. This mirrors
  manager.session_center_supervisor.maintain_command_watcher()'s own
  precedent (see its docstring): a task only becomes Disabled through some
  deliberate action (Stop-ADM, the Tray, a user disabling it directly),
  and silently re-enabling it is the exact P0 popup/focus-steal regression
  that function already closed once for Command Watcher specifically --
  `schtasks /Run` also happens to bypass a task's own Disabled flag and
  run it once anyway, so this module never calls recover_scheduled_task()
  at all when a task is observed Disabled, only when it is enabled but has
  stopped heartbeating.
- Never touches the Scheduled Task's own *registration* (install/create
  scripts under manager/install_*.ps1 or the desktop/ launcher layer) --
  only triggers a run on a task that already exists and is already
  enabled. If a task has been deleted entirely, `query_scheduled_task()`
  returns None and this module reports it "unknown", never fabricates
  "healthy".
- Liveness comes from TWO independent signals, combined conservatively
  (worse of the two wins): the Scheduled Task's own Enabled/Disabled state
  (manager.dashboard_core.parse_scheduled_task_health, via a real
  `schtasks /Query` -- catches "someone disabled it") and a per-component
  heartbeat (manager.scheduler_provenance.read_heartbeats -- catches "it's
  enabled but has stopped actually ticking", the failure mode a bare
  Enabled/Disabled check cannot see at all).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manager import health_evidence, scheduler_provenance
from manager.dashboard_core import ServiceHealthViewModel, parse_scheduled_task_health

SCHTASKS_TIMEOUT_SECONDS = 10
SWEEP_MIN_INTERVAL_SECONDS = 90
RECOVERY_COOLDOWN_SECONDS = 300
# Generous relative to the real observed ~45-90s tick cadence (including
# occasional multi-minute ticks while a real provider turn is in flight --
# see the Command Watcher "LastTaskResult stale while a real PID is alive"
# precedent) without being so long a genuinely dead component sits
# unnoticed for an unreasonable time.
HEARTBEAT_MAX_AGE_SECONDS = 300

# component -> real Windows Scheduled Task name. Fixed, hardcoded: this
# module only ever Enables/Runs a task that already exists under one of
# these exact names, never registers or renames one.
TASK_NAMES = {
    "command_watcher": "AI Development Manager - Command Watcher",
    "drive_dispatch_ingress": "AI Development Manager - Drive Dispatch Ingress",
    "github_dispatch_ingress": "AI Development Manager - GitHub Dispatch Ingress",
    "session_center_supervisor": "AI Development Manager - Session Center Supervisor",
    "quota_refresh": "AI Development Manager - Quota Refresh",
}
# The subset that writes its own manager.scheduler_provenance heartbeat --
# quota_refresh's own run_refresh.ps1 wrapper predates scheduler_provenance
# and is intentionally left alone here (out of scope for this slice); its
# liveness is instead inferred purely from the freshness of the quota data
# it produces (see check_quota()), which is what actually matters.
HEARTBEAT_COMPONENTS = ("command_watcher", "drive_dispatch_ingress", "github_dispatch_ingress",
                        "session_center_supervisor")
# health_evidence.py's fixed COMPONENTS vocabulary uses "session_center",
# not "session_center_supervisor" (the Scheduled Task's own name) -- this
# maps the latter (used above, and by TASK_NAMES/HEARTBEAT_COMPONENTS,
# since that is the literal heartbeat/task-name key) to the former (the
# evidence-store component key) at the one place they meet.
EVIDENCE_COMPONENT = {
    "command_watcher": "command_watcher",
    "drive_dispatch_ingress": "drive_dispatch_ingress",
    "github_dispatch_ingress": "github_dispatch_ingress",
    "session_center_supervisor": "session_center",
}


def _sweep_marker_path(manager_home):
    return Path(manager_home) / "runtime" / "supervisor-last-sweep.json"


def _cooldown_path(manager_home):
    return Path(manager_home) / "runtime" / "supervisor-remediation-state.json"


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _should_run_sweep(manager_home, now, min_interval_seconds=SWEEP_MIN_INTERVAL_SECONDS):
    """Debounce gate: only one caller (of potentially four concurrent ones)
    actually runs the real sweep per interval. Not a distributed lock --
    an occasional double-run from a rare race is harmless (every action
    downstream is itself idempotent), just wasted work, which is exactly
    the class of imperfection this module's docstring already accepts in
    exchange for needing no new always-on process or GCS lock."""
    path = _sweep_marker_path(manager_home)
    try:
        last = _parse_iso(json.loads(path.read_text(encoding="utf-8")).get("last_swept_at"))
    except (OSError, ValueError, AttributeError):
        last = None
    if last is not None and (now - last).total_seconds() < min_interval_seconds:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps({"last_swept_at": now.isoformat()}), encoding="utf-8")
        temp.replace(path)
    except OSError:
        pass
    return True


def _cooldown_ok(manager_home, component, now, cooldown_seconds=RECOVERY_COOLDOWN_SECONDS):
    path = _cooldown_path(manager_home)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    last = _parse_iso(state.get(component))
    return last is None or (now - last).total_seconds() >= cooldown_seconds


def _record_recovery_attempt(manager_home, component, now):
    path = _cooldown_path(manager_home)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state[component] = now.isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(state), encoding="utf-8")
        temp.replace(path)
    except OSError:
        pass


def query_scheduled_task(task_name, runner=subprocess.run):
    """Real `schtasks /Query` for one task, matching the exact stdout shape
    manager.dashboard_core.parse_scheduled_task_health() already parses.
    Returns None (never raises) on any failure -- missing schtasks.exe,
    timeout, nonzero exit, a task name that does not exist -- so the
    caller's own "found=False -> Unknown, never a false Offline/Healthy"
    contract is preserved."""
    try:
        result = runner(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
            capture_output=True, text=True, timeout=SCHTASKS_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 -- see recover_scheduled_task's own comment
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def recover_scheduled_task(task_name, runner=subprocess.run):
    """Bounded, idempotent recovery: trigger a run of a task that is
    already enabled but has stopped heartbeating. Deliberately never
    touches the Enabled/Disabled state (see module docstring) -- callers
    must never invoke this for a task observed Disabled, since
    `schtasks /Run` runs a Disabled task anyway, which would silently
    defeat whatever deliberately disabled it. Safe to call on an
    already-running task (MultipleInstances=IgnoreNew). Returns a short
    outcome string, never raises."""
    try:
        run = runner(["schtasks", "/Run", "/TN", task_name],
                     capture_output=True, text=True, timeout=SCHTASKS_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- an injected/custom runner can raise anything;
        # this module's whole contract is "never raise into the caller", so
        # ANY runner failure must degrade to a reported outcome, not a crash.
        return f"error:{type(exc).__name__}"
    if run.returncode != 0:
        return f"run_failed:{run.returncode}"
    return "attempted"


def _component_state(component, now, task_output, heartbeats):
    """Combine the two independent signals conservatively: a Disabled task
    always wins (that alone proves nothing is ticking), otherwise fall
    back to heartbeat freshness, otherwise Unknown (never guess healthy)."""
    task_name = TASK_NAMES[component]
    health = parse_scheduled_task_health(task_name, task_output)
    if health.found and health.status_label == "Offline":
        # Deliberately no remediation_reason -- see module docstring: a
        # Disabled task is never auto-recovered, only surfaced.
        return "degraded", "scheduled_task_disabled", None, health
    heartbeat = heartbeats.get(component)
    age_seconds = None
    if isinstance(heartbeat, dict):
        updated = _parse_iso(heartbeat.get("updated_at"))
        if updated is not None:
            age_seconds = (now - updated).total_seconds()
    if age_seconds is not None and age_seconds <= HEARTBEAT_MAX_AGE_SECONDS:
        return "healthy", None, None, health
    if age_seconds is not None:
        return "degraded", "heartbeat_stale", "scheduled_task_heartbeat_stale_restart", health
    if not health.found:
        return "unknown", "no_heartbeat_and_task_query_failed", None, health
    # Task exists/enabled but no heartbeat has ever been observed (e.g.
    # freshly installed, or a Python-side crash before scheduler_provenance
    # ever wrote one) -- real signal, but not proof of failure the way a
    # stale-but-once-fresh heartbeat is; treat as unknown, not degraded.
    return "unknown", "no_heartbeat_observed_yet", None, health


def check_heartbeat_component(manager_home, component, now, task_output=None, heartbeats=None, runner=subprocess.run):
    task_name = TASK_NAMES[component]
    if task_output is None:
        task_output = query_scheduled_task(task_name, runner=runner)
    if heartbeats is None:
        heartbeats = scheduler_provenance.read_heartbeats(manager_home)
    state, reason, remediation_reason, health = _component_state(component, now, task_output, heartbeats)
    return {"component": component, "task_name": task_name, "state": state, "degraded_reason": reason,
            "remediation_reason": remediation_reason, "health": health}


def check_quota(*, service_factory=None, max_age_minutes=60):
    """Quota freshness, reusing manager.quota_reader's own existing
    read/summarize logic unmodified -- this is the exact same
    generated_at-driven staleness computation the Dashboard's own Quota
    Center already trusts, not a reinvented threshold. Best-effort: a
    Drive read failure is reported as the `drive` component degraded and
    quota is left unassessed for this sweep (never inferred healthy or
    unhealthy from an unrelated read failure)."""
    from collectors.publish_drive import build_service
    from manager.quota_reader import QuotaReaderError, read_drive_status, summarize
    try:
        service = (service_factory or build_service)()
        document = read_drive_status(service=service)
    except QuotaReaderError as exc:
        return {"drive_reachable": False, "detail": str(exc), "providers": None}
    except Exception as exc:  # pragma: no cover -- defensive, mirrors read_drive_status's own catch-all
        return {"drive_reachable": False, "detail": f"{type(exc).__name__}: {exc}", "providers": None}
    summary = summarize(document, max_age_minutes=max_age_minutes)
    return {"drive_reachable": True, "detail": None, "providers": summary.get("providers", [])}


def check_and_recover(manager_home, *, now=None, runner=subprocess.run, service_factory=None, dry_run=False):
    """Run one full sweep: evaluate every heartbeat-backed component plus
    quota freshness, and attempt bounded/idempotent recovery for anything
    degraded (subject to per-component cooldown). Always records evidence
    via manager.health_evidence, whether or not anything was wrong, so a
    Dashboard reading health-evidence.json sees continuous coverage, not
    only degraded blips. Returns a summary dict; never raises (every
    sub-check is independently best-effort so one failure cannot mask or
    abort evaluation of the rest)."""
    now = _now(now)
    store_path = health_evidence.evidence_store_path(manager_home)
    heartbeats = scheduler_provenance.read_heartbeats(manager_home)
    results = {}

    for component in HEARTBEAT_COMPONENTS:
        try:
            checked = check_heartbeat_component(manager_home, component, now, heartbeats=heartbeats, runner=runner)
        except Exception as exc:  # pragma: no cover -- defensive
            results[component] = {"state": "unknown", "error": str(exc)}
            continue
        remediation_result = None
        if checked["state"] == "degraded" and checked["remediation_reason"] and not dry_run:
            if _cooldown_ok(manager_home, component, now):
                remediation_result = recover_scheduled_task(checked["task_name"], runner=runner)
                _record_recovery_attempt(manager_home, component, now)
            else:
                remediation_result = "skipped_cooldown"
        evidence_component = EVIDENCE_COMPONENT[component]
        if checked["degraded_reason"] == "scheduled_task_disabled":
            unresolved_blocker = f"{checked['task_name']} is Disabled -- restart via the Tray, not auto-recovered"
        elif checked["state"] == "unknown":
            unresolved_blocker = checked["health"].detail
        else:
            unresolved_blocker = None
        try:
            health_evidence.record(
                store_path, evidence_component, state=checked["state"],
                degraded_reason=checked["degraded_reason"],
                last_remediation=checked["degraded_reason"] if checked["degraded_reason"] == "scheduled_task_disabled"
                else (checked["remediation_reason"] if remediation_result else None),
                remediation_result=remediation_result,
                unresolved_blocker=unresolved_blocker,
            )
        except (OSError, ValueError):  # pragma: no cover -- evidence write is best-effort
            pass
        results[component] = {**checked, "remediation_result": remediation_result}

    try:
        quota = check_quota(service_factory=service_factory)
    except Exception as exc:  # pragma: no cover -- defensive
        quota = {"drive_reachable": False, "detail": str(exc), "providers": None}
    if not quota["drive_reachable"]:
        try:
            health_evidence.record(store_path, "drive", state="degraded", degraded_reason=quota["detail"],
                                   unresolved_blocker="Drive is unreachable or credentials are invalid")
        except (OSError, ValueError):
            pass
        results["quota"] = {"state": "unknown", "reason": "drive_unreachable"}
    else:
        entries = health_evidence.quota_evidence_from_summary({"providers": quota["providers"]})
        any_stale = any(entry["last_remediation"] == "stale_telemetry_recollect" for entry in entries)
        remediation_result = None
        # Same "never auto-recover a Disabled task" policy as every other
        # component (see module docstring) -- staleness alone does not
        # justify a bare `/Run`, which would bypass a deliberate Disable.
        # Defensive like the heartbeat-component loop above: a schtasks
        # failure here must not blow up the whole sweep and discard the
        # heartbeat-component results already computed above it.
        try:
            quota_task_health = parse_scheduled_task_health(
                TASK_NAMES["quota_refresh"], query_scheduled_task(TASK_NAMES["quota_refresh"], runner=runner))
        except Exception:  # pragma: no cover -- defensive
            quota_task_health = ServiceHealthViewModel(name=TASK_NAMES["quota_refresh"], found=False,
                                                       detail="query failed", status_label="Unknown")
        quota_task_disabled = quota_task_health.found and quota_task_health.status_label == "Offline"
        if any_stale and quota_task_disabled:
            any_stale = False  # surfaced via unresolved_blocker below instead of a remediation attempt
        if any_stale and not dry_run:
            if _cooldown_ok(manager_home, "quota_refresh", now):
                remediation_result = recover_scheduled_task(TASK_NAMES["quota_refresh"], runner=runner)
                _record_recovery_attempt(manager_home, "quota_refresh", now)
            else:
                remediation_result = "skipped_cooldown"
        worst = next((entry for entry in entries if entry["state"] != "healthy"), entries[0] if entries else None)
        if worst is not None:
            if quota_task_disabled and worst["degraded_reason"] == "stale_telemetry":
                last_remediation = "scheduled_task_disabled"
                unresolved_blocker = f"{TASK_NAMES['quota_refresh']} is Disabled -- restart via the Tray, not auto-recovered"
            else:
                last_remediation = worst["last_remediation"] if remediation_result else None
                unresolved_blocker = worst["unresolved_blocker"]
            try:
                health_evidence.record(
                    store_path, "quota", state=worst["state"], degraded_reason=worst["degraded_reason"],
                    last_remediation=last_remediation,
                    remediation_result=remediation_result, unresolved_blocker=unresolved_blocker,
                )
            except (OSError, ValueError):
                pass
        results["quota"] = {"state": worst["state"] if worst else "unknown", "remediation_result": remediation_result,
                            "providers": quota["providers"]}

    return results


def try_check_and_recover(manager_home, *, now=None, runner=subprocess.run, service_factory=None):
    """The one entry point every Scheduled-Task-backed component's own
    main() should call, right before returning -- never before its own
    real work, and always wrapped so a supervisor failure can never affect
    that component's own exit status. No-ops (returns None) most calls,
    since `_should_run_sweep()` debounces the real work to roughly once
    per SWEEP_MIN_INTERVAL_SECONDS regardless of how many of the four
    components call this in the same window."""
    now = _now(now)
    try:
        if not _should_run_sweep(manager_home, now):
            return None
        return check_and_recover(manager_home, now=now, runner=runner, service_factory=service_factory)
    except Exception:  # pragma: no cover -- defensive: never let self-heal break the real caller
        return None


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-home", required=True)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--force", action="store_true", help="bypass the sweep debounce for a manual/test run")
    parser.add_argument("--dry-run", action="store_true", help="evaluate and record evidence but never recover")
    args = parser.parse_args(argv)
    now = _now()
    if not args.force and not _should_run_sweep(args.manager_home, now):
        print(json.dumps({"status": "skipped_debounce"}))
        return 0
    results = check_and_recover(args.manager_home, now=now, dry_run=args.dry_run)
    print(json.dumps(results, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
