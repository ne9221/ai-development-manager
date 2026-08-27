import base64
import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.dispatch_requests import dispatch_request_registry, dispatch_rejection_registry
from manager.github_dispatch_client import GitHubApiError, GitHubNotFound
from manager.github_dispatch_ingress import (
    REPO_ENV, BRANCH_ENV, poll_github_dispatch_requests, read_request, verify_ingress_repo,
)
from manager.tasks import TaskError, create_project, DriveRecords
from manager.test_dispatcher import quota as quota_fixture
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_tasks import FakeDriveService


REPO = "ne9221/ai-development-manager"
BRANCH = "dispatch-requests"
PATH = "dispatch-requests"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def request(request_id="gh-e2e-1", **changes):
    value = {
        "request_id": request_id, "project_id": "ai-development-manager",
        "title": "Harmless GitHub ingress proof", "goal": "Return a short status report without changing files.",
        "preferred_provider": "codex", "priority": "normal", "created_at": "2026-08-27T11:59:00Z",
    }
    value.update(changes)
    return value


def _entry(name, sha, size):
    return {"name": name, "path": f"{PATH}/{name}", "sha": sha, "size": size, "type": "file"}


class FakeGitHubClient:
    """Fake manager.github_dispatch_client.GitHubApiClient -- never touches
    real GitHub. Holds one directory of {name: document_or_bytes} entries."""

    def __init__(self, documents=(), repo=REPO, branch=BRANCH, path=PATH, branch_exists=True, repo_full_name=None):
        self.repo, self.branch, self.path = repo, branch, path
        self.branch_exists = branch_exists
        self.repo_full_name = repo_full_name if repo_full_name is not None else repo
        self.entries = {}
        self.files = {}
        for index, document in enumerate(documents):
            raw = document if isinstance(document, bytes) else (json.dumps(document) + "\n").encode()
            if isinstance(document, dict) and isinstance(document.get("request_id"), str):
                name = f'{document["request_id"]}.json'
            else:
                name = f"malformed-{index}.json"
            sha = f"sha-{name}"
            self.entries[name] = _entry(name, sha, len(raw))
            self.files[name] = {
                "name": name, "path": f"{path}/{name}", "sha": sha, "size": len(raw),
                "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64",
            }
        self.list_directory_calls = []
        self.get_file_calls = []

    def get_repo(self, repo):
        if repo != self.repo:
            raise GitHubNotFound("github_not_found", "no such repo")
        return {"full_name": self.repo_full_name}

    def get_branch(self, repo, branch):
        if not self.branch_exists or branch != self.branch:
            raise GitHubNotFound("github_not_found", "no such branch")
        return {"name": branch}

    def list_directory(self, repo, path, branch):
        self.list_directory_calls.append((repo, path, branch))
        if path != self.path or branch != self.branch:
            raise GitHubNotFound("github_not_found", "no such directory")
        return [deepcopy(entry) for entry in self.entries.values()]

    def get_file(self, repo, path, branch):
        self.get_file_calls.append((repo, path, branch))
        name = path.rsplit("/", 1)[-1]
        if name not in self.files:
            raise GitHubNotFound("github_not_found", "no such file")
        return deepcopy(self.files[name])


class VerifyIngressRepoTests(unittest.TestCase):
    def test_valid_repo_and_branch_pass(self):
        client = FakeGitHubClient()
        verify_ingress_repo(client, REPO, BRANCH)

    def test_missing_repo_or_branch_arg_fails_closed(self):
        client = FakeGitHubClient()
        with self.assertRaises(TaskError):
            verify_ingress_repo(client, "", BRANCH)
        with self.assertRaises(TaskError):
            verify_ingress_repo(client, REPO, "")

    def test_repo_full_name_mismatch_fails_closed(self):
        client = FakeGitHubClient(repo_full_name="someone-else/ai-development-manager")
        with self.assertRaises(TaskError):
            verify_ingress_repo(client, REPO, BRANCH)

    def test_unknown_repo_fails_closed(self):
        client = FakeGitHubClient(repo="other/repo")
        with self.assertRaises(TaskError):
            verify_ingress_repo(client, REPO, BRANCH)

    def test_missing_branch_fails_closed(self):
        client = FakeGitHubClient(branch_exists=False)
        with self.assertRaises(TaskError):
            verify_ingress_repo(client, REPO, BRANCH)


