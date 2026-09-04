"""Antigravity execution adapter: official ``agentapi`` CLI + IDE language-server RPCs.

Verified transport (2026-09-02, Antigravity IDE 1.107.0): there is no ``agy``
binary; the official machine surface is ``language_server_windows_x64.exe
agentapi new-conversation|send-message|get-conversation-metadata`` talking to
the IDE-started language server (see manager/ag_language_server.py). The
conversation itself runs *inside* the language server -- the CLI returns as
soon as the conversation exists -- so this adapter observes, terminalizes and
cancels through the server's trajectory RPCs, never through the CLI's exit
code.

Transports (``OfficialAgCliRunner(transport=...)``, recorded in every
run-state/evidence record):

* ``ide_bridge`` (default, mode ``live_ide``) -- verified live 2026-09-05 on
  Antigravity IDE 1.107.0: ``AddTrackedWorkspace`` -> ``StartCascade``
  (a NEW cascade bound to exactly the ADM working directory) -> workspace
  readback -> ``SendUserCascadeMessage`` with the model as the server's own
  ``MODEL_PLACEHOLDER_*`` enum. No CLI process, no projects store needed.
* ``agentapi`` (mode ``cli``) -- the official CLI; blocked on an IDE-hosted
  server without a projects store (fails closed in ``prepare``).

Lifecycle (same prepare/start/wait/close contract as CodexLauncher /
ClaudeLauncher, routed by manager/ag_runner.AgRunner):

* ``prepare``  -- READY handshake, no side effects on AG: language server
  discovered, ``GetStatus`` answers, ``GetUserStatus`` carries an account,
  ``RetrieveUserQuotaSummary`` shows the model group is not exhausted, the
  requested model maps onto an ``agentapi`` model, the CLI route exists.
  Assigns the ADM-owned ``thread_id`` (provider_session_id) and persists a
  ``prepared`` run-state record.
* ``start``    -- ``agentapi new-conversation`` (env from the endpoint, CSRF
  never logged), bounded; parses the conversation id; verifies the workspace
  mapping when the server exposes one; persists ``running`` run state.
* ``wait``     -- polls ``GetAllCascadeTrajectories`` / ``GetCascadeTrajectorySteps``
  / ``GetCascadeTrajectoryExecutorMetadatas``; terminal truth = run status
  IDLE + executor termination reason + step statuses + a non-empty final
  response. Exit code 0 is never success. Permission stalls, quota
  exhaustion, timeouts, provider errors and cancellation all get distinct
  failure classifications.
* ``cancel``   -- ``CancelCascadeInvocation`` + ``ForceStopCascadeTree`` +
  kill of the CLI process tree, then reconciliation until the server reports
  IDLE; cancellation evidence is persisted.
* ``close``    -- cancels anything still running so ``provider_stopped`` can be
  proven by execution_runner._stopped() through the ``_process`` handle.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from manager.ag_language_server import (
    TRANSPORT_AGENTAPI,
    TRANSPORT_IDE_BRIDGE,
    TRANSPORTS,
    AgLanguageServerClient,
    AgLsError,
    LanguageServerEndpoint,
    _bucket_records,
    discover_language_server,
    probe_dispatch_route,
    redact,
    resolve_model_placeholder,
)
from manager.ag_run_state import TERMINAL_STATUSES, list_run_states, read_run_state, update_run_state, write_run_state
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

AGENTAPI_MODELS = ("flash_lite", "flash", "pro")
PROJECT_ID_ENV = "ADM_ANTIGRAVITY_PROJECT_ID"
RUN_STATUS_ACTIVE = ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY", "CASCADE_RUN_STATUS_CANCELING")
RUN_STATUS_IDLE = "CASCADE_RUN_STATUS_IDLE"
STEP_DONE = "CORTEX_STEP_STATUS_DONE"
STEP_ERROR = "CORTEX_STEP_STATUS_ERROR"
STEP_CANCELED = "CORTEX_STEP_STATUS_CANCELED"
STEP_WAITING = "CORTEX_STEP_STATUS_WAITING"
STEP_ACTIVE = ("CORTEX_STEP_STATUS_RUNNING", "CORTEX_STEP_STATUS_GENERATING", "CORTEX_STEP_STATUS_PENDING", "CORTEX_STEP_STATUS_QUEUED")
STEP_USER_INPUT = "CORTEX_STEP_TYPE_USER_INPUT"
STEP_PLANNER_RESPONSE = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
STEP_ERROR_MESSAGE = "CORTEX_STEP_TYPE_ERROR_MESSAGE"
STEP_ASK_QUESTION = "CORTEX_STEP_TYPE_ASK_QUESTION"
TERMINATION_CANCELED = "EXECUTOR_TERMINATION_REASON_USER_CANCELED"
TERMINATION_ERROR = "EXECUTOR_TERMINATION_REASON_ERROR"
TERMINATION_TOKEN_BUDGET = "EXECUTOR_TERMINATION_REASON_MAX_TOKEN_BUDGET_EXCEEDED"
TERMINATION_MAX_INVOCATIONS = ("EXECUTOR_TERMINATION_REASON_MAX_INVOCATIONS", "EXECUTOR_TERMINATION_REASON_MAX_FORCED_INVOCATIONS")
QUOTA_ERROR_PATTERN = re.compile(r"(quota|rate.?limit|resource.?exhausted|exhausted|capacity|429)", re.IGNORECASE)
AUTH_ERROR_PATTERN = re.compile(r"(unauthenticated|unauthorized|permission.?denied|auth|login|token expired)", re.IGNORECASE)
MAX_RESPONSE_CHARS = 20000
MAX_EVIDENCE_CHARS = 500
IDLE_GRACE_SECONDS = 20.0
MAX_CONSECUTIVE_POLL_FAILURES = 5


def _safe_home() -> Path:
    try:
        return Path.home()
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "/tmp")


def sanitize_ag_environment(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Strip secondary billing/API keys so the child strictly uses the IDE's own Google account."""
    env = dict(os.environ if base_env is None else base_env)
    for var in SECONDARY_BILLING_ENV_VARS:
        env.pop(var, None)
    return env


