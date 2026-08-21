#!/usr/bin/env python3
"""Tests for deterministic branch/worktree materialization (Slice C)."""

import subprocess
from copy import deepcopy

import pytest

from manager.project_registry import ProjectMetadata
from manager.worktree_materializer import (
    WorktreeMaterializationError,
    compute_worktree_identity,
    compute_worktree_path,
    materialize_worktree,
)


class MemoryStore:
    """Minimal in-memory Task store -- mirrors manager.test_tasks.MemoryStore."""

    def __init__(self):
        self.records = {}

    def put(self, area, project, name, document):
        self.records[(area, project, name)] = deepcopy(document)
        return document

    def get(self, area, project, name):
        return deepcopy(self.records[(area, project, name)])


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def canonical_repo(tmp_path):
    repo = tmp_path / "canonical"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD")

    (repo / "README.md").write_text("hello again\n", encoding="utf-8")
    _git(repo, "commit", "-am", "second commit")
    head2 = _git(repo, "rev-parse", "HEAD")

    # An orphan branch/commit deliberately unreachable from main, to prove
    # baseline-lineage rejection actually walks ancestry rather than merely
    # checking the commit exists somewhere in the repo.
    _git(repo, "checkout", "--orphan", "unrelated")
    _git(repo, "commit", "--allow-empty", "-m", "unrelated history")
    unrelated_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    return {"path": repo, "head": head, "head2": head2, "unrelated_head": unrelated_head}


@pytest.fixture
def project():
    return ProjectMetadata(
        project_id="proj-a", display_name="Project A", aliases=(),
        repo={"canonical_url": "https://github.com/example/project-a"},
        default_branch="main", baseline_resolution_policy={"strategy": "origin_default"},
        common_governance={"reference": "AI-DEVELOPMENT-RULES.md"}, project_rules={"reference": "PROJECT-RULES.md"},
        working_directory_policy={"relative_path": "proj-a"}, isolation_policy={"mode": "worktree_per_task"},
        provider_restrictions={}, protected_paths=(), default_write_boundaries=("*",),
        pointer_rules={}, status="enabled", resolution_status="verified",
    )


def _task(task_id="task-1", project_id="proj-a", baseline_head=None, repo="https://github.com/example/project-a", **overrides):
    task = {
        "task_id": task_id, "project_id": project_id, "title": "Do the thing", "status": "ready",
        "priority": "normal", "created_at": "2026-08-21T00:00:00.000000Z", "updated_at": "2026-08-21T00:00:00.000000Z",
        "task_type": "implementation", "expected_minutes": 20, "scope": ["manager/foo.py"], "constraints": [],
        "acceptance_criteria": ["worktree materialized"], "recommended_provider": None, "assigned_provider": None,
        "mode": None, "effort": None, "depends_on": [], "blocked_reason": None,
        "current_progress": "Not started", "next_action": "Materialize worktree",
        "read_only": False, "needs_repo_edit": True,
        "allowed_paths": ["manager/foo.py"], "baseline_head": baseline_head,
        "execution_policies": ["disposable", "bounded_repo_write", "no_external_writes"],
        "source_context": {"repo": repo},
    }
    task.update(overrides)
    return task


def _store_with_task(task):
    store = MemoryStore()
    store.put("tasks", task["project_id"], task["task_id"], task)
    return store


# --- deterministic identity (pure function) --------------------------------

def test_deterministic_branch_naming():
    a1 = compute_worktree_identity("proj-a", "task-1")
    a2 = compute_worktree_identity("proj-a", "task-1")
    assert a1 == a2
    assert a1["branch"] == "refs/heads/adm-worktree/proj-a/task-1"
    assert a1["branch_short"] == "adm-worktree/proj-a/task-1"


def test_deterministic_branch_naming_differs_by_task():
    a = compute_worktree_identity("proj-a", "task-1")
    b = compute_worktree_identity("proj-a", "task-2")
    assert a["branch"] != b["branch"]
    assert a["worktree_id"] != b["worktree_id"]


def test_deterministic_worktree_path(tmp_path):
    path1, identity1 = compute_worktree_path(tmp_path, "proj-a", "task-1")
    path2, identity2 = compute_worktree_path(tmp_path, "proj-a", "task-1")
    assert path1 == path2
    assert identity1 == identity2


