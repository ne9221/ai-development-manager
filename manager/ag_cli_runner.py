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
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator

from manager.ag_runner import (
    AgLaunchError,
    AgNormalizedEvent,
    LaunchOutcome,
    LaunchRequest,
    MODE_TO_ROUTE,
    PreparedLaunch,
    RunningLaunch,
    normalize_event,
    utc_now,
)


# Env vars that could redirect the child process onto a secondary GCP /
# Vertex / API billing route. Stripped unconditionally before spawning the
# official AG CLI so it cannot silently switch billing identity.
# OPENAI_API_KEY / ANTHROPIC_API_KEY are deliberately NOT included: they do
# not participate in Google/Vertex billing-route selection for this CLI, so
# stripping them isn't a billing-isolation concern in scope here.
SECONDARY_BILLING_ENV_VARS = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "VERTEX_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "GCP_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "VERTEXAI_PROJECT",
    "VERTEXAI_LOCATION",
    "CLOUDSDK_CORE_PROJECT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
)

# GOOGLE_APPLICATION_CREDENTIALS is handled separately from the strip list
# above (see sanitize_ag_environment): verified against the installed
# google-auth `_default.py` checker chain, `google.auth.default()` runs its
# explicit-env-var checker FIRST and outside any try/except that would fall
# through to the next checker. Pointing it at a file that does not exist
# makes that checker raise DefaultCredentialsError immediately, so ADC
# discovery fails closed instead of silently continuing to the gcloud-SDK
# cached-credentials checker or the GCE metadata-server checker. Merely
# unsetting the var (the previous behavior) does NOT achieve this -- it just
# lets those later checkers run and silently succeed against any local
# `gcloud auth application-default login` credentials.
_ADC_FAIL_CLOSED_SENTINEL_NAME = ".adm-ag-adc-fail-closed-sentinel-does-not-exist.json"


def _safe_home() -> Path:
    """Return the canonical real user home directory."""
    try:
        return Path.home().resolve()
    except Exception:
        fallback = os.environ.get("USERPROFILE") or os.environ.get("HOME") or "/tmp"
        return Path(fallback).resolve()


def resolve_canonical_gemini_home(
    raw_path: str | Path | None = None,
    base_env: dict[str, str] | None = None,
) -> Path:
    """Resolve and strictly validate the canonical Antigravity/Gemini home directory.

    Enforces that the path resolves to the canonical `.gemini` directory under the
    current user's home profile, defending against directory traversal (..),
    symlink/junction redirection outside the user profile, alternate drive aliases,
    inherited poison from HOME/USERPROFILE/GEMINI_HOME, and other-user profile
    takeovers. Fails closed with AgLaunchError if an invalid or external path
    is specified.
    """
    expected_home = _safe_home()
    expected_gemini = (expected_home / ".gemini").resolve()

    cand_str: str | None = None
    if raw_path is not None:
        cand_str = str(raw_path)
    else:
        env = os.environ if base_env is None else base_env
        cand_str = env.get("GEMINI_HOME")

    if not cand_str:
        return expected_gemini

    cand_path = Path(cand_str)
    try:
        resolved_cand = cand_path.resolve()
    except Exception as exc:
        raise AgLaunchError(
            "unverified_identity",
            f"Failed to resolve GEMINI_HOME '{cand_str}': {exc}. Fail closed.",
        )

    norm_cand = os.path.normcase(os.path.normpath(str(resolved_cand)))
    norm_expected = os.path.normcase(os.path.normpath(str(expected_gemini)))

    if norm_cand != norm_expected:
        raise AgLaunchError(
            "unverified_identity",
            f"Untrusted GEMINI_HOME '{cand_str}' resolved to '{resolved_cand}', which does "
            f"not match canonical expected profile path '{expected_gemini}'. Fail closed.",
        )

    return expected_gemini


