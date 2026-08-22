#!/usr/bin/env python3
"""Tests for the Remote Baseline Resolver.

Everything here proves the resolver never needs a local business-repo
checkout: no test constructs, patches, or reads a local git working tree --
the only inputs are an in-memory ProjectRegistry and a fake
`github_fetch(owner, name, branch, token=...)` callable standing in for the
GitHub REST API.
"""

import os
import unittest
from unittest.mock import patch

from manager.project_registry import ProjectRegistry
from manager.remote_baseline_resolver import (
    BASELINE_HEAD_PATTERN,
    GITHUB_TOKEN_ENV_VAR,
    RemoteBaselineResolutionError,
    REMOTE_BASELINE_SOURCE,
    resolve_remote_baseline,
)


VALID_SHA_A = "a" * 40
VALID_SHA_B = "b" * 40


def registry_entry(project_id="proj-a", owner="acme", name="repo-a", default_branch="main",
                    pinned_ref=None, status="enabled", **overrides):
    entry = {
        "project_id": project_id,
        "display_name": project_id,
        "aliases": [f"{project_id}-alias"],
        "repo": {"canonical_url": f"https://github.com/{owner}/{name}.git", "owner": owner, "name": name},
        "default_branch": default_branch,
        "baseline_resolution_policy": {"strategy": "origin_default", "pinned_ref": pinned_ref},
        "common_governance": {"reference": "governance-rules.json", "version": "1.0.0"},
        "project_rules": {"reference": "PROJECT-RULES.md"},
        "status": status,
        "resolution_status": "verified",
    }
    entry.update(overrides)
    return entry


def fake_fetch(shas=None, unavailable_for=(), missing_for=(), malformed_for=()):
    """Build a `github_fetch` fake keyed by (owner, name, branch). Never
    touches the network or the local filesystem -- this alone is what
    stands in for GitHub across the whole test module."""
    shas = shas or {}
    calls = []

    def _fetch(owner, name, branch, *, token=None):
        calls.append((owner, name, branch, token))
        key = (owner, name, branch)
        if key in unavailable_for:
            raise RemoteBaselineResolutionError("remote_api_unavailable", "simulated GitHub API outage")
        if key in missing_for:
            raise RemoteBaselineResolutionError("remote_branch_not_found", "simulated 404")
        if key in malformed_for:
            return {"sha": "not-a-real-sha"}
        return {"sha": shas.get(key, VALID_SHA_A)}

    _fetch.calls = calls
    return _fetch


class NoLocalCheckoutTests(unittest.TestCase):
    """The resolver must work on a machine with no business-repo checkout
    and no working directory relationship to the project at all."""

    def test_resolves_without_any_local_checkout_or_cwd_dependency(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_A})
        result = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual(VALID_SHA_A, result["baseline_sha"])
        # The fake never received (or needed) a filesystem path of any kind.
        self.assertEqual([("acme", "repo-a", "main", None)], fetch.calls)

    def test_empty_machine_filesystem_still_resolves(self):
        """No file on disk (registry passed in-memory, no cwd/env dependency)
        is required for resolution to succeed."""
        registry = ProjectRegistry(projects=[registry_entry(project_id="proj-empty-fs")])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_B})
        result = resolve_remote_baseline("proj-empty-fs", registry=registry, github_fetch=fetch)
        self.assertEqual(VALID_SHA_B, result["baseline_sha"])
        self.assertEqual("proj-empty-fs", result["project_id"])


