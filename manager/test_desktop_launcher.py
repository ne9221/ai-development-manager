import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("launch_task.ps1")
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell is required")
class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher space ")
        self.root = Path(self.temp.name)
        package = self.root / "manager"; package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")

    def tearDown(self): self.temp.cleanup()

    def runner(self, mode):
        code = '''import json, os, sys
mode = os.environ.get("FAKE_RUNNER_MODE")
if mode == "malformed":
    print("raw-secret-output"); print("raw-secret-stderr", file=sys.stderr); raise SystemExit(1)
status = "completed" if mode == "success" else "interrupted"
print(json.dumps({"status": status, "execution_id": sys.argv[1] if len(sys.argv) > 1 else "generated-id", "error": {"kind": "TaskClaimConflict", "message": "raw-secret"}}))
raise SystemExit(0 if mode == "success" else 1)
'''
        (self.root / "manager" / "execution_runner.py").write_text(code, encoding="utf-8")

    def launch(self, mode, project="Project With Space", task="Task With Space"):
        self.runner(mode)
        log_home = self.root / "runtime logs"
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                   "-ProjectId", project, "-TaskId", task, "-PythonPath", sys.executable,
                   "-RepositoryPath", str(self.root), "-ManagerHome", str(log_home)]
        environment = {**os.environ, "FAKE_RUNNER_MODE": mode}
        result = subprocess.run(command, text=True, capture_output=True, env=environment, timeout=20)
        logs = list((log_home / "logs").glob("*.jsonl"))
        self.assertEqual(1, len(logs))
        return result, json.loads(logs[0].read_text(encoding="utf-8"))

    def test_success_uses_quoted_paths_and_machine_result(self):
        result, diagnostic = self.launch("success")
        self.assertEqual(0, result.returncode)
        self.assertIn("Completed.", result.stdout)
        self.assertEqual("completed", diagnostic["status"])

    def test_exit_one_and_duplicate_conflict_are_safe(self):
        result, diagnostic = self.launch("conflict")
        self.assertEqual(1, result.returncode)
        self.assertIn("Failed/interrupted.", result.stdout)
        self.assertEqual("interrupted", diagnostic["status"])
        self.assertEqual("TaskClaimConflict", diagnostic["error_kind"])
        self.assertNotIn("raw-secret", result.stdout + result.stderr)

    def test_malformed_json_and_raw_stderr_never_reach_diagnostics(self):
        result, diagnostic = self.launch("malformed")
        self.assertEqual(1, result.returncode)
        self.assertEqual("error", diagnostic["status"])
        self.assertNotIn("raw-secret", result.stdout + result.stderr + json.dumps(diagnostic))

    def test_subprocess_launch_failure_is_safe(self):
        log_home = self.root / "runtime logs"
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                   "-ProjectId", "p1", "-TaskId", "t1", "-PythonPath", str(self.root / "missing.exe"),
                   "-RepositoryPath", str(self.root), "-ManagerHome", str(log_home)]
        result = subprocess.run(command, text=True, capture_output=True, timeout=20)
        diagnostic = json.loads(next((log_home / "logs").glob("*.jsonl")).read_text(encoding="utf-8"))
        self.assertEqual(1, result.returncode)
        self.assertEqual("error", diagnostic["status"])
        self.assertNotIn("missing.exe", result.stdout + result.stderr)


if __name__ == "__main__": unittest.main()
