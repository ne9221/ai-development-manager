"""Small Codex app-server protocol adapter; not wired to Manager lifecycle."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MAX_TIMEOUT_SECONDS = 300.0
MAX_ERROR_CHARS = 1000
MAX_STDERR_CHARS = 8192


class CodexLaunchError(RuntimeError):
    """A bounded, provider-protocol launch failure."""

    def __init__(self, classification: str, detail: str):
        self.classification = classification
        self.detail = str(detail)[:MAX_ERROR_CHARS]
        super().__init__(f"{classification}: {self.detail}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_codex_executable(explicit: str | None = None) -> str:
    candidate = explicit or os.environ.get("CODEX_BIN")
    if candidate:
        path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
    else:
        names = ("codex.cmd", "codex.exe", "codex") if os.name == "nt" else ("codex",)
        path = next((found for name in names if (found := shutil.which(name))), None)
    if not path:
        raise CodexLaunchError("executable_not_found", "Codex CLI was not found")
    return str(Path(path).resolve())


@dataclass(frozen=True)
class LaunchRequest:
    working_directory: str
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str | None = None
    approval_policy: str | None = None
    timeout_seconds: float = 30.0


@dataclass
class PreparedLaunch:
    thread_id: str
    session_path: str | None
    pid: int
    prepared_at: str
    _client: "_AppServerClient" = field(repr=False)
    _request: LaunchRequest = field(repr=False)
    _started: bool = field(default=False, repr=False)


@dataclass
class RunningLaunch:
    prepared: PreparedLaunch
    turn_id: str
    started_at: str
    _cancelled: bool = field(default=False, repr=False)


@dataclass(frozen=True)
class LaunchOutcome:
    status: str
    thread_id: str
    turn_id: str
    completed_at: str
    failure_classification: str | None = None
    failure_detail: str | None = None


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise CodexLaunchError("invalid_request", f"timeout_seconds must be within (0, {MAX_TIMEOUT_SECONDS:g}]")
    return float(value)


def _rpc_error(error: Any) -> str:
    if not isinstance(error, dict):
        return "app-server returned an error"
    code = error.get("code")
    message = error.get("message")
    safe_message = str(message)[:300] if isinstance(message, str) else "app-server returned an error"
    return f"code={code}; {safe_message}" if code is not None else safe_message


class _AppServerClient:
    def __init__(self, process: Any, timeout: float):
        self.process = process
        self.timeout = timeout
        self._responses: dict[int, queue.Queue] = {}
        self._notifications: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 1
        self._closed = False
        self._stderr_tail = ""
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    self._notifications.put(("protocol_error", "malformed JSON from app-server"))
                    continue
                if not isinstance(message, dict):
                    self._notifications.put(("protocol_error", "non-object JSON-RPC message"))
                    continue
                request_id = message.get("id")
                with self._lock:
                    target = self._responses.get(request_id)
                if request_id is not None and target is not None:
                    target.put(message)
                else:
                    self._notifications.put(("notification", message))
        finally:
            self._notifications.put(("eof", self.process.poll()))
            with self._lock:
                targets = list(self._responses.values())
            for target in targets:
                target.put(None)

    def _drain_stderr(self):
        try:
            for chunk in self.process.stderr:
                self._stderr_tail = (self._stderr_tail + str(chunk))[-MAX_STDERR_CHARS:]
        except (OSError, ValueError):
            pass

    def send(self, message: dict):
        if self.process.poll() is not None:
            raise CodexLaunchError("process_exit", f"app-server exited with code {self.process.returncode}")
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CodexLaunchError("transport_error", "could not write to app-server") from exc

    def notify(self, method: str, params: dict | None = None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(self, method: str, params: dict | None = None) -> Any:
        response_queue: queue.Queue = queue.Queue()
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._responses[request_id] = response_queue
        try:
            message = {"method": method, "id": request_id}
            if params is not None:
                message["params"] = params
            self.send(message)
            try:
                response = response_queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise CodexLaunchError("timeout", f"{method} timed out") from exc
            if response is None:
                code = self.process.poll()
                classification = "process_exit" if code is not None else "stdout_eof"
                raise CodexLaunchError(classification, f"app-server closed stdout; exit_code={code}")
            if "error" in response:
                raise CodexLaunchError("protocol_error", f"{method}: {_rpc_error(response['error'])}")
            if "result" not in response:
                raise CodexLaunchError("protocol_error", f"{method}: response has no result")
            return response["result"]
        finally:
            with self._lock:
                self._responses.pop(request_id, None)

    def next_event(self, timeout: float):
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexLaunchError("timeout", "turn completion timed out") from exc

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


class CodexLauncher:
    """Codex-only two-phase app-server adapter."""

    def __init__(self, executable: str | None = None, popen: Callable[..., Any] = subprocess.Popen):
        self.executable = executable
        self._popen = popen

    def _spawn(self, request: LaunchRequest) -> _AppServerClient:
        executable = resolve_codex_executable(self.executable)
        command = [executable, "app-server"]
        if executable.lower().endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", f'""{executable}" app-server"']
        try:
            process = self._popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as exc:
            raise CodexLaunchError("spawn_failed", "failed to start Codex app-server") from exc
        return _AppServerClient(process, _timeout(request.timeout_seconds))

    def prepare(self, request: LaunchRequest) -> PreparedLaunch:
        cwd = Path(request.working_directory)
        if not cwd.is_absolute() or not cwd.is_dir():
            raise CodexLaunchError("invalid_request", "working_directory must be an existing absolute directory")
        client = self._spawn(request)
        try:
            initialized = client.request("initialize", {"clientInfo": {
                "name": "ai_development_manager", "title": "AI Development Manager", "version": "0.1.0"
            }})
            if not isinstance(initialized, dict):
                raise CodexLaunchError("protocol_error", "initialize returned an invalid result")
            client.notify("initialized", {})
            params = {"cwd": str(cwd)}
            if request.model:
                params["model"] = request.model
            if request.sandbox:
                params["sandbox"] = request.sandbox
            if request.approval_policy:
                params["approvalPolicy"] = request.approval_policy
            result = client.request("thread/start", params)
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise CodexLaunchError("protocol_error", "thread/start returned no thread id")
            session_path = thread.get("path")
            if session_path is not None and not isinstance(session_path, str):
                raise CodexLaunchError("protocol_error", "thread/start returned an invalid session path")
            return PreparedLaunch(thread_id, session_path, process_pid(client.process), utc_now(), client, request)
        except Exception:
            client.close()
            raise

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared._started:
            raise CodexLaunchError("invalid_state", "prepared launch has already started")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CodexLaunchError("invalid_request", "prompt must be non-empty")
        params: dict[str, Any] = {
            "threadId": prepared.thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if prepared._request.model:
            params["model"] = prepared._request.model
        if prepared._request.reasoning_effort:
            params["effort"] = prepared._request.reasoning_effort
        result = prepared._client.request("turn/start", params)
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexLaunchError("protocol_error", "turn/start returned no turn id")
        prepared._started = True
        return RunningLaunch(prepared, turn_id, utc_now())

    def wait(self, running: RunningLaunch) -> LaunchOutcome:
        client = running.prepared._client
        while True:
            kind, payload = client.next_event(client.timeout)
            if kind == "protocol_error":
                raise CodexLaunchError("protocol_error", payload)
            if kind == "eof":
                classification = "process_exit" if payload is not None else "stdout_eof"
                raise CodexLaunchError(classification, f"app-server closed stdout; exit_code={payload}")
            if not isinstance(payload, dict) or payload.get("method") != "turn/completed":
                continue
            params = payload.get("params")
            turn = params.get("turn") if isinstance(params, dict) else None
            if not isinstance(turn, dict) or params.get("threadId") != running.prepared.thread_id or turn.get("id") != running.turn_id:
                continue
            status = turn.get("status")
            if status == "completed":
                return LaunchOutcome("completed", running.prepared.thread_id, running.turn_id, utc_now())
            error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
            detail = str(error.get("message") or f"turn ended with status {status}")[:MAX_ERROR_CHARS]
            classification = "cancelled" if running._cancelled and status == "interrupted" else "turn_failed"
            return LaunchOutcome(status if status in ("failed", "interrupted") else "failed", running.prepared.thread_id, running.turn_id, utc_now(), classification, detail)

    def cancel(self, running: RunningLaunch, reason: str | None = None):
        del reason  # Deliberately not sent or logged; it may contain sensitive text.
        if running._cancelled:
            return
        running.prepared._client.request("turn/interrupt", {
            "threadId": running.prepared.thread_id, "turnId": running.turn_id,
        })
        running._cancelled = True

    def close(self, handle: PreparedLaunch | RunningLaunch):
        prepared = handle.prepared if isinstance(handle, RunningLaunch) else handle
        prepared._client.close()


def process_pid(process: Any) -> int:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        raise CodexLaunchError("protocol_error", "app-server process has no valid PID")
    return pid
