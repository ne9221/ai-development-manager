import ast
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from manager import github_dispatch_watcher
from manager.tasks import TaskError
from manager.test_github_dispatch_ingress import FakeGitHubClient


REPO = "ne9221/ai-development-manager"
BRANCH = "dispatch-requests"
PATH = "dispatch-requests"


def request(request_id="gh-e2e-1", **changes):
    # created_at is pinned to the real wall clock (not a fixed literal) so
    # read_request()'s own staleness check (MAX_AGE_SECONDS) passes
    # regardless of when this test suite actually runs -- run_once() uses
    # the real current time (no `now` override), unlike
    # test_github_dispatch_ingress.py's fixed-clock unit tests.
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    value = {
        "request_id": request_id, "project_id": "ai-development-manager",
        "title": "Harmless GitHub ingress proof", "goal": "Return a short status report without changing files.",
        "preferred_provider": "codex", "priority": "normal", "created_at": created_at,
    }
    value.update(changes)
    return value
BUCKET = "adm-lock-bucket"

ENV = {
    "ADM_GITHUB_DISPATCH_INGRESS_REPO": REPO,
    "ADM_GITHUB_DISPATCH_INGRESS_BRANCH": BRANCH,
    "ADM_GITHUB_DISPATCH_INGRESS_PATH": PATH,
    "ADM_GITHUB_DISPATCH_INGRESS_TOKEN": "fake-token",
    "ADM_LOCK_GCS_BUCKET": BUCKET,
}


class FakeStore:
    """Minimal stand-in for manager.tasks.DriveRecords; unused by these
    tests beyond being constructed, since handle_dispatch is mocked out."""

    def __init__(self, service):
        self.service = service


def env(**overrides):
    merged = dict(ENV)
    merged.update(overrides)
    return merged


class RunOnceTests(unittest.TestCase):
    def test_valid_request_calls_existing_poll_path_once(self):
        client = FakeGitHubClient([request()])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-1",
                                     "task_id": "dispatch-gh-e2e-1", "command_id": "dispatch-gh-e2e-1",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = github_dispatch_watcher.run_once(
                build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, handler.call_count)
        self.assertTrue(result["ingress"][0]["accepted"])

    def test_two_valid_requests_are_both_evaluated(self):
        client = FakeGitHubClient([request("gh-e2e-1"), request("gh-e2e-2")])
        handler = Mock(side_effect=lambda store, svc, factory, payload, request_created_at=None: {
            "accepted": True, "request_id": payload["request_id"],
            "task_id": f'dispatch-{payload["request_id"]}', "command_id": f'dispatch-{payload["request_id"]}',
            "status": "queued",
        })
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = github_dispatch_watcher.run_once(
                build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)
        self.assertEqual(2, handler.call_count)
        self.assertEqual(2, len(result["ingress"]))
        self.assertTrue(all(item["accepted"] for item in result["ingress"]))

    def test_one_malformed_and_one_valid_both_processed(self):
        client = FakeGitHubClient([b"{not valid json", request("gh-e2e-2")])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-2",
                                     "task_id": "dispatch-gh-e2e-2", "command_id": "dispatch-gh-e2e-2",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = github_dispatch_watcher.run_once(
                build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)
        self.assertEqual(1, handler.call_count)
        accepted = [item for item in result["ingress"] if item["accepted"]]
        rejected = [item for item in result["ingress"] if not item["accepted"]]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))

    def test_missing_bucket_env_fails_closed(self):
        with patch.dict(os.environ, env(ADM_LOCK_GCS_BUCKET=""), clear=False):
            with self.assertRaises(TaskError):
                github_dispatch_watcher.run_once(
                    build_service_fn=lambda: (_ for _ in ()).throw(AssertionError("must not build a Drive service")),
                    store_factory=FakeStore,
                    client_factory=lambda: (_ for _ in ()).throw(AssertionError("must not build a GitHub client")))

    def test_missing_repo_env_fails_closed_via_existing_check(self):
        client = FakeGitHubClient([request()])
        with patch.dict(os.environ, env(ADM_GITHUB_DISPATCH_INGRESS_REPO=""), clear=False):
            with self.assertRaises(TaskError):
                github_dispatch_watcher.run_once(
                    build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)

    def test_missing_branch_env_fails_closed_via_existing_check(self):
        client = FakeGitHubClient([request()])
        with patch.dict(os.environ, env(ADM_GITHUB_DISPATCH_INGRESS_BRANCH=""), clear=False):
            with self.assertRaises(TaskError):
                github_dispatch_watcher.run_once(
                    build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)

    def test_missing_token_env_fails_closed(self):
        with patch.dict(os.environ, env(ADM_GITHUB_DISPATCH_INGRESS_TOKEN=""), clear=False):
            with self.assertRaises(TaskError):
                github_dispatch_watcher.run_once(
                    build_service_fn=lambda: object(), store_factory=FakeStore,
                    client_factory=github_dispatch_watcher.GitHubApiClient.default)

    def test_drive_auth_failure_fails_closed(self):
        def boom():
            raise RuntimeError("Google Drive API initialization failed: no credentials")

        client = FakeGitHubClient([request()])
        with patch.dict(os.environ, env(), clear=False):
            with self.assertRaises(RuntimeError):
                github_dispatch_watcher.run_once(build_service_fn=boom, store_factory=FakeStore,
                                                 client_factory=lambda: client)

    def test_github_auth_failure_fails_closed(self):
        def boom():
            raise RuntimeError("GitHub API initialization failed: bad token")

        with patch.dict(os.environ, env(), clear=False):
            with self.assertRaises(RuntimeError):
                github_dispatch_watcher.run_once(build_service_fn=lambda: object(), store_factory=FakeStore,
                                                 client_factory=boom)

    def test_duplicate_request_result_preserved_across_polls(self):
        client = FakeGitHubClient([request()])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-1",
                                     "task_id": "dispatch-gh-e2e-1", "command_id": "dispatch-gh-e2e-1",
                                     "status": "completed"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            first = github_dispatch_watcher.run_once(
                build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)
            second = github_dispatch_watcher.run_once(
                build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)
        self.assertEqual(first["ingress"], second["ingress"])
        self.assertEqual(2, handler.call_count)


