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

Orphan-process recovery and correlation re-observe (P1-G self-heal):
- If the target port is occupied by a pid this supervisor does not itself
  track, it is never assumed safe to kill just because a fresh spawn wants
  that port. attempt_orphan_recovery() only treats it as a stale ADM
  orphan after positively verifying (verify_adm_session_center_ownership)
  that its actual OS command line is unmistakably a
  `python ... -m manager.session_center` invocation bound to this exact
  port, with this module's own required identity arguments present. Any
  ambiguity -- unreadable command line, wrong module, wrong port, missing
  identity args -- fails closed exactly like the pre-existing
  port_occupied_unverified behavior; nothing is ever killed on a guess.
- A live, correctly-targeted manager.session_center process is no longer
  "healthy" from this supervisor's point of view merely because its OS
  process is alive: decide() also probes its own reported /api/session
  current_state (read_session_center_state). A permanent
  "correlation_failed" is treated as a degraded, recoverable state, not a
  dead end -- the process is respawned for the *same* execution_id so it
  gets a fresh, bounded correlation attempt against whatever canonical
  Execution/provider-session evidence exists by then.
- Both of the above only ever restart the passive, read-only
  manager.session_center observer. Neither ever dispatches a Command,
  writes Task/Execution/Session SSOT, or launches a second provider
  session -- that would require code this module does not have and does
  not call.
- A JSON evidence log (evidence_path_for(state_path), append-only,
  bounded) separately records last health check / last remediation /
  degraded reason / recovery result / unresolved blocker for operator
  visibility. It is diagnostic only: decide() never reads it back, so a
  missing or corrupt evidence file can never change what this supervisor
  does, only what it shows. A degradation's original failure
  timestamp/reason is written once and never overwritten; a later
  recovery is always a new, separate history entry.
"""

import argparse
import json
import os
import shlex
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from manager.codex_launcher import process_creation_identity, process_identity_state
from manager.command_watcher import load_allowlist
from manager.dispatch_requests import dispatch_request_registry
from manager.refresh_status import RefreshError, runtime_lock, write_atomic
from manager.runtime_bridge import all_projects
from manager.tasks import DriveRecords, TaskError
from manager.trusted_ingress import verify_trusted_ingress_admission

ACTIVE_STATUSES = ("queued", "claimed", "running")
PORT_RECHECK_ATTEMPTS = 10
PORT_RECHECK_INTERVAL_SECONDS = 0.2
MAX_EVIDENCE_HISTORY = 50


def utc_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp if timestamp is not None else time.time(), tz=timezone.utc).isoformat()


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
    """
    candidates = []
    for project_id, task_id in sorted(allowlist):
        try:
            commands = store.list_records("commands", project_id)
        except TaskError:
            continue
        for record in commands:
            if record.get("task_id") == task_id and record.get("status") in ACTIVE_STATUSES:
                candidates.append(record)
    if bucket:
        for project in all_projects(store):
            project_id = project["project_id"]
            try:
                commands = store.list_records("commands", project_id)
            except TaskError:
                continue
            for record in commands:
                if record.get("status") not in ACTIVE_STATUSES:
                    continue
                if (project_id, record.get("task_id")) in allowlist:
                    continue  # already a candidate via the static path above
                if verify_trusted_ingress_admission(store, record, bucket, ingress_registry_factory) is not None:
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


def decide(state, target, session_state=None):
    """Pure decision, no side effects.

    Returns a dict:
      {"action": "noop"}
      {"action": "clear", "kill_pid": pid_or_None}
      {"action": "respawn", "execution_id": ..., "project_id": ..., "kill_pid": pid_or_None}

    This is the exact pre-existing return shape (callers/tests that already
    depend on it are unaffected); a "respawn" caused by a live, correctly-
    targeted process reporting correlation_failed is distinguished from an
    ordinary target-change/not-alive respawn by the caller (run_once),
    which already has session_state and state in scope -- decide() does not
    need to re-express that distinction in its own return contract.

    kill_pid is populated only when that pid was positively verified
    ("live") as the process this supervisor itself recorded -- never for a
    pid that could only be confirmed "unknown" or "replaced".

    session_state is the currently-tracked process's own /api/session
    snapshot (see read_session_center_state), or None when it could not be
    read. A live, correctly-targeted process whose own reported
    current_state is "correlation_failed" is no longer treated as "noop" --
    correlation_failed must be observable and recoverable rather than a
    permanent dead end (see module docstring's re-observe contract). This
    never re-dispatches a Command or creates a second provider session: the
    respawned manager.session_center process is a passive, read-only
    observer of the exact same execution_id, retrying its own correlation
    read against whatever canonical evidence exists by then.
    """
    state_pid, state_execution_id, state_identity = state
    verified_alive = state_pid is not None and process_identity_state(state_pid, state_identity) == "live"
    correlation_failed = isinstance(session_state, dict) and session_state.get("current_state") == "correlation_failed"

    if target is None:
        if state_execution_id is None:
            return {"action": "noop"}
        return {"action": "clear", "kill_pid": state_pid if verified_alive else None}

    wanted = target_execution_id(target)
    if wanted == state_execution_id and verified_alive and not correlation_failed:
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


