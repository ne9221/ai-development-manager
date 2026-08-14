"""Read-only MCP adapter and local stdio transport for the runtime bridge."""

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from cloud.app import default_service_factory
from manager.runtime_bridge import redact, runtime_bridge
from manager.runtime_quota_tool import runtime_quota_status
from manager.tasks import DriveRecords, TaskError


MCP_ADAPTER_VERSION = "1.0"
MAX_REFERENCE_LENGTH = 200
MAX_REQUEST_LENGTH = 4000
MAX_RESPONSE_BYTES = 65536
LOCAL_SERVICE_FACTORY = None
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
server = MCPServer(
    "AI Development Manager",
    version=MCP_ADAPTER_VERSION,
    instructions="Read-only project status and AI dispatch recommendations from the Drive runtime SSOT.",
)


def invoke_bridge(request, service_factory=None, bridge_func=None):
    service_factory = service_factory or LOCAL_SERVICE_FACTORY or default_service_factory
    bridge_func = bridge_func or runtime_bridge
    try:
        service = service_factory()
        result = redact(bridge_func(DriveRecords(service), service, request, read_only=True))
    except TaskError:
        raise TaskError("runtime request is invalid") from None
    except Exception:
        raise TaskError("runtime data is unavailable") from None
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise TaskError("runtime response exceeds transport limit")
    return result


def bounded_text(name, value, maximum, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise TaskError(f"{name} is invalid")
    return value.strip()


def project_reference(project_id, project_alias):
    reference = project_id or project_alias
    return bounded_text("project_id or project_alias", reference, MAX_REFERENCE_LENGTH, required=True)


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
    request = {
        "project_id": project_reference(project_id, project_alias),
        "user_request": bounded_text("user_request", user_request, MAX_REQUEST_LENGTH, required=True),
        "multi_task": multi_task,
    }
    for key, value in (("task_id", task_id), ("preferred_provider", preferred_provider), ("excluded_provider", excluded_provider)):
        if value is not None:
            request[key] = bounded_text(key, value, MAX_REFERENCE_LENGTH)
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


def main():
    global LOCAL_SERVICE_FACTORY
    from collectors.publish_drive import build_service
    LOCAL_SERVICE_FACTORY = build_service
    server.run("stdio")


if __name__ == "__main__":
    main()
