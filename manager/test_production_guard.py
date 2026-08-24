"""Tests for the production runtime checkout drift guard."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
import subprocess

from manager import provenance
from manager.production_guard import (
    PRODUCTION_MARKER_FILENAME,
    ProductionPathGuardError,
    assert_not_production_path,
    is_marked_production_path,
    mark_production_path,
    production_marker_path,
    RuntimeGuardError,
    require_runtime_guard,
    runtime_repository_path,
)


class ProductionGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unmarked_path_is_not_protected(self):
        self.assertFalse(is_marked_production_path(self.root))
        assert_not_production_path(self.root, "do a thing")  # must not raise

    def test_marking_writes_a_readable_json_marker(self):
        mark_production_path(self.root, "a" * 40, self.root / "manager_home")
        marker = production_marker_path(self.root)
        self.assertTrue(marker.exists())
        self.assertIn('"protected": true', marker.read_text(encoding="utf-8"))
        self.assertTrue(is_marked_production_path(self.root))

    def test_marked_path_is_rejected(self):
        mark_production_path(self.root, "a" * 40, self.root / "manager_home")
        with self.assertRaises(ProductionPathGuardError) as ctx:
            assert_not_production_path(self.root, "resolve a dev working directory")
        self.assertEqual("production_path_protected", ctx.exception.code)
        self.assertIn("PRODUCTION_PATH_PROTECTED", str(ctx.exception))

    def test_subdirectory_of_a_marked_path_is_also_rejected(self):
        # A dev Agent must not be able to escape the guard by pointing its
        # working_directory at a nested subdirectory of a marked checkout.
        mark_production_path(self.root, "a" * 40, self.root / "manager_home")
        nested = self.root / "manager" / "subpkg"
        nested.mkdir(parents=True)
        self.assertTrue(is_marked_production_path(nested))
        with self.assertRaises(ProductionPathGuardError):
            assert_not_production_path(nested, "resolve a dev working directory")

    def test_sibling_directory_is_not_protected(self):
        production = self.root / "production"
        production.mkdir(parents=True, exist_ok=True)
        mark_production_path(production, "a" * 40, self.root / "manager_home")
        sibling = self.root / "not-production"
        sibling.mkdir(parents=True, exist_ok=True)
        self.assertFalse(is_marked_production_path(sibling))
        assert_not_production_path(sibling, "do a thing")  # must not raise

    def test_marker_filename_is_the_documented_constant(self):
        mark_production_path(self.root, "a" * 40, self.root / "manager_home")
        self.assertTrue((self.root / PRODUCTION_MARKER_FILENAME).exists())

    def _activated_repo(self):
        repo, home = self.root / "repo", self.root / "home"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        provenance.capture_tested(repo, home)
        provenance.activate(repo, home)
        return repo, home

    def test_valid_production_contract_passes_and_dev_is_unaffected(self):
        repo, home = self._activated_repo()
        self.assertTrue(require_runtime_guard(repo, home)["production"])
        dev = self.root / "dev"; dev.mkdir()
        self.assertEqual({"state": "PASS", "production": False}, require_runtime_guard(dev, home))

    def test_implicit_runtime_identity_comes_from_activated_evidence_not_cwd(self):
        repo, home = self._activated_repo()
        foreign = self.root / "foreign"; foreign.mkdir()
        self.assertEqual(repo.resolve(), runtime_repository_path(home, foreign).resolve())
        previous = Path.cwd()
        try:
            os.chdir(foreign)
            self.assertTrue(require_runtime_guard(manager_home=home)["production"])
        finally:
            os.chdir(previous)

    def test_missing_or_malformed_marker_fails_closed_without_repair(self):
        repo, home = self._activated_repo()
        marker = production_marker_path(repo)
        marker.unlink()
        with self.assertRaisesRegex(RuntimeGuardError, "MARKER_MISSING"):
            require_runtime_guard(repo, home)
        self.assertFalse(marker.exists())
        marker.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeGuardError, "EVIDENCE_INVALID"):
            require_runtime_guard(repo, home)
        self.assertEqual("not json", marker.read_text(encoding="utf-8"))

    def test_dirty_production_tree_fails_closed(self):
        repo, home = self._activated_repo()
        (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeGuardError, "DIRTY_WORKTREE"):
            require_runtime_guard(repo, home)

    def test_all_four_wrappers_keep_the_same_preflight(self):
        manager = Path(__file__).parent
        for name in ("run_command_watcher.ps1", "run_drive_dispatch_ingress.ps1",
                     "run_session_center_supervisor.ps1", "run_refresh.ps1"):
            self.assertIn("manager.provenance verify-running", (manager / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
