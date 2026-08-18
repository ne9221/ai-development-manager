import contextlib
import json
import os
import tempfile
import threading
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
    """M0: manager.dispatcher.dispatch() already resolves a specific
    account_id when it auto-selects among named Claude accounts by quota
    forecast, and manager.command_watcher already treats a command's own
    account_id as launch authority (bypassing the fail-closed automatic-quota
    gate for Claude, per P0.0). The only missing wire was this ingress
    dropping that resolved account_id on the floor when it persisted the
    Command record -- silently forcing every Direct Dispatch Claude command
    onto the auto-selection path, which is unconditionally rejected as
    quota_unreliable. Without this, provider=claude could never actually
    launch through Direct Dispatch."""

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

    def test_command_account_id_lets_command_watcher_bypass_auto_quota_gate(self):
        """Proves the account_id isn't just stored but actually usable: the
        Command Watcher must route this command through the explicit-account
        launch path (which requires no reliable aggregate quota signal) and
        must launch it with the exact account_id the dispatcher chose --
        never falling back to the auto-selection quota gate that would
        otherwise reject every Claude command from this ingress."""
        result = self.call(payload(request_id="req-acct2"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude-a", command["account_id"])
        runner = Mock(return_value=CommandWatcherTests.complete("exec-1"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY), \
             patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}):
            outcome = process_command(
                self.store, self.service, command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True,
                quota_check=lambda service: False,  # auto-selection gate would reject; must never be consulted
                ingress_registry_factory=lambda bucket, project_id, request_id: self.registries.factory(project_id, request_id),
            )
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("claude-a", kwargs.get("account_id"))
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
        # Deliberately no Claude quota entries at all, and Codex quota very
        # healthy -- this is the real production shape the handoff reported
        # (Codex trait score wins, Claude evidence stale/unknown), so any
        # test here that still lands on the exact requested Claude account
        # proves the explicit route, not a coincidence of quota scoring.
        doc = {
            "schema_version": "0.1.0", "generated_at": fresh,
            "providers": [
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

    def test_explicit_claude_b_gets_exact_account(self):
        with _registry_env(self.REGISTRY):
            result = self.call(payload(request_id="req-b", provider="claude", account_id="claude-b"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("claude", command["provider"])
        self.assertEqual("claude-b", command["account_id"])
        self.assertEqual("claude-b", command["requested_account_id"])

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

    def test_invalid_provider_rejected(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            self.call(payload(request_id="req-badprov", provider="antigravity"))
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


if __name__ == "__main__":
    unittest.main()
