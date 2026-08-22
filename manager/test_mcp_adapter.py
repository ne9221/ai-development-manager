import asyncio
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mcp import Client

from cloud.dispatch_ingress import DispatchIngressError
from manager.global_invoke import GlobalInvokeError
from manager.mcp_adapter import (
    MAX_REQUEST_LENGTH, MAX_RESPONSE_BYTES, invoke_bridge, invoke_dispatch, invoke_global,
    invoke_status_chain, invoke_task_status, server,
)
from manager.project_registry import ProjectRegistry
from manager.runtime_quota_tool import read_runtime_status
from manager.tasks import DriveRecords, TaskError, create_handoff, create_project, validate
from manager.test_dispatcher import quota as quota_fixture
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_tasks import FakeDriveService


def contract(**changes):
    value = {
        "contract_version": "1.0", "project": {"project_id": "adm"}, "request_type": "new_task",
        "active_task": None, "latest_handoff_summary": None, "recommended_provider": "codex",
        "mode": "code", "effort": "medium", "estimated_minutes": 12, "split_recommended": False,
        "alternatives": ["claude"], "quota_summary": "80% remaining; official; fresh",
        "quota_freshness": "fresh", "warnings": [], "next_action": "Copy prompt",
        "generated_prompt": "safe prompt", "execution_batches": [],
    }
    value.update(changes)
    return value


async def tool(name, arguments):
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def structured(result):
    return result.structured_content


def quota_document(codex=80, claude=60):
    now = datetime.now(timezone.utc).isoformat()
    providers = []
    for provider, remaining, window in (("codex", codex, "primary"), ("claude", claude, "five_hour")):
        windows = [] if remaining is None else [{"name": window, "used_percent": 100 - remaining, "remaining_percent": remaining}]
        providers.append({
            "provider": provider, "source": "codex_app_server" if provider == "codex" else "claude_code_statusline_rate_limits",
            "source_type": "official", "confidence": "official", "status": "ok" if windows else "unknown",
            "last_updated": now, "windows": windows, "metadata": {"raw_payload": "token=must-not-leak"},
        })
    return {"schema_version": "0.1.0", "generated_at": now, "providers": providers, "raw": "must-not-leak"}


