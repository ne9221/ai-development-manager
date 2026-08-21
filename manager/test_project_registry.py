#!/usr/bin/env python3
"""Tests for Global Project Registry and Resolver (Slice B)."""

import json
import pytest
from pathlib import Path

from manager.project_registry import (
    AmbiguousProjectError,
    DuplicateRepositoryError,
    GovernanceRuleMissingError,
    InvalidProjectRegistryError,
    ProjectDisabledError,
    ProjectNotFoundError,
    ProjectRegistry,
    ProjectRegistryError,
    UnresolvedProjectError,
    get_global_registry,
    load_project_registry,
    normalize_identifier,
    normalize_repo_identity,
)


def test_canonical_project_id_resolve_pass():
    """canonical project_id resolve PASS."""
    registry = get_global_registry(reload=True)
    proj = registry.get_project("ai-development-manager")
    assert proj.project_id == "ai-development-manager"
    assert proj.display_name == "AI Development Manager (AI 一體化)"
    assert proj.default_branch == "main"
    assert proj.status == "enabled"
    assert proj.resolution_status == "verified"


def test_alias_resolve_pass():
    """alias resolve PASS across aliases, display names, and casing."""
    registry = get_global_registry()
    
    # Check distinct aliases
    aliases_to_test = ["adm", "ADM", "ai-dev-mgr", "AI 一體化", "ai_development_manager"]
    for alias in aliases_to_test:
        proj = registry.get_project(alias)
        assert proj.project_id == "ai-development-manager", f"Failed to resolve alias: {alias}"

    # Check non-ADM project alias
    ledger_proj = registry.get_project("明細帳整理器")
    assert ledger_proj.project_id == "ledger-organizer"
    
    ledger_alias = registry.get_project("ledger")
    assert ledger_alias.project_id == "ledger-organizer"


def test_repo_identity_resolve_pass():
    """repo identity resolve PASS for HTTPS, SSH, owner/repo, with/without .git."""
    registry = get_global_registry()
    
    queries = [
        "https://github.com/ne9221/ai-development-manager.git",
        "https://github.com/ne9221/ai-development-manager",
        "http://github.com/ne9221/ai-development-manager.git",
        "git@github.com:ne9221/ai-development-manager.git",
        "ne9221/ai-development-manager",
        "github.com/ne9221/ai-development-manager",
    ]
    for q in queries:
        proj = registry.get_project(q)
        assert proj.project_id == "ai-development-manager", f"Failed to resolve repo identity: {q}"


def test_unknown_project_fail_closed():
    """unknown project FAIL CLOSED (raises ProjectNotFoundError)."""
    registry = get_global_registry()
    with pytest.raises(ProjectNotFoundError) as exc_info:
        registry.get_project("non-existent-project-xyz")
    assert "unknown project" in str(exc_info.value).lower()
    assert issubclass(ProjectNotFoundError, ProjectRegistryError)


def test_ambiguous_alias_fail_closed():
    """ambiguous alias FAIL CLOSED (raises AmbiguousProjectError)."""
    sample_data = {
        "schema_version": "1.0.0",
        "updated_at": "2026-08-21T00:00:00Z",
        "projects": [
            {
                "project_id": "proj-a",
                "display_name": "Project A",
                "aliases": ["common-alias", "alpha"],
                "repo": {"canonical_url": "https://github.com/example/proj-a.git"},
                "default_branch": "main",
                "status": "enabled",
                "resolution_status": "verified",
            },
            {
                "project_id": "proj-b",
                "display_name": "Project B",
                "aliases": ["common-alias", "beta"],
                "repo": {"canonical_url": "https://github.com/example/proj-b.git"},
                "default_branch": "main",
                "status": "enabled",
                "resolution_status": "verified",
            },
        ],
    }
    with pytest.raises(AmbiguousProjectError) as exc_info:
        ProjectRegistry.from_dict(sample_data)
    assert "ambiguous alias" in str(exc_info.value).lower()


def test_duplicate_repository_mapping_fail_closed():
    """duplicate repository mapping FAIL CLOSED (raises DuplicateRepositoryError)."""
    sample_data = {
        "schema_version": "1.0.0",
        "updated_at": "2026-08-21T00:00:00Z",
        "projects": [
            {
                "project_id": "proj-one",
                "display_name": "Project One",
                "aliases": ["p1"],
                "repo": {"canonical_url": "https://github.com/example/shared-repo.git"},
                "default_branch": "main",
                "status": "enabled",
                "resolution_status": "verified",
            },
            {
                "project_id": "proj-two",
                "display_name": "Project Two",
                "aliases": ["p2"],
                "repo": {"canonical_url": "git@github.com:example/shared-repo.git"},
                "default_branch": "main",
                "status": "enabled",
                "resolution_status": "verified",
            },
        ],
    }
    with pytest.raises(DuplicateRepositoryError) as exc_info:
        ProjectRegistry.from_dict(sample_data)
    assert "duplicate repository" in str(exc_info.value).lower()


