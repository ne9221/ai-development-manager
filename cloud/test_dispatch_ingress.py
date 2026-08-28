import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.command_watcher import poll_once, process_command
from manager.dispatch_requests import claim_dispatch_request
from manager.dispatcher import dispatch as dispatcher_dispatch
from manager.tasks import DriveRecords, TaskError, create_project, now_iso, update_task, validate
from manager.test_command_watcher import CommandWatcherTests
from manager.test_dispatcher import quota as quota_fixture
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_tasks import FakeDriveService
from manager.trusted_ingress import (
    ADMISSION_VERSION, ADMISSION_VERSION_V2_REPO_WRITE, REQUIRED_REPO_WRITE_TASK_POLICIES,
    REQUIRED_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN,
)


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

    def test_local_action_creates_codex_command_without_provider_selection(self):
        with patch("cloud.dispatch_ingress.dispatcher_dispatch") as dispatch_mock:
            result = self.call(payload(request_id="local-action-1", local_action="OPEN_EXISTING_ADM_UI"))
        dispatch_mock.assert_not_called()
        task = self.store.get("tasks", "p1", result["task_id"])
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertIs(False, task["needs_repo_edit"])
        self.assertEqual("codex", command["provider"])
        self.assertEqual("OPEN_EXISTING_ADM_UI", command["action"])
        validate("command", command)

    def test_local_action_and_repo_write_are_rejected(self):
        with self.assertRaises(DispatchIngressError):
            self.call(payload(request_id="local-action-write", local_action="OPEN_EXISTING_ADM_UI",
                              constraints={"read_only": False}, repo_write={
                                  "allowed_paths": ["manager/foo.py"], "baseline_head": "a" * 40,
                                  "repo": "https://github.com/example/project"}))

    def test_dispatch_request_schema_rejects_unknown_local_action(self):
        bad = payload(request_id="local-action-schema", local_action="UNKNOWN")
        bad["created_at"] = "2026-08-28T00:00:00Z"
        with self.assertRaises(TaskError):
            validate("dispatch_request", bad)

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

    def test_request_created_at_is_threaded_through_to_the_durable_claim_record(self):
        """Blocker 2: handle_dispatch()'s optional request_created_at kwarg
        (e.g. manager.drive_dispatch_ingress's Drive JSON body created_at)
        must reach the durable claim record as its own `request_created_at`
        field, separate from `ingress_first_observed_at` (this call's own
        now_iso() moment)."""
        handle_dispatch(self.store, self.service, self.registries.factory, payload(),
                        request_created_at="2026-08-23T23:57:00Z")
        document = self.registries.factory("p1", "req-1").document
        self.assertEqual("2026-08-23T23:57:00Z", document["request_created_at"])
        self.assertIsInstance(document["ingress_first_observed_at"], str)
        self.assertNotEqual("2026-08-23T23:57:00Z", document["ingress_first_observed_at"])

    def test_request_created_at_omitted_leaves_it_none(self):
        self.call()  # no request_created_at passed
        document = self.registries.factory("p1", "req-1").document
        self.assertIsNone(document["request_created_at"])

    def test_automatic_selection_persists_only_the_reliable_provider(self):
        document = quota_fixture(80, 90)
        next(item for item in document["providers"] if item["provider"] == "claude")["last_updated"] = "2020-01-01T00:00:00Z"
        with patch("manager.dispatcher.read_drive_status", return_value=document):
            result = self.call(payload(request_id="req-reliable"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("codex", command["provider"])

    def test_no_eligible_provider_releases_claim_for_same_request_id_retry(self):
        stale = quota_fixture(updated="2020-01-01T00:00:00Z")
        request = payload(request_id="req-no-eligible")
        with patch("manager.dispatcher.read_drive_status", return_value=stale):
            with self.assertRaisesRegex(TaskError, "no eligible provider"):
                self.call(request)
        for area in ("tasks", "commands"):
            with self.assertRaises(TaskError):
                self.store.get(area, "p1", "dispatch-req-no-eligible")
        fresh_codex = quota_fixture(80, 90)
        next(item for item in fresh_codex["providers"] if item["provider"] == "claude")["last_updated"] = "2020-01-01T00:00:00Z"
        with patch("manager.dispatcher.read_drive_status", return_value=fresh_codex):
            result = self.call(request)
        self.assertEqual("dispatch-req-no-eligible", result["task_id"])
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("codex", command["provider"])

    def test_failure_before_task_creation_marks_failed_truth_and_stays_retryable(self):
        """P0 dispatch-two-tick-final: a definite (created_by_this_call),
        pre-artifact failure must leave durable "failed" + failure_reason
        truth on the claim record instead of deleting it (the old
        release-based design), so a caller can observe the request was
        received and definitively failed without waiting for/polling a
        Task/Command that will never exist -- and a retry of the same
        request_id must still succeed once the underlying cause is fixed."""
        request = payload(request_id="req-pre-artifact-failure")
        with patch("cloud.dispatch_ingress.dispatcher_dispatch", side_effect=TaskError("simulated pre-task failure")):
            with self.assertRaisesRegex(TaskError, "pre-task failure"):
                self.call(request)
        document = self.registries.factory("p1", "req-pre-artifact-failure").document
        self.assertIsNotNone(document)
        self.assertEqual("failed", document["status"])
        self.assertIn("pre-task failure", document["failure_reason"])
        self.assertEqual("dispatch-req-pre-artifact-failure", document["task_id"])
        result = self.call(request)
        self.assertEqual("dispatch-req-pre-artifact-failure", result["task_id"])
        self.assertEqual("dispatched", self.registries.factory("p1", "req-pre-artifact-failure").document["status"])

    def test_request_visible_as_accepted_before_slow_dispatch_resolves(self):
        """P0 dispatch-two-tick-final: the durable claim record must show
        status="accepted" the instant the request is received -- written in
        the SAME create-if-absent call claim_dispatch_request() makes,
        strictly before manager.dispatcher.dispatch() (quota read +
        execution-history lookup + Task creation) ever runs. A caller that
        queries the claim record while dispatch() is still resolving (slow
        quota, slow execution history, or just ordinary latency) must see
        durable "accepted" truth, not nothing."""
        seen_while_dispatching = {}

        def slow_dispatch(store, service, request, **kwargs):
            # Snapshot the claim record's own state from *inside* dispatch()
            # -- i.e. while the request -> Task creation is still in flight --
            # then delegate to the real dispatcher so the call still succeeds.
            seen_while_dispatching["document"] = self.registries.factory("p1", "req-slow").document
            return dispatcher_dispatch(store, service, request, **kwargs)

        with patch("cloud.dispatch_ingress.dispatcher_dispatch", side_effect=slow_dispatch):
            result = self.call(payload(request_id="req-slow"))
        self.assertEqual("accepted", seen_while_dispatching["document"]["status"])
        self.assertEqual("dispatch-req-slow", seen_while_dispatching["document"]["task_id"])
        self.assertTrue(result["accepted"])
        self.assertEqual("dispatched", self.registries.factory("p1", "req-slow").document["status"])

    def test_ingress_dispatch_uses_bounded_history_lookup(self):
        """P0 dispatch-two-tick-final: cloud.dispatch_ingress.handle_dispatch()
        must bound manager.dispatcher.dispatch()'s own historical-estimate
        lookup on its request -> Task/Command creation path -- the same
        unbounded manager.executions.list_executions() call
        fix/dispatch-two-tick-observability-20260824 already bounded on the
        separate claimed -> reserved (execution_runner.launch_task()) path,
        but never applied here. Without this, a growing project execution
        history delays the Task's own FIRST WRITE, not just a launch-time
        estimate."""
        before = time.monotonic()
        with patch("cloud.dispatch_ingress.dispatcher_dispatch", wraps=dispatcher_dispatch) as wrapped:
            self.call(payload(request_id="req-bounded-history"))
        after = time.monotonic()
        wrapped.assert_called_once()
        history_deadline = wrapped.call_args.kwargs["history_deadline"]
        self.assertIsNotNone(history_deadline)
        from cloud.dispatch_ingress import INGRESS_DISPATCH_HISTORY_BUDGET_SECONDS
        self.assertGreaterEqual(history_deadline, before + INGRESS_DISPATCH_HISTORY_BUDGET_SECONDS - 1.0)
        self.assertLessEqual(history_deadline, after + INGRESS_DISPATCH_HISTORY_BUDGET_SECONDS + 1.0)

    def test_concurrent_retry_of_failed_claim_creates_exactly_one_task_and_command(self):
        """P0 dispatch-two-tick-final: two callers racing to retry the same
        definitively-failed request_id must produce exactly one Task/Command
        -- only one may win the CAS transition of the claim's own status
        from "failed" back to "accepted"; the loser must fall back to the
        ordinary bounded-polling resolution and observe the winner's result,
        never attempt its own concurrent create()."""
        request = payload(request_id="req-concurrent-retry")
        with patch("cloud.dispatch_ingress.dispatcher_dispatch", side_effect=TaskError("simulated failure")):
            with self.assertRaises(TaskError):
                self.call(request)
        self.assertEqual("failed", self.registries.factory("p1", "req-concurrent-retry").document["status"])

        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            barrier.wait(timeout=2)
            try:
                results.append(self.call(request))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        succeeded = [r for r in results if r]
        self.assertGreaterEqual(len(succeeded), 1)
        for result in succeeded:
            self.assertEqual("dispatch-req-concurrent-retry", result["task_id"])
        self.store.get("tasks", "p1", "dispatch-req-concurrent-retry")
        self.store.get("commands", "p1", "dispatch-req-concurrent-retry")

    def test_ambiguous_claim_is_never_rolled_back(self):
        request = payload(request_id="req-ambiguous-no-provider")
        registry = self.registries.factory("p1", "req-ambiguous-no-provider")
        registry.ambiguous_queue.append(ConnectionError("timeout after create"))
        stale = quota_fixture(updated="2020-01-01T00:00:00Z")
        with patch("manager.dispatcher.read_drive_status", return_value=stale):
            with self.assertRaisesRegex(TaskError, "no eligible provider"):
                self.call(request)
        self.assertIsNotNone(registry.document)
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(request)
        self.assertEqual("dispatch_incomplete", ctx.exception.code)

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

    def test_orphan_claim_without_backing_task_or_command_does_not_report_fake_success(self):
        """Regression for the live-discovered bug (2026-08-17 golden E2E):
        a claim record can exist -- e.g. the original request died after
        the CAS claim landed but before Task/Command creation ever ran --
        with no backing Task/Command ever created. A retry with the same
        request_id must never report accepted: true for state that was
        never actually persisted; it must fail closed instead."""
        registry = self.registries.factory("p1", "req-orphan")
        claim_dispatch_request(registry, "p1", "req-orphan", "dispatch-req-orphan", "dispatch-req-orphan", now_iso())
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-orphan"))
        self.assertEqual("dispatch_incomplete", ctx.exception.code)
        with self.assertRaises(TaskError):
            self.store.get("tasks", "p1", "dispatch-req-orphan")
        with self.assertRaises(TaskError):
            self.store.get("commands", "p1", "dispatch-req-orphan")

    def test_partial_state_task_exists_but_command_missing_fails_closed(self):
        """The original claimant died between finishing the Task write and
        the later, separate Command write -- Task alone existing must not
        be mistaken for a completed, retryable-as-success dispatch."""
        registry = self.registries.factory("p1", "req-partial")
        claim_dispatch_request(registry, "p1", "req-partial", "dispatch-req-partial", "dispatch-req-partial", now_iso())
        internal_request = {
            "project_id": "p1", "task_id": "dispatch-req-partial", "title": "Partial",
            "task_type": "general", "complexity": "medium",
            "source_context": {"origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": "req-partial",
                                "goal": "g", "admission_version": ADMISSION_VERSION},
        }
        dispatcher_dispatch(self.store, self.service, internal_request)
        update_task(self.store, "p1", "dispatch-req-partial", priority="normal",
                    read_only=True, execution_policies=sorted(REQUIRED_TASK_POLICIES))
        self.store.get("tasks", "p1", "dispatch-req-partial")  # sanity: Task really was written
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-partial"))
        self.assertEqual("dispatch_incomplete", ctx.exception.code)
        with self.assertRaises(TaskError):
            self.store.get("commands", "p1", "dispatch-req-partial")

    def test_command_with_mismatched_request_id_fails_closed(self):
        """A Command found at the claimed id whose own request_id linkage
        does not match the request being resolved must not be trusted as
        that request's result (request_id collision / corrupted record)."""
        self.call(payload(request_id="req-mismatch"))
        command = self.store.get("commands", "p1", "dispatch-req-mismatch")
        command["request_id"] = "some-other-request"
        self.store.put("commands", "p1", "dispatch-req-mismatch", command)
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-mismatch"))
        self.assertEqual("dispatch_state_inconsistent", ctx.exception.code)

    def test_command_with_mismatched_task_id_fails_closed(self):
        self.call(payload(request_id="req-mismatch-task"))
        command = self.store.get("commands", "p1", "dispatch-req-mismatch-task")
        command["task_id"] = "dispatch-some-other-task"
        self.store.put("commands", "p1", "dispatch-req-mismatch-task", command)
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-mismatch-task"))
        self.assertEqual("dispatch_state_inconsistent", ctx.exception.code)

    def test_malformed_claim_record_fails_closed(self):
        registry = self.registries.factory("p1", "req-malformed")
        registry.document = {"schema_version": "0.1.0", "project_id": "p1"}  # missing required fields
        registry.generation = 1
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-malformed"))
        self.assertEqual("idempotency_backend_unavailable", ctx.exception.code)

    def test_valid_idempotent_retry_returns_existing_result_without_rewriting(self):
        first = self.call(payload(request_id="req-idem"))
        command_before = self.store.get("commands", "p1", "dispatch-req-idem")
        second = self.call(payload(request_id="req-idem"))
        self.assertEqual(first, second)
        self.assertEqual(command_before, self.store.get("commands", "p1", "dispatch-req-idem"))

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


class DirectDispatchClaudeAccountIdentityTests(unittest.TestCase):
    """M0: manager.dispatcher.dispatch() resolves a specific account_id when
    it auto-selects among named Claude accounts by quota forecast, and this
    ingress persists both that resolved account_id and (P0
    claude-auth-routing-truth) the caller's actual requested_account_id
    (null for automatic selection) on the Command record, so a later,
    genuinely explicit request is never confused with the dispatcher's own
    provisional pick -- see manager.command_watcher._explicit_account_id."""

    REGISTRY = [
        {"account_id": "claude-a", "enabled": True, "config_dir": r"C:\accounts\a\.claude"},
        {"account_id": "claude-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
    ]

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        self.registries = SharedMemoryRegistries()
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        reset_soon = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def claude_account(account_id, remaining):
            return {"provider": "claude", "account_id": account_id, "display_name": "claude",
                     "collection_mode": "automatic", "source": "test", "source_type": "official",
                     "confidence": "official", "last_updated": fresh, "status": "ok",
                     "windows": [{"name": "primary", "remaining_percent": remaining, "used_percent": 100 - remaining, "resets_at": reset_soon}]}

        doc = {
            "schema_version": "0.1.0", "generated_at": fresh,
            "providers": [
                claude_account("claude-a", 90), claude_account("claude-b", 40),
                {"provider": "codex", "display_name": "codex", "collection_mode": "automatic",
                 "source": "test", "source_type": "official", "confidence": "official", "last_updated": fresh,
                 "status": "ok", "windows": [{"name": "primary", "remaining_percent": 5, "used_percent": 95, "resets_at": None}]},
            ],
        }
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=doc)
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def call(self, body=None):
        return handle_dispatch(self.store, self.service, self.registries.factory, body if body is not None else payload())

    def test_dispatcher_selected_claude_account_id_survives_onto_command(self):
        result = self.call(payload(request_id="req-acct"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude", command["provider"])
        self.assertEqual("claude-a", command["account_id"])

    def test_command_account_id_is_not_launch_authority_for_automatic_selection(self):
        """P0 claude-auth-routing-truth: dispatcher's own auto-pick (stamped
        onto command.account_id here as "claude-a") is a provisional
        recommendation, not caller intent -- this request never asked for a
        specific account (command.requested_account_id stays null). The
        Command Watcher must route it through the AUTOMATIC path
        (account_id=None, claude_accounts=REGISTRY forwarded so launch_task
        can re-resolve against live auth/fresh quota at launch time), never
        treat command.account_id as if it were an explicit ask -- that was
        the exact real-production bug this fix closes: an account picked
        from a frozen/stale snapshot at dispatch time could otherwise reach
        the provider launch unchecked, skipping the live auth-ready and
        quota-freshness re-check automatic routing is supposed to get.
        (Also the exact bug the Claude fleet eligibility fix's sibling-aware
        selection assumes is closed -- see manager.command_watcher._explicit_account_id.)"""
        result = self.call(payload(request_id="req-acct2"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude-a", command["account_id"])
        self.assertIsNone(command["requested_account_id"])
        runner = Mock(return_value=CommandWatcherTests.complete("exec-1"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY), \
             patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}):
            outcome = process_command(
                self.store, self.service, command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True,
                quota_check=lambda service: True,
                ingress_registry_factory=lambda bucket, project_id, request_id: self.registries.factory(project_id, request_id),
            )
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertIsNone(kwargs.get("account_id"))
        self.assertEqual(self.REGISTRY, kwargs.get("claude_accounts"))
        self.assertEqual("completed", outcome["status"])
        runner = Mock(return_value=CommandWatcherTests.complete("exec-1"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY), \
             patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}):
            outcome = process_command(
                self.store, self.service, command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True,
                quota_check=lambda service: True,
                ingress_registry_factory=lambda bucket, project_id, request_id: self.registries.factory(project_id, request_id),
            )
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertIsNone(kwargs.get("account_id"))
        self.assertEqual(self.REGISTRY, kwargs.get("claude_accounts"))
        self.assertEqual("completed", outcome["status"])

    def test_command_requested_account_id_is_launch_authority_for_explicit_selection(self):
        # A genuine caller-explicit request (requested_account_id set) is
        # the one case that must still reach launch_task's explicit-account
        # path unchanged -- proving the distinction cuts both ways.
        with _registry_env(self.REGISTRY):
            result = self.call(payload(request_id="req-acct3", provider="claude", account_id="claude-b"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude-b", command["account_id"])
        self.assertEqual("claude-b", command["requested_account_id"])
        runner = Mock(return_value=CommandWatcherTests.complete("exec-1"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY), \
             patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}):
            outcome = process_command(
                self.store, self.service, command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True,
                quota_check=lambda service: True,
                ingress_registry_factory=lambda bucket, project_id, request_id: self.registries.factory(project_id, request_id),
            )
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("claude-b", kwargs.get("account_id"))
        self.assertEqual("completed", outcome["status"])


@contextlib.contextmanager
def _registry_env(entries):
    """Point CLAUDE_ACCOUNTS_CONFIG at a temp registry file for the duration
    of the block; restores the previous environment on exit."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "accounts.json"
        path.write_text(json.dumps({"accounts": entries}), encoding="utf-8")
        with patch.dict(os.environ, {"CLAUDE_ACCOUNTS_CONFIG": str(path)}, clear=False):
            yield


class ExplicitProviderAccountRoutingTests(unittest.TestCase):
    """P0-2: a trusted caller (the ChatGPT-facing Direct Dispatch ingress)
    must be able to request a specific provider and, for Claude, a specific
    named account -- and have that request actually respected end to end,
    never silently overridden by the quota-aware auto-selector and never
    silently substituted for a different provider/account."""

    REGISTRY = [
        {"account_id": "claude-a", "enabled": True, "config_dir": r"C:\accounts\a\.claude"},
        {"account_id": "claude-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        {"account_id": "claude-disabled", "enabled": False, "config_dir": r"C:\accounts\d\.claude"},
    ]

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        self.registries = SharedMemoryRegistries()
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = {
            "schema_version": "0.1.0", "generated_at": fresh,
            "providers": [
                {"provider": "claude", "account_id": "claude-a", "display_name": "claude", "collection_mode": "automatic",
                 "source": "test", "source_type": "official", "confidence": "official", "last_updated": fresh,
                 "status": "ok", "windows": [{"name": "primary", "remaining_percent": 90, "used_percent": 10, "resets_at": None}]},
                {"provider": "claude", "account_id": "claude-b", "display_name": "claude", "collection_mode": "automatic",
                 "source": "test", "source_type": "official", "confidence": "official", "last_updated": fresh,
                 "status": "ok", "windows": [{"name": "primary", "remaining_percent": 40, "used_percent": 60, "resets_at": None}]},
                {"provider": "codex", "display_name": "codex", "collection_mode": "automatic",
                 "source": "test", "source_type": "official", "confidence": "official", "last_updated": fresh,
                 "status": "ok", "windows": [{"name": "primary", "remaining_percent": 95, "used_percent": 5, "resets_at": None}]},
            ],
        }
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=doc)
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def call(self, body=None):
        return handle_dispatch(self.store, self.service, self.registries.factory, body if body is not None else payload())

    def test_explicit_claude_a_gets_exact_account(self):
        with _registry_env(self.REGISTRY):
            result = self.call(payload(request_id="req-a", provider="claude", account_id="claude-a"))
        command = self.store.get("commands", "p1", result["command_id"])
        validate("command", command)
        self.assertEqual("claude", command["provider"])
        self.assertEqual("claude-a", command["account_id"])
        self.assertEqual("claude", command["requested_provider"])
        self.assertEqual("claude-a", command["requested_account_id"])
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertEqual("claude-a", task["account_id"])

    def test_explicit_claude_b_gets_exact_account(self):
        with _registry_env(self.REGISTRY):
            result = self.call(payload(request_id="req-b", provider="claude", account_id="claude-b"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude", command["provider"])
        self.assertEqual("claude-b", command["account_id"])
        self.assertEqual("claude-b", command["requested_account_id"])
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertEqual("claude-b", task["account_id"])

    def test_explicit_provider_is_never_overridden_by_auto_selector(self):
        # Quota fixture strongly favors codex; without the explicit route
        # auto-selection would pick codex (proven by the sibling no-provider
        # request below). The explicit request must still win.
        with _registry_env(self.REGISTRY):
            result = self.call(payload(request_id="req-explicit", provider="claude", account_id="claude-a"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude", command["provider"])

        auto_result = self.call(payload(request_id="req-auto"))
        auto_command = self.store.get("commands", "p1", auto_result["command_id"])
        self.assertEqual("codex", auto_command["provider"])
        self.assertIsNone(auto_command["requested_provider"])
        self.assertIsNone(auto_command["requested_account_id"])

    def test_explicit_antigravity_provider_is_accepted(self):
        result = self.call(payload(request_id="req-ag", provider="antigravity"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("antigravity", command["provider"])
        self.assertEqual("antigravity", command["requested_provider"])

    def test_invalid_provider_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-badprov", provider="gemini"))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_account_id_requires_explicit_claude_provider(self):
        # provider omitted entirely (would otherwise auto-select) -- account_id
        # alone is not enough to imply provider=claude.
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-noprov", account_id="claude-a"))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_account_id_with_mismatched_provider_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-mismatch2", provider="codex", account_id="claude-a"))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_unknown_claude_account_id_fails_closed(self):
        with _registry_env(self.REGISTRY):
            with self.assertRaises(DispatchIngressError) as ctx:
                self.call(payload(request_id="req-unknown", provider="claude", account_id="claude-nonexistent"))
        self.assertEqual("unknown_account", ctx.exception.code)
        with self.assertRaises(TaskError):
            self.store.get("tasks", "p1", "dispatch-req-unknown")
        with self.assertRaises(TaskError):
            self.store.get("commands", "p1", "dispatch-req-unknown")

    def test_disabled_claude_account_id_fails_closed(self):
        with _registry_env(self.REGISTRY):
            with self.assertRaises(DispatchIngressError) as ctx:
                self.call(payload(request_id="req-disabled", provider="claude", account_id="claude-disabled"))
        self.assertEqual("unknown_account", ctx.exception.code)

    def test_no_registry_configured_fails_closed_for_explicit_account(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_ACCOUNTS_CONFIG", None)
            with self.assertRaises(DispatchIngressError) as ctx:
                self.call(payload(request_id="req-noreg", provider="claude", account_id="claude-a"))
            self.assertEqual("unknown_account", ctx.exception.code)

    def test_explicit_provider_without_account_id_does_not_require_registry(self):
        # provider=claude alone (no account_id) must still work via
        # dispatcher's own auto-account-selection-among-named-accounts path
        # (or the legacy single-account path); it must not be rejected just
        # because no CLAUDE_ACCOUNTS_CONFIG registry is configured -- that
        # registry is only consulted when an explicit account_id is given.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_ACCOUNTS_CONFIG", None)
            result = self.call(payload(request_id="req-provonly", provider="claude"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude", command["provider"])
        self.assertEqual("claude", command["requested_provider"])
        self.assertIsNone(command["requested_account_id"])

    def test_dispatch_mismatch_fails_closed_without_persisting_command(self):
        # Defense in depth: even if dispatcher_dispatch() somehow resolved a
        # different provider than requested, the ingress must fail closed
        # rather than persist a Command that silently substitutes it.
        with _registry_env(self.REGISTRY), \
             patch("cloud.dispatch_ingress.dispatcher_dispatch") as fake_dispatch:
            fake_dispatch.return_value = {
                "provider": "codex", "account_id": None, "model": None, "fallback_model": None,
                "mode": "code", "effort": "medium", "selection_reason": [], "quota_evidence": {},
            }
            with self.assertRaises(DispatchIngressError) as ctx:
                self.call(payload(request_id="req-swapped", provider="claude", account_id="claude-a"))
        self.assertEqual("dispatch_state_inconsistent", ctx.exception.code)
        with self.assertRaises(TaskError):
            self.store.get("commands", "p1", "dispatch-req-swapped")


class ReadOnlyNeedsRepoEditContractTests(unittest.TestCase):
    """A brand-new Direct Dispatch Task is always read_only=True (forced,
    server-side, unconditionally -- see handle_dispatch's read_only_required
    rejection above). manager.dispatcher.dispatch() has no read_only concept
    of its own and defaults needs_repo_edit=True for any new task that
    doesn't specify it, which used to leave every such Task self-
    contradictory (read_only=True, needs_repo_edit=True) -- a contract
    manager.execution_lifecycle.enter_running_gate() correctly refuses to
    ever launch. These tests prove the ingress now produces an internally
    consistent, launchable contract, and that the authoritative gate itself
    still rejects a genuinely contradictory snapshot unchanged."""

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

    def test_brand_new_direct_dispatch_task_gets_needs_repo_edit_false(self):
        result = self.call(payload(request_id="req-contract"))
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertTrue(task["read_only"])
        self.assertFalse(task["needs_repo_edit"])

    def test_direct_dispatch_task_snapshot_passes_the_read_only_running_gate(self):
        from manager.execution_lifecycle import enter_running_gate
        from manager.executions import reserve_execution
        from manager.test_task_claims import MemoryClaimRegistry

        result = self.call(payload(request_id="req-gate"))
        task_id = result["task_id"]
        command = self.store.get("commands", "p1", task_id)
        reserve_execution(self.store, "p1", task_id, task_id, command["provider"], {"decision": "fresh"})
        # Must not raise: the persisted Task contract is internally
        # consistent (read_only=True, needs_repo_edit=False), so the
        # authoritative gate actually admits it for a read-only launch.
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_fixture()):
            outcome = enter_running_gate(
                self.store, object(), None, "p1", task_id, task_id, command["provider"], "read_only",
                started_at=now_iso(), task_claim_registry=MemoryClaimRegistry(),
            )
        self.assertEqual("running", outcome["execution"]["status"])

    def test_dispatcher_default_for_other_callers_is_unchanged(self):
        # Scope check: the fix lives in the ingress's own internal_request
        # construction, not in manager.dispatcher.dispatch()'s general
        # default -- a direct dispatcher.dispatch() call (the shape every
        # other caller, e.g. the CLI/runtime_bridge, still uses) keeps
        # defaulting needs_repo_edit=True exactly as before.
        store = DriveRecords(FakeDriveService())
        create_project(store, project())
        dispatcher_dispatch(store, self.service, {
            "project_id": "p1", "task_id": "unrelated-task", "title": "Unrelated direct dispatcher call",
            "task_type": "implementation", "complexity": "medium",
        })
        task = store.get("tasks", "p1", "unrelated-task")
        self.assertTrue(task["needs_repo_edit"])

    def test_needs_repo_edit_cannot_be_smuggled_through_the_payload(self):
        # Adversarial: needs_repo_edit is forced server-side (False, to match
        # the also-forced read_only=True) -- a caller must not be able to
        # override it, in either direction, via the request payload.
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-smuggle", needs_repo_edit=True))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_authoritative_gate_still_rejects_a_genuinely_contradictory_snapshot(self):
        # The gate itself is untouched: if some other path ever again
        # produces read_only=True with needs_repo_edit=True, it must still
        # fail closed exactly as before.
        from manager.execution_lifecycle import enter_running_gate
        from manager.executions import reserve_execution
        from manager.test_task_claims import MemoryClaimRegistry

        result = self.call(payload(request_id="req-corrupt"))
        task_id = result["task_id"]
        command = self.store.get("commands", "p1", task_id)
        corrupted = self.store.get("tasks", "p1", task_id)
        corrupted["needs_repo_edit"] = True
        validate("task", corrupted)
        self.store.put("tasks", "p1", task_id, corrupted)
        reserve_execution(self.store, "p1", task_id, task_id, command["provider"], {"decision": "fresh"})
        with self.assertRaises(TaskError):
            enter_running_gate(
                self.store, object(), None, "p1", task_id, task_id, command["provider"], "read_only",
                started_at=now_iso(), task_claim_registry=MemoryClaimRegistry(),
            )


class TrustedRetryLinkageTests(unittest.TestCase):
    """P0: a trusted caller may ask handle_dispatch() to retry a specific
    prior execution of an *existing* task instead of creating a brand-new
    one -- but only under strict, server-side-established linkage. Nothing
    here is taken on the caller's word: task_id is derived from the
    validated prior execution record, eligibility is re-derived from
    manager.executions.retry_eligible(), retry_count is always computed
    server-side, and provider/account_id are inherited verbatim from the
    prior attempt's own Command."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        create_project(self.store, project(project_id="p2"))
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def call(self, body=None):
        return handle_dispatch(self.store, self.service, self.registries.factory, body if body is not None else payload())

    def _original_dispatch(self, request_id="orig-1", project_id="p1"):
        return self.call(payload(request_id=request_id, project_id=project_id))

    def _reserve_and_terminalize(self, task_id, execution_id, project_id="p1", status="failed"):
        """Real reserve_execution(), then a real terminal transition,
        matching an execution that actually ran and finished."""
        from manager.executions import reserve_execution
        reserved = reserve_execution(self.store, project_id, task_id, execution_id, "codex", {"decision": "fresh"})
        terminal = {**reserved, "status": status, "started_at": now_iso(), "completed_at": now_iso(),
                    "finished_at": now_iso(), "elapsed_minutes": 1, "quota_before": {}, "quota_after": {},
                    "quota_delta": {}, "source_confidence": "official", "access": "read_only", "lease_evidence": None,
                    "cleanup_evidence": {"persistence": "complete", "task_claim_release": "released", "writer_release": "not_required"}}
        validate("execution", terminal)
        self.store.put("executions", project_id, execution_id, terminal)
        return terminal

    def _reserve_and_cancel_via_prelaunch(self, task_id, execution_id, project_id="p1"):
        """Real reserve_execution(), then the exact shape
        manager.command_watcher._reconcile_active's real prelaunch-cleanup
        call to cancel_reserved_execution() produces."""
        from manager.executions import cancel_reserved_execution, reserve_execution
        reserve_execution(self.store, project_id, task_id, execution_id, "codex", {"decision": "fresh"})
        return cancel_reserved_execution(self.store, MemoryClaimRegistry(), project_id, execution_id,
                                          "prelaunch failure left a reservation without provider authority")

    def _mark_command_prelaunch_failed(self, command_id, execution_id, project_id="p1"):
        command = self.store.get("commands", project_id, command_id)
        command.update(status="failed", execution_id=execution_id, completed_at=now_iso(),
                        result={"status": "error", "execution_id": execution_id, "session_id": None, "error_kind": "prelaunch_failed"})
        validate("command", command)
        self.store.put("commands", project_id, command_id, command)
        return command

    def _mark_command_failed(self, command_id, execution_id, project_id="p1", error_kind="provider_error"):
        command = self.store.get("commands", project_id, command_id)
        command.update(status="failed", execution_id=execution_id, completed_at=now_iso(),
                        result={"status": "error", "execution_id": execution_id, "session_id": None, "error_kind": error_kind})
        validate("command", command)
        self.store.put("commands", project_id, command_id, command)
        return command

    def test_failed_execution_is_retryable_and_inherits_prior_provider(self):
        original = self._original_dispatch()
        task_id = original["task_id"]
        self._reserve_and_terminalize(task_id, task_id, status="failed")
        prior_command = self._mark_command_failed(task_id, task_id)
        result = self.call(payload(request_id="retry-1", retry_of_execution_id=task_id))
        self.assertTrue(result["accepted"])
        self.assertEqual(task_id, result["task_id"])  # same existing task, not a new one
        retry_command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual(1, retry_command["retry_count"])
        self.assertEqual(task_id, retry_command["retry_of_execution_id"])
        self.assertEqual(prior_command["provider"], retry_command["provider"])
        self.assertEqual("queued", retry_command["status"])
        self.assertEqual(TRUSTED_INGRESS_ORIGIN, retry_command["created_via"])
        self.assertEqual(ADMISSION_VERSION, retry_command["admission_version"])
        # No new Task was created -- the original one is untouched.
        with self.assertRaises(TaskError):
            self.store.get("tasks", "p1", "dispatch-retry-1")

    def test_prelaunch_cancelled_execution_is_retryable(self):
        original = self._original_dispatch(request_id="orig-2")
        task_id = original["task_id"]
        self._reserve_and_cancel_via_prelaunch(task_id, task_id)
        self._mark_command_prelaunch_failed(task_id, task_id)
        result = self.call(payload(request_id="retry-2", retry_of_execution_id=task_id))
        self.assertTrue(result["accepted"])
        retry_command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual(1, retry_command["retry_count"])

    def test_ordinary_cancelled_execution_is_rejected(self):
        original = self._original_dispatch(request_id="orig-3")
        task_id = original["task_id"]
        self._reserve_and_cancel_via_prelaunch(task_id, task_id)
        # No linked failed/prelaunch_failed Command at all -- the Command
        # is still queued, so this cancellation is not structurally proven.
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="retry-3", retry_of_execution_id=task_id))
        self.assertEqual("retry_not_eligible", ctx.exception.code)

    def test_unknown_execution_id_rejected(self):
        self._original_dispatch(request_id="orig-4")
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="retry-4", retry_of_execution_id="does-not-exist"))
        self.assertEqual("unknown_execution", ctx.exception.code)

    def test_cross_project_execution_id_rejected(self):
        # An execution genuinely created under p2 must never be usable to
        # retry a task in p1 -- store.get is scoped by project_id, so this
        # is structurally impossible, not just policy-rejected.
        other = self.call(payload(request_id="orig-p2", project_id="p2"))
        other_task_id = other["task_id"]
        self._reserve_and_terminalize(other_task_id, other_task_id, project_id="p2", status="failed")
        self._mark_command_failed(other_task_id, other_task_id, project_id="p2")
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="retry-cross-project", project_id="p1", retry_of_execution_id=other_task_id))
        self.assertEqual("unknown_execution", ctx.exception.code)

    def test_forged_retry_count_field_rejected(self):
        original = self._original_dispatch(request_id="orig-5")
        task_id = original["task_id"]
        self._reserve_and_terminalize(task_id, task_id, status="failed")
        self._mark_command_failed(task_id, task_id)
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="retry-5", retry_of_execution_id=task_id, retry_count=5))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_provider_and_account_id_cannot_be_combined_with_retry(self):
        original = self._original_dispatch(request_id="orig-6")
        task_id = original["task_id"]
        self._reserve_and_terminalize(task_id, task_id, status="failed")
        self._mark_command_failed(task_id, task_id)
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="retry-6", retry_of_execution_id=task_id, provider="claude"))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_retry_count_exceeding_maximum_rejected(self):
        original = self._original_dispatch(request_id="orig-7")
        task_id = original["task_id"]
        terminal = self._reserve_and_terminalize(task_id, task_id, status="failed")
        terminal["retry_count"] = 2  # already at MAX_RETRY_COUNT
        validate("execution", terminal)
        self.store.put("executions", "p1", task_id, terminal)
        self._mark_command_failed(task_id, task_id)
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="retry-7", retry_of_execution_id=task_id))
        self.assertEqual("retry_not_eligible", ctx.exception.code)

    def test_duplicate_retry_request_id_is_idempotent(self):
        original = self._original_dispatch(request_id="orig-8")
        task_id = original["task_id"]
        self._reserve_and_terminalize(task_id, task_id, status="failed")
        self._mark_command_failed(task_id, task_id)
        first = self.call(payload(request_id="retry-8", retry_of_execution_id=task_id))
        second = self.call(payload(request_id="retry-8", retry_of_execution_id=task_id))
        self.assertEqual(first, second)
        # Only one retry Command was ever persisted.
        self.store.get("commands", "p1", first["command_id"])

    def test_no_unique_linked_command_rejected(self):
        # The prior execution is genuinely failed/interrupted, but no
        # Command links back to it (e.g. a hand-crafted execution) -- there
        # is no provider/account_id evidence to inherit from, so this must
        # fail closed rather than fabricate a routing decision.
        original = self._original_dispatch(request_id="orig-9")
        task_id = original["task_id"]
        self._reserve_and_terminalize(task_id, task_id, status="failed")
        # Command stays "queued" -- never marked failed/linked.
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="retry-9", retry_of_execution_id=task_id))
        self.assertEqual("retry_not_eligible", ctx.exception.code)

    def test_trusted_ingress_provenance_preserved_on_original_task(self):
        original = self._original_dispatch(request_id="orig-10")
        task_id = original["task_id"]
        self._reserve_and_terminalize(task_id, task_id, status="failed")
        self._mark_command_failed(task_id, task_id)
        task_before = self.store.get("tasks", "p1", task_id)
        self.call(payload(request_id="retry-10", retry_of_execution_id=task_id))
        task_after = self.store.get("tasks", "p1", task_id)
        # The retry ingress never touches the Task record at all -- its
        # original trusted-ingress source_context is untouched, byte for
        # byte, and the actual blocked->ready transition remains the sole
        # responsibility of manager.executions.prepare_task_retry(), called
        # later by manager.command_watcher at claim time.
        self.assertEqual(task_before, task_after)
        self.assertEqual(TRUSTED_INGRESS_ORIGIN, task_after["source_context"]["origin"])


