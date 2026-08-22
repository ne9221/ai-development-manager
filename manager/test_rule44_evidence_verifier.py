import unittest

from manager import rule44_evidence_verifier as v


PROJECT_ID = "demo-project"
REQUEST_ID = "req-abc123"
TASK_ID = "dispatch-req-abc123"
COMMAND_ID = "cmd-1"
EXECUTION_ID = "exec-1"
SESSION_ID = "sess-1"
REPO = "https://github.com/ne9221/demo-project.git"
BRANCH = "adm-worktree/demo-project--dispatch-req-abc123"
BASELINE_HEAD = "a" * 40
FINAL_SHA = "b" * 40


def _governance():
    return {
        "rules_version": "1.0.0",
        "rules_digest": "c" * 64,
        "mandatory_rule_ids": ["rule-1"],
        "mandatory_status_fields": ["current_progress"],
    }


def _lease_evidence():
    return {
        "authority": "acquired",
        "lock_id": "repo-" + "d" * 64,
        "generation": 1,
        "repository": "github:ne9221/demo-project",
        "branch": f"refs/heads/{BRANCH}",
        "scope": ["manager/"],
        "baseline_head": BASELINE_HEAD,
    }


def _d2_completion_evidence(commit_sha=FINAL_SHA, remote_sha=None, all_tests_pass=True):
    if remote_sha is None:
        remote_sha = commit_sha
    return {
        "changed_paths": ["manager/foo.py"],
        "tests": [{"id": "unit", "command": ["pytest", "manager", "-q"], "returncode": 0 if all_tests_pass else 1, "passed": all_tests_pass}],
        "commit_sha": commit_sha,
        "commit_created": True,
        "commit_identity": {"task_id": TASK_ID, "execution_id": EXECUTION_ID, "branch": f"refs/heads/{BRANCH}"},
        "remote_sha": remote_sha,
        "branch": f"refs/heads/{BRANCH}",
        "repository": "github:ne9221/demo-project",
        "baseline_head": BASELINE_HEAD,
    }


def repo_write_task(**overrides):
    task = {
        "task_id": TASK_ID, "project_id": PROJECT_ID, "status": "completed",
        # Deliberate ingress contract: working_directory is None for repo-write
        # tasks (Cloud Run has no HOME-local checkout).
        "working_directory": None,
        "branch": BRANCH, "baseline_head": BASELINE_HEAD, "worktree_id": "adm-worktree--demo-project--dispatch-req-abc123",
        "read_only": False, "needs_repo_edit": True,
        "allowed_paths": ["manager/"],
        "execution_policies": ["disposable", "bounded_repo_write", "no_external_writes"],
        "source_context": {"repo": REPO},
        "governance": _governance(),
    }
    task.update(overrides)
    return task


def complete_evidence():
    return {
        "dispatch_request": {
            "schema_version": "0.1.0", "project_id": PROJECT_ID, "request_id": REQUEST_ID,
            "task_id": TASK_ID, "command_id": COMMAND_ID, "created_at": "2026-08-23T00:00:00Z",
        },
        "project": {
            "project_id": PROJECT_ID, "name": "Demo", "repo": REPO, "default_branch": "main",
            "working_directory": "C:/canonical/demo-project", "runtime_ssot": "x",
            "project_rules": [], "active_tasks": [], "current_phase": "x", "important_constraints": [],
        },
        "task": repo_write_task(),
        "command": {
            "command_id": COMMAND_ID, "project_id": PROJECT_ID, "task_id": TASK_ID,
            "provider": "claude", "account_id": "acct-1",
            "requested_provider": None, "requested_account_id": None,
            "request_id": REQUEST_ID, "execution_id": EXECUTION_ID,
        },
        "execution": {
            "execution_id": EXECUTION_ID, "task_id": TASK_ID, "project_id": PROJECT_ID,
            "provider": "claude", "account_id": "acct-1", "status": "completed",
            "completed_at": "2026-08-23T01:00:00Z", "finished_at": "2026-08-23T01:00:00Z",
            "session_id": SESSION_ID, "access": "production_write",
            "lease_evidence": _lease_evidence(), "retry_of_execution_id": None,
            "repo_write_completion_evidence": _d2_completion_evidence(),
            "task_snapshot": {
                "working_directory": "C:/adm-worktrees/demo-project/dispatch-req-abc123",
                "branch": BRANCH, "baseline_head": BASELINE_HEAD,
            },
        },
        "session": {
            "session_id": SESSION_ID, "task_id": TASK_ID, "project_id": PROJECT_ID,
            "provider": "claude", "account_id": "acct-1",
        },
        "handoff": {
            "handoff_id": "ho-1", "task_id": TASK_ID, "project_id": PROJECT_ID,
            "files_changed": ["manager/foo.py"], "commits": [FINAL_SHA],
            "tests": ["python -m pytest manager -q"],
        },
        "task_claim": None,
        "sibling_executions": [
            {"execution_id": EXECUTION_ID, "task_id": TASK_ID, "status": "completed", "retry_of_execution_id": None},
        ],
        "sibling_commands": [
            {"command_id": COMMAND_ID, "task_id": TASK_ID, "request_id": REQUEST_ID},
        ],
        "remote_ref_check": {
            "performed": True, "ref": f"refs/heads/{BRANCH}", "remote_sha": FINAL_SHA,
            "matches": True, "error": None,
        },
        "registry_project": {
            "resolution_status": "verified", "status": "enabled",
            "common_governance": {"reference": "governance-rules.json", "version": "1.0.0"},
            "project_rules": {"reference": "AI-DEVELOPMENT-RULES.md", "mandatory_rule_ids": ["rule-1"]},
            "baseline_resolution_policy": {"strategy": "origin_default", "pinned_ref": None},
            "repo": {"canonical_url": REPO, "owner": "ne9221", "name": "demo-project"},
        },
        "registry_reference_file_check": {"common_governance_exists": True, "project_rules_exists": True},
        "canonical_checkout": {
            "available": True, "path": "C:/canonical/demo-project",
            "repo_identity_ok": True, "head_sha": BASELINE_HEAD, "clean": True,
        },
        "remote_baseline_resolution": {"performed": True, "baseline_sha": BASELINE_HEAD, "error": None},
    }


