"""Claude Code CLI auto-launch adapter.

Exposes the same two-phase `prepare(LaunchRequest) -> PreparedLaunch` shape
`manager.codex_launcher.CodexLauncher` gives the watcher: spawn the provider
process and reach an idle-ready state, without sending a task turn yet. Unlike
Codex's app-server JSON-RPC protocol, Claude Code's non-interactive CLI (`-p`)
is a single subprocess speaking stream-json over stdio; ADM assigns its own
provider-native session id up front via `--session-id` instead of learning one
from the provider after the fact, so `provider_session_id` is deterministic
before the process even exists.

`LaunchRequest` and the OS-level process-identity primitives
(`process_creation_identity`, `process_pid`, `utc_now`) are imported from
`codex_launcher` unchanged rather than duplicated -- they are already
provider-neutral and already cross-imported from that module by
`session_center_supervisor.py` and `command_watcher.py`. `PreparedLaunch` here
is a new, Claude-specific dataclass rather than a reuse of Codex's: Codex's
version has no provider/cwd/branch/model fields and carries a Codex-only
app-server client object, and widening the shared dataclass would risk Codex
regressions for a shape only Claude needs.

`start(prepared, prompt)`/`wait(running)` complete the execution half: start()
writes one stream-json user-message envelope to stdin and closes it (ADM
dispatches one bounded task per launch, not an open-ended chat, so a single
input turn followed by EOF is the right shape); wait() blocks for process
exit and extracts the final `type: "result"` event from the stdout file sink.
The stream-json envelope and result-event shapes below follow the Claude
Agent SDK / Claude Code stream-json conventions as best understood from
`claude --help` and general knowledge of the protocol -- they have **not**
been verified against a real `claude -p --input-format stream-json
--output-format stream-json` invocation (out of scope this round: no real
launch). This needs empirical corroboration in a later real-smoke phase
before anything relies on it end-to-end.

`RunningLaunch`/`LaunchOutcome` are imported from `codex_launcher` unchanged
(reused, not duplicated): both are already fully provider-neutral dataclasses
with no Codex-specific fields, unlike `PreparedLaunch`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from manager.codex_launcher import (
    LaunchOutcome, LaunchRequest, RunningLaunch,
    process_creation_identity, process_pid, utc_now,
)


MAX_ERROR_CHARS = 1000
MAX_STDOUT_READ_BYTES = 2_000_000  # bounded read of the file sink; audit evidence stays on disk regardless

# The only sandbox/approval_policy combination ADM's task policy layer
# currently emits for read_only tasks (execution_runner.py's
# sandbox="read-only"/approval_policy="never" when task["read_only"] is True).
# ClaudeLauncher v1 understands only this exact profile; anything else --
# including today's production_write shape (sandbox=None, approval_policy=None)
# -- fails closed instead of guessing a permissive Claude permission mode.
_READ_ONLY_SANDBOX = "read-only"
_READ_ONLY_APPROVAL = "never"
_READ_ONLY_PERMISSION_MODE = "plan"
_READ_ONLY_ALLOWED_TOOLS = ("Read", "Grep", "Glob", "WebFetch")


class ClaudeLaunchError(RuntimeError):
    """A bounded, provider-protocol launch failure (mirrors CodexLaunchError's shape)."""

    def __init__(self, classification: str, detail: str):
        self.classification = classification
        self.detail = str(detail)[:MAX_ERROR_CHARS]
        super().__init__(f"{classification}: {self.detail}")


def resolve_claude_executable(explicit: str | None = None) -> str:
    candidate = explicit or os.environ.get("CLAUDE_BIN")
    if candidate:
        path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
    else:
        names = ("claude.exe", "claude.cmd", "claude") if os.name == "nt" else ("claude",)
        path = next((found for name in names if (found := shutil.which(name))), None)
    if not path:
        raise ClaudeLaunchError("executable_not_found", "Claude Code CLI was not found")
    return str(Path(path).resolve())


def _permission_profile(request: LaunchRequest) -> tuple[str, tuple[str, ...]]:
    if request.sandbox == _READ_ONLY_SANDBOX and request.approval_policy == _READ_ONLY_APPROVAL:
        return _READ_ONLY_PERMISSION_MODE, _READ_ONLY_ALLOWED_TOOLS
    raise ClaudeLaunchError(
        "unsupported_policy",
        "ClaudeLauncher v1 only supports the read-only safe profile "
        f"(sandbox={_READ_ONLY_SANDBOX!r}, approval_policy={_READ_ONLY_APPROVAL!r}); "
        f"got sandbox={request.sandbox!r}, approval_policy={request.approval_policy!r}",
    )


AUTH_STATUS_TIMEOUT_SECONDS = 10.0


def check_claude_auth_ready(executable: str, env: dict | None, *,
                            timeout: float = AUTH_STATUS_TIMEOUT_SECONDS,
                            run: Callable[..., Any] = subprocess.run) -> bool:
    """P0.2 authentication readiness preflight: `claude auth status --json`
    against the exact executable/env (CLAUDE_CONFIG_DIR override or ambient
    default) the real launch would use, run as its own short-lived,
    non-interactive subprocess -- never the credential-carrying account
    itself, never logged in/out.

    Returns True (AUTH_READY) or False (AUTH_UNAVAILABLE -- confirmed not
    logged in / expired / no usable credential) only when the check produced
    an unambiguous `loggedIn` boolean. Anything else (unexpected exit code,
    unparseable/malformed output, timeout, OS error starting the check)
    raises ClaudeLaunchError("authentication_check_failed", ...) instead of
    guessing readiness (AUTH_UNKNOWN) -- kept as a distinct classification
    from "authentication_unavailable" so a confirmed logged-out account is
    never confused with a broken/uncertain check, and from every other
    ClaudeLaunchError classification so a real provider crash is never
    mislabeled as an auth problem.

    Only the `loggedIn` boolean is ever read out of the check's JSON output;
    the rest (email, orgId, authMethod, ...) and raw stdout/stderr are
    discarded and never appear in a raised error's detail.
    """
    try:
        completed = run([executable, "auth", "status", "--json"], capture_output=True,
                        text=True, timeout=timeout, env=env, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise ClaudeLaunchError(
            "authentication_check_failed", "Claude authentication status check timed out"
        ) from exc
    except OSError as exc:
        raise ClaudeLaunchError(
            "authentication_check_failed", f"could not run Claude authentication status check: {exc}"
        ) from exc

    if completed.returncode not in (0, 1):
        raise ClaudeLaunchError(
            "authentication_check_failed",
            f"Claude authentication status check exited with unexpected code {completed.returncode}",
        )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ClaudeLaunchError(
            "authentication_check_failed", "Claude authentication status check returned unparseable output"
        ) from exc
    logged_in = payload.get("loggedIn") if isinstance(payload, dict) else None
    if not isinstance(logged_in, bool):
        raise ClaudeLaunchError(
            "authentication_check_failed", "Claude authentication status check returned an unexpected shape"
        )
    # The only two self-consistent exit-code/body combinations: a successful
    # check (rc=0) reporting loggedIn=True, or a clean not-logged-in check
    # (rc=1) reporting loggedIn=False. Exit code and body are two independent
    # signals from the same subprocess; trusting either one alone lets a
    # skewed/inconsistent result (e.g. rc=1 with loggedIn=True) slip through
    # as READY. Any other combination fails closed instead of picking a side.
    if (completed.returncode, logged_in) not in ((0, True), (1, False)):
        raise ClaudeLaunchError(
            "authentication_check_failed",
            "Claude authentication status check returned an inconsistent exit code/body combination",
        )
    return logged_in


def _new_session_id() -> str:
    session_id = str(uuid.uuid4())
    if str(uuid.UUID(session_id)) != session_id:
        raise ClaudeLaunchError("protocol_error", "generated session id failed UUID round-trip")
    return session_id


def _build_argv(executable: str, session_id: str, permission_mode: str,
                 allowed_tools: tuple[str, ...], model: str | None) -> list[str]:
    argv = [
        executable, "-p",
        "--session-id", session_id,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode,
        "--allowed-tools", ",".join(allowed_tools),
    ]
    if model:
        argv += ["--model", model]
    return argv


def claude_session_transcript_path(cwd: str, session_id: str) -> str:
    """Deterministic advisory path to Claude's own session transcript, mirroring
    Codex's PreparedLaunch.session_path ("adapter-validated advisory evidence;
    identity is the provider session id, not this path"). The sanitization
    rule below (replace path separators, drive-letter colon, and dots with
    "-") is inferred from a single real, empirically observed example on this
    host (C:\\Users\\EE\\.ai-development-manager -> C--Users-EE--ai-development-manager)
    -- it has not been verified against Claude Code's own source/spec, and
    should be corroborated with more real cwd shapes before anything treats
    this path as more than advisory.
    """
    sanitized = cwd
    for char in (":", "\\", "/", "."):
        sanitized = sanitized.replace(char, "-")
    return str(Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl")


@dataclass
class PreparedLaunch:
    provider: str
    provider_session_id: str
    pid: int
    process_creation_identity: str
    cwd: str
    branch: str | None
    prepared_at: str
    model: str | None
    mode: str
    argv: list  # safe to log/persist: no secrets appear in these flags
    stdout_path: str
    stderr_path: str
    session_path: str | None
    _process: Any = field(repr=False)
    _request: LaunchRequest = field(repr=False)
    _closed: bool = field(default=False, repr=False)
    _started: bool = field(default=False, repr=False)
    account_id: str | None = None
    config_dir: str | None = None


def _encode_stream_json_input(prompt: str) -> bytes:
    """Encode one user turn as a newline-delimited stream-json input line.

    json.dumps escapes embedded quotes/newlines/control characters within the
    JSON string value, so a multiline or quote-containing prompt still lands
    on exactly one ndjson line; ensure_ascii=False plus explicit utf-8
    encoding keeps non-ASCII text intact rather than \\u-escaped.
    """
    envelope = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
    return (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")


def _read_output_text(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STDOUT_READ_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _extract_stream_json_result(raw_text: str) -> dict | None:
    """Return the last well-formed `type: "result"` ndjson event, or None if
    none is present -- malformed lines are skipped rather than raising, but
    the absence of any result event is reported to the caller as None so it
    can fail closed instead of guessing an outcome."""
    result = None
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


class ClaudeLauncher:
    """Claude Code CLI adapter: same prepare(LaunchRequest) -> PreparedLaunch
    shape CodexLauncher exposes to the watcher, spawning `claude -p` instead of
    Codex's app-server protocol."""

    def __init__(self, executable: str | None = None, popen: Callable[..., Any] = subprocess.Popen,
                 log_dir: str | None = None, auth_check: Callable[[str, dict | None], bool] = check_claude_auth_ready):
        self.executable = executable
        self._popen = popen
        self._log_dir = log_dir
        self._auth_check = auth_check

    @staticmethod
    def _kill_quietly(process: Any) -> None:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass

    def prepare(self, request: LaunchRequest, branch: str | None = None,
                account_id: str | None = None, config_dir: str | None = None) -> PreparedLaunch:
        """`account_id`/`config_dir` are additive, optional, and default to
        None (today's single-account behavior: env=None, i.e. the child
        inherits this process's environment unchanged -- identical to before
        this parameter existed). When `config_dir` is given, the child gets a
        copy of the parent environment with only CLAUDE_CONFIG_DIR overridden,
        so a second Claude account's config directory never leaks into (or
        gets clobbered by) the first. `account_id` is carried on the returned
        PreparedLaunch purely as attribution evidence for the caller to persist
        onto the session/execution record -- ClaudeLauncher itself does not
        interpret or validate it against any account registry.

        Before spawning, `self._auth_check` (default `check_claude_auth_ready`)
        gates on whether this exact config_dir/env is actually authenticated,
        raising ClaudeLaunchError("authentication_unavailable", ...) closed
        rather than letting a registered-but-logged-out account reach a
        provider subprocess. See check_claude_auth_ready's docstring for the
        AUTH_READY/AUTH_UNAVAILABLE/AUTH_UNKNOWN contract this implements.
        """
        cwd = Path(request.working_directory)
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ClaudeLaunchError("invalid_request", "working_directory must be an existing absolute directory")
        if config_dir is not None and not str(config_dir).strip():
            raise ClaudeLaunchError("invalid_request", "config_dir must be a non-empty string when provided")

        permission_mode, allowed_tools = _permission_profile(request)
        executable = resolve_claude_executable(self.executable)

        env = None
        if config_dir is not None:
            env = dict(os.environ)
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)

        # P0.2: registered + enabled + config_dir-set is not the same thing as
        # actually logged in -- a real `claude login` may never have happened,
        # or may have expired/been revoked out of band. Check readiness against
        # the exact executable/env this launch is about to use, before any
        # session id, log file, or task subprocess exists, so a not-ready
        # account fails closed with zero side effects rather than a wasted or
        # ambiguous spawn. This is independent of and never influenced by
        # quota confidence/staleness (no quota input reaches this check at
        # all), and it never falls back to a different account -- prepare()
        # is always called for exactly one already-resolved account_id.
        if not self._auth_check(executable, env):
            raise ClaudeLaunchError(
                "authentication_unavailable",
                "Claude account is not authenticated for this profile (not logged in, login expired, "
                "or credential unavailable)" + (f"; account_id={account_id!r}" if account_id else ""),
            )

        session_id = _new_session_id()
        argv = _build_argv(executable, session_id, permission_mode, allowed_tools, request.model)

        log_dir = Path(self._log_dir) if self._log_dir else Path(tempfile.gettempdir())
        stdout_path = log_dir / f"claude-{session_id}.stdout.log"
        stderr_path = log_dir / f"claude-{session_id}.stderr.log"
        try:
            stdout_handle = open(stdout_path, "wb")
            stderr_handle = open(stderr_path, "wb")
        except OSError as exc:
            raise ClaudeLaunchError("spawn_failed", f"failed to open output log files: {exc}") from exc

        # Break the child out of whatever Windows Job Object owns this process
        # (e.g. Task Scheduler's per-instance job) so it keeps running past the
        # launching --once watcher cycle's own exit -- the watcher's design
        # relies on a later poll cycle reconciling an independently-surviving
        # provider process, not on this call blocking for the full run. Without
        # this flag a Task-Scheduler-owned job can kill the child the moment its
        # spawning process exits, before Claude ever produces output.
        #
        # CREATE_BREAKAWAY_FROM_JOB itself requires the *current* job to have
        # been created with JOB_OBJECT_LIMIT_BREAKAWAY_OK -- Task Scheduler's
        # own job does not grant that, so CreateProcess fails closed with
        # ERROR_ACCESS_DENIED (OSError WinError 5) the moment this flag is
        # requested from inside it (confirmed live: works from an ordinary
        # shell, spawn_failed with zero PID from inside the Scheduled Task).
        # Retry once without the flag rather than let a job that disallows
        # breakaway turn "try to protect the child" into "never launch it".
        def _spawn(creationflags):
            kwargs = {"creationflags": creationflags} if creationflags else {}
            return self._popen(
                argv, cwd=str(cwd), stdin=subprocess.PIPE,
                stdout=stdout_handle, stderr=stderr_handle, shell=False, env=env,
                **kwargs,
            )

        try:
            try:
                if os.name == "nt":
                    try:
                        process = _spawn(subprocess.CREATE_BREAKAWAY_FROM_JOB)
                    except OSError:
                        process = _spawn(None)
                else:
                    process = _spawn(None)
            except OSError as exc:
                raise ClaudeLaunchError("spawn_failed", f"failed to start Claude CLI: {exc}") from exc
        finally:
            # The child holds its own duplicated handles; our copies must be
            # closed here regardless of spawn outcome, or they leak.
            stdout_handle.close()
            stderr_handle.close()

        try:
            pid = process_pid(process)
            if process.poll() is not None:
                raise ClaudeLaunchError(
                    "spawn_failed", f"Claude CLI exited immediately with code {process.returncode}"
                )
            identity = process_creation_identity(pid)
            if identity is None:
                raise ClaudeLaunchError(
                    "protocol_error", "could not verify Claude process creation identity"
                )
        except ClaudeLaunchError:
            self._kill_quietly(process)
            raise

        return PreparedLaunch(
            provider="claude",
            provider_session_id=session_id,
            pid=pid,
            process_creation_identity=identity,
            cwd=str(cwd),
            branch=branch,
            account_id=account_id,
            config_dir=config_dir,
            prepared_at=utc_now(),
            model=request.model,
            mode=permission_mode,
            argv=list(argv),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            session_path=claude_session_transcript_path(str(cwd), session_id),
            _process=process,
            _request=request,
        )

    def start(self, prepared: PreparedLaunch, prompt: str) -> RunningLaunch:
        if prepared._closed:
            raise ClaudeLaunchError("invalid_state", "prepared launch has already been closed")
        if prepared._started:
            raise ClaudeLaunchError("invalid_state", "prepared launch has already started")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ClaudeLaunchError("invalid_request", "prompt must be non-empty")
        process = prepared._process
        if process.poll() is not None:
            raise ClaudeLaunchError(
                "protocol_error", f"Claude process exited before start (code {process.returncode})"
            )
        payload = _encode_stream_json_input(prompt)
        try:
            process.stdin.write(payload)
            process.stdin.flush()
            # One bounded task per launch: a single input turn followed by
            # EOF, not an open-ended chat -- Claude processes it and exits.
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            self._kill_quietly(process)
            raise ClaudeLaunchError("spawn_failed", f"failed to write prompt to Claude stdin: {exc}") from exc
        prepared._started = True
        return RunningLaunch(prepared, prepared.provider_session_id, utc_now())

    def wait(self, running: RunningLaunch) -> LaunchOutcome:
        prepared = running.prepared
        process = prepared._process
        turn_timeout = prepared._request.turn_timeout_seconds
        try:
            exit_code = process.wait(timeout=turn_timeout)
        except subprocess.TimeoutExpired as exc:
            self._kill_quietly(process)
            raise ClaudeLaunchError("timeout", "turn completion timed out") from exc

        completed_at = utc_now()
        result = _extract_stream_json_result(_read_output_text(prepared.stdout_path))

        if exit_code != 0:
            detail = (result or {}).get("error") or f"Claude exited with code {exit_code}"
            return LaunchOutcome(
                "failed", prepared.provider_session_id, running.turn_id, completed_at,
                "provider_error", str(detail)[:MAX_ERROR_CHARS],
            )
        if result is None:
            return LaunchOutcome(
                "failed", prepared.provider_session_id, running.turn_id, completed_at,
                "malformed_output", "no result event found in Claude stream-json output",
            )

        # Fail closed rather than trust output-reported identity: the
        # provider-native session id is authority-assigned by ADM at prepare()
        # time, never learned or re-derived from provider output afterward.
        reported_session_id = result.get("session_id")
        if reported_session_id is not None and reported_session_id != prepared.provider_session_id:
            return LaunchOutcome(
                "failed", prepared.provider_session_id, running.turn_id, completed_at,
                "session_id_mismatch",
                f"result reported session_id={reported_session_id!r}, expected {prepared.provider_session_id!r}",
            )

        if result.get("is_error"):
            detail = result.get("result") or result.get("error") or "Claude reported is_error=true"
            return LaunchOutcome(
                "failed", prepared.provider_session_id, running.turn_id, completed_at,
                "turn_failed", str(detail)[:MAX_ERROR_CHARS],
            )
        return LaunchOutcome("completed", prepared.provider_session_id, running.turn_id, completed_at)

    def close(self, handle: PreparedLaunch | RunningLaunch) -> None:
        prepared = handle.prepared if isinstance(handle, RunningLaunch) else handle
        if prepared._closed:
            return
        try:
            if prepared._process.poll() is None:
                self._kill_quietly(prepared._process)
        finally:
            prepared._closed = True
