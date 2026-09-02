"""The runtime_supervisor CLI must validate its manager home before writing.

``--manager-home`` is required, so there was never a silent fallback here.
But it was consumed raw: every helper did ``Path(manager_home) / "runtime"``,
so a caller that passed a checkout path wrote
``runtime/supervisor-last-sweep.json`` and ``health-evidence.json`` straight
into the work tree. That is the same class of contamination that dirtied the
activated production checkout and fail-closed every Scheduled Task for ~54
minutes (2026-09-02); the resolver exists precisely so no writer has to
decide for itself whether a home is safe.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager import runtime_supervisor
from manager.manager_home import ManagerHomeError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_git_checkout(base, name="checkout"):
    checkout = Path(base) / name
    (checkout / ".git").mkdir(parents=True)
    return checkout


class RuntimeSupervisorHomeValidationTests(unittest.TestCase):

    def test_cli_refuses_a_checkout_and_writes_nothing_into_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            before = set(checkout.rglob("*"))

            code = runtime_supervisor.main(["--manager-home", str(checkout),
                                            "--once", "--dry-run"])

            self.assertNotEqual(0, code, "a checkout manager home must not be accepted")
            self.assertEqual(before, set(checkout.rglob("*")),
                             "the refused run still wrote into the checkout")
            self.assertFalse((checkout / "runtime").exists())
            self.assertFalse((checkout / "health-evidence.json").exists())

    def test_cli_refuses_before_the_debounce_marker_is_written(self):
        """The very first durable write is the sweep marker; it must not happen."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            with patch.object(runtime_supervisor, "check_and_recover") as recover:
                code = runtime_supervisor.main(["--manager-home", str(checkout), "--once"])
            self.assertNotEqual(0, code)
            recover.assert_not_called()
            self.assertFalse((checkout / "runtime" / "supervisor-last-sweep.json").exists())

    def test_cli_accepts_a_safe_home_and_passes_the_resolved_path_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "safe-home"
            home.mkdir()
            seen = {}

            def capture(manager_home, **kwargs):
                seen["home"] = manager_home
                return []

            with patch.object(runtime_supervisor, "check_and_recover", side_effect=capture):
                code = runtime_supervisor.main(["--manager-home", str(home),
                                                "--once", "--force", "--dry-run"])
            self.assertEqual(0, code)
            self.assertEqual(Path(str(home)).resolve(), Path(seen["home"]).resolve())
            self.assertTrue(Path(seen["home"]).is_absolute(),
                            "helpers must receive an absolute, validated home")

    def test_cli_refuses_an_unresolvable_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            nested = checkout / "deeply" / "nested"
            nested.mkdir(parents=True)
            code = runtime_supervisor.main(["--manager-home", str(nested),
                                            "--once", "--dry-run"])
            self.assertNotEqual(0, code, "a home nested inside a checkout is still a checkout")

    def test_end_to_end_subprocess_leaves_the_checkout_pristine(self):
        """The wrapper's actual invocation shape, as a real process."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _make_git_checkout(tmp)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT)
            env.pop("AI_MANAGER_HOME", None)
            proc = subprocess.run(
                [sys.executable, "-m", "manager.runtime_supervisor",
                 "--manager-home", str(checkout), "--once", "--dry-run"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180)
            self.assertNotEqual(0, proc.returncode, proc.stdout)
            self.assertEqual([".git"], [p.name for p in checkout.iterdir()])
            self.assertIn("MANAGER_HOME_IN_CHECKOUT", proc.stderr)


class CursorWriterAuditTests(unittest.TestCase):
    """No module may write the Phase-1 cursor outside phase1_cursor.py.

    The mutation lock only serialises writers that take it. That is not a
    guarantee about the world, but it IS a guarantee this repository can
    keep about itself -- provided nothing else ever touches the file.
    """

    CURSOR_NAMES = ("phase1-cursor.json",)
    MUTATORS = ("os.replace", "os.rename", "os.unlink", "os.remove",
                "write_text", "write_bytes", "unlink(")

    def test_only_phase1_cursor_module_mutates_the_cursor_file(self):
        offenders = []
        for path in sorted(REPO_ROOT.glob("manager/*.py")) + \
                sorted(REPO_ROOT.glob("collectors/*.py")) + \
                sorted(REPO_ROOT.glob("cloud/*.py")):
            if path.name in ("phase1_cursor.py",) or path.name.startswith("test_"):
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            if not any(name in body for name in self.CURSOR_NAMES):
                continue
            for number, line in enumerate(body.splitlines(), 1):
                if not any(name in line for name in self.CURSOR_NAMES):
                    continue
                if line.lstrip().startswith("#"):
                    continue
                if any(mutator in line for mutator in self.MUTATORS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
        self.assertEqual([], offenders,
                         "these write the durable cursor outside the locked mutation path: "
                         + "; ".join(offenders))


if __name__ == "__main__":
    unittest.main()