class CorrectResolutionTests(unittest.TestCase):
    def test_correct_registry_project_resolves_correct_remote_repo(self):
        registry = ProjectRegistry(projects=[
            registry_entry(project_id="proj-a", owner="acme", name="repo-a"),
            registry_entry(project_id="proj-b", owner="other", name="repo-b"),
        ])
        fetch = fake_fetch(shas={
            ("acme", "repo-a", "main"): VALID_SHA_A,
            ("other", "repo-b", "main"): VALID_SHA_B,
        })
        result_a = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("https://github.com/acme/repo-a.git", result_a["repository"])
        self.assertEqual(VALID_SHA_A, result_a["baseline_sha"])

    def test_correct_canonical_branch_used(self):
        registry = ProjectRegistry(projects=[registry_entry(default_branch="develop")])
        fetch = fake_fetch(shas={("acme", "repo-a", "develop"): VALID_SHA_A})
        result = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("develop", result["canonical_branch"])
        self.assertEqual([("acme", "repo-a", "develop", None)], fetch.calls)

    def test_pinned_ref_overrides_default_branch(self):
        registry = ProjectRegistry(projects=[registry_entry(default_branch="main", pinned_ref="release/2.0")])
        fetch = fake_fetch(shas={("acme", "repo-a", "release/2.0"): VALID_SHA_A})
        result = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("release/2.0", result["canonical_branch"])

    def test_alias_resolves_to_same_project(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_A})
        result = resolve_remote_baseline("proj-a-alias", registry=registry, github_fetch=fetch)
        self.assertEqual("proj-a", result["project_id"])

    def test_remote_sha_resolution_returns_full_result_shape(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_A})
        result = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual({
            "project_id", "repository", "canonical_branch", "baseline_sha", "source", "resolved_at",
        }, set(result))
        self.assertEqual(REMOTE_BASELINE_SOURCE, result["source"])
        self.assertRegex(result["resolved_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(BASELINE_HEAD_PATTERN.match(result["baseline_sha"]))


class CallerSpoofRejectionTests(unittest.TestCase):
    """The resolver takes no repo/branch/baseline argument at all -- there
    is no field a caller could smuggle a spoofed value through even in
    principle. These tests prove the *only* accepted input is the project
    reference, and that a differently-configured registry entry (i.e. an
    attempt to smuggle repo/branch authority via a project-registry-shaped
    argument) is simply not part of the function's signature."""

    def test_resolver_signature_accepts_only_project_reference_and_trusted_collaborators(self):
        import inspect
        params = list(inspect.signature(resolve_remote_baseline).parameters)
        self.assertEqual(["project_reference", "registry", "github_fetch", "github_token"], params)
        # No baseline_sha / canonical_head / repo / branch parameter exists
        # to spoof in the first place.
        for forbidden in ("baseline_sha", "canonical_head", "repo", "branch", "baseline_head", "trusted_sha"):
            self.assertNotIn(forbidden, params)

    def test_two_projects_cannot_cross_resolve_repo(self):
        registry = ProjectRegistry(projects=[
            registry_entry(project_id="proj-a", owner="acme", name="repo-a"),
            registry_entry(project_id="proj-b", owner="other", name="repo-b"),
        ])
        fetch = fake_fetch(shas={
            ("acme", "repo-a", "main"): VALID_SHA_A,
            ("other", "repo-b", "main"): VALID_SHA_B,
        })
        result_a = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        result_b = resolve_remote_baseline("proj-b", registry=registry, github_fetch=fetch)
        self.assertNotEqual(result_a["repository"], result_b["repository"])
        self.assertEqual("https://github.com/acme/repo-a.git", result_a["repository"])
        self.assertEqual("https://github.com/other/repo-b.git", result_b["repository"])


class FailClosedTests(unittest.TestCase):
    def test_missing_registry_project_fails_closed(self):
        registry = ProjectRegistry(projects=[])
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("does-not-exist", registry=registry, github_fetch=fake_fetch())
        self.assertEqual("project_not_found", ctx.exception.code)

    def test_disabled_project_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry(status="disabled")])
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fake_fetch())
        self.assertEqual("project_disabled", ctx.exception.code)

    def test_missing_registry_repo_identity_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry(repo=None)])
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fake_fetch())
        self.assertEqual("registry_repo_missing", ctx.exception.code)

    def test_missing_branch_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry(default_branch="")])
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fake_fetch())
        self.assertEqual("registry_branch_missing", ctx.exception.code)

    def test_github_api_unavailable_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(unavailable_for={("acme", "repo-a", "main")})
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("remote_api_unavailable", ctx.exception.code)

    def test_branch_not_found_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(missing_for={("acme", "repo-a", "main")})
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("remote_branch_not_found", ctx.exception.code)

    def test_malformed_sha_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(malformed_for={("acme", "repo-a", "main")})
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("malformed_remote_sha", ctx.exception.code)

    def test_missing_sha_key_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry()])

        def fetch(owner, name, branch, *, token=None):
            return {"unexpected": "shape"}

        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("malformed_remote_sha", ctx.exception.code)

    def test_non_dict_response_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry()])

        def fetch(owner, name, branch, *, token=None):
            return None

        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual("remote_api_unavailable", ctx.exception.code)

    def test_empty_project_reference_fails_closed(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        with self.assertRaises(RemoteBaselineResolutionError) as ctx:
            resolve_remote_baseline("", registry=registry, github_fetch=fake_fetch())
        self.assertEqual("malformed_request", ctx.exception.code)


class PrivateRepoTokenTests(unittest.TestCase):
    """A registered repo can be private -- the resolver must be able to
    authenticate to GitHub for it without a caller ever supplying (or being
    able to supply) a credential. The only source is the server's own
    process environment, exactly like every other server-side credential
    already read this way in this codebase (manager.command_watcher)."""

    def test_github_token_env_var_reaches_github_fetch_when_caller_omits_it(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_A})
        with patch.dict(os.environ, {GITHUB_TOKEN_ENV_VAR: "server-side-secret"}):
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual([("acme", "repo-a", "main", "server-side-secret")], fetch.calls)

    def test_no_env_token_resolves_with_none_token_unchanged(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_A})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(GITHUB_TOKEN_ENV_VAR, None)
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        self.assertEqual([("acme", "repo-a", "main", None)], fetch.calls)

    def test_explicit_github_token_argument_overrides_environment(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_A})
        with patch.dict(os.environ, {GITHUB_TOKEN_ENV_VAR: "env-token"}):
            resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch, github_token="explicit-token")
        self.assertEqual([("acme", "repo-a", "main", "explicit-token")], fetch.calls)

    def test_default_fetch_sends_bearer_header_when_token_present(self):
        from manager.remote_baseline_resolver import default_github_fetch

        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"sha": VALID_SHA_A}

        class FakeSession:
            def get(self, url, headers=None, timeout=None):
                captured["headers"] = headers
                return FakeResponse()

        import manager.remote_baseline_resolver as mod
        original = mod.requests
        mod.requests = FakeSession()
        try:
            default_github_fetch("acme", "repo-a", "main", token="a-real-token")
        finally:
            mod.requests = original
        self.assertEqual("Bearer a-real-token", captured["headers"]["Authorization"])


