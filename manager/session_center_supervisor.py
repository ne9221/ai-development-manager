"""Keeps a Session Center process bound to whichever in-scope Command is
currently active, so the localhost UI can outlive any single hands-off
execution and any single ephemeral AI-tool process tree.

Deployed as a short, frequently-triggered Scheduled Task (like
command_watcher.py's --once), not as its own infinite loop: each invocation
checks the current target, (re)spawns Session Center if it changed or died,
records that decision in a small state file, and exits. This module never
touches Task/Execution/Session SSOT and never modifies correlation/terminal
state semantics -- it only decides which execution_id to hand to an
otherwise-unmodified `manager.session_center` process.
"""

import argparse
import ctypes
import json
import os
import subprocess

from manager.command_watcher import load_allowlist
from manager.tasks import DriveRecords, TaskError

ACTIVE_STATUSES = ("queued", "claimed", "running")


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
        pid, execution_id = data.get("pid"), data.get("execution_id")
        if isinstance(pid, int) and isinstance(execution_id, str):
            return pid, execution_id
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return None, None


def write_state(path, pid, execution_id):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"pid": pid, "execution_id": execution_id}, handle)


def process_alive(pid):
    if pid is None:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return exit_code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def decide(store, allowlist, state_pid, state_execution_id):
    """Pure decision, no side effects: (should_respawn, execution_id, project_id).

    should_respawn is False only when the currently-recorded execution_id
    still matches the current target AND that process is still alive.
    """
    target = find_active_command(store, allowlist)
    if target is None:
        return (state_execution_id is not None), None, None
    wanted = target_execution_id(target)
    if wanted == state_execution_id and process_alive(state_pid):
        return False, wanted, target["project_id"]
    return True, wanted, target["project_id"]


def spawn_session_center(python_exe, repo, project_id, execution_id, port, wait_seconds):
    """Start Session Center detached from this (short-lived) process so it
    survives after this invocation exits."""
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [python_exe, "-m", "manager.session_center",
         "--execution-project-id", project_id, "--execution-id", execution_id,
         "--wait-seconds", str(wait_seconds), "--port", str(port)],
        cwd=repo, creationflags=creationflags, close_fds=True,
    )


def kill(pid):
    if not process_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    else:
        os.kill(pid, 15)


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
    state_pid, state_execution_id = read_state(args.state_file)
    should_respawn, execution_id, project_id = decide(store, allowlist, state_pid, state_execution_id)
    if not should_respawn:
        print(json.dumps({"status": "unchanged", "execution_id": state_execution_id}))
        return 0

    kill(state_pid)
    if execution_id is None:
        write_state(args.state_file, None, None)
        print(json.dumps({"status": "idle"}))
        return 0

    process = spawn_session_center(args.python_path, args.repository_path, project_id, execution_id, args.port, args.wait_seconds)
    write_state(args.state_file, process.pid, execution_id)
    print(json.dumps({"status": "spawned", "execution_id": execution_id, "pid": process.pid}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
