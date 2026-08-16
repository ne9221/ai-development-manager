"""Thin authenticated ingress: turn an external high-level task request into
ADM's existing Task + Command contract.

This module only ever writes through manager.dispatcher.dispatch() (the
existing provider/quota decision) and manager.tasks (the existing Drive
persistence). It never launches a provider process, never calls
execution_runner/claude_launcher/codex_launcher, and never touches the
Command Watcher's allowlist or execution-policy gates -- a Command created
here sits `queued` for that existing, unmodified pipeline to pick up (or
correctly leave alone) under its own rules.
"""

import re

from manager.dispatch_requests import claim_dispatch_request
from manager.dispatcher import dispatch as dispatcher_dispatch
from manager.tasks import TaskError, now_iso, update_task, validate


ALLOWED_FIELDS = {"request_id", "project_id", "title", "goal", "priority", "constraints"}
ALLOWED_CONSTRAINT_FIELDS = {"read_only"}
ALLOWED_PRIORITIES = {"low", "normal", "high", "urgent"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_TITLE_LENGTH = 300
MAX_GOAL_LENGTH = 4000


class DispatchIngressError(TaskError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_dispatch_payload(payload):
    if not isinstance(payload, dict) or set(payload) - ALLOWED_FIELDS:
        raise DispatchIngressError("malformed_request", "request must be an object containing only request_id, project_id, title, goal, priority, constraints")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not ID_PATTERN.match(request_id):
        raise DispatchIngressError("malformed_request", "request_id is required and must match ^[A-Za-z0-9._-]{1,128}$")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ID_PATTERN.match(project_id):
        raise DispatchIngressError("malformed_request", "project_id is required and must match ^[A-Za-z0-9._-]{1,128}$")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LENGTH:
        raise DispatchIngressError("malformed_request", f"title is required (1-{MAX_TITLE_LENGTH} chars)")
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_LENGTH:
        raise DispatchIngressError("malformed_request", f"goal is required (1-{MAX_GOAL_LENGTH} chars)")
    priority = payload.get("priority", "normal")
    if priority not in ALLOWED_PRIORITIES:
        raise DispatchIngressError("malformed_request", f"priority must be one of {sorted(ALLOWED_PRIORITIES)}")
    constraints = payload.get("constraints", {})
    if not isinstance(constraints, dict) or set(constraints) - ALLOWED_CONSTRAINT_FIELDS:
        raise DispatchIngressError("malformed_request", "constraints must be an object containing only read_only")
    read_only = constraints.get("read_only", False)
    if not isinstance(read_only, bool):
        raise DispatchIngressError("malformed_request", "constraints.read_only must be a boolean")
    return {
        "request_id": request_id, "project_id": project_id, "title": title.strip(),
        "goal": goal.strip(), "priority": priority, "read_only": read_only,
    }


def handle_dispatch(store, service, lock_registry_factory, payload):
    """Idempotently create a queued Task+Command for one external request and
    return its identity. Never launches a provider.

    `lock_registry_factory(project_id, request_id)` must return a
    GCSLockRegistry-compatible object (create_if_absent/read/read_if_exists).
    """
    clean = validate_dispatch_payload(payload)
    project_id, request_id = clean["project_id"], clean["request_id"]
    try:
        store.get("projects", project_id, project_id)
    except TaskError as exc:
        raise DispatchIngressError("unknown_project", f"unknown project: {project_id}") from exc

    task_id = command_id = f"dispatch-{request_id}"
    try:
        registry = lock_registry_factory(project_id, request_id)
        claim = claim_dispatch_request(registry, project_id, request_id, task_id, command_id, now_iso())
    except DispatchIngressError:
        raise
    except Exception as exc:
        raise DispatchIngressError("idempotency_backend_unavailable", "could not establish request idempotency") from exc

    if not claim["claimed"]:
        status = "queued"
        try:
            status = store.get("commands", project_id, claim["command_id"]).get("status", status)
        except TaskError:
            pass
        return {"accepted": True, "request_id": request_id, "task_id": claim["task_id"],
                "command_id": claim["command_id"], "status": status}

    internal_request = {
        "project_id": project_id, "task_id": task_id, "title": clean["title"],
        "task_type": "general", "complexity": "medium",
        "source_context": {"origin": "direct_dispatch_ingress", "external_request_id": request_id, "goal": clean["goal"]},
    }
    result = dispatcher_dispatch(store, service, internal_request)
    update_task(store, project_id, task_id, priority=clean["priority"], read_only=clean["read_only"])

    command = {
        "command_id": command_id, "project_id": project_id, "task_id": task_id,
        "provider": result["provider"], "model": result["model"], "fallback_model": result["fallback_model"],
        "mode": result["mode"], "effort": result["effort"], "selection_reason": result["selection_reason"],
        "quota_evidence": result["quota_evidence"], "created_at": now_iso(), "status": "queued",
        "execution_id": None, "claimed_at": None, "completed_at": None, "result": None,
    }
    validate("command", command)
    store.put("commands", project_id, command_id, command)
    return {"accepted": True, "request_id": request_id, "task_id": task_id, "command_id": command_id, "status": "queued"}
