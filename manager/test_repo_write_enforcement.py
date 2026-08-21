#!/usr/bin/env python3
"""Tests for runtime allowed_paths enforcement against real git state (Slice D)."""

import subprocess

import pytest

from manager.repo_write_enforcement import (
    AllowedPathsViolationError,
    collect_changed_paths,
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
