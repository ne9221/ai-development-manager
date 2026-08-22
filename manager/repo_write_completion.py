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

import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence

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


class EmptyChangesError(TaskError):
    """Raised when the worktree is clean (nothing left to stage) and the
    current HEAD still equals `baseline_head` -- i.e. no commit exists,
    anywhere, that could represent this execution's admitted changes. This
    is the P0 fix for a real fake-completion path: a clean worktree alone
    was previously treated as proof of a successful repo-write, even when
    it just meant the provider made no durable change at all. That state
    must terminalize as a genuine no-op (`empty_changes`), never as a
    completed repo-write."""


class CommitLineageMismatchError(TaskError):
    """Raised when a clean worktree's current HEAD cannot be proven to be a
    real repo-write completion commit for this exact baseline/task/branch --
    covers an unrelated commit landing in the worktree, a HEAD that does not
    descend from `baseline_head`, an unparsable commit message, or a
    parsable one whose task_id/branch identity does not match what this
    execution expects. A clean worktree is only ever eligible for terminal
    completion once this proof holds; it is never inferred from clean/dirty
    status alone."""


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


# Mirrors _deterministic_commit_message()'s own format exactly -- this is a
# self-round-trip, not an independent spec, so any change to one must change
# the other together.
_COMMIT_HEADER_RE = re.compile(r"^chore\(adm-d2\): (?P<task_id>\S+) execution (?P<execution_id>\S+) repo-write completion\s*$")
_COMMIT_BRANCH_RE = re.compile(r"^branch: (?P<branch>.+)$")


def _parse_completion_commit_message(message: str) -> Optional[Dict[str, str]]:
    """Parse a commit message back into the identity it was created to
    prove: which task, which originating execution, and which branch. Returns
    None (never raises) for anything that does not match the deterministic
    format exactly -- including any unrelated commit -- so the caller can
    treat "unparsable" as a lineage failure without a separate code path."""
    lines = (message or "").splitlines()
    if not lines:
        return None
    header = _COMMIT_HEADER_RE.match(lines[0])
    if not header:
        return None
    branch = None
    for line in lines[1:]:
        branch_match = _COMMIT_BRANCH_RE.match(line)
        if branch_match:
            branch = branch_match.group("branch")
            break
    if branch is None:
        return None
    return {"task_id": header.group("task_id"), "execution_id": header.group("execution_id"), "branch": branch}


def _verify_reusable_completion_commit(working_directory, commit_sha: str, baseline_head: str, task_id: str,
                                       branch: str, runner) -> Dict[str, str]:
    """Prove that a clean worktree's current HEAD is a genuine repo-write
    completion commit for this exact baseline/task/branch before it is ever
    reused as "already done" -- never inferred from clean/dirty status
    alone. `execution_id` is deliberately NOT gated here: a real retry of
    the same task legitimately mints a fresh execution_id per attempt (see
    manager.executions.prepare_task_retry), so the identity that must match
    for safe reuse is (task_id, branch), not execution_id -- the parsed
    execution_id is still returned so callers can record which execution
    actually produced the commit."""
    ancestor = _run(working_directory, "merge-base", "--is-ancestor", baseline_head, commit_sha, runner=runner)
    if ancestor.returncode != 0:
        raise CommitLineageMismatchError(
            f"HEAD {commit_sha} is not a descendant of baseline_head {baseline_head} -- refusing to treat an "
            "unrelated commit as this execution's repo-write completion"
        )
    message = _git_ok(working_directory, "log", "-1", "--format=%B", commit_sha, runner=runner, label="log -1 --format=%B")
    identity = _parse_completion_commit_message(message)
    if identity is None:
        raise CommitLineageMismatchError(
            f"HEAD {commit_sha} does not carry a recognizable repo-write completion commit message -- cannot "
            "verify it belongs to this execution's lineage, so it can never be reused"
        )
    if identity["task_id"] != task_id:
        raise CommitLineageMismatchError(
            f"HEAD {commit_sha} belongs to task {identity['task_id']!r}, not the expected task {task_id!r}"
        )
    if identity["branch"] != branch:
        raise CommitLineageMismatchError(
            f"HEAD {commit_sha} was recorded for branch {identity['branch']!r}, not the expected branch {branch!r}"
        )
    return identity


