"""Local, per-machine mutual exclusion for a Claude CLI ``CLAUDE_CONFIG_DIR``.

Real incident this defends against: two independently-launched ADM Claude
executions (no relation to each other in Task/Execution/Session SSOT) shared
Account A's default config directory (``CLAUDE_CONFIG_DIR`` unset ->
``~/.claude``) at the OS-process level. ~16 concurrent `claude` CLI
subprocesses on one host repeatedly read-modified-wrote the same
``.claude.json`` app-state file with no coordination, and it was observed
truncated from ~40KB to 389 bytes. This module makes sure *ADM itself* never
launches two child Claude processes against the same real, on-disk config
directory at once -- it says nothing about, and cannot fix, races caused by
Claude Code processes ADM did not launch.

Scope and non-goals:
- This is a **local machine** resource. A config directory is never shared
  across hosts, so this is deliberately not a GCS/cloud registry like
  ``worktree_locks.py``/``task_claims.py`` (those protect a cross-machine
  business resource -- a GitHub repository branch, a Task's active
  execution -- and are keyed by project/task/execution identity, not by a
  local filesystem path). Adding a network round-trip and cloud credential
  dependency to guard a resource that was never shared off-host would be
  needless complexity, not correctness.
- The lock resource is the **canonicalized config directory**, not
  ``account_id``. Two different ``account_id``s that (misconfiguration or
  not) resolve to the same real directory on disk must contend for the same
  lock -- the OS-level hazard this defends against does not care what ADM
  called the account.
- Fail-closed, no queueing: a second concurrent attempt on an already-held
  config directory raises :class:`ConfigLockBusyError` immediately. It never
  silently falls back to a different account/config_dir, and it never blocks
  waiting for the first execution to finish.

Building blocks reused unchanged (not duplicated):
- ``manager.refresh_status.runtime_lock`` -- the same short-held,
  non-blocking, cross-process OS file lock ``session_center_supervisor.py``
  already uses to make its own read-decide-write cycle atomic across
  processes. Held only for the brief read-modify-write of this module's own
  state file, never for the lifetime of a Claude launch.
- ``manager.refresh_status.write_atomic`` -- temp-file + ``os.replace()``
  atomic state write, so a reader never observes a partial file.
- ``manager.codex_launcher.process_creation_identity`` /
  ``process_identity_state`` -- the same PID-reuse-safe liveness check
  Recovery and the Session Center supervisor already use, so a dead owner is
  never confused with a live one and a reused PID is never mistaken for its
  old occupant.

Ownership identity: the lock is acquired *before* the Claude child process is
spawned (``execution_runner.run_execution()`` acquires it immediately before
``launcher.prepare()``), so there is no child PID yet to record. The owner
recorded is this *calling* ADM process's own (pid, creation_identity) --
that process runs prepare()/start()/wait()/close() synchronously and stays
alive for the whole launch, so its own liveness is exactly the right proxy
for "is this config directory still in use by an ADM-launched execution".
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from manager.codex_launcher import process_creation_identity, process_identity_state
from manager.refresh_status import RefreshError, runtime_lock, write_atomic
from manager.tasks import TaskError


LOCK_SCHEMA_VERSION = "0.1.0"
DEFAULT_STATE_FILENAME = "claude_config_locks.json"
DEFAULT_LOCK_FILENAME = "claude_config_locks.lock"

# One process-local OS file lock (runtime_lock) briefly guards the whole
# state table (every config directory's entry), not just one directory's
# entry -- so two acquire() calls for *different* config directories can
# still transiently contend for that file lock, even though the resources
# they actually want are unrelated. That transient contention is retried
# here (bounded, short) rather than surfaced as ConfigLockBusyError, so an
# unrelated config directory's launch never fails closed just because it
# happened to call acquire()/release() in the same instant as another one.
# The retry ceiling (~0.4s total) is far below any real Claude launch, so it
# cannot mask a genuinely held config-directory lock -- that path always
# raises ConfigLockBusyError deliberately, never via this retry loop.
_TABLE_LOCK_ATTEMPTS = 40
_TABLE_LOCK_RETRY_SECONDS = 0.01

# runtime_lock() is a *cross-process* OS file lock; every existing caller in
# this codebase (session_center_supervisor.py, refresh_status.py) is one
# short-lived process per invocation, so that has always been sufficient.
# ADM does not currently run multiple threads concurrently through this
# module, but Windows byte-range file locks (msvcrt.locking/LockFile) are
# scoped per-process, not per-thread or per-handle: two threads in the same
# process can each successfully lock the same file region at once without
# contending. Without this thread lock, two threads of one process racing
# acquire_claude_config_lock() for the same config directory could both pass
# straight through runtime_lock() and both win -- exactly the bug this
# module exists to prevent. This process-local lock closes that gap in
# addition to (not instead of) runtime_lock's cross-process protection.
_THREAD_LOCK = threading.Lock()

# Liveness states from process_identity_state() that prove the previous
# owner is provably gone -- safe to reclaim. "unknown" is deliberately
# excluded: it means existence/identity could not be proven either way, and
# guessing "safe" for it would reintroduce exactly the race this module
# exists to prevent.
_RECLAIMABLE_STATES = ("stopped", "replaced")


class ConfigLockBusyError(TaskError):
    """A Claude config directory is already in use, or its previous owner's
    liveness could not be positively disproven. Callers must fail closed on
    this -- never queue, never silently pick a different account/config_dir.
    """

    classification = "CLAUDE_CONFIG_BUSY"


def _home() -> Path:
    return Path(os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager"))


def default_state_path() -> Path:
    return _home() / DEFAULT_STATE_FILENAME


def default_lock_path() -> Path:
    return _home() / DEFAULT_LOCK_FILENAME


def canonical_config_dir(config_dir: str | None) -> str:
    """Resolve ``config_dir`` (or ``None``) to one canonical, comparable
    string identifying the real on-disk directory.

    ``None`` means "ClaudeLauncher.prepare() will not override
    CLAUDE_CONFIG_DIR" -- i.e. Claude itself will use its own default
    resolution (the ``CLAUDE_CONFIG_DIR`` env var if set, else ``~/.claude``).
    That default is resolved here too, rather than treated as "no lock
    needed": the original incident used exactly this unconfigured default
    path, and two concurrent single-account launches with config_dir=None
    both write that same real directory.

    Windows filesystem paths are case-insensitive, so the result is
    lower-cased on ``os.name == "nt"``. ``Path.resolve(strict=False)``
    absolutizes, normalizes ``.``/``..`` and separators, and resolves any
    existing symlink/junction components even when the final path segment
    does not exist yet (e.g. before a first ``claude login``).
    """
    raw = config_dir if isinstance(config_dir, str) and config_dir.strip() else None
    raw = raw or os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = Path(os.path.normpath(str(path)))
    text = str(resolved)
    if os.name == "nt":
        text = text.lower()
    return text


def config_lock_id(canonical_dir: str) -> str:
    """Collision-free, non-reversible key. Hashed (mirroring
    ``worktree_locks.repository_lock_id``'s pattern) so the raw absolute
    path -- which may embed a real username/home directory -- never has to
    be used as a dict/log key; it is only ever carried inside this module's
    own local, unpublished state file."""
    return "claude-config-" + hashlib.sha256(canonical_dir.encode("utf-8")).hexdigest()


def _owner_identity(pid: int | None, creation_identity: str | None) -> tuple[int, str]:
    pid = os.getpid() if pid is None else pid
    identity = creation_identity if creation_identity is not None else process_creation_identity(pid)
    if not isinstance(identity, str) or not identity:
        raise TaskError("could not verify this process's own creation identity; refusing to acquire a config lock")
    return pid, identity


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _read_state(state_path: Path) -> dict:
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": LOCK_SCHEMA_VERSION, "locks": {}}
    except (OSError, ValueError) as exc:
        raise TaskError(f"Claude config lock state is unreadable or corrupt: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != LOCK_SCHEMA_VERSION or not isinstance(raw.get("locks"), dict):
        raise TaskError("Claude config lock state is malformed")
    return raw


def _under_table_lock(lock_path, body):
    """Run ``body()`` once with both the process-local thread lock and the
    cross-process state-table file lock held, retrying the file lock a
    bounded number of times only on transient contention for the file lock
    itself (see the module-level note on ``_TABLE_LOCK_ATTEMPTS``). Every
    other error from ``body()`` propagates immediately, unretried.

    The thread lock is held for the file lock's retry loop too, not just
    ``body()`` -- so two threads never even race each other for the file
    lock (see ``_THREAD_LOCK``'s module-level note)."""
    with _THREAD_LOCK:
        last_exc = None
        for _ in range(_TABLE_LOCK_ATTEMPTS):
            try:
                with runtime_lock(lock_path):
                    return body()
            except RefreshError as exc:
                last_exc = exc
                time.sleep(_TABLE_LOCK_RETRY_SECONDS)
        raise last_exc


def acquire_claude_config_lock(config_dir, *, account_id=None, project_id=None, task_id=None,
                                execution_id=None, pid=None, creation_identity=None,
                                state_path=None, lock_path=None) -> dict:
    """Acquire exclusive local-machine ownership of one canonicalized Claude
    config directory, or raise :class:`ConfigLockBusyError`.

    Idempotent for the exact same live owning execution: a second acquire
    call for the same config directory succeeds and returns the existing
    record unchanged only when (pid, creation_identity, execution_id) all
    match the existing record -- this covers a retry of the same execution
    after a partial failure whose release did not run, without weakening
    exclusion against a different execution in the same process (e.g. two
    executions racing inside one long-lived ADM process).
    """
    canonical = canonical_config_dir(config_dir)
    lock_id = config_lock_id(canonical)
    owner_pid, owner_identity = _owner_identity(pid, creation_identity)
    state_path = Path(state_path) if state_path else default_state_path()
    lock_path = Path(lock_path) if lock_path else default_lock_path()

    def body():
        state = _read_state(state_path)
        existing = state["locks"].get(lock_id)
        if existing is not None:
            status = process_identity_state(existing["pid"], existing["creation_identity"])
            if status == "live":
                if (existing["pid"] == owner_pid and existing["creation_identity"] == owner_identity
                        and existing.get("execution_id") == execution_id):
                    return dict(existing)
                raise ConfigLockBusyError(
                    f"Claude config directory is already in use by execution "
                    f"{existing.get('execution_id')!r} (lock_id={lock_id})"
                )
            if status not in _RECLAIMABLE_STATES:
                raise ConfigLockBusyError(
                    f"Claude config directory's previous owner could not be verified as "
                    f"stopped (lock_id={lock_id}); refusing to guess"
                )
            # status in ("stopped", "replaced"): previous owner is provably gone. Reclaim below.
        record = {
            "lock_id": lock_id, "canonical_config_dir": canonical, "account_id": account_id,
            "project_id": project_id, "task_id": task_id, "execution_id": execution_id,
            "pid": owner_pid, "creation_identity": owner_identity, "acquired_at": _now_iso(),
        }
        state["locks"][lock_id] = record
        write_atomic(state_path, state)
        return dict(record)

    try:
        return _under_table_lock(lock_path, body)
    except RefreshError as exc:
        raise ConfigLockBusyError(f"Claude config lock table is contended (lock_id={lock_id})") from exc


def release_claude_config_lock(record, *, state_path=None, lock_path=None) -> dict:
    """ABA-safe, best-effort release: only removes the entry if it still
    identifies the exact owner (lock_id + pid + creation_identity) that
    acquired it. A record already gone, or now owned by a different owner
    (a later acquire reclaimed it as stale after this one), is reported, not
    raised -- release must never mask a caller's primary exception/outcome
    on top of it."""
    if not record:
        return {"released": False, "reason": "no_record"}
    state_path = Path(state_path) if state_path else default_state_path()
    lock_path = Path(lock_path) if lock_path else default_lock_path()
    lock_id = record.get("lock_id")

    def body():
        state = _read_state(state_path)
        current = state["locks"].get(lock_id)
        if current is None:
            return {"released": False, "reason": "not_held"}
        if current.get("pid") != record.get("pid") or current.get("creation_identity") != record.get("creation_identity"):
            return {"released": False, "reason": "owned_by_another_owner"}
        del state["locks"][lock_id]
        write_atomic(state_path, state)
        return {"released": True}

    try:
        return _under_table_lock(lock_path, body)
    except RefreshError:
        return {"released": False, "reason": "lock_table_contended"}
    except TaskError:
        return {"released": False, "reason": "state_unreadable"}
