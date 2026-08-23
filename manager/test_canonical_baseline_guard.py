#!/usr/bin/env python3
"""Tests for the Canonical Baseline Promotion Guard.

Every test uses an in-memory ProjectRegistry plus fake `ref_sha_reader` /
`compare_reader` callables -- nothing here ever performs a live GitHub API
call, a local git command, or any ref mutation (no push, no force-push, no
branch move). That is the whole point of the gate: it must be verifiable
entirely offline.
"""

import inspect
import unittest

from manager.canonical_baseline_guard import (
    CanonicalBaselineGuardError,
    DEFAULT_FORMAL_BRANCH,
    STATUS_CONVERGENCE_REQUIRED,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    default_compare_reader,
    default_ref_sha_reader,
    evaluate_promotion_gate,
)
from manager.project_registry import ProjectRegistry


SHA_MAIN = "a" * 40
SHA_TARGET = "b" * 40
SHA_FORMAL = "a" * 40  # identical to main by default
SHA_OTHER = "c" * 40
SHA_TARGET_64 = "d" * 64


def registry_entry(project_id="proj-a", owner="acme", name="repo-a", default_branch="main",
                    strategy="origin_default", pinned_ref=None, status="enabled", **overrides):
    entry = {
        "project_id": project_id,
        "display_name": project_id,
        "aliases": [f"{project_id}-alias"],
        "repo": {"canonical_url": f"https://github.com/{owner}/{name}.git", "owner": owner, "name": name},
        "default_branch": default_branch,
        "baseline_resolution_policy": {"strategy": strategy, "pinned_ref": pinned_ref},
        "common_governance": {"reference": "governance-rules.json", "version": "1.0.0"},
        "project_rules": {"reference": "PROJECT-RULES.md"},
        "status": status,
        "resolution_status": "verified",
    }
    entry.update(overrides)
    return entry


class FakeRemote:
    """Records every call and never touches the network or a checkout.
    `shas` maps (owner, name, ref) -> sha for ref_sha_reader.
    `relations` maps (owner, name, base, head) -> status string for
    compare_reader ("identical"/"ahead"/"behind"/"diverged")."""

    def __init__(self, shas=None, relations=None, unavailable_refs=(), unavailable_compares=()):
        self.shas = shas or {}
        self.relations = relations or {}
        self.unavailable_refs = set(unavailable_refs)
        self.unavailable_compares = set(unavailable_compares)
        self.ref_calls = []
        self.compare_calls = []
        self.mutating_calls = []  # must always stay empty

    def ref_sha_reader(self, owner, name, ref, *, token=None):
        self.ref_calls.append((owner, name, ref, token))
        key = (owner, name, ref)
        if key in self.unavailable_refs:
            raise CanonicalBaselineGuardError("simulated remote read failure")
        if key not in self.shas:
            raise CanonicalBaselineGuardError(f"no fake sha configured for {key}")
        return self.shas[key]

    def compare_reader(self, owner, name, base, head, *, token=None):
        self.compare_calls.append((owner, name, base, head, token))
        key = (owner, name, base, head)
        if key in self.unavailable_compares:
            raise CanonicalBaselineGuardError("simulated compare failure")
        if key not in self.relations:
            raise CanonicalBaselineGuardError(f"no fake relation configured for {key}")
        return self.relations[key]

    def push(self, *a, **k):  # pragma: no cover - must never be called
        self.mutating_calls.append(("push", a, k))
        raise AssertionError("push must never be called by the promotion gate")


def build(registry_projects=None, remote=None, **kwargs):
    registry = ProjectRegistry(projects=registry_projects or [registry_entry()])
    remote = remote or FakeRemote()
    kwargs.setdefault("target_sha", SHA_TARGET)
    kwargs.setdefault("tested_sha", SHA_TARGET)
    result = evaluate_promotion_gate(
        "proj-a",
        registry=registry,
        ref_sha_reader=remote.ref_sha_reader,
        compare_reader=remote.compare_reader,
        **kwargs,
    )
    return result, remote