class RepoWriteAdmissionIngressTests(unittest.TestCase):
    """Slice A of the Global Hands-off Execution Layer: cloud.dispatch_ingress's
    explicit, bounded v2-repo-write request shape (constraints.read_only:
    false + an explicit `repo_write` object). Preserving v1: a write
    request with no `repo_write` is still rejected exactly as before --
    see DispatchIngressTests.test_read_only_false_is_rejected_outright,
    unchanged by this slice."""

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
        return handle_dispatch(self.store, self.service, self.registries.factory, body if body is not None else self.write_payload())

    @staticmethod
    def write_payload(**changes):
        value = payload(request_id="req-w1", constraints={"read_only": False},
                         repo_write={"allowed_paths": ["manager/foo.py"], "baseline_head": "a" * 40,
                                     "repo": "https://github.com/example/project"})
        value.update(changes)
        return value

    def test_valid_repo_write_request_creates_bounded_write_task_and_command(self):
        result = self.call()
        task = self.store.get("tasks", "p1", result["task_id"])
        validate("task", task)
        self.assertIs(False, task["read_only"])
        self.assertIs(True, task["needs_repo_edit"])
        self.assertEqual(["manager/foo.py"], task["allowed_paths"])
        self.assertEqual("a" * 40, task["baseline_head"])
        self.assertEqual(sorted(REQUIRED_REPO_WRITE_TASK_POLICIES), sorted(task["execution_policies"]))
        self.assertEqual(ADMISSION_VERSION_V2_REPO_WRITE, task["source_context"]["admission_version"])
        self.assertEqual("https://github.com/example/project", task["source_context"]["repo"])
        command = self.store.get("commands", "p1", result["command_id"])
        validate("command", command)
        self.assertEqual(ADMISSION_VERSION_V2_REPO_WRITE, command["admission_version"])
        self.assertEqual(TRUSTED_INGRESS_ORIGIN, command["created_via"])

    def test_repo_write_task_never_inherits_a_stale_project_working_directory(self):
        """P0 regression (fix/direct-dispatch-working-directory-authority-p0-
        20260822): manager.dispatcher.dispatch() may already have snapshotted
        *some* working_directory onto this Task before handle_dispatch() even
        knew the request was v2-repo-write (e.g. the Project record's own
        literal, stale or not). A genuine bounded repo-write Task must always
        end up with working_directory reset to None so manager.execution_
        runner's Slice C worktree materialization actually engages -- never
        silently reusing the shared canonical checkout, which would defeat
        this project's own worktree_per_task isolation policy."""
        self.store.put("projects", "p1", "p1", {**project(), "working_directory": "C:/shared/canonical-checkout"})
        result = self.call()
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertIsNone(task["working_directory"])

    def test_read_only_false_without_repo_write_still_rejected_as_before(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-w-noop", constraints={"read_only": False}))
        self.assertEqual("read_only_required", ctx.exception.code)

    def test_repo_write_without_explicit_read_only_false_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(self.write_payload(constraints={"read_only": True}))
        self.assertEqual("malformed_request", ctx.exception.code)

    def test_missing_allowed_paths_rejected(self):
        bad = self.write_payload()
        del bad["repo_write"]["allowed_paths"]
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(bad)
        self.assertEqual("malformed_repo_write", ctx.exception.code)

    def test_empty_allowed_paths_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(self.write_payload(repo_write={**self.write_payload()["repo_write"], "allowed_paths": []}))
        self.assertEqual("empty_allowed_paths", ctx.exception.code)

    def test_missing_baseline_head_rejected(self):
        bad = self.write_payload()
        del bad["repo_write"]["baseline_head"]
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(bad)
        self.assertEqual("malformed_repo_write", ctx.exception.code)

    def test_invalid_baseline_head_rejected(self):
        for bad_head in ("abc123", "g" * 40, "A" * 40, "a" * 39, ""):
            with self.subTest(bad_head=bad_head):
                with self.assertRaises(DispatchIngressError) as ctx:
                    self.call(self.write_payload(request_id=f"req-bh-{len(bad_head)}",
                                                  repo_write={**self.write_payload()["repo_write"], "baseline_head": bad_head}))
                self.assertEqual("invalid_baseline_head", ctx.exception.code)

    def test_unsafe_allowed_path_entries_rejected(self):
        unsafe_paths = ("../secret.py", "/etc/passwd", "manager/.git/config", ".git/HEAD",
                         "manager/.env", "config/id_rsa", "creds/secret.key", "manager/*.py", "a/../../b")
        for index, bad_path in enumerate(unsafe_paths):
            with self.subTest(bad_path=bad_path):
                with self.assertRaises(DispatchIngressError) as ctx:
                    self.call(self.write_payload(request_id=f"req-p-{index}",
                                                  repo_write={**self.write_payload()["repo_write"], "allowed_paths": [bad_path]}))
                self.assertEqual("unsafe_allowed_path", ctx.exception.code)

    def test_missing_repo_identity_rejected(self):
        bad = self.write_payload()
        del bad["repo_write"]["repo"]
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(bad)
        self.assertEqual("malformed_repo_write", ctx.exception.code)

    def test_repo_identity_mismatch_with_project_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(self.write_payload(repo_write={**self.write_payload()["repo_write"], "repo": "https://github.com/example/other-repo"}))
        self.assertEqual("repo_identity_mismatch", ctx.exception.code)

    def test_replay_with_widened_allowed_paths_fails_closed(self):
        self.call()
        widened = self.write_payload(repo_write={**self.write_payload()["repo_write"], "allowed_paths": ["manager/foo.py", "manager/bar.py"]})
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(widened)
        self.assertEqual("request_replay_scope_mismatch", ctx.exception.code)
        # The rejected replay must not have mutated the already-created Task.
        task = self.store.get("tasks", "p1", "dispatch-req-w1")
        self.assertEqual(["manager/foo.py"], task["allowed_paths"])

    def test_replay_with_different_baseline_head_fails_closed(self):
        self.call()
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(self.write_payload(repo_write={**self.write_payload()["repo_write"], "baseline_head": "b" * 40}))
        self.assertEqual("request_replay_scope_mismatch", ctx.exception.code)

    def test_replay_with_different_repo_rejected_as_identity_mismatch_before_replay_check(self):
        # A replay naming a different repo never reaches the replay-scope
        # check at all: it is rejected earlier, on every call (not just
        # replays), for not matching the Project's own registered repo.
        # Both are fail-closed; this documents which check fires first.
        self.call()
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(self.write_payload(repo_write={**self.write_payload()["repo_write"], "repo": "https://github.com/example/other-repo"}))
        self.assertEqual("repo_identity_mismatch", ctx.exception.code)

    def test_replay_downgrading_to_read_only_fails_closed(self):
        self.call()
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-w1", constraints={"read_only": True}))
        self.assertEqual("request_replay_scope_mismatch", ctx.exception.code)

    def test_replay_with_identical_repo_write_contract_is_idempotent(self):
        first = self.call()
        second = self.call()
        self.assertEqual(first, second)

    def test_retry_of_execution_id_combined_with_repo_write_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(self.write_payload(retry_of_execution_id="some-execution"))
        self.assertEqual("malformed_request", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
