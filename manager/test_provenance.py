"""Tests for the TESTED == ACTIVATED == RUNNING production provenance contract.

Every SHA used below comes from a real `git commit` in a throwaway temp
repo -- never a fabricated string -- so these tests exercise the actual
git-backed contract, not a mock of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manager import provenance

MANAGER_DIR = Path(__file__).parent
GIT = shutil.which("git")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _commit_again(repo: Path) -> str:
    (repo / "file.txt").write_text(f"v-{datetime.now().timestamp()}", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "next")
    return provenance.get_git_head_sha(repo)


@unittest.skipUnless(GIT, "git required")
class ProvenanceContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _make_repo(self.root, "repo")
        self.home = self.root / "manager_home"
        self._env_backup = dict(os.environ)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    # 1. three SHA equal -> PASS
    def test_matching_tested_activated_running_passes(self):
        real_sha = provenance.get_git_head_sha(self.repo)
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        contract = provenance.verify_running(self.repo, self.home)
        self.assertEqual(real_sha, contract.tested_sha)
        self.assertEqual(real_sha, contract.activated_sha)
        self.assertEqual(real_sha, contract.running_sha)

    # 2. tested missing -> FAIL
    def test_activate_fails_when_tested_evidence_missing(self):
        with self.assertRaisesRegex(provenance.ProvenanceError, "PROVENANCE_MISMATCH"):
            provenance.activate(self.repo, self.home)

    # 3. activated missing -> FAIL
    def test_verify_running_fails_when_activated_evidence_missing(self):
        provenance.capture_tested(self.repo, self.home)
        with self.assertRaisesRegex(provenance.ProvenanceError, "PROVENANCE_MISMATCH"):
            provenance.verify_running(self.repo, self.home)

    # 4. running missing (not a real checkout at runtime) -> FAIL
    def test_verify_running_fails_when_repository_path_is_not_a_git_checkout(self):
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        not_a_repo = self.root / "not_a_repo"
        not_a_repo.mkdir()
        with self.assertRaises(provenance.ProvenanceError):
            provenance.verify_running(not_a_repo, self.home)

    # 5. tested != activated -> FAIL
    def test_verify_running_fails_when_activated_evidence_is_internally_inconsistent(self):
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        activated_path = self.home / "provenance" / "activated_sha.json"
        data = json.loads(activated_path.read_text(encoding="utf-8"))
        data["tested_sha"] = "1" * 40  # corrupt: no longer matches activated_sha
        activated_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(provenance.ProvenanceError, "PROVENANCE_MISMATCH"):
            provenance.verify_running(self.repo, self.home)

    # 6. activated != running -> FAIL
    def test_verify_running_fails_after_repo_advances_past_activated_sha(self):
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        _commit_again(self.repo)
        with self.assertRaisesRegex(provenance.ProvenanceError, "PROVENANCE_MISMATCH"):
            provenance.verify_running(self.repo, self.home)

    # 7. fake env running SHA != git HEAD -> the fake env value must be ignored
    def test_verify_running_ignores_fabricated_env_sha_and_uses_real_git_head(self):
        real_sha = provenance.get_git_head_sha(self.repo)
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        os.environ["ADM_WATCHER_GIT_SHA"] = "f" * 40  # attempted fabrication
        contract = provenance.verify_running(self.repo, self.home)
        self.assertEqual(real_sha, contract.running_sha)
        self.assertNotEqual("f" * 40, contract.running_sha)

    # 8. wrong checkout -> FAIL
    def test_verify_running_fails_for_a_different_checkout_than_was_activated(self):
        other_repo = _make_repo(self.root, "other_repo")
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        with self.assertRaisesRegex(provenance.ProvenanceError, "PROVENANCE_MISMATCH"):
            provenance.verify_running(other_repo, self.home)

    # 9. restart preserves correct contract
    def test_restart_reverifies_successfully_without_mutating_state(self):
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        first = provenance.verify_running(self.repo, self.home)
        second = provenance.verify_running(self.repo, self.home)
        self.assertEqual(first.running_sha, second.running_sha)
        self.assertEqual(first.tested_sha, second.tested_sha)
        self.assertEqual(first.activated_sha, second.activated_sha)

    # 10. stale activation evidence -> FAIL
    def test_verify_running_fails_on_stale_activated_evidence(self):
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        activated_path = self.home / "provenance" / "activated_sha.json"
        data = json.loads(activated_path.read_text(encoding="utf-8"))
        old = datetime.now(timezone.utc) - timedelta(days=90)
        data["captured_at"] = old.isoformat()
        activated_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(provenance.ProvenanceError, "stale"):
            provenance.verify_running(self.repo, self.home, max_age_seconds=30 * 24 * 60 * 60)

    # 11. Unicode/space path regression
    def test_full_cycle_succeeds_with_unicode_and_space_in_paths(self):
        weird_root = self.root / "provenance 测试 dir"
        weird_repo = _make_repo(weird_root, "repo 空格")
        weird_home = weird_root / "manager home 主目录"
        real_sha = provenance.get_git_head_sha(weird_repo)
        provenance.capture_tested(weird_repo, weird_home)
        provenance.activate(weird_repo, weird_home)
        contract = provenance.verify_running(weird_repo, weird_home)
        self.assertEqual(real_sha, contract.running_sha)

    # 12. hidden launch regression: install script's provenance gate must not
    # disturb the existing AdmHiddenLaunch.ps1 wiring, and must run before
    # the Scheduled Task is actually registered.
    def test_install_script_runs_provenance_activate_with_repo_on_cwd_or_pythonpath(self):
        """Regression: the installer's `-m manager.provenance activate` call
        must resolve the `manager` package regardless of the caller's own
        working directory. A first version of this gate ran with whatever
        CWD the installer itself was invoked from and failed closed with a
        ModuleNotFoundError on a real HOME activation attempt -- caught by
        the fail-closed gate doing its job, but still a real bug: fix by
        scoping CWD to $RepositoryPath around the call."""
        installer = (MANAGER_DIR / "install_command_watcher.ps1").read_text(encoding="utf-8")
        activate_index = installer.index("manager.provenance activate")
        push_location_index = installer.index("Push-Location -LiteralPath $RepositoryPath")
        pop_location_index = installer.index("Pop-Location")
        self.assertLess(push_location_index, activate_index)
        self.assertLess(activate_index, pop_location_index)

    def test_install_script_still_wires_hidden_launch_and_gates_before_register(self):
        installer = (MANAGER_DIR / "install_command_watcher.ps1").read_text(encoding="utf-8")
        self.assertIn('. (Join-Path $PSScriptRoot "AdmHiddenLaunch.ps1")', installer)
        self.assertIn(
            'New-AdmHiddenScheduledTaskAction -RepositoryPath $RepositoryPath -WrapperName "command-watcher"',
            installer,
        )
        provenance_call_index = installer.index("manager.provenance activate")
        register_index = installer.index("Register-ScheduledTask")
        self.assertLess(
            provenance_call_index, register_index,
            "provenance activation must be gated before Register-ScheduledTask",
        )


@unittest.skipUnless(GIT, "git required")
class RuntimeEvidenceReadableByDashboardTests(unittest.TestCase):
    """Invariant #17 (see manager/test_dashboard_truth_contract.py's
    WatcherVersionIdentityMustMatchDashboardDisplay / _runtime_identity_gate
    docstring): 'Watcher HEAD vs Dashboard-displayed runtime identity' had
    no production evidence to check against, only a test-fixture. These
    tests exercise the same PASS/FAIL semantics against the real,
    on-disk evidence file verify_running() now writes -- proving the
    invariant is backed by genuine production evidence, not a fixture.
    Neither this file nor test_dashboard_truth_contract.py's assertions
    are altered to make this pass."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _make_repo(self.root, "repo")
        self.home = self.root / "manager_home"

    def tearDown(self):
        self._tmp.cleanup()

    def test_matching_identity_passes_from_real_evidence_file(self):
        self.assertEqual("FAIL", provenance.identity_gate_from_evidence(self.home))
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        provenance.verify_running(self.repo, self.home)
        self.assertEqual("PASS", provenance.identity_gate_from_evidence(self.home))

    def test_missing_evidence_fails_closed(self):
        self.assertIsNone(provenance.read_runtime_evidence(self.home))
        self.assertEqual("FAIL", provenance.identity_gate_from_evidence(self.home))

    def test_a_failed_verify_never_produces_a_passing_evidence_file(self):
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        _commit_again(self.repo)  # activated != running now
        with self.assertRaises(provenance.ProvenanceError):
            provenance.verify_running(self.repo, self.home)
        # No evidence file was ever written for the mismatched attempt.
        self.assertEqual("FAIL", provenance.identity_gate_from_evidence(self.home))

    def test_evidence_file_carries_all_required_fields(self):
        provenance.capture_tested(self.repo, self.home)
        provenance.activate(self.repo, self.home)
        provenance.verify_running(self.repo, self.home)
        evidence = provenance.read_runtime_evidence(self.home)
        for field in ("tested_sha", "activated_sha", "running_sha", "repository_path", "branch", "captured_at"):
            self.assertIn(field, evidence)


