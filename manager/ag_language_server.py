"""Antigravity language-server discovery and Connect-RPC client.

Antigravity IDE 1.107 ships no standalone ``agy`` binary on this machine. Its
real machine-readable automation surface is the bundled language server
(``language_server_windows_x64.exe``), which the IDE starts with a per-run
``--csrf_token`` argument and which listens on two random loopback ports
(HTTPS/gRPC and plain HTTP). Both the official ``agentapi`` CLI (spawned
through ``~/.gemini/antigravity-ide/bin/agentapi.bat``) and the IDE's own UI
talk to that server: Connect-RPC JSON over HTTP with the
``x-codeium-csrf-token`` header.

This module is the single transport core for everything ADM does with AG:

* discovery of the running language server (pid, ports, CSRF nonce, version);
* a small JSON Connect-RPC client with bounded, classified failures;
* ``availability_snapshot()`` -- the structured AG availability/quota contract
  (never a bare ``True``/``False``).

Security boundary: the CSRF token is a per-run local IPC nonce created by the
IDE extension (``randomUUID()``), not an account credential. It is still
treated as a secret here -- never logged, never persisted, never included in
evidence dicts, excluded from ``repr``. The Google OAuth token itself lives in
the IDE's own storage and is never read by this module; the language server
performs every authenticated call on ADM's behalf.

Read-only: nothing in this module starts, stops, or writes to Antigravity.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

LS_EXECUTABLE_NAMES = ("language_server_windows_x64.exe", "language_server_windows_x64", "language_server")
LS_SERVICE = "exa.language_server_pb.LanguageServerService"
CSRF_HEADER = "x-codeium-csrf-token"
SOURCE = "antigravity_language_server"
DEFAULT_APP_DATA_DIR = "antigravity-ide"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_ERROR_CHARS = 500
# The executable must belong to a real Antigravity install; a same-named
# binary somewhere else on disk is never trusted as the provider transport.
TRUSTED_EXECUTABLE_MARKERS = ("extensions/antigravity/bin", "extensions\\antigravity\\bin", "/.gemini/", "\\.gemini\\")
_REDACT_KEY = re.compile(r"(token|secret|api_?key|credential|password|cookie|refresh_)", re.IGNORECASE)
_CSRF_ARG = re.compile(r"--csrf_token(?:=|\s+)(\S+)")
_APP_DATA_ARG = re.compile(r"--app_data_dir(?:=|\s+)(\S+)")
_WORKSPACE_ID_ARG = re.compile(r"--workspace_id(?:=|\s+)(\S+)")
# Execution transports. ``ide_bridge`` drives the IDE-hosted language server's
# own cascade RPCs directly (AddTrackedWorkspace -> StartCascade ->
# SendUserCascadeMessage), verified live 2026-09-05 on Antigravity IDE 1.107.0;
# ``agentapi`` spawns the official CLI, which needs a projects store this
# IDE-hosted server does not have. The transport actually used is recorded in
# every run-state/evidence record -- never inferred afterwards.
TRANSPORT_IDE_BRIDGE = "ide_bridge"
TRANSPORT_AGENTAPI = "agentapi"
TRANSPORTS = (TRANSPORT_IDE_BRIDGE, TRANSPORT_AGENTAPI)
CASCADE_SOURCE_AGENT_API = "CORTEX_TRAJECTORY_SOURCE_AGENT_API"
# The IDE starts two language servers per window: the app-level one (no
# --workspace_id) hosts every cascade/conversation, the per-workspace one
# (--enable_lsp --workspace_id ...) only serves LSP and answers the cascade
# RPCs with an empty trajectory list. Verified live 2026-09-05.
ROLE_CASCADE_HOST = "cascade_host"
ROLE_WORKSPACE_LSP = "workspace_lsp"
VERIFIED_IDE_VERSION = "1.107.0"
_LISTEN_LINE = re.compile(r"^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0|\[::1\]|\[::\]):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE)


class AgLsError(RuntimeError):
    """Bounded, classified language-server failure. ``detail`` never carries the CSRF token."""

    def __init__(self, classification: str, detail: str = ""):
        self.classification = classification
        self.detail = _redact_text(str(detail))[:MAX_ERROR_CHARS]
        super().__init__(f"{classification}: {self.detail}" if self.detail else classification)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _redact_text(text: str) -> str:
    return _CSRF_ARG.sub("--csrf_token [REDACTED]", text)


def redact(value: Any, *, depth: int = 0) -> Any:
    """Return a copy of ``value`` with secret-looking keys replaced; safe for logs and evidence."""
    if depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _REDACT_KEY.search(str(key)) else redact(item, depth=depth + 1)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


@dataclass(frozen=True)
class LanguageServerEndpoint:
    """One discovered Antigravity language server. ``csrf_token`` is excluded from repr/evidence."""

    pid: int
    http_port: int
    https_port: int | None
    app_data_dir: str
    executable: str
    observed_at: str
    source: str = "ide_process"
    ls_version: str | None = None
    creation_identity: str | None = None
    parent_pid: int | None = None
    role: str = ROLE_CASCADE_HOST
    workspace_id: str | None = None
    csrf_token: str = field(default="", repr=False, compare=False)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def agentapi_address(self) -> str:
        # The official agentapi CLI speaks gRPC (h2c) to the plain HTTP port;
        # the HTTPS port rejects it ("error reading server preface").
        return f"127.0.0.1:{self.http_port}"

    def agentapi_env(self) -> dict[str, str]:
        """Environment the official ``agentapi`` CLI requires (verified live 2026-09-02)."""
        env = {"ANTIGRAVITY_LS_ADDRESS": self.agentapi_address, "ANTIGRAVITY_CSRF_TOKEN": self.csrf_token}
        if self.ls_version:
            env["ANTIGRAVITY_LS_VERSION"] = self.ls_version
        return env

    def evidence(self) -> dict[str, Any]:
        """Persistable identity of this server -- no token, ever."""
        return {
            "pid": self.pid, "parent_pid": self.parent_pid, "creation_identity": self.creation_identity,
            "http_port": self.http_port, "https_port": self.https_port, "ls_version": self.ls_version,
            "app_data_dir": self.app_data_dir, "executable": os.path.basename(self.executable),
            "source": self.source, "observed_at": self.observed_at, "role": self.role, "workspace_id": self.workspace_id,
        }


# --------------------------------------------------------------------------- discovery

def _hidden_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout, "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, **kwargs)


_PROCESS_QUERY = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "$items = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'language_server*' } | "
    "Select-Object ProcessId, ParentProcessId, Name, CommandLine, ExecutablePath, "
    "@{n='CreationDate';e={ if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { $null } }}; "
    "ConvertTo-Json -InputObject @($items) -Compress -Depth 3"
)


def list_language_server_processes(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Enumerate same-user language-server processes (Windows CIM). Never raises on an empty result."""
    if os.name != "nt":
        return []
    try:
        completed = _hidden_run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PROCESS_QUERY], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgLsError("process_enumeration_failed", str(exc)) from exc
    return parse_process_listing(completed.stdout)


