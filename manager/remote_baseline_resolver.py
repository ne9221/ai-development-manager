#!/usr/bin/env python3
"""Remote Baseline Resolver (Global Hands-off Execution Layer).

Resolves, entirely server-side, the three facts a repo-write dispatch needs
before it can be trusted with edit authority: the project's canonical
repository, its canonical/default branch, and that branch's current remote
HEAD commit SHA.

This module never shells out to a local `git` checkout of the caller's
business repo (Cloud Run and any other stateless dispatch surface has no
such checkout, and must not be made to require one) and never accepts a
repository, branch, or commit id from a caller. The only input is a project
identity/alias; everything else comes from two authoritative sources that
already existed before this module and neither of which a caller controls:

  1. manager.project_registry (the canonical Global Project Registry) --
     supplies the project's registered repository owner/name and its
     default/pinned canonical branch.
  2. A live read of the GitHub REST API for that exact owner/name/branch --
     supplies the branch's current remote HEAD commit SHA.

Every failure mode -- registry miss, disabled/ambiguous project, missing
repo identity, missing branch, the GitHub API being unreachable or
returning a non-200/malformed body, a malformed SHA -- fails closed with a
RemoteBaselineResolutionError. Nothing here ever guesses, caches past a
single call, or falls back to a caller-suggested value.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import requests

from manager.project_registry import (
    AmbiguousProjectError,
    ProjectDisabledError,
    ProjectMetadata,
    ProjectNotFoundError,
    ProjectRegistry,
    get_global_registry,
)
from manager.tasks import TaskError

# Reuse the ingress's own commit-id pattern (schema/task.schema.json's
# baseline_head contract: full 40-char SHA-1 or 64-char SHA-256 hex) so
# there is exactly one definition of "a valid baseline_head" in this
# codebase, not two that could silently drift apart.
from cloud.dispatch_ingress import BASELINE_HEAD_PATTERN

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_TIMEOUT_SECONDS = 15
REMOTE_BASELINE_SOURCE = "github_remote_api"

# schema/project_registry.schema.json's baseline_resolution_policy.strategy
# enum is ["origin_default", "pinned_commit", "latest_release", "custom"].
# Only the first two are actually implemented below; "pinned_ref" is a
# sibling FIELD on the policy object, never a strategy value itself, and
# must never be accepted as one.
STRATEGY_ORIGIN_DEFAULT = "origin_default"
STRATEGY_PINNED_COMMIT = "pinned_commit"
SUPPORTED_BASELINE_STRATEGIES = frozenset({STRATEGY_ORIGIN_DEFAULT, STRATEGY_PINNED_COMMIT})

# Server-side-only credential for reading private registered repos. Never
# caller-suppliable: read from the process environment (already this
# codebase's established convention -- see e.g. CLAUDE_ACCOUNTS_CONFIG,
# ADM_LOCK_GCS_BUCKET in manager.command_watcher), not from any request
# field. Public repos resolve identically with or without it.
GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"


class RemoteBaselineResolutionError(TaskError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def default_github_fetch(owner: str, name: str, branch: str, *, token: Optional[str] = None) -> Dict[str, Any]:
    """Default remote HEAD lookup: `GET /repos/{owner}/{repo}/commits/{branch}`.

    Returns the parsed JSON body on HTTP 200. Fails closed (raises
    RemoteBaselineResolutionError) on any transport error, non-200 status,
    or unparseable body -- never returns a guessed or stale value.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/commits/{branch}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RemoteBaselineResolutionError(
            "remote_api_unavailable",
            f"GitHub API request failed for {owner}/{name}@{branch}: {exc}",
        ) from exc

    if response.status_code == 404:
        raise RemoteBaselineResolutionError(
            "remote_branch_not_found", f"repository or branch not found: {owner}/{name}@{branch}")
    if response.status_code != 200:
        raise RemoteBaselineResolutionError(
            "remote_api_unavailable",
            f"GitHub API returned HTTP {response.status_code} for {owner}/{name}@{branch}",
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise RemoteBaselineResolutionError(
            "remote_api_unavailable",
            f"GitHub API returned an unparseable response body for {owner}/{name}@{branch}",
        ) from exc
    return body


def _resolve_project(registry: ProjectRegistry, project_reference: str) -> ProjectMetadata:
    if not isinstance(project_reference, str) or not project_reference.strip():
        raise RemoteBaselineResolutionError("malformed_request", "project reference must be a non-empty string")
    try:
        return registry.get_project(project_reference)
    except AmbiguousProjectError as exc:
        raise RemoteBaselineResolutionError("project_ambiguous", str(exc)) from exc
    except ProjectDisabledError as exc:
        raise RemoteBaselineResolutionError("project_disabled", str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise RemoteBaselineResolutionError("project_not_found", str(exc)) from exc


def resolve_remote_baseline(
    project_reference: str,
    registry: Optional[ProjectRegistry] = None,
    github_fetch: Callable[..., Dict[str, Any]] = default_github_fetch,
    github_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve `project_reference` (a canonical project_id or registered
    alias -- never a repo, branch, or commit) to a fully server-authoritative
    baseline record.

    Returns a dict with exactly: project_id, repository, canonical_branch,
    baseline_sha, source, resolved_at. Raises RemoteBaselineResolutionError
    (fail closed) on every ambiguous, missing, or unverifiable input --
    there is no code path that returns a partial or best-guess result.
    """
    registry = registry or get_global_registry()
    project = _resolve_project(registry, project_reference)

    if github_token is None:
        github_token = os.environ.get(GITHUB_TOKEN_ENV_VAR) or None

    repo = project.repo if isinstance(project.repo, dict) else None
    owner = repo.get("owner") if repo else None
    name = repo.get("name") if repo else None
    if not isinstance(owner, str) or not owner.strip() or not isinstance(name, str) or not name.strip():
        raise RemoteBaselineResolutionError(
            "registry_repo_missing",
            f"project {project.project_id!r} has no registered owner/name repository identity",
        )

    policy = project.baseline_resolution_policy if isinstance(project.baseline_resolution_policy, dict) else {}
    strategy = policy.get("strategy")
    pinned_ref = policy.get("pinned_ref")

    if strategy == STRATEGY_ORIGIN_DEFAULT:
        if pinned_ref:
            raise RemoteBaselineResolutionError(
                "baseline_policy_contradiction",
                f"project {project.project_id!r} declares strategy 'origin_default' but also sets a non-empty "
                f"pinned_ref {pinned_ref!r}; refusing to guess which one is authoritative",
            )
        branch = project.default_branch
    elif strategy == STRATEGY_PINNED_COMMIT:
        if not isinstance(pinned_ref, str) or not pinned_ref.strip():
            raise RemoteBaselineResolutionError(
                "pinned_ref_missing",
                f"project {project.project_id!r} declares strategy 'pinned_commit' but has no non-empty pinned_ref",
            )
        # Immutability: "pinned_commit" means pinned to one exact, durable
        # commit identity -- never a mutable ref (a branch or tag name can
        # move; a short SHA is ambiguous and can even stop resolving as
        # more objects are packed). pinned_ref must match the same
        # BASELINE_HEAD_PATTERN contract every other baseline_head in this
        # codebase is already held to (schema/task.schema.json), i.e. a
        # full 40-hex (SHA-1) or 64-hex (SHA-256) commit id.
        if not BASELINE_HEAD_PATTERN.match(pinned_ref.strip()):
            raise RemoteBaselineResolutionError(
                "pinned_ref_not_immutable",
                f"project {project.project_id!r} declares strategy 'pinned_commit' with pinned_ref {pinned_ref!r}, "
                "which is not a full commit SHA (40 or 64 hex characters) -- branch names, tag names, short SHAs, "
                "and other mutable/ambiguous refs are not accepted as an immutable pin",
            )
        branch = pinned_ref
    else:
        raise RemoteBaselineResolutionError(
            "unsupported_baseline_strategy",
            f"project {project.project_id!r} declares baseline_resolution_policy.strategy {strategy!r}, which is "
            f"not implemented (supported: {sorted(SUPPORTED_BASELINE_STRATEGIES)}); refusing to silently "
            "reinterpret it as origin_default",
        )
    if not isinstance(branch, str) or not branch.strip():
        raise RemoteBaselineResolutionError(
            "registry_branch_missing", f"project {project.project_id!r} has no resolvable canonical branch")
    branch = branch.strip()

    body = github_fetch(owner.strip(), name.strip(), branch, token=github_token)
    if not isinstance(body, dict):
        raise RemoteBaselineResolutionError(
            "remote_api_unavailable",
            f"GitHub API returned a non-object response for {owner}/{name}@{branch}",
        )

    sha = body.get("sha")
    if not isinstance(sha, str) or not BASELINE_HEAD_PATTERN.match(sha):
        raise RemoteBaselineResolutionError(
            "malformed_remote_sha",
            f"GitHub API returned a malformed or missing commit sha for {owner}/{name}@{branch}: {sha!r}",
        )

    return {
        "project_id": project.project_id,
        "repository": project.repo_url,
        "canonical_branch": branch,
        "baseline_sha": sha,
        "source": REMOTE_BASELINE_SOURCE,
        "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
