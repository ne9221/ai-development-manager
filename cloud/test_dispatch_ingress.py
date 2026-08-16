import threading
import unittest
from unittest.mock import patch

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.command_watcher import poll_once
from manager.tasks import DriveRecords, TaskError, create_project, validate
from manager.test_dispatcher import quota as quota_fixture
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_tasks import FakeDriveService
from manager.trusted_ingress import ADMISSION_VERSION, REQUIRED_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN


def project(project_id="p1"):
    return {"project_id": project_id, "name": "Project One", "repo": "https://github.com/example/project",
            "default_branch": "main", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [],
            "current_phase": "Phase 1", "important_constraints": []}


def payload(**changes):
    value = {"request_id": "req-1", "project_id": "p1", "title": "Fix the parser", "goal": "Fix the regression in the parser",
              "priority": "normal", "constraints": {"read_only": True}}
    value.update(changes)
    return value


class SharedMemoryRegistries:
    """Test double for a GCS-backed lock_registry_factory: a fresh wrapper
    object per call, but state shared per (project_id, request_id) key --
    matching how a real GCSLockRegistry always points at the same remote
    object path across separate constructions."""
    def __init__(self):
        self.registries = {}

    def factory(self, project_id, request_id):
        key = (project_id, request_id)
        if key not in self.registries:
            self.registries[key] = MemoryClaimRegistry()
        return self.registries[key]


class DispatchIngressTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def call(self, body=None):
        return handle_dispatch(self.store, self.service, self.registries.factory, body if body is not None else payload())

    def test_valid_request_creates_queued_task_and_command(self):
        result = self.call()
        self.assertEqual({"accepted": True, "request_id": "req-1", "task_id": "dispatch-req-1",
                           "command_id": "dispatch-req-1", "status": "queued"}, result)
        task = self.store.get("tasks", "p1", "dispatch-req-1")
        validate("task", task)
        self.assertEqual("Fix the parser", task["title"])
        self.assertEqual("normal", task["priority"])
        # v1 Safe Auto-Admission: read_only and execution_policies are always
        # forced server-side, and source_context carries the trusted-ingress
        # evidence the Command Watcher independently re-verifies.
        self.assertTrue(task["read_only"])
        self.assertEqual(sorted(REQUIRED_TASK_POLICIES), sorted(task["execution_policies"]))
        self.assertEqual("Fix the regression in the parser", task["source_context"]["goal"])
        self.assertEqual(TRUSTED_INGRESS_ORIGIN, task["source_context"]["origin"])
        self.assertEqual("req-1", task["source_context"]["external_request_id"])
        self.assertEqual(ADMISSION_VERSION, task["source_context"]["admission_version"])
        command = self.store.get("commands", "p1", "dispatch-req-1")
        validate("command", command)
        self.assertEqual("queued", command["status"])
        self.assertIn(command["provider"], ("codex", "claude", "antigravity", "gemini_app"))
        self.assertEqual(TRUSTED_INGRESS_ORIGIN, command["created_via"])
        self.assertEqual(ADMISSION_VERSION, command["admission_version"])
        self.assertEqual("req-1", command["request_id"])

    def test_read_only_constraint_defaults_true_when_omitted(self):
        result = self.call(payload(request_id="req-ro", constraints={}))
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertTrue(task["read_only"])

    def test_read_only_false_is_rejected_outright(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-write", constraints={"read_only": False}))
        self.assertEqual("read_only_required", ctx.exception.code)
        with self.assertRaises(TaskError):
            self.store.get("tasks", "p1", "dispatch-req-write")

    def test_priority_is_honored(self):
        result = self.call(payload(request_id="req-pri", priority="urgent"))
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertEqual("urgent", task["priority"])

    def test_duplicate_request_id_does_not_create_two_tasks_or_commands(self):
        first = self.call()
        second = self.call()
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["command_id"], second["command_id"])
        # store.get raises unless exactly one Drive record exists for the id --
        # this is itself proof no duplicate file was created.
        self.store.get("tasks", "p1", "dispatch-req-1")
        self.store.get("commands", "p1", "dispatch-req-1")

    def test_simultaneous_duplicate_requests_create_exactly_one_task_and_command(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            barrier.wait(timeout=2)
            try:
                results.append(self.call())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.store.get("tasks", "p1", "dispatch-req-1")
        self.store.get("commands", "p1", "dispatch-req-1")

    def test_retry_after_ambiguous_transport_failure_still_completes_creation(self):
        registry = self.registries.factory("p1", "req-timeout")
        registry.ambiguous_queue.append(ConnectionError("simulated client timeout after server-side success"))
        first = self.call(payload(request_id="req-timeout"))
        self.store.get("tasks", "p1", first["task_id"])
        self.store.get("commands", "p1", first["command_id"])
        second = self.call(payload(request_id="req-timeout"))
        self.assertEqual(first["task_id"], second["task_id"])

    def test_missing_auth_fields_rejected(self):
        for bad in (
            {"project_id": "p1", "title": "t", "goal": "g"},  # missing request_id
            {"request_id": "r", "title": "t", "goal": "g"},   # missing project_id
            {"request_id": "r", "project_id": "p1", "goal": "g"},  # missing title
            {"request_id": "r", "project_id": "p1", "title": "t"},  # missing goal
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(DispatchIngressError) as ctx:
                    self.call(bad)
                self.assertEqual("malformed_request", ctx.exception.code)

    def test_malformed_extra_field_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(shell="rm -rf /"))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_executable_and_env_and_config_path_fields_rejected(self):
        for dangerous in ("executable", "shell", "env", "working_directory", "claude_config_path",
                          "provider", "account_id", "cwd", "command"):
            with self.subTest(field=dangerous):
                with self.assertRaises(DispatchIngressError) as ctx:
                    self.call(payload(**{dangerous: "/etc/passwd"}))
                self.assertEqual("malformed_request", ctx.exception.code)

    def test_path_traversal_in_ids_rejected(self):
        for bad_id in ("../../etc/passwd", "p1/../p2", "p1/../../secret"):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(DispatchIngressError) as ctx:
                    self.call(payload(request_id=bad_id))
                self.assertEqual("malformed_request", ctx.exception.code)

    def test_unknown_project_fails_closed(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(project_id="does-not-exist"))
        self.assertEqual("unknown_project", ctx.exception.code)

    def test_invalid_priority_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(priority="asap"))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_idempotency_backend_unavailable_fails_closed(self):
        def broken_factory(project_id, request_id):
            raise TaskError("simulated GCS bucket not configured")
        with self.assertRaises(DispatchIngressError) as ctx:
            handle_dispatch(self.store, self.service, broken_factory, payload())
        self.assertEqual("idempotency_backend_unavailable", ctx.exception.code)

    def test_queued_command_is_recognized_by_command_watcher_and_left_alone(self):
        """The Command Watcher must recognize the record contract this
        ingress writes, and -- with no static allowlist entry and no
        ADM_LOCK_GCS_BUCKET configured to corroborate the trusted-ingress
        evidence (manager.trusted_ingress) -- must reject it as
        not_allowlisted rather than launching anything. This proves the
        ingress alone, without the separate idempotency-record cross-check
        wired up, can never grant itself launch authority. The positive
        case (evidence + corroborating record both present) is covered in
        manager/test_command_watcher.py's trusted-ingress admission tests."""
        self.call()
        results = poll_once(self.store, self.service, allowlist=frozenset())
        self.assertEqual(1, len(results))
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, results[0])
        command = self.store.get("commands", "p1", "dispatch-req-1")
        self.assertEqual("queued", command["status"])


if __name__ == "__main__":
    unittest.main()
