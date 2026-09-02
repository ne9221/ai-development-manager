"""Durable local run state for Antigravity executions (restart-safe readback).

One small JSON file per ADM-assigned AG thread id under
``AI_MANAGER_HOME/runtime/antigravity/runs/<thread_id>.json``. It carries the
identities ADM must keep apart -- ADM execution/session (via ``thread_id``,
the provider_session_id ADM assigned), the AG ``conversation_id`` the language
server returned, and the language-server process identity -- plus the last
observed event/cursor, transcript path and terminal state, so a restarted ADM
(or manager.ag_recovery) can find and reconcile a run it did not watch to the
end. Scratch/session state only (AI-DEVELOPMENT-RULES rule 1); never an SSOT.
No token of any kind is ever written here.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted", "cancelled"})
_SAFE_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_state_dir(manager_home: str | os.PathLike | None = None) -> Path:
    home = Path(manager_home or os.environ.get("AI_MANAGER_HOME") or (Path.home() / ".ai-development-manager"))
    return home / "runtime" / "antigravity" / "runs"


def _path(thread_id: str, manager_home=None) -> Path:
    if not isinstance(thread_id, str) or not thread_id or any(ch not in _SAFE_ID for ch in thread_id) or thread_id.startswith("."):
        raise ValueError("invalid antigravity thread id")
    return run_state_dir(manager_home) / f"{thread_id}.json"


def write_run_state(state: dict[str, Any], manager_home=None) -> Path:
    """Atomically persist ``state`` (must carry ``thread_id``); returns the file path."""
    path = _path(state["thread_id"], manager_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = dict(state, updated_at=utc_now())
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_run_state(thread_id: str, manager_home=None) -> dict[str, Any] | None:
    path = _path(thread_id, manager_home)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def update_run_state(thread_id: str, manager_home=None, **fields: Any) -> dict[str, Any]:
    current = read_run_state(thread_id, manager_home) or {"thread_id": thread_id}
    current.update(fields)
    write_run_state(current, manager_home)
    return current


def list_run_states(manager_home=None, *, include_terminal: bool = True) -> list[dict[str, Any]]:
    directory = run_state_dir(manager_home)
    if not directory.is_dir():
        return []
    states = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict) or not document.get("thread_id"):
            continue
        if not include_terminal and document.get("status") in TERMINAL_STATUSES:
            continue
        states.append(document)
    return states