def evaluate(evidence):
    return v.evaluate(evidence, expected_project_id=PROJECT_ID, expected_request_id=REQUEST_ID, expected_repo=REPO)


class CompleteGraphTest(unittest.TestCase):
    def test_complete_synthetic_graph_passes(self):
        report = evaluate(complete_evidence())
        self.assertEqual(report.overall, v.PASS, report.as_dict())
        for code in v.INVARIANT_ORDER:
            self.assertEqual(report.as_dict()["invariants"][code]["verdict"], v.PASS, code)


class MissingSessionTest(unittest.TestCase):
    def test_missing_session_is_fail_or_unknown(self):
        evidence = complete_evidence()
        evidence["session"] = None
        report = evaluate(evidence)
        self.assertIn(report.overall, (v.FAIL, v.UNKNOWN))
        self.assertIn(report.as_dict()["invariants"]["K"]["verdict"], (v.FAIL, v.UNKNOWN))


# --- Required R2 regressions (A-I from the task spec) -----------------------

class RegressionA_WorkingDirectoryNoneTest(unittest.TestCase):
    """A: valid repo-write Task with working_directory=None + isolated
    runtime worktree + D2 completion evidence => PASS."""

    def test_working_directory_none_with_isolated_runtime_and_d2_passes(self):
        evidence = complete_evidence()
        self.assertIsNone(evidence["task"]["working_directory"])
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.PASS, report.as_dict())
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.PASS)
        self.assertEqual(report.as_dict()["invariants"]["F"]["verdict"], v.PASS)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.PASS)


class RegressionB_SharedCanonicalCheckoutTest(unittest.TestCase):
    """B: same Task using canonical/shared checkout => FAIL."""

    def test_runtime_working_directory_equals_canonical_checkout_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["task_snapshot"]["working_directory"] = evidence["project"]["working_directory"]
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.FAIL)


class RegressionC_StoredRemoteShaTrustedAloneTest(unittest.TestCase):
    """C: D2 commit_sha == stored remote_sha but a fresh ls-remote readback
    differs => FAIL. The stored remote_sha is never trusted by itself."""

    def test_fresh_readback_disagreeing_with_stored_remote_sha_fails(self):
        evidence = complete_evidence()
        # D2's own commit_sha/remote_sha still agree with each other...
        self.assertEqual(evidence["execution"]["repo_write_completion_evidence"]["commit_sha"],
                          evidence["execution"]["repo_write_completion_evidence"]["remote_sha"])
        # ...but the independent fresh readback says the remote has moved on.
        evidence["remote_ref_check"] = {
            "performed": True, "ref": f"refs/heads/{BRANCH}", "remote_sha": "f" * 40,
            "matches": False, "error": None,
        }
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["J"]["verdict"], v.FAIL)

    def test_no_fresh_readback_performed_is_unknown_never_pass(self):
        evidence = complete_evidence()
        evidence["remote_ref_check"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["J"]["verdict"], v.UNKNOWN)
        self.assertNotEqual(report.overall, v.PASS)

    def test_d2_internally_inconsistent_commit_vs_remote_sha_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["repo_write_completion_evidence"]["remote_sha"] = "e" * 40
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["J"]["verdict"], v.FAIL)