def parse_process_listing(raw: str) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgLsError("process_enumeration_failed", f"unparseable process listing: {exc}") from exc
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    results = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        results.append({
            "pid": pid,
            "parent_pid": _as_int(item.get("ParentProcessId")),
            "name": str(item.get("Name") or ""),
            "command_line": str(item.get("CommandLine") or ""),
            "executable_path": str(item.get("ExecutablePath") or ""),
            "creation_date": item.get("CreationDate"),
        })
    return results


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_command_line(command_line: str) -> dict[str, Any]:
    """Extract the launch facts ADM needs from the server's argv. The token stays in-memory only."""
    csrf = _CSRF_ARG.search(command_line or "")
    app_data = _APP_DATA_ARG.search(command_line or "")
    workspace = _WORKSPACE_ID_ARG.search(command_line or "")
    enable_lsp = "--enable_lsp" in (command_line or "")
    return {
        "csrf_token": csrf.group(1) if csrf else None,
        "app_data_dir": app_data.group(1) if app_data else DEFAULT_APP_DATA_DIR,
        "persistent_mode": "--persistent_mode" in (command_line or ""),
        "workspace_id": workspace.group(1) if workspace else None,
        "enable_lsp": enable_lsp,
        "role": ROLE_WORKSPACE_LSP if (workspace or enable_lsp) else ROLE_CASCADE_HOST,
    }


def executable_is_trusted(path: str) -> bool:
    normalized = (path or "").replace("\\", "/").lower()
    if not normalized:
        return False
    if os.path.basename(normalized) not in LS_EXECUTABLE_NAMES:
        return False
    return any(marker.replace("\\", "/").lower() in normalized for marker in TRUSTED_EXECUTABLE_MARKERS)


