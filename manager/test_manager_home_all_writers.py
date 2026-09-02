"""One runtime-home contract for *every* durable ADM runtime writer.

manager/test_manager_home_checkout_contamination.py closed the Phase-1
cursor vector. This module closes the rest: `refresh_status`,
`quota_history`, `claude_config_locks` and `quota_reader` each carried
their own spelling of the fallback --

    os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager")

-- which bypassed the resolver entirely. That is not a theoretical
bypass. With ``AI_MANAGER_HOME=""`` (a wrapper invoked with an empty
-ManagerHome) ``os.environ.get`` returns the empty string rather than the
default, ``Path("")`` is ``Path(".")``, and the writers land in the
current working directory. Reproduced against 7a4b3bb with cwd set to a
checkout: ``claude_config_locks.json`` and ``runtime/quota_history.json``
were both written into the checkout, dirtying it exactly as the original
outage did.

The tests here build real git work trees and assert against the
filesystem, so they cannot pass vacuously against the pre-fix modules.
"""

import ast
import io
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

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every module that writes durable ADM runtime state. Read-only consumers
#: (dashboard.py, run_live_stability_gate_c.py -- zero write operations in
#: the whole file) and the production gate (production_guard.py, whose
#: repo-root fallback is deliberate developer-checkout semantics) are
#: intentionally absent.
DURABLE_WRITER_MODULES = (
    "manager/phase1_cursor.py",
    "manager/command_watcher.py",
    "manager/drive_dispatch_watcher.py",
    "manager/github_dispatch_watcher.py",
    "manager/session_center_supervisor.py",
    "manager/refresh_status.py",
    "manager/quota_history.py",
    "manager/claude_config_locks.py",
    "manager/quota_reader.py",
)


def _make_git_checkout(base, name="checkout"):
    checkout = Path(base) / name
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


class AdversarialEnvironmentTests(unittest.TestCase):
    """Step 3: empty, blank, missing and degenerate homes all fail closed."""

    BLANK_VALUES = ("", "   ", "\t")

    def test_blank_ai_manager_home_does_not_resolve_to_the_working_directory(self):
        """The exact shape of the reproduced bypass: AI_MANAGER_HOME="".

        os.environ.get("AI_MANAGER_HOME", <default>) returns "" here, not
        the default, so the pre-fix writers resolved to Path("") == ".".
        """
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp) / "userprofile"
            external.mkdir()
            for blank in self.BLANK_VALUES:
                with self.subTest(value=repr(blank)):
                    environ = {"AI_MANAGER_HOME": blank,
                               "USERPROFILE": str(external),
                               "HOME": str(external)}
                    resolved = resolve_manager_home(environ=environ)
                    self.assertEqual(resolved,
                                     external / ".ai-development-manager")
                    self.assertNotEqual(resolved, Path("."))

    def test_blank_explicit_argument_is_also_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp) / "userprofile"
            external.mkdir()
            environ = {"USERPROFILE": str(external), "HOME": str(external)}
            for blank in self.BLANK_VALUES:
                with self.subTest(value=repr(blank)):
                    self.assertEqual(resolve_manager_home(blank, environ=environ),
                                     external / ".ai-development-manager")

    def test_missing_userprofile_and_home_with_degenerate_path_home(self):
        """Step 3: Path.home() forced to ".", the repo root, and "/"."""
        degenerate = [".", "", os.sep]
        for value in degenerate:
            with self.subTest(path_home=repr(value)):
                with patch("manager.manager_home.Path.home", return_value=Path(value)):
                    self.assertIsNone(canonical_manager_home({}))
                    with self.assertRaises(ManagerHomeError):
                        resolve_manager_home(environ={})

    def test_path_home_inside_the_repo_root_is_rejected(self):
        """A user home that is itself a checkout must not become the ADM
        home: the git-worktree guard is the backstop after canonicalization."""
        with patch("manager.manager_home.Path.home", return_value=REPO_ROOT):
            with self.assertRaises(ManagerHomeError) as ctx:
                resolve_manager_home(environ={})
        self.assertIn("MANAGER_HOME_IN_CHECKOUT", str(ctx.exception))

    def test_unresolvable_path_home_fails_closed(self):
        for error in (RuntimeError("no home"), OSError("no home")):
            with self.subTest(error=type(error).__name__):
                with patch("manager.manager_home.Path.home", side_effect=error):
                    self.assertIsNone(canonical_manager_home({}))
                    with self.assertRaises(ManagerHomeError):
                        resolve_manager_home(environ={})


