#!/usr/bin/env python3
"""Tests for D2 terminal completion (tests gate -> exact stage -> commit ->
push -> remote SHA readback) against real git state and a real bare remote."""

import subprocess
import sys

import pytest

from manager.repo_write_completion import (
    CommitLineageMismatchError,
    CommitStageMismatchError,
    EmptyChangesError,
    PushVerificationError,
    TestsGateFailedError,
    complete_repo_write_execution,
    push_and_verify,
    run_tests_gate,
    run_validation_gate,
    stage_and_commit,
)
from manager.tasks import TaskError


PASS_COMMAND = [sys.executable, "-c", "pass"]
FAIL_COMMAND = [sys.executable, "-c", "import sys; sys.exit(1)"]
BRANCH = "refs/heads/feat/p1/t1"
REPOSITORY = "github:example/project"
VALIDATION_CHECKS = [{"id": "tests", "command": PASS_COMMAND}]


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
    (root / "manager" / "foo.py").write_text("original\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    baseline = _git(root, "rev-parse", "HEAD")

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "origin", "main")

    return {"path": root, "origin": origin, "baseline": baseline}


def _complete(repo, **overrides):
    kwargs = dict(
        working_directory=repo["path"], changed_paths=["manager/foo.py"], baseline_head=repo["baseline"],
        branch=BRANCH, repository=REPOSITORY, validation_checks=VALIDATION_CHECKS, task_id="t1", execution_id="e1",
    )
    kwargs.update(overrides)
    return complete_repo_write_execution(**kwargs)


# --- 1. happy path: exact stage -> commit -> push -> remote readback -------

