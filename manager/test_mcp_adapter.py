import asyncio
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mcp import Client

from cloud.dispatch_ingress import DispatchIngressError
from manager.mcp_adapter import MAX_REQUEST_LENGTH, MAX_RESPONSE_BYTES, invoke_bridge, invoke_dispatch, invoke_task_status, server
from manager.runtime_quota_tool import read_runtime_status
from manager.tasks import DriveRecords, TaskError, create_project, validate
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
                     "adm_create_task", "adm_task_status"},
                    {item.name for item in tools},
                )
                self.assertTrue(all(item.annotations.destructive_hint is False for item in tools))
                write_tools = {"adm_create_task"}
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


if __name__ == "__main__": unittest.main()
