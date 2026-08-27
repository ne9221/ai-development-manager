"""Small Codex app-server protocol adapter; not wired to Manager lifecycle."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MAX_TIMEOUT_SECONDS = 300.0
MAX_TURN_TIMEOUT_SECONDS = 7200.0
HEARTBEAT_INTERVAL_SECONDS = 60.0
MAX_ERROR_CHARS = 1000
MAX_STDERR_CHARS = 8192

# The only two unattended sandbox/approval_policy profiles this launcher
# understands. GOVERNED_REPO_WRITE is the narrowest Codex app-server
# configuration that still allows unattended edits inside the cwd it was
# given (LaunchRequest.working_directory, which for a genuine v2-repo-write
# Task is always its own isolated worktree -- never the shared canonical
# checkout, see manager.execution_runner._resolve_working_directory): the
# built-in "workspace-write" sandbox itself is what refuses any write
# outside that tree, this launcher never widens or disables it, and
# approval_policy="never" means Codex is not expected to pause for
# approval at all rather than this launcher blanket-approving whatever it
# asks. Any other sandbox/approval_policy combination (including today's
# legacy production_write default of both None, and the explicit
# read-only profile) keeps the original fully-deny behavior below.
GOVERNED_REPO_WRITE_SANDBOX = "workspace-write"
GOVERNED_REPO_WRITE_APPROVAL = "never"


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


def _windows_npm_native_binary(executable: str) -> str | None:
    shim = Path(executable)
    if shim.stem.lower() != "codex" or shim.suffix.lower() not in (".cmd", ".bat"):
        return None
    machine = platform.machine().lower()
    targets = {"amd64": "x86_64-pc-windows-msvc", "x86_64": "x86_64-pc-windows-msvc",
               "arm64": "aarch64-pc-windows-msvc", "aarch64": "aarch64-pc-windows-msvc"}
    target = targets.get(machine)
    if target is None:
        return None
    package = "codex-win32-arm64" if target.startswith("aarch64") else "codex-win32-x64"
    codex_package = shim.parent / "node_modules" / "@openai" / "codex"
    for node_modules in (codex_package / "node_modules", shim.parent / "node_modules"):
        candidate = node_modules / "@openai" / package / "vendor" / target / "bin" / "codex.exe"
        if candidate.is_file():
            return str(candidate.resolve())
    return None


@dataclass(frozen=True)
class LaunchRequest:
    working_directory: str
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str | None = None
    approval_policy: str | None = None
    timeout_seconds: float = 30.0
    turn_timeout_seconds: float = 30.0


@dataclass
class PreparedLaunch:
    thread_id: str
    session_path: str | None
    pid: int
    process_creation_identity: str
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
    _heartbeat: Callable[[str], Any] | None = field(default=None, repr=False)
    _last_heartbeat: float = field(default=0.0, repr=False)
    _last_progress: float = field(default=0.0, repr=False)


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


def _turn_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_TURN_TIMEOUT_SECONDS:
        raise CodexLaunchError("invalid_request", f"turn_timeout_seconds must be within (0, {MAX_TURN_TIMEOUT_SECONDS:g}]")
    return float(value)


def _rpc_error(error: Any) -> str:
    if not isinstance(error, dict):
        return "app-server returned an error"
    code = error.get("code")
    message = error.get("message")
    safe_message = str(message)[:300] if isinstance(message, str) else "app-server returned an error"
    return f"code={code}; {safe_message}" if code is not None else safe_message


def _blocked_file_change_reason(turn: dict) -> str | None:
    """A `turn/completed` payload's own `status` field only reflects
    Codex's protocol-level view of the turn (see codex app-server's
    TurnStatus: completed/interrupted/failed/inProgress); it says nothing
    about whether the workspace edit this turn existed to make was actually
    denied along the way. A declined/failed FileChangeThreadItem
    (`turn.items[].type == "fileChange"`, `.status in
    ("declined", "failed")`, per the app-server's own PatchApplyStatus
    enum) is real structured evidence of exactly that -- Codex's own
    "decline" decision explicitly "continues the turn" rather than failing
    it, which is how a blocked edit can otherwise still surface as
    status="completed" here. Only fileChange items are inspected: a
    declined commandExecution/exec approval is this launcher's own
    deliberate, permanent policy (shell/escalated actions are never
    auto-approved, governed repo-write mode or not -- see
    _reply_to_server_request), not evidence the turn failed to do its job.
    """
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "fileChange":
            continue
        status = item.get("status")
        if status in ("declined", "failed"):
            return f"fileChange item {item.get('id')!r} ended with status {status!r}"[:MAX_ERROR_CHARS]
    return None


def _trusted_session_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return str(resolved)


class _AppServerClient:
    def __init__(self, process: Any, timeout: float, governed_repo_write: bool = False):
        self.process = process
        self.timeout = timeout
        self.governed_repo_write = governed_repo_write
        self._responses: dict[int, queue.Queue] = {}
        self._notifications: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._closed = False
        self._failure: CodexLaunchError | None = None
        self._stderr_tail = ""
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    self._fail(CodexLaunchError("protocol_error", "malformed JSON from app-server"))
                    break
                if not isinstance(message, dict):
                    self._fail(CodexLaunchError("protocol_error", "non-object JSON-RPC message"))
                    break
                if "id" in message and "method" not in message:
                    if ("result" in message) == ("error" in message):
                        self._fail(CodexLaunchError("protocol_error", "invalid JSON-RPC response envelope"))
                        break
                    request_id = message["id"]
                    with self._lock:
                        target = next((target for expected, target in self._responses.items()
                                       if type(request_id) is type(expected) and request_id == expected), None)
                    if target is None:
                        self._fail(CodexLaunchError("protocol_error", "response has an unknown request id"))
                        break
                    target.put(message)
                elif "id" in message and isinstance(message.get("method"), str) and message["method"]:
                    self._reply_to_server_request(message)
                elif "id" not in message and isinstance(message.get("method"), str) and message["method"]:
                    self._notifications.put(("notification", message))
                else:
                    self._fail(CodexLaunchError("protocol_error", "invalid JSON-RPC envelope"))
                    break
        finally:
            code = self.process.poll()
            classification = "process_exit" if code is not None else "stdout_eof"
            self._fail(CodexLaunchError(classification, f"app-server closed stdout; exit_code={code}"))

    def _reply_to_server_request(self, message: dict):
        """Deny unattended approval requests and reject unsupported callbacks.

        In governed repo-write mode (self.governed_repo_write, set only for a
        LaunchRequest carrying the exact GOVERNED_REPO_WRITE_SANDBOX/
        GOVERNED_REPO_WRITE_APPROVAL profile -- see manager.execution_runner,
        which only ever selects it for a Task that already independently
        proved repo_write_policy_satisfied()), a file-change/patch approval
        is the one request class this launcher may accept on Codex's behalf:
        it is exactly the "workspace edit" approval_policy="never" is meant
        to avoid ever having to ask for, the built-in "workspace-write"
        sandbox already confines it to the cwd this launcher itself chose
        (the Task's own isolated worktree), and manager.repo_write_
        enforcement independently re-verifies every actual changed path
        against the Task's admitted allowed_paths afterward regardless.
        Command-execution approvals are never auto-approved in any mode --
        shell/escalated actions are outside what a workspace file edit
        requires, so those keep being declined even here.
        """
        method = message["method"]
        decisions = {
            "item/commandExecution/requestApproval": {"decision": "decline"},
            "item/fileChange/requestApproval": {"decision": "decline"},
            "execCommandApproval": {"decision": {"denied": {"rejection": "unattended approval denied"}}},
            "applyPatchApproval": {"decision": {"denied": {"rejection": "unattended approval denied"}}},
        }
        if self.governed_repo_write:
            decisions["item/fileChange/requestApproval"] = {"decision": "accept"}
            decisions["applyPatchApproval"] = {"decision": "approved"}
        if method in decisions:
            response = {"id": message["id"], "result": decisions[method]}
        else:
            response = {"id": message["id"], "error": {
                "code": -32601, "message": "unsupported unattended server request",
            }}
        self.send(response)

    def _fail(self, failure: CodexLaunchError):
        with self._lock:
            if self._failure is not None:
                return
            self._failure = failure
            targets = list(self._responses.values())
        for target in targets:
            target.put(failure)
        self._notifications.put(("failure", failure))
        self.close()

    def raise_if_failed(self):
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def _drain_stderr(self):
        try:
            for chunk in self.process.stderr:
                self._stderr_tail = (self._stderr_tail + str(chunk))[-MAX_STDERR_CHARS:]
        except (OSError, ValueError):
            pass

    def send(self, message: dict):
        self.raise_if_failed()
        if self.process.poll() is not None:
            failure = CodexLaunchError("process_exit", f"app-server exited with code {self.process.returncode}")
            self._fail(failure)
            raise failure
        try:
            with self._write_lock:
                self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            failure = CodexLaunchError("transport_error", "could not write to app-server")
            self._fail(failure)
            raise failure from exc

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
            if isinstance(response, CodexLaunchError):
                raise response
            if "error" in response:
                raise CodexLaunchError("protocol_error", f"{method}: {_rpc_error(response['error'])}")
            if "result" not in response:
                raise CodexLaunchError("protocol_error", f"{method}: response has no result")
            self.raise_if_failed()
            return response["result"]
        finally:
            with self._lock:
                self._responses.pop(request_id, None)

    def next_event(self, timeout: float):
        self.raise_if_failed()
        try:
            event = self._notifications.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexLaunchError("timeout", "turn completion timed out") from exc
        self.raise_if_failed()
        return event

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

    def _spawn(self, timeout: float, governed_repo_write: bool = False) -> _AppServerClient:
        executable = resolve_codex_executable(self.executable)
        native_executable = _windows_npm_native_binary(executable)
        command = [native_executable or executable, "app-server"]
        if native_executable is None and executable.lower().endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", f'""{executable}" app-server"']
        try:
            process = self._popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as exc:
            raise CodexLaunchError("spawn_failed", "failed to start Codex app-server") from exc
        return _AppServerClient(process, timeout, governed_repo_write=governed_repo_write)

    def prepare(self, request: LaunchRequest) -> PreparedLaunch:
        timeout = _timeout(request.timeout_seconds)
        _turn_timeout(request.turn_timeout_seconds)
        cwd = Path(request.working_directory)
        if not cwd.is_absolute() or not cwd.is_dir():
            raise CodexLaunchError("invalid_request", "working_directory must be an existing absolute directory")
        governed_repo_write = (
            request.sandbox == GOVERNED_REPO_WRITE_SANDBOX and request.approval_policy == GOVERNED_REPO_WRITE_APPROVAL
        )
        client = self._spawn(timeout, governed_repo_write)
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
            if not isinstance(thread_id, str) or not thread_id.strip():
                raise CodexLaunchError("protocol_error", "thread/start returned no thread id")
            session_path = _trusted_session_path(thread.get("path"))
            client.raise_if_failed()
            pid = process_pid(client.process)
            creation_identity = process_creation_identity(pid)
            if creation_identity is None:
                raise CodexLaunchError("process_identity_unavailable", "could not verify app-server process creation identity")
            return PreparedLaunch(thread_id, session_path, pid, creation_identity, utc_now(), client, request)
        except Exception:
            client.close()
            raise

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        prepared._client.raise_if_failed()
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
        deadline = time.monotonic() + _turn_timeout(running.prepared._request.turn_timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexLaunchError("timeout", "turn completion timed out")
            try:
                kind, payload = client.next_event(min(remaining, HEARTBEAT_INTERVAL_SECONDS))
            except CodexLaunchError as exc:
                if exc.classification == "timeout" and time.monotonic() < deadline:
                    self._emit_heartbeat(running, "provider_wait")
                    continue
                raise
            if kind == "failure":
                raise payload
            self._emit_heartbeat(running, "provider_event")
            if not isinstance(payload, dict) or payload.get("method") != "turn/completed":
                continue
            params = payload.get("params")
            turn = params.get("turn") if isinstance(params, dict) else None
            if not isinstance(turn, dict) or params.get("threadId") != running.prepared.thread_id or turn.get("id") != running.turn_id:
                continue
            status = turn.get("status")
            if status == "completed":
                if client.governed_repo_write:
                    blocked = _blocked_file_change_reason(turn)
                    if blocked is not None:
                        return LaunchOutcome("failed", running.prepared.thread_id, running.turn_id, utc_now(),
                                             "file_change_blocked", blocked)
                return LaunchOutcome("completed", running.prepared.thread_id, running.turn_id, utc_now())
            error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
            detail = str(error.get("message") or f"turn ended with status {status}")[:MAX_ERROR_CHARS]
            classification = "cancelled" if running._cancelled and status == "interrupted" else "turn_failed"
            return LaunchOutcome(status if status in ("failed", "interrupted") else "failed", running.prepared.thread_id, running.turn_id, utc_now(), classification, detail)

    def set_heartbeat(self, running: RunningLaunch, callback: Callable[[str], Any]):
        running._heartbeat = callback
        running._last_heartbeat = time.monotonic()
        running._last_progress = running._last_heartbeat

    @staticmethod
    def _emit_heartbeat(running: RunningLaunch, event: str):
        now = time.monotonic()
        clock = "_last_progress" if event == "provider_event" else "_last_heartbeat"
        if running._heartbeat and now - getattr(running, clock) >= HEARTBEAT_INTERVAL_SECONDS:
            running._heartbeat(event)
            setattr(running, clock, now)

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


def process_creation_identity(pid: int) -> str | None:
    """Return a boot-scoped OS process creation identity, or None when it cannot be proven."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        class FILETIME(ctypes.Structure):
            _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = (ctypes.c_void_p, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                             ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return f"windows-filetime:{(created.high << 32) | created.low}"
        finally:
            kernel32.CloseHandle(handle)
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        start_ticks = stat[stat.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, UnicodeError, ValueError):
        return None
    return f"linux-proc:{boot_id}:{start_ticks}" if boot_id and start_ticks.isdigit() else None


def process_identity_state(pid, expected_identity):
    """Verify a specific (pid, expected creation identity) pair is still the
    same live OS process -- shared by Recovery's provider-liveness check and
    Session Center's supervisor, so PID-reuse handling is implemented once.

    Returns one of:
    - "unknown": malformed input, or existence/identity could not be proven.
      Never a safe substitute for "live" -- callers must not treat it as
      healthy, and must not kill a PID they could not verify.
    - "stopped": the PID does not currently identify a running process.
    - "live": the PID exists and its creation identity matches exactly.
    - "replaced": the PID exists but belongs to a different process now
      (the original one exited and the PID was reused).
    """
    if not isinstance(pid, int) or pid <= 0 or not isinstance(expected_identity, str) or not expected_identity:
        return "unknown"
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            queried = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            if not queried:
                return "unknown"
            if exit_code.value != 259:
                return "stopped"
        else:
            return "stopped" if kernel32.GetLastError() == 87 else "unknown"
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "stopped"
        except (OSError, PermissionError):
            return "unknown"
    current_identity = process_creation_identity(pid)
    if current_identity is None:
        return "unknown"
    return "live" if current_identity == expected_identity else "replaced"
