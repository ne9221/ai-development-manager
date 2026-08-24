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
import os
import re
from pathlib import Path

PRODUCTION_MARKER_FILENAME = ".adm-production-runtime.json"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ProductionPathGuardError(Exception):
    """Raised when an action would use a marked production checkout as a
    developer/write working directory."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeGuardError(ProductionPathGuardError):
    """A production runtime failed its no-bypass provenance gate."""


def _same_path(left, right) -> bool:
    return _resolve(left) == _resolve(right)


def _activated_evidence_path(manager_home) -> Path:
    return Path(manager_home) / "provenance" / "activated_sha.json"


def _is_production_runtime(repository_path, manager_home) -> bool:
    """Production is explicit: a marker, or activation evidence for this checkout.

    A normal developer checkout has neither and therefore keeps its existing
    test/development behaviour.  A historical production HOME with activation
    evidence but no marker is still production and consequently fails closed.
    """
    if is_marked_production_path(repository_path):
        return True
    evidence_path = _activated_evidence_path(manager_home)
    if not evidence_path.exists():
        return False
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return _same_path(evidence.get("repository_path", ""), repository_path)


def require_runtime_guard(repository_path=None, manager_home=None) -> dict:
    """Prove a production runtime before any operational side effect.

    This is the single Python entrypoint gate.  It is deliberately a no-op for
    an unmarked developer checkout; it never captures evidence, activates, or
    repairs a marker.
    """
    repository_path = Path(repository_path or os.getcwd())
    home_value = manager_home or os.environ.get("AI_MANAGER_HOME")
    # Tests and service accounts can legitimately have no resolvable HOME.
    # This fallback is only for an unmarked developer checkout; production
    # wrappers/direct invocations supply AI_MANAGER_HOME explicitly.
    manager_home = Path(home_value) if home_value else repository_path / ".ai-development-manager"
    if not _is_production_runtime(repository_path, manager_home):
        return {"state": "PASS", "production": False}

    document = validate_production_marker(repository_path, manager_home)

    # Local import avoids a provenance -> production_guard import cycle.
    from manager.provenance import ProvenanceError, verify_running
    try:
        contract = verify_running(repository_path, manager_home)
    except ProvenanceError as exc:
        message = str(exc)
        code = "DIRTY_WORKTREE" if "dirty working tree" in message else "EVIDENCE_INVALID" if "valid JSON" in message else "EVIDENCE_MISSING" if "no " in message else "PROVENANCE_MISMATCH"
        raise RuntimeGuardError(code, f"{code}: {message}") from exc
    validate_production_marker(repository_path, manager_home, contract.activated_sha)
    return {"state": "PASS", "production": True, **contract.to_dict()}


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


def validate_production_marker(repository_path, manager_home, activated_sha=None) -> dict:
    """Validate the marker only; this function never writes or repairs it."""
    marker = production_marker_path(repository_path)
    if not marker.exists():
        raise RuntimeGuardError("MARKER_MISSING", f"MARKER_MISSING: {marker} is required for a production runtime")
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeGuardError("EVIDENCE_INVALID", f"EVIDENCE_INVALID: production marker {marker} is malformed") from exc
    if (document.get("protected") is not True or not _SHA_RE.match(document.get("activated_sha", ""))
            or not _same_path(document.get("manager_home", ""), manager_home)):
        raise RuntimeGuardError("EVIDENCE_INVALID", "EVIDENCE_INVALID: production marker identity is invalid")
    if activated_sha is not None and document["activated_sha"] != activated_sha:
        raise RuntimeGuardError("PROVENANCE_MISMATCH", "PROVENANCE_MISMATCH: marker activated_sha differs from activation evidence")
    return document


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
