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

Bootstrap architecture: this module also carries the ADM host's own git
commit/push authority (`commit_and_push_repo_write_changes`). A repo-write
provider (Codex, sandbox="workspace-write") only edits files and runs local
checks inside its isolated worktree -- it is never expected or trusted to
run `git commit`/`git push` itself. Once an execution's changed paths are
proven in-scope, ADM stages exactly those admitted paths, commits, and
pushes the isolated feature branch using its own git credentials/network;
`capture_repo_write_evidence` then independently reads back the real remote
to prove that push landed, exactly as before.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Sequence

from manager.remote_readback import verify_remote_branch_matches
from manager.tasks import TaskError
from manager.trusted_ingress import REQUIRED_REPO_WRITE_TASK_POLICIES
from manager.worktree_materializer import OWNER_MARKER_FILENAME

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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


def commit_and_push_repo_write_changes(working_directory, branch: str, files_changed: Sequence[str],
                                       commit_message: str, runner=subprocess.run) -> str:
    """ADM-host git authority for a completed repo-write execution (Bootstrap
    architecture): the provider only edits files and runs local checks inside
    its isolated worktree -- it never runs `git commit`/`git push` itself.
    Once `enforce_allowed_paths()` has proven `files_changed` is real and
    in-scope, this stages exactly those admitted paths (never `git add .`),
    commits them on the worktree's already-isolated feature branch, and
    pushes that exact branch to `origin` using the ADM host's own git
    credentials/network -- never `main`, never a merge, never a force push.

    Raises TaskError on any git failure (stage, commit, or push) so a failed
    host-side commit/push can never be mistaken for success; the caller's
    own `capture_repo_write_evidence()` performs the independent remote
    readback afterward, against whatever this function actually pushed.
    """
    if not files_changed:
        raise TaskError("commit_and_push_repo_write_changes requires a non-empty files_changed list")
    branch_short = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
    add_result = _run(working_directory, "add", "--", *files_changed, runner=runner)
    if add_result.returncode != 0:
        raise TaskError(f"git add failed: {(add_result.stderr or '').strip()}")
    commit_result = _run(working_directory, "commit", "-m", commit_message, runner=runner)
    if commit_result.returncode != 0:
        raise TaskError(f"git commit failed: {(commit_result.stderr or '').strip()}")
    push_result = _run(working_directory, "push", "origin", f"HEAD:refs/heads/{branch_short}", runner=runner)
    if push_result.returncode != 0:
        raise TaskError(f"git push failed: {(push_result.stderr or '').strip()}")
    return current_head_sha(working_directory, runner=runner)


def collect_commit_shas(working_directory, baseline_head: str, runner=subprocess.run) -> List[str]:
    """Every commit SHA actually made in the isolated worktree since
    `baseline_head`, oldest first, ending at the current HEAD -- never
    derived from a provider's self-reported "Commit SHA:" text."""
    result = _run(working_directory, "rev-list", "--reverse", f"{baseline_head}..HEAD", runner=runner)
    if result.returncode != 0:
        raise TaskError(f"git rev-list against baseline_head failed: {(result.stderr or '').strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def current_head_sha(working_directory, runner=subprocess.run) -> str:
    result = _run(working_directory, "rev-parse", "HEAD", runner=runner)
    if result.returncode != 0:
        raise TaskError(f"git rev-parse HEAD failed: {(result.stderr or '').strip()}")
    sha = (result.stdout or "").strip()
    if not SHA_PATTERN.match(sha):
        raise TaskError(f"git rev-parse HEAD returned an unexpected value: {sha!r}")
    return sha


def capture_repo_write_evidence(working_directory, baseline_head: str, branch: str, files_changed: Sequence[str],
                                test_evidence: Optional[Sequence[str]] = None, runner=subprocess.run) -> Dict[str, Any]:
    """Build real, independently-verified terminal success evidence for a
    completed repo-write execution (Global Hands-off Execution Layer, Slice
    D2): the actual changed paths (already verified in-scope by
    enforce_allowed_paths()), the actual commit SHAs and final HEAD made in
    the isolated worktree, and independent remote-readback proof the feature
    branch was actually pushed and exactly matches the local HEAD -- never a
    provider's self-reported "Commit SHA:"/"GitHub push status:" text.

    `test_evidence` is preserved verbatim when the caller supplies it (e.g.
    from a provider's structured completion report); it is never inferred or
    fabricated when omitted, so a missing test-evidence source stays an
    empty list rather than a guessed one.

    Raises TaskError -- never returns partial evidence with an unverified
    push -- on any git or remote-readback failure, so a caller can downgrade
    the execution to failed instead of ever persisting invented success
    evidence.
    """
    commits = collect_commit_shas(working_directory, baseline_head, runner=runner)
    final_commit_sha = current_head_sha(working_directory, runner=runner)
    if commits and commits[-1] != final_commit_sha:
        raise TaskError("final commit SHA does not match the tip of the collected commit history")
    branch_short = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
    readback = verify_remote_branch_matches(working_directory, branch_short, final_commit_sha, runner=runner)
    return {
        "files_changed": list(files_changed), "commits": commits, "final_commit_sha": final_commit_sha,
        "branch": branch_short, "worktree_path": str(working_directory),
        "push_status": "verified", "remote_sha": readback["remote_sha"],
        "tests": list(test_evidence) if test_evidence else [],
    }
