#!/usr/bin/env python3
"""Runtime enforcement that a repo-write execution's actual git changes stay
within its Task's admitted allowed_paths (Global Hands-off Execution Layer,
Slice D).

Slice A (cloud.dispatch_ingress / manager.trusted_ingress) validates and
bounds allowed_paths at admission time; Slice C (manager.worktree_materializer)
guarantees a v2-repo-write Task's provider only ever runs inside its own
isolated worktree. Until this slice, nothing checked that the provider's
*actual* file changes stayed within that admitted scope once it started
running -- a provider could edit anything inside the worktree and the
execution would still be reported "completed". This module closes that gap:
it inspects real `git` state in the isolated worktree (never what the
provider claims it touched) and identifies every changed path -- modified,
newly created, deleted, or renamed -- so a caller can fail the execution
closed, before it is ever persisted as "completed", if any of them falls
outside allowed_paths.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, Iterable, List, Sequence

from manager.tasks import TaskError
from manager.trusted_ingress import REQUIRED_REPO_WRITE_TASK_POLICIES
from manager.worktree_materializer import OWNER_MARKER_FILENAME


class AllowedPathsViolationError(TaskError):
    """Raised with every offending path -- never just the first one found --
    so the caller's failure summary/evidence can name the actual scope
    breach rather than a generic rejection."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = list(violations)
        super().__init__(
            f"repo-write execution modified path(s) outside its admitted allowed_paths: {self.violations}")