def test_happy_path_stages_commits_pushes_and_reads_back_remote(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    evidence = _complete(repo)

    assert evidence["commit_created"] is True
    assert evidence["commit_sha"] == evidence["remote_sha"]
    assert evidence["changed_paths"] == ["manager/foo.py"]
    assert evidence["branch"] == BRANCH
    assert evidence["repository"] == REPOSITORY
    assert evidence["baseline_head"] == repo["baseline"]
    assert all(check["passed"] for check in evidence["tests"])
    assert evidence["commit_identity"] == {"task_id": "t1", "execution_id": "e1", "branch": BRANCH}

    remote_line = _git(repo["path"], "ls-remote", "origin", BRANCH)
    assert remote_line.split("\t")[0] == evidence["commit_sha"]
    # main (the branch this worktree happened to be checked out on) is
    # untouched by this push -- only the named task branch ref moved.
    assert _git(repo["path"], "ls-remote", "origin", "refs/heads/main").split("\t")[0] == repo["baseline"]


# --- 3. tests fail -> no commit/push ----------------------------------------

def test_tests_gate_failure_blocks_commit_and_push(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(TestsGateFailedError):
        _complete(repo, validation_checks=[{"id": "tests", "command": FAIL_COMMAND}])

    assert _git(repo["path"], "rev-parse", "HEAD") == repo["baseline"]
    assert _git(repo["path"], "status", "--porcelain") != ""
    assert _git(repo["path"], "ls-remote", "origin", BRANCH) == ""


# --- 2 (module-level analogue). nothing to stage is rejected up front ------

def test_empty_changed_paths_rejected_before_any_git_mutation(repo):
    with pytest.raises(TaskError):
        _complete(repo, changed_paths=[])
    assert _git(repo["path"], "rev-parse", "HEAD") == repo["baseline"]


# --- 4. push fail -> not completed (retry then succeeds) -------------------

def test_push_failure_leaves_local_commit_but_nothing_on_remote(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")

    def failing_runner(cmd, **kwargs):
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "simulated network failure")
        return subprocess.run(cmd, **kwargs)

    with pytest.raises(PushVerificationError):
        _complete(repo, runner=failing_runner)

    assert _git(repo["path"], "rev-parse", "HEAD") != repo["baseline"]  # committed locally
    assert _git(repo["path"], "ls-remote", "origin", BRANCH) == ""  # never reached remote

    # A genuine retry (real runner this time) resumes from the real state
    # left behind: reuses the existing local commit rather than creating a
    # second one, then completes the push.
    retried = _complete(repo)
    assert retried["commit_created"] is False
    assert _git(repo["path"], "ls-remote", "origin", BRANCH).split("\t")[0] == retried["commit_sha"]


# --- 5. remote SHA mismatch -> not completed --------------------------------

def test_remote_sha_mismatch_after_push_is_rejected(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")

    def spoofing_readback_runner(cmd, **kwargs):
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"{'0' * 40}\t{BRANCH}\n", "")
        return subprocess.run(cmd, **kwargs)

    with pytest.raises(PushVerificationError):
        _complete(repo, runner=spoofing_readback_runner)
    # The real push actually landed (only readback was spoofed); a plain
    # git ls-remote (no injected runner) proves the true remote tip.
    real_readback = _git(repo["path"], "ls-remote", "origin", BRANCH)
    assert real_readback.split("\t")[0] != "0" * 40


# --- 6. retry after successful completion -> no duplicate commit -----------

def test_retry_after_success_creates_no_duplicate_commit(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    first = _complete(repo)
    count_before = _git(repo["path"], "rev-list", "--count", "HEAD")

    second = _complete(repo)
    count_after = _git(repo["path"], "rev-list", "--count", "HEAD")

    assert second["commit_created"] is False
    assert second["commit_sha"] == first["commit_sha"]
    assert second["remote_sha"] == first["remote_sha"]
    assert count_before == count_after


# --- 7. unrelated dirty file in a sibling worktree untouched ---------------

def test_unrelated_dirty_file_in_a_sibling_worktree_is_untouched(repo, tmp_path):
    sibling = tmp_path / "sibling-worktree"
    _git(repo["path"], "worktree", "add", "-b", "sibling-branch", str(sibling), repo["baseline"])
    (sibling / "manager" / "unrelated.py").write_text("dirty and unrelated\n", encoding="utf-8")
    status_before = _git(sibling, "status", "--porcelain")

    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    _complete(repo)

    status_after = _git(sibling, "status", "--porcelain")
    assert status_before == status_after
    assert "unrelated.py" in status_after


# --- 8. only admitted files in the commit -----------------------------------

def test_only_admitted_changed_paths_enter_the_commit(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    (repo["path"] / "manager" / "extra_untracked.py").write_text("scratch\n", encoding="utf-8")

    evidence = _complete(repo)

    committed_files = _git(repo["path"], "diff-tree", "--no-commit-id", "--name-only", "-r", evidence["commit_sha"]).splitlines()
    assert committed_files == ["manager/foo.py"]
    status_after = _git(repo["path"], "status", "--porcelain")
    assert "extra_untracked.py" in status_after  # left untouched, never staged or committed


# --- protected branch guard --------------------------------------------------

def test_push_refuses_protected_main_branch(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(TaskError, match="protected"):
        _complete(repo, branch="refs/heads/main")
    assert _git(repo["path"], "ls-remote", "origin", "refs/heads/main").split("\t")[0] == repo["baseline"]


# --- unit-level coverage of the individual steps ----------------------------

def test_run_tests_gate_rejects_empty_command(repo):
    with pytest.raises(TaskError):
        run_tests_gate(repo["path"], [])


def test_stage_and_commit_rejects_empty_changed_paths(repo):
    with pytest.raises(TaskError):
        stage_and_commit(repo["path"], [], "t1", "e1", BRANCH, repo["baseline"])


def test_stage_and_commit_mismatch_resets_index(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    (repo["path"] / "manager" / "other.py").write_text("also changed\n", encoding="utf-8")

    def over_staging_runner(cmd, **kwargs):
        result = subprocess.run(cmd, **kwargs)
        if cmd[3:5] == ["add", "--"]:
            subprocess.run(["git", "-C", str(repo["path"]), "add", "manager/other.py"], **kwargs)
        return result

    with pytest.raises(CommitStageMismatchError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"], runner=over_staging_runner)
    assert _git(repo["path"], "diff", "--cached", "--name-only") == ""


def test_deterministic_commit_message_contains_identity(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    stage_and_commit(repo["path"], ["manager/foo.py"], "task-42", "exec-99", BRANCH, repo["baseline"])
    message = _git(repo["path"], "log", "-1", "--format=%B")
    assert "task-42" in message and "exec-99" in message and "manager/foo.py" in message


def test_push_and_verify_no_op_when_remote_already_up_to_date(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    commit = stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])
    first_sha = push_and_verify(repo["path"], BRANCH, commit["commit_sha"])
    second_sha = push_and_verify(repo["path"], BRANCH, commit["commit_sha"])
    assert first_sha == second_sha == commit["commit_sha"]


# --- P0-A: clean-worktree fake completion can never be inferred from ------
# --- clean/dirty status alone -- it must be proven from real git lineage --

def test_clean_worktree_with_head_equal_to_baseline_is_empty_changes_not_success(repo):
    # No edit was ever made: the worktree is clean and HEAD is still exactly
    # baseline_head. The old logic treated "clean" alone as proof of a
    # completed repo-write; this must terminalize as a genuine no-op.
    with pytest.raises(EmptyChangesError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])


def test_clean_worktree_with_same_task_branch_wrong_execution_is_rejected(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    first = stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])
    assert first["created"] is True

    with pytest.raises(CommitLineageMismatchError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e2", BRANCH, repo["baseline"])


def test_direct_retry_reuses_only_the_authorized_predecessor_commit(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    first = stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])
    retry = stage_and_commit(
        repo["path"], ["manager/foo.py"], "t1", "e2", BRANCH, repo["baseline"],
        authorized_predecessor_execution_ids={"e1"},
    )
    assert retry["created"] is False
    assert retry["commit_sha"] == first["commit_sha"]


def test_dirty_worktree_whose_head_already_diverged_from_baseline_fails_closed(repo):
    # Something else committed on top of baseline_head (simulated here by
    # committing directly) before this call ever ran; the worktree is also
    # left dirty with an in-scope edit on top of that unexpected commit.
    # Building a completion commit on unproven lineage must be refused.
    _git(repo["path"], "commit", "--allow-empty", "-m", "unexpected commit")
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(CommitLineageMismatchError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])


def test_clean_worktree_with_an_unrelated_commit_is_rejected(repo):
    # HEAD advanced past baseline_head, but via a commit that carries none of
    # this module's own completion-commit identity -- an unrelated commit
    # must never be silently accepted as this execution's completion.
    _git(repo["path"], "commit", "--allow-empty", "-m", "totally unrelated commit")
    with pytest.raises(CommitLineageMismatchError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])


def test_clean_worktree_with_wrong_task_identity_is_rejected(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    stage_and_commit(repo["path"], ["manager/foo.py"], "other-task", "e1", BRANCH, repo["baseline"])
    with pytest.raises(CommitLineageMismatchError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])


def test_clean_worktree_with_wrong_branch_identity_is_rejected(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", "refs/heads/feat/p1/other-task", repo["baseline"])
    with pytest.raises(CommitLineageMismatchError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])


def test_reused_prior_task_commit_from_a_different_task_is_rejected(repo):
    # A different task's already-completed commit sits at HEAD (e.g. from a
    # shared/contaminated worktree); this task's own baseline_head is the
    # same starting point, so worktree-clean-with-advanced-HEAD alone must
    # not be treated as *this* task's own completion.
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    stage_and_commit(repo["path"], ["manager/foo.py"], "t2-prior-task", "e-prior", BRANCH, repo["baseline"])
    with pytest.raises(CommitLineageMismatchError):
        stage_and_commit(repo["path"], ["manager/foo.py"], "t1", "e1", BRANCH, repo["baseline"])


def test_lineage_identity_persists_onto_completion_evidence(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    evidence = _complete(repo)
    assert evidence["commit_identity"] == {"task_id": "t1", "execution_id": "e1", "branch": BRANCH}

    # A genuine retry with a fresh execution_id reuses the same commit and
    # the evidence still correctly attributes it to the originating
    # execution_id "e1", never falsely restamped as newly created by "e2".
    reused = complete_repo_write_execution(
        working_directory=repo["path"], changed_paths=["manager/foo.py"], baseline_head=repo["baseline"],
        branch=BRANCH, repository=REPOSITORY, validation_checks=VALIDATION_CHECKS, task_id="t1", execution_id="e2",
        authorized_predecessor_execution_ids={"e1"},
    )
    assert reused["commit_created"] is False
    assert reused["commit_identity"] == {"task_id": "t1", "execution_id": "e1", "branch": BRANCH}


def test_retry_reconciliation_never_converts_a_prior_tests_failure_into_fake_success(repo):
    (repo["path"] / "manager" / "foo.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(TestsGateFailedError):
        _complete(repo, validation_checks=[{"id": "tests", "command": FAIL_COMMAND}])
    # The failed attempt committed nothing and pushed nothing.
    assert _git(repo["path"], "rev-parse", "HEAD") == repo["baseline"]
    assert _git(repo["path"], "ls-remote", "origin", BRANCH) == ""

    # A genuine retry must run the real validation gate again -- it can
    # never reconcile the prior failure into a fake success just because a
    # later attempt is made.
    retried = _complete(repo)
    assert retried["commit_created"] is True
    assert all(check["passed"] for check in retried["tests"])


def test_run_validation_gate_enforces_every_required_check_in_order(repo):
    evidence = run_validation_gate(repo["path"], [
        {"id": "first", "command": PASS_COMMAND}, {"id": "second", "command": PASS_COMMAND},
    ])
    assert [item["id"] for item in evidence] == ["first", "second"]
    assert all(item["passed"] for item in evidence)


def test_run_validation_gate_fails_on_first_failing_required_check(repo):
    with pytest.raises(TestsGateFailedError) as exc_info:
        run_validation_gate(repo["path"], [
            {"id": "first", "command": PASS_COMMAND}, {"id": "second", "command": FAIL_COMMAND},
        ])
    assert exc_info.value.evidence["id"] == "second"


def test_run_validation_gate_rejects_empty_check_list(repo):
    with pytest.raises(TaskError):
        run_validation_gate(repo["path"], [])
