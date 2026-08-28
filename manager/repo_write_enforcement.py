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

Test-evidence enforcement (P0, 2026-08-28): a provider's own claim to have
run tests before committing was never verified or even captured -- a live
E2E proved a "completed" repo-write execution could carry `tests_status:
"not_provided"` and still be treated as durably done. `capture_repo_write_
evidence` now runs a Task's own declared `validation_command` itself (a
caller-supplied, project-agnostic shell command -- never hardcoded to one
ecosystem's test runner) directly in the isolated worktree, after the real
commit/push above, and records the real command, exit code, bounded output,
and timestamps -- never a provider's self-report. A Task with no
validation_command is `tests_status: "not_required"` and is never gated on
it. A Task WITH one is never marked "completed" by manager.execution_runner
when that command's real exit code is nonzero (or it times out) -- see that
module's own status-downgrade logic, which reads this function's returned
`tests_status` rather than treating any repo-write evidence as automatic
success.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Sequence

from manager.remote_readback import verify_remote_branch_matches
from manager.tasks import TaskError, now_iso
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


def _empty_commits(working_directory, commits: Sequence[str], runner=subprocess.run) -> List[str]:
    """Every commit SHA in `commits` whose own tree change is empty (e.g.
    `git commit --allow-empty`, or a commit whose only change was later
    reverted within the same range) -- checked individually via `git
    diff-tree` rather than inferred from the aggregate baseline..HEAD diff,
    since an aggregate diff can be non-empty even while one commit inside
    the range contributed nothing real."""
    empty = []
    for sha in commits:
        result = _run(working_directory, "diff-tree", "--no-commit-id", "--name-only", "-r", sha, runner=runner)
        if result.returncode != 0:
            raise TaskError(f"git diff-tree for commit {sha} failed: {(result.stderr or '').strip()}")
        if not result.stdout.strip():
            empty.append(sha)
    return empty


DEFAULT_VALIDATION_TIMEOUT_SECONDS = 600
MAX_VALIDATION_OUTPUT_CHARS = 4000