def stage_and_commit(working_directory, changed_paths: Sequence[str], task_id: str, execution_id: str, branch: str,
                     baseline_head: str, runner=subprocess.run) -> Dict[str, Any]:
    """Stage exactly `changed_paths` (never `git add .`) and commit them with
    a deterministic message.

    If the worktree is already clean, this is only ever treated as "already
    done" once proven: HEAD must differ from `baseline_head` (a clean
    worktree whose HEAD still equals baseline_head means no commit exists at
    all -- that terminalizes as EmptyChangesError, never a fake success), and
    HEAD must be a real, identity-matching repo-write completion commit for
    this exact (task_id, branch), descending from baseline_head (see
    `_verify_reusable_completion_commit`). Only once both hold is the
    existing HEAD reused, so a retry after a successful commit can never
    produce a second commit -- and a clean worktree can never be mistaken
    for success on its own.

    If the worktree is dirty, the pre-commit HEAD must itself already equal
    `baseline_head` -- if it does not, some other commit landed in this
    worktree between the caller's baseline snapshot and now, and building a
    completion commit on top of an unexpected lineage is refused rather than
    silently proceeding."""
    if not changed_paths:
        raise TaskError("repo-write completion requires at least one admitted changed path")

    if _worktree_clean(working_directory, runner):
        head = _git_ok(working_directory, "rev-parse", "HEAD", runner=runner, label="rev-parse HEAD")
        if head == baseline_head:
            raise EmptyChangesError(
                f"worktree is clean and HEAD ({head}) still equals baseline_head -- no repo-write completion "
                "commit exists for this execution; this is a genuine no-op, not a completed repo-write"
            )
        identity = _verify_reusable_completion_commit(working_directory, head, baseline_head, task_id, branch, runner)
        return {"commit_sha": head, "created": False, "commit_identity": identity}

    pre_commit_head = _git_ok(working_directory, "rev-parse", "HEAD", runner=runner, label="rev-parse HEAD")
    if pre_commit_head != baseline_head:
        raise CommitLineageMismatchError(
            f"current HEAD ({pre_commit_head}) does not match the expected baseline_head ({baseline_head}) -- "
            "refusing to build a repo-write completion commit on top of an unexpected lineage"
        )

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
    identity = {"task_id": task_id, "execution_id": execution_id, "branch": branch}
    return {"commit_sha": commit_sha, "created": True, "commit_identity": identity}


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


def run_validation_gate(working_directory, validation_checks: Sequence[Dict[str, Any]], runner=subprocess.run) -> List[Dict[str, Any]]:
    """Run every required validation check (Global Hands-off P0-B: resolved
    from the project's own authoritative configuration -- see
    manager.project_registry.ProjectMetadata.required_validation_checks --
    never a provider- or caller-supplied default) in order against the
    provider's real, still-uncommitted working tree. A project that requires
    checks A/B/C must pass all of them; the first failing check raises
    immediately (with its own id attached to the evidence) and blocks every
    check after it, exactly like a single tests gate did before."""
    if not isinstance(validation_checks, (list, tuple)) or not validation_checks:
        raise TaskError("repo-write completion requires at least one required validation check")
    evidence = []
    for check in validation_checks:
        if not isinstance(check, dict) or not check.get("id") or not check.get("command"):
            raise TaskError(f"malformed validation check {check!r}: requires non-empty 'id' and 'command'")
        try:
            result = run_tests_gate(working_directory, check["command"], runner=runner)
        except TestsGateFailedError as exc:
            exc.evidence["id"] = check["id"]
            raise
        result["id"] = check["id"]
        evidence.append(result)
    return evidence


def complete_repo_write_execution(*, working_directory, changed_paths: List[str], baseline_head: str, branch: str,
                                  repository: str, validation_checks: Sequence[Dict[str, Any]], task_id: str,
                                  execution_id: str, remote: str = "origin", runner=subprocess.run) -> Dict[str, Any]:
    """Orchestrate the full D2 terminal-completion sequence for one bounded
    repo-write execution whose provider already stopped and reported
    success, and whose actual changed paths were already re-verified
    in-scope by manager.repo_write_enforcement.enforce_allowed_paths (the
    caller passes that call's own return value as `changed_paths`, never
    recomputing it here). `validation_checks` must be resolved by the caller
    from the project's own authoritative configuration (never guessed here,
    never weakenable by the provider). Returns the completion evidence a
    caller should persist onto the terminal Execution; raises TaskError (or
    a subclass) on any failure, before which nothing irreversible has
    happened past that point.
    """
    validation_evidence = run_validation_gate(working_directory, validation_checks, runner=runner)
    commit = stage_and_commit(working_directory, changed_paths, task_id, execution_id, branch, baseline_head, runner=runner)
    remote_sha = push_and_verify(working_directory, branch, commit["commit_sha"], remote=remote, runner=runner)
    return {
        "changed_paths": list(changed_paths),
        "tests": validation_evidence,
        "commit_sha": commit["commit_sha"],
        "commit_created": commit["created"],
        "commit_identity": commit["commit_identity"],
        "remote_sha": remote_sha,
        "branch": branch,
        "repository": repository,
        "baseline_head": baseline_head,
    }