def verify_auth_identity() -> str:
    """Legacy local-profile presence check (kept for the headless fallback route).

    The real authentication proof for the language-server route is
    ``GetUserStatus`` carrying an account e-mail (see OfficialAgCliRunner.prepare).
    """
    gemini_home = Path(os.environ.get("GEMINI_HOME", _safe_home() / ".gemini"))
    oauth_file = gemini_home / "oauth_credentials.json"
    config_dir = gemini_home / "config"
    ide_dir = gemini_home / "antigravity-ide"
    antigravity_dir = gemini_home / "antigravity"
    appdata = os.environ.get("APPDATA")
    state_db = Path(appdata) / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb" if appdata else None
    has_local_profile = (
        config_dir.is_dir() or ide_dir.is_dir() or antigravity_dir.is_dir() or oauth_file.is_file()
        or (state_db and state_db.is_file())
    )
    if not has_local_profile:
        raise AgLaunchError(
            "unverified_identity",
            "Cannot prove Antigravity local identity: no local configuration directory (~/.gemini/config, ~/.gemini/antigravity-ide) or OAuth credential profile found. Fail closed.",
        )
    return "local_google_account_profile"


def resolve_ag_cli_executable(explicit: str | None = None) -> tuple[str, list[str]]:
    r"""Locate an ``agentapi`` entrypoint on disk. Returns (executable_path, prefix_args).

    Prefer the language-server executable itself with an ``agentapi`` prefix
    (what Antigravity's own sidecar helper does via ANTIGRAVITY_AGENTAPI_EXE);
    the ``.bat`` shim is accepted only as a last resort.
    """
    import shutil

    if explicit:
        path = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if path:
            resolved = str(Path(path).resolve())
            return resolved, (["agentapi"] if "language_server" in Path(resolved).name.lower() else [])
        raise AgLaunchError("executable_not_found", f"Explicit Antigravity CLI executable not found: {explicit}")

    env_bin = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE") or os.environ.get("AGENTAPI_BIN") or os.environ.get("ANTIGRAVITY_BIN") or os.environ.get("AGY_BIN")
    if env_bin:
        path = shutil.which(env_bin) or (env_bin if Path(env_bin).is_file() else None)
        if path:
            resolved = str(Path(path).resolve())
            return resolved, (["agentapi"] if "language_server" in Path(resolved).name.lower() else [])

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        for ls_path in (
            Path(local_appdata) / "Programs" / "Antigravity IDE" / "resources" / "app" / "extensions" / "antigravity" / "bin" / "language_server_windows_x64.exe",
            Path(local_appdata) / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe",
        ):
            if ls_path.is_file():
                return str(ls_path.resolve()), ["agentapi"]

    gemini_home = Path(os.environ.get("GEMINI_HOME", _safe_home() / ".gemini"))
    for sub in ("antigravity-ide/bin/agentapi.bat", "antigravity-ide/bin/agentapi", "antigravity/bin/agentapi.bat", "antigravity/bin/agentapi"):
        cand = gemini_home / sub
        if cand.is_file():
            return str(cand.resolve()), []

    names = ("agentapi.bat", "agentapi.cmd", "agentapi.exe", "agentapi") if os.name == "nt" else ("agentapi",)
    for name in names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve()), []
    raise AgLaunchError("executable_not_found", "Official Antigravity agentapi entrypoint was not found")


def map_agentapi_model(model: str | None) -> str | None:
    """ADM/user model names -> the only values ``agentapi --model`` accepts, or raise ``unknown_model``."""
    if model is None or not str(model).strip():
        return None
    text = str(model).strip().lower().replace("-", "_")
    if text in AGENTAPI_MODELS:
        return text
    if "flash_lite" in text or "flashlite" in text or "lite" in text:
        return "flash_lite"
    if "flash" in text:
        return "flash"
    if "pro" in text:
        return "pro"
    raise AgLaunchError("unknown_model", f"model {model!r} does not map onto agentapi models {AGENTAPI_MODELS}")


BINDING_INVARIANT = "ONE_EXECUTION_ONE_ACTIVE_AG_BINDING"


def _classify_rpc_failure(exc: AgLsError) -> str:
    """Normalize a dispatch-time language-server failure into ADM's failure vocabulary."""
    detail = exc.detail or ""
    if exc.classification == "rpc_unauthenticated" or AUTH_ERROR_PATTERN.search(detail):
        return "auth_transient"
    if QUOTA_ERROR_PATTERN.search(detail):
        return "quota_exhausted"
    if exc.classification == "malformed_response":
        return "malformed_output"
    if exc.classification == "ls_unreachable":
        return "ls_unreachable"
    return "dispatch_failed"


def _kill_process_tree(process: Any) -> None:
    """Terminate the CLI process and everything it spawned (Windows: taskkill /T)."""
    pid = getattr(process, "pid", None)
    if os.name == "nt" and isinstance(pid, int) and pid > 0:
        try:
            kwargs: dict[str, Any] = {"capture_output": True, "timeout": 10}
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], **kwargs)
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass


def _find_conversation_id(payload: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(payload, dict):
        for key in ("conversationId", "conversation_id", "cascadeId", "cascade_id", "rootConversationId", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _find_conversation_id(value, depth + 1)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_conversation_id(item, depth + 1)
            if found:
                return found
    return None


def parse_new_conversation_output(stdout: str) -> str:
    """Extract the conversation id from ``agentapi new-conversation`` JSON; classify failures."""
    text = (stdout or "").strip()
    if not text:
        raise AgLaunchError("dispatch_failed", "agentapi new-conversation produced no output")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise AgLaunchError("malformed_output", f"agentapi output is not JSON: {text[:MAX_EVIDENCE_CHARS]}") from exc
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        message = str(error)
        if QUOTA_ERROR_PATTERN.search(message):
            raise AgLaunchError("quota_exhausted", message)
        if "project" in message.lower():
            raise AgLaunchError("project_unresolved", message)
        if "Unavailable" in message or "connection" in message.lower() or "ANTIGRAVITY_LS_ADDRESS" in message:
            raise AgLaunchError("ls_unreachable", message)
        if AUTH_ERROR_PATTERN.search(message):
            raise AgLaunchError("auth_transient", message)
        raise AgLaunchError("dispatch_failed", message)
    conversation_id = _find_conversation_id(payload)
    if not conversation_id:
        raise AgLaunchError("malformed_output", f"agentapi output carried no conversation id: {text[:MAX_EVIDENCE_CHARS]}")
    return conversation_id


def _step_text(step: dict[str, Any]) -> str:
    """Best-effort human text of a trajectory step (planner response / error message)."""
    for key in ("plannerResponse", "errorMessage", "userInput", "askQuestion", "message"):
        body = step.get(key)
        if isinstance(body, dict):
            for inner in ("response", "text", "message", "content", "userResponse"):
                value = body.get(inner)
                if isinstance(value, str) and value.strip():
                    return value
            items = body.get("items")
            if isinstance(items, list):
                texts = [item.get("text") for item in items if isinstance(item, dict) and isinstance(item.get("text"), str)]
                if texts:
                    return "\n".join(texts)
        elif isinstance(body, str) and body.strip():
            return body
    for key in ("response", "text", "content"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _uri_path(uri: str) -> str:
    text = str(uri)
    if text.lower().startswith("file:///"):
        text = text[8:]
        if len(text) > 1 and text[1] != ":" and text[0] == "/":
            text = "/" + text
    elif text.lower().startswith("file://"):
        text = text[7:]
    from urllib.parse import unquote
    return unquote(text)


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def classify_run(summary: dict[str, Any] | None, steps: list[dict[str, Any]] | None,
                 executor_metadatas: list[dict[str, Any]] | None, *, seconds_since_start: float,
                 idle_grace_seconds: float = IDLE_GRACE_SECONDS) -> dict[str, Any]:
    """Pure terminal-truth classifier over the language server's own state.

    Returns ``{"state": running|waiting_permission|completed|failed|interrupted,
    "classification": ..., "detail": ..., "response_text": ..., "termination_reason": ...}``.
    """
    status = (summary or {}).get("status") or ""
    steps = [step for step in (steps or []) if isinstance(step, dict)]
    metadatas = [item for item in (executor_metadatas or []) if isinstance(item, dict)]
    statuses = [str(step.get("status") or "") for step in steps]
    types = [str(step.get("type") or "") for step in steps]
    waiting = any(state == STEP_WAITING for state in statuses) or (STEP_ASK_QUESTION in types and status == RUN_STATUS_IDLE)
    error_texts = [_step_text(step) for step, kind, state in zip(steps, types, statuses) if kind == STEP_ERROR_MESSAGE or state == STEP_ERROR]
    error_text = next((text for text in error_texts if text), "")
    planner_text = ""
    for step, kind, state in zip(reversed(steps), reversed(types), reversed(statuses)):
        if kind == STEP_PLANNER_RESPONSE and state == STEP_DONE:
            planner_text = _step_text(step)
            if planner_text:
                break
    latest = max(metadatas, key=lambda item: int(item.get("lastStepIdx") or 0), default=None)
    termination = str((latest or {}).get("terminationReason") or "")
    quota_hit = bool(error_text) and bool(QUOTA_ERROR_PATTERN.search(error_text))

    if status in RUN_STATUS_ACTIVE:
        if waiting:
            return {"state": "waiting_permission", "classification": "permission_required", "detail": "provider is waiting for a permission/answer", "response_text": planner_text, "termination_reason": termination}
        return {"state": "running", "classification": None, "detail": None, "response_text": planner_text, "termination_reason": termination}
    if status != RUN_STATUS_IDLE:
        return {"state": "running", "classification": None, "detail": f"run status {status or 'unknown'}", "response_text": planner_text, "termination_reason": termination}
    # IDLE: the language server reports nothing running for this conversation.
    if not steps and seconds_since_start < idle_grace_seconds:
        return {"state": "running", "classification": None, "detail": "idle before first step (grace)", "response_text": "", "termination_reason": termination}
    if termination == TERMINATION_CANCELED or any(state == STEP_CANCELED for state in statuses):
        return {"state": "interrupted", "classification": "cancelled", "detail": "provider reports user cancellation", "response_text": planner_text, "termination_reason": termination}
    if quota_hit:
        return {"state": "failed", "classification": "quota_exhausted", "detail": error_text[:MAX_EVIDENCE_CHARS], "response_text": planner_text, "termination_reason": termination}
    if termination == TERMINATION_TOKEN_BUDGET:
        return {"state": "failed", "classification": "token_budget_exceeded", "detail": termination, "response_text": planner_text, "termination_reason": termination}
    if termination in TERMINATION_MAX_INVOCATIONS:
        return {"state": "failed", "classification": "max_invocations", "detail": termination, "response_text": planner_text, "termination_reason": termination}
    if termination == TERMINATION_ERROR or error_text:
        detail = error_text[:MAX_EVIDENCE_CHARS] or termination
        if AUTH_ERROR_PATTERN.search(detail):
            return {"state": "failed", "classification": "auth_transient", "detail": detail, "response_text": planner_text, "termination_reason": termination}
        return {"state": "failed", "classification": "provider_error", "detail": detail, "response_text": planner_text, "termination_reason": termination}
    if waiting:
        return {"state": "waiting_permission", "classification": "permission_required", "detail": "provider idle on an unanswered permission/question", "response_text": planner_text, "termination_reason": termination}
    if not latest and not planner_text:
        if seconds_since_start < idle_grace_seconds:
            return {"state": "running", "classification": None, "detail": "idle before the executor started (grace)", "response_text": "", "termination_reason": termination}
        return {"state": "failed", "classification": "prompt_not_started", "detail": "conversation exists but the executor never ran (prompt swallowed before READY?)", "response_text": "", "termination_reason": termination}
    if not planner_text:
        return {"state": "failed", "classification": "empty_response", "detail": "run finished without a final planner response", "response_text": "", "termination_reason": termination}
    return {"state": "completed", "classification": None, "detail": None, "response_text": planner_text[:MAX_RESPONSE_CHARS], "termination_reason": termination}


class AgCliProcess:
    """Bounded runner for one ``agentapi`` CLI invocation (JSON on stdout)."""

    def __init__(self, process: subprocess.Popen, timeout: float = 60.0):
        self.process = process
        self.timeout = timeout
        self.stdout = ""
        self.stderr = ""
        self.timed_out = False

    def communicate(self) -> tuple[str, str]:
        try:
            self.stdout, self.stderr = self.process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            _kill_process_tree(self.process)
            raise AgLaunchError("dispatch_timeout", f"agentapi did not return within {self.timeout:g}s; process tree terminated")
        return self.stdout or "", self.stderr or ""

    def terminate(self) -> None:
        if self.process.poll() is None:
            _kill_process_tree(self.process)


class _ConversationHandle:
    """Process-like handle so execution_runner._stopped() can prove the provider stopped."""

    def __init__(self, runner: "OfficialAgCliRunner", prepared: PreparedLaunch):
        self._runner = runner
        self._prepared = prepared
        self.pid = prepared.pid

    def poll(self):
        state = self._prepared._target
        if not state.get("conversation_id") or state.get("status") in TERMINAL_STATUSES:
            return 0
        return None

    def wait(self, timeout=None):
        deadline = self._runner._clock() + (timeout if timeout is not None else 0)
        while self.poll() is None:
            self._runner._refresh_terminal_state(self._prepared)
            if self.poll() is not None:
                break
            if self._runner._clock() >= deadline:
                raise subprocess.TimeoutExpired("antigravity conversation", timeout)
            self._runner._sleep(min(1.0, self._runner.poll_interval_seconds))
        return 0


class OfficialAgCliRunner:
    """Language-server-backed Antigravity adapter (mode ``cli``)."""

    def __init__(self, executable_resolver: Callable[..., Any] | None = None,
                 auth_verifier: Callable[[], str] | None = None, default_mode: str = "cli", *,
                 transport: str = TRANSPORT_IDE_BRIDGE,
                 discover: Callable[..., LanguageServerEndpoint] | None = None,
                 client_factory: Callable[..., AgLanguageServerClient] | None = None,
                 popen: Callable[..., Any] = subprocess.Popen,
                 poll_interval_seconds: float = 2.0, agentapi_timeout_seconds: float = 60.0,
                 permission_stall_seconds: float = 90.0, cancel_reconcile_seconds: float = 30.0,
                 manager_home: str | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, kill_tree: Callable[[Any], None] = _kill_process_tree):
        if transport not in TRANSPORTS:
            raise ValueError(f"unknown Antigravity transport {transport!r}; expected one of {TRANSPORTS}")
        self.transport = transport
        self._resolve_executable = executable_resolver
        self._verify_auth = auth_verifier
        self.default_mode = default_mode
        self._discover = discover or discover_language_server
        self._client_factory = client_factory or AgLanguageServerClient
        self._popen = popen
        self.poll_interval_seconds = poll_interval_seconds
        self.agentapi_timeout_seconds = agentapi_timeout_seconds
        self.permission_stall_seconds = permission_stall_seconds
        self.cancel_reconcile_seconds = cancel_reconcile_seconds
        self._manager_home = manager_home
        self._clock = clock
        self._sleep = sleep
        self._kill_tree = kill_tree

    # ------------------------------------------------------------------ helpers
    def _launcher_argv(self, endpoint: LanguageServerEndpoint) -> list[str]:
        if self._resolve_executable is not None:
            resolved = self._resolve_executable()
            if isinstance(resolved, tuple):
                return [str(resolved[0]), *list(resolved[1])]
            return [str(resolved)]
        if endpoint.executable and Path(endpoint.executable).is_file():
            return [endpoint.executable, "agentapi"]
        executable, prefix = resolve_ag_cli_executable()
        return [executable, *prefix]

    def _state(self, prepared: PreparedLaunch) -> dict[str, Any]:
        return prepared._target

    def _persist(self, prepared: PreparedLaunch, **fields: Any) -> None:
        state = self._state(prepared)
        state.update(fields)
        try:
            write_run_state(state, self._manager_home)
        except Exception:
            # Local readback state must never break the live run.
            pass

    def _client(self, prepared: PreparedLaunch) -> AgLanguageServerClient:
        client = getattr(prepared, "_ls_client", None)
        if client is None:
            client = self._client_factory(prepared._endpoint, timeout=prepared._request.timeout_seconds)
            prepared._ls_client = client
        return client

    def is_alive(self) -> bool:
        """AgRunner facade protocol: a trusted language server is discoverable right now (no side effects)."""
        try:
            self._discover(timeout=10.0)
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------ READY handshake
    def ready_probe(self, client: AgLanguageServerClient, request: LaunchRequest) -> dict[str, Any]:
        """Raise AgLaunchError unless AG is provably ready for a new task; return readiness evidence."""
        try:
            client.get_status()
        except AgLsError as exc:
            raise AgLaunchError("ls_unreachable", exc.detail) from exc
        try:
            user_status = client.get_user_status()
        except AgLsError as exc:
            classification = "auth_unavailable" if exc.classification == "rpc_unauthenticated" else "ls_unreachable"
            raise AgLaunchError(classification, exc.detail) from exc
        status = user_status.get("userStatus") if isinstance(user_status, dict) else None
        email = (status or {}).get("email") if isinstance(status, dict) else None
        if not email:
            raise AgLaunchError("auth_unavailable", "GetUserStatus carries no signed-in account; the IDE is not logged in")
        try:
            buckets = _bucket_records(client.retrieve_user_quota_summary())
        except AgLsError as exc:
            raise AgLaunchError("quota_unverified", exc.detail) from exc
        gemini = [b for b in buckets if "gemini" in (b["bucket_id"] + " " + b["group"]).lower()]
        catalog_model = None
        model = None
        if self.transport == TRANSPORT_IDE_BRIDGE:
            # The model is the server's own placeholder enum from its live
            # catalog (never a table ADM guessed); an unknown or exhausted
            # model refuses to launch. Gate on the quota group the chosen
            # model actually draws from.
            try:
                catalog_model = resolve_model_placeholder(user_status, request.model)
            except AgLsError as exc:
                raise AgLaunchError(exc.classification, exc.detail) from exc
            if "gemini" in str(catalog_model["model_id"]).lower():
                relevant = gemini or buckets
            else:
                relevant = [b for b in buckets if b not in gemini] or buckets
        else:
            model = map_agentapi_model(request.model)
            # agentapi models are Gemini models: gate on the Gemini group when it is
            # identifiable, otherwise on every bucket.
            relevant = gemini or buckets
        exhausted = [b for b in relevant if b["remaining_fraction"] <= 0.0]
        if exhausted and len(exhausted) == len(relevant):
            until = max((b["reset_time"] for b in exhausted if b.get("reset_time")), default=None)
            raise AgLaunchError("quota_exhausted", f"unavailable_until={until}; buckets={[b['bucket_id'] for b in exhausted]}")
        # Quota readability does not imply dispatchability: an IDE-hosted
        # language server answers every status RPC while its projects store
        # stays uninitialized, which is exactly what `agentapi
        # new-conversation` needs (it always sends a project_env_config).
        # Refuse to launch rather than spawn a CLI that cannot create a
        # conversation -- read-only probe, no model turn.
        route = probe_dispatch_route(client, transport=self.transport)
        if not route["available"]:
            raise AgLaunchError("dispatch_route_unavailable", f"{route['reason']}: {route.get('detail') or ''}")
        configs = ((status or {}).get("cascadeModelConfigData") or {}).get("clientModelConfigs") if isinstance(status, dict) else None
        model_ids = [c.get("modelId") for c in (configs or []) if isinstance(c, dict)]
        return {
            "ready": True, "account_email": email, "plan_name": ((status.get("planStatus") or {}).get("planInfo") or {}).get("planName"),
            "agentapi_model": model, "models_available": len(model_ids), "dispatch_route": route,
            "transport": self.transport, "model": catalog_model,
            "quota_min_remaining_fraction": min(b["remaining_fraction"] for b in relevant),
            "quota_degraded": bool(exhausted), "observed_at": utc_now(),
        }

    def prepare(self, request: LaunchRequest) -> PreparedLaunch:
        cwd = Path(str(request.working_directory or ""))
        if not str(request.working_directory or "").strip() or not cwd.is_absolute() or not cwd.is_dir():
            raise AgLaunchError("invalid_request", "working_directory must be an existing absolute directory")
        if self._verify_auth is not None:
            self._verify_auth()
        try:
            endpoint = self._discover(timeout=request.timeout_seconds)
        except AgLsError as exc:
            raise AgLaunchError(exc.classification, exc.detail) from exc
        client = self._client_factory(endpoint, timeout=request.timeout_seconds)
        readiness = self.ready_probe(client, request)
        argv = None
        if self.transport == TRANSPORT_AGENTAPI:
            argv = self._launcher_argv(endpoint)
            if not Path(argv[0]).is_file():
                raise AgLaunchError("route_unavailable", f"agentapi entrypoint not found: {argv[0]}")
            mode = request.force_mode if request.force_mode in ("cli", "headless") else self.default_mode
        else:
            mode = "live_ide"
        thread_id = f"ag-{'live' if mode == 'live_ide' else mode}-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        catalog_model = readiness.get("model") or {}
        state = {
            "thread_id": thread_id, "provider": "antigravity", "status": "prepared", "conversation_id": None,
            "transport": self.transport, "provider_run_id": None, "binding": None,
            "model_id": catalog_model.get("model_id"), "model_placeholder": catalog_model.get("placeholder"),
            "project_id": request.project_id, "working_directory": str(cwd), "model": request.model,
            "agentapi_model": readiness["agentapi_model"], "language_server": endpoint.evidence(),
            "readiness": readiness, "prepared_at": now, "started_at": None, "last_event": "provider_prepared",
            "last_event_at": now, "step_cursor": 0, "transcript_path": None, "termination_reason": None,
            "cancel_evidence": None, "workspace_check": None,
        }
        prepared = PreparedLaunch(
            thread_id=thread_id, session_path=None, pid=endpoint.pid,
            process_creation_identity=endpoint.creation_identity or f"ls-pid-{endpoint.pid}",
            prepared_at=now, mode=mode, _target=state, _request=request,
        )
        prepared._endpoint = endpoint
        prepared._ls_client = client
        prepared._argv = argv
        prepared._process = _ConversationHandle(self, prepared)
        prepared._cli = None
        prepared._closed = False
        self._persist(prepared)
        return prepared

    # ------------------------------------------------------------------ dispatch
    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared._started:
            raise AgLaunchError("already_started", "Prepared Antigravity launch was already started")
        if not isinstance(prompt, str) or not prompt.strip():
            raise AgLaunchError("invalid_request", "prompt must be non-empty")
        prepared._started = True
        request = prepared._request
        endpoint: LanguageServerEndpoint = prepared._endpoint
        client = self._client(prepared)
        try:
            client.get_status()
        except AgLsError as exc:
            raise AgLaunchError("ls_unreachable", f"language server went away before dispatch: {exc.detail}") from exc
        if self.transport == TRANSPORT_IDE_BRIDGE:
            return self._start_ide_bridge(prepared, prompt)

        state = self._state(prepared)
        argv = [*prepared._argv, "new-conversation"]
        if state.get("agentapi_model"):
            argv.append(f"--model={state['agentapi_model']}")
        argv.append(f"--title=adm-{prepared.thread_id}")
        argv.append(prompt)
        env = sanitize_ag_environment(os.environ)
        env.update(endpoint.agentapi_env())
        project_override = os.environ.get(PROJECT_ID_ENV)
        if project_override:
            env["ANTIGRAVITY_PROJECT_ID"] = project_override
        try:
            process = self._popen(argv, cwd=str(request.working_directory), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        except Exception as exc:
            raise AgLaunchError("spawn_failed", f"failed to spawn agentapi: {exc}") from exc
        cli = AgCliProcess(process, timeout=self.agentapi_timeout_seconds)
        prepared._cli = cli
        state["agentapi_pid"] = getattr(process, "pid", None)
        try:
            stdout, stderr = cli.communicate()
        except AgLaunchError as exc:
            self._persist(prepared, status="failed", last_event="dispatch_timeout", last_event_at=utc_now(), termination_reason=exc.classification)
            raise
        try:
            conversation_id = parse_new_conversation_output(stdout)
        except AgLaunchError as exc:
            detail = exc.detail
            if stderr.strip():
                detail = f"{detail} | stderr: {redact(stderr.strip())[:MAX_EVIDENCE_CHARS]}"
            self._persist(prepared, status="failed", last_event="dispatch_failed", last_event_at=utc_now(), termination_reason=exc.classification)
            raise AgLaunchError(exc.classification, detail) from exc
        transcript = str(_safe_home() / ".gemini" / "antigravity-ide" / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl")
        started_at = utc_now()
        prepared.session_path = transcript
        self._persist(prepared, conversation_id=conversation_id, status="running", started_at=started_at,
                      last_event="turn_started", last_event_at=started_at, transcript_path=transcript)
        self._verify_workspace(prepared, conversation_id)
        running = RunningLaunch(prepared=prepared, turn_id=f"turn-{uuid.uuid4().hex[:8]}", started_at=started_at)
        running._started_clock = self._clock()
        return running

    def _start_ide_bridge(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        """transport=ide_bridge: AddTrackedWorkspace -> StartCascade -> claim binding -> verify workspace -> SendUserCascadeMessage.

        The cascade is always NEW (never an adopted/foreground one), it is bound
        to exactly the ADM working directory, and the workspace readback is
        checked BEFORE the first model turn so a mismatch never runs anything.
        """
        request = prepared._request
        endpoint: LanguageServerEndpoint = prepared._endpoint
        client = self._client(prepared)
        state = self._state(prepared)
        workspace = Path(str(request.working_directory)).resolve()
        try:
            client.add_tracked_workspace(str(workspace))
        except AgLsError as exc:
            self._persist(prepared, status="failed", last_event="workspace_bind_failed", last_event_at=utc_now(),
                          termination_reason="workspace_bind_failed")
            raise AgLaunchError("workspace_bind_failed", f"AddTrackedWorkspace refused {workspace}: {exc.detail}") from exc
        try:
            response = client.start_cascade([workspace.as_uri()])
        except AgLsError as exc:
            classification = _classify_rpc_failure(exc)
            self._persist(prepared, status="failed", last_event="dispatch_failed", last_event_at=utc_now(), termination_reason=classification)
            raise AgLaunchError(classification, f"StartCascade failed: {exc.detail}") from exc
        conversation_id = _find_conversation_id(response) if isinstance(response, dict) else None
        if not conversation_id:
            self._persist(prepared, status="failed", last_event="dispatch_failed", last_event_at=utc_now(), termination_reason="malformed_output")
            raise AgLaunchError("malformed_output", f"StartCascade answered without a cascadeId: {json.dumps(redact(response))[:MAX_EVIDENCE_CHARS]}")
        binding = self._claim_binding(prepared, conversation_id, endpoint)
        transcript = str(_safe_home() / ".gemini" / "antigravity-ide" / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl")
        prepared.session_path = transcript
        bound_at = utc_now()
        self._persist(prepared, conversation_id=conversation_id, status="bound", binding=binding, transcript_path=transcript,
                      last_event="cascade_bound", last_event_at=bound_at)
        self._verify_workspace(prepared, conversation_id)
        try:
            client.send_user_cascade_message(conversation_id, prompt, model_placeholder=state["model_placeholder"],
                                             ide_version=endpoint.ls_version)
        except AgLsError as exc:
            classification = _classify_rpc_failure(exc)
            self.cancel(prepared, reason="dispatch_failed")
            self._persist(prepared, status="failed", last_event="dispatch_failed", last_event_at=utc_now(), termination_reason=classification)
            raise AgLaunchError(classification, f"SendUserCascadeMessage failed: {exc.detail}") from exc
        started_at = utc_now()
        self._persist(prepared, status="running", started_at=started_at, last_event="turn_started", last_event_at=started_at)
        running = RunningLaunch(prepared=prepared, turn_id=f"turn-{uuid.uuid4().hex[:8]}", started_at=started_at)
        running._started_clock = self._clock()
        return running

    def _claim_binding(self, prepared: PreparedLaunch, conversation_id: str, endpoint: LanguageServerEndpoint) -> dict[str, Any]:
        """ONE_EXECUTION_ONE_ACTIVE_AG_BINDING: this execution owns exactly one, freshly created cascade.

        A cascade id already claimed by another non-terminal run is never
        adopted or touched (not even cancelled -- it is someone else's live
        session); the launch fails ``binding_ambiguous`` instead.
        """
        try:
            others = [item for item in list_run_states(self._manager_home, include_terminal=False)
                      if item.get("conversation_id") == conversation_id and item.get("thread_id") != prepared.thread_id]
        except Exception:
            others = []
        if others:
            self._persist(prepared, status="failed", last_event="binding_ambiguous", last_event_at=utc_now(), termination_reason="binding_ambiguous")
            raise AgLaunchError("binding_ambiguous", f"cascade {conversation_id} is already bound to {[o.get('thread_id') for o in others][:3]}")
        return {"invariant": BINDING_INVARIANT, "conversation_id": conversation_id, "thread_id": prepared.thread_id,
                "language_server_pid": endpoint.pid, "language_server_identity": endpoint.creation_identity,
                "workspace": str(Path(str(prepared._request.working_directory)).resolve()), "claimed_at": utc_now()}

    def _verify_workspace(self, prepared: PreparedLaunch, conversation_id: str) -> None:
        """STOP (cancel + fail) when the provider's workspace provably differs from ADM's working directory."""
        request = prepared._request
        client = self._client(prepared)
        uris: list[str] = []
        try:
            metadata = client.get_conversation_metadata(conversation_id).get("metadata") or {}
            raw = metadata.get("workspaceUris") or []
            uris = [str(item) for item in raw if isinstance(item, str)]
            for item in metadata.get("workspaces") or []:
                if isinstance(item, dict):
                    # Live shape (IDE 1.107.0): workspaces[].workspaceFolderAbsoluteUri / gitRootAbsoluteUri.
                    for key in ("workspaceFolderAbsoluteUri", "gitRootAbsoluteUri", "uri", "workspaceUri", "path"):
                        if isinstance(item.get(key), str):
                            uris.append(item[key])
        except AgLsError as exc:
            self._persist(prepared, workspace_check={"result": "unverified", "reason": exc.classification})
            return
        if not uris:
            verdict = {"result": "unverified", "reason": "provider exposes no workspace for the conversation"}
            self._persist(prepared, workspace_check=verdict)
            if request.sandbox != "read-only":
                self.cancel(prepared, reason="workspace_unverified")
                raise AgLaunchError("workspace_unverified", "cannot prove the Antigravity workspace matches the ADM working directory; write task refused")
            return
        matches = [uri for uri in uris if _same_path(_uri_path(uri), str(request.working_directory))]
        if matches:
            self._persist(prepared, workspace_check={"result": "verified", "workspace": matches[0]})
            return
        self._persist(prepared, workspace_check={"result": "mismatch", "workspaces": uris[:5]})
        self.cancel(prepared, reason="workspace_mismatch")
        raise AgLaunchError("workspace_mismatch", f"Antigravity conversation workspace {uris[:3]} != {request.working_directory}")

    def set_heartbeat(self, running: RunningLaunch, callback: Callable[[str], Any]) -> None:
        running._heartbeat = callback

    # ------------------------------------------------------------------ observation
    def _observe(self, prepared: PreparedLaunch, *, fetch_steps: bool) -> tuple[dict | None, list | None, list | None]:
        client = self._client(prepared)
        conversation_id = self._state(prepared)["conversation_id"]
        summaries = client.get_all_cascade_trajectories().get("trajectorySummaries") or {}
        summary = summaries.get(conversation_id) if isinstance(summaries, dict) else None
        steps = metadatas = None
        if fetch_steps:
            steps = client.get_cascade_trajectory_steps(conversation_id).get("steps")
            if not isinstance(steps, list):
                raise AgLaunchError("malformed_provider_state", "GetCascadeTrajectorySteps.steps is not a list")
            try:
                metadatas = client.get_cascade_trajectory_executor_metadatas(conversation_id).get("executorMetadata") or []
            except AgLsError:
                metadatas = []
        return summary, steps, metadatas

    def _refresh_terminal_state(self, prepared: PreparedLaunch) -> None:
        state = self._state(prepared)
        if not state.get("conversation_id") or state.get("status") in TERMINAL_STATUSES:
            return
        try:
            summary, steps, metadatas = self._observe(prepared, fetch_steps=True)
        except (AgLsError, AgLaunchError):
            return
        verdict = classify_run(summary, steps, metadatas, seconds_since_start=float("inf"))
        if verdict["state"] in ("completed", "failed", "interrupted"):
            self._persist(prepared, status=verdict["state"], termination_reason=verdict["termination_reason"],
                          last_event="turn_terminal", last_event_at=utc_now())

    def wait(self, running: RunningLaunch) -> LaunchOutcome:
        prepared = running.prepared
        state = self._state(prepared)
        request = prepared._request
        started = getattr(running, "_started_clock", None)
        if started is None:
            started = self._clock()
        deadline = started + request.turn_timeout_seconds
        last_step_count = -1
        waiting_since: float | None = None
        consecutive_failures = 0
        verdict: dict[str, Any] | None = None
        cancel_evidence = None

        def outcome(status: str, classification: str | None, detail: str | None, response: str | None, extra: dict | None = None) -> LaunchOutcome:
            stats = {
                "conversation_id": state.get("conversation_id"), "provider_run_ref": f"antigravity:conversation:{state.get('conversation_id')}",
                "language_server_pid": state.get("language_server", {}).get("pid"), "steps_observed": max(last_step_count, 0),
                "termination_reason": (verdict or {}).get("termination_reason"), "transcript_path": state.get("transcript_path"),
                "workspace_check": state.get("workspace_check"), "cancel_evidence": cancel_evidence or state.get("cancel_evidence"),
                "transport": state.get("transport"), "provider_run_id": state.get("provider_run_id"),
                "model_id": state.get("model_id"), "binding": state.get("binding"),
            }
            if extra:
                stats.update(extra)
            self._persist(prepared, status=status if status != "interrupted" else ("cancelled" if classification == "cancelled" else "interrupted"),
                          last_event="turn_terminal", last_event_at=utc_now(), termination_reason=classification or stats["termination_reason"])
            return LaunchOutcome(status=status, thread_id=prepared.thread_id, turn_id=running.turn_id, completed_at=utc_now(),
                                 failure_classification=classification, failure_detail=detail, response_text=response or None, stats=stats)

        while True:
            if running._cancelled:
                cancel_evidence = self.cancel(prepared, reason="cancelled_by_runner")
                return outcome("interrupted", "cancelled", "Execution was cancelled by runner", None)
            now = self._clock()
            if now >= deadline:
                cancel_evidence = self.cancel(prepared, reason="turn_timeout")
                return outcome("failed", "turn_timeout", f"Antigravity turn exceeded timeout of {request.turn_timeout_seconds:g} seconds; conversation stopped", None)
            try:
                summary, _, _ = self._observe(prepared, fetch_steps=False)
                step_count = int((summary or {}).get("stepCount") or 0)
                fetch = step_count != last_step_count or (summary or {}).get("status") not in RUN_STATUS_ACTIVE
                steps = metadatas = None
                if fetch:
                    _, steps, metadatas = self._observe(prepared, fetch_steps=True)
                consecutive_failures = 0
            except (AgLsError, AgLaunchError) as exc:
                consecutive_failures += 1
                if isinstance(exc, AgLaunchError) and exc.classification == "malformed_provider_state":
                    cancel_evidence = self.cancel(prepared, reason="malformed_provider_state")
                    return outcome("failed", "malformed_provider_state", exc.detail, None)
                if consecutive_failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                    classification = "ls_unreachable" if getattr(exc, "classification", "") in ("ls_unreachable", "malformed_response") else "provider_state_unavailable"
                    return outcome("failed", classification, f"language server stopped answering while the conversation was running: {getattr(exc, 'detail', exc)}", None)
                self._sleep(self.poll_interval_seconds)
                continue
            if steps is not None:
                if step_count > last_step_count:
                    if running._heartbeat and last_step_count >= 0:
                        running._heartbeat("provider_event")
                    last_step_count = step_count
                    self._persist(prepared, step_cursor=step_count, last_event="provider_event", last_event_at=utc_now())
                elif running._heartbeat:
                    running._heartbeat("provider_heartbeat")
                verdict = classify_run(summary, steps, metadatas, seconds_since_start=now - started)
                latest_executor = max((m for m in (metadatas or []) if isinstance(m, dict)),
                                      key=lambda m: int(m.get("lastStepIdx") or 0), default=None)
                run_id = (latest_executor or {}).get("executionId")
                if isinstance(run_id, str) and run_id and state.get("provider_run_id") != run_id:
                    # AG's own executor run id: distinct from the cascade/conversation id and from ADM's ids.
                    self._persist(prepared, provider_run_id=run_id)
            elif running._heartbeat:
                running._heartbeat("provider_heartbeat")
            if verdict is not None:
                if verdict["state"] == "completed":
                    return outcome("completed", None, None, verdict["response_text"])
                if verdict["state"] == "failed":
                    return outcome("failed", verdict["classification"], verdict["detail"], verdict["response_text"] or None)
                if verdict["state"] == "interrupted":
                    return outcome("interrupted", verdict["classification"], verdict["detail"], verdict["response_text"] or None)
                if verdict["state"] == "waiting_permission":
                    waiting_since = waiting_since if waiting_since is not None else now
                    if now - waiting_since >= self.permission_stall_seconds:
                        cancel_evidence = self.cancel(prepared, reason="permission_stall")
                        return outcome("failed", "permission_stall", f"provider waited on a permission/question for {self.permission_stall_seconds:g}s without a headless answer; conversation stopped", verdict["response_text"] or None)
                else:
                    waiting_since = None
            self._sleep(self.poll_interval_seconds)

    # ------------------------------------------------------------------ cancellation
    def cancel(self, handle: PreparedLaunch | RunningLaunch, reason: str = "cancelled") -> dict[str, Any]:
        """Stop the conversation, kill the CLI tree, reconcile with the server, persist evidence."""
        prepared = handle.prepared if isinstance(handle, RunningLaunch) else handle
        state = self._state(prepared)
        evidence: dict[str, Any] = {"reason": reason, "requested_at": utc_now(), "rpc": {}, "cli_process_killed": False,
                                    "confirmed": False, "final_run_status": None, "confirmed_at": None}
        cli = getattr(prepared, "_cli", None)
        if cli is not None and cli.process.poll() is None:
            self._kill_tree(cli.process)
            evidence["cli_process_killed"] = True
        conversation_id = state.get("conversation_id")
        if conversation_id:
            client = self._client(prepared)
            body = {"cascadeId": conversation_id, "conversationId": conversation_id}
            for rpc in ("CancelCascadeInvocation", "ForceStopCascadeTree"):
                try:
                    client.call(rpc, body)
                    evidence["rpc"][rpc] = "ok"
                except AgLsError as exc:
                    evidence["rpc"][rpc] = exc.classification
            deadline = self._clock() + self.cancel_reconcile_seconds
            while True:
                try:
                    summaries = client.get_all_cascade_trajectories().get("trajectorySummaries") or {}
                    status = (summaries.get(conversation_id) or {}).get("status") if isinstance(summaries, dict) else None
                except AgLsError as exc:
                    status = f"unobservable:{exc.classification}"
                evidence["final_run_status"] = status
                if status is None or status == RUN_STATUS_IDLE or (isinstance(status, str) and status.startswith("unobservable")):
                    evidence["confirmed"] = status == RUN_STATUS_IDLE or status is None
                    evidence["confirmed_at"] = utc_now()
                    break
                if self._clock() >= deadline:
                    break
                self._sleep(min(1.0, self.poll_interval_seconds))
        else:
            evidence["confirmed"] = True
            evidence["confirmed_at"] = utc_now()
        state["cancel_evidence"] = evidence
        if state.get("status") not in TERMINAL_STATUSES:
            self._persist(prepared, status="cancelled" if evidence["confirmed"] else "interrupted", last_event="cancelled",
                          last_event_at=utc_now(), termination_reason=reason)
        else:
            self._persist(prepared)
        return evidence

    def close(self, handle: PreparedLaunch | RunningLaunch) -> None:
        prepared = handle.prepared if isinstance(handle, RunningLaunch) else handle
        if prepared is None or getattr(prepared, "_closed", False):
            return
        try:
            state = self._state(prepared)
            if state.get("conversation_id") and state.get("status") not in TERMINAL_STATUSES:
                self.cancel(prepared, reason="closed_while_running")
            cli = getattr(prepared, "_cli", None)
            if cli is not None:
                cli.terminate()
        finally:
            prepared._closed = True

    # ------------------------------------------------------------------ readback
    def read_back(self, thread_id: str) -> dict[str, Any] | None:
        """Durable run-state readback (restart-safe); see manager.ag_run_state."""
        return read_run_state(thread_id, self._manager_home)


# Backward compatibility aliases
AgCliRunner = OfficialAgCliRunner
AgHeadlessProcess = AgCliProcess