class RegressionD_TestsFailedTest(unittest.TestCase):
    """D: D2 tests failed => FAIL."""

    def test_d2_failing_test_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["repo_write_completion_evidence"] = _d2_completion_evidence(all_tests_pass=False)
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["H"]["verdict"], v.FAIL)


class RegressionE_AutomaticSelectionPassTest(unittest.TestCase):
    """E: provider/account omitted originally + real terminal attribution
    => PASS automatic selection."""

    def test_automatic_selection_passes(self):
        evidence = complete_evidence()
        self.assertIsNone(evidence["command"]["requested_provider"])
        self.assertIsNone(evidence["command"]["requested_account_id"])
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.PASS)
        self.assertEqual(report.overall, v.PASS)


class RegressionF_ExplicitRequestedProviderTest(unittest.TestCase):
    """F: explicit requested_provider => FAIL Rule44 automatic-selection
    invariant."""

    def test_explicit_requested_provider_fails(self):
        evidence = complete_evidence()
        evidence["command"]["requested_provider"] = "claude"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)


class RegressionG_ExplicitRequestedAccountIdTest(unittest.TestCase):
    """G: explicit requested_account_id => FAIL."""

    def test_explicit_requested_account_id_fails(self):
        evidence = complete_evidence()
        evidence["command"]["requested_account_id"] = "acct-1"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_provisional_account_id_alone_is_not_confused_with_requested(self):
        """command.account_id (the ADM-assigned account) must never be
        mistaken for command.requested_account_id (an explicit caller ask)."""
        evidence = complete_evidence()
        evidence["command"]["account_id"] = "acct-1"
        evidence["command"]["requested_account_id"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.PASS)


class RegressionH_D2DisagreesWithLeaseTest(unittest.TestCase):
    """H: D2 branch/repository/baseline disagrees with Task/lease => FAIL."""

    def test_d2_branch_disagrees_with_task_branch_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["repo_write_completion_evidence"]["branch"] = "refs/heads/some-other-branch"
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.FAIL)

    def test_d2_baseline_disagrees_with_task_baseline_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["repo_write_completion_evidence"]["baseline_head"] = "9" * 40
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.FAIL)

    def test_d2_repository_disagrees_with_lease_repository_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["repo_write_completion_evidence"]["repository"] = "github:someone-else/other-repo"
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.FAIL)

    def test_lease_baseline_disagrees_with_task_baseline_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["lease_evidence"]["baseline_head"] = "9" * 40
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.FAIL)


class RegressionI_MissingD2ForRepoWriteTest(unittest.TestCase):
    """I: missing D2 completion evidence for repo-write => UNKNOWN/FAIL
    closed, never PASS from Handoff text."""

    def test_missing_d2_for_repo_write_task_never_passes_from_handoff(self):
        evidence = complete_evidence()
        evidence["execution"]["repo_write_completion_evidence"] = None
        # Handoff still claims files/tests/commits were done -- must never
        # be accepted as substitute proof for a repo-write Task.
        report = evaluate(evidence)
        self.assertNotEqual(report.overall, v.PASS)
        for code in ("G", "H", "I", "J"):
            self.assertIn(report.as_dict()["invariants"][code]["verdict"], (v.FAIL, v.UNKNOWN), code)


# --- R3 regressions ----------------------------------------------------------

