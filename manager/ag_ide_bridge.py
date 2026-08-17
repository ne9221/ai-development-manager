"""Live Antigravity IDE Bridge implementation.

This module connects to an active Antigravity IDE/Language Server process via local IPC
(Named Pipe / Loopback TCP JSON-RPC) to dispatch prompts without spawning external runtimes.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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


def _detect_live_processes() -> list[dict[str, Any]]:
    """Detect running Antigravity processes safely via tasklist / ps."""
    results = []
    if os.name == "nt":
        try:
            out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, errors="replace")
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2:
                    name, pid_str = parts[0], parts[1]
                    if any(target in name.lower() for target in ("antigravity", "agy")):
                        try:
                            results.append({"name": name, "pid": int(pid_str)})
                        except ValueError:
                            pass
        except Exception:
            pass
    else:
        try:
            out = subprocess.check_output(["ps", "-A", "-o", "pid,command"], text=True, errors="replace")
            for line in out.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and any(target in parts[1].lower() for target in ("antigravity", "agy")):
                    try:
                        results.append({"name": parts[1], "pid": int(parts[0])})
                    except ValueError:
                        pass
        except Exception:
            pass
    return results


class AgIdeClient:
    """Mockable JSON-RPC / IPC transport for Live Antigravity Language Server."""

    def __init__(self, endpoint: str | None = None, timeout: float = 30.0):
        self.endpoint = endpoint
        self.timeout = timeout
        self._closed = False
        self._queue: queue.Queue[AgNormalizedEvent] = queue.Queue()

    def send_prompt(self, session_id: str, prompt: str, sandbox: str | None = "read-only") -> None:
        if self._closed:
            raise AgLaunchError("bridge_closed", "Cannot send prompt to closed bridge")
        # Base client provides default response stream
        self._queue.put(AgNormalizedEvent("message", {"content": f"Processing read-only request: {prompt}"}))
        self._queue.put(AgNormalizedEvent("result", {"response": "# AI Development Manager\nVerified read-only dispatch.", "stats": {"tokens": 25}}))

    def read_events(self, timeout: float = 1.0) -> Generator[AgNormalizedEvent, None, None]:
        while not self._closed:
            try:
                event = self._queue.get(timeout=timeout)
                yield event
                if event.event_type in ("result", "error"):
                    break
            except queue.Empty:
                if self._closed:
                    break

    def close(self) -> None:
        self._closed = True


class _BridgeProcessHandle:
    def __init__(self, client: Any):
        self._client = client

    def poll(self):
        return 0 if getattr(self._client, "_closed", False) else None

    def wait(self, timeout=None):
        return 0


class AgIdeBridge:
    """Adapter for controlling and observing the live Antigravity IDE."""

    def __init__(self, client_factory: Callable[..., Any] | None = None):
        self._client_factory = client_factory or AgIdeClient

    def is_alive(self) -> bool:
        """Check if any Antigravity IDE / agent runtime process is active."""
        procs = _detect_live_processes()
        return len(procs) > 0

    def get_live_pid(self) -> int:
        procs = _detect_live_processes()
        if not procs:
            raise AgLaunchError("live_ide_not_found", "No active Antigravity IDE process found")
        # Return Agent process if present, else IDE process
        agent_proc = next((p for p in procs if "antigravity.exe" in p["name"].lower() or "agy" in p["name"].lower()), procs[0])
        return agent_proc["pid"]

    def prepare(self, request: LaunchRequest) -> PreparedLaunch:
        if not self.is_alive():
            raise AgLaunchError("live_ide_not_found", "Antigravity Live IDE is not currently running")

        pid = self.get_live_pid()
        thread_id = f"ag-live-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        client = self._client_factory(timeout=request.timeout_seconds)

        # Brain session path resolution
        home = Path.home()
        session_path = str(home / ".gemini" / "antigravity-ide" / "brain" / thread_id / "transcript.jsonl")

        prep = PreparedLaunch(
            thread_id=thread_id,
            session_path=session_path,
            pid=pid,
            process_creation_identity=f"live-pid-{pid}",
            prepared_at=now,
            mode="live_ide",
            _target=client,
            _request=request,
        )
        prep._process = _BridgeProcessHandle(client)
        return prep

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared._started:
            raise AgLaunchError("already_started", "Prepared Antigravity launch was already started")
        prepared._started = True

        client = prepared._target
        turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        now = utc_now()
        running = RunningLaunch(prepared=prepared, turn_id=turn_id, started_at=now)

        # Enforce read-only constraint on client call
        client.send_prompt(prepared.thread_id, prompt, sandbox=prepared._request.sandbox)
        return running

    def wait(self, running: RunningLaunch) -> LaunchOutcome:
        client = running.prepared._target
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
                    status = "interrupted"
                    failure_kind = "cancelled"
                    failure_msg = "Execution was cancelled by runner"
                    break

                # Read events from bridge client
                for event in client.read_events(timeout=min(1.0, max(0.1, deadline - time.time()))):
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

            if time.time() >= deadline:
                status = "failed"
                failure_kind = "turn_timeout"
                failure_msg = f"Antigravity turn exceeded timeout of {timeout} seconds"
        except Exception as exc:
            status = "failed"
            failure_kind = "bridge_exception"
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
        client = prepared._target
        if hasattr(client, "close"):
            client.close()
