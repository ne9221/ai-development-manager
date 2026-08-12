import asyncio
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mcp import Client

from manager.mcp_adapter import invoke_bridge, server
from manager.runtime_quota_tool import read_runtime_status
from manager.tasks import TaskError


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
                self.assertEqual({"adm_dispatch", "adm_status", "adm_runtime_quota_status", "adm_health"}, {item.name for item in tools})
                self.assertTrue(all(item.annotations.read_only_hint and item.annotations.destructive_hint is False for item in tools))
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

    def test_invalid_project_and_malformed_input(self):
        with patch("manager.mcp_adapter.invoke_bridge", side_effect=TaskError("project resolution expected one match; found 0")):
            self.assertTrue(asyncio.run(tool("adm_dispatch", {"project_id": "missing", "user_request": "work"})).is_error)
        self.assertTrue(asyncio.run(tool("adm_dispatch", {"project_id": "adm"})).is_error)
        self.assertTrue(asyncio.run(tool("adm_status", {})).is_error)


if __name__ == "__main__": unittest.main()
