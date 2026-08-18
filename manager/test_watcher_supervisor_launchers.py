"""Regression coverage for run_command_watcher.ps1 / run_session_center_supervisor.ps1:
neither wrapper may override GOOGLE_DRIVE_TOKEN with a ManagerHome-derived path.
That path is not guaranteed to hold a real token (the canonical, already-working
credential lives at collectors.publish_drive.token_path()'s own default), and an
unconditional override here silently shadows it -- collectors.publish_drive itself
already fully covers correct resolution/refresh/reauth behavior (see
collectors/test_drive_auth.py); these tests only guard the wrapper scripts' own
environment setup, by actually executing them with a stub Python module standing
in for the real manager module they invoke."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
MANAGER_DIR = Path(__file__).parent


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell is required")
class LauncherEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher-env-test-")
        self.root = Path(self.temp.name)
        package = self.root / "manager"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        self.manager_home = self.root / "manager-home"
        self.manager_home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _write_env_dump_stub(self, module_name):
        (self.root / "manager" / f"{module_name}.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'GOOGLE_DRIVE_TOKEN': os.environ.get('GOOGLE_DRIVE_TOKEN'), "
            "'AI_MANAGER_HOME': os.environ.get('AI_MANAGER_HOME')}))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

    def _run(self, script_name, extra_args):
        script = MANAGER_DIR / script_name
        command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(script), "-PythonPath", sys.executable,
                   "-RepositoryPath", str(self.root), "-ManagerHome", str(self.manager_home), *extra_args]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30, cwd=str(self.root))
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_command_watcher_wrapper_never_overrides_google_drive_token(self):
        self._write_env_dump_stub("command_watcher")
        env = self._run("run_command_watcher.ps1", ["-GcsBucket", "test-bucket", "-GcsObject", "test-object"])
        self.assertIsNone(env["GOOGLE_DRIVE_TOKEN"], "wrapper must not shadow the canonical Drive token resolver")
        self.assertEqual(str(self.manager_home), env["AI_MANAGER_HOME"], "unrelated AI_MANAGER_HOME wiring must be unaffected")

    def test_session_center_supervisor_wrapper_never_overrides_google_drive_token(self):
        self._write_env_dump_stub("session_center_supervisor")
        env = self._run("run_session_center_supervisor.ps1", ["-StateFile", str(self.root / "state.json")])
        self.assertIsNone(env["GOOGLE_DRIVE_TOKEN"], "wrapper must not shadow the canonical Drive token resolver")
        self.assertEqual(str(self.manager_home), env["AI_MANAGER_HOME"], "unrelated AI_MANAGER_HOME wiring must be unaffected")

    def test_preexisting_google_drive_token_env_var_still_passes_through_untouched(self):
        """If the caller (or the machine's own environment) already has a real
        GOOGLE_DRIVE_TOKEN set, the wrapper must not clobber it either -- it
        simply never touches this variable at all anymore."""
        self._write_env_dump_stub("command_watcher")
        script = MANAGER_DIR / "run_command_watcher.ps1"
        command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(script), "-PythonPath", sys.executable,
                   "-RepositoryPath", str(self.root), "-ManagerHome", str(self.manager_home),
                   "-GcsBucket", "test-bucket", "-GcsObject", "test-object"]
        environment = {**os.environ, "GOOGLE_DRIVE_TOKEN": r"C:\already\set\token.json"}
        result = subprocess.run(command, text=True, capture_output=True, timeout=30, cwd=str(self.root), env=environment)
        env = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(r"C:\already\set\token.json", env["GOOGLE_DRIVE_TOKEN"])


if __name__ == "__main__":
    unittest.main()