def find_port_owner_pid(host, port, timeout=5.0):
    """Best-effort discovery of the PID currently bound to (host, port).
    Never a substitute for positive identity verification -- see
    verify_adm_session_center_ownership -- only tells the caller *what to
    check next*, never *what is safe to do*."""
    if os.name == "nt":
        script = (
            f"(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue "
            "| Select-Object -First 1 -ExpandProperty OwningProcess)"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = result.stdout.strip()
        return int(text) if text.isdigit() else None
    try:
        result = subprocess.run(["lsof", "-t", f"-i:{int(port)}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return int(lines[0]) if lines and lines[0].isdigit() else None


def read_process_command_line(pid, timeout=5.0):
    """Best-effort read of a PID's full OS command line, or None if it
    cannot be read. Read-only -- never used by itself to authorize any
    action; see verify_adm_session_center_ownership."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        script = (
            f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}" -ErrorAction SilentlyContinue).CommandLine'
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = result.stdout.strip()
        return text or None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    return text or None


def verify_adm_session_center_ownership(cmdline, port):
    """Positive verification only, fail-closed on any ambiguity: True iff
    `cmdline` is unmistakably a `python ... -m manager.session_center`
    invocation bound to this exact `port`, with the project/provider
    identity arguments spawn_session_center() always supplies. Any
    unreadable/unparseable/partial match returns False -- never a guess,
    and callers must never kill a pid this returns False for.
    """
    if not cmdline:
        return False
    try:
        tokens = shlex.split(cmdline, posix=(os.name != "nt"))
    except ValueError:
        return False
    if not tokens:
        return False
    if "python" not in Path(tokens[0]).name.lower():
        return False
    lowered = [token.lower() for token in tokens]
    try:
        module_index = lowered.index("-m")
    except ValueError:
        return False
    if module_index + 1 >= len(tokens) or tokens[module_index + 1] != "manager.session_center":
        return False

    def value_after(flag):
        try:
            index = lowered.index(flag)
        except ValueError:
            return None
        return tokens[index + 1] if index + 1 < len(tokens) else None

    if value_after("--port") != str(port):
        return False
    project_value = value_after("--execution-project-id") or value_after("--project-id")
    if not project_value:
        return False
    if not value_after("--provider"):
        return False
    return True


def attempt_orphan_recovery(host, port, find_owner_pid=find_port_owner_pid,
                             read_cmdline=read_process_command_line, kill_fn=None):
    """Called only when the port this supervisor needs is occupied by a pid
    it does not itself track. Returns remediation evidence either way.

    Never kills anything it cannot positively verify as its own stale
    manager.session_center process (see verify_adm_session_center_ownership)
    -- an unrelated process on the port is always left alone and always
    fails closed, exactly like the pre-existing port_occupied_unverified
    behavior.
    """
    kill_fn = kill_fn or kill
    owner_pid = find_owner_pid(host, port)
    if owner_pid is None:
        return {"verified": False, "owner_pid": None, "action": "none", "reason": "port_owner_unknown"}
    cmdline = read_cmdline(owner_pid)
    if not cmdline:
        return {"verified": False, "owner_pid": owner_pid, "action": "none", "reason": "command_line_unreadable"}
    if not verify_adm_session_center_ownership(cmdline, port):
        return {"verified": False, "owner_pid": owner_pid, "action": "none",
                "reason": "command_line_not_adm_session_center"}

    kill_fn(owner_pid)
    for _ in range(PORT_RECHECK_ATTEMPTS):
        if port_available(host, port):
            break
        time.sleep(PORT_RECHECK_INTERVAL_SECONDS)
    freed = port_available(host, port)
    return {"verified": True, "owner_pid": owner_pid, "action": "killed",
            "reason": "verified_adm_orphan_session_center", "port_freed": freed}


def read_session_center_state(host, port, timeout=2.0):
    """Best-effort read of the currently-serving Session Center's own
    reported correlation state (/api/session). Any failure -- unreachable,
    non-200, malformed body -- returns None, which decide() treats
    identically to "no new information": it never regresses to worse than
    the pre-existing pid-liveness-only behavior."""
    url = f"http://{host}:{port}/api/session"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read())
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def evidence_path_for(state_path):
    return Path(str(state_path) + ".evidence.json")


def read_evidence(path):
    """Fail closed: any missing/malformed evidence file means "nothing known
    yet", never an error -- this file is diagnostic visibility, not SSOT."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("history"), list):
            return data
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return {"history": [], "open_degradation": None}


def append_evidence(path, *, health_check_at, remediation=None, degraded_reason=None,
                     recovery_result=None, unresolved_blocker=None, open_degradation="__unset__"):
    """Append-only history: a past entry's original timestamp/reason is
    never mutated or dropped, only ever added to (bounded to the most
    recent MAX_EVIDENCE_HISTORY entries). `open_degradation` tracks the one
    active, not-yet-recovered degradation (if any) across cycles so a later
    cycle can recognize "this is the same problem recovering" versus
    "unrelated new problem"; pass None to explicitly clear it, or omit to
    leave the prior value untouched.
    """
    data = read_evidence(path)
    history = data.get("history", [])
    if remediation is not None:
        history = (history + [remediation])[-MAX_EVIDENCE_HISTORY:]
    new_open_degradation = data.get("open_degradation") if open_degradation == "__unset__" else open_degradation
    new_data = {
        "last_health_check": health_check_at,
        "last_remediation": remediation if remediation is not None else data.get("last_remediation"),
        "degraded_reason": degraded_reason,
        "recovery_result": recovery_result,
        "unresolved_blocker": unresolved_blocker,
        "open_degradation": new_open_degradation,
        "history": history,
    }
    write_atomic(Path(path), new_data)
    return new_data


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


def _no_session_state_probe(host, port):
    """Default for run_once()'s read_session_state: makes zero network
    calls and always reports "nothing new observed" -- decide() then
    behaves exactly as it did before correlation-failed re-observe existed.
    A caller must explicitly pass read_session_center_state (main() does)
    to opt into the real HTTP probe; nothing calls out to a real host:port
    just because run_once() was called."""
    return None


def _no_orphan_recovery(host, port):
    """Default for run_once()'s recover_orphan: makes zero subprocess
    calls and always reports "not verified" -- collapsing straight back to
    the pre-existing, unconditional port_occupied_unverified fail-closed
    behavior. A caller must explicitly pass attempt_orphan_recovery
    (main() does) to opt into real PID/command-line inspection."""
    return {"verified": False, "owner_pid": None, "action": "none", "reason": "not_configured"}


def run_once(store, allowlist, state_path, python_exe, repo, port, wait_seconds, host="127.0.0.1",
            bucket=None, ingress_registry_factory=dispatch_request_registry,
            read_session_state=_no_session_state_probe, recover_orphan=_no_orphan_recovery):
    """The entire read-decide-kill/spawn-write cycle for one invocation.
    Callers must hold the exclusive lock for the whole duration -- see main().

    read_session_state and recover_orphan default to pure no-ops (see
    above) precisely so that calling run_once() can never, by itself,
    make a real network call or spawn a real subprocess -- both require an
    explicit opt-in. main() is the only caller that opts in for real.

    Also maintains a diagnostic evidence log (evidence_path_for(state_path))
    covering: last health check, last remediation, current degraded reason,
    most recent recovery result, and any unresolved blocker -- see module
    docstring's state-visibility contract. This file is purely additive
    observability; it is never read back to make a decision (decide() only
    ever consumes `state` and `session_state`), so a missing/corrupt
    evidence file can never change supervisor behavior, only visibility.
    """
    checked_at = utc_iso()
    evidence_path = evidence_path_for(state_path)
    prior_evidence = read_evidence(evidence_path)
    open_degradation = prior_evidence.get("open_degradation")

    state = read_state(state_path)
    session_state = None
    if state[0] is not None and state[1] is not None:
        session_state = read_session_state(host, port)
    target = find_active_command(store, allowlist, bucket=bucket, ingress_registry_factory=ingress_registry_factory)
    decision = decide(state, target, session_state)

    currently_correlation_failed = isinstance(session_state, dict) and session_state.get("current_state") == "correlation_failed"

    # Recovered: a prior cycle recorded an open degradation for this exact
    # execution_id, and this cycle's live probe shows it is no longer
    # correlation_failed. Appended as a new history entry -- the original
    # failure entry already in history is never rewritten.
    recovered_this_cycle = False
    if (isinstance(open_degradation, dict) and open_degradation.get("execution_id") == state[1]
            and session_state is not None and not currently_correlation_failed):
        prior_evidence = append_evidence(
            evidence_path, health_check_at=checked_at,
            remediation={
                "event": "recovered", "execution_id": state[1], "at": checked_at,
                "original_failed_at": open_degradation.get("failed_at"),
                "original_error": open_degradation.get("error"),
            },
            degraded_reason=None, recovery_result="recovered", unresolved_blocker=None,
            open_degradation=None,
        )
        open_degradation = None
        recovered_this_cycle = True

    if decision["action"] == "noop":
        # Skip this cycle's plain health-check write when a "recovered"
        # entry was just appended above -- writing again here with no
        # remediation would otherwise clobber that recovery_result back to
        # None within the same cycle.
        if not currently_correlation_failed and not recovered_this_cycle:
            append_evidence(evidence_path, health_check_at=checked_at, degraded_reason=None, unresolved_blocker=None)
        return {"status": "unchanged", "execution_id": state[1]}

    is_correlation_failed_reobserve = (
        decision["action"] == "respawn" and decision["execution_id"] == state[1] and currently_correlation_failed
    )
    if is_correlation_failed_reobserve and isinstance(session_state, dict):
        # Durably record the original failure evidence *before* the process
        # that observed it is killed below -- this is the only point where
        # that in-memory evidence is still readable.
        append_evidence(
            evidence_path, health_check_at=checked_at,
            remediation={
                "event": "correlation_failed_detected", "execution_id": state[1],
                "error": session_state.get("error"), "failed_at": session_state.get("failed_at"),
                "detected_at": checked_at,
            },
            degraded_reason="correlation_failed", recovery_result="reobserving",
            unresolved_blocker=f"execution {state[1]} has not yet correlated to a live provider session",
            open_degradation={"execution_id": state[1], "failed_at": session_state.get("failed_at"),
                               "error": session_state.get("error")},
        )

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
        append_evidence(evidence_path, health_check_at=checked_at, degraded_reason=None, unresolved_blocker=None)
        return {"status": "idle"}

    if not port_available(host, port):
        # Something is listening that this run did not just verify-and-kill
        # as its own. Attempt bounded, positively-verified orphan recovery
        # (goal A) -- never spawn a second listener, never guess who it is,
        # never kill anything that fails verification.
        orphan = recover_orphan(host, port)
        if not (orphan.get("verified") and orphan.get("port_freed")):
            append_evidence(
                evidence_path, health_check_at=checked_at, remediation={
                    "event": "orphan_check", "at": checked_at, "port": port, **orphan,
                },
                degraded_reason="port_occupied_unverified", recovery_result=None,
                unresolved_blocker=f"port {port} is held by an unverified process (pid={orphan.get('owner_pid')})",
            )
            # Unchanged pre-existing return shape -- the orphan check detail
            # lives in the evidence log (last_remediation), not here, so
            # this never breaks a caller depending on this dict's shape.
            return {"status": "attention", "reason": "port_occupied_unverified"}
        append_evidence(
            evidence_path, health_check_at=checked_at, remediation={
                "event": "orphan_recovery", "at": checked_at, "port": port, **orphan,
            },
            degraded_reason=None, recovery_result="orphan_killed_port_freed", unresolved_blocker=None,
        )

    process = spawn_session_center(python_exe, repo, decision["project_id"], decision["execution_id"], port, wait_seconds,
                                   provider=decision.get("provider", "codex"))
    identity = process_creation_identity(process.pid)
    write_state(state_path, process.pid, decision["execution_id"], identity)
    append_evidence(evidence_path, health_check_at=checked_at, degraded_reason=None if not open_degradation else "correlation_failed",
                     recovery_result="spawned", unresolved_blocker=None)
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
            # Real production run: explicitly opt into the live /api/session
            # probe and real orphan-process verification. run_once()'s own
            # defaults are no-ops precisely so nothing else has to.
            result = run_once(store, allowlist, args.state_file, args.python_path,
                              args.repository_path, args.port, args.wait_seconds,
                              bucket=os.environ.get("ADM_LOCK_GCS_BUCKET"),
                              read_session_state=read_session_center_state,
                              recover_orphan=attempt_orphan_recovery)
    except RefreshError:
        # Another invocation is already mid-cycle: no-op, spawn/kill/write
        # nothing. This is not a failure -- it is mutual exclusion working.
        result = {"status": "locked"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