def list_listening_ports(pid: int, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[int]:
    """Loopback TCP ports ``pid`` is listening on, via ``netstat -ano`` (no extra dependencies)."""
    if os.name != "nt":
        return []
    try:
        completed = _hidden_run(["netstat", "-ano", "-p", "tcp"], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgLsError("port_enumeration_failed", str(exc)) from exc
    return parse_listening_ports(completed.stdout, pid)


def parse_listening_ports(netstat_output: str, pid: int) -> list[int]:
    ports = []
    for line in (netstat_output or "").splitlines():
        match = _LISTEN_LINE.match(line)
        if match and int(match.group(2)) == pid:
            port = int(match.group(1))
            if port not in ports:
                ports.append(port)
    return sorted(ports)


def _default_opener(url: str, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _probe_http_port(port: int, csrf_token: str, opener: Callable[..., tuple[int, bytes]], timeout: float) -> bool:
    """True when ``port`` answers ``GetStatus`` as plain-HTTP Connect-RPC (the HTTPS port fails the handshake)."""
    try:
        status, body = opener(f"http://127.0.0.1:{port}/{LS_SERVICE}/GetStatus", b"{}",
                              {"content-type": "application/json", CSRF_HEADER: csrf_token, "connect-protocol-version": "1"}, timeout)
    except Exception:
        return False
    if status != 200:
        return False
    try:
        return isinstance(json.loads(body or b"{}"), dict)
    except (ValueError, TypeError):
        return False


def discover_language_server(*, app_data_dir: str = DEFAULT_APP_DATA_DIR, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                             process_lister: Callable[..., list[dict[str, Any]]] | None = None,
                             port_lister: Callable[..., list[int]] | None = None,
                             opener: Callable[..., tuple[int, bytes]] | None = None,
                             identity: Callable[[int], str | None] | None = None,
                             ls_version: str | None = None) -> LanguageServerEndpoint:
    """Find the one live Antigravity language server ADM may talk to, fail-closed.

    Classifications: ``ide_not_running`` (no trusted server process),
    ``csrf_unavailable`` (process found but its argv carries no token),
    ``ls_unreachable`` (process found, no port answers), plus the enumeration
    failures raised by the helpers. When several servers run, the app-level
    cascade host (``ROLE_CASCADE_HOST``) is preferred over a per-workspace
    LSP server, and among equals the most recently created one is used.
    """
    process_lister = process_lister or list_language_server_processes
    port_lister = port_lister or list_listening_ports
    opener = opener or _default_opener
    observed_at = utc_now()
    candidates = []
    csrf_missing = False
    for proc in process_lister(timeout=timeout) if process_lister is list_language_server_processes else process_lister():
        executable = proc.get("executable_path") or _executable_from_command_line(proc.get("command_line", ""))
        if not executable_is_trusted(executable):
            continue
        launch = parse_command_line(proc.get("command_line", ""))
        if launch["app_data_dir"] != app_data_dir:
            continue
        if not launch["csrf_token"]:
            csrf_missing = True
            continue
        candidates.append((proc, launch, executable))
    if not candidates:
        if csrf_missing:
            raise AgLsError("csrf_unavailable", "an Antigravity language server is running but exposes no --csrf_token")
        raise AgLsError("ide_not_running", "no trusted Antigravity language server process is running")
    candidates.sort(key=lambda item: str(item[0].get("creation_date") or ""), reverse=True)
    candidates.sort(key=lambda item: item[1]["role"] != ROLE_CASCADE_HOST)  # stable: cascade host first
    last_error = None
    for proc, launch, executable in candidates:
        pid = proc["pid"]
        ports = port_lister(pid, timeout=timeout) if port_lister is list_listening_ports else port_lister(pid)
        http_port = next((port for port in ports if _probe_http_port(port, launch["csrf_token"], opener, timeout)), None)
        if http_port is None:
            last_error = AgLsError("ls_unreachable", f"language server pid {pid} answers on none of {ports}")
            continue
        https_port = next((port for port in ports if port != http_port), None)
        creation = None
        if identity is not None:
            creation = identity(pid)
        else:
            try:
                from manager.codex_launcher import process_creation_identity
                creation = process_creation_identity(pid)
            except Exception:
                creation = None
        return LanguageServerEndpoint(
            pid=pid, http_port=http_port, https_port=https_port, app_data_dir=launch["app_data_dir"],
            executable=executable, observed_at=observed_at, source="ide_process", ls_version=ls_version,
            creation_identity=creation, parent_pid=proc.get("parent_pid"), role=launch["role"],
            workspace_id=launch["workspace_id"], csrf_token=launch["csrf_token"],
        )
    raise last_error or AgLsError("ls_unreachable", "no reachable Antigravity language server")


def _executable_from_command_line(command_line: str) -> str:
    text = (command_line or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else ""
    return text.split(" ", 1)[0] if text else ""


# --------------------------------------------------------------------------- client

class AgLanguageServerClient:
    """Minimal Connect-RPC JSON client bound to one discovered endpoint."""

    def __init__(self, endpoint: LanguageServerEndpoint, *, opener: Callable[..., tuple[int, bytes]] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.endpoint = endpoint
        self._opener = opener or _default_opener
        self.timeout = timeout

    def call(self, rpc: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        url = f"{self.endpoint.base_url}/{LS_SERVICE}/{rpc}"
        headers = {"content-type": "application/json", CSRF_HEADER: self.endpoint.csrf_token, "connect-protocol-version": "1"}
        payload = json.dumps(body or {}).encode("utf-8")
        try:
            status, raw = self._opener(url, payload, headers, timeout or self.timeout)
        except Exception as exc:
            raise AgLsError("ls_unreachable", f"{rpc}: {type(exc).__name__}: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw or "{}")
        except (ValueError, TypeError) as exc:
            raise AgLsError("malformed_response", f"{rpc}: non-JSON response (HTTP {status})") from exc
        if status != 200:
            code = str(parsed.get("code") if isinstance(parsed, dict) else "") or f"http_{status}"
            message = str(parsed.get("message") if isinstance(parsed, dict) else "")[:MAX_ERROR_CHARS]
            if status == 403 or code in ("unauthenticated", "permission_denied"):
                raise AgLsError("rpc_unauthenticated", f"{rpc}: {code}: {message}")
            if code == "invalid_argument":
                raise AgLsError("rpc_invalid_argument", f"{rpc}: {message}")
            if code == "failed_precondition":
                raise AgLsError("rpc_failed_precondition", f"{rpc}: {message}")
            if "not found" in message.lower():
                raise AgLsError("rpc_not_found", f"{rpc}: {message}")
            raise AgLsError("rpc_failed", f"{rpc}: HTTP {status} {code}: {message}")
        if not isinstance(parsed, dict):
            raise AgLsError("malformed_response", f"{rpc}: response is not an object")
        return parsed

    # Read-only status surface (no model turn is consumed by any of these).
    def get_status(self) -> dict[str, Any]:
        return self.call("GetStatus")

    def get_user_status(self) -> dict[str, Any]:
        return self.call("GetUserStatus")

    def retrieve_user_quota_summary(self) -> dict[str, Any]:
        return self.call("RetrieveUserQuotaSummary")

    def get_all_cascade_trajectories(self) -> dict[str, Any]:
        return self.call("GetAllCascadeTrajectories")

    def get_conversation_metadata(self, conversation_id: str) -> dict[str, Any]:
        return self.call("GetConversationMetadata", {"conversationId": conversation_id})

    def get_cascade_trajectory_steps(self, cascade_id: str) -> dict[str, Any]:
        return self.call("GetCascadeTrajectorySteps", {"cascadeId": cascade_id})

    def get_cascade_trajectory_executor_metadatas(self, cascade_id: str) -> dict[str, Any]:
        return self.call("GetCascadeTrajectoryExecutorMetadatas", {"cascadeId": cascade_id})

    # -- IDE-bridge dispatch RPCs (shapes verified live 2026-09-05, IDE 1.107.0) --
    def add_tracked_workspace(self, workspace_path: str) -> dict[str, Any]:
        """Register an absolute folder path (no ``file://``) so the server may open a cascade in it."""
        return self.call("AddTrackedWorkspace", {"workspace": str(workspace_path)})

    def start_cascade(self, workspace_uris: list[str], *, source: str = CASCADE_SOURCE_AGENT_API) -> dict[str, Any]:
        """Create a NEW, empty conversation bound to ``workspace_uris``; returns ``{"cascadeId": ...}``."""
        return self.call("StartCascade", {"source": source, "workspaceUris": [str(uri) for uri in workspace_uris]})

    def send_user_cascade_message(self, cascade_id: str, text: str, *, model_placeholder: str,
                                  ide_version: str | None = None) -> dict[str, Any]:
        """Deliver one user turn. The model goes in ``cascadeConfig.plannerConfig.requestedModel``
        as the server's own ``MODEL_PLACEHOLDER_*`` enum; the server applies its default tool policy
        (no auto-execution widening is requested here)."""
        body = {
            "cascadeId": cascade_id,
            "items": [{"text": text}],
            "metadata": {"ideName": "antigravity", "ideVersion": ide_version or VERIFIED_IDE_VERSION,
                         "extensionName": "antigravity", "locale": "en"},
            "cascadeConfig": {"plannerConfig": {
                "conversational": {"plannerMode": "CONVERSATIONAL_PLANNER_MODE_DEFAULT", "agenticMode": True},
                "requestedModel": {"model": model_placeholder},
            }},
        }
        return self.call("SendUserCascadeMessage", body)


# --------------------------------------------------------------------------- availability contract

def _bucket_records(quota_summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten RetrieveUserQuotaSummary into bucket records; raises on a changed schema."""
    response = quota_summary.get("response") if isinstance(quota_summary, dict) else None
    groups = response.get("groups") if isinstance(response, dict) else None
    if not isinstance(groups, list):
        raise AgLsError("quota_schema_changed", "RetrieveUserQuotaSummary.response.groups missing")
    records = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            fraction = bucket.get("remainingFraction")
            if fraction is None:
                # protobuf JSON omits zero-valued scalars: a missing fraction on a
                # bucket that still names a reset time is a truly exhausted bucket.
                fraction = 0.0 if bucket.get("resetTime") else None
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
                continue
            records.append({
                "group": str(group.get("displayName") or ""),
                "bucket_id": str(bucket.get("bucketId") or ""),
                "display_name": str(bucket.get("displayName") or ""),
                "window": str(bucket.get("window") or ""),
                "remaining_fraction": max(0.0, min(1.0, float(fraction))),
                "reset_time": bucket.get("resetTime"),
            })
    if not records:
        raise AgLsError("quota_schema_changed", "RetrieveUserQuotaSummary carried no bucket with a remaining fraction")
    return records


def _account_records(user_status: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    status = user_status.get("userStatus") if isinstance(user_status, dict) else None
    if not isinstance(status, dict):
        return None, []
    plan = status.get("planStatus") if isinstance(status.get("planStatus"), dict) else {}
    plan_info = plan.get("planInfo") if isinstance(plan.get("planInfo"), dict) else {}
    account = {
        "email": status.get("email") or None,
        "name": status.get("name") or None,
        "plan_name": plan_info.get("planName"),
        "teams_tier": plan_info.get("teamsTier"),
        "available_prompt_credits": plan.get("availablePromptCredits"),
        "available_flow_credits": plan.get("availableFlowCredits"),
    }
    models = []
    config_data = status.get("cascadeModelConfigData") if isinstance(status.get("cascadeModelConfigData"), dict) else {}
    for config in config_data.get("clientModelConfigs") or []:
        if not isinstance(config, dict):
            continue
        quota_info = config.get("quotaInfo") if isinstance(config.get("quotaInfo"), dict) else {}
        fraction = quota_info.get("remainingFraction")
        if fraction is None and quota_info.get("resetTime"):
            fraction = 0.0
        models.append({
            "model_id": config.get("modelId"),
            "label": config.get("label"),
            "remaining_fraction": float(fraction) if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) else None,
            "reset_time": quota_info.get("resetTime"),
        })
    return account, models[:40]


def probe_dispatch_route(client: AgLanguageServerClient, *, transport: str = TRANSPORT_IDE_BRIDGE) -> dict[str, Any]:
    """Can this language server actually accept a NEW conversation from ADM over ``transport``?

    Quota readability and dispatchability are different facts and must not be
    conflated. Both probes are read-only and consume no model turn.

    * ``ide_bridge`` -- the server's cascade subsystem must answer
      ``GetAllCascadeTrajectories`` (the per-workspace LSP server answers it
      too, with an empty map, so discovery -- not this probe -- is what picks
      the cascade host). Whether a *specific* cascade can be created is only
      ever proven by ``StartCascade`` itself at dispatch time.
    * ``agentapi`` -- the official CLI always sends a ``project_env_config``,
      which needs an initialized projects store. Verified live on Antigravity
      IDE 1.107.0 (2026-09-02 and again 2026-09-05): an IDE-hosted server
      answers ``ReadProject`` with ``projects store not initialized``, so that
      route stays blocked (``projectsStore is nil, but projectEnvConfig was
      provided`` / ``project_id is required when providing project_env_config``).

    Returns ``{"available": bool, "transport": str, "reason": str|None, "detail": str|None}``.
    """
    if transport == TRANSPORT_IDE_BRIDGE:
        try:
            summaries = client.get_all_cascade_trajectories()
        except AgLsError as exc:
            return {"available": False, "transport": transport, "reason": exc.classification, "detail": exc.detail}
        if not isinstance(summaries, dict):
            return {"available": False, "transport": transport, "reason": "malformed_response",
                    "detail": "GetAllCascadeTrajectories did not answer with a JSON object"}
        return {"available": True, "transport": transport, "reason": None, "detail": None}
    if transport != TRANSPORT_AGENTAPI:
        raise ValueError(f"unknown Antigravity transport {transport!r}; expected one of {TRANSPORTS}")
    try:
        client.call("ReadProject", {})
    except AgLsError as exc:
        detail = (exc.detail or "").lower()
        if "projects store not initialized" in detail or "projectsstore is nil" in detail:
            return {"available": False, "transport": transport, "reason": "projects_store_unavailable", "detail": exc.detail}
        if exc.classification in ("rpc_invalid_argument", "rpc_not_found"):
            # The store answered and merely rejected an empty request: it exists.
            return {"available": True, "transport": transport, "reason": None, "detail": None}
        return {"available": False, "transport": transport, "reason": exc.classification, "detail": exc.detail}
    return {"available": True, "transport": transport, "reason": None, "detail": None}


def _model_catalog(user_status: dict[str, Any]) -> list[dict[str, Any]]:
    status = user_status.get("userStatus") if isinstance(user_status, dict) else None
    configs = ((status or {}).get("cascadeModelConfigData") or {}).get("clientModelConfigs") if isinstance(status, dict) else None
    catalog = []
    for config in configs or []:
        if not isinstance(config, dict):
            continue
        placeholder = (config.get("modelOrAlias") or {}).get("model") if isinstance(config.get("modelOrAlias"), dict) else None
        quota = config.get("quotaInfo") if isinstance(config.get("quotaInfo"), dict) else {}
        catalog.append({
            "model_id": config.get("modelId"), "label": config.get("label"), "placeholder": placeholder,
            "recommended": bool(config.get("isRecommended")),
            # protobuf JSON omits zero-valued scalars: a present resetTime with
            # no remainingFraction means exhausted (0), not unknown.
            "remaining_fraction": float(quota.get("remainingFraction") or 0.0) if quota else None,
            "reset_time": quota.get("resetTime"),
        })
    return catalog


def resolve_model_placeholder(user_status: dict[str, Any], requested: str | None = None) -> dict[str, Any]:
    """Map an ADM model name onto the server's own ``MODEL_PLACEHOLDER_*`` enum -- from the live
    catalog ``GetUserStatus.cascadeModelConfigData.clientModelConfigs``, never from a table ADM
    guessed. ``requested`` matches ``modelId``, ``label`` or the placeholder itself
    (case-insensitive); ``None`` picks the cheapest recommended Gemini Flash model that still
    has quota. Raises ``unknown_model`` / ``model_quota_exhausted`` / ``model_catalog_unavailable``.
    """
    catalog = [item for item in _model_catalog(user_status) if item["placeholder"] and item["model_id"]]
    if not catalog:
        raise AgLsError("model_catalog_unavailable", "GetUserStatus exposes no client model configs with a model placeholder")
    if requested is not None and str(requested).strip():
        wanted = str(requested).strip().lower()
        for item in catalog:
            if wanted in {str(item["model_id"]).lower(), str(item["label"] or "").lower(), str(item["placeholder"]).lower()}:
                if item["remaining_fraction"] is not None and item["remaining_fraction"] <= 0.0:
                    raise AgLsError("model_quota_exhausted", f"{item['model_id']} exhausted until {item['reset_time']}")
                return item
        raise AgLsError("unknown_model", f"model {requested!r} is not in the live catalog {[c['model_id'] for c in catalog]}")
    usable = [item for item in catalog if item["remaining_fraction"] is None or item["remaining_fraction"] > 0.0]
    if not usable:
        raise AgLsError("model_quota_exhausted", "every catalog model is exhausted")

    def rank(item: dict[str, Any]) -> tuple:
        model_id = str(item["model_id"]).lower()
        return (not ("gemini" in model_id and "flash" in model_id), not item["recommended"], "low" not in model_id, model_id)

    return sorted(usable, key=rank)[0]


def availability_snapshot(*, discover: Callable[..., LanguageServerEndpoint] | None = None,
                          client_factory: Callable[..., AgLanguageServerClient] | None = None,
                          now: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                          transport: str = TRANSPORT_IDE_BRIDGE) -> dict[str, Any]:
    """Structured AG availability -- status/source/observed_at/freshness/confidence/account/models/remaining/reset_at/reason.

    ``status`` is one of ``available`` / ``degraded`` (some bucket exhausted) /
    ``unavailable`` (no server, or every bucket exhausted -> ``unavailable_until``)
    / ``unverified`` (server reachable but quota could not be read). Nothing
    here is guessed: without a real quota read, ``remaining`` stays None.
    """
    discover = discover or discover_language_server
    client_factory = client_factory or AgLanguageServerClient
    observed_at = now or utc_now()
    snapshot: dict[str, Any] = {
        "provider": "antigravity", "status": "unavailable", "source": SOURCE, "observed_at": observed_at,
        "freshness": "fresh", "confidence": "unknown", "account": None, "models": [], "buckets": [],
        "remaining": None, "remaining_percent": None, "reset_at": None, "unavailable_until": None,
        "reason": None, "language_server": None, "dispatch_route": None, "can_accept_new_task": False,
        "transport": transport,
    }
    try:
        endpoint = discover(timeout=timeout)
    except AgLsError as exc:
        snapshot.update(reason=exc.classification, detail=exc.detail)
        return snapshot
    snapshot["language_server"] = endpoint.evidence()
    client = client_factory(endpoint, timeout=timeout)
    try:
        user_status = client.get_user_status()
        quota_summary = client.retrieve_user_quota_summary()
        buckets = _bucket_records(quota_summary)
    except AgLsError as exc:
        snapshot.update(status="unverified", reason=exc.classification, detail=exc.detail)
        return snapshot
    account, models = _account_records(user_status)
    snapshot.update(account=account, models=models, buckets=buckets, confidence="official")
    route = probe_dispatch_route(client, transport=transport)
    snapshot["dispatch_route"] = route
    if account is None or not account.get("email"):
        snapshot.update(status="unverified", reason="account_identity_unavailable")
        return snapshot
    remaining = min(bucket["remaining_fraction"] for bucket in buckets)
    exhausted = [bucket for bucket in buckets if bucket["remaining_fraction"] <= 0.0]
    resets = sorted(bucket["reset_time"] for bucket in buckets if bucket.get("reset_time"))
    snapshot.update(remaining=round(remaining, 4), remaining_percent=round(remaining * 100, 1), reset_at=resets[0] if resets else None)
    if exhausted and len(exhausted) == len(buckets):
        until = sorted(bucket["reset_time"] for bucket in exhausted if bucket.get("reset_time"))
        snapshot.update(status="unavailable", reason="quota_exhausted", unavailable_until=until[-1] if until else None)
    elif exhausted:
        snapshot.update(status="degraded", reason="quota_exhausted_partial",
                        unavailable_until=max((bucket["reset_time"] for bucket in exhausted if bucket.get("reset_time")), default=None),
                        can_accept_new_task=True)
    else:
        snapshot.update(status="available", can_accept_new_task=True)
    if not route["available"]:
        # Quota stays truthfully readable/fresh; only dispatch is refused.
        snapshot["can_accept_new_task"] = False
        if snapshot["status"] == "available":
            snapshot["status"] = "degraded"
        snapshot["reason"] = snapshot["reason"] or route["reason"]
    return snapshot


def main(argv: list[str] | None = None) -> int:
    """Print the availability snapshot (secrets redacted). Layer-3 local compatibility smoke."""
    snapshot = availability_snapshot()
    print(json.dumps(redact(snapshot), ensure_ascii=False, indent=2))
    return 0 if snapshot["status"] in ("available", "degraded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
