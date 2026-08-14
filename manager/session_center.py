"""Minimal localhost UI for one live Windows Codex session."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
 const root=document.querySelector('#fields');root.replaceChildren();for(const [k,label] of Object.entries(labels)){const dt=document.createElement('dt');dt.textContent=label;const dd=document.createElement('dd');dd.textContent=s[k]??'—';root.append(dt,dd)}}
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


def load_execution(path: Path, provider_session_id: str, provider_cwd: str) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionCenterError("could not read ADM Execution JSON") from exc
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
    session_file: Path
    cwd: str
    started_at: str
    project_id: str
    task_id: str
    execution_id: str | None
    branch: str | None
    provider: str = "codex"
    idle_seconds: float = 15.0
    correlated: bool = False
    _size: int = field(init=False)
    _latest: float = field(default_factory=time.time, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        self._size = self.session_file.stat().st_size

    def snapshot(self) -> dict:
        now = time.time()
        size = self.session_file.stat().st_size
        with self._lock:
            if size != self._size:
                self._size = size
                self._latest = now
            latest = self._latest
        return {
            "provider": self.provider,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id or "UNLINKED",
            "provider_session_id": self.provider_session_id,
            "cwd": self.cwd,
            "branch": self.branch or "—",
            "started_at": self.started_at,
            "current_state": "running" if now - latest < self.idle_seconds else "waiting",
            "latest_activity": utc_iso(latest),
            "correlated": self.correlated,
        }


def handler_for(session: LiveSession):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", HTML.encode())
            elif self.path == "/api/session":
                self._send(200, "application/json", json.dumps(session.snapshot()).encode())
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


def build_session(args: argparse.Namespace) -> LiveSession:
    session_file = find_codex_session(args.provider_session_id)
    meta = read_codex_meta(session_file, args.provider_session_id)
    if args.execution_file:
        execution = load_execution(args.execution_file, args.provider_session_id, meta["cwd"])
        return LiveSession(
            args.provider_session_id, session_file, meta["cwd"], meta["started_at"], execution["project_id"],
            execution["task_id"], execution["execution_id"], execution["branch"], execution["provider"], args.idle_seconds, True,
        )
    if not args.project_id or not args.task_id:
        raise SessionCenterError("project/task are required when no ADM Execution JSON is provided")
    return LiveSession(
        args.provider_session_id, session_file, meta["cwd"], meta["started_at"], args.project_id, args.task_id,
        None, args.branch or current_branch(meta["cwd"]), "codex", args.idle_seconds, False,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--provider-session-id", required=True)
    result.add_argument("--execution-file", type=Path)
    result.add_argument("--project-id")
    result.add_argument("--task-id")
    result.add_argument("--branch")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--idle-seconds", type=float, default=15.0)
    return result


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.port <= 65535 or args.idle_seconds <= 0:
        raise SystemExit("invalid port or idle timeout")
    try:
        session = build_session(args)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(session))
    except (SessionCenterError, OSError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1
    print(json.dumps({"url": f"http://127.0.0.1:{args.port}", "provider_session_id": session.provider_session_id}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