def test_invalid_identity_characters_rejected():
    with pytest.raises(WorktreeMaterializationError) as exc:
        compute_worktree_identity("proj a", "task-1")
    assert exc.value.code == "invalid_identity"


# --- admission/identity preconditions ---------------------------------------

def test_wrong_repo_reject(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"], repo="https://github.com/example/some-other-repo")
    store = _store_with_task(task)
    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, task, canonical_repo["path"], canonical_repo["path"].parent / "workspace")
    assert exc.value.code == "repo_identity_mismatch"


def test_missing_baseline_rejected(canonical_repo, project):
    task = _task(baseline_head="f" * 40)
    store = _store_with_task(task)
    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, task, canonical_repo["path"], canonical_repo["path"].parent / "workspace")
    assert exc.value.code == "invalid_baseline_head"


def test_malformed_baseline_rejected(canonical_repo, project):
    task = _task(baseline_head="not-a-sha")
    store = _store_with_task(task)
    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, task, canonical_repo["path"], canonical_repo["path"].parent / "workspace")
    assert exc.value.code == "invalid_baseline_head"


def test_baseline_lineage_mismatch_rejected(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["unrelated_head"])
    store = _store_with_task(task)
    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, task, canonical_repo["path"], canonical_repo["path"].parent / "workspace")
    assert exc.value.code == "baseline_lineage_mismatch"


def test_non_repo_write_task_rejected(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"], read_only=True, needs_repo_edit=False,
                 execution_policies=["disposable", "read_only", "no_repo_writes", "no_external_writes"])
    store = _store_with_task(task)
    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, task, canonical_repo["path"], canonical_repo["path"].parent / "workspace")
    assert exc.value.code == "not_repo_write_task"


# --- create / reuse / persistence ------------------------------------------