class MCPAdapterTests(unittest.TestCase):
    def test_health_and_read_only_tool_definitions(self):
        async def check():
            async with Client(server) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual(
                    {"adm_dispatch", "adm_status", "adm_runtime_quota_status", "adm_health",
                     "adm_create_task", "adm_task_status", "adm_invoke", "adm_invoke_status"},
                    {item.name for item in tools},
                )
                self.assertTrue(all(item.annotations.destructive_hint is False for item in tools))
                write_tools = {"adm_create_task", "adm_invoke"}
                for item in tools:
                    self.assertEqual(item.name not in write_tools, item.annotations.read_only_hint)
                quota_tool = next(item for item in tools if item.name == "adm_runtime_quota_status")
                self.assertEqual({"max_age_minutes"}, set(quota_tool.input_schema["properties"]))
                self.assertEqual(
                    {"default": 60, "minimum": 1, "maximum": 1440, "title": "Max Age Minutes", "type": "integer"},
                    quota_tool.input_schema["properties"]["max_age_minutes"],
                )
                result = await client.call_tool("adm_health", {})
                self.assertEqual({"status": "ok", "mcp_adapter_version": "1.0", "runtime_contract_version": "1.0"}, structured(result))
        asyncio.run(check())

    def test_runtime_quota_known_codex_and_claude_round_trip_is_sanitized_json(self):
        with patch("manager.runtime_bridge.read_drive_status", return_value=quota_document()):
            result = asyncio.run(tool("adm_runtime_quota_status", {}))
        value = structured(result)
        self.assertEqual({"contract_version", "schema_version", "generated_at", "providers"}, set(value))
        self.assertEqual(80, value["providers"]["codex"]["windows"][0]["remaining_percent"])
        self.assertEqual(60, value["providers"]["claude"]["windows"][0]["remaining_percent"])
        serialized = json.dumps(value)
        self.assertNotIn("metadata", serialized); self.assertNotIn("raw_payload", serialized); self.assertNotIn("must-not-leak", serialized)
        self.assertEqual(value, json.loads(serialized))

    def test_runtime_quota_unknown_is_not_zero_and_loader_failure_is_bounded(self):
        with patch("manager.runtime_bridge.read_drive_status", return_value=quota_document(claude=None)):
            unknown = structured(asyncio.run(tool("adm_runtime_quota_status", {"max_age_minutes": 30})))
        self.assertEqual("unknown", unknown["providers"]["claude"]["status"])
        self.assertEqual([], unknown["providers"]["claude"]["windows"])
        self.assertFalse(any(key.endswith("_percent") for key in unknown["providers"]["claude"]))

        with patch("manager.runtime_bridge.read_drive_status", side_effect=RuntimeError("Bearer backend-secret")):
            unavailable = structured(asyncio.run(tool("adm_runtime_quota_status", {})))
        self.assertEqual({"codex", "claude"}, set(unavailable["providers"]))
        self.assertTrue(all(item["status"] == "unavailable" and item["windows"] == [] for item in unavailable["providers"].values()))
        self.assertNotIn("backend-secret", json.dumps(unavailable))

    def test_runtime_quota_stale_round_trip(self):
        document = quota_document()
        old = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        document["generated_at"] = old
        for provider in document["providers"]:
            provider["last_updated"] = old
        with patch("manager.runtime_bridge.read_drive_status", return_value=document):
            stale = structured(asyncio.run(tool("adm_runtime_quota_status", {})))
        self.assertTrue(all(item["status"] == "stale" for item in stale["providers"].values()))

    def test_runtime_quota_valid_boundaries_reach_loader(self):
        with patch("manager.runtime_bridge.read_drive_status", return_value=quota_document()), \
             patch("manager.runtime_quota_tool.read_runtime_status", wraps=read_runtime_status) as loader:
            for value in (1, 1440):
                with self.subTest(value=value):
                    self.assertFalse(asyncio.run(tool("adm_runtime_quota_status", {"max_age_minutes": value})).is_error)
        self.assertEqual([1, 1440], [call.kwargs["max_age_minutes"] for call in loader.call_args_list])

    def test_runtime_quota_unexpected_property_is_not_forwarded(self):
        with patch("manager.runtime_bridge.read_drive_status", return_value=quota_document()), \
             patch("manager.runtime_quota_tool.read_runtime_status", wraps=read_runtime_status) as loader:
            result = asyncio.run(tool("adm_runtime_quota_status", {"service": {"token": "caller-value"}}))
        self.assertFalse(result.is_error)
        loader.assert_called_once_with(max_age_minutes=60)

    def test_runtime_quota_invalid_arguments_fail_closed_before_loader(self):
        invalid = (-1, 0, 1441, 1.0, "60", True, None, 10**200, {"nested": "value"})
        with patch("manager.runtime_quota_tool.read_runtime_status") as loader:
            for value in invalid:
                with self.subTest(value=value):
                    self.assertTrue(asyncio.run(tool("adm_runtime_quota_status", {"max_age_minutes": value})).is_error)
        loader.assert_not_called()

    def test_adapter_import_does_not_require_pywintypes(self):
        code = 'import sys; sys.modules["pywintypes"] = None; import manager.runtime_quota_tool'
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_dispatch_alias_continuation_multitask_and_prompt(self):
        calls = []
        def fake(request, **_kwargs):
            calls.append(request)
            return contract(request_type="scheduling" if request.get("multi_task") else "continuation" if request.get("task_id") else "new_task")
        with patch("manager.mcp_adapter.invoke_bridge", side_effect=fake):
            alias = structured(asyncio.run(tool("adm_dispatch", {"project_alias": "ADM", "user_request": "work"})))
            continuation = structured(asyncio.run(tool("adm_dispatch", {"project_id": "adm", "task_id": "t1", "user_request": "continue"})))
            multi = structured(asyncio.run(tool("adm_dispatch", {"project_id": "adm", "user_request": "schedule", "multi_task": True})))
        self.assertEqual("1.0", alias["contract_version"]); self.assertEqual("safe prompt", alias["generated_prompt"])
        self.assertEqual("continuation", continuation["request_type"]); self.assertEqual("scheduling", multi["request_type"])
        self.assertEqual("ADM", calls[0]["project_id"]); self.assertTrue(calls[2]["multi_task"])

    def test_status_unknown_stale_is_compact(self):
        raw = contract(quota_summary="quota unknown", quota_freshness="stale", warnings=["quota stale"])
        raw["providers"] = [{"raw": "must not leak"}]
        with patch("manager.mcp_adapter.invoke_bridge", return_value=raw):
            status = structured(asyncio.run(tool("adm_status", {"project_alias": "ADM"})))
        self.assertEqual("stale", status["quota_freshness"]); self.assertEqual("quota unknown", status["quota_summary"])
        self.assertNotIn("providers", status); self.assertNotIn("generated_prompt", status)

    def test_secret_redaction_contract_and_no_write_runtime_reuse(self):
        class Service:
            def files(self): return object()
        calls = []
        def bridge(store, service, request, read_only=False):
            calls.append(read_only); return contract(generated_prompt="token=hunter2")
        result = invoke_bridge({"project_id": "adm", "user_request": "work"}, lambda: Service(), bridge)
        self.assertEqual([True], calls); self.assertEqual("1.0", result["contract_version"])
        self.assertNotIn("hunter2", json.dumps(result))

    def test_backend_exception_and_oversized_output_are_fixed_safe_errors(self):
        with patch("manager.mcp_adapter.default_service_factory", side_effect=RuntimeError("Bearer backend-secret")):
            unavailable = asyncio.run(tool("adm_dispatch", {"project_id": "adm", "user_request": "work"}))
        self.assertTrue(unavailable.is_error)
        self.assertIn("runtime data is unavailable", str(unavailable.content))
        self.assertNotIn("backend-secret", str(unavailable.content))

        def oversized(*_args, **_kwargs):
            return contract(generated_prompt="x" * MAX_RESPONSE_BYTES)
        class Service:
            def files(self): return object()
        with self.assertRaisesRegex(TaskError, "transport limit"):
            invoke_bridge({"project_id": "adm", "user_request": "work"}, Service, oversized)

    def test_invalid_project_and_malformed_input(self):
        with patch("manager.mcp_adapter.invoke_bridge", side_effect=TaskError("project resolution expected one match; found 0")):
            self.assertTrue(asyncio.run(tool("adm_dispatch", {"project_id": "missing", "user_request": "work"})).is_error)
        self.assertTrue(asyncio.run(tool("adm_dispatch", {"project_id": "adm"})).is_error)
        self.assertTrue(asyncio.run(tool("adm_status", {})).is_error)
        self.assertTrue(asyncio.run(tool("adm_dispatch", {"project_id": "adm", "task_id": "x" * 201, "user_request": "work"})).is_error)
        self.assertTrue(asyncio.run(tool("adm_dispatch", {"project_id": "adm", "user_request": "x" * (MAX_REQUEST_LENGTH + 1)})).is_error)

    def test_invalid_project_task_and_raw_exception_messages_are_not_exposed(self):
        for failure in (TaskError("missing task token=task-secret"), RuntimeError("Bearer backend-secret")):
            with patch("manager.mcp_adapter.default_service_factory", return_value=object()), \
                 patch("manager.mcp_adapter.runtime_bridge", side_effect=failure):
                result = asyncio.run(tool("adm_dispatch", {"project_id": "adm", "task_id": "missing", "user_request": "work"}))
            serialized = str(result.content)
            self.assertTrue(result.is_error)
            self.assertNotIn("task-secret", serialized)
            self.assertNotIn("backend-secret", serialized)

    def test_module_exposes_stdio_entrypoint(self):
        source = subprocess.run(
            [sys.executable, "-c", "import manager.mcp_adapter as m; assert callable(m.main)"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, source.returncode, source.stderr)

    def test_no_direct_provider_spawn_from_mcp_adapter(self):
        """adm_create_task must only ever reach the provider pipeline through
        the existing Direct Dispatch ingress -- never launch/execution
        machinery directly, and never accept a caller-chosen provider,
        account, or execution control."""
        import manager.mcp_adapter as module
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("execution_runner", "claude_launcher", "codex_launcher", "subprocess.Popen", "os.system"):
            self.assertNotIn(forbidden, source)


class SharedMemoryRegistries:
    """Mirrors cloud/test_dispatch_ingress.py's double: a fresh wrapper per
    call, state shared per (project_id, request_id) key."""
    def __init__(self):
        self.registries = {}

    def factory(self, project_id, request_id):
        key = (project_id, request_id)
        if key not in self.registries:
            self.registries[key] = MemoryClaimRegistry()
        return self.registries[key]


def project(project_id="p1"):
    return {"project_id": project_id, "name": "Project One", "repo": "https://github.com/example/project",
            "default_branch": "main", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [],
            "current_phase": "Phase 1", "important_constraints": []}


class MCPCreateTaskToolTests(unittest.TestCase):
    """adm_create_task: the one write surface, restricted to exactly
    project_id/title/goal/request_id and wired only through
    cloud.dispatch_ingress.handle_dispatch (Safe Auto-Admission v1)."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def test_invoke_dispatch_creates_disposable_read_only_task_and_command(self):
        result = invoke_dispatch(
            {"project_id": "p1", "title": "Investigate flaky test", "goal": "Read logs, report findings", "request_id": "req-1"},
            write_service_factory=lambda: self.service, lock_registry_factory=self.registries.factory,
        )
        self.assertEqual({"accepted": True, "request_id": "req-1", "task_id": "dispatch-req-1",
                           "command_id": "dispatch-req-1", "status": "queued"}, result)
        task = self.store.get("tasks", "p1", "dispatch-req-1")
        validate("task", task)
        self.assertTrue(task["read_only"])
        command = self.store.get("commands", "p1", "dispatch-req-1")
        validate("command", command)
        self.assertEqual("direct_dispatch_ingress", command["created_via"])

    def test_invoke_dispatch_is_idempotent_on_replay(self):
        first = invoke_dispatch({"project_id": "p1", "title": "t", "goal": "g", "request_id": "req-dup"},
                                write_service_factory=lambda: self.service, lock_registry_factory=self.registries.factory)
        second = invoke_dispatch({"project_id": "p1", "title": "t", "goal": "g", "request_id": "req-dup"},
                                 write_service_factory=lambda: self.service, lock_registry_factory=self.registries.factory)
        self.assertEqual(first["task_id"], second["task_id"])

    def test_dispatch_ingress_error_propagates_with_safe_message(self):
        with self.assertRaises(DispatchIngressError) as ctx:
            invoke_dispatch({"project_id": "does-not-exist", "title": "t", "goal": "g", "request_id": "req-2"},
                            write_service_factory=lambda: self.service, lock_registry_factory=self.registries.factory)
        self.assertEqual("unknown_project", ctx.exception.code)

    def test_write_service_unavailable_is_sanitized(self):
        def broken():
            raise RuntimeError("credential=super-secret")
        with self.assertRaises(TaskError) as ctx:
            invoke_dispatch({"project_id": "p1", "title": "t", "goal": "g", "request_id": "req-3"},
                            write_service_factory=broken, lock_registry_factory=self.registries.factory)
        self.assertNotIn("super-secret", str(ctx.exception))

    def test_unexpected_dispatch_exception_is_sanitized(self):
        def broken_dispatch(*_args, **_kwargs):
            raise RuntimeError("token=hunter2")
        with self.assertRaises(TaskError) as ctx:
            invoke_dispatch({"project_id": "p1", "title": "t", "goal": "g", "request_id": "req-4"},
                            write_service_factory=lambda: self.service, lock_registry_factory=self.registries.factory,
                            dispatch_func=broken_dispatch)
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_tool_only_accepts_the_four_narrow_fields(self):
        """The tool's own function signature is the enforcement point: an
        MCP client cannot pass provider/account_id/executable/env/etc.
        through it at all -- extra properties are dropped before they ever
        reach invoke_dispatch, exactly like adm_runtime_quota_status's
        unmodeled-property behavior documented in docs/MCP-INTEGRATION.md."""
        captured = []
        def fake(payload, **_kwargs):
            captured.append(payload)
            return {"accepted": True, "request_id": payload["request_id"], "task_id": "t", "command_id": "c", "status": "queued"}
        with patch("manager.mcp_adapter.invoke_dispatch", side_effect=fake):
            result = asyncio.run(tool("adm_create_task", {
                "project_id": "p1", "title": "t", "goal": "g", "request_id": "req-5",
                "provider": "claude", "account_id": "acct-1", "executable": "/bin/sh",
                "env": {"X": "1"}, "config_dir": "/tmp", "working_directory": "/tmp",
            }))
        self.assertFalse(result.is_error)
        self.assertEqual({"project_id", "title", "goal", "request_id"}, set(captured[0]))

    def test_create_task_tool_missing_field_is_error(self):
        result = asyncio.run(tool("adm_create_task", {"project_id": "p1", "title": "t", "goal": "g"}))
        self.assertTrue(result.is_error)


class MCPTaskStatusToolTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()
        invoke_dispatch({"project_id": "p1", "title": "t", "goal": "g", "request_id": "req-1"},
                        write_service_factory=lambda: self.service, lock_registry_factory=self.registries.factory)

    def tearDown(self):
        self.quota_patch.stop()

    def test_invoke_task_status_resolves_task_and_command_with_bounded_fields(self):
        result = invoke_task_status("p1", "dispatch-req-1", "dispatch-req-1", service_factory=lambda: self.service)
        self.assertEqual("queued", result["command"]["status"])
        self.assertIn("status", result["task"])
        # No raw source_context (goal/origin/idempotency evidence) leaks through the status surface.
        self.assertNotIn("source_context", result["task"])
        self.assertNotIn("quota_evidence", result["task"])

    def test_invoke_task_status_raises_when_neither_found(self):
        with self.assertRaises(TaskError):
            invoke_task_status("p1", "no-such-task", "no-such-command", service_factory=lambda: self.service)

    def test_status_tool_resolves_via_request_id_convention(self):
        captured = []
        def fake(project_id, task_id, command_id, **_kwargs):
            captured.append((project_id, task_id, command_id))
            return {"project_id": project_id, "task_id": task_id, "command_id": command_id, "task": {}, "command": {}}
        with patch("manager.mcp_adapter.invoke_task_status", side_effect=fake):
            result = asyncio.run(tool("adm_task_status", {"project_id": "p1", "request_id": "req-1"}))
        self.assertFalse(result.is_error)
        self.assertEqual(("p1", "dispatch-req-1", "dispatch-req-1"), captured[0])

    def test_status_tool_explicit_ids_override_request_id_convention(self):
        captured = []
        def fake(project_id, task_id, command_id, **_kwargs):
            captured.append((project_id, task_id, command_id))
            return {"project_id": project_id, "task_id": task_id, "command_id": command_id, "task": {}, "command": {}}
        with patch("manager.mcp_adapter.invoke_task_status", side_effect=fake):
            asyncio.run(tool("adm_task_status", {"project_id": "p1", "request_id": "req-1", "task_id": "manual-t1", "command_id": "manual-c1"}))
        self.assertEqual(("p1", "manual-t1", "manual-c1"), captured[0])

    def test_status_tool_requires_at_least_one_identity(self):
        result = asyncio.run(tool("adm_task_status", {"project_id": "p1"}))
        self.assertTrue(result.is_error)

    def test_status_tool_unknown_identity_is_error_not_empty_success(self):
        result = asyncio.run(tool("adm_task_status", {"project_id": "p1", "request_id": "never-created"}))
        self.assertTrue(result.is_error)


REPO_URL = "https://github.com/example/project"


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


def invoke_request(**changes):
    value = {"idempotency_key": "inv-1", "project": "p1", "title": "Fix parser", "goal": "Fix the parser regression"}
    value.update(changes)
    return value


class MCPGlobalInvokeToolTests(unittest.TestCase):
    """adm_invoke / invoke_global: the full backend-neutral contract
    (project alias resolution, no caller-working_directory authority,
    automatic-provider-by-default, auditable explicit override,
    idempotency) layered over manager.global_invoke.global_invoke, which
    this module reuses unmodified rather than re-implementing."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        self.registry = ProjectRegistry(projects=[registry_entry()])
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    def call(self, req=None):
        return invoke_global(req if req is not None else invoke_request(),
                             write_service_factory=lambda: self.service,
                             lock_registry_factory=self.registries.factory, registry=self.registry)

    def test_project_alias_resolves_to_canonical_project_id(self):
        result = self.call(invoke_request(project="proj-one", idempotency_key="inv-alias"))
        self.assertTrue(result["accepted"])
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertEqual("p1", task["project_id"])

    def test_unresolved_project_fails_closed(self):
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(invoke_request(project="no-such-project", idempotency_key="inv-missing"))
        self.assertEqual("project_not_found", ctx.exception.code)
        # Nothing was written for a project that could not be resolved.
        with self.assertRaises(TaskError):
            self.store.get("tasks", "p1", "dispatch-inv-missing")

    def test_omitted_provider_uses_automatic_selection(self):
        result = self.call(invoke_request(idempotency_key="inv-auto"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertIsNone(command["requested_provider"])
        self.assertIn(command["provider"], ("codex", "claude", "antigravity"))

    def test_explicit_provider_is_recorded_and_auditable(self):
        result = self.call(invoke_request(idempotency_key="inv-explicit", preferred_provider="codex"))
        command = self.store.get("commands", "p1", result["command_id"])
        self.assertEqual("codex", command["requested_provider"])
        self.assertEqual("codex", command["provider"])

    def test_same_idempotency_key_replay_does_not_double_dispatch(self):
        first = self.call(invoke_request(idempotency_key="inv-dup"))
        second = self.call(invoke_request(idempotency_key="inv-dup"))
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(1, len([r for r in self.store.list_records("tasks", "p1") if r["task_id"] == first["task_id"]]))

    def test_no_working_directory_field_accepted_request_or_task(self):
        """There is no field in the invocation contract for a caller to
        supply, override, or forge working_directory -- supplying one is
        rejected as an unsupported field, never silently accepted or used."""
        req = invoke_request(idempotency_key="inv-cwd", working_directory="C:/attacker/evil")
        with self.assertRaises(GlobalInvokeError) as ctx:
            self.call(req)
        self.assertEqual("malformed_request", ctx.exception.code)
        self.assertIn("working_directory", str(ctx.exception))

    def test_tool_wiring_round_trip(self):
        """Proves adm_invoke actually calls invoke_global with the caller's
        fields (never touching a real Drive credential in this test --
        that is invoke_global's own, separately-tested responsibility)."""
        captured = []
        def fake(request, **_kwargs):
            captured.append(request)
            return {"accepted": True, "request_id": request["idempotency_key"], "task_id": "t", "command_id": "c", "status": "queued"}
        with patch("manager.mcp_adapter.invoke_global", side_effect=fake):
            result = structured(asyncio.run(tool("adm_invoke", {
                "project": "p1", "title": "t", "goal": "g", "idempotency_key": "inv-tool-1",
            })))
        self.assertTrue(result["accepted"])
        self.assertEqual({"idempotency_key": "inv-tool-1", "project": "p1", "title": "t", "goal": "g"}, captured[0])


class MCPInvokeStatusChainTests(unittest.TestCase):
    """adm_invoke_status / invoke_status_chain: the full Task->Command->
    Execution->Session->Handoff readback the contract requires, on top of
    the existing (unmodified) invoke_task_status Task/Command lookup."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, project())
        self.registries = SharedMemoryRegistries()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()
        self.result = invoke_dispatch(
            {"project_id": "p1", "title": "t", "goal": "g", "request_id": "req-chain"},
            write_service_factory=lambda: self.service, lock_registry_factory=self.registries.factory)
        self.task_id = self.result["task_id"]
        self.command_id = self.result["command_id"]

    def tearDown(self):
        self.quota_patch.stop()

    def test_queued_state_with_no_execution_session_or_handoff_yet(self):
        result = invoke_status_chain("p1", self.task_id, self.command_id, service_factory=lambda: self.service)
        self.assertEqual("queued", result["state"])
        self.assertIsNone(result["execution"])
        self.assertIsNone(result["session"])
        self.assertIsNone(result["handoff"])

    def test_full_chain_resolves_execution_session_and_handoff(self):
        session_record = {
            "session_id": "sess-1", "provider": "codex", "provider_session_id": "raw-1", "account_id": None,
            "project_id": "p1", "task_id": self.task_id, "conversation_label": "c1", "title": "Session",
            "summary": "did work", "started_at": "2026-08-23T00:00:00Z", "updated_at": "2026-08-23T00:05:00Z",
            "working_directory": None, "repository": None, "source_identifier": "codex:raw-1",
            "source_path": None, "parent_session_id": None, "usage_ref": None, "resume_ref": None,
            "mapping_source": None, "content_hash": None, "classification_method": "unclassified",
            "classification_confidence": None, "classification_status": "needs_review",
            "status": "completed", "message_count": 3, "model": "gpt-5", "first_user_prompt": "go",
        }
        self.store.put("sessions", "p1", "sess-1", session_record)
        execution_record = {
            "execution_id": "exec-1", "task_id": self.task_id, "project_id": "p1", "provider": "codex",
            "mode": "code", "effort": "medium", "reserved_at": None, "started_at": "2026-08-23T00:00:00Z",
            "completed_at": "2026-08-23T00:05:00Z", "finished_at": None, "session_id": "sess-1",
            "provider_session_id": "raw-1", "account_id": None, "elapsed_minutes": 5, "status": "completed",
            "retry_count": 0, "retry_of_execution_id": None, "heartbeat_at": None, "progress_updated_at": None,
            "hard_timeout_at": None, "last_provider_event": None, "provider_evidence": None, "stale_at": None,
            "recovery_reason": None, "terminal_reason": None, "quota_evidence": None, "quota_before": {},
            "quota_after": {}, "quota_delta": {}, "source_confidence": "official", "access": "read_only",
            "lease_evidence": None, "cleanup_evidence": None, "repo_write_completion_evidence": None,
            "notes": [], "task_snapshot": {"task_type": "general", "complexity": "medium", "needs_repo_edit": False},
        }
        self.store.put("executions", "p1", "exec-1", execution_record)
        command = self.store.get("commands", "p1", self.command_id)
        command["execution_id"] = "exec-1"
        command["status"] = "completed"
        self.store.put("commands", "p1", self.command_id, command)
        create_handoff(self.store, {
            "handoff_id": "ho-1", "task_id": self.task_id, "project_id": "p1", "from_provider": "codex",
            "to_provider": "claude", "from_session": "sess-1", "reason": "provider switch",
            "current_state": "done", "next_action": "review", "acceptance_criteria": ["done"],
            "minimal_context": "context",
        })

        result = invoke_status_chain("p1", self.task_id, self.command_id, service_factory=lambda: self.service)
        self.assertEqual("completed", result["state"])
        self.assertEqual("exec-1", result["execution"]["execution_id"])
        self.assertEqual("sess-1", result["execution"]["session_id"])
        self.assertEqual("sess-1", result["session"]["session_id"])
        self.assertEqual("ho-1", result["handoff"]["handoff_id"])

    def test_missing_execution_is_reported_as_none_not_fabricated(self):
        command = self.store.get("commands", "p1", self.command_id)
        command["execution_id"] = "no-such-execution"
        self.store.put("commands", "p1", self.command_id, command)
        result = invoke_status_chain("p1", self.task_id, self.command_id, service_factory=lambda: self.service)
        self.assertIsNone(result["execution"])
        self.assertIsNone(result["session"])

    def test_unknown_identity_is_error_not_empty_success(self):
        with self.assertRaises(TaskError):
            invoke_status_chain("p1", "no-such-task", "no-such-command", service_factory=lambda: self.service)

    def test_tool_wiring_resolves_via_request_id_convention(self):
        captured = []
        def fake(project_id, task_id, command_id, **_kwargs):
            captured.append((project_id, task_id, command_id))
            return {"project_id": project_id, "task_id": task_id, "command_id": command_id,
                    "task": {}, "command": {}, "execution": None, "session": None, "handoff": None, "state": "queued"}
        with patch("manager.mcp_adapter.invoke_status_chain", side_effect=fake):
            result = structured(asyncio.run(tool("adm_invoke_status", {"project_id": "p1", "request_id": "req-chain"})))
        self.assertEqual("queued", result["state"])
        self.assertEqual(("p1", "dispatch-req-chain", "dispatch-req-chain"), captured[0])


if __name__ == "__main__": unittest.main()