class MainCliTests(unittest.TestCase):
    def test_main_once_success_returns_zero(self):
        client = FakeGitHubClient([request()])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-1",
                                     "task_id": "dispatch-gh-e2e-1", "command_id": "dispatch-gh-e2e-1",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.github_dispatch_ingress.handle_dispatch", handler), \
             patch("manager.github_dispatch_watcher.build_service", lambda: object()), \
             patch("manager.github_dispatch_watcher.DriveRecords", FakeStore), \
             patch("manager.github_dispatch_watcher.GitHubApiClient.default", lambda: client):
            self.assertEqual(0, github_dispatch_watcher.main(["--once"]))

    def test_main_requires_once_flag(self):
        with self.assertRaises(SystemExit):
            github_dispatch_watcher.main([])

    def test_main_missing_bucket_env_returns_nonzero(self):
        with patch.dict(os.environ, env(ADM_LOCK_GCS_BUCKET=""), clear=False):
            self.assertEqual(1, github_dispatch_watcher.main(["--once"]))

    def test_main_missing_token_env_returns_nonzero(self):
        with patch.dict(os.environ, env(ADM_GITHUB_DISPATCH_INGRESS_TOKEN=""), clear=False), \
             patch("manager.github_dispatch_watcher.build_service", lambda: object()), \
             patch("manager.github_dispatch_watcher.DriveRecords", FakeStore):
            self.assertEqual(1, github_dispatch_watcher.main(["--once"]))

    def test_main_drive_auth_failure_returns_nonzero(self):
        def boom():
            raise RuntimeError("Google Drive API initialization failed: no credentials")

        client = FakeGitHubClient([request()])
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.github_dispatch_watcher.build_service", boom), \
             patch("manager.github_dispatch_watcher.GitHubApiClient.default", lambda: client):
            self.assertEqual(1, github_dispatch_watcher.main(["--once"]))


class NoProviderLaunchAuthorityTests(unittest.TestCase):
    """Static + behavioral proof this thin runner never gains provider
    launch authority: it must not import or reference any of the launcher/
    execution-runner/orchestrator surfaces Command Watcher alone owns."""

    FORBIDDEN_NAMES = ("ClaudeLauncher", "CodexLauncher", "AgRunner", "execution_runner", "launch_task")

    def _module_source(self):
        import inspect
        return inspect.getsource(github_dispatch_watcher)

    def test_source_does_not_reference_provider_launch_names(self):
        source = self._module_source()
        for name in self.FORBIDDEN_NAMES:
            self.assertNotIn(name, source, f"github_dispatch_watcher.py must never reference {name}")

    def test_module_imports_exclude_provider_launch_modules(self):
        tree = ast.parse(self._module_source())
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        forbidden_modules = {
            "manager.claude_launcher", "manager.codex_launcher", "manager.ag_runner", "manager.execution_runner",
            "manager.command_watcher",
        }
        self.assertFalse(imported_modules & forbidden_modules,
                         f"github_dispatch_watcher.py must not import: {imported_modules & forbidden_modules}")

    def test_runner_module_has_no_claude_launcher_attribute(self):
        self.assertFalse(hasattr(github_dispatch_watcher, "ClaudeLauncher"))

    def test_runner_module_has_no_codex_launcher_attribute(self):
        self.assertFalse(hasattr(github_dispatch_watcher, "CodexLauncher"))

    def test_runner_module_has_no_ag_runner_attribute(self):
        self.assertFalse(hasattr(github_dispatch_watcher, "AgRunner"))

    def test_runner_module_has_no_execution_runner_attribute(self):
        self.assertFalse(hasattr(github_dispatch_watcher, "execution_runner"))
        self.assertFalse(hasattr(github_dispatch_watcher, "launch_task"))

    def test_no_provider_process_started_across_full_run(self):
        client = FakeGitHubClient([request()])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-1",
                                     "task_id": "dispatch-gh-e2e-1", "command_id": "dispatch-gh-e2e-1",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.github_dispatch_ingress.handle_dispatch", handler), \
             patch("subprocess.Popen", side_effect=AssertionError("no process may be started")), \
             patch("os.startfile", side_effect=AssertionError("no process may be started"), create=True):
            result = github_dispatch_watcher.run_once(
                build_service_fn=lambda: object(), store_factory=FakeStore, client_factory=lambda: client)
        self.assertEqual("ok", result["status"])


if __name__ == "__main__":
    unittest.main()
