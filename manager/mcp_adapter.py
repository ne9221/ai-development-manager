"""MCP adapter and local stdio transport for the runtime bridge.

Every tool here except adm_create_task is read-only. adm_create_task is
the one narrow write surface: it only ever calls
cloud.dispatch_ingress.handle_dispatch() (the same authenticated Direct
Dispatch ingress the REST route uses) and never launches a provider or
touches execution/launcher machinery directly -- see
manager/trusted_ingress.py for the Safe Auto-Admission contract that
governs what happens to the Task/Command it creates.
"""

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from cloud.app import default_lock_registry_factory, default_service_factory, default_write_service_factory
from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.runtime_bridge import redact, runtime_bridge
from manager.runtime_quota_tool import runtime_quota_status
from manager.tasks import DriveRecords, TaskError, validate


MCP_ADAPTER_VERSION = "1.0"
MAX_REFERENCE_LENGTH = 200
MAX_REQUEST_LENGTH = 4000
MAX_RESPONSE_BYTES = 65536
LOCAL_SERVICE_FACTORY = None
LOCAL_WRITE_SERVICE_FACTORY = None
TASK_STATUS_FIELDS = ("status", "current_progress", "next_action", "blocked_reason", "updated_at",
                      "completed_at", "assigned_provider", "account_id", "read_only")
COMMAND_STATUS_FIELDS = ("status", "provider", "account_id", "execution_id", "claimed_at",
                         "completed_at", "result", "recovery_reason")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
IDEMPOTENT_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
server = MCPServer(
    "AI Development Manager",
    version=MCP_ADAPTER_VERSION,
    instructions="Project status, AI dispatch recommendations, and disposable read-only task creation from the Drive runtime SSOT.",
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


def invoke_dispatch(payload, write_service_factory=None, lock_registry_factory=None, dispatch_func=None):
    """Call the same authenticated Direct Dispatch ingress the REST route
    uses. `payload` must already be the exact narrow shape adm_create_task
    builds -- this function adds no fields and removes no validation;
    cloud.dispatch_ingress.validate_dispatch_payload remains the sole
    authority on what is accepted. DispatchIngressError/TaskError messages
    are already bounded-safe (no credentials/tracebacks) and are re-raised
    verbatim so the caller gets an actionable reason; anything else
    (backend/transport failure) is sanitized before it can propagate.
    """
    write_service_factory = write_service_factory or LOCAL_WRITE_SERVICE_FACTORY or default_write_service_factory
    lock_registry_factory = lock_registry_factory or default_lock_registry_factory
    dispatch_func = dispatch_func or handle_dispatch
    try:
        service = write_service_factory()
        store = DriveRecords(service)
    except Exception:
        raise TaskError("runtime data is unavailable") from None
    try:
        return dispatch_func(store, service, lock_registry_factory, payload)
    except (DispatchIngressError, TaskError):
        raise
    except Exception:
        raise TaskError("dispatch ingress failed") from None


def invoke_task_status(project_id, task_id, command_id, service_factory=None):
    """Read back Task/Command status by exact id, read-only. Returns
    whichever of task/command actually exist (either may be absent, e.g. a
    Task with no Command yet); raises only when neither is found."""
    service_factory = service_factory or LOCAL_SERVICE_FACTORY or default_service_factory
    try:
        service = service_factory()
        store = DriveRecords(service)
    except Exception:
        raise TaskError("runtime data is unavailable") from None
    response = {"project_id": project_id, "task_id": task_id, "command_id": command_id, "task": None, "command": None}
    try:
        task = store.get("tasks", project_id, task_id)
        validate("task", task)
        response["task"] = {key: task.get(key) for key in TASK_STATUS_FIELDS}
    except TaskError:
        pass
    try:
        command = store.get("commands", project_id, command_id)
        validate("command", command)
        response["command"] = {key: command.get(key) for key in COMMAND_STATUS_FIELDS}
    except TaskError:
        pass
    if response["task"] is None and response["command"] is None:
        raise TaskError("no matching task or command found")
    return response


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


@server.tool(annotations=IDEMPOTENT_WRITE, structured_output=True)
def adm_create_task(project_id: str, title: str, goal: str, request_id: str) -> dict[str, Any]:
    """Create a disposable, read-only Task+Command through the authenticated
    Direct Dispatch ingress (Safe Auto-Admission v1). The created Task is
    always forced read-only with the full disposable/no-repo-writes/
    no-external-writes policy set -- there is no way to request a write
    task, a specific provider/account, an execution/command line, or any
    other execution control through this tool. Never launches a provider;
    the created Command sits `queued` for the existing pipeline to pick up
    under its own rules. Idempotent: replaying the same request_id returns
    the identity already created, never a second Task/Command."""
    return invoke_dispatch({"project_id": project_id, "title": title, "goal": goal, "request_id": request_id})


@server.tool(annotations=READ_ONLY, structured_output=True)
def adm_task_status(project_id: str, request_id: str | None = None, task_id: str | None = None,
                    command_id: str | None = None) -> dict[str, Any]:
    """Read back Task/Command status by request_id (the id adm_create_task
    was called with -- this resolves it using the ingress's own
    task_id/command_id = "dispatch-<request_id>" convention) or by an
    explicit task_id/command_id. At least one of request_id, task_id, or
    command_id is required. Returns status (queued/claimed/running/
    completed/failed/blocked/attention), provider/account, progress,
    latest update, and the final result summary when available."""
    project_id = bounded_text("project_id", project_id, MAX_REFERENCE_LENGTH, required=True)
    request_id = bounded_text("request_id", request_id, MAX_REFERENCE_LENGTH)
    task_id = bounded_text("task_id", task_id, MAX_REFERENCE_LENGTH)
    command_id = bounded_text("command_id", command_id, MAX_REFERENCE_LENGTH)
    resolved_task_id = task_id or (f"dispatch-{request_id}" if request_id else None)
    resolved_command_id = command_id or (f"dispatch-{request_id}" if request_id else None)
    if not resolved_task_id and not resolved_command_id:
        raise TaskError("one of request_id, task_id, or command_id is required")
    return invoke_task_status(project_id, resolved_task_id, resolved_command_id)


server.tool(name="adm_runtime_quota_status", annotations=READ_ONLY, structured_output=True)(runtime_quota_status)


@server.tool(annotations=READ_ONLY, structured_output=True)
def adm_health() -> dict[str, Any]:
    """Return MCP adapter and runtime contract versions without accessing Drive."""
    return {"status": "ok", "mcp_adapter_version": MCP_ADAPTER_VERSION, "runtime_contract_version": "1.0"}


def main():
    global LOCAL_SERVICE_FACTORY, LOCAL_WRITE_SERVICE_FACTORY
    from collectors.publish_drive import build_service
    # The desktop OAuth flow already requests the full (write-capable)
    # drive scope -- see collectors/publish_drive.py:SCOPES -- so the same
    # factory is correct for both the read-only tools (which still always
    # pass read_only=True to the bridge) and adm_create_task's one write
    # path. Only the Cloud Run ASGI mount needs the separate readonly-
    # scoped vs write-scoped service-account factories from cloud.app.
    LOCAL_SERVICE_FACTORY = build_service
    LOCAL_WRITE_SERVICE_FACTORY = build_service
    server.run("stdio")


if __name__ == "__main__":
    main()
