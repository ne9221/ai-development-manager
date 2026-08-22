#!/usr/bin/env python3
"""Tests for the Global Invoke adapter, focused on the P0 fix: baseline
authority for a repo_write invocation must come from the server-side
Remote Baseline Resolver, never from a caller-supplied field, and never
from a local git checkout of the business repo."""

import unittest
from unittest.mock import patch

from cloud.dispatch_ingress import DispatchIngressError
from cloud.test_dispatch_ingress import SharedMemoryRegistries, project as drive_project
from manager.global_invoke import GlobalInvokeError, global_invoke
from manager.project_registry import ProjectRegistry
from manager.tasks import DriveRecords, create_project, validate
from manager.test_dispatcher import quota as quota_fixture
from manager.test_tasks import FakeDriveService


REPO_URL = "https://github.com/example/project"
VALID_SHA = "c" * 40


def registry_entry(project_id="p1", **overrides):
    entry = {
        "project_id": project_id,
        "display_name": "Project One",
        "aliases": ["proj-one"],
        "repo": {"canonical_url": REPO_URL, "owner": "example", "name": "project"},
        "default_branch": "main",
        "baseline_resolution_policy": {"strategy": "origin_default", "pinned_ref": None},
        "common_governance": {"reference": "governance-rules.json", "version": "1.0.0"},
        "project_rules": {"reference": "PROJECT-RULES.md"},
        "status": "enabled",
        "resolution_status": "verified",
    }
    entry.update(overrides)
    return entry


def request(**changes):
    value = {"idempotency_key": "gi-1", "project": "p1", "title": "Fix parser", "goal": "Fix the parser regression"}
    value.update(changes)
    return value


def write_request(**changes):
    value = request(idempotency_key="gi-w1", repo_write=True, allowed_paths=["manager/foo.py"])
    value.update(changes)
    return value


class RecordingBaselineResolver:
    """Stands in for manager.remote_baseline_resolver.resolve_remote_baseline
    -- proves global_invoke never touches git or the filesystem to get a
    baseline; it only ever calls this collaborator with a project_id."""

    def __init__(self, sha=VALID_SHA, repository=REPO_URL, project_id_override=None):
        self.sha = sha
        self.repository = repository
        self.project_id_override = project_id_override
        self.calls = []

    def __call__(self, project_id, registry=None):
        self.calls.append(project_id)
        return {
            "project_id": self.project_id_override or project_id,
            "repository": self.repository,
            "canonical_branch": "main",
            "baseline_sha": self.sha,
            "source": "github_remote_api",
            "resolved_at": "2026-08-22T00:00:00Z",
        }


class GlobalInvokeBaselineAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, drive_project())
        self.registry = ProjectRegistry(projects=[registry_entry()])
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def call(self, req=None, resolver=None):
        return global_invoke(self.store, self.service, self.registries.factory,
                              req if req is not None else request(), registry=self.registry,
                              baseline_resolver=resolver or RecordingBaselineResolver())

    def test_repo_write_uses_server_resolved_baseline_head(self):
        resolver = RecordingBaselineResolver(sha=VALID_SHA)
        result = self.call(write_request(), resolver=resolver)
        self.assertEqual(["p1"], resolver.calls)
        task = self.store.get("tasks", "p1", result["task_id"])
        validate("task", task)
        self.assertEqual(VALID_SHA, task["baseline_head"])
        self.assertEqual(REPO_URL, task["source_context"]["repo"])

    def test_read_only_invocation_never_calls_baseline_resolver(self):
        resolver = RecordingBaselineResolver()
        self.call(request(), resolver=resolver)
        self.assertEqual([], resolver.calls)

    def test_caller_cannot_supply_baseline_sha_field(self):
        for forbidden_field in ("baseline_sha", "baseline_head", "canonical_head", "repo", "branch"):
            with self.subTest(field=forbidden_field):
                req = write_request(idempotency_key=f"gi-spoof-{forbidden_field}")
                req[forbidden_field] = "attacker-supplied-value"
                with self.assertRaises(GlobalInvokeError) as ctx:
                    self.call(req)
                self.assertEqual("malformed_request", ctx.exception.code)
                self.assertIn(forbidden_field, str(ctx.exception))

    def test_resolver_result_for_wrong_project_is_rejected(self):
        resolver = RecordingBaselineResolver(project_id_override="some-other-project")
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(write_request(), resolver=resolver)
        self.assertEqual("baseline_resolution_failed", ctx.exception.code)

    def test_resolver_result_for_wrong_repository_is_rejected(self):
        resolver = RecordingBaselineResolver(repository="https://github.com/attacker/evil-repo.git")
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(write_request(), resolver=resolver)
        self.assertEqual("repo_identity_mismatch", ctx.exception.code)

    def test_resolved_baseline_head_matches_dispatch_ingress_shape(self):
        resolver = RecordingBaselineResolver(sha=VALID_SHA)
        result = self.call(write_request(), resolver=resolver)
        self.assertTrue(result["accepted"])

    def test_no_baseline_field_in_allowed_request_fields(self):
        from manager.global_invoke import ALLOWED_GLOBAL_INVOKE_FIELDS
        for forbidden in ("baseline_sha", "baseline_head", "canonical_head", "repo", "branch"):
            self.assertNotIn(forbidden, ALLOWED_GLOBAL_INVOKE_FIELDS)

    def test_existing_read_only_invoke_behavior_unaffected(self):
        result = self.call(request())
        self.assertEqual({"accepted": True, "request_id": "gi-1", "task_id": "dispatch-gi-1",
                           "command_id": "dispatch-gi-1", "status": "queued"}, result)
        task = self.store.get("tasks", "p1", "dispatch-gi-1")
        self.assertTrue(task["read_only"])


if __name__ == "__main__":
    unittest.main()
