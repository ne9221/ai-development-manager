import asyncio
import json
import unittest
from unittest.mock import patch

from mcp import Client

from manager.mcp_adapter import invoke_bridge, server
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


class MCPAdapterTests(unittest.TestCase):
    def test_health_and_read_only_tool_definitions(self):
        async def check():
            async with Client(server) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({"adm_dispatch", "adm_status", "adm_health"}, {item.name for item in tools})
                self.assertTrue(all(item.annotations.read_only_hint and item.annotations.destructive_hint is False for item in tools))
                result = await client.call_tool("adm_health", {})
                self.assertEqual({"status": "ok", "mcp_adapter_version": "1.0", "runtime_contract_version": "1.0"}, structured(result))
        asyncio.run(check())

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
