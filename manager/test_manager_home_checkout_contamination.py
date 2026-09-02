"""Regression suite for the 2026-09-02 checkout-contamination outage.

Live incident: a process running with cwd set to the activated production
checkout and no ``AI_MANAGER_HOME`` in its environment wrote
``runtime/phase1-cursor.json`` into that checkout.  ``is_checkout_clean()``
counts untracked files, so the tree went dirty and rule 18 fail-closed
every Scheduled Task for ~54 minutes.

Every test here builds a *real* git work tree on disk and asserts against
the filesystem, so none of them can pass vacuously if the resolver is
reverted to its old ``os.environ.get("AI_MANAGER_HOME", ".")`` default.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.manager_home import (
    ManagerHomeError,
    canonical_manager_home,
    resolve_manager_home,
)
from manager.phase1_cursor import (
    _resolve_cursor_path,
    load_phase1_cursor,
    save_phase1_cursor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_git_checkout(base):
    """A real git work tree -- the thing the outage was about."""
    checkout = Path(base) / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True,
                   capture_output=True)
    (checkout / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    identity = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git"] + identity + ["add", "-A"], cwd=str(checkout),
                   check=True, capture_output=True)
    subprocess.run(["git"] + identity + ["commit", "-qm", "baseline"],
                   cwd=str(checkout), check=True, capture_output=True)
    return checkout


def _git_status(checkout):
    return subprocess.run(["git", "status", "--short"], cwd=str(checkout),
                          capture_output=True, text=True, check=True).stdout


class CursorPathResolutionTests(unittest.TestCase):
    """Contract 1/2/3/4: explicit > env > canonical > fail closed."""

    def test_explicit_manager_home_is_used_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "explicit-home"
            path = _resolve_cursor_path(manager_home=home)
            self.assertEqual(path, home / "runtime" / "phase1-cursor.json")

    def test_explicit_manager_home_actually_receives_the_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "explicit-home"
            save_phase1_cursor({"project_cursor": 3, "generation": 0},
                               manager_home=home)
            written = home / "runtime" / "phase1-cursor.json"
            self.assertTrue(written.exists())
            document = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(document["project_cursor"], 3)

    def test_env_manager_home_is_used_when_no_explicit_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "env-home"
            with patch.dict(os.environ, {"AI_MANAGER_HOME": str(home)}):
                save_phase1_cursor({"project_cursor": 7, "generation": 0})
            self.assertTrue((home / "runtime" / "phase1-cursor.json").exists())

    def test_canonical_user_level_home_is_used_when_env_is_absent(self):
        """Contract 3: the fallback is the external user home, never cwd."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_profile = Path(tmp) / "userprofile"
            fake_profile.mkdir()
            environ = {"USERPROFILE": str(fake_profile), "HOME": str(fake_profile)}
            resolved = resolve_manager_home(environ=environ)
            self.assertEqual(resolved, fake_profile / ".ai-development-manager")
            self.assertEqual(canonical_manager_home(environ), resolved)

    def test_fails_closed_when_no_home_can_be_derived(self):
        """Contract 4: an underivable home raises -- it never guesses."""
        with patch("manager.manager_home.canonical_manager_home", return_value=None):
            with self.assertRaises(ManagerHomeError) as ctx:
                resolve_manager_home(environ={})
        self.assertIn("MANAGER_HOME_UNRESOLVED", str(ctx.exception))

    def test_fails_closed_when_home_resolves_inside_a_git_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            with self.assertRaises(ManagerHomeError) as ctx:
                resolve_manager_home(str(checkout), environ={})
            self.assertIn("MANAGER_HOME_IN_CHECKOUT", str(ctx.exception))

    def test_no_file_is_created_when_resolution_fails_closed(self):
        """Fail closed means fail closed: not a partial write elsewhere."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            before = set(checkout.rglob("*"))
            with patch.dict(os.environ, {"AI_MANAGER_HOME": str(checkout)}):
                with self.assertRaises(ManagerHomeError):
                    save_phase1_cursor({"project_cursor": 1, "generation": 0})
            self.assertEqual(set(checkout.rglob("*")), before)
            self.assertFalse((checkout / "runtime").exists())


class CheckoutContaminationTests(unittest.TestCase):
    """The live incident itself: cwd is a checkout, the env is unset."""

    def test_cursor_write_with_cwd_in_checkout_never_touches_the_checkout(self):
        """PRE-FIX this created <checkout>/runtime/phase1-cursor.json."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            external = Path(tmp) / "userprofile"
            external.mkdir()
            original = os.getcwd()
            env = dict(os.environ)
            env.pop("AI_MANAGER_HOME", None)
            env["USERPROFILE"] = env["HOME"] = str(external)
            try:
                os.chdir(checkout)
                with patch.dict(os.environ, env, clear=True):
                    save_phase1_cursor({"project_cursor": 1,
                                        "per_project_record_cursor": {"p1": 4},
                                        "per_project_attention_visits": {"p1": 1},
                                        "generation": 5})
            finally:
                os.chdir(original)

            self.assertFalse((checkout / "runtime").exists(),
                             "durable runtime state was written into the git checkout")
            self.assertEqual(_git_status(checkout), "",
                             "checkout went dirty -- rule 18 would fail-close production")
            landed = external / ".ai-development-manager" / "runtime" / "phase1-cursor.json"
            self.assertTrue(landed.exists(),
                            "the write should have landed in the canonical external home")

    def test_subprocess_with_cwd_in_checkout_never_contaminates_it(self):
        """Requirement 5: a child process, not just an in-process call."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            external = Path(tmp) / "userprofile"
            external.mkdir()
            env = dict(os.environ)
            env.pop("AI_MANAGER_HOME", None)
            env["USERPROFILE"] = env["HOME"] = str(external)
            env["PYTHONPATH"] = str(REPO_ROOT)
            script = ("from manager.phase1_cursor import save_phase1_cursor;"
                      "save_phase1_cursor({'project_cursor': 2, 'generation': 0})")
            proc = subprocess.run([sys.executable, "-c", script], cwd=str(checkout),
                                  env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((checkout / "runtime").exists())
            self.assertEqual(_git_status(checkout), "")

    def test_load_with_cwd_in_checkout_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            external = Path(tmp) / "userprofile"
            external.mkdir()
            original = os.getcwd()
            env = dict(os.environ)
            env.pop("AI_MANAGER_HOME", None)
            env["USERPROFILE"] = env["HOME"] = str(external)
            try:
                os.chdir(checkout)
                with patch.dict(os.environ, env, clear=True):
                    cursor = load_phase1_cursor()
            finally:
                os.chdir(original)
            self.assertEqual(cursor["generation"], 0)
            self.assertFalse((checkout / "runtime").exists())
            self.assertEqual(_git_status(checkout), "")


class CanonicalStatePreservationTests(unittest.TestCase):
    """Contract 6/7: an existing cursor is advanced, never reset."""

    def test_existing_cursor_is_not_reset_or_overwritten_wholesale(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            existing = {"project_cursor": 9,
                        "per_project_record_cursor": {"p%d" % i: i for i in range(13)},
                        "per_project_attention_visits": {"p%d" % i: i * 2 for i in range(13)},
                        "generation": 2342,
                        "updated_at": "2026-09-02T12:00:00Z"}
            path = home / "runtime" / "phase1-cursor.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(existing), encoding="utf-8")

            loaded = load_phase1_cursor(manager_home=home)
            self.assertEqual(loaded["generation"], 2342)
            self.assertEqual(len(loaded["per_project_record_cursor"]), 13)

            saved = save_phase1_cursor(loaded, manager_home=home,
                                       expected_generation=2342)
            self.assertEqual(saved["generation"], 2343)
            self.assertEqual(len(saved["per_project_record_cursor"]), 13)
            self.assertEqual(saved["per_project_attention_visits"]["p12"], 24)

    def test_phase1_fairness_state_survives_a_resolver_roundtrip(self):
        """The rotation state the resolver change must not disturb."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with patch.dict(os.environ, {"AI_MANAGER_HOME": str(home)}):
                first = save_phase1_cursor(
                    {"project_cursor": 4,
                     "per_project_record_cursor": {"a": 1, "b": 2},
                     "per_project_attention_visits": {"a": 3},
                     "generation": 0})
                reloaded = load_phase1_cursor()
            self.assertEqual(reloaded, first)
            self.assertEqual(reloaded["project_cursor"], 4)
            self.assertEqual(reloaded["per_project_attention_visits"], {"a": 3})


