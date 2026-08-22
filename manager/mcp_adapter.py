"""MCP adapter and local stdio transport for the runtime bridge.

Every tool here except adm_create_task and adm_invoke is read-only.
adm_create_task and adm_invoke are the two write surfaces: adm_create_task
only ever calls cloud.dispatch_ingress.handle_dispatch() directly (the
same authenticated Direct Dispatch ingress the REST route uses) for its
narrower disposable-read-only-task shape; adm_invoke calls
manager.global_invoke.global_invoke(), which itself only ever reaches
handle_dispatch() -- neither tool launches a provider or touches
execution/launcher machinery directly. See manager/trusted_ingress.py for
the Safe Auto-Admission contract that governs what happens to the
Task/Command either tool creates.
"""

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from cloud.app import default_lock_registry_factory, default_service_factory, default_write_service_factory
from cloud.dispatch_ingress import DispatchIngressError, MAX_GOAL_LENGTH, MAX_TITLE_LENGTH, handle_dispatch
from manager.global_invoke import GlobalInvokeError, global_invoke, resolve_project
from manager.project_registry import get_global_registry
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
COMMAND_STATUS_FIELDS = ("status", "provider", "account_id", "requested_provider", "requested_account_id",
                         "selection_reason", "execution_id", "claimed_at", "completed_at", "result", "recovery_reason")
EXECUTION_STATUS_FIELDS = ("execution_id", "status", "session_id", "provider", "account_id",
                           "started_at", "completed_at", "terminal_reason", "recovery_reason")
SESSION_STATUS_FIELDS = ("session_id", "status", "provider", "started_at", "updated_at", "summary")
HANDOFF_STATUS_FIELDS = ("handoff_id", "created_at", "from_provider", "to_provider", "reason", "next_action")
# Normalizes each record area's own richer status enum (see schema/*.schema.json)
# down to the five caller-facing states the invocation contract promises
# (queued/running/completed/failed/blocked). Command status wins whenever a
# Command exists (it is the more current record); Task status is only the
# fallback for a request_id that has not been claimed into a Command yet.
COMMAND_STATE_MAP = {"queued": "queued", "claimed": "running", "running": "running",
                     "attention": "blocked", "completed": "completed", "failed": "failed"}
TASK_STATE_MAP = {"queued": "queued", "ready": "queued", "in_progress": "running",
                  "blocked": "blocked", "completed": "completed", "cancelled": "failed"}
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


def _resolve_linked_record(store, area, project_id, record_id, kind, fields):
    """One edge of the chain: `record_id` is None only when there is
    genuinely no linkage yet (e.g. no Command has claimed an execution_id
    yet) -- that, and only that, is reported as (None, False). Once a
    linkage id exists, its target must actually be readable and valid; a
    fetch/validation failure at that point is a real inconsistency (a
    dangling id, a corrupted record) and must never be reported the same
    way as "not created yet" -- callers use the second element to tell the
    two apart and must not silently swallow it."""
    if not record_id:
        return None, False
    try:
        record = store.get(area, project_id, record_id)
        validate(kind, record)
    except TaskError:
        return None, True
    return {key: record.get(key) for key in fields}, False


