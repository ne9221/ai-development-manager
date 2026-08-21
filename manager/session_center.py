"""Minimal localhost UI for one live Windows Codex or Claude session."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


# Deliberately includes "cancelled" unlike the recovery-arbitration TERMINAL
# sets elsewhere in this package: this constant governs what the UI must
# display, not what recovery is allowed to act on.
TERMINAL_EXECUTION_STATUSES = frozenset({"completed", "failed", "interrupted", "cancelled"})

# terminalize_execution() writes execution.status as terminal before
# cleanup_execution() has released task-claim/writer-lease authority (an
# interim cleanup_evidence with "retained" values is persisted first, then
# overwritten once release actually completes). Never surface a terminal
# status from that window: it would show "completed" while ADM authority is
# still held, which hands-off automation must never treat as done.
FINISHING = "finishing"


def _cleanup_confirmed(record: dict) -> bool:
    """Fail-closed: True only when cleanup evidence proves the task claim,
    and (for anything but an explicit read-only access) the writer lease,
    have actually been released."""
    evidence = record.get("cleanup_evidence")
    if not isinstance(evidence, dict) or evidence.get("task_claim_release") != "released":
        return False
    writer_release = evidence.get("writer_release")
    if record.get("access") == "read_only":
        return writer_release in ("released", "not_required")
    return writer_release == "released"


def _authoritative_state(record: dict) -> str | None:
    """Map a raw Execution record to what the UI may display: non-terminal
    statuses pass through unchanged; a terminal status only passes through
    once cleanup is confirmed, otherwise it degrades to FINISHING."""
    status = record.get("status") if isinstance(record, dict) else None
    if not isinstance(status, str):
        return None
    if status not in TERMINAL_EXECUTION_STATUSES:
        return status
    return status if _cleanup_confirmed(record) else FINISHING


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Windows Session Center</title>
<style>
body{font:15px system-ui;margin:0;background:#10151d;color:#e8edf4}main{max-width:1100px;margin:40px auto;padding:0 20px}
h1{font-size:24px}.card{background:#18212d;border:1px solid #344255;border-radius:10px;padding:20px}
.badge{display:inline-block;padding:4px 9px;border-radius:99px;background:#164e3b;color:#8ff0c0}.bad{background:#55252b;color:#ffb4bc}
dl{display:grid;grid-template-columns:190px 1fr;gap:10px 18px}dt{color:#8fa2b8}dd{margin:0;font-family:ui-monospace,monospace;overflow-wrap:anywhere}
.pulse{width:10px;height:10px;background:#45d483;border-radius:50%;display:inline-block;margin-right:8px}small{color:#8fa2b8}
</style></head><body><main><h1>Thin Windows Session Center</h1><p><span class="pulse"></span><span id="summary">Loading…</span></p>
<section class="card"><dl id="fields"></dl></section><p><small>Live localhost view · refreshes every second · no transcript content is read</small></p>
<script>
const labels={provider:'Provider / AI',project_id:'Project',task_id:'Task ID',execution_id:'Execution ID',provider_session_id:'Provider session ID',cwd:'cwd',branch:'Branch',started_at:'Started at',current_state:'Current state',latest_activity:'Latest activity'};
async function refresh(){const r=await fetch('/api/session',{cache:'no-store'});const s=await r.json();
 document.querySelector('#summary').innerHTML=`<span class="badge ${s.correlated?'':'bad'}">${s.correlated?'CORRELATED':'UNLINKED'}</span> &nbsp; ${s.current_state}`;
 const root=document.querySelector('#fields');root.replaceChildren();for(const [k,label] of Object.entries(labels)){const dt=document.createElement('dt');dt.textContent=label;const dd=document.createElement('dd');dd.textContent=(k==='provider'&&s.account_id)?`${s.provider} · ${s.account_id}`:(s[k]??'—');root.append(dt,dd)}}
refresh();setInterval(refresh,1000);
</script></main></body></html>"""


class SessionCenterError(RuntimeError):
    pass


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def find_codex_session(provider_session_id: str, codex_home: Path | None = None) -> Path:
    root = (codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))) / "sessions"
    matches = list(root.rglob(f"*{provider_session_id}*.jsonl"))
    if len(matches) != 1:
        raise SessionCenterError(f"expected one Codex session file, found {len(matches)}")
    return matches[0]