class TerminalIdentityTruthTest(unittest.TestCase):
    """R3 correction 1: automatic-routing PASS requires Execution.provider
    == Session.provider, and for Claude, Execution.account_id ==
    Session.account_id (both non-empty). Command.provider/account_id are
    provisional routing evidence only -- current runtime permits automatic
    sibling-account substitution."""

    def test_command_provisional_account_differs_from_terminal_but_execution_session_agree_passes(self):
        """Command Claude A / Execution Claude B / Session Claude B: allowed
        because Command's account is only provisional and
        requested_account_id is None."""
        evidence = complete_evidence()
        evidence["command"]["account_id"] = "acct-A"
        evidence["command"]["requested_account_id"] = None
        evidence["execution"]["account_id"] = "acct-B"
        evidence["session"]["account_id"] = "acct-B"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.PASS, report.as_dict()["invariants"]["E"])
        self.assertEqual(report.overall, v.PASS)

    def test_execution_and_session_account_disagree_fails(self):
        """Execution Claude A / Session Claude B => FAIL."""
        evidence = complete_evidence()
        evidence["execution"]["account_id"] = "acct-A"
        evidence["session"]["account_id"] = "acct-B"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_execution_and_session_provider_disagree_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["provider"] = "claude"
        evidence["session"]["provider"] = "codex"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_claude_terminal_account_missing_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["account_id"] = None
        evidence["session"]["account_id"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_missing_execution_or_session_is_unknown(self):
        evidence = complete_evidence()
        evidence["session"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.UNKNOWN)


class CanonicalCheckoutIntegrityTest(unittest.TestCase):
    """R3 correction 2: invariant O independently inspects the real
    canonical checkout (repo identity, clean status, HEAD == admitted
    baseline) rather than only comparing paths."""

    def test_canonical_checkout_dirty_fails(self):
        evidence = complete_evidence()
        evidence["canonical_checkout"]["clean"] = False
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_canonical_checkout_repo_identity_mismatch_fails(self):
        evidence = complete_evidence()
        evidence["canonical_checkout"]["repo_identity_ok"] = False
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_canonical_checkout_head_disagrees_with_baseline_fails(self):
        evidence = complete_evidence()
        evidence["canonical_checkout"]["head_sha"] = "9" * 40
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_canonical_checkout_unavailable_is_unknown_never_pass(self):
        evidence = complete_evidence()
        evidence["canonical_checkout"] = {"available": False, "reason": "ADM_WORKSPACE_ROOT is not set"}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.UNKNOWN)
        self.assertNotEqual(report.overall, v.PASS)

    def test_runtime_working_directory_equals_independently_inspected_canonical_path_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["task_snapshot"]["working_directory"] = evidence["canonical_checkout"]["path"]
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_fully_confirmed_canonical_checkout_passes(self):
        evidence = complete_evidence()
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.PASS)


class RegistryGovernanceAuthorityTest(unittest.TestCase):
    """R3 correction 3: invariant C now also requires the Global Project
    Registry entry itself to be verified+enabled with non-empty
    common_governance/project_rules references -- Task.governance alone
    never proves PROJECT-RULES authority."""

    def test_registry_unresolved_fails(self):
        evidence = complete_evidence()
        evidence["registry_project"]["resolution_status"] = "unresolved"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_registry_disabled_fails(self):
        evidence = complete_evidence()
        evidence["registry_project"]["status"] = "disabled"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_missing_common_governance_reference_fails(self):
        evidence = complete_evidence()
        evidence["registry_project"]["common_governance"] = {}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.FAIL)

    def test_missing_project_rules_reference_fails(self):
        evidence = complete_evidence()
        evidence["registry_project"]["project_rules"] = {}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.FAIL)

    def test_confirmed_missing_reference_file_fails(self):
        evidence = complete_evidence()
        evidence["registry_reference_file_check"]["project_rules_exists"] = False
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.FAIL)

    def test_reference_file_check_not_performed_still_passes_structurally(self):
        """'Where technically practical' -- an unperformed file-existence
        check is noted in the detail but never blocks PASS on its own."""
        evidence = complete_evidence()
        evidence["registry_reference_file_check"] = {"common_governance_exists": None, "project_rules_exists": None}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.PASS)

    def test_no_registry_entry_resolved_is_unknown(self):
        evidence = complete_evidence()
        evidence["registry_project"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.UNKNOWN)
        self.assertNotEqual(report.overall, v.PASS)

    def test_registry_repo_mismatch_fails(self):
        evidence = complete_evidence()
        evidence["registry_project"]["repo"]["canonical_url"] = "https://github.com/someone-else/other-repo.git"
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["C"]["verdict"], v.FAIL)

    def test_detail_distinguishes_common_governance_stamp_from_project_rules_authority(self):
        evidence = complete_evidence()
        report = evaluate(evidence)
        detail = report.as_dict()["invariants"]["C"]["detail"]
        self.assertIn("Common Governance", detail)
        self.assertIn("project_rules", detail)


