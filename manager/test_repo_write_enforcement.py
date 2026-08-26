#!/usr/bin/env python3
"""Tests for runtime allowed_paths enforcement against real git state (Slice D)."""

import subprocess

import pytest

from manager.repo_write_enforcement import (
    AllowedPathsViolationError,
    OWNER_MARKER_FILENAME,
    capture_repo_write_evidence,
    collect_changed_paths,
    collect_commit_shas,
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
        "remote_sha": final_sha, "tests": [], "test_evidence_status": "unknown",
    }


def test_capture_repo_write_evidence_preserves_supplied_test_evidence(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        test_evidence=["pytest manager/test_foo.py::test_x PASSED"],
    )
    assert evidence["tests"] == ["pytest manager/test_foo.py::test_x PASSED"]


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


# --- P0-1: fail-closed terminal success evidence (real change required) -----

def test_capture_repo_write_evidence_fails_closed_on_baseline_only_push(repo_with_origin):
    """Scenario A: provider pushes its feature branch, but it still points at
    baseline_head -- nothing was ever committed. A remote readback alone
    would call this "verified" (the pushed branch really does match local
    HEAD); this must fail closed because HEAD never advanced past baseline
    and there is no commit history at all."""
    _git(repo_with_origin["path"], "push", "origin", "main")

    with pytest.raises(TaskError) as exc:
        capture_repo_write_evidence(repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", [])
    assert "refusing to record terminal success evidence" in str(exc.value).lower()


def test_capture_repo_write_evidence_fails_closed_on_empty_commit_zero_files(repo_with_origin):
    """Scenario B: provider makes and pushes an empty commit -- HEAD advances
    and commit history is non-empty, but zero files actually changed. Must
    still fail closed: a real change is required, not just a new commit
    SHA."""
    _git(repo_with_origin["path"], "commit", "--allow-empty", "-m", "empty commit, no real change")
    _git(repo_with_origin["path"], "push", "origin", "main")

    with pytest.raises(TaskError) as exc:
        capture_repo_write_evidence(repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", [])
    assert "files_changed" in str(exc.value).lower() or "changed file" in str(exc.value).lower()


def test_capture_repo_write_evidence_fails_closed_on_uncommitted_change(repo_with_origin):
    """Scenario C: provider modifies an allowed file but never commits it.
    The change is real and visible (files_changed is non-empty), and the
    branch may even have been pushed -- but since nothing was committed,
    HEAD is still baseline_head and commit history is empty. Must fail
    closed rather than accept an uncommitted working-tree edit as terminal
    success evidence."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed but never committed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "push", "origin", "main")

    with pytest.raises(TaskError) as exc:
        capture_repo_write_evidence(
            repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        )
    assert "commit" in str(exc.value).lower() or "baseline" in str(exc.value).lower()


def test_capture_repo_write_evidence_real_commit_and_push_still_passes(repo_with_origin):
    """Scenario D: a real, in-scope committed and pushed change must still
    pass -- the new fail-closed checks must not regress the legitimate
    success path."""
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("real change\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "real committed change")
    final_sha = _git(repo_with_origin["path"], "rev-parse", "HEAD")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
    )
    assert evidence["files_changed"] == ["manager/foo.py"]
    assert evidence["commits"] == [final_sha]
    assert evidence["final_commit_sha"] == final_sha
    assert evidence["final_commit_sha"] != repo_with_origin["baseline"]
    assert evidence["remote_sha"] == final_sha


# --- P0-2: test evidence truth (unknown vs. reported, never fabricated) ----

def test_capture_repo_write_evidence_test_status_unknown_when_not_supplied(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
    )
    assert evidence["tests"] == []
    assert evidence["test_evidence_status"] == "unknown"


def test_capture_repo_write_evidence_test_status_reported_when_supplied(repo_with_origin):
    (repo_with_origin["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _git(repo_with_origin["path"], "commit", "-am", "edit foo")
    _git(repo_with_origin["path"], "push", "origin", "main")

    evidence = capture_repo_write_evidence(
        repo_with_origin["path"], repo_with_origin["baseline"], "refs/heads/main", ["manager/foo.py"],
        test_evidence=["pytest manager/test_foo.py::test_x PASSED"],
    )
    assert evidence["tests"] == ["pytest manager/test_foo.py::test_x PASSED"]
    assert evidence["test_evidence_status"] == "reported"