def _run_validation_command(working_directory, command: str, runner=subprocess.run,
                            timeout_seconds: int = DEFAULT_VALIDATION_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Independently run a Task's own declared validation_command in the
    isolated worktree and capture real, bounded evidence of the outcome --
    never a provider's self-report of having run it. Deliberately generic
    (a plain shell command string, `shell=True`): pytest, `npm test`,
    `node --test`, or any project-defined validation script all work the
    same way, so this never hardcodes one ecosystem's test runner. The
    caller treats a nonzero exit_code OR timed_out=True as failure; output
    is combined stdout+stderr, truncated to the last
    MAX_VALIDATION_OUTPUT_CHARS characters so a runaway or noisy test suite
    can never bloat a persisted Drive record.

    Any failure to even launch/complete the command at all -- not just a
    timeout, but e.g. the shell interpreter itself being unavailable, a
    permissions error, or any other OS-level failure -- is captured here as
    exit_code=None and reported to the caller exactly like a failed run
    (never re-raised): a validation_command that could not be proven to have
    passed must never be silently read downstream as "no evidence" and
    fall through to a completed status by omission."""
    started_at = now_iso()
    try:
        result = runner(command, cwd=str(working_directory), shell=True, text=True,
                        encoding="utf-8", errors="replace", capture_output=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) or isinstance(exc.stderr, str) else ""
        return {
            "command": command, "exit_code": None,
            "output_summary": (output[-MAX_VALIDATION_OUTPUT_CHARS:] if output
                               else f"validation_command timed out after {timeout_seconds}s"),
            "started_at": started_at, "completed_at": now_iso(), "timed_out": True,
        }
    except Exception as exc:
        return {
            "command": command, "exit_code": None,
            "output_summary": f"validation_command could not be run: {exc}"[-MAX_VALIDATION_OUTPUT_CHARS:],
            "started_at": started_at, "completed_at": now_iso(), "timed_out": False,
        }
    output = (result.stdout or "") + (result.stderr or "")
    return {
        "command": command, "exit_code": result.returncode,
        "output_summary": output[-MAX_VALIDATION_OUTPUT_CHARS:],
        "started_at": started_at, "completed_at": now_iso(), "timed_out": False,
    }


def capture_repo_write_evidence(working_directory, baseline_head: str, branch: str, files_changed: Sequence[str],
                                validation_command: Optional[str] = None,
                                validation_timeout_seconds: int = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
                                runner=subprocess.run) -> Dict[str, Any]:
    """Build real, independently-verified terminal success evidence for a
    completed repo-write execution (Global Hands-off Execution Layer, Slice
    D2): the actual changed paths (already verified in-scope by
    enforce_allowed_paths()), the actual commit SHAs and final HEAD made in
    the isolated worktree, and independent remote-readback proof the feature
    branch was actually pushed and exactly matches the local HEAD -- never a
    provider's self-reported "Commit SHA:"/"GitHub push status:" text.

    Fails closed -- raises TaskError, never returns partial or fabricated
    evidence -- on every one of these, so none of them can ever reach a
    persisted "completed" execution:
      - zero changed files (a provider-reported "completed" turn that
        touched nothing real);
      - zero commits since baseline_head (a "baseline-only" branch: the
        worktree was never actually committed to, whether or not it was
        pushed) -- under the Bootstrap architecture this is caught earlier,
        by execution_runner.py's own check before it ever calls
        commit_and_push_repo_write_changes(), but this function re-proves it
        independently rather than trusting that caller;
      - final_commit_sha == baseline_head (the same zero-new-commits case,
        checked directly rather than only inferred from an empty commit
        list, in case baseline_head itself ever becomes reachable again
        through some other ref manipulation);
      - any individual commit in the range whose own tree diff is empty
        (`git commit --allow-empty` or an equivalent no-op commit used to
        manufacture the appearance of progress);
      - a push that cannot be independently verified against the real
        remote (manager.remote_readback.verify_remote_branch_matches).

    `validation_command`, when the Task declared one, is run by THIS
    function itself in the isolated worktree (_run_validation_command) --
    never taken from a provider's self-report -- and the real exit code
    (or a timeout) determines `tests_status`: "passed"/"failed". A Task
    with no validation_command gets `tests_status: "not_required"` and
    `tests: []`, and is never gated on test outcome at all -- this function
    still raises on the commit/push checks above regardless of
    validation_command, but never raises merely because a declared
    validation failed; the caller (manager.execution_runner) is the one
    that reads the returned tests_status and decides whether that downgrades
    the execution's own terminal status, so the real command/exit_code/
    output evidence is always persisted (via record_repo_write_evidence)
    even when validation failed, never discarded by an exception.
    """
    commits = collect_commit_shas(working_directory, baseline_head, runner=runner)
    if not commits:
        raise TaskError("repo-write execution recorded no commits since baseline_head (baseline-only branch)")
    final_commit_sha = current_head_sha(working_directory, runner=runner)
    if commits[-1] != final_commit_sha:
        raise TaskError("final commit SHA does not match the tip of the collected commit history")
    if final_commit_sha == baseline_head:
        raise TaskError("final commit SHA matches baseline_head; no new commit was actually made")
    if not files_changed:
        raise TaskError("repo-write execution produced no changed files (zero real changes)")
    empty_commits = _empty_commits(working_directory, commits, runner=runner)
    if empty_commits:
        raise TaskError(f"repo-write execution contains empty commit(s) with no tree changes: {empty_commits}")
    branch_short = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
    readback = verify_remote_branch_matches(working_directory, branch_short, final_commit_sha, runner=runner)
    if validation_command:
        test_result = _run_validation_command(working_directory, validation_command, runner=runner,
                                              timeout_seconds=validation_timeout_seconds)
        tests = [test_result]
        tests_status = "failed" if test_result["timed_out"] or test_result["exit_code"] != 0 else "passed"
    else:
        tests = []
        tests_status = "not_required"
    return {
        "files_changed": list(files_changed), "commits": commits, "final_commit_sha": final_commit_sha,
        "branch": branch_short, "worktree_path": str(working_directory),
        "push_status": "verified", "remote_sha": readback["remote_sha"],
        "tests": tests,
        "tests_status": tests_status,
    }
