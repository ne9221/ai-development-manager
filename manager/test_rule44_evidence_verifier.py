import unittest

from manager import rule44_evidence_verifier as v


PROJECT_ID = "demo-project"
REQUEST_ID = "req-abc123"
TASK_ID = "dispatch-req-abc123"
COMMAND_ID = "cmd-1"
EXECUTION_ID = "exec-1"
SESSION_ID = "sess-1"
REPO = "https://github.com/ne9221/demo-project.git"
BRANCH = "feat/rule44-demo"
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
        "task": {
            "task_id": TASK_ID, "project_id": PROJECT_ID, "status": "completed",
            "working_directory": "C:/worktrees/demo-project/feat-rule44-demo",
            "branch": BRANCH, "baseline_head": BASELINE_HEAD, "worktree_id": "wt-1",
            "read_only": False, "worktree_id_": None,
            "governance": _governance(),
        },
        "command": {
            "command_id": COMMAND_ID, "project_id": PROJECT_ID, "task_id": TASK_ID,
            "provider": "claude", "account_id": "acct-1", "request_id": REQUEST_ID,
            "execution_id": EXECUTION_ID,
        },
        "execution": {
            "execution_id": EXECUTION_ID, "task_id": TASK_ID, "project_id": PROJECT_ID,
            "provider": "claude", "account_id": "acct-1", "status": "completed",
            "completed_at": "2026-08-23T01:00:00Z", "finished_at": "2026-08-23T01:00:00Z",
            "session_id": SESSION_ID, "access": "production_write",
            "lease_evidence": _lease_evidence(), "retry_of_execution_id": None,
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
        "final_commit_sha": FINAL_SHA,
        "test_evidence": {"passed": True, "exit_code": 0, "command": "python -m pytest manager -q"},
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


class LocalCommitNoPushTest(unittest.TestCase):
    def test_local_commit_without_remote_readback_fails(self):
        evidence = complete_evidence()
        evidence["remote_ref_check"] = {
            "performed": True, "ref": f"refs/heads/{BRANCH}", "remote_sha": "f" * 40,
            "matches": False, "error": None,
        }
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["J"]["verdict"], v.FAIL)

    def test_no_remote_check_performed_is_unknown_never_pass(self):
        evidence = complete_evidence()
        evidence["remote_ref_check"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["J"]["verdict"], v.UNKNOWN)
        self.assertNotEqual(report.overall, v.PASS)


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


class SharedCanonicalCheckoutTest(unittest.TestCase):
    def test_repo_write_in_shared_canonical_checkout_fails(self):
        evidence = complete_evidence()
        evidence["task"]["working_directory"] = evidence["project"]["working_directory"]
        report = evaluate(evidence)
        self.assertEqual(report.overall, v.FAIL)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.FAIL)

    def test_repo_write_without_worktree_id_fails(self):
        evidence = complete_evidence()
        evidence["task"]["worktree_id"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["O"]["verdict"], v.FAIL)


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
        self.assertIn(report.as_dict()["invariants"]["G"]["verdict"], (v.FAIL, v.UNKNOWN))
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


class ProviderAccountAttributionTest(unittest.TestCase):
    def test_provider_account_attribution_preserved(self):
        evidence = complete_evidence()
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.PASS)

    def test_claude_provider_without_account_id_fails(self):
        evidence = complete_evidence()
        evidence["command"]["account_id"] = None
        evidence["execution"]["account_id"] = None
        evidence["session"]["account_id"] = None
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["E"]["verdict"], v.FAIL)
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


class TestsNotIndependentlyVerifiableTest(unittest.TestCase):
    def test_freetext_handoff_tests_alone_is_unknown_not_pass(self):
        evidence = complete_evidence()
        evidence.pop("test_evidence", None)
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["H"]["verdict"], v.UNKNOWN)
        self.assertNotEqual(report.overall, v.PASS)

    def test_structured_passing_test_evidence_passes(self):
        evidence = complete_evidence()
        evidence["test_evidence"] = {"passed": True, "exit_code": 0, "command": "pytest manager -q"}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["H"]["verdict"], v.PASS)
        self.assertEqual(report.overall, v.PASS)

    def test_structured_failing_test_evidence_fails(self):
        evidence = complete_evidence()
        evidence["test_evidence"] = {"passed": False, "exit_code": 1, "command": "pytest manager -q"}
        report = evaluate(evidence)
        self.assertEqual(report.as_dict()["invariants"]["H"]["verdict"], v.FAIL)
        self.assertEqual(report.overall, v.FAIL)

    def test_no_tests_at_all_fails(self):
        evidence = complete_evidence()
        evidence.pop("test_evidence", None)
        evidence["handoff"]["tests"] = []
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


class CollectEvidenceFakeStoreTest(unittest.TestCase):
    """Exercises collect_evidence()'s wiring against fake store/registries,
    never touching real Drive/GCS/git -- proves the collector reads the
    right record types by name without needing live credentials."""

    def test_collect_evidence_assembles_expected_shape(self):
        class FakeStore:
            def __init__(self):
                self.data = {
                    ("tasks", PROJECT_ID, TASK_ID): dict(complete_evidence()["task"]),
                    ("commands", PROJECT_ID, COMMAND_ID): dict(complete_evidence()["command"]),
                    ("executions", PROJECT_ID, EXECUTION_ID): dict(complete_evidence()["execution"]),
                    ("sessions", PROJECT_ID, SESSION_ID): dict(complete_evidence()["session"]),
                    ("projects", PROJECT_ID, PROJECT_ID): dict(complete_evidence()["project"]),
                }

            def get(self, area, project_id, name):
                key = (area, project_id, name)
                if key not in self.data:
                    from manager.tasks import TaskError
                    raise TaskError("not found")
                return self.data[key]

            def latest(self, area, project_id, task_id):
                return dict(complete_evidence()["handoff"])

            def list_records(self, area, project_id):
                if area == "executions":
                    return [dict(complete_evidence()["sibling_executions"][0])]
                if area == "commands":
                    return [dict(complete_evidence()["sibling_commands"][0])]
                return []

        class FakeDispatchRegistry:
            def __init__(self, bucket, project_id, request_id):
                pass

            def read_if_exists(self):
                return (dict(complete_evidence()["dispatch_request"]), 1, None)

        class FakeClaimRegistry:
            def __init__(self, bucket, project_id, task_id):
                pass

            def read_if_exists(self):
                return None

        def fake_git_runner(args, **kwargs):
            class R:
                returncode = 0
                stdout = f"{FINAL_SHA}\trefs/heads/{BRANCH}\n"
                stderr = ""
            return R()

        import manager.rule44_evidence_verifier as mod
        store = FakeStore()
        evidence = mod.collect_evidence(
            store, PROJECT_ID, REQUEST_ID,
            task_claim_registry_factory=FakeClaimRegistry,
            dispatch_registry_factory=FakeDispatchRegistry,
            final_commit_sha=FINAL_SHA, expected_repo=REPO,
            git_runner=fake_git_runner,
        )

        self.assertEqual(evidence["task"]["task_id"], TASK_ID)
        self.assertEqual(evidence["command"]["command_id"], COMMAND_ID)
        self.assertEqual(evidence["execution"]["execution_id"], EXECUTION_ID)
        self.assertEqual(evidence["session"]["session_id"], SESSION_ID)
        self.assertEqual(evidence["project"]["project_id"], PROJECT_ID)
        self.assertEqual(len(evidence["sibling_executions"]), 1)
        self.assertEqual(len(evidence["sibling_commands"]), 1)


if __name__ == "__main__":
    unittest.main()