def invoke_status_chain(project_id, task_id, command_id, service_factory=None):
    """Full Task -> Command -> Execution -> Session -> Handoff lookup
    chain for one invocation, plus a normalized `state` (queued/running/
    completed/failed/blocked), read-only. Builds on invoke_task_status
    (unchanged) for the Task/Command edge and only adds the edges that
    module does not expose: Command.execution_id -> Execution,
    Execution.session_id -> Session, and the Task's own latest Handoff (a
    Task may have zero, one, or several Handoffs over its lifetime; only
    the most recent is returned here, matching manager.tasks.DriveRecords.
    latest()'s existing single-record convention used elsewhere).

    A record with no linkage id yet (no Command claimed, no Execution
    reserved, no Session linked) is null -- that is a legitimate, expected
    "not created yet" state. A Handoff is likewise legitimately absent
    before terminal completion; there is no handoff_id field on Task or
    Command to prove a broken linkage against, so a missing Handoff is
    always reported as null, never as inconsistent.

    A record whose linkage id DOES exist but cannot be read or fails
    schema validation is a different, more serious case -- a dangling or
    corrupted reference -- and is never silently reported as the null
    "not created yet" case. `chain_integrity` names exactly which edge (if
    any) is broken, and `state` is forced to "blocked" whenever any edge
    is broken, even if the Command/Task's own status would otherwise read
    as further along (e.g. "completed") -- a caller must not be told a
    chain finished successfully when part of it could not actually be
    verified."""
    service_factory = service_factory or LOCAL_SERVICE_FACTORY or default_service_factory
    base = invoke_task_status(project_id, task_id, command_id, service_factory=service_factory)
    try:
        service = service_factory()
        store = DriveRecords(service)
    except Exception:
        raise TaskError("runtime data is unavailable") from None

    command = base.get("command")
    execution_id = command.get("execution_id") if command else None
    execution, execution_broken = _resolve_linked_record(
        store, "executions", project_id, execution_id, "execution", EXECUTION_STATUS_FIELDS)

    session_id = execution.get("session_id") if execution else None
    session, session_broken = _resolve_linked_record(
        store, "sessions", project_id, session_id, "session", SESSION_STATUS_FIELDS)

    handoff = None
    try:
        handoff_record = store.latest("handoffs", project_id, task_id)
        handoff = {key: handoff_record.get(key) for key in HANDOFF_STATUS_FIELDS}
    except TaskError:
        pass

    if command is not None:
        state = COMMAND_STATE_MAP.get(command.get("status"), "blocked")
    elif base.get("task") is not None:
        state = TASK_STATE_MAP.get(base["task"].get("status"), "blocked")
    else:
        state = "blocked"
    chain_broken = execution_broken or session_broken
    if chain_broken:
        state = "blocked"

    base["execution"] = execution
    base["session"] = session
    base["handoff"] = handoff
    base["state"] = state
    base["chain_integrity"] = {
        "execution_unreadable": execution_broken,
        "session_unreadable": session_broken,
    }
    return base


def invoke_global(request, write_service_factory=None, lock_registry_factory=None,
                  registry=None, baseline_resolver=None):
    """Call manager.global_invoke.global_invoke -- the same server-side
    project-alias-resolving, no-caller-working_directory, automatic-
    provider-by-default facade that already exists for this contract --
    through the exact write-service/store construction convention
    invoke_dispatch already uses. Adds no fields, removes no validation:
    global_invoke() and, beneath it, cloud.dispatch_ingress.
    validate_dispatch_payload() remain the sole authorities on what is
    accepted."""
    write_service_factory = write_service_factory or LOCAL_WRITE_SERVICE_FACTORY or default_write_service_factory
    lock_registry_factory = lock_registry_factory or default_lock_registry_factory
    try:
        service = write_service_factory()
        store = DriveRecords(service)
    except Exception:
        raise TaskError("runtime data is unavailable") from None
    kwargs = {}
    if registry is not None:
        kwargs["registry"] = registry
    if baseline_resolver is not None:
        kwargs["baseline_resolver"] = baseline_resolver
    try:
        return global_invoke(store, service, lock_registry_factory, request, **kwargs)
    except (GlobalInvokeError, TaskError):
        raise
    except Exception:
        raise TaskError("global invoke failed") from None


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


