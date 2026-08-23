import ast
import json
import os
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from manager import drive_dispatch_watcher
from manager.drive_dispatch_ingress import FOLDER_NAME
from manager.tasks import MIME_FOLDER, MIME_JSON, TaskError


OWNER = "owner@example.com"
FOLDER_ID = "ingress-folder"
BUCKET = "adm-lock-bucket"

ENV = {
    "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID": FOLDER_ID,
    "ADM_DRIVE_DISPATCH_INGRESS_OWNER": OWNER,
    "ADM_LOCK_GCS_BUCKET": BUCKET,
}


def _private_owner():
    return {
        "owners": [{"emailAddress": OWNER, "permissionId": "owner-permission", "me": True}],
        "permissions": [{"id": "owner-permission", "emailAddress": OWNER, "type": "user", "role": "owner"}],
        "ownedByMe": True,
    }


def _request_document(request_id="drive-e2e-1", **changes):
    # created_at is pinned to the real wall clock (not a fixed literal) so
    # read_request()'s own staleness check (MAX_AGE_SECONDS) passes
    # regardless of when this test suite actually runs -- run_once() uses
    # the real current time (no `now` override), unlike
    # test_drive_dispatch_ingress.py's fixed-clock unit tests.
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    value = {
        "request_id": request_id, "project_id": "ai-development-manager",
        "title": "Harmless ingress proof", "goal": "Return a short status report without changing files.",
        "preferred_provider": "codex", "priority": "normal", "created_at": created_at,
    }
    value.update(changes)
    return value


class Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return deepcopy(self.value) if not isinstance(self.value, bytes) else self.value


class FakeFiles:
    """A fake googleapiclient Drive `files()` resource restricted to exactly
    the folder+listed request files it was constructed with -- it never
    reaches real Drive and never scans anything outside the one configured
    ingress folder."""

    def __init__(self, documents):
        self.folder = {
            "id": FOLDER_ID, "name": FOLDER_NAME, "mimeType": MIME_FOLDER, "trashed": False,
            "parents": ["adm-root"], "driveId": None, **_private_owner(),
        }
        self.entries = {}
        for document in documents:
            raw = document if isinstance(document, bytes) else (json.dumps(document) + "\n").encode()
            if isinstance(document, dict):
                name = f'{document["request_id"]}.json'
            else:
                name = "malformed.json"
            file_id = f"file-{name}"
            metadata = {
                "id": file_id, "name": name, "mimeType": MIME_JSON, "trashed": False,
                "parents": [FOLDER_ID], "size": str(len(raw)), "driveId": None, **_private_owner(),
            }
            self.entries[file_id] = (metadata, raw)
        self.get_media_calls = []
        self.list_calls = []

    def get(self, fileId, fields):
        if fileId == FOLDER_ID:
            return Call(self.folder)
        metadata, _ = self.entries[fileId]
        return Call(metadata)

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return Call({"files": [metadata for metadata, _ in self.entries.values()]})

    def get_media(self, fileId):
        self.get_media_calls.append(fileId)
        _, raw = self.entries[fileId]
        return Call(raw)


class About:
    def get(self, fields):
        return Call({"user": {"emailAddress": OWNER, "permissionId": "owner-permission"}})