class Case1AllAligned(unittest.TestCase):
    def test_main_formal_tested_target_all_equal_activation_allowed(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        result, remote = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(STATUS_PASS, result.status)
        self.assertTrue(result.activation_allowed)
        self.assertTrue(result.fast_forward_possible)
        self.assertFalse(result.canonical_convergence_required)
        self.assertFalse(result.formal_convergence_required)
        self.assertEqual(SHA_TARGET, result.canonical_main_sha)
        self.assertEqual(SHA_TARGET, result.formal_sha)
        self.assertEqual(SHA_TARGET, result.tested_sha)
        self.assertEqual(0, len(remote.mutating_calls))


class Case2MainBehindDescendant(unittest.TestCase):
    def test_main_behind_target_strict_descendant_convergence_required_not_yet_allowed(self):
        remote = FakeRemote(
            shas={
                ("acme", "repo-a", "main"): SHA_MAIN,
                ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
            },
            relations={("acme", "repo-a", SHA_MAIN, SHA_TARGET): "ahead"},
        )
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(STATUS_CONVERGENCE_REQUIRED, result.status)
        self.assertFalse(result.activation_allowed)
        self.assertTrue(result.canonical_convergence_required)
        self.assertTrue(result.fast_forward_possible)


class Case3MainDiverged(unittest.TestCase):
    def test_main_diverged_from_target_fails(self):
        remote = FakeRemote(
            shas={
                ("acme", "repo-a", "main"): SHA_MAIN,
                ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
            },
            relations={("acme", "repo-a", SHA_MAIN, SHA_TARGET): "diverged"},
        )
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(STATUS_FAIL, result.status)
        self.assertFalse(result.activation_allowed)
        self.assertFalse(result.fast_forward_possible)


class Case4MainAhead(unittest.TestCase):
    def test_main_ahead_of_target_fails(self):
        remote = FakeRemote(
            shas={
                ("acme", "repo-a", "main"): SHA_MAIN,
                ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
            },
            relations={("acme", "repo-a", SHA_MAIN, SHA_TARGET): "behind"},
        )
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(STATUS_FAIL, result.status)
        self.assertFalse(result.activation_allowed)
        self.assertIn("ahead of TARGET", result.reason)


class Case5FormalBehindFastForwardable(unittest.TestCase):
    def test_formal_behind_target_but_fast_forward_possible_convergence_required(self):
        remote = FakeRemote(
            shas={
                ("acme", "repo-a", "main"): SHA_TARGET,
                ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_FORMAL,
            },
            relations={("acme", "repo-a", SHA_FORMAL, SHA_TARGET): "ahead"},
        )
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(STATUS_CONVERGENCE_REQUIRED, result.status)
        self.assertFalse(result.activation_allowed)
        self.assertTrue(result.formal_convergence_required)
        self.assertFalse(result.canonical_convergence_required)
        self.assertTrue(result.fast_forward_possible)


class Case6FormalDiverged(unittest.TestCase):
    def test_formal_diverged_fails(self):
        remote = FakeRemote(
            shas={
                ("acme", "repo-a", "main"): SHA_TARGET,
                ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_FORMAL,
            },
            relations={("acme", "repo-a", SHA_FORMAL, SHA_TARGET): "diverged"},
        )
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(STATUS_FAIL, result.status)
        self.assertFalse(result.activation_allowed)


class Case7TestedMismatch(unittest.TestCase):
    def test_tested_not_equal_target_fails(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_OTHER)
        self.assertEqual(STATUS_FAIL, result.status)
        self.assertFalse(result.activation_allowed)
        self.assertIn("TESTED evidence", result.reason)

    def test_tested_mismatch_short_circuits_before_any_compare_call(self):
        """TESTED must be checked before spending a remote comparison call
        -- there is no reason to even ask "is this a fast-forward" for a
        SHA that was never actually tested."""
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_OTHER)
        self.assertEqual([], remote.compare_calls)


