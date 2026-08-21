#!/usr/bin/env python3
"""Deterministic branch/worktree materialization (Global Hands-off Execution
Layer, Slice C).

Slice A (manager.trusted_ingress / cloud.dispatch_ingress) establishes the
v2-repo-write admission contract: a Task carries bounded, server-validated
evidence (allowed_paths, baseline_head, source_context.repo) but no working
directory of its own yet. Slice B (manager.project_registry) resolves a
project_id to its canonical repo/branch/workspace/isolation policy without
relying on ChatGPT conversation memory or a hardcoded machine path.

This module is the missing link between the two: given a Task that already
passed v2-repo-write admission, resolve -- deterministically, from
project_id + task_id alone, never from a session/conversation identity --
the exact branch name and worktree path it must run in, create or reuse
that worktree, and persist the result onto the Task (with read-back
verification) before any provider is ever launched against it.

Every failure mode here is fail-closed: a caller never gets silently routed
to the project's canonical checkout (that would defeat isolation and let a
"bounded" write task touch the one shared clone every other task also
reads), and a mismatched retry/duplicate/foreign-task worktree is always
rejected rather than reused or overwritten.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from manager.project_registry import normalize_repo_identity
from manager.tasks import TaskError, update_task
from manager.trusted_ingress import repo_write_policy_satisfied


# Same identifier charset dispatch_ingress already enforces on project_id/
# task_id -- re-validated here defensively since this module may be called
# directly, not only via that ingress.
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
BASELINE_HEAD_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
OWNER_MARKER_FILENAME = ".adm-worktree-owner.json"


class WorktreeMaterializationError(TaskError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def compute_worktree_identity(project_id: str, task_id: str) -> Dict[str, str]:
    """Pure function of (project_id, task_id) -- the only durable identity
    this slice is allowed to key off. No session/conversation/timestamp
    input ever participates, so a retry (same project_id/task_id) always
    recomputes the exact same branch/worktree_id.

    `branch` is the full ref (`refs/heads/...`) persisted onto the Task --
    manager.execution_lifecycle.enter_running_gate() requires a
    production-write Task's own `branch` field to be a full heads ref,
    matching manager.worktree_locks.canonical_branch()'s convention.
    `branch_short` is the same branch's short name, for `git` commands
    (`worktree add -b <short>`) that expect a short branch name rather than
    a fully qualified ref."""
    if not isinstance(project_id, str) or not ID_PATTERN.match(project_id):
        raise WorktreeMaterializationError("invalid_identity", f"invalid project_id: {project_id!r}")
    if not isinstance(task_id, str) or not ID_PATTERN.match(task_id):
        raise WorktreeMaterializationError("invalid_identity", f"invalid task_id: {task_id!r}")
    short_branch = f"adm-worktree/{project_id}/{task_id}"
    return {
        "branch": f"refs/heads/{short_branch}",
        "branch_short": short_branch,
        "worktree_id": f"{project_id}--{task_id}",
    }


def compute_worktree_path(workspace_root, project_id: str, task_id: str) -> Path:
    identity = compute_worktree_identity(project_id, task_id)
    return Path(workspace_root) / "worktrees" / project_id / task_id, identity


def _run(cwd, *args, runner=subprocess.run):
    return runner(["git", "-C", str(cwd), *args], text=True, encoding="utf-8", errors="replace", capture_output=True)


def _git_ok(cwd, *args, runner=subprocess.run, error_code="git_command_failed"):
    result = _run(cwd, *args, runner=runner)
    if result.returncode != 0:
        raise WorktreeMaterializationError(error_code, f"git {' '.join(args)} failed: {(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def _verify_repo_write_evidence(task: Dict[str, Any]) -> None:
    if not repo_write_policy_satisfied(task):
        raise WorktreeMaterializationError(
            "not_repo_write_task",
            "task does not carry valid bounded repo-write admission evidence (Slice A); "
            "worktree materialization only applies to v2-repo-write tasks",
        )


def _verify_repo_identity(project, task: Dict[str, Any]) -> None:
    task_repo = (task.get("source_context") or {}).get("repo")
    if not task_repo or normalize_repo_identity(task_repo) != project.repo_identity:
        raise WorktreeMaterializationError(
            "repo_identity_mismatch",
            f"task's repo {task_repo!r} does not match project {project.project_id!r}'s registered canonical repo",
        )


def _verify_baseline_lineage(canonical_checkout, baseline_head: str, default_branch: str, runner) -> None:
    if not isinstance(baseline_head, str) or not BASELINE_HEAD_PATTERN.match(baseline_head):
        raise WorktreeMaterializationError("invalid_baseline_head", f"baseline_head missing or malformed: {baseline_head!r}")
    exists = _run(canonical_checkout, "cat-file", "-e", f"{baseline_head}^{{commit}}", runner=runner)
    if exists.returncode != 0:
        raise WorktreeMaterializationError(
            "invalid_baseline_head", f"baseline_head {baseline_head} does not exist in the canonical checkout")
    ancestry = _run(canonical_checkout, "merge-base", "--is-ancestor", baseline_head, default_branch, runner=runner)
    if ancestry.returncode != 0:
        raise WorktreeMaterializationError(
            "baseline_lineage_mismatch",
            f"baseline_head {baseline_head} is not part of the allowed canonical lineage ({default_branch})",
        )


def _list_worktrees(canonical_checkout, runner):
    output = _git_ok(canonical_checkout, "worktree", "list", "--porcelain", runner=runner, error_code="worktree_list_failed")
    entries, current = [], {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip()
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
    if current:
        entries.append(current)
    return entries


def _same_path(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def _read_owner_marker(worktree_path) -> Optional[Dict[str, Any]]:
    marker = Path(worktree_path) / OWNER_MARKER_FILENAME
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_owner_marker(worktree_path, owner: Dict[str, Any]) -> None:
    marker = Path(worktree_path) / OWNER_MARKER_FILENAME
    marker.write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")


def _verify_existing_worktree_owner(worktree_path, branch, baseline_head, owner) -> None:
    """A worktree already registered at this deterministic path -- either from
    a genuine retry of this exact task, or a racing duplicate dispatch, or
    (fail-closed case) a foreign task/baseline that must never be silently
    reused or mutated."""
    marker = _read_owner_marker(worktree_path)
    if marker is None or marker.get("project_id") != owner["project_id"] or marker.get("task_id") != owner["task_id"]:
        raise WorktreeMaterializationError(
            "worktree_ownership_mismatch",
            f"worktree at {worktree_path} exists but is not owned by project_id={owner['project_id']!r} task_id={owner['task_id']!r}",
        )
    if marker.get("branch") != branch or marker.get("baseline_head") != baseline_head:
        raise WorktreeMaterializationError(
            "worktree_identity_mismatch",
            f"worktree at {worktree_path} was previously materialized under a different branch/baseline_head "
            f"(recorded branch={marker.get('branch')!r} baseline_head={marker.get('baseline_head')!r}); "
            "refusing to reuse or mutate it",
        )


def _ensure_physical_worktree(canonical_checkout, worktree_path: Path, branch: str, branch_short: str,
                               baseline_head: str, owner, runner) -> None:
    entries = _list_worktrees(canonical_checkout, runner)
    match = next((entry for entry in entries if _same_path(entry.get("path"), worktree_path)), None)
    if match is not None:
        _verify_existing_worktree_owner(worktree_path, branch, baseline_head, owner)
        return

    if worktree_path.exists():
        raise WorktreeMaterializationError(
            "worktree_path_collision", f"path {worktree_path} already exists but is not a registered git worktree")

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = _run(canonical_checkout, "worktree", "add", "-b", branch_short, str(worktree_path), baseline_head, runner=runner)
    if result.returncode != 0:
        # A concurrent duplicate dispatch may have won the create race between
        # our list() and our add(): re-check before treating this as a hard
        # failure, since the deterministic branch/path make that the only
        # benign reason `worktree add` could fail here.
        entries = _list_worktrees(canonical_checkout, runner)
        match = next((entry for entry in entries if _same_path(entry.get("path"), worktree_path)), None)
        if match is None:
            raise WorktreeMaterializationError(
                "worktree_create_failed", f"git worktree add failed: {(result.stderr or '').strip()}")
        _verify_existing_worktree_owner(worktree_path, branch, baseline_head, owner)
        return

    _write_owner_marker(worktree_path, owner)


def materialize_worktree(store, project, task: Dict[str, Any], canonical_checkout, workspace_root, runner=subprocess.run) -> Dict[str, Any]:
    """Ensure a deterministic, isolated git worktree exists for `task`, persist
    its identity onto the Task (read back and verified), and return it.

    `project` is a manager.project_registry.ProjectMetadata (Slice B).
    `canonical_checkout` is the existing, shared clone this project already
    resolves for read-only/legacy tasks -- used here only as the source `git
    worktree add` branches off of; its own working tree is never modified
    (worktree add/list/cat-file/merge-base never touch tracked files).
    `runner` overrides subprocess.run for tests.
    """
    project_id, task_id = task["project_id"], task["task_id"]

    _verify_repo_write_evidence(task)
    _verify_repo_identity(project, task)

    baseline_head = task["baseline_head"]
    default_branch = project.default_branch or "main"
    _verify_baseline_lineage(canonical_checkout, baseline_head, default_branch, runner)

    identity = compute_worktree_identity(project_id, task_id)
    branch, branch_short, worktree_id = identity["branch"], identity["branch_short"], identity["worktree_id"]
    worktree_path = Path(workspace_root) / "worktrees" / project_id / task_id

    # Defense in depth: a Task record that already carries a worktree_id/
    # branch inconsistent with what we just deterministically computed can
    # never be trusted at face value -- whether from a foreign task's
    # spoofed/copied fields or a corrupted record -- fail closed rather than
    # silently accepting whatever the caller's Task dict already claims.
    if task.get("worktree_id") is not None and task["worktree_id"] != worktree_id:
        raise WorktreeMaterializationError(
            "worktree_ownership_mismatch",
            f"task already carries worktree_id {task['worktree_id']!r}, which does not match the "
            f"deterministically computed identity {worktree_id!r} for project_id/task_id",
        )
    if task.get("branch") is not None and task["branch"] != branch:
        raise WorktreeMaterializationError(
            "worktree_identity_mismatch",
            f"task already carries branch {task['branch']!r}, which does not match the deterministically "
            f"computed branch {branch!r}",
        )

    owner = {"project_id": project_id, "task_id": task_id, "worktree_id": worktree_id,
             "branch": branch, "baseline_head": baseline_head}
    _ensure_physical_worktree(canonical_checkout, worktree_path, branch, branch_short, baseline_head, owner, runner)

    resolved_working_directory = str(worktree_path)
    update_task(store, project_id, task_id, working_directory=resolved_working_directory,
                branch=branch, worktree_id=worktree_id, baseline_head=baseline_head)

    # Read-back verification: never trust the write succeeded just because
    # update_task() didn't raise -- re-fetch and compare before this is ever
    # handed to a provider launch.
    persisted = store.get("tasks", project_id, task_id)
    for field, expected in (("working_directory", resolved_working_directory), ("branch", branch),
                             ("worktree_id", worktree_id), ("baseline_head", baseline_head)):
        if persisted.get(field) != expected:
            raise WorktreeMaterializationError(
                "persistence_verification_failed",
                f"Task {field} read back as {persisted.get(field)!r}, expected {expected!r}",
            )

    return {"working_directory": resolved_working_directory, "branch": branch,
            "worktree_id": worktree_id, "baseline_head": baseline_head}
