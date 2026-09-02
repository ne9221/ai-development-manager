"""Runtime-home resolution contract tests.

These exist because of a real, proven production outage (2026-09-02): a
cwd fallback in the durable Phase-1 cursor path let a process write
`<production checkout>/runtime/phase1-cursor.json`, which dirtied the
checkout and made the Rule 18 provenance guard fail-close every ADM
component for ~54 minutes.

The tests deliberately include a NEGATIVE CONTROL
(test_old_cwd_fallback_would_have_contaminated_the_checkout) that
reproduces the pre-fix resolution and asserts it DOES contaminate, so the
post-fix assertions cannot silently become vacuous.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.phase1_cursor import (_resolve_cursor_path, load_phase1_cursor,
                                   save_phase1_cursor)
from manager.provenance import is_checkout_clean
from manager.runtime_home import (CANONICAL_HOME_DIRNAME, RuntimeHomeError,
                                  resolve_ai_manager_home)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


class _TempGitCheckout:
    """A throwaway git checkout, so contamination is observable exactly the
    way `is_checkout_clean()` observes it in production."""

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="adm-checkout-"))
        _git(self.dir, "init", "-q")
        _git(self.dir, "config", "user.email", "t@example.com")
        _git(self.dir, "config", "user.name", "t")
        (self.dir / "keep.txt").write_text("x", encoding="utf-8")
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-qm", "base")
        return self.dir

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class ResolveRuntimeHomeTests(unittest.TestCase):

    def test_explicit_manager_home_is_used_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Path(tmp), resolve_ai_manager_home(tmp))

    def test_explicit_manager_home_beats_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = resolve_ai_manager_home(tmp, environ={"AI_MANAGER_HOME": tempfile.gettempdir()})
            self.assertEqual(Path(tmp), resolved)

    def test_environment_value_is_used_when_no_explicit_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Path(tmp), resolve_ai_manager_home(environ={"AI_MANAGER_HOME": tmp}))

    def test_missing_environment_falls_back_to_canonical_user_home(self):
        resolved = resolve_ai_manager_home(environ={})
        self.assertEqual(Path.home() / CANONICAL_HOME_DIRNAME, resolved)

    def test_canonical_fallback_is_never_the_current_working_directory(self):
        resolved = resolve_ai_manager_home(environ={})
        self.assertNotEqual(Path.cwd(), resolved)
        self.assertFalse(str(resolved).startswith(str(Path.cwd())),
                         "runtime home must never live under cwd")

    def test_empty_or_whitespace_environment_value_is_treated_as_unset(self):
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                self.assertEqual(Path.home() / CANONICAL_HOME_DIRNAME,
                                 resolve_ai_manager_home(environ={"AI_MANAGER_HOME": value}))

    def test_unresolvable_user_home_fails_closed(self):
        with patch("manager.runtime_home.Path.home", side_effect=RuntimeError("no home")):
            with self.assertRaises(RuntimeHomeError):
                resolve_ai_manager_home(environ={})

    def test_fail_closed_resolution_creates_no_file(self):
        with _TempGitCheckout() as checkout:
            seed = checkout / "seed.json"
            data = load_phase1_cursor(cursor_path=seed)
            with patch("manager.runtime_home.Path.home", side_effect=RuntimeError("no home")):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("AI_MANAGER_HOME", None)
                    with self.assertRaises(RuntimeHomeError):
                        save_phase1_cursor(data)
            self.assertFalse((checkout / "runtime").exists(),
                             "a fail-closed resolution must write nothing at all")
            self.assertTrue(is_checkout_clean(checkout))


class CursorPathContaminationTests(unittest.TestCase):

    def test_old_cwd_fallback_would_have_contaminated_the_checkout(self):
        """NEGATIVE CONTROL: proves the guarded scenario is real."""
        with _TempGitCheckout() as checkout:
            self.assertTrue(is_checkout_clean(checkout))
            legacy_home = "."
            legacy_path = Path(checkout) / legacy_home / "runtime" / "phase1-cursor.json"
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text(json.dumps({"generation": 6}), encoding="utf-8")
            self.assertFalse(is_checkout_clean(checkout),
                             "pre-fix behavior MUST be detectable as checkout contamination")

    def test_explicit_home_writes_cursor_under_that_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_phase1_cursor(load_phase1_cursor(manager_home=tmp), manager_home=tmp)
            self.assertTrue((Path(tmp) / "runtime" / "phase1-cursor.json").exists())

    def test_missing_env_with_cwd_in_checkout_never_writes_into_checkout(self):
        with _TempGitCheckout() as checkout, tempfile.TemporaryDirectory() as fake_home:
            env = dict(os.environ)
            env.pop("AI_MANAGER_HOME", None)
            env["USERPROFILE"] = fake_home
            env["HOME"] = fake_home
            env["PYTHONPATH"] = str(REPO_ROOT)
            code = ("from manager.phase1_cursor import save_phase1_cursor, load_phase1_cursor;"
                    "save_phase1_cursor(load_phase1_cursor())")
            result = subprocess.run([sys.executable, "-c", code], cwd=str(checkout),
                                    env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((checkout / "runtime").exists(),
                             "checkout was contaminated: " + result.stderr)
            self.assertTrue(is_checkout_clean(checkout))
            self.assertTrue((Path(fake_home) / CANONICAL_HOME_DIRNAME / "runtime"
                             / "phase1-cursor.json").exists(),
                            "cursor must land in the canonical external home instead")

    def test_resolved_cursor_path_is_outside_the_repository(self):
        resolved = _resolve_cursor_path()
        self.assertFalse(str(resolved).startswith(str(REPO_ROOT)),
                         "cursor path must not live inside the checkout")

    def test_explicit_cursor_path_still_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "custom.json"
            save_phase1_cursor({"generation": 3}, cursor_path=target)
            self.assertTrue(target.exists())


class CanonicalStatePreservationTests(unittest.TestCase):

    def test_existing_canonical_generation_is_preserved_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir(parents=True)
            existing = {"project_cursor": 3,
                        "per_project_record_cursor": {"ai-development-manager": 916},
                        "per_project_attention_visits": {}, "generation": 2342,
                        "updated_at": "2026-09-02T13:29:54Z"}
            (runtime / "phase1-cursor.json").write_text(json.dumps(existing), encoding="utf-8")

            loaded = load_phase1_cursor(manager_home=tmp)
            self.assertEqual(2342, loaded["generation"])
            self.assertEqual({"ai-development-manager": 916}, loaded["per_project_record_cursor"])

            saved = save_phase1_cursor(loaded, manager_home=tmp, expected_generation=2342)
            self.assertEqual(2343, saved["generation"], "generation must advance, never reset to 0")
            self.assertEqual({"ai-development-manager": 916}, saved["per_project_record_cursor"])

    def test_resolution_is_pure_and_creates_no_directories(self):
        target = Path(tempfile.gettempdir()) / "adm-never-created-by-resolution"
        home = resolve_ai_manager_home(environ={"AI_MANAGER_HOME": str(target)})
        self.assertFalse(home.exists(), "resolution must be pure; only writers may mkdir")


class GovernanceContractTests(unittest.TestCase):

    def test_gitignore_has_no_blanket_runtime_ignore(self):
        gitignore = REPO_ROOT / ".gitignore"
        entries = [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()]
        for banned in ("runtime/", "runtime", "/runtime", "/runtime/"):
            self.assertNotIn(banned, entries,
                             "ignoring runtime/ would hide checkout contamination from the "
                             "Rule 18 cleanliness guard -- the fix must be path resolution, "
                             "not concealment")

    def test_unrelated_runtime_like_drift_is_still_detected(self):
        with _TempGitCheckout() as checkout:
            self.assertTrue(is_checkout_clean(checkout))
            (checkout / "runtime").mkdir()
            (checkout / "runtime" / "something-else.json").write_text("{}", encoding="utf-8")
            self.assertFalse(is_checkout_clean(checkout),
                             "checkout contamination must remain detectable after the fix")

    def test_production_wrappers_still_set_ai_manager_home_explicitly(self):
        wrappers = ["manager/run_command_watcher.ps1", "manager/run_drive_dispatch_ingress.ps1",
                    "manager/run_github_dispatch_ingress.ps1", "manager/run_refresh.ps1",
                    "manager/run_session_center_supervisor.ps1"]
        for name in wrappers:
            with self.subTest(wrapper=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertIn("$env:AI_MANAGER_HOME = $ManagerHome", text)
                self.assertIn("[Parameter(Mandatory=$true)][string]$ManagerHome", text)

    def test_no_module_retains_a_cwd_runtime_home_fallback(self):
        """AST-based so that prose mentioning the old expression in a
        docstring (this fix documents it deliberately) is never mistaken
        for a live cwd fallback -- only real executable code counts."""
        offenders = []
        for path in sorted(REPO_ROOT.rglob("*.py")):
            if path.name.startswith("test_") or ".git" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                rendered = ast.unparse(node).replace("'", '"')
                if 'os.environ.get("AI_MANAGER_HOME", ".")' in rendered:
                    offenders.append(str(path.relative_to(REPO_ROOT)) + ": " + rendered)
                if 'os.environ.get("AI_MANAGER_HOME")' in rendered and "os.getcwd()" in rendered:
                    offenders.append(str(path.relative_to(REPO_ROOT)) + ": " + rendered)
        self.assertEqual([], offenders, "cwd fallback for the runtime home must not reappear")

    def test_the_cwd_fallback_detector_actually_detects_one(self):
        """Negative control for the detector above: it must flag real code."""
        tree = ast.parse('home = os.environ.get("AI_MANAGER_HOME", ".")')
        found = [ast.unparse(n).replace("'", '"') for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and 'os.environ.get("AI_MANAGER_HOME", ".")' in ast.unparse(n).replace("'", '"')]
        self.assertEqual(1, len(found))


if __name__ == "__main__":
    unittest.main()