class IndependentBaselineProofTest(unittest.TestCase):
    """R3 correction 4: invariant D requires Task.baseline_head to equal
    one fresh, independently-resolved canonical remote baseline -- Task/
    lease/D2 agreeing with each other is no longer sufficient by itself."""

    def test_baseline_resolution_not_performed_is_unknown(self):
        evidence = complete_evidence()
        evidence["remote_baseline_resolution"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.UNKNOWN)
        self.assertNotEqual(report.overall, v.PASS)

    def test_baseline_resolution_error_fails(self):
        evidence = complete_evidence()
        evidence["remote_baseline_resolution"] = {"performed": True, "baseline_sha": None, "error": "remote_api_unavailable"}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_baseline_resolution_mismatch_fails(self):
        evidence = complete_evidence()
        evidence["remote_baseline_resolution"] = {"performed": True, "baseline_sha": "9" * 40, "error": None}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_baseline_resolution_match_passes(self):
        evidence = complete_evidence()
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["D"]["verdict"], v.PASS)


class InspectCanonicalCheckoutHelperTest(unittest.TestCase):
    def _project_metadata(self):
        from manager.project_registry import ProjectMetadata
        return ProjectMetadata(
            project_id=PROJECT_ID, display_name="Demo", aliases=(),
            repo={"canonical_url": REPO, "owner": "ne9221", "name": "demo-project"},
            default_branch="main", baseline_resolution_policy={"strategy": "origin_default", "pinned_ref": None},
            common_governance={"reference": "governance-rules.json"}, project_rules={"reference": "AI-DEVELOPMENT-RULES.md"},
            validation_policy={}, working_directory_policy={"relative_path": "demo-project", "env_var": "ADM_WORKSPACE_ROOT"},
            isolation_policy={}, provider_restrictions={}, protected_paths=(), default_write_boundaries=(),
            pointer_rules={}, status="enabled", resolution_status="verified", unresolved_reason=None,
        )

    def test_workspace_root_not_set_is_unavailable(self):
        result = v.inspect_canonical_checkout(self._project_metadata(), workspace_root=None, exists_check=lambda p: True)
        # workspace_root=None with no env var set should be unavailable unless ADM_WORKSPACE_ROOT happens to be set
        import os
        if "ADM_WORKSPACE_ROOT" not in os.environ:
            self.assertFalse(result["available"])

    def test_path_does_not_exist_is_unavailable(self):
        result = v.inspect_canonical_checkout(self._project_metadata(), workspace_root="C:/workspace", exists_check=lambda p: False)
        self.assertFalse(result["available"])

    def test_available_and_clean(self):
        def fake_runner(args, **kwargs):
            class R:
                returncode = 0
                stderr = ""
                if "remote" in args:
                    stdout = REPO + "\n"
                elif "rev-parse" in args:
                    stdout = BASELINE_HEAD + "\n"
                else:
                    stdout = ""
            return R()

        result = v.inspect_canonical_checkout(self._project_metadata(), workspace_root="C:/workspace",
                                               exists_check=lambda p: True, git_runner=fake_runner)
        self.assertTrue(result["available"])
        self.assertTrue(result["repo_identity_ok"])
        self.assertEqual(result["head_sha"], BASELINE_HEAD)
        self.assertTrue(result["clean"])

    def test_dirty_status(self):
        def fake_runner(args, **kwargs):
            class R:
                returncode = 0
                stderr = ""
                if "remote" in args:
                    stdout = REPO + "\n"
                elif "rev-parse" in args:
                    stdout = BASELINE_HEAD + "\n"
                else:
                    stdout = " M manager/foo.py\n"
            return R()

        result = v.inspect_canonical_checkout(self._project_metadata(), workspace_root="C:/workspace",
                                               exists_check=lambda p: True, git_runner=fake_runner)
        self.assertTrue(result["available"])
        self.assertFalse(result["clean"])


class CheckRepoFileExistsHelperTest(unittest.TestCase):
    def test_returns_true_on_200(self):
        class R:
            status_code = 200
        result = v.check_repo_file_exists("ne9221", "demo-project", "governance-rules.json", BASELINE_HEAD,
                                           http_get=lambda url, **kw: R())
        self.assertTrue(result)

    def test_returns_false_on_404(self):
        class R:
            status_code = 404
        result = v.check_repo_file_exists("ne9221", "demo-project", "governance-rules.json", BASELINE_HEAD,
                                           http_get=lambda url, **kw: R())
        self.assertFalse(result)

    def test_returns_none_on_error(self):
        def raiser(*a, **k):
            raise RuntimeError("network down")
        result = v.check_repo_file_exists("ne9221", "demo-project", "governance-rules.json", BASELINE_HEAD,
                                           http_get=raiser)
        self.assertIsNone(result)


# --- Other invariants / prior-generation coverage kept for regression -------