class Case8RemoteUnavailable(unittest.TestCase):
    def test_canonical_branch_read_failure_is_unknown_not_fail(self):
        remote = FakeRemote(unavailable_refs={("acme", "repo-a", "main")})
        result, _ = build(remote=remote)
        self.assertEqual(STATUS_UNKNOWN, result.status)
        self.assertFalse(result.activation_allowed)

    def test_formal_branch_read_failure_is_unknown(self):
        remote = FakeRemote(
            shas={("acme", "repo-a", "main"): SHA_TARGET},
            unavailable_refs={("acme", "repo-a", DEFAULT_FORMAL_BRANCH)},
        )
        result, _ = build(remote=remote)
        self.assertEqual(STATUS_UNKNOWN, result.status)
        self.assertFalse(result.activation_allowed)

    def test_no_tested_evidence_is_unknown_not_fail(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        result, _ = build(remote=remote, tested_sha=None)
        self.assertEqual(STATUS_UNKNOWN, result.status)
        self.assertFalse(result.activation_allowed)

    def test_compare_failure_is_unknown(self):
        remote = FakeRemote(
            shas={
                ("acme", "repo-a", "main"): SHA_MAIN,
                ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
            },
            unavailable_compares={("acme", "repo-a", SHA_MAIN, SHA_TARGET)},
        )
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(STATUS_UNKNOWN, result.status)
        self.assertFalse(result.activation_allowed)


class Case9UnsupportedStrategy(unittest.TestCase):
    def test_unsupported_strategy_fails_closed(self):
        projects = [registry_entry(strategy="wild_guess")]
        remote = FakeRemote()
        result, _ = build(registry_projects=projects, remote=remote)
        self.assertEqual(STATUS_FAIL, result.status)
        self.assertFalse(result.activation_allowed)
        self.assertIn("unsupported", result.strategy)
        # Never even attempted a remote read for an unsupported strategy.
        self.assertEqual([], remote.ref_calls)


class Case10OriginDefaultContradictoryPin(unittest.TestCase):
    def test_origin_default_with_pinned_ref_fails_closed(self):
        projects = [registry_entry(strategy="origin_default", pinned_ref="release/2.0")]
        remote = FakeRemote()
        result, _ = build(registry_projects=projects, remote=remote)
        self.assertEqual(STATUS_FAIL, result.status)
        self.assertFalse(result.activation_allowed)
        self.assertIn("contradictory", result.reason)
        self.assertEqual([], remote.ref_calls)


class Case11PinnedCommitStrategyNotBroken(unittest.TestCase):
    def test_pinned_commit_strategy_uses_pinned_commit_as_canonical_authority(self):
        pinned = "e" * 40
        projects = [registry_entry(strategy="pinned_commit", pinned_ref=pinned, default_branch="main")]
        remote = FakeRemote(shas={
            ("acme", "repo-a", pinned): pinned,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): pinned,
        })
        result, remote = build(registry_projects=projects, remote=remote, target_sha=pinned, tested_sha=pinned)
        self.assertEqual(STATUS_PASS, result.status)
        self.assertTrue(result.activation_allowed)
        self.assertEqual(pinned, result.canonical_main_sha)
        # The registered default_branch ("main") must never be read for a
        # pinned_commit project -- only the pinned commit itself.
        self.assertNotIn(("acme", "repo-a", "main", None), remote.ref_calls)

    def test_pinned_commit_with_malformed_pinned_ref_fails_closed(self):
        projects = [registry_entry(strategy="pinned_commit", pinned_ref="not-a-sha")]
        remote = FakeRemote()
        result, _ = build(registry_projects=projects, remote=remote)
        self.assertEqual(STATUS_FAIL, result.status)
        self.assertFalse(result.activation_allowed)


