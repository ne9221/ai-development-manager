"""Optional thin adapter for Hydra's proven MCP manager surface."""

from __future__ import annotations

import json
import os
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from manager.codex_launcher import (HEARTBEAT_INTERVAL_SECONDS, LaunchOutcome, LaunchRequest,
                                    process_creation_identity, process_identity_state, utc_now)


class HydraUnavailable(RuntimeError):
    classification = "hydra_unavailable"


class HydraRuntime:
    """Discovers Hydra on every reconnect and exposes only proven manager tools."""

    REQUIRED_TOOLS = frozenset({
        "hydra_list_agents", "hydra_create_agent", "hydra_send_prompt",
        "hydra_get_output", "hydra_kill_agent",
    })

    def __init__(self, config_path: str | Path | None = None, lock_path: str | Path | None = None,
                 timeout: float = 5.0):
        home = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "hydra"
        self.config_path = Path(config_path) if config_path else home / "manager-workspace" / ".mcp.json"
        self.lock_path = Path(lock_path) if lock_path else home / "daemon.lock"
        self.timeout = timeout
        self._endpoint: str | None = None

    def installed(self) -> bool:
        executable = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / "hydra" / "Hydra.exe"
        try:
            return executable.is_file() or self.config_path.is_file()
        except OSError:
            return self.config_path.is_file()

    def discover_endpoint(self) -> str:
        try:
            document = json.loads(self.config_path.read_text(encoding="utf-8"))
            url = document["mcpServers"]["hydra"]["url"]
            parsed = urlsplit(url)
            valid = (
                isinstance(url, str) and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port is not None and parsed.path == "/mcp"
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HydraUnavailable("Hydra MCP discovery file is unavailable or invalid") from exc
        if not valid:
            raise HydraUnavailable("Hydra MCP endpoint must be a dynamic loopback HTTP /mcp URL")
        self._endpoint = url
        return url

    def _post(self, endpoint: str, payload: dict, session_id: str | None = None) -> tuple[dict, Any]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id
        request = urllib.request.Request(endpoint, json.dumps(payload).encode("utf-8"), headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                document = json.loads(raw) if raw else {}
                return document, response.headers
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            raise HydraUnavailable("Hydra MCP endpoint did not return a valid response") from exc

    def _rpc(self, endpoint: str, method: str, params: dict | None = None):
        initialized, headers = self._post(endpoint, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "ai-development-manager", "version": "0.1.0"},
            },
        })
        session_id = headers.get("mcp-session-id")
        if isinstance(session_id, (list, tuple)):
            session_id = session_id[0]
        if initialized.get("error") or not isinstance(initialized.get("result"), dict) or not session_id:
            raise HydraUnavailable("Hydra MCP initialization failed")
        self._post(endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"}, str(session_id))
        response, _ = self._post(endpoint, {
            "jsonrpc": "2.0", "id": 2, "method": method, "params": params or {},
        }, str(session_id))
        if response.get("error") or "result" not in response:
            raise HydraUnavailable("Hydra MCP request failed")
        return response["result"]

    def _with_rediscovery(self, method: str, params: dict | None = None):
        last_error = None
        for _ in range(2):
            endpoint = self._endpoint or self.discover_endpoint()
            try:
                return self._rpc(endpoint, method, params)
            except HydraUnavailable as exc:
                last_error = exc
                self._endpoint = None
        raise HydraUnavailable("Hydra MCP endpoint is stale or unavailable after rediscovery") from last_error

    def daemon_identity(self) -> tuple[int, str]:
        try:
            pid = json.loads(self.lock_path.read_text(encoding="utf-8"))["pid"]
            identity = process_creation_identity(pid) if isinstance(pid, int) and pid > 0 else None
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HydraUnavailable("Hydra daemon identity is unavailable") from exc
        if identity is None:
            raise HydraUnavailable("Hydra daemon lock is stale")
        return pid, identity

    def health(self) -> dict:
        try:
            tools = self._with_rediscovery("tools/list").get("tools", [])
            names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
            if not self.REQUIRED_TOOLS.issubset(names):
                raise HydraUnavailable("Hydra MCP manager tools are incomplete")
            pid, identity = self.daemon_identity()
            return {"healthy": True, "installed": self.installed(), "endpoint": self._endpoint,
                    "pid": pid, "creation_identity": identity}
        except HydraUnavailable as exc:
            return {"healthy": False, "installed": self.installed(), "endpoint": None, "error": str(exc)}

    def _call(self, name: str, arguments: dict | None = None):
        result = self._with_rediscovery("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise HydraUnavailable(f"Hydra tool {name} returned an invalid result")
        if result.get("isError"):
            raise HydraUnavailable(f"Hydra tool {name} failed")
        content = result.get("content")
        text = next((item.get("text") for item in content or []
                     if isinstance(item, dict) and item.get("type") == "text"), None)
        if not isinstance(text, str):
            raise HydraUnavailable(f"Hydra tool {name} returned no text result")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def list_agents(self):
        return self._call("hydra_list_agents")

    def create_agent(self, *, name: str, project_dir: str, provider: str, model: str,
                     reasoning_effort: str | None = None, initial_prompt: str = "", yolo: bool = False):
        arguments = {"name": name, "projectDir": project_dir, "provider": provider, "model": model,
                     "initialPrompt": initial_prompt, "yolo": yolo}
        if reasoning_effort:
            arguments["reasoningEffort"] = reasoning_effort
        return self._call("hydra_create_agent", arguments)

    def send_prompt(self, agent_id: str, prompt: str):
        return self._call("hydra_send_prompt", {"agentId": agent_id, "prompt": prompt})

    def get_output(self, agent_id: str, lines: int = 100):
        return self._call("hydra_get_output", {"agentId": agent_id, "lines": lines})

    def kill_agent(self, agent_id: str):
        return self._call("hydra_kill_agent", {"agentId": agent_id})

    def restart_agent(self, agent_id: str):
        return self._call("hydra_restart_agent", {"agentId": agent_id})


@dataclass
class HydraPreparedLaunch:
    provider_session_id: str
    pid: int
    process_creation_identity: str
    prepared_at: str
    session_path: None
    hydra_agent_id: str
    hydra_endpoint: str
    _request: LaunchRequest = field(repr=False)
    account_id: str | None = None
    _closed: bool = field(default=False, repr=False)
    _baseline_output: Any = field(default=None, repr=False)


@dataclass
class HydraRunningLaunch:
    prepared: HydraPreparedLaunch
    turn_id: str
    started_at: str
    _heartbeat: Callable[[str], Any] | None = field(default=None, repr=False)
    _last_heartbeat: float = field(default=0.0, repr=False)


class HydraLauncher:
    """Adapts Hydra manager tools to ADM's existing launcher lifecycle."""

    def __init__(self, provider: str, runtime: HydraRuntime | None = None, poll_interval: float = 1.0,
                 settle_seconds: float = 5.0):
        if provider not in {"claude", "codex"}:
            raise ValueError("HydraLauncher provider must be claude or codex")
        self.provider = provider
        self.runtime = runtime or HydraRuntime()
        self.poll_interval = poll_interval
        self.settle_seconds = settle_seconds

    def prepare(self, request: LaunchRequest, **provider_options) -> HydraPreparedLaunch:
        account_id = provider_options.pop("account_id", None)
        config_dir = provider_options.pop("config_dir", None)
        if provider_options or config_dir is not None:
            raise HydraUnavailable("Hydra backend cannot select a non-default provider config directory")
        health = self.runtime.health()
        if not health.get("healthy"):
            raise HydraUnavailable("Hydra backend is unhealthy")
        model = request.model or ("sonnet" if self.provider == "claude" else "gpt-5.3-codex")
        agent = self.runtime.create_agent(
            name=f"ADM {self.provider} {uuid.uuid4().hex[:8]}", project_dir=request.working_directory,
            provider=self.provider, model=model, reasoning_effort=request.reasoning_effort,
        )
        if isinstance(agent, str) and "{" in agent:
            try:
                agent = json.loads(agent[agent.index("{"):])
            except json.JSONDecodeError:
                pass
        if not isinstance(agent, dict):
            raise HydraUnavailable("Hydra create_agent returned invalid evidence")
        agent_id = agent.get("id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise HydraUnavailable("Hydra create_agent returned no agent identity")
        try:
            deadline = time.monotonic() + request.timeout_seconds
            session_id = None
            while time.monotonic() < deadline:
                current = next((item for item in self.runtime.list_agents() if item.get("id") == agent_id), None)
                if current is None:
                    raise HydraUnavailable("Hydra agent disappeared during provider startup")
                session_id = current.get("sessionId")
                if isinstance(session_id, str) and session_id.strip():
                    break
                if current.get("status") in {"failed", "error", "exited", "stopped", "killed"}:
                    raise HydraUnavailable("Hydra provider failed before session evidence")
                time.sleep(min(self.poll_interval, 0.25))
            if not isinstance(session_id, str) or not session_id.strip():
                raise HydraUnavailable("Hydra provider session evidence timed out")
        except Exception:
            try:
                self.runtime.kill_agent(agent_id)
            except HydraUnavailable:
                pass
            raise
        return HydraPreparedLaunch(
            session_id, health["pid"], health["creation_identity"], utc_now(), None,
            agent_id, health["endpoint"], request, account_id=account_id,
            _baseline_output=self.runtime.get_output(agent_id, 20),
        )

    def start(self, prepared: HydraPreparedLaunch, prompt: str) -> HydraRunningLaunch:
        if not isinstance(prompt, str) or not prompt.strip():
            raise HydraUnavailable("Hydra prompt must be non-empty")
        self.runtime.send_prompt(prepared.hydra_agent_id, prompt)
        return HydraRunningLaunch(prepared, prepared.hydra_agent_id, utc_now())

    def wait(self, running: HydraRunningLaunch) -> LaunchOutcome:
        deadline = time.monotonic() + running.prepared._request.turn_timeout_seconds
        seen_active = False
        output_changed = False
        last_output = running.prepared._baseline_output
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            agents = self.runtime.list_agents()
            agent = next((item for item in agents if item.get("id") == running.prepared.hydra_agent_id), None)
            if agent is None:
                raise HydraUnavailable("Hydra agent disappeared before terminal evidence")
            status = agent.get("status")
            if status in {"running", "busy", "working"}:
                seen_active = True
                if running._heartbeat and time.monotonic() - running._last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    running._heartbeat("provider_event")
                    running._last_heartbeat = time.monotonic()
            elif status == "idle" and seen_active:
                self.runtime.get_output(running.prepared.hydra_agent_id, 100)
                return LaunchOutcome("completed", running.prepared.provider_session_id,
                                     running.turn_id, utc_now())
            elif status in {"failed", "error", "exited", "stopped", "killed"}:
                return LaunchOutcome("failed", running.prepared.provider_session_id,
                                     running.turn_id, utc_now(), "hydra_agent_failed", str(status))
            output = self.runtime.get_output(running.prepared.hydra_agent_id, 100)
            if output != last_output:
                output_changed = output_changed or output != running.prepared._baseline_output
                last_output = output
                stable_since = time.monotonic()
            elif output_changed and time.monotonic() - stable_since >= self.settle_seconds:
                return LaunchOutcome("completed", running.prepared.provider_session_id,
                                     running.turn_id, utc_now())
            time.sleep(self.poll_interval)
        raise HydraUnavailable("Hydra turn completion timed out")

    def set_heartbeat(self, running: HydraRunningLaunch, callback: Callable[[str], Any]):
        running._heartbeat = callback
        running._last_heartbeat = time.monotonic()

    def close(self, handle: HydraPreparedLaunch | HydraRunningLaunch):
        prepared = handle.prepared if isinstance(handle, HydraRunningLaunch) else handle
        if prepared._closed:
            return
        try:
            self.runtime.kill_agent(prepared.hydra_agent_id)
            prepared._closed = True
        except HydraUnavailable:
            if process_identity_state(prepared.pid, prepared.process_creation_identity) == "stopped":
                prepared._closed = True
                return
            raise

    @staticmethod
    def provider_stopped(prepared: HydraPreparedLaunch) -> bool:
        return prepared._closed or process_identity_state(prepared.pid, prepared.process_creation_identity) == "stopped"
