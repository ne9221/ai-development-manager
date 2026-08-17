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
import json
import os
import subprocess
import time
from pathlib import Path

from manager.codex_launcher import process_creation_identity, process_identity_state
from manager.command_watcher import load_allowlist
from manager.refresh_status import RefreshError, runtime_lock, write_atomic
from manager.tasks import DriveRecords, TaskError

ACTIVE_STATUSES = ("queued", "claimed", "running")
PORT_RECHECK_ATTEMPTS = 10
PORT_RECHECK_INTERVAL_SECONDS = 0.2


def find_active_command(store, allowlist):
    """Deterministic: the most-recently-created in-scope, non-terminal
    command. An empty allowlist means nothing is ever followed."""
    candidates = []
    for project_id, task_id in sorted(allowlist):
        try:
            commands = store.list_records("commands", project_id)
        except TaskError:
            continue
        for record in commands:
            if record.get("task_id") == task_id and record.get("status") in ACTIVE_STATUSES:
                candidates.append(record)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c.get("created_at") or "")
    return candidates[-1]


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


def run_once(store, allowlist, state_path, python_exe, repo, port, wait_seconds, host="127.0.0.1"):
    """The entire read-decide-kill/spawn-write cycle for one invocation.
    Callers must hold the exclusive lock for the whole duration -- see main().
    """
    state = read_state(state_path)
    target = find_active_command(store, allowlist)
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
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--wait-seconds", type=float, default=1800.0)
    args = parser.parse_args(argv)

    try:
        from collectors.publish_drive import build_service
        store = DriveRecords(build_service())
    except Exception:
        print(json.dumps({"status": "unavailable"}))
        return 0

    allowlist = load_allowlist()
    try:
        with runtime_lock(lock_path_for(args.state_file)):
            result = run_once(store, allowlist, args.state_file, args.python_path,
                              args.repository_path, args.port, args.wait_seconds)
    except RefreshError:
        # Another invocation is already mid-cycle: no-op, spawn/kill/write
        # nothing. This is not a failure -- it is mutual exclusion working.
        result = {"status": "locked"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