def read_codex_meta(path: Path, provider_session_id: str) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            record = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionCenterError("could not read Codex session metadata") from exc
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict) or record.get("type") != "session_meta" or payload.get("id") != provider_session_id:
        raise SessionCenterError("Codex session metadata identity mismatch")
    if not isinstance(payload.get("cwd"), str) or not isinstance(record.get("timestamp"), str):
        raise SessionCenterError("Codex session metadata is incomplete")
    return {"cwd": payload["cwd"], "started_at": record["timestamp"]}


def _run_claude_agents_json(executable: str | None = None, timeout: float = 5.0) -> list:
    """Invoke `claude agents --json` and parse its structured session list.
    Any failure -- missing executable, nonzero exit, malformed JSON, a
    non-list result -- raises SessionCenterError so the caller's own bounded
    polling loop (identical to Codex's) retries rather than treating one
    failed probe as final.
    """
    exe = executable or shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")
    if not exe:
        raise SessionCenterError("Claude CLI was not found")
    try:
        result = subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionCenterError(f"claude agents --json failed to run: {exc}") from exc
    if result.returncode != 0:
        raise SessionCenterError(f"claude agents --json exited with code {result.returncode}")
    try:
        agents = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SessionCenterError("claude agents --json returned malformed JSON") from exc
    if not isinstance(agents, list):
        raise SessionCenterError("claude agents --json did not return a list")
    return agents


def find_claude_session(provider_session_id: str, executable: str | None = None) -> dict:
    """Exact-match provider_session_id (the UUID ADM assigned before spawn,
    via --session-id) against claude agents --json's sessionId field -- the
    sole identity authority. PID/cwd/startedAt are read back only as
    corroborating evidence attached to the confirmed match; they are never
    used to pick among candidates, so a decoy entry sharing this host's cwd
    or an unrelated agent reusing a PID can never be mistaken for the target."""
    agents = _run_claude_agents_json(executable)
    matches = [agent for agent in agents if isinstance(agent, dict) and agent.get("sessionId") == provider_session_id]
    if len(matches) != 1:
        raise SessionCenterError(
            f"expected exactly one Claude agent session for {provider_session_id!r}, found {len(matches)}"
        )
    agent = matches[0]
    for key in ("pid", "cwd", "startedAt"):
        if key not in agent:
            raise SessionCenterError(f"claude agents --json entry is missing {key!r}")
    return agent


def read_claude_meta(agent: dict) -> dict:
    cwd, started_at_ms, pid = agent.get("cwd"), agent.get("startedAt"), agent.get("pid")
    if not isinstance(cwd, str) or not cwd:
        raise SessionCenterError("Claude agent entry is missing cwd")
    if not isinstance(started_at_ms, (int, float)) or isinstance(started_at_ms, bool):
        raise SessionCenterError("Claude agent entry is missing startedAt")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise SessionCenterError("Claude agent entry is missing pid")
    return {"cwd": cwd, "started_at": utc_iso(started_at_ms / 1000), "pid": pid}


def resolve_provider_meta(provider: str, provider_session_id: str) -> dict:
    """Provider dispatch for locating and reading live provider session
    evidence: {"cwd", "started_at", "session_file" (Path|None), "pid" (int|None)}.
    An unrecognized provider fails closed -- it never falls back to Codex's
    (or any other provider's) resolver."""
    if provider == "codex":
        session_file = find_codex_session(provider_session_id)
        meta = read_codex_meta(session_file, provider_session_id)
        return {**meta, "session_file": session_file, "pid": None}
    if provider == "claude":
        agent = find_claude_session(provider_session_id)
        meta = read_claude_meta(agent)
        return {**meta, "session_file": None}
    raise SessionCenterError(f"unsupported provider for session correlation: {provider!r}")


def load_execution(path: Path, provider_session_id: str, provider_cwd: str) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionCenterError("could not read ADM Execution JSON") from exc
    return validate_execution(record, provider_session_id, provider_cwd)


