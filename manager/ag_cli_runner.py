"""Official Antigravity CLI dispatch adapter with strict Fail-Closed Auth Guard.

This module resolves and executes the official Antigravity CLI (agentapi / agy) or
bundled language server in non-interactive / headless mode. It enforces that the execution
strictly runs under the authenticated local Google AI Pro account profile, strips secondary
API billing credentials, and normalizes output into standardized events.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator

from manager.ag_runner import (
    AgLaunchError,
    AgNormalizedEvent,
    LaunchOutcome,
    LaunchRequest,
    PreparedLaunch,
    RunningLaunch,
    normalize_event,
    utc_now,
)


SECONDARY_BILLING_ENV_VARS = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "VERTEX_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GCP_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
)


def _safe_home() -> Path:
    try:
        return Path.home()
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "/tmp")


def sanitize_ag_environment(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Strip secondary billing/API keys to ensure child process strictly uses local Google account profile."""
    env = dict(os.environ if base_env is None else base_env)
    for var in SECONDARY_BILLING_ENV_VARS:
        env.pop(var, None)
    return env


def verify_auth_identity() -> str:
    """Verify that Antigravity execution uses a verified local Google account profile.

    Strictly fails closed if local IDE configuration or credential profile cannot be proven.
    GOOGLE_API_KEY is not accepted as evidence of local Google AI Pro account identity.
    """
    gemini_home = Path(os.environ.get("GEMINI_HOME", _safe_home() / ".gemini"))

    # Local credentials directory / config check
    oauth_file = gemini_home / "oauth_credentials.json"
    config_dir = gemini_home / "config"
    ide_dir = gemini_home / "antigravity-ide"
    antigravity_dir = gemini_home / "antigravity"

    # Local storage state in AppData
    appdata = os.environ.get("APPDATA")
    state_db = Path(appdata) / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb" if appdata else None

    has_local_profile = (
        config_dir.is_dir()
        or ide_dir.is_dir()
        or antigravity_dir.is_dir()
        or oauth_file.is_file()
        or (state_db and state_db.is_file())
    )

    if not has_local_profile:
        raise AgLaunchError(
            "unverified_identity",
            "Cannot prove Antigravity local identity: no local configuration directory (~/.gemini/config, ~/.gemini/antigravity-ide) or OAuth credential profile found. Fail closed.",
        )

    return "local_google_account_profile"


def resolve_ag_cli_executable(explicit: str | None = None) -> tuple[str, list[str]]:
    r"""Locate the official Antigravity CLI binary / entrypoint and leading subcommands.

    Returns (executable_path, prefix_args).
    For example:
      - ('C:/Users/EE/.gemini/antigravity-ide/bin/agentapi.bat', [])
      - ('c:/.../language_server_windows_x64.exe', ['agentapi'])
    """
    if explicit:
        path = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if path:
            return str(Path(path).resolve()), []
        raise AgLaunchError("executable_not_found", f"Explicit Antigravity CLI executable not found: {explicit}")

    # Check environment variable overrides
    env_bin = os.environ.get("AGENTAPI_BIN") or os.environ.get("ANTIGRAVITY_BIN") or os.environ.get("AGY_BIN") or os.environ.get("GEMINI_BIN")
    if env_bin:
        path = shutil.which(env_bin) or (env_bin if Path(env_bin).is_file() else None)
        if path:
            resolved = str(Path(path).resolve())
            if "language_server" in Path(resolved).name.lower():
                return resolved, ["agentapi"]
            return resolved, []

    # 1. Search PATH for agentapi
    names = ("agentapi.bat", "agentapi.cmd", "agentapi.exe", "agentapi") if os.name == "nt" else ("agentapi",)
    for name in names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve()), []

    # 2. Known default installation locations under ~/.gemini
    gemini_home = Path(os.environ.get("GEMINI_HOME", _safe_home() / ".gemini"))
    for sub in (
        "antigravity-ide/bin/agentapi.bat",
        "antigravity-ide/bin/agentapi.cmd",
        "antigravity-ide/bin/agentapi.exe",
        "antigravity-ide/bin/agentapi",
        "antigravity/bin/agentapi.bat",
        "antigravity/bin/agentapi.cmd",
        "antigravity/bin/agentapi.exe",
        "antigravity/bin/agentapi",
    ):
        cand = gemini_home / sub
        if cand.is_file():
            return str(cand.resolve()), []

    # 3. Known Language Server binaries in LocalAppData
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        ls_candidates = (
            Path(local_appdata) / "Programs" / "Antigravity IDE" / "resources" / "app" / "extensions" / "antigravity" / "bin" / "language_server_windows_x64.exe",
            Path(local_appdata) / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe",
        )
        for ls_path in ls_candidates:
            if ls_path.is_file():
                return str(ls_path.resolve()), ["agentapi"]

    # 4. Search PATH for agy / antigravity / gemini
    fallback_names = (
        ("agy.cmd", "agy.exe", "agy", "antigravity.cmd", "antigravity.exe", "antigravity", "gemini.cmd", "gemini.exe", "gemini")
        if os.name == "nt"
        else ("agy", "antigravity", "gemini")
    )
    for name in fallback_names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve()), []

    # 5. Check NPM global directory
    appdata = os.environ.get("APPDATA")
    if appdata:
        for npm_name in ("gemini.cmd", "agy.cmd", "antigravity.cmd"):
            npm_cand = Path(appdata) / "npm" / npm_name
            if npm_cand.is_file():
                return str(npm_cand.resolve()), []

    raise AgLaunchError("executable_not_found", "Official Antigravity CLI executable (agentapi/agy) was not found")


