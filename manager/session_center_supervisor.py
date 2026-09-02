"""Keeps a Session Center process bound to whichever in-scope Command is
currently active, so the localhost UI can outlive any single hands-off
execution and any single ephemeral AI-tool process tree.

Deployed as a short, frequently-triggered Scheduled Task (like
command_watcher.py's --once), not as its own infinite loop: each invocation
acquires an exclusive cross-process lock, reads state, decides, kills/spawns
as needed, writes state, and exits -- all inside that one lock. This module
never touches Task/Execution/Session SSOT and never modifies correlation or
terminal-state semantics; it only decides which execution_id to hand to an
otherwise-unmodified `manager.session_center` process.

Concurrency/crash/PID-reuse safety (adversarial review findings):
- The whole read-decide-kill/spawn-write critical section runs inside
  refresh_status.runtime_lock(); a second overlapping invocation that can't
  acquire the lock fails cleanly and touches nothing (see run_once()).
- State is written with refresh_status.write_atomic() (temp file +
  os.replace()) so a reader never observes a partially-written file.
- Liveness is verified with codex_launcher.process_identity_state(), the
  same PID-reuse-safe check Recovery uses -- never bare PID existence.
- A pid is only ever killed after being positively verified ("live") as the
  process this supervisor itself recorded. An unreadable identity or a
  mismatched one is never treated as "safe to kill" or "safe to treat as
  healthy" -- both fail closed into "don't know, re-establish carefully."
- Before spawning, the target port is probed. If it is occupied and not
  explained by a pid this run just verified-and-killed, spawning is refused
  (fail closed) rather than guessing who holds the port.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import time
from pathlib import Path

from manager.codex_launcher import process_creation_identity, process_identity_state
from manager.command_watcher import (
    POLL_TIME_BUDGET_SECONDS,
    RECENT_COMMAND_SWEEP_BUDGET_SECONDS,
    WATCHER_DISCOVERY_TIMEOUT_SECONDS,
    _enumerate_commands,
    _enumerate_project_ids,
    _enumerate_recent_commands,
    _rotated_project_ids,
    load_allowlist,
    queued_command_pending_only_health,
)
from manager.dispatch_requests import dispatch_request_registry
from manager.refresh_status import RefreshError, runtime_lock, write_atomic
from manager.tasks import DriveRecords, TaskError
from manager.trusted_ingress import verify_trusted_ingress_admission
from manager.production_guard import RuntimeGuardError, require_runtime_guard

# A queued Command has no provider process or Execution to correlate. Following
# one lets an abandoned historical queue item occupy :8765 indefinitely.
ACTIVE_STATUSES = ("claimed", "running")
PORT_RECHECK_ATTEMPTS = 10
PORT_RECHECK_INTERVAL_SECONDS = 0.2
WATCHER_TASK_NAME = "AI Development Manager - Command Watcher"


def _powershell_json(script):
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def query_command_watcher_task():
    """Read only the one approved root task; no wildcard task discovery."""
    quoted = WATCHER_TASK_NAME.replace("'", "''")
    script = (
        f"$t=Get-ScheduledTask -TaskName '{quoted}' -ErrorAction Stop; "
        "$a=@($t.Actions|ForEach-Object{[pscustomobject]@{execute=$_.Execute;arguments=$_.Arguments}}); "
        "[pscustomobject]@{task_name=$t.TaskName;task_path=$t.TaskPath;state=[string]$t.State;enabled=[bool]$t.Settings.Enabled;actions=$a} "
        "| ConvertTo-Json -Depth 5 -Compress"
    )
    return _powershell_json(script)


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _approved_watcher_identity(task, repository_path):
    if task.get("task_name") != WATCHER_TASK_NAME or task.get("task_path") != "\\":
        return False
    actions = task.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        return False
    action = actions[0]
    executable = os.path.basename(action.get("execute") or "").lower()
    arguments = action.get("arguments") or ""
    if executable == "wscript.exe":
        # Hidden-launch installers (see AdmHiddenLaunch.ps1) route through a
        # generated per-repository VBS wrapper instead of invoking
        # powershell.exe directly, so identity is proven by that wrapper's
        # path living exactly under this repository -- not by inspecting
        # -RepositoryPath, which no longer appears at the Task Action level.
        expected_vbs = str(Path(repository_path) / "manager" / "generated" / "command-watcher.vbs")
        return arguments.strip() in (f'"{expected_vbs}"', f"'{expected_vbs}'")
    if executable != "powershell.exe":
        return False
    match = re.search(r"-RepositoryPath\s+(['\"])(.*?)\1", arguments, re.IGNORECASE)
    if not match or not _same_path(match.group(2), repository_path):
        return False
    runner = str(Path(repository_path) / "manager" / "run_command_watcher.ps1")
    return any(quoted in arguments for quoted in (f"'{runner}'", f'"{runner}"'))


def maintain_command_watcher(repository_path, maintenance_path, incident_path,
                             query_task=query_command_watcher_task,
                             now=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")):
    """Report the Command Watcher task's health -- never re-enables it.

    A Scheduled Task only ever becomes Disabled through some deliberate
    action (Stop-ADM, the Tray, or a user disabling it directly in Task
    Scheduler); there is no reliable way to distinguish that from an
    "unexpected" disable worth auto-recovering, so this must never guess.
    Silently re-enabling a task the user just disabled is exactly the P0
    HOME popup/focus-steal bug this closes: every automatic re-enable
    unblocks the next Scheduled Task trigger, which runs again and can pop a
    console. Recovery from a genuine external disable is an explicit user
    action (Tray "Start Services" / "Restart Services"), never a silent
    per-tick side effect of this supervisor.
    """
    sentinel = Path(maintenance_path).is_file()
    try:
        task = query_task()
    except Exception:
        task = None
    previous_state = task.get("state") if isinstance(task, dict) else "Unavailable"
    evidence = {
        "detected_at": now(), "previous_state": previous_state,
        "sentinel": sentinel, "attempted": False, "result": "healthy",
    }
    if not task or not _approved_watcher_identity(task, repository_path):
        evidence["result"] = "identity_rejected"
    elif task.get("enabled") is not False:
        return evidence
    else:
        evidence["result"] = "intentional_maintenance" if sentinel else "disabled_left_alone"
    write_atomic(Path(incident_path), evidence)
    return evidence


def find_active_command(store, allowlist, bucket=None, ingress_registry_factory=dispatch_request_registry):
    """Deterministic: the most-recently-created in-scope, non-terminal
    command. An empty allowlist and no bucket means nothing is ever
    followed.

    In-scope has two independent routes, matching manager.command_watcher's
    own admission model exactly so a command that Command Watcher will
    launch is never invisible to the Session Center UI:

    1. The existing static ADM_WATCHER_ALLOWLIST_PATH allowlist -- entirely
       unchanged from before this parameter existed.
    2. A command admitted under the v1 trusted-ingress contract (see
       manager.trusted_ingress.verify_trusted_ingress_admission, the exact
       same fail-closed check Command Watcher itself uses -- never
       duplicated or re-implemented here). Only evaluated when `bucket` is
       given; a command is never assumed in-scope just for being
       queued/claimed/running.

    Cold-start fallback: Command Watcher itself refuses to claim a `queued`
    Command while Session Center is unhealthy (see
    command_watcher.session_center_healthy) -- so if this function only ever
    reacted to claimed/running Commands, a cold Session Center and a
    legitimately admissible queued Command would deadlock forever (neither
    side ever moves first). When the scan above finds no claimed/running
    candidate at all, a `queued` Command is used instead, but only one that
    already independently clears process_command's own governance/
    trusted-ingress/policy gate (queued_command_pending_only_health, the
    same two admission routes as above, replayed read-only) -- this can
    never turn an ordinary not-yet-admitted queued Command into an active
    one, and it never takes priority over a real claimed/running Command.

    Bucket-route discovery order mirrors command_watcher.poll_once()'s own
    two-phase sweep (see _enumerate_recent_commands's docstring): a cheap,
    modifiedTime-ordered "recent" batch across every rotated project first,
    then the full bounded historical sweep as the recovery path for
    anything older. Without the recent-first pass, a project with a large
    historical Command backlog can bury a Command created/claimed only
    moments ago outside that project's bounded historical window for as
    long as its rotation excludes it -- live-reproduced during Gate 3
    cold-start activation (2026-08-30): a project's historical sweep
    returned 36 Commands from days earlier and never reached one queued
    minutes before. Classification (admission route, ACTIVE_STATUSES vs.
    eligible-queued) is identical in both phases; a record seen in both is
    harmless -- pool/sort/pick-latest already tolerates duplicates.
    """
    candidates = []
    queued_candidates = []
    deadline = time.monotonic() + POLL_TIME_BUDGET_SECONDS

    def classify_bucket_record(project_id, record):
        if (project_id, record.get("task_id")) in allowlist:
            return  # already scanned via the static path above
        status = record.get("status")
        if status in ACTIVE_STATUSES:
            if verify_trusted_ingress_admission(store, record, bucket, ingress_registry_factory) is not None:
                candidates.append(record)
        elif status == "queued" and queued_command_pending_only_health(
                store, record, allowlist, bucket, ingress_registry_factory):
            queued_candidates.append(record)

    # Bucket-route recent-sweep runs FIRST, before either historical scan
    # below -- deliberately, not incidentally. The static allowlist loop's
    # own per-entry historical fetch can itself be expensive for a project
    # with a large backlog (it hydrates as much as the shared `deadline`
    # allows, looking for just one task_id); if it ran first, it could
    # consume nearly the whole budget before the recent-sweep -- what the
    # cold-start fallback actually depends on -- ever got a chance to run
    # at all. Live HOME reproduction (Gate 3 activation, 2026-08-30): the
    # allowlist's own 'ai-development-manager' entry's historical fetch
    # alone took 30 of the 40s shared budget, so the recent-sweep that ran
    # afterward had almost nothing left and returned empty for that exact
    # project -- find_active_command() returned None on multiple
    # consecutive real ticks as a direct result.
    bucket_project_ids = []
    if bucket:
        try:
            bucket_project_ids = _rotated_project_ids(_enumerate_project_ids(store, deadline=deadline))
        except TaskError:
            bucket_project_ids = []
        recent_deadline = min(deadline, time.monotonic() + RECENT_COMMAND_SWEEP_BUDGET_SECONDS)
        for project_id in bucket_project_ids:
            if time.monotonic() >= recent_deadline:
                break
            try:
                commands = _enumerate_recent_commands(store, project_id, deadline=recent_deadline)
            except TaskError:
                continue
            for record in commands:
                classify_bucket_record(project_id, record)

    for project_id, task_id in sorted(allowlist):
        try:
            commands = _enumerate_commands(store, project_id, deadline=deadline)
        except TaskError:
            continue
        for record in commands:
            if record.get("task_id") != task_id:
                continue
            if record.get("status") in ACTIVE_STATUSES:
                candidates.append(record)
            elif record.get("status") == "queued" and queued_command_pending_only_health(
                    store, record, allowlist, bucket, ingress_registry_factory):
                queued_candidates.append(record)

    if bucket:
        for project_id in bucket_project_ids:
            if time.monotonic() >= deadline:
                break
            try:
                commands = _enumerate_commands(store, project_id, deadline=deadline)
            except TaskError:
                continue
            for record in commands:
                classify_bucket_record(project_id, record)
    pool = candidates or queued_candidates
    if not pool:
        return None
    pool.sort(key=lambda c: c.get("created_at") or "")
    return pool[-1]


def target_execution_id(command):
    return command.get("execution_id") or f"command-{command['command_id']}"


def read_state(path):
    """Fail closed: any missing/malformed state file means "nothing known"."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        pid, execution_id, identity = data.get("pid"), data.get("execution_id"), data.get("creation_identity")
        if isinstance(pid, int) and isinstance(execution_id, str) and isinstance(identity, str) and identity:
            return pid, execution_id, identity
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return None, None, None


def write_state(path, pid, execution_id, creation_identity):
    """Atomic: readers only ever see a complete old state or a complete new
    one, never a partial write, even if this process is killed mid-write."""
    write_atomic(Path(path), {"pid": pid, "execution_id": execution_id, "creation_identity": creation_identity})


def lock_path_for(state_path):
    return Path(str(state_path) + ".lock")


def decide(state, target):
    """Pure decision, no side effects.

    Returns a dict:
      {"action": "noop"}
      {"action": "clear", "kill_pid": pid_or_None}
      {"action": "respawn", "execution_id": ..., "project_id": ..., "kill_pid": pid_or_None}

    kill_pid is populated only when that pid was positively verified
    ("live") as the process this supervisor itself recorded -- never for a
    pid that could only be confirmed "unknown" or "replaced".
    """
    state_pid, state_execution_id, state_identity = state
    verified_alive = state_pid is not None and process_identity_state(state_pid, state_identity) == "live"

    if target is None:
        if state_execution_id is None:
            return {"action": "noop"}
        return {"action": "clear", "kill_pid": state_pid if verified_alive else None}

    wanted = target_execution_id(target)
    if wanted == state_execution_id and verified_alive:
        return {"action": "noop"}

    kill_pid = state_pid if (state_execution_id is not None and verified_alive) else None
    return {"action": "respawn", "execution_id": wanted, "project_id": target["project_id"],
            "provider": target.get("provider", "codex"), "kill_pid": kill_pid}


def port_available(host, port):
    """True only if nothing is currently listening -- a real bind probe,
    not a reuse-hack, so it reflects genuine occupancy on both platforms."""
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def kill(pid):
    """Terminate a pid this run has already positively verified as its own,
    live child (see decide()'s kill_pid). Already-gone is not an error --
    both branches below tolerate a pid that exited in the meantime."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def spawn_session_center(python_exe, repo, project_id, execution_id, port, wait_seconds, provider="codex"):
    """Start Session Center detached from this (short-lived) process so it
    survives after this invocation exits.

    provider is deterministically known from the target Command already --
    no Drive/filesystem read needed -- so it can be shown in the
    PendingCorrelation view immediately, before the Execution's
    provider_session_id is ever observed."""
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [python_exe, "-m", "manager.session_center",
         "--execution-project-id", project_id, "--execution-id", execution_id,
         "--wait-seconds", str(wait_seconds), "--port", str(port), "--provider", provider],
        cwd=repo, creationflags=creationflags, close_fds=True,
    )


def run_once(store, allowlist, state_path, python_exe, repo, port, wait_seconds, host="127.0.0.1",
            bucket=None, ingress_registry_factory=dispatch_request_registry):
    """The entire read-decide-kill/spawn-write cycle for one invocation.
    Callers must hold the exclusive lock for the whole duration -- see main().
    """
    require_runtime_guard(repo)
    state = read_state(state_path)
    target = find_active_command(store, allowlist, bucket=bucket, ingress_registry_factory=ingress_registry_factory)
    decision = decide(state, target)

    if decision["action"] == "noop":
        return {"status": "unchanged", "execution_id": state[1]}

    if decision.get("kill_pid"):
        kill(decision["kill_pid"])
        # Give the OS a bounded chance to release the port before we probe
        # it -- only done here, right after a verified kill of our own
        # tracked child; never elsewhere, where there is nothing to wait for.
        for _ in range(PORT_RECHECK_ATTEMPTS):
            if port_available(host, port):
                break
            time.sleep(PORT_RECHECK_INTERVAL_SECONDS)

    if decision["action"] == "clear":
        write_state(state_path, None, None, None)
        return {"status": "idle"}

    if not port_available(host, port):
        # Something is listening that this run did not just verify-and-kill
        # as its own. Never spawn a second listener, never guess who it is.
        return {"status": "attention", "reason": "port_occupied_unverified"}

    process = spawn_session_center(python_exe, repo, decision["project_id"], decision["execution_id"], port, wait_seconds,
                                   provider=decision.get("provider", "codex"))
    identity = process_creation_identity(process.pid)
    write_state(state_path, process.pid, decision["execution_id"], identity)
    return {"status": "spawned", "execution_id": decision["execution_id"], "pid": process.pid}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-path", required=True)
    parser.add_argument("--repository-path", required=True)
    parser.add_argument("--manager-home", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--wait-seconds", type=float, default=1800.0)
    args = parser.parse_args(argv)
    try:
        require_runtime_guard(args.repository_path, args.manager_home)
    except RuntimeGuardError as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code}))
        return 1

    from manager.scheduler_provenance import finish, start
    # --manager-home is a required argument: use it rather than re-reading
    # the environment, whose old default was "." (the working directory).
    manager_home = args.manager_home
    invocation = start(manager_home, "session_center_supervisor")
    try:
        with runtime_lock(lock_path_for(args.state_file)):
            runtime = Path(args.manager_home) / "runtime"
            watcher = maintain_command_watcher(
                args.repository_path,
                runtime / "watcher-maintenance.json",
                runtime / "watcher-self-heal.json",
            )
            try:
                from collectors.publish_drive import build_service
                store = DriveRecords(build_service(timeout=WATCHER_DISCOVERY_TIMEOUT_SECONDS))
            except Exception:
                result = {"status": "unavailable"}
            else:
                result = run_once(store, load_allowlist(), args.state_file, args.python_path,
                                  args.repository_path, args.port, args.wait_seconds,
                                  bucket=os.environ.get("ADM_LOCK_GCS_BUCKET"))
            result["watcher_maintenance"] = watcher
    except RefreshError:
        # Another invocation is already mid-cycle: no-op, spawn/kill/write
        # nothing. This is not a failure -- it is mutual exclusion working.
        result = {"status": "locked"}
    print(json.dumps(result))
    finish(manager_home, invocation,
           "failed" if result.get("status") == "unavailable" else "completed")
    from manager.runtime_supervisor import try_check_and_recover
    try_check_and_recover(manager_home)
    return 0


if __name__ == "__main__":
    from manager.win_background_guard import install_hidden_subprocess_guard
    install_hidden_subprocess_guard()
    raise SystemExit(main())
