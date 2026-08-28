"""Live, real-process durability acceptance harness for the 5 core ADM
Scheduled Tasks (Command Watcher, Drive Dispatch Ingress, GitHub Dispatch
Ingress, Quota Refresh, Session Center Supervisor + its detached Session
Center child).

This is deliberately NOT a pytest suite: it drives the REAL production
Scheduled Tasks on the host it runs on (killing a verified, currently-live
child PID and observing whether the task's own natural next trigger
recovers cleanly), which is unsafe to run automatically in CI or on every
`pytest` invocation. It is a standalone, explicitly-invoked acceptance
script -- see the module docstring of each `cycle_*` function for exactly
what it does and does not touch.

Safety invariants, enforced throughout:
  - Never kill a PID by image name (`taskkill /IM ...`) -- only ever a
    single PID this script has independently verified (via WMI
    CommandLine) as the correct, currently-live child of the specific task
    under test. See `_verify_and_kill`.
  - Never disable a Scheduled Task -- `manager.session_center_supervisor
    .maintain_command_watcher` deliberately never auto-re-enables a
    Disabled task (see its own docstring), so testing "disable -> auto
    recovery" would only prove the (already-documented) absence of that
    behavior, at the cost of leaving a real production task off if this
    script were interrupted. Instead, this harness kills a live *running*
    instance mid-tick and verifies the task's own next natural trigger
    still produces a clean, fresh, non-blocked invocation -- the actual
    concern behind "does a crash wedge recovery".
  - Never calls manager.command_watcher.process_command() or any other
    write-capable dispatch/promotion function directly -- all "did it
    recover" evidence comes from read-only observation of
    runtime/scheduler-invocations/*.json (the same provenance trail
    manager.scheduler_provenance.start()/finish() already writes for every
    real invocation) plus live process/port/Scheduled-Task state.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


TASKS = {
    "command_watcher": {
        "task_name": "AI Development Manager - Command Watcher",
        "module_substr": "manager.command_watcher",
    },
    "drive_ingress": {
        "task_name": "AI Development Manager - Drive Dispatch Ingress",
        "module_substr": "manager.drive_dispatch_watcher",
    },
    "github_ingress": {
        "task_name": "AI Development Manager - GitHub Dispatch Ingress",
        "module_substr": "manager.github_dispatch_watcher",
    },
    "quota_refresh": {
        "task_name": "AI Development Manager - Quota Refresh",
        "module_substr": "refresh_status",
    },
    "session_center_supervisor": {
        "task_name": "AI Development Manager - Session Center Supervisor",
        "module_substr": "manager.session_center_supervisor",
    },
}

RECOVERY_TIMEOUT_SECONDS = 180
RECOVERY_TARGET_SECONDS = 60
POLL_INTERVAL_SECONDS = 3


def _run_ps(cmd: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout


def _wmi_processes() -> list[dict]:
    out = _run_ps(
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | ConvertTo-Json -Depth 3"
    )
    if not out.strip():
        return []
    data = json.loads(out)
    return [data] if isinstance(data, dict) else list(data)


def find_live_pid(module_substr: str) -> dict | None:
    """Read-only: find the current python.exe process (if any) whose
    command line names this task's module -- used only to positively
    identify a PID before this script ever kills it. Returns None if no
    matching process is currently running (the task is between ticks)."""
    for proc in _wmi_processes():
        cmdline = proc.get("CommandLine") or ""
        if module_substr in cmdline:
            return {"pid": int(proc["ProcessId"]), "cmdline": cmdline}
    return None


def _verify_and_kill(pid: int, module_substr: str) -> bool:
    """Re-verifies (WMI, not cached) that `pid` is STILL alive AND still
    carries `module_substr` in its own command line immediately before
    killing it -- closes the TOCTOU window between discovery and kill (the
    tick could have finished naturally in between). Kills by exact PID
    only, never by image name. Returns True if a kill was actually issued."""
    out = _run_ps(
        f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json"
    )
    if not out.strip():
        return False
    data = json.loads(out)
    cmdline = (data or {}).get("CommandLine") or ""
    if module_substr not in cmdline:
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    return True


def _scheduler_invocations_dir(manager_home: Path) -> Path:
    return manager_home / "runtime" / "scheduler-invocations"


def _read_invocations(manager_home: Path, task_name: str, since_mtime: float) -> list[dict]:
    directory = _scheduler_invocations_dir(manager_home)
    records = []
    for f in directory.glob("*.json"):
        try:
            if f.stat().st_mtime < since_mtime:
                continue
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("task_name") == task_name:
            doc["_mtime"] = f.stat().st_mtime
            doc["_file"] = f.name
            records.append(doc)
    records.sort(key=lambda r: r["_mtime"])
    return records


@dataclass
class CycleResult:
    component: str
    cycle: int
    killed_pid: int | None
    failure_detected_at: float
    recovery_started_at: float | None = None
    healthy_at: float | None = None
    recovery_seconds: float | None = None
    within_target: bool | None = None
    new_invocation_id: str | None = None
    duplicate_overlap_detected: bool = False
    status: str = "unknown"
    notes: str = field(default_factory=str)


def run_kill_recover_cycle(component: str, manager_home: Path, cycle_index: int,
                          wait_for_live_seconds: int = 90) -> CycleResult:
    cfg = TASKS[component]
    task_name, module_substr = cfg["task_name"], cfg["module_substr"]

    # 1. Wait for a live instance of this task to actually be mid-execution
    #    (Task Scheduler's own ~1min trigger means this is usually seconds
    #    away). We deliberately kill a REAL in-flight tick, not a synthetic
    #    one, so the recovery we observe is the real one.
    deadline = time.monotonic() + wait_for_live_seconds
    live = None
    while time.monotonic() < deadline:
        live = find_live_pid(module_substr)
        if live:
            break
        time.sleep(1)
    if not live:
        return CycleResult(component, cycle_index, None, time.time(), status="skipped",
                           notes=f"no live {component} instance observed within {wait_for_live_seconds}s")

    since = time.time()
    killed = _verify_and_kill(live["pid"], module_substr)
    failure_detected_at = time.time()
    if not killed:
        return CycleResult(component, cycle_index, live["pid"], failure_detected_at, status="skipped",
                           notes="instance exited naturally before kill could be verified (race, not a defect)")

    # 2. Poll runtime/scheduler-invocations for a FRESH record (a real new
    #    Scheduled Task trigger, not the one we just killed) reaching
    #    status=completed.
    result = CycleResult(component, cycle_index, live["pid"], failure_detected_at)
    poll_deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
    seen_ids = set()
    overlapping = []
    while time.monotonic() < poll_deadline:
        records = _read_invocations(manager_home, task_name, since - 5)
        fresh = [r for r in records if r.get("python_pid") != live["pid"]]
        if fresh and result.recovery_started_at is None:
            result.recovery_started_at = time.time()
        completed = [r for r in fresh if r.get("status") == "completed"]
        if completed:
            result.healthy_at = time.time()
            result.new_invocation_id = completed[-1].get("scheduler_invocation_id")
            result.status = "recovered"
            # Duplicate/overlap check: any two DIFFERENT invocation ids for
            # this task with overlapping [started_at, ended_at] windows
            # would mean two real concurrent executions -- the exact
            # "duplicate process" failure mode item H asks about.
            windows = []
            for r in records:
                try:
                    s = r.get("started_at")
                    e = r.get("ended_at") or r.get("finished_at")
                    if s and e:
                        windows.append((r["scheduler_invocation_id"], s, e))
                except Exception:
                    continue
            windows.sort(key=lambda w: w[1])
            for i in range(1, len(windows)):
                if windows[i][1] < windows[i - 1][2]:
                    overlapping.append((windows[i - 1][0], windows[i][0]))
            result.duplicate_overlap_detected = bool(overlapping)
            if overlapping:
                result.notes = f"overlap between {overlapping}"
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    if result.status != "recovered":
        result.status = "recovery_timeout"
        result.notes = f"no fresh completed invocation within {RECOVERY_TIMEOUT_SECONDS}s of kill"
        return result

    result.recovery_seconds = result.healthy_at - result.failure_detected_at
    result.within_target = result.recovery_seconds <= RECOVERY_TARGET_SECONDS
    return result


def run_session_center_child_cycle(manager_home: Path, cycle_index: int) -> CycleResult:
    """Session Center's own child (`manager.session_center`) is a detached,
    long-lived correlation HTTP server (see session_center.py's
    `server.serve_forever()`), not a per-tick process like the other 4
    tasks -- so its recovery signal is different: after killing it, the
    NEXT Session Center Supervisor tick must (a) notice the tracked pid is
    dead, (b) free port 8765 for a fresh bind, (c) spawn exactly one new
    child with a new pid/creation-identity, recorded in
    session_center_supervisor_state.json -- never two children racing for
    the same port.
    """
    state_path = manager_home / "session_center_supervisor_state.json"
    live = find_live_pid("manager.session_center ")
    if not live or "session_center_supervisor" in live["cmdline"]:
        # find_live_pid("manager.session_center ") intentionally has a
        # trailing space so it does not also match
        # "manager.session_center_supervisor"
        return CycleResult("session_center_child", cycle_index, None, time.time(), status="skipped",
                           notes="no live session_center child to kill this cycle")
    before_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    killed = _verify_and_kill(live["pid"], "manager.session_center ")
    failure_detected_at = time.time()
    if not killed:
        return CycleResult("session_center_child", cycle_index, live["pid"], failure_detected_at,
                           status="skipped", notes="exited naturally before verified kill (race)")

    result = CycleResult("session_center_child", cycle_index, live["pid"], failure_detected_at)
    deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if state_path.is_file():
            try:
                after_state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                after_state = None
            if after_state and after_state.get("pid") not in (None, before_state.get("pid"), live["pid"]):
                result.recovery_started_at = time.time()
                new_live = find_live_pid("manager.session_center ")
                if new_live and new_live["pid"] == after_state.get("pid"):
                    result.healthy_at = time.time()
                    result.status = "recovered"
                    result.new_invocation_id = str(after_state.get("pid"))
                    break
            elif after_state and after_state.get("execution_id") is None and before_state.get("execution_id") is not None:
                # legitimately cleared (no active command anymore) -- not a failure
                result.healthy_at = time.time()
                result.status = "recovered_idle"
                break
        time.sleep(POLL_INTERVAL_SECONDS)
    if result.status == "unknown":
        result.status = "recovery_timeout"
        result.notes = f"session_center_supervisor_state.json did not reflect a fresh child within {RECOVERY_TIMEOUT_SECONDS}s"
        return result
    if result.healthy_at:
        result.recovery_seconds = result.healthy_at - result.failure_detected_at
        result.within_target = result.recovery_seconds <= RECOVERY_TARGET_SECONDS
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-home", required=True)
    parser.add_argument("--components", nargs="+", default=list(TASKS.keys()) + ["session_center_child"])
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--wait-seconds", type=int, default=90,
                        help="how long to wait for a live in-flight instance to appear before giving up "
                             "(quota_refresh ~15min cadence and session_center_supervisor's own ~8min "
                             "effective cadence need this raised well above the 90s default)")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manager_home = Path(args.manager_home)
    results: list[CycleResult] = []
    for component in args.components:
        for cycle in range(1, args.cycles + 1):
            print(f"=== {component} cycle {cycle}/{args.cycles} ===", flush=True)
            if component == "session_center_child":
                r = run_session_center_child_cycle(manager_home, cycle)
            else:
                r = run_kill_recover_cycle(component, manager_home, cycle, wait_for_live_seconds=args.wait_seconds)
            print(json.dumps(r.__dict__, indent=2), flush=True)
            results.append(r)

    Path(args.output).write_text(
        json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
