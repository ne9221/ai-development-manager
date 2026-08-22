#!/usr/bin/env python3
"""Canonical Global Project Registry & Resolver (Slice B).

Enables ADM and downstream components to resolve registered development projects
by canonical project_id, alias, or repository identity without relying on ChatGPT
conversation memory or hardcoded machine-specific HOME paths.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


# Default locations for the canonical project registry
DEFAULT_REGISTRY_FILENAMES = (
    "project-registry.json",
    "manager/project_registry.json",
    "config/project_registry.json",
)


class ProjectRegistryError(Exception):
    """Base exception for all Project Registry and Resolver failures."""


class ProjectNotFoundError(ProjectRegistryError):
    """Raised when a requested project cannot be resolved (fail-closed)."""


class AmbiguousProjectError(ProjectRegistryError):
    """Raised when an alias or query resolves to multiple distinct projects (fail-closed)."""


class DuplicateRepositoryError(ProjectRegistryError):
    """Raised when multiple projects claim ownership of the same repository identity (fail-closed)."""


class InvalidProjectRegistryError(ProjectRegistryError):
    """Raised when the project registry data structure is malformed or invalid."""


class UnresolvedProjectError(ProjectRegistryError):
    """Raised when write dispatch is requested for an unverified/unresolved project."""


class ProjectDisabledError(ProjectRegistryError):
    """Raised when interacting with a disabled or archived project without explicit bypass."""


class GovernanceRuleMissingError(ProjectRegistryError):
    """Raised when required PROJECT-RULES or repository metadata are missing for write dispatch."""


def normalize_identifier(ident: str) -> str:
    """Normalize project identifier or alias for case-insensitive lookup."""
    if not isinstance(ident, str):
        return ""
    return ident.strip().lower()


def normalize_repo_identity(repo_input: Union[str, Dict[str, Any], None]) -> Optional[str]:
    """Normalize a repository URL, SSH remote, or owner/repo string to a canonical identity.
    
    Examples:
        'https://github.com/ne9221/ai-development-manager.git' -> 'github.com/ne9221/ai-development-manager'
        'git@github.com:ne9221/ai-development-manager.git'     -> 'github.com/ne9221/ai-development-manager'
        'ne9221/ai-development-manager'                       -> 'github.com/ne9221/ai-development-manager'
    """
    if repo_input is None:
        return None

    raw_url = ""
    if isinstance(repo_input, dict):
        raw_url = repo_input.get("canonical_url") or ""
        if not raw_url and repo_input.get("owner") and repo_input.get("name"):
            raw_url = f"https://github.com/{repo_input['owner']}/{repo_input['name']}"
    elif isinstance(repo_input, str):
        raw_url = repo_input.strip()

    if not raw_url:
        return None

    # Handle SSH git@github.com:owner/repo.git
    ssh_match = re.match(r"^git@([^:]+):(.+)$", raw_url)
    if ssh_match:
        host, path = ssh_match.groups()
        clean_path = path.lstrip("/").removesuffix(".git")
        return f"{host.lower()}/{clean_path.lower()}"

    # Handle http:// or https://
    url_match = re.match(r"^https?://([^/]+)/(.+)$", raw_url, re.IGNORECASE)
    if url_match:
        host, path = url_match.groups()
        clean_path = path.lstrip("/").removesuffix(".git")
        return f"{host.lower()}/{clean_path.lower()}"

    # Handle domain-prefixed or bare owner/repo
    clean_str = raw_url.removesuffix(".git").strip("/")
    parts = clean_str.split("/")
    if len(parts) == 2:
        return f"github.com/{parts[0].lower()}/{parts[1].lower()}"
    elif len(parts) >= 3 and "." in parts[0]:
        return f"{parts[0].lower()}/{'/'.join(p.lower() for p in parts[1:])}"

    return clean_str.lower()


@dataclass(frozen=True)
class ProjectMetadata:
    """Immutable representation of a registered project's metadata."""
    project_id: str
    display_name: str
    aliases: tuple[str, ...]
    repo: Optional[Dict[str, Any]]
    default_branch: str
    baseline_resolution_policy: Dict[str, Any]
    common_governance: Dict[str, Any]
    project_rules: Dict[str, Any]
    working_directory_policy: Dict[str, Any]
    isolation_policy: Dict[str, Any]
    provider_restrictions: Dict[str, Any]
    protected_paths: tuple[str, ...]
    default_write_boundaries: tuple[str, ...]
    pointer_rules: Dict[str, Any]
    status: str  # "enabled", "disabled", "archived"
    resolution_status: str  # "verified", "unresolved"
    unresolved_reason: Optional[str] = None

    @property
    def repo_url(self) -> Optional[str]:
        if self.repo and isinstance(self.repo, dict):
            return self.repo.get("canonical_url")
        return None

    @property
    def repo_identity(self) -> Optional[str]:
        return normalize_repo_identity(self.repo)

    def resolve_runtime_working_directory(
        self,
        workspace_root: Optional[Union[str, Path]] = None,
        override_path: Optional[Union[str, Path]] = None,
    ) -> Path:
        """Resolve the machine-independent working directory for this project at runtime.
        
        Does NOT mutate or pollute canonical registry metadata.
        """
        if override_path is not None:
            return Path(override_path)

        relative_path = self.working_directory_policy.get("relative_path", self.project_id)
        
        if workspace_root is not None:
            return Path(workspace_root) / relative_path

        env_var_name = self.working_directory_policy.get("env_var", "ADM_WORKSPACE_ROOT")
        env_root = os.environ.get(env_var_name)
        if env_root:
            return Path(env_root) / relative_path

        # Default fallback to current working directory or relative path
        return Path.cwd() / relative_path

    def validate_write_dispatch_preconditions(self) -> None:
        """Validate that this project is in an active, verified, and complete state for write dispatch."""
        if self.status != "enabled":
            raise ProjectDisabledError(f"project {self.project_id!r} is {self.status}; write dispatch is disabled")

        if self.resolution_status != "verified":
            reason = self.unresolved_reason or "project metadata is unverified from cloud SSOT"
            raise UnresolvedProjectError(
                f"project {self.project_id!r} is marked 'unresolved' ({reason}); write dispatch rejected"
            )

        if not self.repo_url:
            raise GovernanceRuleMissingError(
                f"project {self.project_id!r} is missing canonical repository metadata; write dispatch rejected"
            )

        rule_ref = self.project_rules.get("reference") if isinstance(self.project_rules, dict) else None
        if not rule_ref:
            raise GovernanceRuleMissingError(
                f"project {self.project_id!r} is missing canonical PROJECT-RULES reference; write dispatch rejected"
            )

        gov_ref = self.common_governance.get("reference") if isinstance(self.common_governance, dict) else None
        if not gov_ref:
            raise GovernanceRuleMissingError(
                f"project {self.project_id!r} is missing common governance reference; write dispatch rejected"
            )