def validate_execution(record: dict, provider_session_id: str, provider_cwd: str) -> dict:
    required = ("provider", "project_id", "task_id", "execution_id", "provider_session_id")
    if not isinstance(record, dict) or any(not isinstance(record.get(key), str) or not record[key] for key in required):
        raise SessionCenterError("ADM Execution JSON is missing identity fields")
    if record["provider_session_id"] != provider_session_id:
        raise SessionCenterError("ADM Execution provider_session_id mismatch")
    snapshot = record.get("task_snapshot")
    if not isinstance(snapshot, dict):
        raise SessionCenterError("ADM Execution is missing task_snapshot")
    cwd = snapshot.get("working_directory")
    branch = snapshot.get("branch")
    if not isinstance(cwd, str) or not isinstance(branch, str):
        raise SessionCenterError("ADM Execution task_snapshot is missing cwd or branch")
    if os.path.normcase(os.path.abspath(cwd)) != os.path.normcase(os.path.abspath(provider_cwd)):
        raise SessionCenterError("ADM Execution cwd does not match provider session")
    return {**record, "cwd": cwd, "branch": branch}


def wait_for_execution(store, project_id: str, execution_id: str, wait_seconds: float) -> dict:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            record = store.get("executions", project_id, execution_id)
        except KeyError:
            record = None
        except RuntimeError as exc:
            if "expected one Drive record" not in str(exc) or "found 0" not in str(exc):
                raise
            record = None
        if isinstance(record, dict) and isinstance(record.get("provider_session_id"), str):
            return record
        if time.monotonic() >= deadline:
            raise SessionCenterError("timed out waiting for ADM Execution session link")
        time.sleep(.25)


def current_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True, timeout=3, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