class DuplicateCommandTest(unittest.TestCase):
    def test_duplicate_command_sharing_request_id_fails(self):
        evidence = complete_evidence()
        evidence["sibling_commands"] = [
            {"command_id": COMMAND_ID, "task_id": TASK_ID, "request_id": REQUEST_ID},
            {"command_id": "cmd-2", "task_id": TASK_ID, "request_id": REQUEST_ID},
        ]
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["N"]["verdict"], v.FAIL)


class WrongRepositoryTest(unittest.TestCase):
    def test_wrong_repo_project_mismatch_fails(self):
        evidence = complete_evidence()
        evidence["task"]["project_id"] = "other-project"
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["B"]["verdict"], v.FAIL)


class MismatchedIdentityTest(unittest.TestCase):
    def test_mismatched_task_command_execution_identity_fails(self):
        evidence = complete_evidence()
        evidence["execution"]["task_id"] = "some-other-task"
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["K"]["verdict"], v.FAIL)


class TerminalWithoutHandoffTest(unittest.TestCase):
    def test_terminal_execution_but_no_handoff_is_fail_or_unknown(self):
        evidence = complete_evidence()
        evidence["handoff"] = None
        report = evaluate(evidence)
        self.assertIn(report.as_dict()["invariants"]["K"]["verdict"], (v.FAIL, v.UNKNOWN))
        self.assertNotEqual(report.overall, v.PASS)


class ReleasedClaimSingleAuthorityTest(unittest.TestCase):
    def test_released_claim_and_single_authority_passes(self):
        evidence = complete_evidence()
        evidence["task_claim"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["M"]["verdict"], v.PASS)
        self.assertEqual(report.as_dict()["invariants"]["N"]["verdict"], v.PASS)
        self.assertEqual(report.overall, v.PASS)

    def test_claim_still_held_by_this_execution_fails(self):
        evidence = complete_evidence()
        evidence["task_claim"] = {"execution_id": EXECUTION_ID, "task_id": TASK_ID, "project_id": PROJECT_ID}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["M"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)


class DuplicateExecutionChainTest(unittest.TestCase):
    def test_two_independent_root_executions_fail(self):
        evidence = complete_evidence()
        evidence["sibling_executions"] = [
            {"execution_id": EXECUTION_ID, "task_id": TASK_ID, "status": "completed", "retry_of_execution_id": None},
            {"execution_id": "exec-rogue", "task_id": TASK_ID, "status": "completed", "retry_of_execution_id": None},
        ]
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["N"]["verdict"], v.FAIL)


class NonRepoWriteFallbackTest(unittest.TestCase):
    """A read-only (non-repo-write) task still uses the legacy Handoff/
    test_evidence/final_commit_sha fallback path -- this module remains
    generic across write and non-write dispatches."""

    def _read_only_evidence(self):
        evidence = complete_evidence()
        evidence["task"] = repo_write_task(read_only=True, needs_repo_edit=False, working_directory="C:/adm-worktrees/demo-project/dispatch-req-abc123")
        evidence["execution"]["access"] = "read_only"
        evidence["execution"]["lease_evidence"] = None
        evidence["execution"]["repo_write_completion_evidence"] = None
        evidence["final_commit_sha"] = FINAL_SHA
        return evidence

    def test_freetext_handoff_tests_alone_is_unknown_not_pass(self):
        evidence = self._read_only_evidence()
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["H"]["verdict"], v.UNKNOWN)
        self.assertNotEqual(report.overall, v.PASS)

    def test_structured_passing_test_evidence_passes_that_invariant(self):
        evidence = self._read_only_evidence()
        evidence["test_evidence"] = {"passed": True, "exit_code": 0, "command": "pytest manager -q"}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["H"]["verdict"], v.PASS)

    def test_structured_failing_test_evidence_fails(self):
        evidence = self._read_only_evidence()
        evidence["test_evidence"] = {"passed": False, "exit_code": 1, "command": "pytest manager -q"}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["H"]["verdict"], v.FAIL)


class RemoteCheckHelperTest(unittest.TestCase):
    def test_check_remote_ref_matches(self):
        def fake_runner(args, **kwargs):
            class R:
                returncode = 0
                stdout = f"{FINAL_SHA}\trefs/heads/{BRANCH}\n"
                stderr = ""
            return R()

        result = v.check_remote_ref(REPO, BRANCH, FINAL_SHA, runner=fake_runner)
        self.assertTrue(result["performed"])
        self.assertTrue(result["matches"])
        self.assertEqual(result["remote_sha"], FINAL_SHA)

    def test_check_remote_ref_missing_ref(self):
        def fake_runner(args, **kwargs):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        result = v.check_remote_ref(REPO, BRANCH, FINAL_SHA, runner=fake_runner)
        self.assertTrue(result["performed"])
        self.assertFalse(result["matches"])
        self.assertIsNotNone(result["error"])

    def test_check_remote_ref_no_expected_sha(self):
        result = v.check_remote_ref(REPO, BRANCH, None, runner=lambda *a, **k: None)
        self.assertFalse(result["performed"])


