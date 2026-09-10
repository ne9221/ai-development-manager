#!/usr/bin/env python3
"""Tests for runtime allowed_paths enforcement against real git state (Slice D)."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from manager.manager_home import ManagerHomeError
from manager.repo_write_enforcement import (
    AllowedPathsViolationError,
    MANAGER_HOME_ENV_VAR,
    OWNER_MARKER_FILENAME,
    VALIDATION_MANAGER_HOME_PREFIX,
    _remove_validation_manager_home,
    _resolve_validation_command,
    _run_validation_command,
    capture_repo_write_evidence,
    collect_changed_paths,
    collect_commit_shas,
    commit_and_push_repo_write_changes,
    current_head_sha,
    enforce_allowed_paths,
)
from manager.tasks import TaskError


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "manager").mkdir()
    (root / "other").mkdir()
    (root / "manager" / "foo.py").write_text("original\n", encoding="utf-8")
    (root / "other" / "bar.py").write_text("original\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    baseline = _git(root, "rev-parse", "HEAD")
    return {"path": root, "baseline": baseline}


# --- collect_changed_paths reflects real git state --------------------------

def test_allowed_file_modified_pass(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    changed = enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert changed == ["manager/foo.py"]


def test_allowed_directory_descendant_modified_pass(repo):
    (repo["path"] / "manager").mkdir(exist_ok=True)
    (repo["path"] / "manager" / "sub.py").write_text("new\n", encoding="utf-8")
    changed = enforce_allowed_paths(repo["path"], repo["baseline"], ["manager"])
    assert changed == ["manager/sub.py"]


def test_sibling_prefix_confusion_path_rejected(repo):
    # "manager/foo.py" must never accidentally authorize "manager/foo.py.bad".
    (repo["path"] / "manager" / "foo.py.bad").write_text("sneaky\n", encoding="utf-8")
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert exc.value.violations == ["manager/foo.py.bad"]


def test_outside_path_modified_rejected(repo):
    (repo["path"] / "other" / "bar.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert exc.value.violations == ["other/bar.py"]


def test_new_untracked_outside_file_rejected(repo):
    (repo["path"] / "other" / "new_untracked.py").write_text("new\n", encoding="utf-8")
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert exc.value.violations == ["other/new_untracked.py"]


def test_deleted_outside_file_rejected(repo):
    (repo["path"] / "other" / "bar.py").unlink()
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert exc.value.violations == ["other/bar.py"]


def test_renamed_file_crossing_allowed_boundary_rejected(repo):
    _git(repo["path"], "mv", "manager/foo.py", "other/foo.py")
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    # Both sides of the rename are inspected (collect_changed_paths includes
    # the vacated old path too), but only the genuinely out-of-scope new
    # location is reported as a violation -- the old path still matches its
    # own allowed entry even though the file no longer lives there.
    assert exc.value.violations == ["other/foo.py"]


def test_renamed_file_within_allowed_directory_pass(repo):
    _git(repo["path"], "mv", "manager/foo.py", "manager/renamed.py")
    changed = enforce_allowed_paths(repo["path"], repo["baseline"], ["manager"])
    assert set(changed) == {"manager/foo.py", "manager/renamed.py"}


def test_traversal_like_allowed_path_fails_closed(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    for bad_allowed in ("../etc/passwd", "/etc/passwd", "manager/../other"):
        with pytest.raises(TaskError):
            enforce_allowed_paths(repo["path"], repo["baseline"], [bad_allowed])


def test_malformed_allowed_paths_list_fails_closed(repo):
    with pytest.raises(TaskError):
        enforce_allowed_paths(repo["path"], repo["baseline"], [])
    with pytest.raises(TaskError):
        enforce_allowed_paths(repo["path"], repo["baseline"], [""])


def test_no_changes_at_all_passes_with_empty_result(repo):
    changed = enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert changed == []


def test_multiple_allowed_entries_all_respected(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    (repo["path"] / "other" / "bar.py").write_text("changed\n", encoding="utf-8")
    changed = enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py", "other/bar.py"])
    assert changed == ["manager/foo.py", "other/bar.py"]


# --- violation cannot reach a "successful completion" style callback -------

def test_violation_blocks_a_downstream_success_style_callback(repo):
    """Structural proof (Slice D's own narrow contract, before any real
    commit/push implementation exists): a caller that only invokes its
    completion/commit/push step after enforce_allowed_paths() returns
    cleanly can never reach that step on a violation."""
    (repo["path"] / "other" / "bar.py").write_text("changed\n", encoding="utf-8")
    completion_calls = []

    def fake_complete_and_push():
        completion_calls.append("called")

    with pytest.raises(AllowedPathsViolationError):
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
        fake_complete_and_push()  # unreachable if the line above raised

    assert completion_calls == []


def test_clean_execution_allows_the_downstream_success_style_callback(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    completion_calls = []

    def fake_complete_and_push():
        completion_calls.append("called")

    enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    fake_complete_and_push()

    assert completion_calls == ["called"]


# --- collect_changed_paths in isolation (mocked runner) --------------------

def test_collect_changed_paths_includes_rename_both_sides():
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        if "diff" in command:
            return subprocess.CompletedProcess(command, 0, "R100\told/path.py\tnew/path.py\nM\tmanager/x.py\n", "")
        return subprocess.CompletedProcess(command, 0, "untracked/new.py\n", "")

    changed = collect_changed_paths("/fake/dir", "a" * 40, runner=fake_runner)
    assert changed == ["manager/x.py", "new/path.py", "old/path.py", "untracked/new.py"]


def test_collect_changed_paths_retries_one_transient_git_read_failure():
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        if "diff" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if len([call for call in calls if "ls-files" in call]) == 1:
            return subprocess.CompletedProcess(command, 1, "", "transient")
        return subprocess.CompletedProcess(command, 0, "manager/recovered.py\n", "")

    with patch("manager.repo_write_enforcement.time.sleep") as sleep:
        changed = collect_changed_paths("/fake/dir", "a" * 40, runner=fake_runner)
    assert changed == ["manager/recovered.py"]
    sleep.assert_called_once_with(0.25)


def test_collect_changed_paths_still_fails_closed_after_retry():
    def fake_runner(command, **kwargs):
        if "diff" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "persistent")

    with patch("manager.repo_write_enforcement.time.sleep"), pytest.raises(TaskError, match="git ls-files failed: persistent"):
        collect_changed_paths("/fake/dir", "a" * 40, runner=fake_runner)


# --- OWNER_MARKER_FILENAME exclusion (fix/repo-write-owner-marker-exclusion-20260826) --
#
# manager.worktree_materializer.materialize_worktree() unconditionally
# writes OWNER_MARKER_FILENAME (".adm-worktree-owner.json") into every
# repo-write worktree it creates -- ADM's own internal ownership-tracking
# file, never something a provider wrote or a task's allowed_paths could
# ever have named. Before this fix it was collected as an untracked change
# like any other file and unconditionally rejected as an allowed_paths
# violation, so every real repo-write task failed regardless of what the
# provider actually touched.

def _write_owner_marker(repo_path):
    (repo_path / OWNER_MARKER_FILENAME).write_text('{"task_id": "t1"}\n', encoding="utf-8")


def test_worktree_with_only_owner_marker_passes(repo):
    """1: a worktree whose only change is the owner marker itself passes
    with an empty changed-paths result -- the marker is invisible to
    allowed_paths enforcement entirely, not merely "authorized"."""
    _write_owner_marker(repo["path"])
    changed = enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert changed == []


def test_allowed_real_file_plus_owner_marker_passes(repo):
    """2."""
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _write_owner_marker(repo["path"])
    changed = enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert changed == ["manager/foo.py"]


def test_unauthorized_real_file_plus_owner_marker_fails_on_real_file_only(repo):
    """3: the owner marker never appears in the violation list, even when a
    real violation is present alongside it."""
    (repo["path"] / "other" / "bar.py").write_text("changed\n", encoding="utf-8")
    _write_owner_marker(repo["path"])
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert exc.value.violations == ["other/bar.py"]


def test_arbitrary_hidden_file_still_fails_closed(repo):
    """4: the exclusion is exact-match only -- an unrelated hidden file is
    still collected and enforced normally."""
    (repo["path"] / ".evil.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert exc.value.violations == [".evil.json"]


def test_similar_named_files_still_fail_closed(repo):
    """5: a provider-created file that merely resembles the marker name
    (suffix or different content) is never silently exempted -- only the
    exact literal OWNER_MARKER_FILENAME path is excluded."""
    (repo["path"] / f"{OWNER_MARKER_FILENAME}.bak").write_text("{}\n", encoding="utf-8")
    (repo["path"] / ".adm-other.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["manager/foo.py"])
    assert exc.value.violations == [".adm-other.json", f"{OWNER_MARKER_FILENAME}.bak"]


def test_owner_marker_exclusion_is_root_relative_not_a_basename_match(repo):
    """A same-named file nested in a subdirectory is a different path from
    the root-level marker and must still be enforced -- this exclusion is
    an exact repo-relative path match, never a bare filename/basename
    match that could be exploited by nesting a same-named file elsewhere."""
    (repo["path"] / "manager" / OWNER_MARKER_FILENAME).write_text("{}\n", encoding="utf-8")
    with pytest.raises(AllowedPathsViolationError) as exc:
        enforce_allowed_paths(repo["path"], repo["baseline"], ["other/bar.py"])
    assert exc.value.violations == [f"manager/{OWNER_MARKER_FILENAME}"]


def test_collect_changed_paths_excludes_owner_marker_via_mocked_runner():
    """OWNER_MARKER_FILENAME is excluded even when it is the only untracked
    entry git reports, exercised in isolation like the rename test above."""
    def fake_runner(command, **kwargs):
        if "diff" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, f"{OWNER_MARKER_FILENAME}\nreal/file.py\n", "")

    changed = collect_changed_paths("/fake/dir", "a" * 40, runner=fake_runner)
    assert changed == ["real/file.py"]


# --- P0-A/P0-B: real commit history + independently-verified remote evidence -

@pytest.fixture
def repo_with_origin(repo):
    origin = repo["path"].parent / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    _git(repo["path"], "remote", "add", "origin", str(origin))
    return {**repo, "origin": origin}


def test_collect_commit_shas_empty_with_no_new_commits(repo):
    assert collect_commit_shas(repo["path"], repo["baseline"]) == []


def test_collect_commit_shas_oldest_first_ending_at_head(repo):
    (repo["path"] / "manager" / "foo.py").write_text("first\n", encoding="utf-8")
    _git(repo["path"], "commit", "-am", "first commit")
    first_sha = _git(repo["path"], "rev-parse", "HEAD")
    (repo["path"] / "manager" / "foo.py").write_text("second\n", encoding="utf-8")
    _git(repo["path"], "commit", "-am", "second commit")
    second_sha = _git(repo["path"], "rev-parse", "HEAD")

    assert collect_commit_shas(repo["path"], repo["baseline"]) == [first_sha, second_sha]
    assert current_head_sha(repo["path"]) == second_sha


def test_current_head_sha_matches_rev_parse(repo):
    assert current_head_sha(repo["path"]) == repo["baseline"]


def test_capture_repo_write_evidence_verified_push(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    final_sha = _git(repo_with_origin["path"], "rev-parse", "HEAD")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
    )

    assert evidence == {
        "files_changed": ["manager/foo.py"], "commits": [final_sha], "final_commit_sha": final_sha,
        "branch": "main", "worktree_path": str(repo_with_origin["path"]), "push_status": "verified",
        "remote_sha": final_sha, "tests": [], "tests_status": "not_required",
    }


def test_capture_repo_write_evidence_runs_validation_command_and_records_a_pass(repo_with_origin):
    """ADM independently runs the Task's own declared validation_command
    itself (never a provider self-report) and records the real command,
    exit code, and output."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    command = f'{sys.executable} -c "print(123); import sys; sys.exit(0)"'
    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        validation_command=command,
    )
    assert evidence["tests_status"] == "passed"
    assert len(evidence["tests"]) == 1
    result = evidence["tests"][0]
    assert result["command"] == command
    assert result["exit_code"] == 0
    assert "123" in result["output_summary"]
    assert result["timed_out"] is False
    assert result["started_at"] and result["completed_at"]


