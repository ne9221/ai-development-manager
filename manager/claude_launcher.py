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

`start()`/turn execution (writing the actual task prompt over the stream-json
stdin channel and reading streamed responses) is deliberately out of scope
here -- this module only covers the `prepare()` contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from manager.codex_launcher import LaunchRequest, process_creation_identity, process_pid, utc_now


MAX_ERROR_CHARS = 1000

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
        "--permission-mode", permission_mode,
        "--allowed-tools", ",".join(allowed_tools),
    ]
    if model:
        argv += ["--model", model]
    return argv


@dataclass
class PreparedLaunch:
    provider: str
    provider_session_id: str
    pid: int
    process_creation_identity: str
    cwd: str
    branch: str | None
    started_at: str
    model: str | None
    mode: str
    argv: list  # safe to log/persist: no secrets appear in these flags
    stdout_path: str
    stderr_path: str
    _process: Any = field(repr=False)
    _request: LaunchRequest = field(repr=False)
    _closed: bool = field(default=False, repr=False)


class ClaudeLauncher:
    """Claude Code CLI adapter: same prepare(LaunchRequest) -> PreparedLaunch
    shape CodexLauncher exposes to the watcher, spawning `claude -p` instead of
    Codex's app-server protocol."""

    def __init__(self, executable: str | None = None, popen: Callable[..., Any] = subprocess.Popen,
                 log_dir: str | None = None):
        self.executable = executable
        self._popen = popen
        self._log_dir = log_dir

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

    def prepare(self, request: LaunchRequest, branch: str | None = None) -> PreparedLaunch:
        cwd = Path(request.working_directory)
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ClaudeLaunchError("invalid_request", "working_directory must be an existing absolute directory")

        permission_mode, allowed_tools = _permission_profile(request)
        executable = resolve_claude_executable(self.executable)
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

        try:
            try:
                process = self._popen(
                    argv, cwd=str(cwd), stdin=subprocess.PIPE,
                    stdout=stdout_handle, stderr=stderr_handle, shell=False,
                )
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
            started_at=utc_now(),
            model=request.model,
            mode=permission_mode,
            argv=list(argv),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            _process=process,
            _request=request,
        )

    def close(self, prepared: PreparedLaunch) -> None:
        if prepared._closed:
            return
        try:
            if prepared._process.poll() is None:
                self._kill_quietly(prepared._process)
        finally:
            prepared._closed = True