class Case12NoForcePush(unittest.TestCase):
    def test_module_source_contains_no_force_push_or_push_invocation(self):
        import manager.canonical_baseline_guard as mod
        source = inspect.getsource(mod)
        self.assertNotIn("--force", source)
        self.assertNotIn("git push", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn(".push(", source)

    def test_gate_never_calls_the_fake_remotes_push_method(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual(0, len(remote.mutating_calls))


class Case13NoMutationInTestSuite(unittest.TestCase):
    def test_default_readers_only_ever_issue_http_get(self):
        """Prove the real (non-injected) transport implementations only
        ever call requests.get -- never post/put/patch/delete -- so even
        exercising the *real* code path (with a fake `requests` session)
        cannot mutate anything remote."""
        calls = []

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"sha": SHA_TARGET, "status": "identical"}

        class FakeSession:
            def get(self, url, headers=None, timeout=None):
                calls.append(url)
                return FakeResponse()

            def post(self, *a, **k):  # pragma: no cover
                raise AssertionError("must never POST")

        import manager.canonical_baseline_guard as mod
        original = mod.requests
        mod.requests = FakeSession()
        try:
            default_ref_sha_reader("acme", "repo-a", "main")
            default_compare_reader("acme", "repo-a", SHA_TARGET, SHA_TARGET)
        finally:
            mod.requests = original
        self.assertEqual(2, len(calls))


class SignatureAndResultShapeTests(unittest.TestCase):
    def test_result_to_dict_shape(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        result, _ = build(remote=remote, target_sha=SHA_TARGET, tested_sha=SHA_TARGET)
        self.assertEqual({
            "target_sha", "canonical_main_sha", "formal_sha", "tested_sha", "strategy",
            "fast_forward_possible", "canonical_convergence_required", "formal_convergence_required",
            "activation_allowed", "status", "reason",
        }, set(result.to_dict()))

    def test_malformed_target_sha_raises_input_error(self):
        registry = ProjectRegistry(projects=[registry_entry()])
        with self.assertRaises(CanonicalBaselineGuardError):
            evaluate_promotion_gate("proj-a", "not-a-sha", registry=registry,
                                     ref_sha_reader=FakeRemote().ref_sha_reader,
                                     compare_reader=FakeRemote().compare_reader)

    def test_accepts_sha256_target(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET_64,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET_64,
        })
        result, _ = build(remote=remote, target_sha=SHA_TARGET_64, tested_sha=SHA_TARGET_64)
        self.assertEqual(STATUS_PASS, result.status)

    def test_unknown_project_is_unknown_status(self):
        registry = ProjectRegistry(projects=[])
        remote = FakeRemote()
        result = evaluate_promotion_gate(
            "does-not-exist", SHA_TARGET, registry=registry,
            ref_sha_reader=remote.ref_sha_reader, compare_reader=remote.compare_reader,
        )
        self.assertEqual(STATUS_UNKNOWN, result.status)
        self.assertFalse(result.activation_allowed)


class TestedShaReaderTests(unittest.TestCase):
    def test_tested_sha_reader_used_when_tested_sha_not_supplied(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        registry = ProjectRegistry(projects=[registry_entry()])
        result = evaluate_promotion_gate(
            "proj-a", SHA_TARGET, registry=registry,
            ref_sha_reader=remote.ref_sha_reader, compare_reader=remote.compare_reader,
            tested_sha_reader=lambda: SHA_TARGET,
        )
        self.assertEqual(STATUS_PASS, result.status)

    def test_tested_sha_reader_returning_none_is_unknown(self):
        remote = FakeRemote(shas={
            ("acme", "repo-a", "main"): SHA_TARGET,
            ("acme", "repo-a", DEFAULT_FORMAL_BRANCH): SHA_TARGET,
        })
        registry = ProjectRegistry(projects=[registry_entry()])
        result = evaluate_promotion_gate(
            "proj-a", SHA_TARGET, registry=registry,
            ref_sha_reader=remote.ref_sha_reader, compare_reader=remote.compare_reader,
            tested_sha_reader=lambda: None,
        )
        self.assertEqual(STATUS_UNKNOWN, result.status)


if __name__ == "__main__":
    unittest.main()
