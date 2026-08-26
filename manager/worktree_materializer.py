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

from manager.production_guard import ProductionPathGuardError, assert_not_production_path
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


DEFAULT_BRANCH_PREFIX = "adm-worktree"
DEFAULT_WORKTREE_ID_PREFIX = ""
SUPPORTED_ISOLATION_MODES = frozenset({"worktree_per_task"})


def compute_worktree_identity(project_id: str, task_id: str, branch_prefix: str = DEFAULT_BRANCH_PREFIX,
                              worktree_id_prefix: str = DEFAULT_WORKTREE_ID_PREFIX) -> Dict[str, str]:
    """Pure function of (project_id, task_id, and the project's own registered
    isolation_policy naming -- itself a pure function of project_id via the
    Global Project Registry) -- never a session/conversation/timestamp
    identity. A retry (same project_id/task_id, same registry state) always
    recomputes the exact same branch/worktree_id.

    `branch_prefix`/`worktree_id_prefix` come from the project's own
    isolation_policy.branch_prefix/worktree_prefix (Slice B) when materialize_
    worktree() calls this -- never hardcoded per-project -- so this stays
    project-agnostic; the defaults here only apply when a project's registry
    entry declares no explicit prefix.

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
    short_branch = f"{branch_prefix.strip('/')}/{project_id}/{task_id}"
    return {
        "branch": f"refs/heads/{short_branch}",
        "branch_short": short_branch,
        "worktree_id": f"{worktree_id_prefix}{project_id}--{task_id}",
    }


def _verify_isolation_policy(project) -> Dict[str, str]:
    """The project's own registered isolation_policy (Slice B) must actually
    call for per-task worktree isolation -- this is what makes worktree
    materialization the correct/expected execution path for this project at
    all, rather than an assumption this module bakes in unconditionally.
    Any other (or missing) mode fails closed rather than silently
    materializing a worktree the project's own policy never asked for."""
    policy = project.isolation_policy if isinstance(project.isolation_policy, dict) else {}
    mode = policy.get("mode")
    if mode not in SUPPORTED_ISOLATION_MODES:
        raise WorktreeMaterializationError(
            "isolation_policy_not_worktree_per_task",
            f"project {project.project_id!r} isolation_policy.mode is {mode!r}, not one of {sorted(SUPPORTED_ISOLATION_MODES)}",
        )
    return {
        "branch_prefix": policy.get("branch_prefix") or DEFAULT_BRANCH_PREFIX,
        "worktree_id_prefix": policy.get("worktree_prefix") or DEFAULT_WORKTREE_ID_PREFIX,
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


def _reset_worktree_to_baseline(worktree_path: Path, baseline_head: str, owner, runner) -> None:
    """Same-task retry hygiene (Global Hands-off Execution Layer, Slice C2):
    a worktree already materialized for this exact task/branch/baseline_head
    (ownership already verified by the caller) may still carry the PRIOR
    attempt's provider changes -- stray commits made after baseline_head,
    uncommitted edits, stray untracked files -- reusing it as-is would
    silently hand a retry a dirty starting point instead of the admitted
    baseline. This restores the worktree deterministically back to
    baseline_head and removes every untracked/ignored artifact the prior
    attempt may have left (including ADM's own owner marker, since it too is
    untracked), then recreates the owner marker -- refusing (fail-closed)
    rather than reusing the worktree if any step cannot be proven to have
    succeeded. A fresh task_id never reaches this function at all: it only
    runs once an existing worktree at this exact deterministic path has
    already been verified to belong to this project_id/task_id/branch/
    baseline_head."""
    reset = _run(worktree_path, "reset", "--hard", baseline_head, runner=runner)
    if reset.returncode != 0:
        raise WorktreeMaterializationError(
            "retry_reset_failed", f"git reset --hard {baseline_head} failed: {(reset.stderr or '').strip()}")
    head = _run(worktree_path, "rev-parse", "HEAD", runner=runner)
    if head.returncode != 0 or (head.stdout or "").strip() != baseline_head:
        raise WorktreeMaterializationError(
            "retry_reset_verification_failed",
            f"worktree at {worktree_path} did not verifiably reset to baseline_head {baseline_head!r}",
        )

    clean = _run(worktree_path, "clean", "-fdx", runner=runner)
    if clean.returncode != 0:
        raise WorktreeMaterializationError(
            "retry_clean_failed", f"git clean -fdx failed at {worktree_path}: {(clean.stderr or '').strip()}")
    status = _run(worktree_path, "status", "--porcelain", runner=runner)
    if status.returncode != 0 or (status.stdout or "").strip():
        raise WorktreeMaterializationError(
            "retry_clean_verification_failed",
            f"worktree at {worktree_path} is not verifiably clean after retry cleanup: {(status.stdout or '').strip()!r}",
        )

    _write_owner_marker(worktree_path, owner)
    if _read_owner_marker(worktree_path) != owner:
        raise WorktreeMaterializationError(
            "retry_owner_marker_verification_failed",
            f"ADM owner marker could not be verified after retry cleanup at {worktree_path}",
        )


def _ensure_physical_worktree(canonical_checkout, worktree_path: Path, branch: str, branch_short: str,
                               baseline_head: str, owner, runner) -> None:
    # Defense in depth: the deterministic worktrees/<project_id>/<task_id>
    # naming already keeps this path away from any real checkout, but never
    # trust that by construction alone -- if a workspace_root were ever
    # misconfigured to point at (or under) a marked production runtime
    # checkout, this must still fail closed rather than materialize a
    # "worktree" that is actually the protected production path.
    try:
        assert_not_production_path(worktree_path, "materialize an isolated worktree")
    except ProductionPathGuardError as exc:
        raise WorktreeMaterializationError("production_path_protected", str(exc)) from exc

    entries = _list_worktrees(canonical_checkout, runner)
    match = next((entry for entry in entries if _same_path(entry.get("path"), worktree_path)), None)
    if match is not None:
        _verify_existing_worktree_owner(worktree_path, branch, baseline_head, owner)
        _reset_worktree_to_baseline(worktree_path, baseline_head, owner, runner)
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
        _reset_worktree_to_baseline(worktree_path, baseline_head, owner, runner)
        return

    _write_owner_marker(worktree_path, owner)


def verify_checkout_repo_identity(checkout_path, project, runner=subprocess.run) -> None:
    """Verify that `checkout_path` is a real git checkout whose `origin`
    remote matches `project`'s registered repo identity.

    Used by manager.execution_runner's legacy (non-worktree) working_
    directory resolution path: when a working_directory is resolved via the
    Global Project Registry's workspace_root convention rather than through
    materialize_worktree() itself, nothing else has ever confirmed the
    resolved local path (a machine-local junction/pointer someone else
    maintains) actually points at the right repository -- a stale or
    mis-pointed workspace pointer must fail closed here rather than
    silently running a task against the wrong repository (see
    fix/direct-dispatch-working-directory-authority-p0-20260822 R2)."""
    result = _run(checkout_path, "remote", "get-url", "origin", runner=runner)
    if result.returncode != 0:
        raise WorktreeMaterializationError(
            "workspace_checkout_invalid",
            f"{checkout_path} does not look like a git checkout with an 'origin' remote "
            f"(git remote get-url origin failed): {(result.stderr or '').strip()}",
        )
    remote_identity = normalize_repo_identity((result.stdout or "").strip())
    if remote_identity != project.repo_identity:
        raise WorktreeMaterializationError(
            "workspace_checkout_repo_mismatch",
            f"{checkout_path} is a checkout of {remote_identity!r}, not project "
            f"{project.project_id!r}'s registered repo {project.repo_identity!r}",
        )


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
    naming = _verify_isolation_policy(project)

    baseline_head = task["baseline_head"]
    default_branch = project.default_branch or "main"
    _verify_baseline_lineage(canonical_checkout, baseline_head, default_branch, runner)

    identity = compute_worktree_identity(project_id, task_id, **naming)
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
