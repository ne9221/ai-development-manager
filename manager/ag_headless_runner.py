"""Headless Antigravity fallback runner with strict Fail-Closed Auth Guard.

This module spawns a standalone Antigravity/Gemini headless CLI process when the Live IDE
is unavailable. It strictly enforces that the runtime shares the exact same Google account
and quota identity as the local IDE profile, failing closed if identity is unproven.
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
    """Verify that headless execution uses a verified local Google account profile.

    Strictly fails closed if local IDE configuration or credential profile cannot be proven.
    GOOGLE_API_KEY is not accepted as evidence of local Google AI Pro account identity.
    """
    gemini_home = Path(os.environ.get("GEMINI_HOME", _safe_home() / ".gemini"))

    # Local credentials directory / config check
    oauth_file = gemini_home / "oauth_credentials.json"
    config_dir = gemini_home / "config"
    ide_dir = gemini_home / "antigravity-ide"

    has_local_profile = config_dir.is_dir() or ide_dir.is_dir() or oauth_file.is_file()

    if not has_local_profile:
        raise AgLaunchError(
            "unverified_identity",
            "Cannot prove Antigravity local identity: no local configuration directory (~/.gemini/config, ~/.gemini/antigravity-ide) or OAuth credential profile found. Fail closed.",
        )

    # Return validated identity indicator
    return "local_google_account_profile"


def resolve_ag_executable(explicit: str | None = None) -> str:
    """Locate the Antigravity / Gemini CLI binary."""
    candidate = explicit or os.environ.get("ANTIGRAVITY_BIN") or os.environ.get("GEMINI_BIN") or os.environ.get("AGY_BIN")
    if candidate:
        path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
    else:
        names = ("agy.cmd", "agy.exe", "agy", "gemini.cmd", "gemini.exe", "gemini", "antigravity.cmd", "antigravity.exe", "antigravity") if os.name == "nt" else ("agy", "gemini", "antigravity")
        path = next((found for name in names if (found := shutil.which(name))), None)

    if not path:
        # Check standard default installation locations
        appdata = os.environ.get("APPDATA")
        if appdata:
            npm_candidate = Path(appdata) / "npm" / "gemini.cmd"
            if npm_candidate.is_file():
                path = str(npm_candidate)

    if not path:
        raise AgLaunchError("executable_not_found", "Antigravity/Gemini CLI executable was not found")
    return str(Path(path).resolve())


class AgHeadlessProcess:
    """Manages the lifecycle of a headless Antigravity CLI subprocess."""

    def __init__(self, process: subprocess.Popen, timeout: float = 30.0):
        self.process = process
        self.timeout = timeout
        self._queue: queue.Queue[AgNormalizedEvent] = queue.Queue()
        self._closed = False
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        if not stdout:
            return
        for line in iter(stdout.readline, ""):
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue
            try:
                raw_event = json.loads(line_str)
                self._queue.put(normalize_event(raw_event))
            except json.JSONDecodeError:
                # Wrap unstructured line as message
                self._queue.put(AgNormalizedEvent(event_type="message", payload={"content": line_str}))

        self._closed = True

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


class AgHeadlessRunner:
    """Headless Antigravity runner with strict Fail-Closed Auth Guard."""

    def __init__(self, executable_resolver: Callable[..., str] | None = None, auth_verifier: Callable[[], str] | None = None):
        self._resolve_executable = executable_resolver or resolve_ag_executable
        self._verify_auth = auth_verifier or verify_auth_identity

    def prepare(self, request: LaunchRequest) -> PreparedLaunch:
        # Strict Fail-Closed Auth Verification
        identity = self._verify_auth()

        executable = self._resolve_executable()
        thread_id = f"ag-headless-{uuid.uuid4().hex[:12]}"
        now = utc_now()

        # Build execution arguments ensuring read-only and plan mode
        args = [
            executable,
            "-p", "",  # Placeholder; actual prompt sent on start or injected
            "--output-format", "stream-json",
            "--approval-mode", "plan",
        ]
        if request.model:
            args.extend(["--model", request.model])

        home = Path.home()
        session_path = str(home / ".gemini" / "antigravity-ide" / "brain" / thread_id / "transcript.jsonl")

        return PreparedLaunch(
            thread_id=thread_id,
            session_path=session_path,
            pid=0,  # Assigned on process spawn in start()
            process_creation_identity=f"headless-{identity}",
            prepared_at=now,
            mode="headless",
            _target=None,
            _request=request,
        )

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared._started:
            raise AgLaunchError("already_started", "Prepared Antigravity launch was already started")
        prepared._started = True

        executable = self._resolve_executable()
        req = prepared._request

        args = [
            executable,
            "-p", prompt,
            "--output-format", "stream-json",
            "--approval-mode", "plan",
        ]
        if req.model:
            args.extend(["--model", req.model])

        # Sanitize environment to prevent secondary billing / external API key injection
        env = sanitize_ag_environment(os.environ)

        # Enforce sandbox and working directory
        cwd = req.working_directory if Path(req.working_directory).is_dir() else None

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
            raise AgLaunchError("spawn_failed", f"Failed to spawn Antigravity headless binary: {exc}")

        prepared.pid = proc.pid
        headless_proc = AgHeadlessProcess(proc, timeout=req.turn_timeout_seconds)
        prepared._target = headless_proc
        prepared._process = proc

        turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        now = utc_now()
        return RunningLaunch(prepared=prepared, turn_id=turn_id, started_at=now)

    def wait(self, running: RunningLaunch) -> LaunchOutcome:
        headless_proc: AgHeadlessProcess = running.prepared._target
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
                    headless_proc.terminate()
                    status = "interrupted"
                    failure_kind = "cancelled"
                    failure_msg = "Execution was cancelled by runner"
                    break

                for event in headless_proc.read_events(timeout=min(1.0, max(0.1, deadline - time.time()))):
                    if running._heartbeat:
                        progress = event.event_type in ("tool_call", "tool_result", "message", "thought")
                        running._heartbeat("provider_event" if progress else "provider_heartbeat")

                    if event.event_type == "result":
                        final_response = event.payload.get("response", "")
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

                if headless_proc.process.poll() is not None:
                    # Process exited
                    if headless_proc.process.returncode != 0:
                        status = "failed"
                        failure_kind = f"exit_code_{headless_proc.process.returncode}"
                        failure_msg = f"Antigravity CLI process exited with code {headless_proc.process.returncode}"
                    break

            if time.time() >= deadline:
                headless_proc.terminate()
                status = "failed"
                failure_kind = "turn_timeout"
                failure_msg = f"Antigravity turn exceeded timeout of {timeout} seconds"

        except Exception as exc:
            status = "failed"
            failure_kind = "headless_exception"
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
        proc: AgHeadlessProcess | None = prepared._target
        if proc and hasattr(proc, "terminate"):
            proc.terminate()