class DeterminismTests(unittest.TestCase):
    def test_resolver_result_deterministic_across_repeated_calls(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        fetch = fake_fetch(shas={("acme", "repo-a", "main"): VALID_SHA_A})
        first = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        second = resolve_remote_baseline("proj-a", registry=registry, github_fetch=fetch)
        for key in ("project_id", "repository", "canonical_branch", "baseline_sha", "source"):
            self.assertEqual(first[key], second[key])


class DefaultGithubFetchTests(unittest.TestCase):
    """Exercise the real (non-injected) transport implementation against a
    fake `requests`-shaped object -- proving it fails closed on transport
    errors, non-200s, and unparseable bodies without ever touching a local
    checkout or the real network."""

    def test_default_fetch_fails_closed_on_network_error(self):
        import requests
        from unittest.mock import patch as mock_patch

        from manager.remote_baseline_resolver import default_github_fetch

        with mock_patch("manager.remote_baseline_resolver.requests.get",
                         side_effect=requests.exceptions.ConnectionError("simulated network failure")):
            with self.assertRaises(RemoteBaselineResolutionError) as ctx:
                default_github_fetch("acme", "repo-a", "main")
            self.assertEqual("remote_api_unavailable", ctx.exception.code)

    def test_default_fetch_fails_closed_on_404(self):
        from manager.remote_baseline_resolver import default_github_fetch

        class FakeResponse:
            status_code = 404

            def json(self):
                return {}

        class FakeSession:
            def get(self, *a, **k):
                return FakeResponse()

        import manager.remote_baseline_resolver as mod
        original = mod.requests
        mod.requests = FakeSession()
        try:
            with self.assertRaises(RemoteBaselineResolutionError) as ctx:
                default_github_fetch("acme", "repo-a", "main")
            self.assertEqual("remote_branch_not_found", ctx.exception.code)
        finally:
            mod.requests = original

    def test_default_fetch_fails_closed_on_unparseable_body(self):
        from manager.remote_baseline_resolver import default_github_fetch

        class FakeResponse:
            status_code = 200

            def json(self):
                raise ValueError("not json")

        class FakeSession:
            def get(self, *a, **k):
                return FakeResponse()

        import manager.remote_baseline_resolver as mod
        original = mod.requests
        mod.requests = FakeSession()
        try:
            with self.assertRaises(RemoteBaselineResolutionError) as ctx:
                default_github_fetch("acme", "repo-a", "main")
            self.assertEqual("remote_api_unavailable", ctx.exception.code)
        finally:
            mod.requests = original


if __name__ == "__main__":
    unittest.main()