def test_first_create_pass(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    workspace_root = canonical_repo["path"].parent / "workspace"
    result = materialize_worktree(store, project, task, canonical_repo["path"], workspace_root)

    assert result["branch"] == "refs/heads/adm-worktree/proj-a/task-1"
    assert result["worktree_id"] == "proj-a--task-1"
    assert result["baseline_head"] == canonical_repo["head"]
    from pathlib import Path
    assert Path(result["working_directory"]) == workspace_root / "worktrees" / "proj-a" / "task-1"
    assert Path(result["working_directory"]).is_dir()
    assert _git(result["working_directory"], "rev-parse", "HEAD") == canonical_repo["head"]
    assert _git(result["working_directory"], "symbolic-ref", "--short", "HEAD") == "adm-worktree/proj-a/task-1"


def test_task_persistence_before_provider_launch(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    workspace_root = canonical_repo["path"].parent / "workspace"
    result = materialize_worktree(store, project, task, canonical_repo["path"], workspace_root)

    persisted = store.get("tasks", "proj-a", "task-1")
    assert persisted["working_directory"] == result["working_directory"]
    assert persisted["branch"] == result["branch"]
    assert persisted["worktree_id"] == result["worktree_id"]
    assert persisted["baseline_head"] == result["baseline_head"]


def test_provider_cwd_exactly_equals_isolated_worktree(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    workspace_root = canonical_repo["path"].parent / "workspace"
    result = materialize_worktree(store, project, task, canonical_repo["path"], workspace_root)

    # The value later handed to LaunchRequest(working_directory=...) must be
    # exactly this path -- never the canonical checkout.
    assert result["working_directory"] != str(canonical_repo["path"])


def test_identical_retry_reuses_same_worktree(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    workspace_root = canonical_repo["path"].parent / "workspace"

    first = materialize_worktree(store, project, task, canonical_repo["path"], workspace_root)
    # A retry re-reads the (still-unmaterialized-looking, fresh) task input,
    # not the mutated store record -- simulate that with an unchanged dict.
    second = materialize_worktree(store, project, _task(baseline_head=canonical_repo["head"]),
                                  canonical_repo["path"], workspace_root)

    assert first == second
    entries = [line for line in _git(canonical_repo["path"], "worktree", "list", "--porcelain").split("\n\n") if line.strip()]
    assert len(entries) == 2  # canonical checkout itself + exactly one linked worktree


def test_duplicate_dispatch_does_not_create_duplicate_worktree(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    workspace_root = canonical_repo["path"].parent / "workspace"

    results = [materialize_worktree(store, project, _task(baseline_head=canonical_repo["head"]),
                                    canonical_repo["path"], workspace_root) for _ in range(3)]
    assert results[0] == results[1] == results[2]
    entries = [line for line in _git(canonical_repo["path"], "worktree", "list", "--porcelain").split("\n\n") if line.strip()]
    assert len(entries) == 2


def test_same_task_different_baseline_fails_closed(canonical_repo, project):
    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    workspace_root = canonical_repo["path"].parent / "workspace"
    materialize_worktree(store, project, task, canonical_repo["path"], workspace_root)

    retry_task = _task(baseline_head=canonical_repo["head2"])
    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, retry_task, canonical_repo["path"], workspace_root)
    assert exc.value.code == "worktree_identity_mismatch"


def test_different_task_cannot_reuse_another_task_worktree(canonical_repo, project):
    task_a = _task(task_id="task-a", baseline_head=canonical_repo["head"])
    store = _store_with_task(task_a)
    workspace_root = canonical_repo["path"].parent / "workspace"
    materialize_worktree(store, project, task_a, canonical_repo["path"], workspace_root)

    identity_a = compute_worktree_identity("proj-a", "task-a")
    spoofed = _task(task_id="task-b", baseline_head=canonical_repo["head"],
                     worktree_id=identity_a["worktree_id"], branch=identity_a["branch"])
    store.put("tasks", "proj-a", "task-b", spoofed)
    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, spoofed, canonical_repo["path"], workspace_root)
    assert exc.value.code in ("worktree_ownership_mismatch", "worktree_identity_mismatch")


def test_wrong_branch_ownership_fails_closed(canonical_repo, project):
    """A worktree materialized at the right deterministic path/branch by
    something other than this module (no owner marker) must never be
    silently adopted."""
    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    workspace_root = canonical_repo["path"].parent / "workspace"
    identity = compute_worktree_identity("proj-a", "task-1")
    worktree_path, _ = compute_worktree_path(workspace_root, "proj-a", "task-1")
    worktree_path.parent.mkdir(parents=True)
    _git(canonical_repo["path"], "worktree", "add", "-b", identity["branch_short"], str(worktree_path), canonical_repo["head"])

    with pytest.raises(WorktreeMaterializationError) as exc:
        materialize_worktree(store, project, task, canonical_repo["path"], workspace_root)
    assert exc.value.code == "worktree_ownership_mismatch"


def test_canonical_checkout_remains_untouched(canonical_repo, project):
    dirty_file = canonical_repo["path"] / "scratch.txt"
    dirty_file.write_text("unrelated in-progress work\n", encoding="utf-8")
    status_before = _git(canonical_repo["path"], "status", "--porcelain")
    branch_before = _git(canonical_repo["path"], "symbolic-ref", "--short", "HEAD")
    head_before = _git(canonical_repo["path"], "rev-parse", "HEAD")

    task = _task(baseline_head=canonical_repo["head"])
    store = _store_with_task(task)
    materialize_worktree(store, project, task, canonical_repo["path"], canonical_repo["path"].parent / "workspace")

    assert _git(canonical_repo["path"], "status", "--porcelain") == status_before
    assert _git(canonical_repo["path"], "symbolic-ref", "--short", "HEAD") == branch_before
    assert _git(canonical_repo["path"], "rev-parse", "HEAD") == head_before
    assert dirty_file.read_text(encoding="utf-8") == "unrelated in-progress work\n"


def test_dirty_canonical_checkout_not_modified_on_rejected_call(canonical_repo, project):
    dirty_file = canonical_repo["path"] / "scratch.txt"
    dirty_file.write_text("dirty\n", encoding="utf-8")
    status_before = _git(canonical_repo["path"], "status", "--porcelain")

    task = _task(baseline_head=canonical_repo["unrelated_head"])
    store = _store_with_task(task)
    with pytest.raises(WorktreeMaterializationError):
        materialize_worktree(store, project, task, canonical_repo["path"], canonical_repo["path"].parent / "workspace")

    assert _git(canonical_repo["path"], "status", "--porcelain") == status_before