class FakeDriveService:
    """Fake Drive service -- never touches real Drive."""

    def __init__(self, documents=()):
        self._files = FakeFiles(list(documents))

    def files(self):
        return self._files

    def about(self):
        return About()


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
        service = FakeDriveService([_request_document()])
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1",
                                     "task_id": "dispatch-drive-e2e-1", "command_id": "dispatch-drive-e2e-1",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            result = drive_dispatch_watcher.run_once(
                build_service_fn=lambda: service, store_factory=FakeStore)
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, handler.call_count)
        self.assertTrue(result["ingress"][0]["accepted"])

    def test_two_valid_requests_are_both_evaluated(self):
        service = FakeDriveService([_request_document("drive-e2e-1"), _request_document("drive-e2e-2")])
        handler = Mock(side_effect=lambda store, svc, factory, payload: {
            "accepted": True, "request_id": payload["request_id"],
            "task_id": f'dispatch-{payload["request_id"]}', "command_id": f'dispatch-{payload["request_id"]}',
            "status": "queued",
        })
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            result = drive_dispatch_watcher.run_once(
                build_service_fn=lambda: service, store_factory=FakeStore)
        self.assertEqual(2, handler.call_count)
        self.assertEqual(2, len(result["ingress"]))
        self.assertTrue(all(item["accepted"] for item in result["ingress"]))

    def test_one_malformed_and_one_valid_valid_still_processed(self):
        service = FakeDriveService([b"{not valid json", _request_document("drive-e2e-2")])
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-2",
                                     "task_id": "dispatch-drive-e2e-2", "command_id": "dispatch-drive-e2e-2",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            result = drive_dispatch_watcher.run_once(
                build_service_fn=lambda: service, store_factory=FakeStore)
        self.assertEqual(1, handler.call_count)
        accepted = [item for item in result["ingress"] if item["accepted"]]
        rejected = [item for item in result["ingress"] if not item["accepted"]]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))

    def test_missing_bucket_env_fails_closed(self):
        with patch.dict(os.environ, env(ADM_LOCK_GCS_BUCKET=""), clear=False):
            with self.assertRaises(TaskError):
                drive_dispatch_watcher.run_once(
                    build_service_fn=lambda: (_ for _ in ()).throw(AssertionError("must not build a Drive service")),
                    store_factory=FakeStore)

    def test_missing_folder_id_env_fails_closed_via_existing_check(self):
        service = FakeDriveService([_request_document()])
        with patch.dict(os.environ, env(ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID=""), clear=False):
            with self.assertRaises(TaskError):
                drive_dispatch_watcher.run_once(build_service_fn=lambda: service, store_factory=FakeStore)

    def test_missing_owner_env_fails_closed_via_existing_check(self):
        service = FakeDriveService([_request_document()])
        with patch.dict(os.environ, env(ADM_DRIVE_DISPATCH_INGRESS_OWNER=""), clear=False):
            with self.assertRaises(TaskError):
                drive_dispatch_watcher.run_once(build_service_fn=lambda: service, store_factory=FakeStore)

    def test_drive_auth_failure_fails_closed(self):
        def boom():
            raise RuntimeError("Google Drive API initialization failed: no credentials")

        with patch.dict(os.environ, env(), clear=False):
            with self.assertRaises(RuntimeError):
                drive_dispatch_watcher.run_once(build_service_fn=boom, store_factory=FakeStore)

    def test_gcs_config_failure_surfaces_as_rejected_not_swallowed(self):
        # Bucket env is present (so run_once's own eager check passes) but
        # the underlying GCS registry construction fails once handle_dispatch
        # reaches it -- this must surface as an observable rejection of that
        # one request, not a crash and not a silent no-op.
        service = FakeDriveService([_request_document()])

        def broken_handle_dispatch(store, svc, lock_registry_factory, payload):
            # Mirrors what real handle_dispatch does: calls the injected
            # lock_registry_factory(project_id, request_id), which here
            # simulates a GCS bucket/object misconfiguration.
            lock_registry_factory(payload["project_id"], payload["request_id"])
            raise AssertionError("unreachable: registry factory should have raised")

        def failing_registry_factory(bucket, project_id, request_id):
            raise TaskError("GCS lock bucket and repo-relative object name are required")

        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_ingress.handle_dispatch", broken_handle_dispatch):
            result = drive_dispatch_watcher.run_once(
                build_service_fn=lambda: service, store_factory=FakeStore,
                poll=lambda store, svc, bucket: drive_dispatch_watcher.poll_drive_dispatch_requests(
                    store, svc, bucket, registry_factory=failing_registry_factory))
        self.assertFalse(result["ingress"][0]["accepted"])

    def test_duplicate_request_result_preserved_across_polls(self):
        service = FakeDriveService([_request_document()])
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1",
                                     "task_id": "dispatch-drive-e2e-1", "command_id": "dispatch-drive-e2e-1",
                                     "status": "completed"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            first = drive_dispatch_watcher.run_once(build_service_fn=lambda: service, store_factory=FakeStore)
            second = drive_dispatch_watcher.run_once(build_service_fn=lambda: service, store_factory=FakeStore)
        self.assertEqual(first["ingress"], second["ingress"])
        # Idempotency itself (no duplicate Task/Command) is enforced by the
        # existing GCS-CAS claim inside handle_dispatch/dispatch_requests --
        # here we only confirm the runner calls the existing path both
        # times and surfaces an identical result, never inventing its own
        # duplicate-suppression logic.
        self.assertEqual(2, handler.call_count)