class ReadRequestTests(unittest.TestCase):
    def test_valid_request_parses(self):
        client = FakeGitHubClient([request()])
        entry = client.entries["gh-e2e-1.json"]
        document = read_request(client, REPO, PATH, BRANCH, entry, now=NOW)
        self.assertEqual("gh-e2e-1", document["request_id"])

    def test_wrong_filename_rejected(self):
        client = FakeGitHubClient([request()])
        entry = deepcopy(client.entries["gh-e2e-1.json"])
        entry["name"] = "other.json"
        with self.assertRaises(TaskError):
            read_request(client, REPO, PATH, BRANCH, entry, now=NOW)

    def test_oversized_declared_size_rejected(self):
        client = FakeGitHubClient([request()])
        entry = deepcopy(client.entries["gh-e2e-1.json"])
        entry["size"] = 999999
        with self.assertRaises(TaskError):
            read_request(client, REPO, PATH, BRANCH, entry, now=NOW)

    def test_stale_request_rejected(self):
        client = FakeGitHubClient([request(created_at="2026-08-01T00:00:00Z")])
        entry = client.entries["gh-e2e-1.json"]
        with self.assertRaises(TaskError):
            read_request(client, REPO, PATH, BRANCH, entry, now=NOW)

    def test_future_dated_request_rejected(self):
        client = FakeGitHubClient([request(created_at="2026-08-27T13:00:00Z")])
        entry = client.entries["gh-e2e-1.json"]
        with self.assertRaises(TaskError):
            read_request(client, REPO, PATH, BRANCH, entry, now=NOW)

    def test_malformed_json_rejected(self):
        client = FakeGitHubClient([b"{not valid json"])
        entry = client.entries["malformed-0.json"]
        with self.assertRaises(TaskError):
            read_request(client, REPO, PATH, BRANCH, entry, now=NOW)

    def test_schema_invalid_document_rejected(self):
        client = FakeGitHubClient([{k: v for k, v in request().items() if k != "request_id"}])
        entry = client.entries["malformed-0.json"]
        with self.assertRaises(TaskError):
            read_request(client, REPO, PATH, BRANCH, entry, now=NOW)

    def test_sha_mismatch_between_listing_and_fetch_rejected(self):
        client = FakeGitHubClient([request()])
        entry = deepcopy(client.entries["gh-e2e-1.json"])
        entry["sha"] = "different-sha"
        with self.assertRaises(TaskError):
            read_request(client, REPO, PATH, BRANCH, entry, now=NOW)