@server.tool(annotations=IDEMPOTENT_WRITE, structured_output=True)
def adm_invoke(project: str, title: str, goal: str, idempotency_key: str,
               priority: str | None = None, repo_write: bool = False,
               allowed_paths: list[str] | None = None,
               preferred_provider: str | None = None, account_id: str | None = None) -> dict[str, Any]:
    """The one stable, backend-neutral invocation entry point: submit one
    task by project identity/alias alone -- no repo, branch, baseline, or
    working_directory field exists here for a caller to supply, override,
    or forge one; `project` is resolved server-side through the Global
    Project Registry (manager.global_invoke.resolve_project), and a
    repo_write invocation's baseline is resolved server-side through
    manager.remote_baseline_resolver (GitHub-remote-API, never a local
    checkout). Provider/account selection is automatic (quota-aware) by
    default; an explicit preferred_provider/account_id is honored and
    recorded on the Command, never silently overridden. Idempotent on
    idempotency_key: replaying the same key never double-dispatches, and
    returns the identity already created. Never launches a provider --
    creates a queued Task+Command for the existing Command Watcher
    pipeline to pick up under its own rules, exactly like adm_create_task,
    but covering the full external contract (project alias, optional
    bounded repo-write, explicit provider/account override) rather than
    adm_create_task's narrower disposable-read-only-task shape. The
    result always includes the resolved canonical project_id (never just
    the caller's raw alias) alongside request_id/task_id/command_id/
    status -- pass that same project_id straight to adm_invoke_status, or
    pass the original alias again; both resolve to the identical project."""
    request: dict[str, Any] = {
        "idempotency_key": bounded_text("idempotency_key", idempotency_key, MAX_REFERENCE_LENGTH, required=True),
        "project": bounded_text("project", project, MAX_REFERENCE_LENGTH, required=True),
        "title": bounded_text("title", title, MAX_TITLE_LENGTH, required=True),
        "goal": bounded_text("goal", goal, MAX_GOAL_LENGTH, required=True),
    }
    if priority is not None:
        request["priority"] = bounded_text("priority", priority, MAX_REFERENCE_LENGTH)
    if repo_write:
        request["repo_write"] = True
        request["allowed_paths"] = list(allowed_paths) if allowed_paths else []
    if preferred_provider is not None:
        request["preferred_provider"] = bounded_text("preferred_provider", preferred_provider, MAX_REFERENCE_LENGTH)
    if account_id is not None:
        request["account_id"] = bounded_text("account_id", account_id, MAX_REFERENCE_LENGTH)
    return invoke_global(request)


@server.tool(annotations=READ_ONLY, structured_output=True)
def adm_invoke_status(project: str, request_id: str | None = None, task_id: str | None = None,
                      command_id: str | None = None) -> dict[str, Any]:
    """Full Task->Command->Execution->Session->Handoff lookup chain for one
    invocation (by request_id -- the idempotency_key adm_invoke or
    adm_create_task was called with, resolved via the shared "dispatch-
    <request_id>" convention -- or by an explicit task_id/command_id), plus
    a normalized `state` (queued/running/completed/failed/blocked) and
    `chain_integrity` (whether the Execution/Session edges, if linked at
    all, actually resolved). `project` is resolved through the same
    Global Project Registry resolver adm_invoke uses
    (manager.global_invoke.resolve_project) -- pass either the original
    project alias or the canonical project_id adm_invoke already
    returned; both resolve to the identical project, so a caller never
    has to maintain two different project-reference conventions across
    submit and status. At least one of request_id, task_id, or command_id
    is required. A record with no linkage id yet is reported as null; a
    record whose linkage id exists but could not be read/validated is
    never reported the same way -- see chain_integrity. A genuinely
    unknown identity (neither Task nor Command found) is an error, not an
    empty success."""
    project_reference_value = bounded_text("project", project, MAX_REFERENCE_LENGTH, required=True)
    request_id = bounded_text("request_id", request_id, MAX_REFERENCE_LENGTH)
    task_id = bounded_text("task_id", task_id, MAX_REFERENCE_LENGTH)
    command_id = bounded_text("command_id", command_id, MAX_REFERENCE_LENGTH)
    resolved_task_id = task_id or (f"dispatch-{request_id}" if request_id else None)
    resolved_command_id = command_id or (f"dispatch-{request_id}" if request_id else None)
    if not resolved_task_id and not resolved_command_id:
        raise TaskError("one of request_id, task_id, or command_id is required")
    resolved_project = resolve_project(get_global_registry(), project_reference_value)
    return invoke_status_chain(resolved_project.project_id, resolved_task_id, resolved_command_id)


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
