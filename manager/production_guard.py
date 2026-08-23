"""Production runtime checkout drift guard.

A production Watcher checkout is turned "production" by exactly one
explicit, gated action: `manager.provenance.activate()`. This module gives
that action a mechanical, on-disk, machine-readable side effect -- a marker
file written into the checkout root itself -- so every other code path that
might otherwise resolve a working_directory onto that same checkout (a
developer task's legacy working_directory fallback, a worktree
materializer computing a destination path, a human running the wrong
command in the wrong folder) can check for it and fail closed, instead of
relying on a human remembering not to `git checkout` inside it.

Deliberately dependency-free (stdlib only): `manager.provenance` runs as a
lean Scheduled Task preflight with a minimal Python environment, and this
module is imported from there as well as from manager.execution_runner /
manager.worktree_materializer, so it must never pull in the heavier
manager.tasks / collectors.publish_drive import chain.
"""

from __future__ import annotations

import json
from pathlib import Path

PRODUCTION_MARKER_FILENAME = ".adm-production-runtime.json"


class ProductionPathGuardError(Exception):
    """Raised when an action would use a marked production checkout as a
    developer/write working directory."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _resolve(path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def production_marker_path(repository_path) -> Path:
    return Path(repository_path) / PRODUCTION_MARKER_FILENAME


def mark_production_path(repository_path, activated_sha: str, manager_home) -> None:
    """Stamp repository_path as a protected production runtime checkout.

    Called only from provenance.activate() -- the one explicit, gated
    action that legitimately turns a checkout into "production". Writing
    this marker is idempotent: re-activation just refreshes it in place,
    and it is never removed automatically (a checkout does not silently
    stop being production-marked just because activation was re-run
    elsewhere).
    """
    marker = production_marker_path(repository_path)
    marker.write_text(
        json.dumps(
            {"protected": True, "activated_sha": activated_sha, "manager_home": str(manager_home)},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def is_marked_production_path(path) -> bool:
    """Whether `path` itself, or any ancestor directory of it, carries a
    production-runtime marker.

    Ancestor-checking (not just an exact-path check) means a caller cannot
    escape the guard by resolving a working_directory to a subdirectory
    nested inside a marked production checkout.
    """
    resolved = _resolve(path)
    for candidate in (resolved, *resolved.parents):
        if (candidate / PRODUCTION_MARKER_FILENAME).exists():
            return True
    return False


def assert_not_production_path(path, action: str) -> None:
    """Fail closed if `path` is a marked production runtime checkout.

    `action` names what was about to happen (e.g. "resolve a developer
    task's working_directory", "materialize an isolated worktree") so the
    raised error is a legible blocker, not a bare rejection.
    """
    if is_marked_production_path(path):
        raise ProductionPathGuardError(
            "production_path_protected",
            f"PRODUCTION_PATH_PROTECTED: refusing to {action} at {path!r} -- this checkout is a marked "
            "production runtime path (see .adm-production-runtime.json); use an isolated worktree instead",
        )