class RepositoryContractTests(unittest.TestCase):
    """Contracts 8/9/10: wrappers, .gitignore, and detectability."""

    def test_production_wrappers_all_set_the_manager_home_env(self):
        wrappers = ["manager/run_command_watcher.ps1",
                    "manager/run_drive_dispatch_ingress.ps1",
                    "manager/run_github_dispatch_ingress.ps1",
                    "manager/run_refresh.ps1",
                    "manager/run_session_center_supervisor.ps1"]
        for name in wrappers:
            with self.subTest(wrapper=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertIn("$env:AI_MANAGER_HOME = $ManagerHome", text)

    def test_no_product_entrypoint_defaults_the_home_to_the_working_directory(self):
        unsafe_env_default = 'AI_MANAGER_HOME", "."'
        unsafe_cwd_default = 'AI_MANAGER_HOME") or os.getcwd()'
        offenders = []
        for path in sorted(REPO_ROOT.rglob("*.py")):
            if path.name.startswith("test_") or ".git" in path.parts:
                continue
            if path.name == "manager_home.py":
                continue  # documents the old spelling in its module docstring
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                if unsafe_env_default in line or unsafe_cwd_default in line:
                    offenders.append("%s:%d" % (path.relative_to(REPO_ROOT), number))
        self.assertEqual(offenders, [])

    def test_gitignore_does_not_hide_runtime_state_in_the_checkout(self):
        """Contamination must stay *detectable*, not be ignored away."""
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        entries = {line.strip() for line in text.splitlines()}
        blanket_ignores = ("runtime/", "runtime", "logs/", "logs",
                           "health-evidence.json", "*.json")
        for blanket in blanket_ignores:
            self.assertNotIn(blanket, entries,
                             ".gitignore must not mask checkout contamination "
                             "via %r" % (blanket,))

    def test_stray_runtime_state_in_a_checkout_is_still_detectable(self):
        """Contract 10: the cleanliness guard must still catch contamination
        that arrives by some *other* route than this resolver."""
        from manager.provenance import is_checkout_clean
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            self.assertTrue(is_checkout_clean(checkout))

            stray = checkout / "runtime" / "phase1-cursor.json"
            stray.parent.mkdir(parents=True)
            stray.write_text('{"generation": 6}\n', encoding="utf-8")
            self.assertFalse(is_checkout_clean(checkout),
                             "an untracked runtime/ must still dirty the checkout")


if __name__ == "__main__":
    unittest.main()
