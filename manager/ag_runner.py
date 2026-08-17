"""Antigravity direct dispatch execution facade and event models.

This module provides normalized event translation, outcome dataclasses, and the AgRunner
facade which automatically negotiates between Live IDE, Official CLI, and Headless execution.
It does not contain IDE-specific IPC or low-level subprocess transport implementations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generator


MAX_TIMEOUT_SECONDS = 300.0
MAX_TURN_TIMEOUT_SECONDS = 7200.0
MAX_ERROR_CHARS = 1000

# Route identity constants (execution evidence attribution). These are
# distinct concepts and must never be collapsed into one another:
#  - AG_OFFICIAL_CLI: a verified standalone `agy` executable only.
#  - AG_LIVE_IDE_IPC: the live Antigravity IDE / Language Server IPC bridge.
#  - GEMINI_CLI_FALLBACK: agentapi, the bundled language_server+agentapi
#    combo, or the generic `gemini` CLI -- legitimate fallback tooling, but
#    never the verified official CLI.
ROUTE_OFFICIAL_CLI = "AG_OFFICIAL_CLI"
ROUTE_LIVE_IDE_IPC = "AG_LIVE_IDE_IPC"
ROUTE_GEMINI_CLI_FALLBACK = "GEMINI_CLI_FALLBACK"

MODE_TO_ROUTE = {
    "cli": ROUTE_OFFICIAL_CLI,
    "headless": ROUTE_GEMINI_CLI_FALLBACK,
    "live_ide": ROUTE_LIVE_IDE_IPC,
}


class AgLaunchError(RuntimeError):
    """Bounded Antigravity provider launch failure."""

    def __init__(self, classification: str, detail: str):
        self.classification = classification
        self.detail = str(detail)[:MAX_ERROR_CHARS]
        super().__init__(f"{classification}: {self.detail}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class LaunchRequest:
    working_directory: str
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str | None = "read-only"
    approval_policy: str | None = "plan"
    timeout_seconds: float = 30.0
    turn_timeout_seconds: float = 30.0
    force_mode: str | None = None  # None (auto hybrid), "live_ide", "cli", or "headless"


@dataclass
class PreparedLaunch:
    thread_id: str
    session_path: str | None
    pid: int
    process_creation_identity: str
    prepared_at: str
    mode: str  # "live_ide", "cli", or "headless"
    _target: Any = field(repr=False)
    _request: LaunchRequest = field(repr=False)
    _started: bool = field(default=False, repr=False)
    # Execution evidence: which route/runner actually served this launch, and
    # whether a fallback transition occurred getting here. Never left
    # defaulted-but-misleading -- callers that build PreparedLaunch directly
    # are expected to set route_used/actual_runner explicitly.
    route_used: str = ""
    actual_runner: str = ""
    fallback_occurred: bool = False
    fallback_reason: str | None = None


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
    status: str  # "completed", "failed", "interrupted"
    thread_id: str
    turn_id: str
    completed_at: str
    failure_classification: str | None = None
    failure_detail: str | None = None
    response_text: str | None = None
    stats: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgNormalizedEvent:
    event_type: str  # "init", "thought", "tool_call", "tool_result", "message", "result", "error"
    payload: dict[str, Any]
    timestamp: str = field(default_factory=utc_now)


def normalize_event(raw_event: dict[str, Any]) -> AgNormalizedEvent:
    """Normalize vendor/transport-specific events into standardized AgNormalizedEvent."""
    if not isinstance(raw_event, dict):
        return AgNormalizedEvent(event_type="error", payload={"detail": "malformed event"})

    kind = raw_event.get("type") or raw_event.get("event")
    if kind in ("init", "session_init", "session/create"):
        return AgNormalizedEvent(
            event_type="init",
            payload={"session_id": raw_event.get("session_id") or raw_event.get("id") or raw_event.get("sessionId")},
        )
    if kind in ("thought", "reasoning"):
        return AgNormalizedEvent(
            event_type="thought",
            payload={"thought": raw_event.get("thought") or raw_event.get("content") or ""},
        )
    if kind in ("tool_call", "tool_use"):
        return AgNormalizedEvent(
            event_type="tool_call",
            payload={
                "tool": raw_event.get("tool") or raw_event.get("name"),
                "args": raw_event.get("args") or raw_event.get("arguments") or {},
            },
        )
    if kind in ("tool_result", "tool_response"):
        return AgNormalizedEvent(
            event_type="tool_result",
            payload={"output": raw_event.get("output") or raw_event.get("result")},
        )
    if kind in ("message", "response_item"):
        msg = raw_event.get("message") or raw_event
        return AgNormalizedEvent(
            event_type="message",
            payload={
                "role": msg.get("role", "assistant"),
                "content": msg.get("content") or raw_event.get("content") or "",
            },
        )
    if kind in ("result", "turn_result", "completed"):
        return AgNormalizedEvent(
            event_type="result",
            payload={
                "response": raw_event.get("response") or raw_event.get("output") or "",
                "stats": raw_event.get("stats") or {},
            },
        )
    if kind in ("error", "failure"):
        return AgNormalizedEvent(
            event_type="error",
            payload={
                "error": raw_event.get("error") or raw_event.get("message") or "unknown provider error",
                "code": raw_event.get("code"),
            },
        )
    # Check for structured dictionary output: {"error": "..."} or {"response": "..."}
    if "error" in raw_event and raw_event["error"]:
        return AgNormalizedEvent(
            event_type="error",
            payload={
                "error": raw_event["error"],
                "code": raw_event.get("code") or "provider_error",
            },
        )
    if "response" in raw_event:
        return AgNormalizedEvent(
            event_type="result",
            payload={
                "response": raw_event["response"],
                "stats": raw_event.get("stats") or {},
            },
        )
    # Default fallback
    return AgNormalizedEvent(event_type="message", payload={"content": str(raw_event)})


class AgRunner:
    """Facade and router for Antigravity live, CLI, and headless execution."""

    def __init__(self, ide_bridge: Any = None, headless_runner: Any = None, cli_runner: Any = None):
        self._ide_bridge = ide_bridge
        self._headless_runner = headless_runner
        self._cli_runner = cli_runner
        self.last_fallback_reason: str | None = None

    def _get_ide_bridge(self):
        if self._ide_bridge is None:
            from manager.ag_ide_bridge import AgIdeBridge
            self._ide_bridge = AgIdeBridge()
        return self._ide_bridge

    def _get_cli_runner(self):
        if self._cli_runner is None:
            from manager.ag_cli_runner import OfficialAgCliRunner
            self._cli_runner = OfficialAgCliRunner()
        return self._cli_runner

    def _get_headless_runner(self):
        if self._headless_runner is None:
            from manager.ag_headless_runner import AgHeadlessRunner
            self._headless_runner = AgHeadlessRunner()
        return self._headless_runner

    def _get_fallback_runner(self):
        if self._cli_runner is not None:
            return self._cli_runner
        if self._headless_runner is not None:
            return self._headless_runner
        # Default auto-mode fallback after Live IDE is the permissive
        # GEMINI_CLI_FALLBACK route (headless), never the strict
        # AG_OFFICIAL_CLI-only resolver -- explicit force_mode="cli" is the
        # only way to require a verified standalone `agy`.
        return self._get_headless_runner()

    def prepare(self, request: LaunchRequest) -> PreparedLaunch:
        self.last_fallback_reason = None

        if request.force_mode == "cli":
            return self._get_cli_runner().prepare(request)
        if request.force_mode == "headless":
            return self._get_headless_runner().prepare(request)
        if request.force_mode == "live_ide":
            bridge = self._get_ide_bridge()
            if not bridge.is_alive():
                raise AgLaunchError("live_ide_not_found", "Antigravity Live IDE/runtime is not running")
            return bridge.prepare(request)

        # Hybrid auto-discovery: Live IDE prioritized only if transport is actually available
        bridge = self._get_ide_bridge()
        if bridge.is_alive():
            try:
                return bridge.prepare(request)
            except AgLaunchError as exc:
                if exc.classification in ("live_ide_transport_unavailable", "live_ide_not_found"):
                    self.last_fallback_reason = exc.classification
                    # Observable fallback to CLI / headless runner
                else:
                    raise
        else:
            self.last_fallback_reason = "live_ide_not_found"

        prepared = self._get_fallback_runner().prepare(request)
        if self.last_fallback_reason:
            # A fallback transition happened getting here -- record it so
            # evidence never shows AG_OFFICIAL_CLI/AG_LIVE_IDE_IPC for a
            # launch that actually fell back.
            prepared.fallback_occurred = True
            prepared.fallback_reason = self.last_fallback_reason
        return prepared

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared.mode == "live_ide":
            return self._get_ide_bridge().start(prepared, prompt)
        if prepared.mode == "cli":
            return self._get_cli_runner().start(prepared, prompt)
        return self._get_headless_runner().start(prepared, prompt)

    def set_heartbeat(self, running: RunningLaunch, callback: Callable[[str], Any]) -> None:
        running._heartbeat = callback
        if running.prepared.mode == "live_ide":
            target = self._get_ide_bridge()
        elif running.prepared.mode == "cli":
            target = self._get_cli_runner()
        else:
            target = self._get_headless_runner()
        if hasattr(target, "set_heartbeat"):
            target.set_heartbeat(running, callback)

    def wait(self, running: RunningLaunch) -> LaunchOutcome:
        if running.prepared.mode == "live_ide":
            return self._get_ide_bridge().wait(running)
        if running.prepared.mode == "cli":
            return self._get_cli_runner().wait(running)
        return self._get_headless_runner().wait(running)

    def close(self, target: Any) -> None:
        prepared = getattr(target, "prepared", target)
        if prepared and getattr(prepared, "mode", None) == "live_ide":
            self._get_ide_bridge().close(prepared)
        elif prepared and getattr(prepared, "mode", None) == "cli":
            self._get_cli_runner().close(prepared)
        elif prepared:
            self._get_headless_runner().close(prepared)