@dataclass
class LiveSession:
    provider_session_id: str
    # None for providers (e.g. Claude) whose transcript path is only
    # optional advisory corroboration, never the identity authority --
    # exact provider_session_id match (via find_claude_session) already
    # confirmed correlation before a LiveSession is ever constructed.
    session_file: Path | None
    cwd: str
    started_at: str
    project_id: str
    task_id: str
    execution_id: str | None
    branch: str | None
    provider: str = "codex"
    idle_seconds: float = 15.0
    correlated: bool = False
    status_source: Callable[[], str | None] | None = field(default=None, repr=False)
    pid: int | None = None
    mode: str | None = None
    # Threaded straight from the authoritative Execution record's own
    # account_id (manager/executions.py session_link_fields) -- never
    # guessed from config_dir, PID, or session title. None for legacy
    # Executions and for the raw provider-session-id mode, which has no
    # Execution record to read it from.
    account_id: str | None = None
    _size: int = field(init=False)
    _latest: float = field(default_factory=time.time, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        self._size = self.session_file.stat().st_size if self.session_file is not None else 0

    def snapshot(self) -> dict:
        now = time.time()
        size = self.session_file.stat().st_size if self.session_file is not None else self._size
        with self._lock:
            if size != self._size:
                self._size = size
                self._latest = now
            latest = self._latest
        # Authoritative ADM lifecycle status always wins over transcript
        # activity: an execution can only be "completed"/"failed"/etc. on
        # explicit SSOT evidence, never inferred from transcript inactivity.
        # Without a transcript file at all (no activity signal to observe),
        # this authoritative-status path is the *only* source of truth --
        # the idle/running fallback below degrades to "no observed
        # activity yet" rather than a meaningful running/waiting distinction.
        authoritative_status = self.status_source() if self.status_source else None
        if authoritative_status in TERMINAL_EXECUTION_STATUSES or authoritative_status == FINISHING:
            current_state = authoritative_status
        else:
            current_state = "running" if now - latest < self.idle_seconds else "waiting"
        return {
            "provider": self.provider,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id or "UNLINKED",
            "provider_session_id": self.provider_session_id,
            "pid": self.pid,
            "mode": self.mode,
            "cwd": self.cwd,
            "branch": self.branch or "—",
            "started_at": self.started_at,
            "current_state": current_state,
            "latest_activity": utc_iso(latest),
            "correlated": self.correlated,
            "account_id": self.account_id,
        }


@dataclass
class PendingCorrelation:
    """Displayed while the HTTP server is already up but the target
    Execution/session has not been observed yet -- the deterministic
    execution_id is known immediately (from the CLI args that name it), so
    this is never a guess and never fakes a provider_session_id or a
    running/completed state."""
    project_id: str | None
    task_id: str | None
    execution_id: str | None
    provider: str = "codex"
    error: str | None = None
    # First-failure timestamp only -- see SessionView.fail(). Exists so a
    # supervisor probing /api/session before respawning this process (to
    # re-attempt correlation, see manager.session_center_supervisor) can
    # durably record the *original* failure evidence before it is lost with
    # this process. Never reset by a later fail() call within this process.
    failed_at: str | None = None

    def snapshot(self) -> dict:
        return {
            "provider": self.provider,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id or "UNLINKED",
            "provider_session_id": None,
            "cwd": None,
            "branch": None,
            "started_at": None,
            "current_state": "correlation_failed" if self.error else "correlating",
            "latest_activity": None,
            "correlated": False,
            "account_id": None,
            "error": self.error,
            "failed_at": self.failed_at,
        }


class SessionView:
    """Thread-safe holder the HTTP handler reads through. Starts as a
    PendingCorrelation and is swapped, at most once, for a resolved
    LiveSession -- in place, no server restart -- once correlation succeeds.
    """
    def __init__(self, pending: PendingCorrelation):
        self._lock = threading.Lock()
        self._current: PendingCorrelation | LiveSession = pending

    def snapshot(self) -> dict:
        with self._lock:
            current = self._current
        return current.snapshot()

    def resolve(self, live_session: LiveSession) -> None:
        with self._lock:
            self._current = live_session

    def fail(self, message: str) -> None:
        with self._lock:
            if isinstance(self._current, PendingCorrelation):
                self._current = PendingCorrelation(
                    self._current.project_id, self._current.task_id,
                    self._current.execution_id, self._current.provider, error=message,
                    # First-failure wins: a repeated fail() (there is none
                    # today, but this must not silently start rewriting
                    # history if one is ever added) never overwrites the
                    # original failure timestamp.
                    failed_at=self._current.failed_at or utc_iso(time.time()),
                )


def handler_for(view: SessionView):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", HTML.encode())
            elif self.path == "/api/session":
                self._send(200, "application/json", json.dumps(view.snapshot()).encode())
            elif self.path == "/health":
                # Liveness only: the HTTP server itself is up and serving.
                # Deliberately independent of Drive reachability or
                # correlation state -- those are what /api/session reports.
                self._send(200, "application/json", b'{"status":"ok"}')
            else:
                self._send(404, "application/json", b'{"error":"not found"}')

        def _send(self, status: int, content_type: str, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return Handler


def drive_status_source(store, project_id: str, execution_id: str) -> Callable[[], str | None]:
    """Re-read the authoritative Drive execution state; never raise into the poll loop."""
    def read() -> str | None:
        try:
            record = store.get("executions", project_id, execution_id)
        except (KeyError, RuntimeError):
            return None
        return _authoritative_state(record) if isinstance(record, dict) else None
    return read


def file_status_source(execution_file: Path, provider_session_id: str, provider_cwd: str) -> Callable[[], str | None]:
    """Re-read the ADM Execution JSON snapshot's state; never raise into the poll loop."""
    def read() -> str | None:
        try:
            record = validate_execution(
                json.loads(execution_file.read_text(encoding="utf-8")), provider_session_id, provider_cwd
            )
        except (OSError, json.JSONDecodeError, SessionCenterError):
            return None
        return _authoritative_state(record)
    return read


def _validate_identity_args(args: argparse.Namespace) -> None:
    """Pure, non-blocking input validation. Anything that requires waiting
    on Drive or the filesystem is deferred to the background resolver so it
    can never delay the HTTP server binding."""
    if args.execution_project_id or args.execution_id:
        if not args.execution_project_id or not args.execution_id or args.execution_file or args.provider_session_id:
            raise SessionCenterError("execution project/id mode cannot be combined with another identity source")
        return
    if not args.provider_session_id:
        raise SessionCenterError("provider session ID or execution project/id is required")
    if not args.execution_file and (not args.project_id or not args.task_id):
        raise SessionCenterError("project/task are required when no ADM Execution JSON is provided")


def build_pending(args: argparse.Namespace) -> PendingCorrelation:
    """Fast: figures out the deterministic identity to display while
    correlating, without touching Drive or the filesystem. provider is
    already deterministically known from the target Command at spawn time
    (see session_center_supervisor.spawn_session_center) -- no Drive/
    filesystem read needed to show it here, same as project_id/task_id/
    execution_id."""
    _validate_identity_args(args)
    provider = getattr(args, "provider", None) or "codex"
    if args.execution_project_id:
        return PendingCorrelation(args.execution_project_id, None, args.execution_id, provider)
    return PendingCorrelation(args.project_id, args.task_id, None, provider)


def _resolve_execution_mode(args: argparse.Namespace, deadline: float) -> LiveSession:
    from manager.tasks import DriveRecords, build_service
    store = DriveRecords(build_service())
    execution = wait_for_execution(store, args.execution_project_id, args.execution_id, max(0.1, deadline - time.monotonic()))
    provider_session_id = execution["provider_session_id"]
    last_error: SessionCenterError | None = None
    while time.monotonic() < deadline:
        try:
            meta = resolve_provider_meta(execution["provider"], provider_session_id)
            execution = validate_execution(execution, provider_session_id, meta["cwd"])
            return LiveSession(
                provider_session_id, meta["session_file"], meta["cwd"], meta["started_at"], execution["project_id"],
                execution["task_id"], execution["execution_id"], execution["branch"], execution["provider"], args.idle_seconds, True,
                status_source=drive_status_source(store, execution["project_id"], execution["execution_id"]),
                pid=meta.get("pid"), mode=execution.get("mode"), account_id=execution.get("account_id"),
            )
        except SessionCenterError as exc:
            last_error = exc
            time.sleep(.25)
    raise last_error or SessionCenterError("timed out waiting for the provider session")


def _resolve_provider_session_mode(args: argparse.Namespace, deadline: float) -> LiveSession:
    provider_session_id = args.provider_session_id
    provider = getattr(args, "provider", None) or "codex"
    last_error: SessionCenterError | None = None
    while time.monotonic() < deadline:
        try:
            meta = resolve_provider_meta(provider, provider_session_id)
            if args.execution_file:
                execution = load_execution(args.execution_file, provider_session_id, meta["cwd"])
                return LiveSession(
                    provider_session_id, meta["session_file"], meta["cwd"], meta["started_at"], execution["project_id"],
                    execution["task_id"], execution["execution_id"], execution["branch"], execution["provider"], args.idle_seconds, True,
                    status_source=file_status_source(args.execution_file, provider_session_id, meta["cwd"]),
                    pid=meta.get("pid"), mode=execution.get("mode"), account_id=execution.get("account_id"),
                )
            return LiveSession(
                provider_session_id, meta["session_file"], meta["cwd"], meta["started_at"], args.project_id, args.task_id,
                None, args.branch or current_branch(meta["cwd"]), provider, args.idle_seconds, False,
                pid=meta.get("pid"),
            )
        except SessionCenterError as exc:
            last_error = exc
            time.sleep(.25)
    raise last_error or SessionCenterError("timed out waiting for the provider session")


def resolve_session(args: argparse.Namespace, deadline: float) -> LiveSession:
    if args.execution_project_id:
        return _resolve_execution_mode(args, deadline)
    return _resolve_provider_session_mode(args, deadline)


def _resolve_and_swap(view: SessionView, args: argparse.Namespace) -> None:
    """Runs in a background thread; never blocks HTTP server startup."""
    deadline = time.monotonic() + args.wait_seconds
    try:
        live_session = resolve_session(args, deadline)
    except SessionCenterError as exc:
        view.fail(str(exc))
        return
    view.resolve(live_session)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--provider-session-id")
    result.add_argument("--execution-file", type=Path)
    result.add_argument("--execution-project-id")
    result.add_argument("--execution-id")
    result.add_argument("--wait-seconds", type=float, default=60.0)
    result.add_argument("--project-id")
    result.add_argument("--task-id")
    result.add_argument("--branch")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--idle-seconds", type=float, default=15.0)
    result.add_argument("--provider", default="codex")
    return result


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.port <= 65535 or args.idle_seconds <= 0 or args.wait_seconds <= 0:
        raise SystemExit("invalid port or idle timeout")
    try:
        pending = build_pending(args)
    except SessionCenterError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1
    view = SessionView(pending)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(view))
    except OSError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1
    # /health is reachable the instant this prints -- correlation (which may
    # depend on the watcher having launched a provider yet) happens after,
    # in the background, so a health-gated launcher can never deadlock on it.
    print(json.dumps({"url": f"http://127.0.0.1:{args.port}"}))
    threading.Thread(target=_resolve_and_swap, args=(view, args), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
