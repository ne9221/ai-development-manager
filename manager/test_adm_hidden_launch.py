"""Guards against regressing to a console-flashing Scheduled Task launch.

Live HOME process-tree inspection showed the three ADM Scheduled Tasks
(Command Watcher, Session Center Supervisor, Quota Refresh) directly
registering "powershell.exe -WindowStyle Hidden ..." as the task Action.
That flashes a console because the window is only hidden *after* Windows has
already created it. These tests assert every installer instead routes
through the shared AdmHiddenLaunch.ps1 helper, which launches via a
generated VBS wrapper (WshShell.Run windowStyle=0) that never creates a
visible window in the first place, while leaving the legacy PowerShell
arguments themselves untouched.

A first version of AdmHiddenLaunch.ps1 shipped with `exitCode = shell.Run
"...", 0, True` (no parentheses around the call), which is invalid
VBScript whenever the return value is assigned -- VBScript requires
`shell.Run(...)` in that position. Every generated .vbs failed to compile
and Windows Script Host popped a visible "语句未结束" error dialog for
each Scheduled Task tick, which is exactly the class of visible popup this
whole fix exists to eliminate. That regression passed pure text-substring
assertions because the broken line still contained all the expected
substrings -- so this file also actually compiles/runs each generated .vbs
through cscript.exe, for all three real task wrapper names, instead of
only pattern-matching source text.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MANAGER_DIR = Path(__file__).parent
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
CSCRIPT = shutil.which("cscript")

INSTALLERS = [
    ("install_command_watcher.ps1", "command-watcher"),
    ("install_session_center_supervisor.ps1", "session-center-supervisor"),
    ("install_scheduler.ps1", "quota-refresh"),
]


class AdmHiddenLaunchHelperTest(unittest.TestCase):
    def setUp(self):
        self.helper = (MANAGER_DIR / "AdmHiddenLaunch.ps1").read_text(encoding="utf-8")

    def test_defines_hidden_task_action_function(self):
        self.assertIn("function New-AdmHiddenScheduledTaskAction", self.helper)

    def test_action_executable_is_wscript_not_powershell(self):
        self.assertIn('New-ScheduledTaskAction -Execute "wscript.exe"', self.helper)

    def test_generated_vbs_runs_hidden_and_waits_for_exit_code(self):
        # Must be shell.Run(...) with parentheses: this is a VBScript
        # assignment (exitCode = ...), and VBScript is a syntax error
        # ("语句未結束" / "expected end of statement") without them.
        self.assertIn("shell.Run(\"\"powershell.exe $escapedArgs\"\", 0, True)", self.helper)
        self.assertIn("WScript.Quit exitCode", self.helper)

    def test_double_quotes_are_escaped_for_vbs_string_literal(self):
        self.assertIn("$PowerShellArguments.Replace('\"', '\"\"')", self.helper)


@unittest.skipUnless(POWERSHELL and CSCRIPT and os.name == "nt", "Windows PowerShell + cscript required")
class GeneratedVbsActuallyRunsTest(unittest.TestCase):
    """Regression test for the missing-parentheses bug: generate the real
    .vbs for all three tasks and actually compile+execute it with cscript,
    instead of only checking the generator's source text. A harmless local
    .ps1 stands in for the real runner so this never touches a real
    Scheduled Task, GCS bucket, or provider CLI."""

    def _run_wrapper(self, wrapper_name, repo, extra_args=""):
        fake_runner = repo / "manager" / "run_fake.ps1"
        fake_runner.parent.mkdir(parents=True, exist_ok=True)
        fake_runner.write_text(
            "param([Parameter(ValueFromRemainingArguments=$true)]$rest)\nexit 7\n",
            encoding="utf-8",
        )
        arguments = (
            f'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass '
            f'-File "{fake_runner}" -PythonDeps "C:\\Program Files\\Fake Deps" '
            f'-GcsBucket "adm-lock-smoke-test" {extra_args}'
        ).strip()
        script = f'''
$ErrorActionPreference = "Stop"
. "{MANAGER_DIR / 'AdmHiddenLaunch.ps1'}"
$action = New-AdmHiddenScheduledTaskAction -RepositoryPath "{repo}" -WrapperName "{wrapper_name}" -PowerShellArguments '{arguments}'
$action.Arguments
'''
        res = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(0, res.returncode, f"generating the action failed: {res.stderr}")
        vbs_path = res.stdout.strip().strip('"')
        self.assertTrue(Path(vbs_path).exists(), f"expected generated vbs at {vbs_path}")
        run = subprocess.run(
            [CSCRIPT, "//nologo", vbs_path], capture_output=True, text=True, timeout=20,
        )
        return vbs_path, run

    def test_all_three_wrapper_names_compile_and_propagate_exit_code(self):
        for _, wrapper_name in INSTALLERS:
            with self.subTest(wrapper=wrapper_name):
                with tempfile.TemporaryDirectory() as tmp:
                    vbs_path, run = self._run_wrapper(wrapper_name, Path(tmp))
                    self.assertNotIn(
                        "VBScript", run.stderr,
                        f"generated {vbs_path} failed to compile:\n{run.stderr}",
                    )
                    self.assertEqual(
                        7, run.returncode,
                        f"expected the fake runner's exit code (7) to propagate; "
                        f"got {run.returncode}, stderr={run.stderr}",
                    )


class InstallerHiddenLaunchWiringTest(unittest.TestCase):
    def test_every_scheduled_task_installer_uses_the_hidden_helper(self):
        for filename, wrapper_name in INSTALLERS:
            with self.subTest(installer=filename):
                content = (MANAGER_DIR / filename).read_text(encoding="utf-8")
                self.assertIn('. (Join-Path $PSScriptRoot "AdmHiddenLaunch.ps1")', content)
                self.assertIn(
                    f'New-AdmHiddenScheduledTaskAction -RepositoryPath $RepositoryPath -WrapperName "{wrapper_name}"',
                    content,
                )
                self.assertNotIn('New-ScheduledTaskAction -Execute "powershell.exe"', content)

    def test_session_center_supervisor_installer_can_pass_gcs_bucket(self):
        """run_session_center_supervisor.ps1 has always accepted -GcsBucket
        (needed for trusted-ingress command discovery), but the installer
        never exposed it -- a real functional regression risk when
        reinstalling live HOME tasks through this installer. Preserve it."""
        installer = (MANAGER_DIR / "install_session_center_supervisor.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$GcsBucket", installer)
        self.assertIn('$arguments += " -GcsBucket `"$GcsBucket`""', installer)

    def test_legacy_powershell_arguments_are_preserved(self):
        installer = (MANAGER_DIR / "install_command_watcher.ps1").read_text(encoding="utf-8")
        runner = (MANAGER_DIR / "run_command_watcher.ps1").read_text(encoding="utf-8")
        self.assertIn('-AllowlistPath `"$AllowlistPath`"', installer)
        self.assertIn('$env:ADM_WATCHER_ALLOWLIST_PATH = $AllowlistPath', runner)
        self.assertIn('-WindowStyle Hidden', installer)


if __name__ == "__main__":
    unittest.main()
