import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.verify import (
    DispatchRecordProbe, DriveReadbackProbe, ExecutionRecordProbe, GitProbe, ProbeUnavailable, TestEvidenceProbe,
    normalize_repo, observation, adm_test_evidence, verify,
)

H = "3f2a9c1d0b8e7f6a5c4d3e2f1a0b9c8d7e6f5a4b"
OTHER = "9e8d7c6b5a4938271605f4e3d2c1b0a998877665"


def claims(role=v.WORKER, **facts):
    result = r.blank_result("e-1", "t-1", role)
    for field, value in facts.items():
        if value is not None:
            result = r.with_fact(result, field, r.fact(value, v.REPORTED, "agent:fenced"))
    return result


class FakeProbe:
    def __init__(self, name, observations=(), signals=(), unavailable=None):
        self.name, self._obs, self._signals, self._unavailable = name, list(observations), set(signals), unavailable

    def run(self, result, context):
        if self._unavailable:
            raise ProbeUnavailable(self._unavailable)
        return [dict(o, probe=self.name) for o in self._obs], self._signals


class FoldRuleTests(unittest.TestCase):
    def test_equivalent_claim_becomes_verified_with_the_observed_value(self):
        verified, report = verify(claims(head_sha=H[:7]), {}, [FakeProbe("git", [observation("head_sha", v.VERIFIED, H, "git")])])
        self.assertEqual((H, v.VERIFIED, "probe:git"), (r.value(verified, "head_sha"), r.level(verified, "head_sha"),
                                                          r.get(verified, "head_sha")["source"]))
        self.assertEqual([], report["signals"])

    def test_different_observation_contradicts_and_keeps_the_claim(self):
        verified, report = verify(claims(remote_sha=H), {}, [FakeProbe("git", [observation("remote_sha", v.VERIFIED, OTHER, "git")])])
        self.assertEqual((H, v.CONTRADICTED), (r.value(verified, "remote_sha"), r.level(verified, "remote_sha")))
        self.assertIn(OTHER, r.get(verified, "remote_sha")["detail"])
        self.assertEqual(["verify.claim_contradicted"], report["signals"])

    def test_observation_fills_an_unclaimed_fact(self):
        verified, _ = verify(claims(), {}, [FakeProbe("git", [observation("git_status", v.VERIFIED, "dirty", "git")])])
        self.assertEqual(("dirty", v.VERIFIED), (r.value(verified, "git_status"), r.level(verified, "git_status")))

    def test_undecided_or_unavailable_probe_changes_nothing(self):
        start = claims(status="PASS", tests_failed=0)
        verified, report = verify(start, {}, [
            FakeProbe("a", [observation("tests_failed", v.UNKNOWN, None, "a")]),
            FakeProbe("b", unavailable="no evidence"),
        ])
        self.assertEqual(start["facts"], verified["facts"])
        self.assertEqual([{"probe": "b", "reason": "no evidence"}], report["probes_unavailable"])

    def test_disagreeing_probes_make_the_fact_unknown_and_escalate(self):
        verified, report = verify(claims(head_sha=H), {}, [
            FakeProbe("git", [observation("head_sha", v.VERIFIED, H, "git")]),
            FakeProbe("adm_execution_record", [observation("head_sha", v.VERIFIED, OTHER, "adm_execution_record")]),
        ])
        self.assertEqual((None, v.UNKNOWN, "probe:conflict"),
                         (r.value(verified, "head_sha"), r.level(verified, "head_sha"), r.get(verified, "head_sha")["source"]))
        self.assertIn("verify.sources_disagree", report["signals"])

    def test_contradiction_without_a_claim_changes_nothing(self):
        verified, report = verify(claims(), {}, [FakeProbe("git", [observation("commit_sha", v.CONTRADICTED, None, "git")])])
        self.assertEqual(v.UNKNOWN, r.level(verified, "commit_sha"))
        self.assertEqual([], report["signals"])

    def test_identity_contradiction_uses_its_specific_signal(self):
        verified, report = verify(claims(worktree="/elsewhere"), {},
                                  [FakeProbe("git", [observation("worktree", v.VERIFIED, "/assigned", "git")])])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "worktree"))
        self.assertEqual(["verify.worktree.path_mismatch"], report["signals"])

    def test_heuristic_claim_that_is_contradicted_is_still_a_false_pass(self):
        start = r.with_fact(claims(), "status", r.fact("PASS", v.DERIVED, "agent:heuristic"))
        _, report = verify(start, {}, [FakeProbe("t", [observation("status", v.CONTRADICTED, None, "t")])])
        self.assertIn("verify.claim_contradicted", report["signals"])

    def test_verify_never_mutates_its_input(self):
        start = claims(head_sha=H)
        before = r.get(start, "head_sha").copy()
        verify(start, {}, [FakeProbe("git", [observation("head_sha", v.VERIFIED, OTHER, "git")])])
        self.assertEqual(before, r.get(start, "head_sha"))

    def test_verification_is_deterministic_regardless_of_observation_order(self):
        obs = [observation("head_sha", v.VERIFIED, H, "git"), observation("git_status", v.VERIFIED, "clean", "git")]
        left, _ = verify(claims(head_sha=H), {}, [FakeProbe("git", obs)])
        right, _ = verify(claims(head_sha=H), {}, [FakeProbe("git", list(reversed(obs)))])
        self.assertEqual(left, right)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=nextplan-test", "-c", "user.email=nextplan@example.invalid", "-c",
         "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class GitProbeRealRepositoryTests(unittest.TestCase):
    """Against real git: a bare remote and a clone, all under the OS temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self.remote, self.work = root / "remote.git", root / "work"
        git(root, "init", "--bare", str(self.remote))
        git(root, "clone", "-q", str(self.remote), str(self.work))
        (self.work / "README.md").write_text("base\n", encoding="utf-8")
        git(self.work, "add", "README.md")
        git(self.work, "commit", "-q", "-m", "base")
        git(self.work, "push", "-q", "origin", "main")
        self.base = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "-b", "feat/x")
        (self.work / "pkg").mkdir()
        (self.work / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
        git(self.work, "add", "pkg/a.py")
        git(self.work, "commit", "-q", "-m", "change")
        git(self.work, "push", "-q", "-u", "origin", "feat/x")
        self.head = git(self.work, "rev-parse", "HEAD")
        self.context = {"worktree": str(self.work), "repo": normalize_repo(str(self.remote)), "branch": "feat/x",
                        "base_sha": self.base, "allowed_paths": ["pkg"]}

    def tearDown(self):
        self._tmp.cleanup()

    def honest_claims(self, **changes):
        facts = dict(head_sha=self.head[:10], commit_sha=self.head, remote_sha=self.head, push_status="pushed",
                     git_status="clean", branch="feat/x", files_changed=["pkg/a.py"], commits=[self.head[:8]])
        facts.update(changes)
        return claims(**facts)

    def test_honest_claims_are_all_verified(self):
        verified, report = verify(self.honest_claims(), self.context, [GitProbe()])
        for field in ("head_sha", "commit_sha", "remote_sha", "push_status", "git_status", "branch", "files_changed",
                      "commits", "base_sha", "worktree", "repo"):
            self.assertEqual(v.VERIFIED, r.level(verified, field), field)
        self.assertEqual(self.head, r.value(verified, "head_sha"))
        self.assertEqual([], report["signals"])

    def test_dirty_out_of_scope_file_contradicts_clean(self):
        (self.work / "stray.txt").write_text("x", encoding="utf-8")
        verified, report = verify(self.honest_claims(), self.context, [GitProbe()])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "git_status"))
        self.assertIn("verify.git_status.dirty_out_of_scope", report["signals"])
        self.assertIn("verify.claim_contradicted", report["signals"])

    def test_nonexistent_commit_is_contradicted(self):
        verified, report = verify(self.honest_claims(commit_sha=OTHER), self.context, [GitProbe()])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "commit_sha"))
        self.assertIn("verify.claim_contradicted", report["signals"])

    def test_unpushed_commit_contradicts_pushed(self):
        (self.work / "pkg" / "b.py").write_text("y = 2\n", encoding="utf-8")
        git(self.work, "add", "pkg/b.py")
        git(self.work, "commit", "-q", "-m", "local only")
        head = git(self.work, "rev-parse", "HEAD")
        verified, report = verify(self.honest_claims(head_sha=head, commit_sha=head, remote_sha=head, commits=None),
                                  self.context, [GitProbe()])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "push_status"))
        self.assertEqual(v.CONTRADICTED, r.level(verified, "remote_sha"))
        self.assertIn("verify.push.local_ahead_of_remote", report["signals"])

    def test_claimed_push_of_a_branch_that_does_not_exist_remotely(self):
        git(self.work, "checkout", "-q", "-b", "feat/never-pushed")
        context = dict(self.context, branch="feat/never-pushed")
        verified, report = verify(self.honest_claims(branch="feat/never-pushed", commits=None), context, [GitProbe()])
        self.assertIn("verify.remote.branch_absent", report["signals"])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "push_status"))

    def test_change_outside_allowed_paths_is_a_scope_violation(self):
        _, report = verify(self.honest_claims(), dict(self.context, allowed_paths=["docs"]), [GitProbe()])
        self.assertIn("verify.diff.out_of_allowed_paths", report["signals"])

    def test_detached_head_is_detected(self):
        git(self.work, "checkout", "-q", "--detach", "HEAD")
        _, report = verify(self.honest_claims(), self.context, [GitProbe()])
        self.assertIn("verify.head.detached", report["signals"])

    def test_wrong_repository_is_detected(self):
        context = dict(self.context, repo="github:ne9221/some-other-project")
        verified, report = verify(self.honest_claims(repo="github:ne9221/some-other-project"), context, [GitProbe()])
        self.assertIn("verify.repo.identity_mismatch", report["signals"])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "repo"))

    def test_required_base_not_contained_is_stale(self):
        git(self.work, "checkout", "-q", "main")
        (self.work / "later.txt").write_text("later\n", encoding="utf-8")
        git(self.work, "add", "later.txt")
        git(self.work, "commit", "-q", "-m", "main moved")
        newer_base = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "feat/x")
        _, report = verify(self.honest_claims(), dict(self.context, base_sha=newer_base), [GitProbe()])
        self.assertIn("verify.base.behind_required_base", report["signals"])

    def test_network_disabled_leaves_remote_claims_reported(self):
        verified, _ = verify(self.honest_claims(), self.context, [GitProbe(network=False)])
        self.assertEqual(v.REPORTED, r.level(verified, "remote_sha"))

    def test_unreachable_remote_is_signalled_and_leaves_claims_reported(self):
        def runner(args, cwd):
            if args[0] == "ls-remote":
                return 128, "", "fatal: unable to access"
            from manager.nextplan.verify import subprocess_git
            return subprocess_git(args, cwd)
        verified, report = verify(self.honest_claims(), self.context, [GitProbe(runner=runner)])
        self.assertIn("verify.github.unreachable", report["signals"])
        self.assertEqual(v.REPORTED, r.level(verified, "remote_sha"))

    def test_probe_refuses_any_write_command(self):
        with self.assertRaises(ValueError):
            GitProbe()._git(str(self.work), "push", "origin", "feat/x")

    def test_missing_worktree_makes_the_probe_unavailable(self):
        _, report = verify(self.honest_claims(), dict(self.context, worktree=str(self.work / "nope")), [GitProbe()])
        self.assertEqual("git", report["probes_unavailable"][0]["probe"])

    def test_probing_does_not_modify_the_worktree(self):
        before = git(self.work, "status", "--porcelain=v1", "-uall")
        index = (self.work / ".git" / "index").read_bytes()
        verify(self.honest_claims(), self.context, [GitProbe()])
        self.assertEqual(before, git(self.work, "status", "--porcelain=v1", "-uall"))
        self.assertEqual(index, (self.work / ".git" / "index").read_bytes())


class ExecutionRecordProbeTests(unittest.TestCase):
    EXECUTION = {"repo_write_evidence": {
        "files_changed": ["pkg/a.py"], "commits": [H], "final_commit_sha": H, "branch": "feat/x",
        "worktree_path": "/w", "push_status": "verified", "remote_sha": H,
        "tests": [{"command": "pytest", "exit_code": 0, "output_summary": "", "started_at": "x", "completed_at": "y"}],
        "tests_status": "passed",
    }}

    def test_adm_verified_repo_write_evidence_is_reused(self):
        verified, _ = verify(claims(remote_sha=H, push_status="pushed"), {"execution_record": self.EXECUTION},
                             [ExecutionRecordProbe()])
        for field in ("remote_sha", "push_status", "head_sha", "commit_sha", "files_changed", "commits"):
            self.assertEqual(v.VERIFIED, r.level(verified, field), field)

    def test_adm_validation_runs_become_test_evidence(self):
        evidence = adm_test_evidence(self.EXECUTION)
        self.assertEqual("adm_run", evidence["source"])
        verified, _ = verify(claims(tests_failed=0), {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual((0, v.VERIFIED), (r.value(verified, "tests_failed"), r.level(verified, "tests_failed")))

    def test_no_evidence_means_unavailable_not_failed(self):
        self.assertIsNone(adm_test_evidence({"repo_write_evidence": None}))
        _, report = verify(claims(), {}, [ExecutionRecordProbe(), TestEvidenceProbe()])
        self.assertEqual(2, len(report["probes_unavailable"]))


class TestEvidenceProbeTests(unittest.TestCase):
    def test_count_mismatch_is_a_hallucinated_result(self):
        evidence = {"source": "artifact", "runs": [{"command": "pytest", "exit_code": 0, "passed": 9, "failed": 0}]}
        verified, report = verify(claims(tests_passed=12, tests_failed=0), {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "tests_passed"))
        self.assertIn("verify.tests.count_mismatch", report["signals"])

    def test_failing_run_contradicts_zero_failures_and_pass(self):
        evidence = {"source": "adm_run", "runs": [{"command": "pytest", "exit_code": 1, "timed_out": False}]}
        verified, report = verify(claims(status="PASS", tests_failed=0), {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "tests_failed"))
        self.assertEqual(v.CONTRADICTED, r.level(verified, "status"))
        self.assertIn("verify.tests.failed", report["signals"])
        self.assertIn("verify.claim_contradicted", report["signals"])

    def test_timeout_counts_as_failure(self):
        evidence = {"source": "adm_run", "runs": [{"command": "pytest", "exit_code": 0, "timed_out": True}]}
        _, report = verify(claims(), {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertIn("verify.tests.failed", report["signals"])


class DriveAndDispatchProbeTests(unittest.TestCase):
    TARGET = {"file_id": "f1", "expected_sha256": hashlib.sha256(b"evidence").hexdigest()}

    def test_readback_match_verifies_sync(self):
        verified, _ = verify(claims(ssot_sync_drive="synced"), {"drive_evidence": self.TARGET},
                             [DriveReadbackProbe(lambda _id: b"evidence")])
        self.assertEqual(v.VERIFIED, r.level(verified, "ssot_sync_drive"))

    def test_upload_claim_without_matching_readback_is_contradicted(self):
        verified, report = verify(claims(ssot_sync_drive="synced"), {"drive_evidence": self.TARGET},
                                  [DriveReadbackProbe(lambda _id: b"something else")])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "ssot_sync_drive"))
        self.assertIn("verify.drive.readback_mismatch", report["signals"])

    def test_unreachable_drive_leaves_the_claim_reported(self):
        def boom(_id):
            raise ConnectionError("403")
        verified, report = verify(claims(ssot_sync_drive="synced"), {"drive_evidence": self.TARGET}, [DriveReadbackProbe(boom)])
        self.assertEqual(v.REPORTED, r.level(verified, "ssot_sync_drive"))
        self.assertIn("verify.drive.unreachable", report["signals"])

    def test_dispatch_record_catches_a_foreign_session(self):
        verified, report = verify(claims(session_id="s-other"), {"dispatch": {"task_id": "t-1", "session_id": "s-w1"}},
                                  [DispatchRecordProbe()])
        self.assertEqual(v.CONTRADICTED, r.level(verified, "session_id"))
        self.assertIn("result.identity_mismatch", report["signals"])


if __name__ == "__main__":
    unittest.main()