def sanitize_ag_environment(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Strip/override env vars that could switch the child process onto a
    secondary GCP/Vertex/API billing route or external GEMINI_HOME profile,
    so the official AG CLI route strictly stays on the verified local Google
    AI Pro account profile.
    """
    env = dict(os.environ if base_env is None else base_env)
    for var in SECONDARY_BILLING_ENV_VARS:
        env.pop(var, None)
    env["GOOGLE_APPLICATION_CREDENTIALS"] = str(_safe_home() / _ADC_FAIL_CLOSED_SENTINEL_NAME)
    env["GEMINI_HOME"] = str(resolve_canonical_gemini_home())
    return env


def _parse_timestamp(val: Any) -> float | None:
    """Parse integer, float, numeric string, or ISO-8601 string into UTC epoch seconds.
    Returns None if malformed.
    """
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        # Milliseconds timestamp (> 1e11) vs seconds
        return float(val) / 1000.0 if val > 1e11 else float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # Numeric string (e.g. "1723900000" or "1723900000000")
        try:
            num = float(s)
            return num / 1000.0 if num > 1e11 else num
        except ValueError:
            pass
        # ISO-8601 timestamp string
        try:
            s_iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
            dt = datetime.fromisoformat(s_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    return None


def _parse_local_credential_token(path: Path) -> bool:
    """True only if `path` is a real, parseable credential file containing a
    non-empty token/session field AND valid future expiry metadata.

    If expiry metadata is present:
      - expired -> reject (False)
      - malformed expiry -> fail closed (False)
      - future expiry -> allow (True)
    If credential schema has NO verifiable expiry, we do not upgrade static token
    existence to strong identity proof (returns False, downgrading to CLI positive proof).
    """
    try:
        if not path.is_file():
            return False
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False

    token_fields = ("access_token", "refresh_token", "id_token", "session_token", "token")
    has_token = any(isinstance(data.get(field), str) and data.get(field).strip() for field in token_fields)
    if not has_token:
        return False

    expiry_fields = ("expiry", "expires_at", "expiration", "expires", "exp", "valid_until")
    expiry_val = None
    for field in expiry_fields:
        if field in data:
            expiry_val = data[field]
            break

    if expiry_val is None:
        # No verifiable expiry metadata in schema: cannot treat static token as strong proof
        return False

    expiry_epoch = _parse_timestamp(expiry_val)
    if expiry_epoch is None:
        # Malformed expiry metadata -> fail closed
        return False

    if expiry_epoch <= time.time():
        # Expired credential -> reject
        return False

    return True


_EXPLICIT_UNAUTHENTICATED_PATTERNS = (
    r"\bguest\b",
    r"\bnot\s+authenticated\b",
    r"\bunauthenticated\b",
    r"\bunauthorized\b",
    r"\blogin\s+required\b",
    r"\bnot\s+logged\s+in\b",
    r"\bplease\s+log\s*in\b",
    r"\bplease\s+run\b",
    r"未登入",
    r"未登录",
    r"請登入",
    r"请登录",
    r"訪客",
    r"访客",
)

_POSITIVE_AUTH_TEXT_PATTERNS = (
    r"logged\s+in\s+as\s+[^\s@]+@[^\s]+",
    r"authenticated\s+as\s+[^\s@]+@[^\s]+",
    r"logged\s+in\s+as\s+[\w\.\-]+",
    r"authenticated\s+as\s+[\w\.\-]+",
    r"active\s+account\s*:\s*[^\s]+",
    r"auth\s+status\s*:\s*(?:authenticated|logged_in|active)",
    r"status\s*:\s*(?:authenticated|logged_in|active)",
    r"account\s*:\s*[^\s@]+@[^\s]+",
)


def _cli_auth_status_check(timeout: float = 5.0) -> bool:
    """Secondary identity check: verify positive proof of authentication from
    the resolved AG CLI auth status, bounded by `timeout`.

    Strictly requires positive evidence of an active, authenticated account.
    Blank output, guest session, Chinese/foreign unauthenticated indicators,
    or unknown success text are rejected (fail closed).
    """
    try:
        executable, prefix_args = resolve_ag_cli_executable()
    except AgLaunchError:
        return False
    try:
        result = subprocess.run(
            [executable, *prefix_args, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False

    stdout_raw = result.stdout or ""
    stderr_raw = result.stderr or ""
    combined = f"{stdout_raw}\n{stderr_raw}".strip()
    if not combined:
        return False

    # Check explicit unauthenticated / guest markers
    for unauth_pat in _EXPLICIT_UNAUTHENTICATED_PATTERNS:
        if re.search(unauth_pat, combined, re.IGNORECASE):
            return False

    # 1. Try structured JSON parsing
    for line in combined.splitlines():
        line_str = line.strip()
        if not (line_str.startswith("{") and line_str.endswith("}")):
            continue
        try:
            data = json.loads(line_str)
        except Exception:
            continue
        if isinstance(data, dict):
            status = str(data.get("status", "")).strip().lower()
            if status in ("unauthenticated", "guest", "logged_out", "none", "error"):
                return False
            if data.get("authenticated") is True or data.get("logged_in") is True:
                acct = str(data.get("account") or data.get("email") or data.get("user") or "").strip().lower()
                if acct == "guest":
                    return False
                return True
            if status in ("authenticated", "logged_in", "active"):
                return True
            acct = str(data.get("account") or data.get("email") or "").strip()
            if acct and acct.lower() != "guest":
                return True

    # 2. Plain text positive pattern matching
    for pos_pat in _POSITIVE_AUTH_TEXT_PATTERNS:
        if re.search(pos_pat, combined, re.IGNORECASE):
            return True

    # Unknown output without positive proof -> fail closed
    return False


def verify_auth_identity(timeout: float = 5.0) -> str:
    """Verify that Antigravity execution uses a verified local Google account profile.

    Accepts only a verifiable credential/token file (real parsed token content
    with valid future expiry, not mere presence) or a bounded-timeout official
    CLI auth-status check with positive proof. Directory existence (~/.gemini/config,
    ~/.gemini/antigravity-ide, etc.) is never accepted as proof. Strictly fails
    closed if neither check can positively verify identity. GOOGLE_API_KEY is not
    accepted as evidence of local Google AI Pro account identity.
    """
    gemini_home = resolve_canonical_gemini_home()
    oauth_file = gemini_home / "oauth_credentials.json"

    if _parse_local_credential_token(oauth_file):
        return "local_google_account_profile"

    if _cli_auth_status_check(timeout=timeout):
        return "official_cli_auth_status"

    raise AgLaunchError(
        "unverified_identity",
        "Cannot prove Antigravity local identity: no parseable valid-credential file "
        "(~/.gemini/oauth_credentials.json) with active expiry and official CLI "
        "auth-status check did not verify positive authenticated proof. Directory "
        "existence is not accepted as proof. Fail closed.",
    )


def resolve_ag_official_cli_executable(explicit: str | None = None) -> tuple[str, list[str]]:
    """Locate ONLY a verified standalone `agy` Antigravity CLI executable.

    This is the AG_OFFICIAL_CLI route. It deliberately does NOT accept
    `agentapi`, the bundled `language_server` + `agentapi` combo, or the
    generic `gemini` CLI -- those are the GEMINI_CLI_FALLBACK route (see
    resolve_ag_cli_executable) and must never be reported back as the
    verified official CLI. If no real standalone `agy` binary can be found,
    this fails closed with `route_unavailable` rather than silently
    substituting a different tool or faking readiness.
    """
    if explicit:
        path = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if path:
            return str(Path(path).resolve()), []
        raise AgLaunchError("route_unavailable", f"Explicit Antigravity official CLI executable not found: {explicit}")

    env_bin = os.environ.get("AGY_BIN")
    if env_bin:
        path = shutil.which(env_bin) or (env_bin if Path(env_bin).is_file() else None)
        if path:
            return str(Path(path).resolve()), []

    names = ("agy.exe", "agy.cmd", "agy.bat", "agy") if os.name == "nt" else ("agy",)
    for name in names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve()), []

    raise AgLaunchError(
        "route_unavailable",
        "Verified standalone Antigravity official CLI executable (agy) was not found on this "
        "machine. AG_OFFICIAL_CLI route is unavailable -- refusing to substitute agentapi, the "
        "bundled language server, or the generic gemini CLI. Fail closed.",
    )


def resolve_ag_cli_executable(explicit: str | None = None) -> tuple[str, list[str]]:
    r"""Locate an Antigravity CLI-shaped binary via the GEMINI_CLI_FALLBACK
    route: agentapi, the bundled language_server + agentapi combo, or the
    generic agy/antigravity/gemini CLI. This is deliberately permissive --
    used for the headless fallback runner, never for the strict
    AG_OFFICIAL_CLI route (see resolve_ag_official_cli_executable).

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

    # 2. Known default installation locations under verified canonical ~/.gemini
    gemini_home = resolve_canonical_gemini_home()
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


# exit_code == 0 is not sufficient proof of success: a "result"-shaped event
# or plain accumulated output can still carry an auth/quota failure. These
# markers catch that masked-failure case so it normalizes to FAILED instead
# of a false COMPLETED.
_MASKED_FAILURE_STATUS_VALUES = {"error", "failed", "failure"}
_MASKED_FAILURE_PHRASES = ("quota exceeded", "unauthorized")


def _detect_masked_failure(raw_event: dict[str, Any] | None, text: str = "") -> tuple[str, str] | None:
    """Detect a failure signal masked behind exit_code==0 / a superficially
    success-shaped payload. Returns (error_code, message) if a failure
    signal is found, else None.
    """
    candidate_texts: list[str] = [text] if text else []
    if isinstance(raw_event, dict):
        status = str(raw_event.get("status") or "").strip().lower()
        if status in _MASKED_FAILURE_STATUS_VALUES:
            detail = raw_event.get("message") or raw_event.get("response") or status
            return (str(raw_event.get("code") or f"status_{status}"), str(detail))
        if raw_event.get("error"):
            return (str(raw_event.get("code") or "provider_error"), str(raw_event["error"]))
        if raw_event.get("unauthorized") is True:
            return ("unauthorized", str(raw_event.get("message") or "Unauthorized"))
        candidate_texts.extend(str(v) for v in raw_event.values() if isinstance(v, str))

    lowered = "\n".join(candidate_texts).lower()
    for phrase in _MASKED_FAILURE_PHRASES:
        if phrase in lowered:
            return (
                phrase.replace(" ", "_"),
                f"Detected '{phrase}' in provider output despite exit_code 0 / success-shaped payload",
            )
    return None


def _windows_safe_invocation(args: list[str]) -> list[str]:
    """Wrap `.bat`/`.cmd` targets through `cmd.exe /c` (list-form argv, never
    shell=True) so Windows can actually launch them. CreateProcess cannot
    execute a batch file directly (it is not a valid Win32 application);
    shell=True would instead let the whole command line -- including
    unsanitized `prompt` text -- be re-parsed as a single shell string, which
    is the dangerous pattern this must avoid. Routing only the resolved
    executable through `cmd.exe /c` keeps every argv element (including the
    prompt) as one discrete, `list2cmdline`-quoted token.
    """
    if os.name != "nt" or not args:
        return args
    if Path(args[0]).suffix.lower() not in (".bat", ".cmd"):
        return args
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    return [comspec, "/c", *args]


def _kill_process_tree(pid: int) -> None:
    """Best-effort kill of the full Windows process tree rooted at `pid`, so
    a timeout/cancel doesn't leave orphaned children (e.g. node.exe spawned
    by a .bat/.cmd wrapper) or a locked port behind. Never raises.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        pass


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
        while True:
            try:
                line = stdout.readline()
            except Exception:
                break
            if not line or not isinstance(line, (str, bytes)):
                break
            if isinstance(line, bytes):
                try:
                    line = line.decode("utf-8", errors="replace")
                except Exception:
                    break
            line_str = line.strip()
            if not line_str:
                continue
            try:
                raw_event = json.loads(line_str)
                masked = _detect_masked_failure(raw_event)
                if masked:
                    code, msg = masked
                    event = AgNormalizedEvent(event_type="error", payload={"error": msg, "code": code})
                else:
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
        while True:
            try:
                line = stderr.readline()
            except Exception:
                break
            if not line or not isinstance(line, (str, bytes)):
                break
            if isinstance(line, bytes):
                try:
                    line = line.decode("utf-8", errors="replace")
                except Exception:
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
        if os.name == "nt" and self.process.pid:
            _kill_process_tree(self.process.pid)
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
        if executable_resolver is None:
            # mode="cli" is the strict AG_OFFICIAL_CLI route (standalone agy
            # only); any other mode (e.g. "headless") uses the permissive
            # GEMINI_CLI_FALLBACK resolver. Never share one resolver across
            # both -- that is exactly the route-mislabeling this guards
            # against.
            executable_resolver = (
                resolve_ag_official_cli_executable if default_mode == "cli" else resolve_ag_cli_executable
            )
        self._resolve_executable = executable_resolver
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

        gemini_home = resolve_canonical_gemini_home()
        session_path = str(gemini_home / "antigravity-ide" / "brain" / thread_id / "transcript.jsonl")

        return PreparedLaunch(
            thread_id=thread_id,
            session_path=session_path,
            pid=0,  # Assigned on process spawn in start()
            process_creation_identity=f"{mode}-{identity}",
            prepared_at=now,
            mode=mode,
            route_used=MODE_TO_ROUTE.get(mode, mode),
            actual_runner=self.__class__.__name__,
            _target=None,
            _request=request,
        )

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared._started:
            raise AgLaunchError("already_started", "Prepared Antigravity launch was already started")
        prepared._started = True

        executable, prefix_args = self._get_resolved_executable()
        req = prepared._request

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

        # Enforce sandbox and working directory
        cwd = req.working_directory if Path(req.working_directory).is_dir() else None

        # Windows cannot CreateProcess a .bat/.cmd directly; route it through
        # cmd.exe /c as a list-form argv (never shell=True) so `prompt` stays
        # a single quoted token instead of being re-parsed as a shell string.
        popen_args = _windows_safe_invocation(args)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

        try:
            proc = subprocess.Popen(
                popen_args,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
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
                        # exit_code == 0 alone is not proof of success -- scan
                        # for a failure signal masked in the output.
                        masked = _detect_masked_failure(
                            None, text=f"{final_response}\n{cli_proc.get_stderr_summary()}"
                        )
                        if masked:
                            status = "failed"
                            failure_kind, failure_msg = masked
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