class RunnerAndInstallerTextTests(unittest.TestCase):
    """Static checks mirroring the acceptance harness Gate 7, plus that the
    referenced identifiers are wired to real values, not just present."""

    def test_runner_sets_all_three_provenance_env_vars_from_contract(self):
        runner = (MANAGER_DIR / "run_command_watcher.ps1").read_text(encoding="utf-8")
        for name in ("ADM_WATCHER_GIT_SHA", "ADM_TESTED_GIT_SHA", "ADM_ACTIVATED_GIT_SHA"):
            self.assertIn(name, runner)
        self.assertIn("manager.provenance verify-running", runner)
        self.assertIn('$env:ADM_WATCHER_GIT_SHA = $contract.running_sha', runner)
        self.assertIn('$env:ADM_TESTED_GIT_SHA = $contract.tested_sha', runner)
        self.assertIn('$env:ADM_ACTIVATED_GIT_SHA = $contract.activated_sha', runner)

    def test_runner_fails_closed_before_launching_watcher_module(self):
        runner = (MANAGER_DIR / "run_command_watcher.ps1").read_text(encoding="utf-8")
        gate_index = runner.index("manager.provenance verify-running")
        launch_index = runner.index("manager.command_watcher --once")
        exit_index = runner.index("exit 1", gate_index)
        self.assertLess(gate_index, exit_index)
        self.assertLess(exit_index, launch_index)


if __name__ == "__main__":
    unittest.main()
