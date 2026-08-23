"""Tests for the production runtime checkout drift guard."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manager.production_guard import (
    PRODUCTION_MARKER_FILENAME,
    ProductionPathGuardError,
    assert_not_production_path,
    is_marked_production_path,
    mark_production_path,
    production_marker_path,
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


if __name__ == "__main__":
    unittest.main()