class ProjectRegistry:
    """Project-agnostic Global Project Registry and Resolver."""

    def __init__(self, projects: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        self._projects_by_id: Dict[str, ProjectMetadata] = {}
        self._alias_map: Dict[str, str] = {}  # normalized_alias -> canonical project_id
        self._repo_map: Dict[str, str] = {}   # normalized_repo_identity -> canonical project_id
        self._raw_entries: Dict[str, Dict[str, Any]] = {}

        if projects:
            for p in projects:
                self.register_project(p)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProjectRegistry:
        if not isinstance(data, dict) or "projects" not in data:
            raise InvalidProjectRegistryError("registry data must be a dictionary containing 'projects' list")
        return cls(projects=data["projects"])

    @classmethod
    def from_file(cls, file_path: Union[str, Path]) -> ProjectRegistry:
        path = Path(file_path)
        if not path.exists():
            raise InvalidProjectRegistryError(f"project registry file does not exist: {path}")
        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as exc:
            raise InvalidProjectRegistryError(f"failed to read/parse project registry file {path}: {exc}") from exc
        return cls.from_dict(data)

    def register_project(self, raw_entry: Dict[str, Any]) -> ProjectMetadata:
        """Register and validate a single project entry."""
        if not isinstance(raw_entry, dict):
            raise InvalidProjectRegistryError("project entry must be a dictionary")

        project_id = raw_entry.get("project_id")
        if not project_id or not isinstance(project_id, str):
            raise InvalidProjectRegistryError("project entry missing valid 'project_id'")

        canonical_id = project_id.strip()
        if canonical_id in self._projects_by_id:
            raise InvalidProjectRegistryError(f"duplicate canonical project_id registered: {canonical_id}")

        display_name = raw_entry.get("display_name", canonical_id)
        aliases = tuple(str(a).strip() for a in raw_entry.get("aliases", []) if str(a).strip())
        default_branch = raw_entry.get("default_branch", "main")
        status = raw_entry.get("status", "enabled")
        resolution_status = raw_entry.get("resolution_status", "verified")
        unresolved_reason = raw_entry.get("unresolved_reason")

        repo = raw_entry.get("repo")
        baseline_policy = raw_entry.get("baseline_resolution_policy", {"strategy": "origin_default"})
        common_governance = raw_entry.get("common_governance", {})
        project_rules = raw_entry.get("project_rules", {})
        working_directory_policy = raw_entry.get("working_directory_policy", {
            "relative_path": canonical_id,
            "resolver_strategy": "workspace_relative",
        })
        isolation_policy = raw_entry.get("isolation_policy", {"mode": "worktree_per_task"})
        provider_restrictions = raw_entry.get("provider_restrictions", {})
        protected_paths = tuple(raw_entry.get("protected_paths", []))
        default_write_boundaries = tuple(raw_entry.get("default_write_boundaries", ["*"]))
        pointer_rules = raw_entry.get("pointer_rules", {
            "tasks_area": "tasks",
            "executions_area": "executions",
            "handoffs_area": "handoffs",
            "sessions_area": "sessions",
            "overviews_area": "overviews",
        })

        metadata = ProjectMetadata(
            project_id=canonical_id,
            display_name=display_name,
            aliases=aliases,
            repo=repo,
            default_branch=default_branch,
            baseline_resolution_policy=baseline_policy,
            common_governance=common_governance,
            project_rules=project_rules,
            working_directory_policy=working_directory_policy,
            isolation_policy=isolation_policy,
            provider_restrictions=provider_restrictions,
            protected_paths=protected_paths,
            default_write_boundaries=default_write_boundaries,
            pointer_rules=pointer_rules,
            status=status,
            resolution_status=resolution_status,
            unresolved_reason=unresolved_reason,
        )

        # Index and check for duplicate repository identities
        repo_ident = metadata.repo_identity
        if repo_ident:
            if repo_ident in self._repo_map:
                existing_id = self._repo_map[repo_ident]
                raise DuplicateRepositoryError(
                    f"duplicate repository identity {repo_ident!r} claimed by both {existing_id!r} and {canonical_id!r}"
                )
            self._repo_map[repo_ident] = canonical_id

        # Index canonical ID into alias map
        norm_canonical = normalize_identifier(canonical_id)
        self._alias_map[norm_canonical] = canonical_id

        # Index aliases and display name into alias map with ambiguity detection
        all_aliases = set(aliases)
        if display_name:
            all_aliases.add(display_name)

        for alias in all_aliases:
            norm_alias = normalize_identifier(alias)
            if not norm_alias:
                continue
            if norm_alias in self._alias_map:
                existing_id = self._alias_map[norm_alias]
                if existing_id != canonical_id:
                    raise AmbiguousProjectError(
                        f"ambiguous alias {alias!r} (normalized: {norm_alias!r}) maps to multiple projects: "
                        f"{existing_id!r} and {canonical_id!r}"
                    )
            else:
                self._alias_map[norm_alias] = canonical_id

        self._projects_by_id[canonical_id] = metadata
        self._raw_entries[canonical_id] = raw_entry
        return metadata

    def get_project(self, query: str, allow_disabled: bool = False) -> ProjectMetadata:
        """Resolve a project by canonical project_id, alias, or repository identity.
        
        Fails closed with ProjectNotFoundError, AmbiguousProjectError, or ProjectDisabledError.
        """
        if not isinstance(query, str) or not query.strip():
            raise ProjectNotFoundError("project query must be a non-empty string")

        clean_query = query.strip()
        
        # 1. Direct canonical ID lookup
        if clean_query in self._projects_by_id:
            proj = self._projects_by_id[clean_query]
            if proj.status != "enabled" and not allow_disabled:
                raise ProjectDisabledError(f"project {proj.project_id!r} is disabled")
            return proj

        # 2. Normalized alias / ID / display name lookup
        norm_query = normalize_identifier(clean_query)
        if norm_query in self._alias_map:
            canonical_id = self._alias_map[norm_query]
            proj = self._projects_by_id[canonical_id]
            if proj.status != "enabled" and not allow_disabled:
                raise ProjectDisabledError(f"project {proj.project_id!r} is disabled")
            return proj

        # 3. Repository identity lookup
        repo_ident = normalize_repo_identity(clean_query)
        if repo_ident and repo_ident in self._repo_map:
            canonical_id = self._repo_map[repo_ident]
            proj = self._projects_by_id[canonical_id]
            if proj.status != "enabled" and not allow_disabled:
                raise ProjectDisabledError(f"project {proj.project_id!r} is disabled")
            return proj

        raise ProjectNotFoundError(f"unknown project: {query!r} could not be resolved in project registry")

    def resolve_for_dispatch(self, query: str, write: bool = False) -> ProjectMetadata:
        """Resolve project for execution dispatch. If write=True, enforces all write preconditions."""
        proj = self.get_project(query, allow_disabled=False)
        if write:
            proj.validate_write_dispatch_preconditions()
        return proj

    def list_projects(
        self,
        status: Optional[str] = None,
        resolution_status: Optional[str] = None,
    ) -> List[ProjectMetadata]:
        """List registered projects, optionally filtered by status."""
        results = list(self._projects_by_id.values())
        if status:
            results = [p for p in results if p.status == status]
        if resolution_status:
            results = [p for p in results if p.resolution_status == resolution_status]
        return results

    def get_raw_entry(self, project_id: str) -> Dict[str, Any]:
        """Retrieve raw unmodified registry dictionary for a project."""
        if project_id not in self._raw_entries:
            raise ProjectNotFoundError(f"project {project_id!r} not found in raw registry")
        return self._raw_entries[project_id]


def resolve_authoritative_working_directory(project_id: str, fallback: Optional[str] = None) -> Optional[str]:
    """Resolve the authoritative, machine-local working_directory for a
    project_id via the Global Project Registry + its configured workspace
    env var (default ADM_WORKSPACE_ROOT) -- never from an unmaintained
    literal path stored elsewhere (e.g. a cloud Project record's own
    `working_directory` field, which nothing keeps synchronized with the
    actual current checkout; see
    fix/direct-dispatch-working-directory-authority-p0-20260822).

    Falls back to `fallback` unchanged whenever the project is not
    registered, or its configured workspace env var is not set on this
    machine, so this never regresses a project that hasn't been migrated
    onto the registry's workspace_root convention yet.
    """
    try:
        project = get_global_registry().get_project(project_id, allow_disabled=True)
    except ProjectRegistryError:
        return fallback
    env_var_name = project.working_directory_policy.get("env_var", "ADM_WORKSPACE_ROOT")
    workspace_root = os.environ.get(env_var_name)
    if not workspace_root:
        return fallback
    return str(project.resolve_runtime_working_directory(workspace_root=workspace_root))


_GLOBAL_REGISTRY: Optional[ProjectRegistry] = None


def find_canonical_registry_path() -> Path:
    """Locate the canonical project-registry JSON file in standard locations."""
    env_path = os.environ.get("ADM_PROJECT_REGISTRY_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    repo_root = Path(__file__).parents[1]
    for filename in DEFAULT_REGISTRY_FILENAMES:
        candidate = repo_root / filename
        if candidate.exists():
            return candidate

    manager_dir = Path(__file__).parent
    candidate_mgr = manager_dir / "project_registry.json"
    if candidate_mgr.exists():
        return candidate_mgr

    raise InvalidProjectRegistryError("could not find canonical project registry file in standard locations")


def load_project_registry(path: Optional[Union[str, Path]] = None) -> ProjectRegistry:
    """Load project registry from explicit path or default location."""
    target_path = Path(path) if path else find_canonical_registry_path()
    return ProjectRegistry.from_file(target_path)


def get_global_registry(reload: bool = False) -> ProjectRegistry:
    """Get or initialize the cached global project registry."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None or reload:
        _GLOBAL_REGISTRY = load_project_registry()
    return _GLOBAL_REGISTRY