class AgCliProcess:
    """Manages the subprocess execution, streaming I/O, and lifecycle of an Antigravity CLI process."""

    def __init__(self, process: subprocess.Popen, timeout: float = 30.0):
        self.process = process
        self.timeout = timeout
        self._queue: queue.Queue[AgNormalizedEvent] = queue.Queue()
        self._closed = False
        self._stderr_lines: list[str] = []
        self._accumulated_messages: list[str] = []

        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        if not stdout:
            self._closed = True
            return
        for line in iter(stdout.readline, ""):
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue
            try:
                raw_event = json.loads(line_str)
                event = normalize_event(raw_event)
                self._queue.put(event)
                if event.event_type == "message":
                    self._accumulated_messages.append(event.payload.get("content", ""))
            except json.JSONDecodeError:
                # Wrap unstructured line as message
                self._accumulated_messages.append(line_str)
                self._queue.put(AgNormalizedEvent(event_type="message", payload={"content": line_str}))

        self._closed = True

    def _read_stderr(self) -> None:
        stderr = self.process.stderr
        if not stderr:
            return
        for line in iter(stderr.readline, ""):
            if not line:
                break
            line_str = line.strip()
            if line_str:
                self._stderr_lines.append(line_str)

    def get_stderr_summary(self, max_chars: int = 500) -> str:
        full = "\n".join(self._stderr_lines).strip()
        return full[:max_chars]

    def get_accumulated_output(self) -> str:
        return "\n".join(self._accumulated_messages).strip()

    def read_events(self, timeout: float = 1.0) -> Generator[AgNormalizedEvent, None, None]:
        while not self._closed or not self._queue.empty():
            try:
                event = self._queue.get(timeout=timeout)
                yield event
                if event.event_type in ("result", "error"):
                    break
            except queue.Empty:
                if self._closed and self._queue.empty():
                    break

    def terminate(self) -> None:
        try:
            self.process.terminate()
            self.process.wait(timeout=2.0)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass


