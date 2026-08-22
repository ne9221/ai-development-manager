#!/usr/bin/env python3
"""Terminal completion for a bounded v2-repo-write execution (Global
Hands-off Execution Layer, Slice D2).

Slice A (manager.trusted_ingress / cloud.dispatch_ingress) admits a bounded
repo-write Task; Slice C (manager.worktree_materializer) guarantees its
provider only ever runs inside its own isolated worktree; Slice D
(manager.repo_write_enforcement) re-verifies, from real `git` state, that
the provider's actual changes stayed within the Task's admitted
allowed_paths. None of that yet turns a provider's "completed" outcome into
a real, pushed, remotely-verified commit -- until this slice, a bounded
repo-write execution could be persisted "completed" having produced nothing
durable at all. This module is that missing terminal step: run the
project's tests gate against the provider's real (still uncommitted)
changes, stage only the exact admitted changed paths (never `git add .`),
create a deterministic commit, push the execution's own isolated task
branch, and read back the remote branch tip to prove the push actually
landed -- all against real `git`/subprocess state, never anything the
provider claims.

Every step here is fail-closed: a tests-gate failure, a staged-path
mismatch, a push failure, or a remote SHA mismatch each raise before any
irreversible step past it runs, and none of them ever silently proceed to
"completed" evidence. Retrying this same call against the same worktree is
always safe: whether a commit/push already happened is derived from real
`git status`/`git ls-remote` state each time, never from a caller-supplied
flag, so a retry after full success reuses the existing commit (never
creates a duplicate) and a retry after a partial failure (e.g. commit
succeeded, push did not) resumes from exactly the real state left behind.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Sequence

from manager.tasks import TaskError

# The isolated task branch this module pushes is always Slice C's own
# per-task branch (e.g. "refs/heads/adm-worktree/<project>/<task>"), never
# a project's default branch -- this is defense in depth, not the primary
# guarantee (Slice C's naming already makes "main"/"master" unreachable
# here), matching the explicit "never push canonical main" requirement.
PROTECTED_BRANCH_REFS = frozenset({"refs/heads/main", "refs/heads/master"})


class TestsGateFailedError(TaskError):
    """Raised with the tests-gate's own evidence attached, so a caller's
    failure summary can name the real command and exit code rather than a
    generic rejection."""

    def __init__(self, evidence: Dict[str, Any], detail: str) -> None:
        self.evidence = evidence
        super().__init__(
            f"tests gate failed (exit {evidence.get('returncode')}) for command "
            f"{' '.join(evidence.get('command', []))}: {detail}"
        )


class CommitStageMismatchError(TaskError):
    """Raised if `git add` staged anything other than exactly the admitted
    changed paths -- should be unreachable given Slice D already verified
    those paths are the only real changes, but this never trusts that
    silently; the index is reset before this is raised so no over-broad
    stage is ever left behind for anything downstream to commit."""


class PushVerificationError(TaskError):
    """Raised when a push cannot be proven to have landed exactly as pushed
    -- a failed push, an unreadable remote ref, or a remote SHA that does
    not match the local commit that was just pushed."""


def _run(cwd, *args, runner=subprocess.run):
    return runner(["git", "-C", str(cwd), *args], text=True, encoding="utf-8", errors="replace", capture_output=True)


def _git_ok(cwd, *args, runner=subprocess.run, label=None):
    result = _run(cwd, *args, runner=runner)
    if result.returncode != 0:
        raise TaskError(f"git {label or ' '.join(args)} failed: {(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def run_tests_gate(working_directory, tests_command: Sequence[str], runner=subprocess.run) -> Dict[str, Any]:
    """Execute the project-specified tests gate against the provider's real,
    still-uncommitted working tree. Runs before any staging/commit so a
    failing tests gate can never leave a committed-but-untested state
    behind."""
    if not isinstance(tests_command, (list, tuple)) or not tests_command or not all(isinstance(part, str) and part for part in tests_command):
        raise TaskError("tests gate requires a non-empty list of command arguments")
    result = runner(list(tests_command), cwd=str(working_directory), text=True, encoding="utf-8", errors="replace", capture_output=True)
    evidence = {"command": list(tests_command), "returncode": result.returncode, "passed": result.returncode == 0}
    if not evidence["passed"]:
        detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[:2000]
        raise TestsGateFailedError(evidence, detail)
    return evidence


def _worktree_clean(working_directory, runner) -> bool:
    return _git_ok(working_directory, "status", "--porcelain", runner=runner, label="status --porcelain") == ""


def _deterministic_commit_message(task_id: str, execution_id: str, branch: str, changed_paths: Sequence[str]) -> str:
    lines = [f"chore(adm-d2): {task_id} execution {execution_id} repo-write completion", "", f"branch: {branch}", "changed_paths:"]
    lines += [f"  - {path}" for path in changed_paths]
    return "\n".join(lines) + "\n"


def stage_and_commit(working_directory, changed_paths: Sequence[str], task_id: str, execution_id: str, branch: str,
                     runner=subprocess.run) -> Dict[str, Any]:
    """Stage exactly `changed_paths` (never `git add .`) and commit them with
    a deterministic message. If the worktree is already clean (a prior call
    already committed these exact changes -- the only way that can be true,
    since Slice D already proved `changed_paths` is non-empty and within
    scope), no new commit is created and the existing HEAD is reused, so a
    retry after a successful commit can never produce a second commit."""
    if not changed_paths:
        raise TaskError("repo-write completion requires at least one admitted changed path")
    if _worktree_clean(working_directory, runner):
        head = _git_ok(working_directory, "rev-parse", "HEAD", runner=runner, label="rev-parse HEAD")
        return {"commit_sha": head, "created": False}

    _git_ok(working_directory, "add", "--", *changed_paths, runner=runner, label="add")
    staged = _git_ok(working_directory, "diff", "--cached", "--name-only", runner=runner, label="diff --cached").splitlines()
    staged_set, expected_set = set(staged), set(changed_paths)
    if staged_set != expected_set:
        _run(working_directory, "reset", runner=runner)
        raise CommitStageMismatchError(
            f"staged paths {sorted(staged_set)} do not exactly match admitted changed paths {sorted(expected_set)}"
        )
    message = _deterministic_commit_message(task_id, execution_id, branch, changed_paths)
    _git_ok(working_directory, "commit", "-m", message, runner=runner, label="commit")
    commit_sha = _git_ok(working_directory, "rev-parse", "HEAD", runner=runner, label="rev-parse HEAD")
    return {"commit_sha": commit_sha, "created": True}


def push_and_verify(working_directory, branch: str, commit_sha: str, remote: str = "origin", runner=subprocess.run) -> str:
    """Push `commit_sha` to the execution's own isolated task branch (never
    force, never main/master) and read back the remote ref to prove the
    push actually landed -- if the pushed commit is already the remote tip
    (a retry after a prior successful push), the push is a real, safe
    no-op; git itself reports success without creating anything new."""
    if branch in PROTECTED_BRANCH_REFS:
        raise TaskError(f"refusing to push protected branch {branch!r}")
    push = _run(working_directory, "push", remote, f"{commit_sha}:{branch}", runner=runner)
    if push.returncode != 0:
        raise PushVerificationError(f"push to {remote} {branch} failed: {(push.stderr or '').strip()}")
    ls_remote = _run(working_directory, "ls-remote", remote, branch, runner=runner)
    if ls_remote.returncode != 0:
        raise PushVerificationError(f"remote SHA readback failed: {(ls_remote.stderr or '').strip()}")
    lines = [line for line in (ls_remote.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise PushVerificationError(f"remote SHA readback returned no ref for {branch!r} on {remote!r}")
    remote_sha = lines[0].split("\t")[0].strip()
    if remote_sha != commit_sha:
        raise PushVerificationError(f"remote SHA {remote_sha} does not match local commit {commit_sha} after push")
    return remote_sha


def complete_repo_write_execution(*, working_directory, changed_paths: List[str], baseline_head: str, branch: str,
                                  repository: str, tests_command: Sequence[str], task_id: str, execution_id: str,
                                  remote: str = "origin", runner=subprocess.run) -> Dict[str, Any]:
    """Orchestrate the full D2 terminal-completion sequence for one bounded
    repo-write execution whose provider already stopped and reported
    success, and whose actual changed paths were already re-verified
    in-scope by manager.repo_write_enforcement.enforce_allowed_paths (the
    caller passes that call's own return value as `changed_paths`, never
    recomputing it here). Returns the completion evidence a caller should
    persist onto the terminal Execution; raises TaskError (or a subclass)
    on any failure, before which nothing irreversible has happened past
    that point.
    """
    tests_evidence = run_tests_gate(working_directory, tests_command, runner=runner)
    commit = stage_and_commit(working_directory, changed_paths, task_id, execution_id, branch, runner=runner)
    remote_sha = push_and_verify(working_directory, branch, commit["commit_sha"], remote=remote, runner=runner)
    return {
        "changed_paths": list(changed_paths),
        "tests": tests_evidence,
        "commit_sha": commit["commit_sha"],
        "commit_created": commit["created"],
        "remote_sha": remote_sha,
        "branch": branch,
        "repository": repository,
        "baseline_head": baseline_head,
    }