class MainCliTests(unittest.TestCase):
    def test_main_once_success_returns_zero(self):
        service = FakeDriveService([_request_document()])
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1",
                                     "task_id": "dispatch-drive-e2e-1", "command_id": "dispatch-drive-e2e-1",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_ingress.handle_dispatch", handler), \
             patch("manager.drive_dispatch_watcher.build_service", lambda: service), \
             patch("manager.drive_dispatch_watcher.DriveRecords", FakeStore):
            self.assertEqual(0, drive_dispatch_watcher.main(["--once"]))

    def test_main_requires_once_flag(self):
        with self.assertRaises(SystemExit):
            drive_dispatch_watcher.main([])

    def test_main_missing_bucket_env_returns_nonzero(self):
        with patch.dict(os.environ, env(ADM_LOCK_GCS_BUCKET=""), clear=False):
            self.assertEqual(1, drive_dispatch_watcher.main(["--once"]))

    def test_main_drive_auth_failure_returns_nonzero(self):
        def boom():
            raise RuntimeError("Google Drive API initialization failed: no credentials")

        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_watcher.build_service", boom):
            self.assertEqual(1, drive_dispatch_watcher.main(["--once"]))


class NoProviderLaunchAuthorityTests(unittest.TestCase):
    """Static + behavioral proof this thin runner never gains provider
    launch authority: it must not import or reference any of the launcher/
    execution-runner/orchestrator surfaces Command Watcher alone owns."""

    FORBIDDEN_NAMES = ("ClaudeLauncher", "CodexLauncher", "AgRunner", "execution_runner", "launch_task")

    def _module_source(self):
        import inspect
        return inspect.getsource(drive_dispatch_watcher)

    def test_source_does_not_reference_provider_launch_names(self):
        source = self._module_source()
        for name in self.FORBIDDEN_NAMES:
            self.assertNotIn(name, source, f"drive_dispatch_watcher.py must never reference {name}")

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
        }
        self.assertFalse(imported_modules & forbidden_modules,
                         f"drive_dispatch_watcher.py must not import: {imported_modules & forbidden_modules}")

    def test_runner_module_has_no_claude_launcher_attribute(self):
        self.assertFalse(hasattr(drive_dispatch_watcher, "ClaudeLauncher"))

    def test_runner_module_has_no_codex_launcher_attribute(self):
        self.assertFalse(hasattr(drive_dispatch_watcher, "CodexLauncher"))

    def test_runner_module_has_no_ag_runner_attribute(self):
        self.assertFalse(hasattr(drive_dispatch_watcher, "AgRunner"))

    def test_runner_module_has_no_execution_runner_attribute(self):
        self.assertFalse(hasattr(drive_dispatch_watcher, "execution_runner"))
        self.assertFalse(hasattr(drive_dispatch_watcher, "launch_task"))

    def test_no_provider_process_started_across_full_run(self):
        """End-to-end proof over run_once(): even with a real (mocked, not
        launched) handle_dispatch call, no subprocess/process-spawning API
        is ever invoked by this module."""
        service = FakeDriveService([_request_document()])
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1",
                                     "task_id": "dispatch-drive-e2e-1", "command_id": "dispatch-drive-e2e-1",
                                     "status": "queued"})
        with patch.dict(os.environ, env(), clear=False), \
             patch("manager.drive_dispatch_ingress.handle_dispatch", handler), \
             patch("subprocess.Popen", side_effect=AssertionError("no process may be started")), \
             patch("os.startfile", side_effect=AssertionError("no process may be started"), create=True):
            result = drive_dispatch_watcher.run_once(build_service_fn=lambda: service, store_factory=FakeStore)
        self.assertEqual("ok", result["status"])


if __name__ == "__main__":
    unittest.main()