def test_capture_repo_write_evidence_never_fabricates_tests_when_not_supplied(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
    )
    assert evidence["tests"] == []


def test_capture_repo_write_evidence_fails_closed_when_not_pushed(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo, never pushed")

    with pytest.raises(TaskError):
        capture_repo_write_evidence(
            repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        )


def test_capture_repo_write_evidence_fails_closed_with_no_origin_configured(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo["path"], "commit", "-am", "edit foo, no origin at all")

    with pytest.raises(TaskError):
        capture_repo_write_evidence(repo["path"], repo["baseline"], "refs/heads/main", ["manager/foo.py"])


# --- P0-C: baseline-only branches, empty commits, and zero real changes -----

def test_capture_repo_write_evidence_fails_closed_on_baseline_only_branch(repo_with_origin):
    """A branch that was pushed but never actually committed to (HEAD still
    equals baseline_head) must never be accepted as a completed repo-write,
    regardless of what files_changed the caller passes in."""
    _git(repo_with_origin["path"], "push", "origin", "main")

    with pytest.raises(TaskError, match="no commits"):
        capture_repo_write_evidence(repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", [])


def test_capture_repo_write_evidence_fails_closed_on_final_sha_equal_to_baseline(repo_with_origin):
    """Direct final_commit_sha == baseline_head check, independent of the
    empty-commits check above -- exercised the same way (nothing committed)
    but asserting the more specific message."""
    _git(repo_with_origin["path"], "push", "origin", "main")

    with pytest.raises(TaskError):
        capture_repo_write_evidence(repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", [])


def test_capture_repo_write_evidence_fails_closed_on_zero_files_changed_despite_real_commits(repo_with_origin):
    """A commit and its exact revert leave real, non-empty commit history
    while the working tree ends up byte-identical to baseline_head --
    files_changed (independently computed by enforce_allowed_paths via git
    diff against baseline_head) correctly reports zero changed paths in
    that case, and this must still fail closed rather than accept commits
    alone as proof of real work."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("original\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "revert foo back to baseline content")
    _git(repo_with_origin["path"], "push", "origin", "main")

    with pytest.raises(TaskError, match="no changed files"):
        capture_repo_write_evidence(repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", [])


def test_capture_repo_write_evidence_fails_closed_on_empty_commit(repo_with_origin):
    """`git commit --allow-empty` (or an equivalent no-op commit) must never
    count as real progress, even when the caller-supplied files_changed is
    non-empty (e.g. from a separate uncommitted edit elsewhere)."""
    _git(repo_with_origin["path"], "commit", "--allow-empty", "-m", "empty commit, no tree change")
    _git(repo_with_origin["path"], "push", "origin", "main")

    with pytest.raises(TaskError, match="empty commit"):
        capture_repo_write_evidence(
            repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        )


def test_capture_repo_write_evidence_fails_closed_on_uncommitted_change(repo_with_origin):
    """A real, in-scope file edit that was never committed at all (working
    tree differs from baseline_head, but there is no commit history to show
    for it) must fail closed on the "no commits" gate -- files_changed being
    non-empty is not, by itself, proof anything was actually committed."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed but never committed\n", encoding="utf-8")

    with pytest.raises(TaskError, match="no commits"):
        capture_repo_write_evidence(
            repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        )


def test_capture_repo_write_evidence_tests_status_is_not_required_when_no_validation_command(repo_with_origin):
    """A Task that declared no validation_command is never gated on tests --
    tests_status must say so explicitly, not fall back to a value that
    could be misread as "tests were skipped/missing"."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
    )
    assert evidence["tests_status"] == "not_required"
    assert evidence["tests"] == []


def test_capture_repo_write_evidence_tests_status_is_failed_on_nonzero_exit(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    command = f'{sys.executable} -c "import sys; print(\'boom\'); sys.exit(1)"'
    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        validation_command=command,
    )
    assert evidence["tests_status"] == "failed"
    assert evidence["tests"][0]["exit_code"] == 1
    assert "boom" in evidence["tests"][0]["output_summary"]


def test_capture_repo_write_evidence_tests_status_is_failed_on_timeout(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    command = f'{sys.executable} -c "import time; time.sleep(5)"'
    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        validation_command=command, validation_timeout_seconds=1,
    )
    assert evidence["tests_status"] == "failed"
    assert evidence["tests"][0]["timed_out"] is True
    assert evidence["tests"][0]["exit_code"] is None


# --- Real P0 (2026-08-29): deterministic validation runtime ----------------
#
# Live-reproduced: a real repo-write execution's validation_command
# ("python -m pytest ...") genuinely ran -- real command, real exit_code,
# real output, exactly as this architecture is designed to produce -- but
# failed with ModuleNotFoundError because the bare "python" in the command
# resolved, via subprocess.run(shell=True)'s own ambient PATH lookup, to a
# DIFFERENT interpreter than the one the Watcher's own -PythonPath/
# -PythonDeps configuration was built for (an ABI mismatch: -PythonDeps'
# native extension was compiled for the pinned interpreter's Python version,
# not whatever "python" happened to be first on PATH). The enforcement
# itself was never dishonest; the ambient interpreter resolution was simply
# not deterministic. These tests cover the fix: a bare python/python3/py
# leading token in validation_command is resolved to sys.executable --
# already the single authoritative interpreter for this purpose, since the
# entire Watcher process tree (and everything it calls in-process,
# including this function) is itself launched via the pinned -PythonPath
# interpreter, with -PythonDeps exported as this same process's PYTHONPATH.

def test_resolve_validation_command_substitutes_bare_python_token():
    resolved, executable = _resolve_validation_command("python -m pytest -q")
    assert executable == sys.executable
    assert resolved == f'"{sys.executable}" -m pytest -q'


def test_resolve_validation_command_substitutes_python3_and_py_tokens():
    resolved3, exe3 = _resolve_validation_command("python3 script.py")
    assert exe3 == sys.executable
    assert resolved3 == f'"{sys.executable}" script.py'

    resolved_py, exe_py = _resolve_validation_command("py -3 script.py")
    assert exe_py == sys.executable
    assert resolved_py == f'"{sys.executable}" -3 script.py'


def test_resolve_validation_command_leaves_explicit_interpreter_path_unchanged():
    """A command that already names an explicit interpreter (e.g. a Task
    author who deliberately wants a project virtualenv's own python) must
    never be rewritten -- only a genuinely bare, ambiguous leading token is."""
    explicit = f"{sys.executable} -m pytest -q"
    resolved, executable = _resolve_validation_command(explicit)
    assert resolved == explicit
    assert executable is None


def test_resolve_validation_command_leaves_non_python_commands_unchanged():
    """Node/npm/arbitrary shell commands are untouched -- no existing
    authoritative resolver for other ecosystems exists in this codebase to
    reuse, so this deliberately does not invent one."""
    for command in ("npm test", "node --test", "pytest -q", "./run-checks.sh"):
        resolved, executable = _resolve_validation_command(command)
        assert resolved == command
        assert executable is None


def test_capture_repo_write_evidence_resolves_bare_python_and_records_executable(repo_with_origin):
    """End-to-end: a bare "python" validation_command (no absolute path --
    exactly the real-world shape a Task author would naturally write) is
    the ORIGINAL string in the persisted evidence's "command" field (what
    was actually declared), but ran against sys.executable, recorded
    separately in "executable" -- deterministic and auditable, not silently
    dependent on whatever the spawned shell's ambient PATH resolved."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    bare_command = "python -c \"print(123)\""
    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        validation_command=bare_command,
    )
    assert evidence["tests_status"] == "passed"
    result = evidence["tests"][0]
    assert result["command"] == bare_command
    assert result["executable"] == sys.executable
    assert result["exit_code"] == 0
    assert "123" in result["output_summary"]


def test_capture_repo_write_evidence_fails_closed_on_missing_interpreter(repo_with_origin, monkeypatch):
    """A configured interpreter that cannot actually be launched (wrong or
    missing) must fail closed -- exit_code=None, tests_status="failed" --
    never silently fall through to a completed status."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    monkeypatch.setattr(sys, "executable", str(repo_with_origin["path"] / "no-such-interpreter.exe"))
    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        validation_command="python -c \"print(1)\"",
    )
    # The exact exit_code the shell reports for "command not found" varies
    # (cmd.exe returns nonzero rather than raising) -- what must hold
    # regardless is that this never reads as success.
    assert evidence["tests_status"] == "failed"
    assert evidence["tests"][0]["exit_code"] != 0


# --- Bootstrap architecture: ADM host commit/push authority ----------------
#
# The provider (Codex, sandbox="workspace-write") only edits files and runs
# local checks in its isolated worktree; it never runs `git commit`/`git
# push` itself. commit_and_push_repo_write_changes() is the ADM host's own
# git authority, exercised after enforce_allowed_paths() has already proven
# the changed paths are real and in-scope.

def test_commit_and_push_stages_only_admitted_paths_and_pushes_feature_branch(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("host committed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "checkout", "-b", "feature/adm-host-commit")

    final_sha = commit_and_push_repo_write_changes(
        repo_with_origin["path"], "refs/heads/feature/adm-host-commit", ["manager/foo.py"],
        "codex repo_write: t1/exec-a",
    )

    assert final_sha == _git(repo_with_origin["path"], "rev-parse", "HEAD")
    assert final_sha != repo_with_origin["baseline"]
    log = _git(repo_with_origin["path"], "log", "-1", "--format=%s")
    assert log == "codex repo_write: t1/exec-a"
    remote_sha = _git(repo_with_origin["path"], "ls-remote", "origin", "refs/heads/feature/adm-host-commit").split()[0]
    assert remote_sha == final_sha


def test_commit_and_push_never_stages_unadmitted_paths(repo_with_origin):
    """Only the exact admitted files_changed list is ever staged -- never
    `git add .` -- so an unrelated dirty file in the worktree is neither
    committed nor pushed."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("admitted change\n", encoding="utf-8")
    (repo_with_origin["path"] / "other" / "bar.py").write_text("unrelated dirty file\n", encoding="utf-8")

    commit_and_push_repo_write_changes(
        repo_with_origin["path"], "refs/heads/main", ["manager/foo.py"], "codex repo_write: t1/exec-a",
    )

    status = _git(repo_with_origin["path"], "status", "--porcelain", "--", "other/bar.py")
    assert status == "M other/bar.py"


def test_commit_and_push_requires_non_empty_files_changed():
    with pytest.raises(TaskError):
        commit_and_push_repo_write_changes("/fake/dir", "refs/heads/main", [], "message")


def test_commit_and_push_fails_closed_on_git_add_failure():
    def fake_runner(command, **kwargs):
        if "add" in command:
            return subprocess.CompletedProcess(command, 128, "", "fatal: pathspec did not match any files")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(TaskError):
        commit_and_push_repo_write_changes("/fake/dir", "refs/heads/main", ["manager/missing.py"], "message",
                                           runner=fake_runner)


def test_commit_and_push_fails_closed_on_git_commit_failure():
    def fake_runner(command, **kwargs):
        if "commit" in command:
            return subprocess.CompletedProcess(command, 1, "", "nothing to commit")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(TaskError):
        commit_and_push_repo_write_changes("/fake/dir", "refs/heads/main", ["manager/foo.py"], "message",
                                           runner=fake_runner)


def test_commit_and_push_fails_closed_on_git_push_failure(repo):
    (repo["path"] / "manager" / "foo.py").write_text("no origin configured\n", encoding="utf-8")

    with pytest.raises(TaskError):
        commit_and_push_repo_write_changes(repo["path"], "refs/heads/main", ["manager/foo.py"], "message")


# --- Validation runtime home isolation (P0 HANDS_OFF_VALIDATION_TRUTH) ------
#
# Rule44 fresh E2E-A (2026-09-03) failed after a real edit, a real commit
# 49fa1e7, a real push and a matching remote read-back, because the host's
# own validation step inherited the Command Watcher's production
# AI_MANAGER_HOME and the root conftest.py correctly refused to run the
# suite there (pytest exit 4). These tests pin the invariant that closes
# it: every validation subprocess is handed an explicit, execution-specific,
# disposable manager home, and never inherits the parent's.


def _normalized(path):
    """Windows-correct path comparison (the production home and an
    inherited value can differ only by case)."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _capture_validation_env(working_directory, command='python -c "pass"', exit_code=0):
    """Run _run_validation_command against a fake runner that records the
    exact keyword arguments it was handed, so a test can assert on the
    environment the child process would really have received."""
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["command"] = cmd
        captured["kwargs"] = kwargs
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, exit_code, "", "")

    evidence = _run_validation_command(working_directory, command, runner=fake_runner)
    return evidence, captured


# A. Production inheritance attack -------------------------------------------

def test_validation_subprocess_never_inherits_a_production_manager_home(tmp_path, monkeypatch):
    """The exact Rule44-A shape: the parent (the Command Watcher) carries
    AI_MANAGER_HOME pointing at the live production manager home. The child
    must not receive it."""
    production = tmp_path / "profile" / ".ai-development-manager"
    monkeypatch.setenv(MANAGER_HOME_ENV_VAR, str(production))

    evidence, captured = _capture_validation_env(tmp_path)

    assert captured["env"] is not None, "no env supplied; the child inherits the parent environment"
    child_home = captured["env"][MANAGER_HOME_ENV_VAR]
    assert _normalized(child_home) != _normalized(production)
    assert os.path.isabs(child_home)
    assert VALIDATION_MANAGER_HOME_PREFIX in child_home
    assert evidence["manager_home"] == child_home


def test_validation_subprocess_manager_home_is_never_inside_a_git_work_tree(tmp_path, monkeypatch):
    """manager.manager_home's own invariant, enforced here too: durable
    runtime state must never be written into a checkout."""
    monkeypatch.setenv(MANAGER_HOME_ENV_VAR, str(tmp_path / ".ai-development-manager"))
    _, captured = _capture_validation_env(tmp_path)
    home = Path(captured["env"][MANAGER_HOME_ENV_VAR])
    for directory in (home, *home.parents):
        assert not (directory / ".git").exists(), f"manager home {home} is inside checkout {directory}"


def test_validation_subprocess_manager_home_is_not_the_worktree_or_cwd(tmp_path):
    """Never the repo/worktree under validation, and never a cwd-relative
    fallback -- both are the 2026-09-02 contamination outage's shape."""
    _, captured = _capture_validation_env(tmp_path)
    home = captured["env"][MANAGER_HOME_ENV_VAR]
    assert _normalized(home) != _normalized(tmp_path)
    assert _normalized(home) != _normalized(os.getcwd())
    assert not _normalized(home).startswith(_normalized(tmp_path) + os.sep)


# B. Missing parent env -------------------------------------------------------

def test_validation_subprocess_gets_explicit_home_when_parent_has_none(tmp_path, monkeypatch):
    """With AI_MANAGER_HOME unset the child must still be handed an
    explicit isolated home -- never left to resolve_manager_home's
    canonical ~/.ai-development-manager fallback, which is production."""
    monkeypatch.delenv(MANAGER_HOME_ENV_VAR, raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("HOME", str(tmp_path / "profile"))

    _, captured = _capture_validation_env(tmp_path)

    child_home = captured["env"][MANAGER_HOME_ENV_VAR]
    assert child_home and child_home.strip()
    assert os.path.isabs(child_home)
    canonical = tmp_path / "profile" / ".ai-development-manager"
    assert _normalized(child_home) != _normalized(canonical)


# C. Blank / whitespace parent env -------------------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_validation_subprocess_ignores_a_blank_parent_manager_home(tmp_path, monkeypatch, blank):
    """A blank AI_MANAGER_HOME is treated as unset by resolve_manager_home,
    which then falls back to the real canonical home. The child must get a
    real isolated path instead of inheriting the blank value."""
    monkeypatch.setenv(MANAGER_HOME_ENV_VAR, blank)
    _, captured = _capture_validation_env(tmp_path)
    child_home = captured["env"][MANAGER_HOME_ENV_VAR]
    assert child_home.strip(), "blank manager home leaked to the child"
    assert os.path.isabs(child_home)
    assert VALIDATION_MANAGER_HOME_PREFIX in child_home


# D. The rest of the parent environment is preserved --------------------------

def test_validation_environment_preserves_the_rest_of_the_parent_environment(tmp_path, monkeypatch):
    """Only AI_MANAGER_HOME is narrowed. PYTHONPATH in particular is how
    the Watcher's pinned interpreter finds its dependencies."""
    monkeypatch.setenv("PYTHONPATH", "C:\\adm-deps")
    monkeypatch.setenv("ADM_UNRELATED_MARKER", "preserved")
    _, captured = _capture_validation_env(tmp_path)
    env = captured["env"]
    assert env["PYTHONPATH"] == "C:\\adm-deps"
    assert env["ADM_UNRELATED_MARKER"] == "preserved"


def test_validation_never_mutates_the_hosts_own_environment(tmp_path, monkeypatch):
    """This runs inside the long-lived Command Watcher, whose own
    AI_MANAGER_HOME must keep pointing at the real production home."""
    host_home = str(tmp_path / "host-home")
    monkeypatch.setenv(MANAGER_HOME_ENV_VAR, host_home)
    _capture_validation_env(tmp_path)
    assert os.environ[MANAGER_HOME_ENV_VAR] == host_home


# E. Execution isolation ------------------------------------------------------

def test_two_validation_runs_never_share_one_disposable_manager_home(tmp_path):
    """Two independent executions validating concurrently must not write
    durable runtime state into the same home."""
    first, _ = _capture_validation_env(tmp_path)
    second, _ = _capture_validation_env(tmp_path)
    assert first["manager_home"] and second["manager_home"]
    assert first["manager_home"] != second["manager_home"]


def test_disposable_manager_home_is_cleaned_up_after_the_run(tmp_path):
    """Bounded lifetime: one validation run, one home. The Watcher runs
    this repeatedly and must not leak a directory per execution."""
    evidence, _ = _capture_validation_env(tmp_path)
    assert not Path(evidence["manager_home"]).exists()


# F. Evidence truth -----------------------------------------------------------

def test_recorded_manager_home_is_what_the_real_child_process_actually_saw(tmp_path):
    """End-to-end against a REAL subprocess (no fake runner): the recorded
    manager_home must be the value the child genuinely received, not a
    separately-computed guess. Also proves the real Windows path behaviour
    of handing an absolute temp path through cmd.exe."""
    probe = tmp_path / "probe.py"
    probe.write_text("import os\nprint(os.environ['AI_MANAGER_HOME'])\n", encoding="utf-8")

    evidence = _run_validation_command(tmp_path, f'python "{probe}"')

    assert evidence["exit_code"] == 0, evidence["output_summary"]
    child_reported = evidence["output_summary"].strip().splitlines()[-1].strip()
    assert _normalized(child_reported) == _normalized(evidence["manager_home"])


def test_real_child_process_does_not_receive_the_parents_production_home(tmp_path, monkeypatch):
    """The baseline reproduction, inverted: a fake USERPROFILE makes
    <profile>/.ai-development-manager the canonical production home for
    this process; at base the child inherited exactly that string and
    pytest exited 4. It must now receive an isolated home instead."""
    profile = tmp_path / "profile"
    profile.mkdir()
    production = profile / ".ai-development-manager"
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv(MANAGER_HOME_ENV_VAR, str(production))

    probe = tmp_path / "probe.py"
    probe.write_text("import os\nprint(os.environ['AI_MANAGER_HOME'])\n", encoding="utf-8")
    evidence = _run_validation_command(tmp_path, f'python "{probe}"')

    assert evidence["exit_code"] == 0, evidence["output_summary"]
    child_reported = evidence["output_summary"].strip().splitlines()[-1].strip()
    assert _normalized(child_reported) != _normalized(production)
    assert _normalized(child_reported) == _normalized(evidence["manager_home"])


def test_validation_fails_closed_when_no_isolated_manager_home_can_be_established(tmp_path):
    """A manager home that cannot be established safely must fail the
    validation closed -- never fall through to a completed status, and
    never silently run against whatever the parent carried."""
    def exploding_home():
        raise ManagerHomeError("MANAGER_HOME_IN_CHECKOUT: simulated")

    def fake_runner(cmd, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("validation ran without a safe isolated manager home")

    with patch("manager.repo_write_enforcement._isolated_validation_manager_home", exploding_home):
        evidence = _run_validation_command(tmp_path, 'python -c "pass"', runner=fake_runner)

    assert evidence["exit_code"] is None
    assert evidence["timed_out"] is False
    assert evidence["manager_home"] is None
    assert "isolated manager home" in evidence["output_summary"]


def test_manager_home_is_recorded_in_persisted_repo_write_evidence(repo_with_origin):
    """The evidence a Task actually persists carries the manager home the
    validation really ran against."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        validation_command='python -c "print(123)"',
    )

    assert evidence["tests_status"] == "passed"
    recorded = evidence["tests"][0]["manager_home"]
    assert recorded and os.path.isabs(recorded)
    assert VALIDATION_MANAGER_HOME_PREFIX in recorded


def test_execution_schema_accepts_evidence_with_and_without_manager_home():
    """The new field is optional: records persisted before this fix (no
    manager_home) must still validate against the schema."""
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "execution.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    tests_schema = schema["properties"]["repo_write_evidence"]["oneOf"][2]["properties"]["tests"]

    legacy = [{"command": "pytest", "exit_code": 0, "output_summary": "ok",
               "started_at": "2026-09-03T12:39:00Z", "completed_at": "2026-09-03T12:40:00Z"}]
    current = [dict(legacy[0], manager_home="C:\\Temp\\adm-validation-home-xyz")]

    jsonschema.validate(legacy, tests_schema)
    jsonschema.validate(current, tests_schema)


def test_cleanup_never_deletes_anything_it_did_not_create(tmp_path):
    """Regression for a real defect found by mutation testing on
    2026-09-10: the first revision of this fix removed whatever path the
    home helper returned, so a helper that returned the worktree under
    validation deleted the entire checkout. Cleanup is now keyed on the
    literal mkdtemp result and guarded by prefix + parent directory."""
    precious = tmp_path / "checkout"
    (precious / "manager").mkdir(parents=True)
    (precious / "manager" / "keep.py").write_text("do not delete me\n", encoding="utf-8")

    def bad_home():
        return str(precious), str(precious)

    def fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("manager.repo_write_enforcement._isolated_validation_manager_home", bad_home):
        _run_validation_command(tmp_path, 'python -c "pass"', runner=fake_runner)

    assert precious.exists(), "cleanup deleted a directory it did not create"
    assert (precious / "manager" / "keep.py").read_text(encoding="utf-8") == "do not delete me\n"


def test_cleanup_removes_only_prefixed_directories_in_the_temp_root(tmp_path):
    """The guard itself, exercised directly in both directions."""
    outside = tmp_path / (VALIDATION_MANAGER_HOME_PREFIX + "decoy")
    outside.mkdir()
    _remove_validation_manager_home(str(outside))
    assert outside.exists(), "removed a prefixed directory outside the temp root"

    real = tempfile.mkdtemp(prefix=VALIDATION_MANAGER_HOME_PREFIX)
    _remove_validation_manager_home(real)
    assert not Path(real).exists(), "failed to remove its own temp directory"