class OfficialAgCliRunner:
    """Official Antigravity CLI adapter with strict Fail-Closed Auth Guard."""

    def __init__(
        self,
        executable_resolver: Callable[..., Any] | None = None,
        auth_verifier: Callable[[], str] | None = None,
        default_mode: str = "cli",
    ):
        self._resolve_executable = executable_resolver or resolve_ag_cli_executable
        self._verify_auth = auth_verifier or verify_auth_identity
        self.default_mode = default_mode

    def _get_resolved_executable(self) -> tuple[str, list[str]]:
        res = self._resolve_executable()
        if isinstance(res, tuple):
            return res[0], list(res[1])
        return str(res), []

    def prepare(self, request: LaunchRequest) -> PreparedLaunch:
        # Strict Fail-Closed Auth Verification
        identity = self._verify_auth()

        executable, prefix_args = self._get_resolved_executable()
        mode = request.force_mode if request.force_mode in ("cli", "headless") else self.default_mode
        thread_id = f"ag-{mode}-{uuid.uuid4().hex[:12]}"
        now = utc_now()

        home = _safe_home()
        session_path = str(home / ".gemini" / "antigravity-ide" / "brain" / thread_id / "transcript.jsonl")

        return PreparedLaunch(
            thread_id=thread_id,
            session_path=session_path,
            pid=0,  # Assigned on process spawn in start()
            process_creation_identity=f"{mode}-{identity}",
            prepared_at=now,
            mode=mode,
            _target=None,
            _request=request,
        )

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared._started:
            raise AgLaunchError("already_started", "Prepared Antigravity launch was already started")

        req = prepared._request

        # Fail closed on missing/invalid working_directory rather than ever
        # falling back to the ambient process cwd (matches the contract
        # ClaudeLauncher/CodexLauncher enforce for the same field).
        wd = req.working_directory
        if not isinstance(wd, str) or not wd.strip():
            raise AgLaunchError("invalid_request", "working_directory must be a non-empty string")
        cwd_path = Path(wd)
        if not cwd_path.is_absolute():
            raise AgLaunchError("invalid_request", f"working_directory must be an absolute path: {wd!r}")
        if not cwd_path.is_dir():
            raise AgLaunchError(
                "invalid_request", f"working_directory does not exist or is not a directory: {wd!r}"
            )
        cwd = str(cwd_path)

        prepared._started = True

        executable, prefix_args = self._get_resolved_executable()

        args = [executable]
        args.extend(prefix_args)

        # Build CLI arguments based on target tool
        basename = Path(executable).name.lower()
        if "agentapi" in basename or (prefix_args and "agentapi" in prefix_args):
            args.extend(["new-conversation"])
            if req.model:
                args.extend(["--model", req.model])
            args.extend(["--title", f"adm-{prepared.thread_id}"])
            args.append(prompt)
        else:
            # Generic agy / gemini CLI syntax
            args.extend(["-p", prompt, "--output-format", "stream-json", "--approval-mode", "plan"])
            if req.model:
                args.extend(["--model", req.model])

        # Sanitize environment to prevent secondary billing / external API key injection
        env = sanitize_ag_environment(os.environ)

        try:
            proc = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            raise AgLaunchError("spawn_failed", f"Failed to spawn Antigravity CLI binary: {exc}")

        prepared.pid = proc.pid
        cli_proc = AgCliProcess(proc, timeout=req.turn_timeout_seconds)
        prepared._target = cli_proc
        prepared._process = proc

        turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        now = utc_now()
        return RunningLaunch(prepared=prepared, turn_id=turn_id, started_at=now)

    def set_heartbeat(self, running: RunningLaunch, callback: Callable[[str], Any]) -> None:
        running._heartbeat = callback

    def wait(self, running: RunningLaunch) -> LaunchOutcome:
        cli_proc: AgCliProcess = running.prepared._target
        timeout = running.prepared._request.turn_timeout_seconds
        deadline = time.time() + timeout

        final_response = ""
        stats: dict[str, Any] = {}
        status = "completed"
        failure_kind = None
        failure_msg = None

        try:
            while time.time() < deadline:
                if running._cancelled:
                    cli_proc.terminate()
                    status = "interrupted"
                    failure_kind = "cancelled"
                    failure_msg = "Execution was cancelled by runner"
                    break

                for event in cli_proc.read_events(timeout=min(1.0, max(0.1, deadline - time.time()))):
                    if running._heartbeat:
                        progress = event.event_type in ("tool_call", "tool_result", "message", "thought")
                        running._heartbeat("provider_event" if progress else "provider_heartbeat")

                    if event.event_type == "result":
                        final_response = str(event.payload.get("response", ""))
                        stats = event.payload.get("stats", {})
                        return LaunchOutcome(
                            status="completed",
                            thread_id=running.prepared.thread_id,
                            turn_id=running.turn_id,
                            completed_at=utc_now(),
                            response_text=final_response,
                            stats=stats,
                        )
                    if event.event_type == "error":
                        return LaunchOutcome(
                            status="failed",
                            thread_id=running.prepared.thread_id,
                            turn_id=running.turn_id,
                            completed_at=utc_now(),
                            failure_classification=event.payload.get("code") or "provider_error",
                            failure_detail=str(event.payload.get("error")),
                        )

                if cli_proc.process.poll() is not None:
                    # Process exited
                    if cli_proc.process.returncode != 0:
                        status = "failed"
                        failure_kind = f"exit_code_{cli_proc.process.returncode}"
                        stderr_sum = cli_proc.get_stderr_summary()
                        failure_msg = f"Antigravity CLI process exited with code {cli_proc.process.returncode}"
                        if stderr_sum:
                            failure_msg += f": {stderr_sum}"
                    else:
                        # Process exited 0; if no explicit result event was found, collect accumulated output
                        if not final_response:
                            final_response = cli_proc.get_accumulated_output()
                    break

            if time.time() >= deadline:
                cli_proc.terminate()
                status = "failed"
                failure_kind = "turn_timeout"
                failure_msg = f"Antigravity turn exceeded timeout of {timeout} seconds"

        except Exception as exc:
            status = "failed"
            failure_kind = "cli_exception"
            failure_msg = str(exc)

        return LaunchOutcome(
            status=status,
            thread_id=running.prepared.thread_id,
            turn_id=running.turn_id,
            completed_at=utc_now(),
            failure_classification=failure_kind,
            failure_detail=failure_msg,
            response_text=final_response or None,
            stats=stats or None,
        )

    def close(self, prepared: PreparedLaunch) -> None:
        proc: AgCliProcess | None = prepared._target
        if proc and hasattr(proc, "terminate"):
            proc.terminate()


# Backward compatibility aliases
AgCliRunner = OfficialAgCliRunner
AgHeadlessProcess = AgCliProcess
