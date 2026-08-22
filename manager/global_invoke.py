#!/usr/bin/env python3
"""Global Invoke adapter (Global Hands-off Execution Layer, Slice E).

The one stable, ChatGPT/MCP/external-caller-facing entry point that lets any
project conversation dispatch work by project identity/alias alone, without
the caller ever having to know (or being able to forge) that project's
canonical repo, baseline commit, or governance/PROJECT-RULES identity.

Chain: project identity/alias -> manager.project_registry (Slice B, the
Global Project Registry resolver) -> Common Governance / PROJECT-RULES
presence check -> canonical repo cross-check -> baseline resolve ->
allowed_paths (shape-validated by the existing ingress) -> v2 trusted
ingress admission -> cloud.dispatch_ingress.handle_dispatch (Slice A, the
existing, unmodified authenticated Direct Dispatch admission chain).

This module never launches a provider and never grants any authority
cloud.dispatch_ingress does not already grant on its own -- it only removes
the burden (and the forgery surface) of a caller supplying repo, baseline,
or governance/PROJECT-RULES identity itself. A caller may name a project by
id or alias, a task description, an optional provider/account preference,
allowed_paths, and whether the invocation needs bounded repo-write
authority; there is no field here for repo, baseline_head, or a governance/
PROJECT-RULES reference -- those are always derived server-side.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, Optional

from cloud.dispatch_ingress import (
    BASELINE_HEAD_PATTERN, DispatchIngressError, MAX_GOAL_LENGTH, MAX_TITLE_LENGTH, handle_dispatch,
)
from manager.project_registry import (
    AmbiguousProjectError, GovernanceRuleMissingError, ProjectDisabledError, ProjectMetadata,
    ProjectNotFoundError, ProjectRegistry, UnresolvedProjectError, get_global_registry,
)
from manager.tasks import TaskError


ALLOWED_GLOBAL_INVOKE_FIELDS = {
    "idempotency_key", "project", "title", "goal", "priority",
    "repo_write", "allowed_paths", "preferred_provider", "account_id",
}


class GlobalInvokeError(TaskError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _run(cwd, *args, runner=subprocess.run):
    return runner(["git", "-C", str(cwd), *args], text=True, encoding="utf-8", errors="replace", capture_output=True)


def resolve_baseline_head(canonical_checkout, project: ProjectMetadata, runner=subprocess.run) -> str:
    """Resolve baseline_head server-side from the project's own registered
    baseline_resolution_policy (Slice B) against the canonical checkout --
    never taken from a caller-supplied field; global_invoke() has no field
    a caller could use to request an arbitrary baseline_head. `pinned_ref`
    wins when the policy sets one; otherwise the project's own
    default_branch is resolved."""
    policy = project.baseline_resolution_policy if isinstance(project.baseline_resolution_policy, dict) else {}
    pinned_ref = policy.get("pinned_ref")
    ref = pinned_ref if pinned_ref else (project.default_branch or "main")
    result = _run(canonical_checkout, "rev-parse", f"{ref}^{{commit}}", runner=runner)
    if result.returncode != 0:
        raise GlobalInvokeError(
            "baseline_resolution_failed",
            f"could not resolve baseline_head for project {project.project_id!r} from ref {ref!r}: "
            f"{(result.stderr or '').strip()}",
        )
    head = (result.stdout or "").strip()
    if not BASELINE_HEAD_PATTERN.match(head):
        raise GlobalInvokeError(
            "baseline_resolution_failed", f"resolved baseline_head {head!r} is not a valid commit id")
    return head


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
                   registry: Optional[ProjectRegistry] = None, canonical_checkout=None,
                   baseline_resolver=resolve_baseline_head) -> Dict[str, Any]:
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

    `canonical_checkout` defaults to the project's own registered
    working_directory_policy resolution (Slice B); tests inject a real temp
    git repo instead.
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
    extra_source_context = {
        "resolved_via": "global_invoke", "resolved_project_reference": project_reference,
    }

    if repo_write:
        allowed_paths = request.get("allowed_paths")
        if not isinstance(allowed_paths, list) or not allowed_paths:
            raise GlobalInvokeError(
                "empty_allowed_paths", "allowed_paths is required and must be a non-empty list when repo_write is true")
        repo_url = _verify_repo_write_eligible(store, project)
        checkout = canonical_checkout or project.resolve_runtime_working_directory()
        baseline_head = baseline_resolver(checkout, project)
        payload["repo_write"] = {"allowed_paths": list(allowed_paths), "baseline_head": baseline_head, "repo": repo_url}
        extra_source_context["governance_snapshot"] = dict(project.common_governance) if isinstance(project.common_governance, dict) else project.common_governance
        extra_source_context["project_rules"] = dict(project.project_rules) if isinstance(project.project_rules, dict) else project.project_rules
    elif request.get("allowed_paths") is not None:
        raise GlobalInvokeError("malformed_request", "allowed_paths requires repo_write: true")

    preferred_provider = _bounded_text("preferred_provider", request.get("preferred_provider"), 200)
    if preferred_provider is not None:
        payload["provider"] = preferred_provider
    account_id = _bounded_text("account_id", request.get("account_id"), 200)
    if account_id is not None:
        payload["account_id"] = account_id

    try:
        return handle_dispatch(store, service, lock_registry_factory, payload, extra_source_context=extra_source_context)
    except DispatchIngressError:
        # Already bounded-safe (no credentials/tracebacks); re-raised
        # verbatim so the caller gets the actionable underlying admission
        # reason, matching manager.mcp_adapter.invoke_dispatch's existing
        # re-raise-verbatim convention.
        raise
