#!/usr/bin/env python3
"""Global Invoke adapter (Global Hands-off Execution Layer).

The one stable, ChatGPT/MCP/external-caller-facing entry point that lets any
project conversation dispatch work by project identity/alias alone, without
the caller ever having to know (or being able to forge) that project's
canonical repo, baseline commit, or governance/PROJECT-RULES identity.

Chain: project identity/alias -> manager.project_registry (the Global
Project Registry resolver) -> Common Governance / PROJECT-RULES presence
check -> canonical repo cross-check -> manager.remote_baseline_resolver
(server-side, GitHub-remote-API baseline resolution -- no local business
repo checkout, ever) -> allowed_paths (shape-validated by the existing
ingress) -> v2 trusted ingress admission ->
cloud.dispatch_ingress.handle_dispatch (the existing, unmodified
authenticated Direct Dispatch admission chain).

This module never launches a provider and never grants any authority
cloud.dispatch_ingress does not already grant on its own -- it only removes
the burden (and the forgery surface) of a caller supplying repo, baseline,
or governance/PROJECT-RULES identity itself. A caller may name a project by
id or alias, a task description, an optional provider/account preference,
allowed_paths, and whether the invocation needs bounded repo-write
authority; ALLOWED_GLOBAL_INVOKE_FIELDS deliberately has no field for repo,
branch, or baseline_head/baseline_sha -- those are always derived
server-side, and supplying one is rejected as a malformed request rather
than trusted as a hint.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from cloud.dispatch_ingress import (
    DispatchIngressError,
    MAX_GOAL_LENGTH,
    MAX_TITLE_LENGTH,
    handle_dispatch,
)
from manager.project_registry import (
    AmbiguousProjectError,
    GovernanceRuleMissingError,
    ProjectDisabledError,
    ProjectMetadata,
    ProjectNotFoundError,
    ProjectRegistry,
    UnresolvedProjectError,
    get_global_registry,
)
from manager.remote_baseline_resolver import resolve_remote_baseline
from manager.tasks import TaskError

ALLOWED_GLOBAL_INVOKE_FIELDS = {
    "idempotency_key", "project", "title", "goal", "priority",
    "repo_write", "allowed_paths", "preferred_provider", "account_id",
}


class GlobalInvokeError(TaskError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def resolve_project(registry: ProjectRegistry, project_reference: str) -> ProjectMetadata:
    """Deterministic project resolution: exact project_id, registered
    alias, or repository identity -- fails closed on ambiguity, a disabled
    project, or an unknown project. Every downstream step uses only the
    registry's own resolved canonical project_id, never the caller's raw
    reference string."""
    try:
        return registry.get_project(project_reference)
    except AmbiguousProjectError as exc:
        raise GlobalInvokeError("project_ambiguous", str(exc)) from exc
    except ProjectDisabledError as exc:
        raise GlobalInvokeError("project_disabled", str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise GlobalInvokeError("project_not_found", str(exc)) from exc


_WRITE_PRECONDITION_CODES = {
    ProjectDisabledError: "project_disabled",
    UnresolvedProjectError: "repo_write_not_eligible",
    GovernanceRuleMissingError: "governance_missing",
}


def _verify_repo_write_eligible(store, project: ProjectMetadata) -> str:
    """Everything the *project* must independently prove before this module
    ever builds a repo_write request: enabled + verified + governance/
    PROJECT-RULES references present
    (ProjectMetadata.validate_write_dispatch_preconditions), plus that the
    Global Project Registry's own repo identity agrees with the separate
    Drive Project record cloud.dispatch_ingress will independently
    re-check at admission time -- two independent sources of repo truth
    must agree, neither trusted on its own say-so. Returns the
    cross-checked repo URL to stamp into repo_write.repo."""
    try:
        project.validate_write_dispatch_preconditions()
    except tuple(_WRITE_PRECONDITION_CODES) as exc:
        raise GlobalInvokeError(_WRITE_PRECONDITION_CODES[type(exc)], str(exc)) from exc

    try:
        drive_project = store.get("projects", project.project_id, project.project_id)
    except TaskError as exc:
        raise GlobalInvokeError(
            "unknown_project",
            f"project {project.project_id!r} is registered in the Global Project Registry but has no "
            "Drive project record",
        ) from exc

    if drive_project.get("repo") != project.repo_url:
        raise GlobalInvokeError(
            "repo_identity_mismatch",
            f"Global Project Registry repo ({project.repo_url!r}) does not match the Drive project "
            f"record's repo ({drive_project.get('repo')!r}) for {project.project_id!r}",
        )
    return project.repo_url


def _bounded_text(name, value, maximum, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise GlobalInvokeError("malformed_request", f"{name} is invalid")
    return value.strip()


def global_invoke(store, service, lock_registry_factory, request: Dict[str, Any],
                   registry: Optional[ProjectRegistry] = None,
                   baseline_resolver: Callable[..., Dict[str, Any]] = resolve_remote_baseline) -> Dict[str, Any]:
    """The single stable entry point a ChatGPT/MCP/external caller uses
    regardless of which project it names.

    `request` fields (the ONLY fields a caller may supply):
      - idempotency_key (str, required): becomes the ingress request_id.
      - project (str, required): canonical project_id or a registered alias.
      - title, goal (str, required).
      - priority (optional).
      - repo_write (bool, default False): whether this invocation needs
        bounded repo-write authority.
      - allowed_paths (list[str], required iff repo_write): what the
        caller wants touched -- shape/safety re-validated by
        cloud.dispatch_ingress.validate_dispatch_payload exactly as it
        already does for the existing REST/MCP write path; this module
        keeps no separate, possibly-divergent copy of that logic.
      - preferred_provider, account_id (optional): passed straight through
        as routing evidence, identical to the existing ingress contract.

    `baseline_resolver` defaults to
    manager.remote_baseline_resolver.resolve_remote_baseline (server-side,
    GitHub-remote-API resolution keyed only by the resolved project_id);
    tests inject a fake with the same signature.
    """
    registry = registry or get_global_registry()
    if not isinstance(request, dict):
        raise GlobalInvokeError("malformed_request", "request must be an object")
    unexpected = set(request) - ALLOWED_GLOBAL_INVOKE_FIELDS
    if unexpected:
        raise GlobalInvokeError("malformed_request", f"unsupported field(s): {sorted(unexpected)}")

    project_reference = _bounded_text("project", request.get("project"), 300, required=True)
    idempotency_key = _bounded_text("idempotency_key", request.get("idempotency_key"), 128, required=True)
    title = _bounded_text("title", request.get("title"), MAX_TITLE_LENGTH, required=True)
    goal = _bounded_text("goal", request.get("goal"), MAX_GOAL_LENGTH, required=True)

    project = resolve_project(registry, project_reference)

    repo_write = bool(request.get("repo_write", False))
    payload = {
        "request_id": idempotency_key, "project_id": project.project_id,
        "title": title, "goal": goal, "priority": request.get("priority", "normal"),
        "constraints": {"read_only": not repo_write},
    }

    if repo_write:
        allowed_paths = request.get("allowed_paths")
        if not isinstance(allowed_paths, list) or not allowed_paths:
            raise GlobalInvokeError(
                "empty_allowed_paths", "allowed_paths is required and must be a non-empty list when repo_write is true")
        repo_url = _verify_repo_write_eligible(store, project)

        baseline = baseline_resolver(project.project_id, registry=registry)
        if not isinstance(baseline, dict) or baseline.get("project_id") != project.project_id:
            # Defense in depth: the resolver is only ever asked about the
            # registry's own resolved project_id, but never trust that a
            # (possibly faked-in-tests) resolver actually honored that --
            # a baseline record for a different project must never be
            # allowed to backdoor a different repo's write authority in.
            raise GlobalInvokeError(
                "baseline_resolution_failed",
                f"baseline resolver did not return a baseline for project {project.project_id!r}",
            )
        if baseline.get("repository") != repo_url:
            raise GlobalInvokeError(
                "repo_identity_mismatch",
                f"resolved baseline repository ({baseline.get('repository')!r}) does not match the "
                f"cross-checked project repo ({repo_url!r})",
            )
        baseline_head = baseline.get("baseline_sha")

        payload["repo_write"] = {"allowed_paths": list(allowed_paths), "baseline_head": baseline_head, "repo": repo_url}
    elif request.get("allowed_paths") is not None:
        raise GlobalInvokeError("malformed_request", "allowed_paths requires repo_write: true")

    preferred_provider = _bounded_text("preferred_provider", request.get("preferred_provider"), 200)
    if preferred_provider is not None:
        payload["provider"] = preferred_provider
    account_id = _bounded_text("account_id", request.get("account_id"), 200)
    if account_id is not None:
        payload["account_id"] = account_id

    try:
        result = handle_dispatch(store, service, lock_registry_factory, payload)
    except DispatchIngressError:
        # Already bounded-safe (no credentials/tracebacks); re-raised
        # verbatim so the caller gets the actionable underlying admission
        # reason, matching manager.mcp_adapter.invoke_dispatch's existing
        # re-raise-verbatim convention.
        raise
    # handle_dispatch()'s own return shape has no project_id field (it is
    # only ever called with an already-canonical project_id, so it has no
    # alias to resolve). This module is the one place in the chain that
    # actually resolved project_reference -> project.project_id -- stamping
    # it onto the result here means a caller that named a project by alias
    # never has to independently re-resolve that alias just to keep working
    # with the canonical id the rest of the record chain (Task/Command/
    # Execution/Session/Handoff) is keyed by.
    return {**result, "project_id": project.project_id}