def test_missing_project_rules_fail_closed_for_write_dispatch():
    """missing PROJECT-RULES FAIL CLOSED for write dispatch."""
    sample_data = {
        "schema_version": "1.0.0",
        "updated_at": "2026-08-21T00:00:00Z",
        "projects": [
            {
                "project_id": "no-rules-proj",
                "display_name": "No Rules Project",
                "aliases": ["nr"],
                "repo": {"canonical_url": "https://github.com/example/no-rules.git"},
                "default_branch": "main",
                "status": "enabled",
                "resolution_status": "verified",
                # Missing project_rules
            }
        ],
    }
    registry = ProjectRegistry.from_dict(sample_data)
    proj = registry.get_project("no-rules-proj")
    assert proj.project_id == "no-rules-proj"

    with pytest.raises(GovernanceRuleMissingError) as exc_info:
        registry.resolve_for_dispatch("no-rules-proj", write=True)
    assert "project-rules" in str(exc_info.value).lower()


def test_missing_repo_metadata_fail_closed_for_write_dispatch():
    """missing repo metadata FAIL CLOSED for write dispatch."""
    sample_data = {
        "schema_version": "1.0.0",
        "updated_at": "2026-08-21T00:00:00Z",
        "projects": [
            {
                "project_id": "no-repo-proj",
                "display_name": "No Repo Project",
                "aliases": ["norepo"],
                "repo": None,
                "default_branch": "main",
                "project_rules": {"reference": "PROJECT-RULES.md"},
                "common_governance": {"reference": "governance-rules.json"},
                "status": "enabled",
                "resolution_status": "verified",
            }
        ],
    }
    registry = ProjectRegistry.from_dict(sample_data)
    with pytest.raises(GovernanceRuleMissingError) as exc_info:
        registry.resolve_for_dispatch("no-repo-proj", write=True)
    assert "repository metadata" in str(exc_info.value).lower()


def test_disabled_project_fail_closed():
    """disabled project FAIL CLOSED."""
    sample_data = {
        "schema_version": "1.0.0",
        "updated_at": "2026-08-21T00:00:00Z",
        "projects": [
            {
                "project_id": "disabled-proj",
                "display_name": "Disabled Project",
                "aliases": ["dis"],
                "repo": {"canonical_url": "https://github.com/example/disabled.git"},
                "default_branch": "main",
                "status": "disabled",
                "resolution_status": "verified",
            }
        ],
    }
    registry = ProjectRegistry.from_dict(sample_data)
    
    # get_project fails closed by default
    with pytest.raises(ProjectDisabledError) as exc_info:
        registry.get_project("disabled-proj")
    assert "disabled" in str(exc_info.value).lower()
    
    # Explicit allow_disabled=True returns the project object
    disabled_proj = registry.get_project("disabled-proj", allow_disabled=True)
    assert disabled_proj.status == "disabled"

    # resolve_for_dispatch always fails closed
    with pytest.raises(ProjectDisabledError):
        registry.resolve_for_dispatch("disabled-proj", write=False)


def test_unresolved_project_rejects_write_dispatch():
    """unresolved project rejects write dispatch."""
    registry = get_global_registry()
    # ledger-organizer is currently marked unresolved until verified from cloud SSOT
    proj = registry.get_project("ledger-organizer")
    assert proj.resolution_status == "unresolved"

    # Read resolution succeeds
    read_resolved = registry.resolve_for_dispatch("ledger-organizer", write=False)
    assert read_resolved.project_id == "ledger-organizer"

    # Write dispatch MUST fail closed
    with pytest.raises(UnresolvedProjectError) as exc_info:
        registry.resolve_for_dispatch("ledger-organizer", write=True)
    assert "unresolved" in str(exc_info.value).lower()


