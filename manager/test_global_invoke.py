"""Tests for the Global Invoke adapter (Global Hands-off Execution Layer, Slice E)."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud.dispatch_ingress import DispatchIngressError
from cloud.test_dispatch_ingress import SharedMemoryRegistries
from manager.global_invoke import GlobalInvokeError, global_invoke, resolve_baseline_head, resolve_project
from manager.project_registry import (
    AmbiguousProjectError, ProjectDisabledError, ProjectNotFoundError, ProjectRegistry,
)
from manager.tasks import DriveRecords, create_project, validate
from manager.test_dispatcher import quota as quota_fixture
from manager.test_tasks import FakeDriveService
from manager.trusted_ingress import ADMISSION_VERSION, ADMISSION_VERSION_V2_REPO_WRITE


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def drive_project(project_id="proj-a", repo="https://github.com/example/project-a", **overrides):
    value = {"project_id": project_id, "name": "Project A", "repo": repo, "default_branch": "main",
              "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [], "current_phase": "Phase 1",
              "important_constraints": []}
    value.update(overrides)
    return value


def registry_entry(project_id="proj-a", aliases=("Project A Alias",), repo="https://github.com/example/project-a",
                    status="enabled", resolution_status="verified",
                    common_governance=None, project_rules=None):
    return {
        "project_id": project_id, "display_name": project_id, "aliases": list(aliases),
        "repo": {"canonical_url": repo}, "default_branch": "main",
        "baseline_resolution_policy": {"strategy": "origin_default", "pinned_ref": None},
        "common_governance": {"reference": "governance-rules.json"} if common_governance is None else common_governance,
        "project_rules": {"reference": "PROJECT-RULES.md"} if project_rules is None else project_rules,
        "working_directory_policy": {"relative_path": project_id, "resolver_strategy": "workspace_relative"},
        "isolation_policy": {"mode": "worktree_per_task"},
        "status": status, "resolution_status": resolution_status,
    }


class StubRegistry:
    """Directly forces manager.project_registry.ProjectRegistry.get_project's
    own documented failure modes -- the real registry prevents ambiguous
    aliases at registration time, so an ambiguous *lookup* is only
    reachable this way without fighting that (correct) constructor
    invariant."""

    def __init__(self, exc):
        self.exc = exc

    def get_project(self, query):
        raise self.exc


class GlobalInvokeTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "canonical"
        self.repo.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "init")
        self.head = _git(self.repo, "rev-parse", "HEAD")

        create_project(self.store, drive_project())
        self.registry = ProjectRegistry(projects=[registry_entry()])

    def tearDown(self):
        self.quota_patch.stop()
        self._tmp.cleanup()

    def call(self, request, registry=None, canonical_checkout=None):
        return global_invoke(self.store, self.service, self.registries.factory, request,
                              registry=registry or self.registry, canonical_checkout=canonical_checkout or self.repo)

    def base_request(self, **overrides):
        value = {"project": "proj-a", "title": "Fix the parser", "goal": "Fix the regression",
                  "idempotency_key": "inv-1"}
        value.update(overrides)
        return value

    # --- project resolution -------------------------------------------------

    def test_exact_project_id_resolve(self):
        result = self.call(self.base_request(project="proj-a"))
        self.assertEqual({"accepted": True, "request_id": "inv-1", "task_id": "dispatch-inv-1",
                           "command_id": "dispatch-inv-1", "status": "queued"}, result)
        task = self.store.get("tasks", "proj-a", result["task_id"])
        self.assertEqual("proj-a", task["project_id"])

    def test_alias_resolve(self):
        result = self.call(self.base_request(project="project a alias"))
        task = self.store.get("tasks", "proj-a", result["task_id"])
        self.assertEqual("proj-a", task["project_id"])
        self.assertEqual("project a alias", task["source_context"]["resolved_project_reference"])

    def test_ambiguous_alias_fail_closed(self):
        stub = StubRegistry(AmbiguousProjectError("ambiguous alias 'x'"))
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(self.base_request(project="x"), registry=stub)
        self.assertEqual("project_ambiguous", ctx.exception.code)
        with self.assertRaises(Exception):
            self.store.get("tasks", "proj-a", "dispatch-inv-1")

    def test_unknown_project_fail_closed(self):
        stub = StubRegistry(ProjectNotFoundError("unknown project 'ghost'"))
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(self.base_request(project="ghost"), registry=stub)
        self.assertEqual("project_not_found", ctx.exception.code)

    def test_disabled_project_fail_closed(self):
        stub = StubRegistry(ProjectDisabledError("project 'proj-a' is disabled"))
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(self.base_request(), registry=stub)
        self.assertEqual("project_disabled", ctx.exception.code)

    # --- repo-write eligibility ---------------------------------------------

    def test_non_repo_write_eligible_project_reject(self):
        registry = ProjectRegistry(projects=[registry_entry(resolution_status="unresolved")])
        create_project(self.store, drive_project())
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(self.base_request(repo_write=True, allowed_paths=["manager/foo.py"]), registry=registry)
        self.assertEqual("repo_write_not_eligible", ctx.exception.code)
        with self.assertRaises(Exception):
            self.store.get("tasks", "proj-a", "dispatch-inv-1")

    def test_governance_missing_reject(self):
        registry = ProjectRegistry(projects=[registry_entry(common_governance={})])
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(self.base_request(repo_write=True, allowed_paths=["manager/foo.py"]), registry=registry)
        self.assertEqual("governance_missing", ctx.exception.code)

    def test_repo_mismatch_reject(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        create_project(store, drive_project(repo="https://github.com/example/DIFFERENT-repo"))
        with self.assertRaises(GlobalInvokeError) as ctx:
            global_invoke(store, service, self.registries.factory,
                           self.base_request(repo_write=True, allowed_paths=["manager/foo.py"]),
                           registry=self.registry, canonical_checkout=self.repo)
        self.assertEqual("repo_identity_mismatch", ctx.exception.code)

    def test_allowed_paths_unsafe_reject(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(self.base_request(repo_write=True, allowed_paths=["../../etc/passwd"]))
        self.assertEqual("unsafe_allowed_path", ctx.exception.code)

    # --- v1/v2 boundary -------------------------------------------------------

    def test_v1_cannot_escalate_to_v2(self):
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(self.base_request(allowed_paths=["manager/foo.py"]))  # repo_write omitted (defaults False)
        self.assertEqual("malformed_request", ctx.exception.code)

        with self.assertRaises(GlobalInvokeError) as ctx2:
            self.call({**self.base_request(), "constraints": {"read_only": False}})
        self.assertEqual("malformed_request", ctx2.exception.code)

        result = self.call(self.base_request())
        task = self.store.get("tasks", "proj-a", result["task_id"])
        self.assertTrue(task["read_only"])
        self.assertEqual(ADMISSION_VERSION, task["source_context"]["admission_version"])

    def test_idempotent_duplicate_invoke(self):
        first = self.call(self.base_request(repo_write=True, allowed_paths=["manager/foo.py"]))
        second = self.call(self.base_request(repo_write=True, allowed_paths=["manager/foo.py"]))
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["command_id"], second["command_id"])
        self.store.get("tasks", "proj-a", "dispatch-inv-1")
        self.store.get("commands", "proj-a", "dispatch-inv-1")

    # --- authoritative binding -------------------------------------------------

    def test_generated_task_and_command_carry_authoritative_binding(self):
        result = self.call(self.base_request(repo_write=True, allowed_paths=["manager/foo.py"], project="project a alias"))
        task = self.store.get("tasks", "proj-a", result["task_id"])
        validate("task", task)
        command = self.store.get("commands", "proj-a", result["command_id"])
        validate("command", command)

        self.assertEqual("proj-a", task["project_id"])
        self.assertFalse(task["read_only"])
        self.assertTrue(task["needs_repo_edit"])
        self.assertEqual(ADMISSION_VERSION_V2_REPO_WRITE, task["source_context"]["admission_version"])
        self.assertEqual("https://github.com/example/project-a", task["source_context"]["repo"])
        self.assertEqual(self.head, task["baseline_head"])
        self.assertEqual(["manager/foo.py"], task["allowed_paths"])
        self.assertEqual("global_invoke", task["source_context"]["resolved_via"])
        self.assertEqual("project a alias", task["source_context"]["resolved_project_reference"])
        self.assertEqual({"reference": "governance-rules.json"}, task["source_context"]["governance_snapshot"])
        self.assertEqual({"reference": "PROJECT-RULES.md"}, task["source_context"]["project_rules"])

        self.assertEqual("proj-a", command["project_id"])
        self.assertEqual(task["task_id"], command["task_id"])
        self.assertEqual(ADMISSION_VERSION_V2_REPO_WRITE, command["admission_version"])


class ResolveBaselineHeadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "canonical"
        self.repo.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "init")
        self.head = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_default_branch_head(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        project = registry.get_project("proj-a")
        self.assertEqual(self.head, resolve_baseline_head(self.repo, project))

    def test_fails_closed_on_unresolvable_ref(self):
        registry = ProjectRegistry(projects=[registry_entry(project_id="proj-b", aliases=())])
        project = registry.get_project("proj-b")
        object.__setattr__(project, "default_branch", "no-such-branch")
        with self.assertRaises(GlobalInvokeError) as ctx:
            resolve_baseline_head(self.repo, project)
        self.assertEqual("baseline_resolution_failed", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