class ExplicitHomeContractTests(unittest.TestCase):
    """Step 4: the accept/reject table for explicit and env-supplied homes."""

    def test_external_temp_home_is_accepted_explicitly_and_via_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "external-home"
            self.assertEqual(resolve_manager_home(str(home), environ={}), home)
            self.assertEqual(
                resolve_manager_home(environ={"AI_MANAGER_HOME": str(home)}), home)

    def test_repo_and_repo_subdirectories_are_rejected_both_ways(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            candidates = (checkout, checkout / "runtime",
                          checkout / "runtime" / "nested")
            for candidate in candidates:
                with self.subTest(path=str(candidate), source="explicit"):
                    with self.assertRaises(ManagerHomeError) as ctx:
                        resolve_manager_home(str(candidate), environ={})
                    self.assertIn("MANAGER_HOME_IN_CHECKOUT", str(ctx.exception))
                with self.subTest(path=str(candidate), source="env"):
                    with self.assertRaises(ManagerHomeError):
                        resolve_manager_home(
                            environ={"AI_MANAGER_HOME": str(candidate)})

    def test_canonical_production_style_external_home_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "userprofile"
            profile.mkdir()
            environ = {"USERPROFILE": str(profile), "HOME": str(profile)}
            self.assertEqual(resolve_manager_home(environ=environ),
                             profile / ".ai-development-manager")


class DurableWriterAdversarialTests(unittest.TestCase):
    """Step 7: every migrated writer, driven for real with cwd inside a
    checkout and a blank AI_MANAGER_HOME -- the reproduced bypass."""

    def _run_in_checkout(self, checkout, body, environ_extra=None):
        original = os.getcwd()
        env = dict(os.environ)
        env["AI_MANAGER_HOME"] = ""
        env.update(environ_extra or {})
        try:
            os.chdir(checkout)
            with patch.dict(os.environ, env, clear=True):
                return body()
        finally:
            os.chdir(original)

    def _assert_clean(self, checkout):
        self.assertEqual(_git_status(checkout), "",
                         "a durable writer contaminated the checkout")

    def test_claude_config_locks_never_writes_into_the_checkout(self):
        """PRE-FIX this wrote <checkout>/claude_config_locks.json."""
        from manager import claude_config_locks
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            external = Path(tmp) / "profile"
            external.mkdir()

            def body():
                return (claude_config_locks.default_state_path(),
                        claude_config_locks.default_lock_path())

            state, lock = self._run_in_checkout(
                checkout, body,
                {"USERPROFILE": str(external), "HOME": str(external)})
            for path in (state, lock):
                self.assertTrue(Path(path).is_absolute())
                self.assertFalse(str(path).startswith(str(checkout)))
            self.assertFalse((checkout / "claude_config_locks.json").exists())
            self._assert_clean(checkout)

    def test_quota_history_store_never_writes_into_the_checkout(self):
        """PRE-FIX this wrote <checkout>/runtime/quota_history.json."""
        from manager.quota_history import get_default_quota_history_store
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            external = Path(tmp) / "profile"
            external.mkdir()

            def body():
                store = get_default_quota_history_store()
                store.clear()  # the store's own documented write path
                return store.path

            path = self._run_in_checkout(
                checkout, body,
                {"USERPROFILE": str(external), "HOME": str(external)})
            self.assertTrue(Path(path).is_absolute())
            self.assertFalse((checkout / "runtime").exists())
            self._assert_clean(checkout)

    def test_refresh_status_account_discovery_never_uses_the_checkout(self):
        from manager import refresh_status
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            external = Path(tmp) / "profile"
            external.mkdir()

            def body():
                # Both helpers resolve a home internally when given none.
                refresh_status.discover_claude_accounts()
                refresh_status.discover_claude_config_dirs()
                return True

            self.assertTrue(self._run_in_checkout(
                checkout, body,
                {"USERPROFILE": str(external), "HOME": str(external)}))
            self.assertFalse((checkout / "config").exists())
            self._assert_clean(checkout)

    def test_every_writer_fails_closed_when_no_home_can_be_derived(self):
        """No external home at all: raise, and write nothing anywhere."""
        from manager import claude_config_locks, refresh_status
        from manager.quota_history import get_default_quota_history_store
        from manager.phase1_cursor import save_phase1_cursor

        cases = (
            ("claude_config_locks", claude_config_locks.default_state_path),
            ("quota_history", get_default_quota_history_store),
            ("refresh_status", refresh_status.discover_claude_accounts),
            ("phase1_cursor",
             lambda: save_phase1_cursor({"project_cursor": 1, "generation": 0})),
        )
        # No AI_MANAGER_HOME, no USERPROFILE, no HOME, and a degenerate
        # Path.home(): there is nowhere safe left, so every writer must
        # raise rather than fall back to the cwd it is standing in.
        stripped = {"USERPROFILE": "", "HOME": ""}
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            for name, call in cases:
                with self.subTest(writer=name):
                    with patch("manager.manager_home.Path.home",
                               return_value=Path(".")):
                        with self.assertRaises(ManagerHomeError):
                            self._run_in_checkout(checkout, call, stripped)
            self._assert_clean(checkout)

    def test_quota_reader_gate_degrades_instead_of_breaking_a_real_read(self):
        """The acceptance gate is a local affordance: an unresolvable home
        makes it a no-op rather than failing a genuine quota read."""
        from manager.quota_reader import _gate_home
        with patch("manager.manager_home.Path.home", return_value=Path(".")):
            with patch.dict(os.environ, {"AI_MANAGER_HOME": ""}, clear=True):
                self.assertIsNone(_gate_home())


class TestIsolationTests(unittest.TestCase):
    """Step 5: the suite's own home must never be the live production one."""

    def test_session_home_is_external_and_not_the_production_home(self):
        import conftest

        session_home = os.environ.get("AI_MANAGER_HOME")
        self.assertTrue(session_home, "the suite must pin AI_MANAGER_HOME")
        self.assertEqual(conftest.ISOLATED_MANAGER_HOME, session_home)

        production = conftest._canonical_production_home()
        if production is not None:
            self.assertFalse(conftest._same_path(session_home, production),
                             "the suite is pointed at the live production home")
        self.assertIsNone(conftest._enclosing_git_worktree(session_home))

    def test_per_test_override_still_wins_over_the_session_home(self):
        from manager.phase1_cursor import _resolve_cursor_path
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "per-test-home"
            with patch.dict(os.environ, {"AI_MANAGER_HOME": str(override)}):
                self.assertEqual(
                    _resolve_cursor_path(),
                    override / "runtime" / "phase1-cursor.json")

    def test_conftest_refuses_an_ambient_production_home(self):
        """Pure path comparison -- this test never touches that directory."""
        import conftest

        production = conftest._canonical_production_home()
        self.assertIsNotNone(production)
        with patch.dict(os.environ, {"AI_MANAGER_HOME": production}):
            with self.assertRaises(RuntimeError) as ctx:
                conftest._install_isolated_manager_home()
        self.assertIn("live production manager home", str(ctx.exception))

    def test_conftest_refuses_an_ambient_home_inside_a_checkout(self):
        import conftest

        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            with patch.dict(os.environ, {"AI_MANAGER_HOME": str(checkout)}):
                with self.assertRaises(RuntimeError) as ctx:
                    conftest._install_isolated_manager_home()
            self.assertIn("git work tree", str(ctx.exception))


class SessionCenterExplicitHomeTests(unittest.TestCase):
    """Step 8: re-verify only. --manager-home must reach scheduler
    provenance, the runtime state directory and the self-heal sweep."""

    def test_explicit_manager_home_beats_a_divergent_environment(self):
        from manager import session_center_supervisor

        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "temp-A"
            decoy = Path(tmp) / "temp-B"
            repo = Path(tmp) / "repo"
            for directory in (explicit, decoy, repo):
                directory.mkdir()

            def fake_start(manager_home, component, *a, **k):
                seen["start"] = str(manager_home)
                return {"scheduler_invocation_id": "x"}

            def fake_finish(manager_home, context, status):
                seen["finish"] = str(manager_home)

            def fake_recover(manager_home, *a, **k):
                seen["recover"] = str(manager_home)

            def fake_maintain(repository_path, maintenance_path, self_heal_path):
                seen["runtime_dir"] = str(Path(maintenance_path).parent)
                return {}

            def no_drive():
                raise RuntimeError("no Drive in this test")

            with patch.dict(os.environ, {"AI_MANAGER_HOME": str(decoy)}), \
                 patch("manager.scheduler_provenance.start", fake_start), \
                 patch("manager.scheduler_provenance.finish", fake_finish), \
                 patch("manager.runtime_supervisor.try_check_and_recover", fake_recover), \
                 patch.object(session_center_supervisor, "maintain_command_watcher",
                              fake_maintain), \
                 patch("collectors.publish_drive.build_service", no_drive), \
                 patch("sys.stdout", io.StringIO()):
                exit_code = session_center_supervisor.main([
                    "--python-path", sys.executable,
                    "--repository-path", str(repo),
                    "--manager-home", str(explicit),
                    "--state-file", str(Path(tmp) / "state.json"),
                ])

            self.assertEqual(0, exit_code)
            for stage in ("start", "finish", "recover"):
                self.assertEqual(str(explicit), seen.get(stage), stage)
            self.assertEqual(str(explicit / "runtime"), seen.get("runtime_dir"))
            self.assertNotIn(str(decoy), list(seen.values()))


class SingleResolverStaticContractTests(unittest.TestCase):
    """Step 6: AST-based, so a module may still *describe* the old spelling
    in a docstring or comment without failing the audit."""

    def _executable_source(self, path):
        """Module source with comments and docstrings blanked out."""
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        lines = source.splitlines()
        blanked = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc = body[0]
                blanked.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
        kept = []
        for number, line in enumerate(lines, 1):
            if number in blanked or line.lstrip().startswith("#"):
                kept.append("")
            else:
                kept.append(line)
        return kept

    #: Executable spellings that would reintroduce a second contract.
    FORBIDDEN = (
        'AI_MANAGER_HOME", "."',
        "AI_MANAGER_HOME', '.'",
        'AI_MANAGER_HOME") or os.getcwd()',
        'AI_MANAGER_HOME", Path.home()',
        'AI_MANAGER_HOME") or os.path.expanduser',
        'AI_MANAGER_HOME") or Path',
        'getenv("AI_MANAGER_HOME", ".")',
        '.ai-development-manager"',
    )

    def test_no_durable_writer_resolves_the_home_itself(self):
        offenders = []
        for path in DURABLE_WRITER_MODULES:
            for number, line in enumerate(self._executable_source(path), 1):
                for spelling in self.FORBIDDEN:
                    if spelling in line:
                        offenders.append("%s:%d  %s" % (path, number, line.strip()))
        self.assertEqual(offenders, [], "durable writers must use the resolver")

    def test_every_durable_writer_imports_the_canonical_resolver(self):
        for path in DURABLE_WRITER_MODULES:
            with self.subTest(module=path):
                source = "\n".join(self._executable_source(path))
                self.assertIn("from manager.manager_home import", source)
                self.assertIn("resolve_manager_home", source)

    def test_only_one_runtime_home_resolver_module_exists(self):
        self.assertTrue((REPO_ROOT / "manager" / "manager_home.py").exists())
        self.assertFalse(
            (REPO_ROOT / "manager" / "runtime_home.py").exists(),
            "a second runtime-home resolver module would be a second contract")

    def test_gitignore_still_does_not_hide_checkout_contamination(self):
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        entries = {line.strip() for line in text.splitlines()}
        for blanket in ("runtime/", "runtime", "logs/", "logs",
                        "health-evidence.json", "claude_config_locks.json",
                        "*.json"):
            self.assertNotIn(blanket, entries)


if __name__ == "__main__":
    unittest.main()
