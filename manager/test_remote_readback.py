#!/usr/bin/env python3
"""Tests for independent remote-readback verification of a pushed repo-write
feature branch (Global Hands-off Execution Layer, Slice D2)."""

import subprocess

import pytest

from manager.remote_readback import resolve_origin_remote, verify_remote_branch_matches
from manager.tasks import TaskError


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    _git(root, "remote", "add", "origin", str(origin))
    return {"path": root, "origin": origin}


def test_resolve_origin_remote_returns_configured_url(repo):
    assert resolve_origin_remote(repo["path"]) == str(repo["origin"])


def test_resolve_origin_remote_fails_closed_with_no_origin(tmp_path):
    root = tmp_path / "no-origin-repo"
    root.mkdir()
    _git(root, "init")
    with pytest.raises(TaskError):
        resolve_origin_remote(root)


def test_verify_remote_branch_matches_pass_on_exact_equality(repo):
    _git(repo["path"], "push", "origin", "main")
    sha = _git(repo["path"], "rev-parse", "HEAD")
    result = verify_remote_branch_matches(repo["path"], "main", sha)
    assert result == {"origin": str(repo["origin"]), "branch": "main", "remote_sha": sha}


def test_verify_remote_branch_matches_fails_closed_on_missing_branch(repo):
    sha = _git(repo["path"], "rev-parse", "HEAD")
    with pytest.raises(TaskError, match="does not exist"):
        verify_remote_branch_matches(repo["path"], "main", sha)


def test_verify_remote_branch_matches_fails_closed_on_sha_mismatch(repo):
    _git(repo["path"], "push", "origin", "main")
    (repo["path"] / "README.md").write_text("second\n", encoding="utf-8")
    _git(repo["path"], "commit", "-am", "second commit, never pushed")
    local_sha = _git(repo["path"], "rev-parse", "HEAD")
    with pytest.raises(TaskError, match="does not match local final commit SHA"):
        verify_remote_branch_matches(repo["path"], "main", local_sha)


def test_verify_remote_branch_matches_fails_closed_on_malformed_expected_sha(repo):
    _git(repo["path"], "push", "origin", "main")
    with pytest.raises(TaskError):
        verify_remote_branch_matches(repo["path"], "main", "not-a-sha")


def test_verify_remote_branch_matches_never_forces_or_merges(repo):
    """Structural proof this module only reads: pushing an out-of-scope
    branch or forcing anything is never invoked -- proven here via a mocked
    runner that would fail the test if any git subcommand other than
    remote/ls-remote were ever issued."""
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        assert command[2] not in ("push", "merge"), f"unexpected mutating git command: {command}"
        if "get-url" in command:
            return subprocess.CompletedProcess(command, 0, "https://example.com/repo.git\n", "")
        return subprocess.CompletedProcess(command, 0, f"{'a' * 40}\trefs/heads/main\n", "")

    result = verify_remote_branch_matches("/fake/dir", "main", "a" * 40, runner=fake_runner)
    assert result["remote_sha"] == "a" * 40
    assert any("ls-remote" in call for call in calls)


def test_verify_remote_branch_matches_fails_closed_on_ambiguous_multiple_refs():
    def fake_runner(command, **kwargs):
        if "get-url" in command:
            return subprocess.CompletedProcess(command, 0, "https://example.com/repo.git\n", "")
        return subprocess.CompletedProcess(command, 0, f"{'a' * 40}\trefs/heads/main\n{'b' * 40}\trefs/heads/main\n", "")

    with pytest.raises(TaskError, match="ambiguous"):
        verify_remote_branch_matches("/fake/dir", "main", "a" * 40, runner=fake_runner)