def is_bounded_repo_write_snapshot(snapshot: Dict[str, Any]) -> bool:
    """Whether an Execution's own `task_snapshot` (manager.executions.
    task_snapshot()) represents a genuine, bounded v2-repo-write Task that
    this enforcement must run for.

    Deliberately independent of manager.trusted_ingress.repo_write_policy_
    satisfied(): that function also requires source_context.repo, but
    task_snapshot() never carries source_context (see its own field list) --
    reusing it directly against a snapshot would always return False and
    silently disable this entire check in production. This checks only the
    fields task_snapshot() actually preserves: read_only/needs_repo_edit,
    execution_policies (Slice A's own bounded-write marker set), a non-empty
    allowed_paths, and a baseline_head to diff against.
    """
    if snapshot.get("read_only") is not False or snapshot.get("needs_repo_edit") is not True:
        return False
    policies = snapshot.get("execution_policies")
    if not (isinstance(policies, list) and REQUIRED_REPO_WRITE_TASK_POLICIES.issubset(set(policies))):
        return False
    allowed_paths = snapshot.get("allowed_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        return False
    baseline_head = snapshot.get("baseline_head")
    return isinstance(baseline_head, str) and bool(baseline_head)


def _run(cwd, *args, runner=subprocess.run):
    return runner(["git", "-C", str(cwd), *args], text=True, encoding="utf-8", errors="replace", capture_output=True)


def collect_changed_paths(working_directory, baseline_head: str, runner=subprocess.run) -> List[str]:
    """Every repo-relative path the worktree's actual git state shows as
    touched relative to `baseline_head` -- modified, added, or deleted
    tracked files, both sides of a rename/copy, plus any new untracked file
    -- never derived from what the provider claims to have edited.

    `git diff <baseline_head>` (no second ref) compares the given commit
    directly against the current working tree through the index, so it
    already captures both staged and unstaged tracked-file changes; it
    cannot see untracked files, which `git ls-files --others
    --exclude-standard` supplies separately.

    Excludes exactly `manager.worktree_materializer.OWNER_MARKER_FILENAME`
    (`.adm-worktree-owner.json`) at the worktree root -- ADM's own internal
    ownership-tracking file, written unconditionally into every repo-write
    worktree by `materialize_worktree()`, never something a provider wrote
    or a task's `allowed_paths` could ever have named. This exclusion is
    exact-match only (never a prefix/suffix/wildcard match), so a similarly
    named path a provider actually created or modified --
    `.adm-worktree-owner.json.bak`, `.adm-other.json`, the marker filename
    appearing anywhere other than the worktree root -- is still collected
    and enforced normally; only this one literal ADM-owned path is exempt.
    """
    diff = _run(working_directory, "diff", "--name-status", "-M", "--no-color", baseline_head, "--", ".", runner=runner)
    if diff.returncode != 0:
        raise TaskError(f"git diff against baseline_head failed: {(diff.stderr or '').strip()}")
    changed = set()
    for line in diff.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status[:1] in ("R", "C") and len(parts) >= 3:
            # Both sides of a rename/copy must be checked: a file moved from
            # an allowed path to a disallowed one (or vice versa) is a real
            # scope breach either way.
            changed.add(parts[1])
            changed.add(parts[2])
        elif len(parts) >= 2:
            changed.add(parts[1])

    untracked = _run(working_directory, "ls-files", "--others", "--exclude-standard", runner=runner)
    if untracked.returncode != 0:
        raise TaskError(f"git ls-files failed: {(untracked.stderr or '').strip()}")
    for line in untracked.stdout.splitlines():
        if line.strip():
            changed.add(line.strip())

    changed.discard(OWNER_MARKER_FILENAME)
    return sorted(changed)


def _normalize_changed_path(path: str) -> str:
    """Defensive re-normalization of a path `git` itself reported. git
    always emits repo-relative, forward-slash, non-traversal paths in this
    output -- but this never trusts that blindly: an absolute path, a `..`
    segment, or a backslash surfacing here fails closed rather than being
    silently compared as if it were safe."""
    if not isinstance(path, str) or not path:
        raise TaskError("changed path from git is empty or not a string")
    normalized = path.replace("\\", "/")
    segments = normalized.split("/")
    if normalized.startswith("/") or normalized.startswith("~") or any(segment in ("", ".", "..") for segment in segments):
        raise TaskError(f"changed path is not a safe repo-relative path: {path!r}")
    return normalized


def _normalize_allowed_path(path: str) -> str:
    """Same fail-closed shape check applied to the Task's own admitted
    allowed_paths -- a malformed entry here (this should already be
    impossible past cloud.dispatch_ingress's own admission-time validation,
    but this module never assumes that) is rejected outright rather than
    silently matched against or ignored."""
    if not isinstance(path, str) or not path:
        raise TaskError("allowed_paths entry is empty or not a string")
    normalized = path.replace("\\", "/").rstrip("/")
    segments = normalized.split("/")
    if not normalized or normalized.startswith("/") or normalized.startswith("~") or any(segment in ("", ".", "..") for segment in segments):
        raise TaskError(f"allowed_paths entry is not a safe repo-relative path: {path!r}")
    return normalized


def _is_within_allowed(changed: str, allowed: str) -> bool:
    """`allowed` authorizes itself exactly, and any true descendant path
    (separator-bounded, so `manager/foo.py` never accidentally authorizes a
    sibling-looking `manager/foo.py.bad`, only real `manager/foo.py/...`
    descendants)."""
    return changed == allowed or changed.startswith(allowed + "/")


def enforce_allowed_paths(working_directory, baseline_head: str, allowed_paths: Iterable[str], runner=subprocess.run) -> List[str]:
    """Return the sorted list of changed paths if every one of them falls
    within `allowed_paths`; raise AllowedPathsViolationError (naming every
    offending path) otherwise. Fails closed on a malformed allowed_paths
    entry or an unsafe changed-path shape rather than silently permitting
    either.
    """
    normalized_allowed = [_normalize_allowed_path(path) for path in allowed_paths]
    if not normalized_allowed:
        raise TaskError("allowed_paths must be a non-empty list to enforce")
    changed_paths = [_normalize_changed_path(path) for path in collect_changed_paths(working_directory, baseline_head, runner=runner)]
    violations = [path for path in changed_paths if not any(_is_within_allowed(path, allowed) for allowed in normalized_allowed)]
    if violations:
        raise AllowedPathsViolationError(violations)
    return changed_paths