class RepoIdentityHelperTest(unittest.TestCase):
    def test_recognizes_github_colon_form_and_https_url_as_same_repo(self):
        self.assertEqual(v._repo_identity("github:ne9221/demo-project"), v._repo_identity(REPO))

    def test_different_repo_is_not_equal(self):
        self.assertNotEqual(v._repo_identity("github:someone-else/other-repo"), v._repo_identity(REPO))


class CollectEvidenceFakeStoreTest(unittest.TestCase):
    """Exercises collect_evidence()'s wiring against fake store/registries,
    never touching real Drive/GCS/git -- proves the collector reads the
    right record types by name, and drives the fresh remote readback from
    D2's own branch/repository/commit_sha, without needing live credentials."""

    def test_collect_evidence_assembles_expected_shape_and_uses_d2_for_remote_check(self):
        fixture = complete_evidence()

        class FakeStore:
            def __init__(self):
                self.data = {
                    ("tasks", PROJECT_ID, TASK_ID): dict(fixture["task"]),
                    ("commands", PROJECT_ID, COMMAND_ID): dict(fixture["command"]),
                    ("executions", PROJECT_ID, EXECUTION_ID): dict(fixture["execution"]),
                    ("sessions", PROJECT_ID, SESSION_ID): dict(fixture["session"]),
                    ("projects", PROJECT_ID, PROJECT_ID): dict(fixture["project"]),
                }

            def get(self, area, project_id, name):
                key = (area, project_id, name)
                if key not in self.data:
                    from manager.tasks import TaskError
                    raise TaskError("not found")
                return self.data[key]

            def latest(self, area, project_id, task_id):
                return dict(fixture["handoff"])

            def list_records(self, area, project_id):
                if area == "executions":
                    return [dict(fixture["sibling_executions"][0])]
                if area == "commands":
                    return [dict(fixture["sibling_commands"][0])]
                return []

        class FakeDispatchRegistry:
            def __init__(self, bucket, project_id, request_id):
                pass

            def read_if_exists(self):
                return (dict(fixture["dispatch_request"]), 1, None)

        class FakeClaimRegistry:
            def __init__(self, bucket, project_id, task_id):
                pass

            def read_if_exists(self):
                return None

        seen_calls = []

        def fake_git_runner(args, **kwargs):
            seen_calls.append(args)

            class R:
                returncode = 0
                stdout = f"{FINAL_SHA}\trefs/heads/{BRANCH}\n"
                stderr = ""
            return R()

        store = FakeStore()
        evidence = v.collect_evidence(
            store, PROJECT_ID, REQUEST_ID,
            task_claim_registry_factory=FakeClaimRegistry,
            dispatch_registry_factory=FakeDispatchRegistry,
            expected_repo=REPO,
            git_runner=fake_git_runner,
        )

        self.assertEqual(evidence["task"]["task_id"], TASK_ID)
        self.assertEqual(evidence["command"]["command_id"], COMMAND_ID)
        self.assertEqual(evidence["execution"]["execution_id"], EXECUTION_ID)
        self.assertEqual(evidence["session"]["session_id"], SESSION_ID)
        self.assertEqual(evidence["project"]["project_id"], PROJECT_ID)
        self.assertEqual(len(evidence["sibling_executions"]), 1)
        self.assertEqual(len(evidence["sibling_commands"]), 1)
        # No final_commit_sha was supplied -- the remote check must still
        # have been driven from D2's own commit_sha/branch/repository.
        self.assertTrue(evidence["remote_ref_check"]["performed"])
        self.assertTrue(evidence["remote_ref_check"]["matches"])
        self.assertEqual(len(seen_calls), 1)
        self.assertIn(f"refs/heads/{BRANCH}", seen_calls[0])

    def test_collect_evidence_wires_registry_canonical_checkout_and_baseline_resolution(self):
        """R3: collect_evidence() must resolve the Global Project Registry
        entry, independently inspect the canonical checkout, and perform a
        fresh canonical-baseline resolution -- all read-only, all via
        injected fakes here (never live Drive/GCS/git/GitHub)."""
        fixture = complete_evidence()

        class FakeStore:
            def __init__(self):
                self.data = {
                    ("tasks", PROJECT_ID, TASK_ID): dict(fixture["task"]),
                    ("commands", PROJECT_ID, COMMAND_ID): dict(fixture["command"]),
                    ("executions", PROJECT_ID, EXECUTION_ID): dict(fixture["execution"]),
                    ("sessions", PROJECT_ID, SESSION_ID): dict(fixture["session"]),
                    ("projects", PROJECT_ID, PROJECT_ID): dict(fixture["project"]),
                }

            def get(self, area, project_id, name):
                key = (area, project_id, name)
                if key not in self.data:
                    from manager.tasks import TaskError
                    raise TaskError("not found")
                return self.data[key]

            def latest(self, area, project_id, task_id):
                return dict(fixture["handoff"])

            def list_records(self, area, project_id):
                if area == "executions":
                    return [dict(fixture["sibling_executions"][0])]
                if area == "commands":
                    return [dict(fixture["sibling_commands"][0])]
                return []

        class FakeDispatchRegistry:
            def __init__(self, bucket, project_id, request_id):
                pass

            def read_if_exists(self):
                return (dict(fixture["dispatch_request"]), 1, None)

        class FakeClaimRegistry:
            def __init__(self, bucket, project_id, task_id):
                pass

            def read_if_exists(self):
                return None

        from manager.project_registry import ProjectMetadata, ProjectRegistry

        project_metadata = ProjectMetadata(
            project_id=PROJECT_ID, display_name="Demo", aliases=(),
            repo={"canonical_url": REPO, "owner": "ne9221", "name": "demo-project"},
            default_branch="main", baseline_resolution_policy={"strategy": "origin_default", "pinned_ref": None},
            common_governance={"reference": "governance-rules.json"}, project_rules={"reference": "AI-DEVELOPMENT-RULES.md"},
            validation_policy={}, working_directory_policy={"relative_path": "demo-project", "env_var": "ADM_WORKSPACE_ROOT"},
            isolation_policy={}, provider_restrictions={}, protected_paths=(), default_write_boundaries=(),
            pointer_rules={}, status="enabled", resolution_status="verified", unresolved_reason=None,
        )

        class FakeRegistry:
            def get_project(self, query, allow_disabled=False):
                return project_metadata

        def fake_git_runner(args, **kwargs):
            class R:
                returncode = 0
                stderr = ""
                if "ls-remote" in args:
                    stdout = f"{FINAL_SHA}\trefs/heads/{BRANCH}\n"
                elif "remote" in args:
                    stdout = REPO + "\n"
                elif "rev-parse" in args:
                    stdout = BASELINE_HEAD + "\n"
                else:
                    stdout = ""
            return R()

        def fake_github_fetch(owner, name, branch, token=None):
            return {"sha": BASELINE_HEAD}

        def fake_repo_file_exists(owner, name, path, ref, token=None):
            return True

        store = FakeStore()
        evidence = v.collect_evidence(
            store, PROJECT_ID, REQUEST_ID,
            task_claim_registry_factory=FakeClaimRegistry,
            dispatch_registry_factory=FakeDispatchRegistry,
            expected_repo=REPO,
            git_runner=fake_git_runner,
            project_registry=FakeRegistry(),
            workspace_root="C:/workspace",
            github_fetch=fake_github_fetch,
            repo_file_exists_check=fake_repo_file_exists,
            canonical_checkout_exists_check=lambda p: True,
        )

        self.assertIsNotNone(evidence["registry_project"])
        self.assertEqual(evidence["registry_project"]["resolution_status"], "verified")
        self.assertEqual(evidence["registry_project"]["status"], "enabled")
        self.assertTrue(evidence["registry_reference_file_check"]["common_governance_exists"])
        self.assertTrue(evidence["registry_reference_file_check"]["project_rules_exists"])
        self.assertTrue(evidence["canonical_checkout"]["available"])
        self.assertTrue(evidence["canonical_checkout"]["repo_identity_ok"])
        self.assertEqual(evidence["canonical_checkout"]["head_sha"], BASELINE_HEAD)
        self.assertTrue(evidence["canonical_checkout"]["clean"])
        self.assertTrue(evidence["remote_baseline_resolution"]["performed"])
        self.assertEqual(evidence["remote_baseline_resolution"]["baseline_sha"], BASELINE_HEAD)
        self.assertIsNone(evidence["remote_baseline_resolution"]["error"])

        report = v.evaluate(evidence, expected_project_id=PROJECT_ID, expected_request_id=REQUEST_ID, expected_repo=REPO)
        self.assertEqual(report.overall, v.PASS, report.as_dict())


if __name__ == "__main__":
    unittest.main()