def test_machine_specific_runtime_mapping_does_not_pollute_canonical_registry(tmp_path):
    """machine-specific runtime mapping does not pollute canonical registry (no hardcoded HOME)."""
    registry = get_global_registry()
    proj = registry.get_project("ai-development-manager")
    
    # Default policy uses machine-independent relative path
    assert proj.working_directory_policy.get("relative_path") == "ai-development-manager"

    # Compute runtime paths with different workspace roots
    custom_root_1 = tmp_path / "workspace_a"
    custom_root_2 = tmp_path / "workspace_b"
    
    path_1 = proj.resolve_runtime_working_directory(workspace_root=custom_root_1)
    path_2 = proj.resolve_runtime_working_directory(workspace_root=custom_root_2)
    
    assert path_1 == custom_root_1 / "ai-development-manager"
    assert path_2 == custom_root_2 / "ai-development-manager"
    assert path_1 != path_2

    # Verify canonical registry object and raw JSON are completely unpolluted
    raw_entry = registry.get_raw_entry("ai-development-manager")
    assert "C:\\Users" not in str(raw_entry)
    assert "/home/" not in str(raw_entry)


def test_extensible_new_registry_entry_no_code_change():
    """adding a registry entry does not need resolver code modification."""
    custom_registry_data = {
        "schema_version": "1.0.0",
        "updated_at": "2026-08-21T00:00:00Z",
        "projects": [
            {
                "project_id": "future-custom-project-2027",
                "display_name": "Future Custom Project 2027",
                "aliases": ["fcp", "future_custom"],
                "repo": {
                    "canonical_url": "https://github.com/org/future-custom-project-2027.git",
                    "owner": "org",
                    "name": "future-custom-project-2027",
                },
                "default_branch": "master",
                "baseline_resolution_policy": {"strategy": "origin_default"},
                "common_governance": {"reference": "governance-rules.json"},
                "project_rules": {"reference": "PROJECT-RULES.md"},
                "working_directory_policy": {
                    "relative_path": "future-custom-project-2027",
                    "resolver_strategy": "workspace_relative",
                },
                "isolation_policy": {"mode": "worktree_per_task"},
                "status": "enabled",
                "resolution_status": "verified",
            }
        ],
    }
    registry = ProjectRegistry.from_dict(custom_registry_data)
    
    # Resolve by ID, alias, and repo identity without touching any python code
    p_id = registry.get_project("future-custom-project-2027")
    p_alias = registry.get_project("future_custom")
    p_repo = registry.get_project("https://github.com/org/future-custom-project-2027")
    
    assert p_id.project_id == "future-custom-project-2027"
    assert p_alias.project_id == "future-custom-project-2027"
    assert p_repo.project_id == "future-custom-project-2027"
    
    # Verify write dispatch precondition check passes
    dispatched = registry.resolve_for_dispatch("fcp", write=True)
    assert dispatched.project_id == "future-custom-project-2027"


def test_adm_and_non_adm_project_same_resolver_contract():
    """ADM and at least one non-ADM project can be resolved via same resolver contract."""
    registry = get_global_registry()
    
    adm = registry.get_project("ai-development-manager")
    ledger = registry.get_project("ledger-organizer")
    outlook = registry.get_project("outlook-mail")
    email = registry.get_project("email-organizer")
    bookkeeping = registry.get_project("bookkeeping")
    
    for p in [adm, ledger, outlook, email, bookkeeping]:
        assert hasattr(p, "project_id")
        assert hasattr(p, "display_name")
        assert hasattr(p, "aliases")
        assert hasattr(p, "repo_url")
        assert hasattr(p, "default_branch")
        assert hasattr(p, "status")
        assert hasattr(p, "resolution_status")
        assert p.status == "enabled"


def test_normalize_helpers():
    """Test identifier and repo normalization helpers."""
    assert normalize_identifier("  AI 一體化  ") == "ai 一體化"
    assert normalize_identifier("ai_development_manager") == "ai_development_manager"
    
    assert normalize_repo_identity("https://github.com/ne9221/ai-development-manager.git") == "github.com/ne9221/ai-development-manager"
    assert normalize_repo_identity("git@github.com:ne9221/ai-development-manager.git") == "github.com/ne9221/ai-development-manager"
    assert normalize_repo_identity("ne9221/ai-development-manager") == "github.com/ne9221/ai-development-manager"
    assert normalize_repo_identity({"canonical_url": "https://github.com/ne9221/ai-development-manager"}) == "github.com/ne9221/ai-development-manager"


def test_project_registry_schema_validation():
    """Verify that project-registry.json validates against schema/project_registry.schema.json."""
    import jsonschema
    
    repo_root = Path(__file__).parents[1]
    schema_path = repo_root / "schema" / "project_registry.schema.json"
    registry_path = repo_root / "project-registry.json"
    
    assert schema_path.exists()
    assert registry_path.exists()
    
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    
    # jsonschema.validate raises ValidationError if invalid
    jsonschema.validate(instance=registry_data, schema=schema)

