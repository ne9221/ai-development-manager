"""Read-only MCP adapter for the existing runtime bridge."""

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from cloud.app import default_service_factory
from manager.runtime_bridge import redact, runtime_bridge
from manager.runtime_quota_tool import runtime_quota_status
from manager.tasks import DriveRecords, TaskError


MCP_ADAPTER_VERSION = "1.0"
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
server = MCPServer(
    "AI Development Manager",
    version=MCP_ADAPTER_VERSION,
    instructions="Read-only project status and AI dispatch recommendations from the Drive runtime SSOT.",
)


def invoke_bridge(request, service_factory=default_service_factory, bridge_func=runtime_bridge):
    service = service_factory()
    return redact(bridge_func(DriveRecords(service), service, request, read_only=True))


def project_reference(project_id, project_alias):
    reference = project_id or project_alias
    if not isinstance(reference, str) or not reference.strip():
        raise TaskError("project_id or project_alias is required")
    return reference.strip()


@server.tool(annotations=READ_ONLY, structured_output=True)
def adm_dispatch(
    user_request: str,
    project_id: str | None = None,
    project_alias: str | None = None,
    task_id: str | None = None,
    preferred_provider: str | None = None,
    excluded_provider: str | None = None,
    multi_task: bool = False,
) -> dict[str, Any]:
    """Return the sanitized runtime bridge contract and generated prompt without writing runtime data."""
    if not isinstance(user_request, str) or not user_request.strip():
        raise TaskError("user_request is required")
    request = {
        "project_id": project_reference(project_id, project_alias),
        "user_request": user_request.strip(),
        "multi_task": multi_task,
    }
    for key, value in (("task_id", task_id), ("preferred_provider", preferred_provider), ("excluded_provider", excluded_provider)):
        if value is not None:
            request[key] = value
    return invoke_bridge(request)


@server.tool(annotations=READ_ONLY, structured_output=True)
def adm_status(project_id: str | None = None, project_alias: str | None = None) -> dict[str, Any]:
    """Return a compact active-task, handoff, quota, and next-action status."""
    reference = project_reference(project_id, project_alias)
    result = invoke_bridge({"project_id": reference, "user_request": f"Status for {reference}"})
    return {key: result.get(key) for key in (
        "contract_version", "project", "active_task", "latest_handoff_summary",
        "recommended_provider", "quota_summary", "quota_freshness", "warnings", "next_action",
    )}


server.tool(name="adm_runtime_quota_status", annotations=READ_ONLY, structured_output=True)(runtime_quota_status)


@server.tool(annotations=READ_ONLY, structured_output=True)
def adm_health() -> dict[str, Any]:
    """Return MCP adapter and runtime contract versions without accessing Drive."""
    return {"status": "ok", "mcp_adapter_version": MCP_ADAPTER_VERSION, "runtime_contract_version": "1.0"}