class PollGithubDispatchRequestsTests(unittest.TestCase):
    def test_valid_request_maps_only_allowed_fields(self):
        client = FakeGitHubClient([request()])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-1", "task_id": "dispatch-gh-e2e-1",
                                     "command_id": "dispatch-gh-e2e-1", "status": "queued"})
        with patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
                                                    registry_factory=lambda *_args: object())
        self.assertTrue(result[0]["accepted"])
        payload = handler.call_args.args[3]
        self.assertEqual("codex", payload["provider"])
        self.assertEqual({"read_only": True}, payload["constraints"])
        self.assertNotIn("created_at", payload)
        self.assertEqual("2026-08-27T11:59:00Z", handler.call_args.kwargs["request_created_at"])

    def test_file_id_uses_git_blob_sha(self):
        client = FakeGitHubClient([request()])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-1", "task_id": "dispatch-gh-e2e-1",
                                     "command_id": "dispatch-gh-e2e-1", "status": "queued"})
        with patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
                                                    registry_factory=lambda *_args: object())
        self.assertEqual("sha-gh-e2e-1.json", result[0]["file_id"])

    def test_repo_write_forwarded_exactly(self):
        repo_write = {"allowed_paths": ["a.py"], "baseline_head": "ff4ab5bb77582f56c6f2bd7091cf8bf952d67fe2",
                      "repo": "https://github.com/example/project"}
        client = FakeGitHubClient([request(repo_write=repo_write)])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-1", "task_id": "dispatch-gh-e2e-1",
                                     "command_id": "dispatch-gh-e2e-1", "status": "queued"})
        with patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
                                          registry_factory=lambda *_args: object())
        payload = handler.call_args.args[3]
        self.assertEqual({"read_only": False}, payload["constraints"])
        self.assertEqual(repo_write, payload["repo_write"])

    def test_empty_directory_returns_no_results(self):
        client = FakeGitHubClient([])
        result = poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW)
        self.assertEqual([], result)

    def test_directory_not_found_yet_returns_no_results(self):
        # verify_ingress_repo() only checks repo/branch identity (both still
        # match); the listing call itself targets a different, nonexistent
        # path and 404s -- treated as "nothing submitted yet", not a
        # misconfiguration.
        client = FakeGitHubClient([])
        result = poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, "missing-path", NOW)
        self.assertEqual([], result)

    def test_unknown_repo_fails_closed(self):
        client = FakeGitHubClient([request()], repo="other/repo")
        with self.assertRaises(TaskError):
            poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW)

    def test_malformed_request_leaves_durable_rejected_truth(self):
        cases = [
            (b"{broken", "ingress_rejected"),
            ({k: v for k, v in request().items() if k != "request_id"}, "ingress_rejected"),
            (request(preferred_provider="gemini"), "ingress_rejected"),
        ]
        for document, expected_reason_code in cases:
            with self.subTest(document=document if isinstance(document, bytes) else document.get("request_id")):
                client = FakeGitHubClient([document])
                registry = MemoryClaimRegistry()
                result = poll_github_dispatch_requests(
                    object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
                    rejection_registry_factory=lambda _bucket, _file_id: registry)
                self.assertFalse(result[0]["accepted"])
                self.assertIsNotNone(registry.document)
                self.assertEqual("rejected", registry.document["status"])
                self.assertEqual(expected_reason_code, registry.document["reason_code"])
                self.assertIsNotNone(registry.document["message"])

    def test_malformed_and_valid_together_both_evaluated_fault_isolated(self):
        client = FakeGitHubClient([b"{broken", request("gh-e2e-2")])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-e2e-2", "task_id": "dispatch-gh-e2e-2",
                                     "command_id": "dispatch-gh-e2e-2", "status": "queued"})
        with patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
                                                    registry_factory=lambda *_args: object())
        accepted = [item for item in result if item["accepted"]]
        rejected = [item for item in result if not item["accepted"]]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))
        self.assertEqual(1, handler.call_count)

    def test_rejection_recording_failure_never_masks_the_real_rejection_outcome(self):
        client = FakeGitHubClient([b"{broken"])

        class BrokenRegistry:
            def read_if_exists(self):
                raise TaskError("simulated backend unavailable")

        result = poll_github_dispatch_requests(
            object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
            rejection_registry_factory=lambda _bucket, _file_id: BrokenRegistry())
        self.assertFalse(result[0]["accepted"])

    def test_max_candidates_bound_enforced(self):
        documents = [request(f"gh-e2e-{index}") for index in range(20)]
        client = FakeGitHubClient(documents)
        handler = Mock(side_effect=lambda store, svc, factory, payload, request_created_at=None: {
            "accepted": True, "request_id": payload["request_id"],
            "task_id": f'dispatch-{payload["request_id"]}', "command_id": f'dispatch-{payload["request_id"]}',
            "status": "queued",
        })
        with patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
                                                    registry_factory=lambda *_args: object(), max_candidates=5)
        self.assertEqual(5, len(result))
        self.assertEqual(5, handler.call_count)

    def test_deadline_stops_new_reads(self):
        import time as time_module
        documents = [request(f"gh-e2e-{index}") for index in range(10)]
        client = FakeGitHubClient(documents)
        handler = Mock(return_value={"accepted": True, "request_id": "x", "task_id": "y", "command_id": "z", "status": "queued"})
        with patch("manager.github_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_dispatch_requests(object(), object(), "bucket", client, REPO, BRANCH, PATH, NOW,
                                                    registry_factory=lambda *_args: object(),
                                                    deadline=time_module.monotonic() - 1)
        self.assertEqual([], result)
        self.assertEqual(0, handler.call_count)

    def test_no_archive_delete_or_write_api_calls_in_source(self):
        import ast
        import inspect
        source = inspect.getsource(__import__("manager.github_dispatch_ingress", fromlist=["*"]))
        tree = ast.parse(source)
        called_names = {node.func.attr for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        forbidden = {"delete_file", "put_file", "create_file", "update_file", "create_or_update_file",
                    "delete_directory", "push", "commit_file"}
        self.assertFalse(called_names & forbidden)

    def test_no_provider_launch_imports_in_source(self):
        import ast
        import inspect
        source = inspect.getsource(__import__("manager.github_dispatch_ingress", fromlist=["*"]))
        tree = ast.parse(source)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        forbidden_modules = {
            "manager.command_watcher", "manager.claude_launcher", "manager.codex_launcher",
            "manager.ag_runner", "manager.execution_runner",
        }
        self.assertFalse(imported_modules & forbidden_modules,
                         f"github_dispatch_ingress.py must not import: {imported_modules & forbidden_modules}")


class RealHandleDispatchEndToEndTests(unittest.TestCase):
    """Real handle_dispatch(), real DriveRecords/FakeDriveService store, real
    MemoryClaimRegistry-backed shared idempotency registry -- only the
    GitHub HTTP layer is faked. Mirrors cloud.test_dispatch_ingress.py's own
    SharedMemoryRegistries pattern exactly, so the same request_id claimed
    twice (whether from two GitHub polls or one GitHub + one Drive poll)
    provably collides in the one shared registry, never creating a second
    Task/Command."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, {
            "project_id": "ai-development-manager", "name": "ADM", "repo": "https://github.com/example/project",
            "default_branch": "main", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [],
            "current_phase": "Phase 1", "important_constraints": [],
        })
        self.registries = {}
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def _registry_factory(self, bucket, project_id, request_id):
        key = (project_id, request_id)
        if key not in self.registries:
            self.registries[key] = MemoryClaimRegistry()
        return self.registries[key]

    def test_valid_github_request_creates_exactly_one_task_and_command(self):
        client = FakeGitHubClient([request("gh-real-1", project_id="ai-development-manager")])
        result = poll_github_dispatch_requests(self.store, self.service, "bucket", client, REPO, BRANCH, PATH, NOW,
                                                registry_factory=self._registry_factory)
        self.assertTrue(result[0]["accepted"])
        task = self.store.get("tasks", "ai-development-manager", "dispatch-gh-real-1")
        command = self.store.get("commands", "ai-development-manager", "dispatch-gh-real-1")
        self.assertEqual("queued", command["status"])
        self.assertEqual("gh-real-1", command["request_id"])
        self.assertTrue(task["read_only"])

    def test_repeated_poll_of_same_file_does_not_create_second_task_or_command(self):
        client = FakeGitHubClient([request("gh-real-2", project_id="ai-development-manager")])
        first = poll_github_dispatch_requests(self.store, self.service, "bucket", client, REPO, BRANCH, PATH, NOW,
                                              registry_factory=self._registry_factory)
        second = poll_github_dispatch_requests(self.store, self.service, "bucket", client, REPO, BRANCH, PATH, NOW,
                                               registry_factory=self._registry_factory)
        self.assertTrue(first[0]["accepted"])
        self.assertTrue(second[0]["accepted"])
        self.assertEqual(first[0]["task_id"], second[0]["task_id"])
        self.assertEqual(first[0]["command_id"], second[0]["command_id"])
        # Exactly one Task/Command record exists for this identity -- a
        # second get() for the same id just returns the same one record,
        # there is no way for a duplicate to exist under a different name
        # since (task_id, command_id) is deterministic from request_id.
        task = self.store.get("tasks", "ai-development-manager", "dispatch-gh-real-2")
        self.assertIsNotNone(task)

    def test_same_request_id_from_drive_and_github_collides_in_shared_registry(self):
        """The whole point of reusing dispatch_request_registry() unforked:
        a request_id claimed via one ingress path is claimed for good,
        regardless of which path claims it first."""
        from manager.dispatch_requests import claim_dispatch_request
        registry = self._registry_factory("bucket", "ai-development-manager", "gh-real-3")
        # Simulate the Drive poller having already claimed this exact
        # request_id first.
        claim_dispatch_request(registry, "ai-development-manager", "gh-real-3",
                               "dispatch-gh-real-3", "dispatch-gh-real-3", "2026-08-27T11:00:00Z")
        client = FakeGitHubClient([request("gh-real-3", project_id="ai-development-manager")])
        result = poll_github_dispatch_requests(self.store, self.service, "bucket", client, REPO, BRANCH, PATH, NOW,
                                                registry_factory=self._registry_factory)
        # The GitHub poller's own claim attempt loses the race (claimed already
        # exists) and falls into _resolve_existing_claim -- since no Task/
        # Command was ever actually created by the simulated prior Drive
        # claim, this must fail closed rather than fabricate a fake success.
        self.assertFalse(result[0]["accepted"])


if __name__ == "__main__":
    unittest.main()
