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
"""

import unittest
from pathlib import Path

MANAGER_DIR = Path(__file__).parent

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
        self.assertIn("shell.Run \"\"powershell.exe $escapedArgs\"\", 0, True", self.helper)
        self.assertIn("WScript.Quit exitCode", self.helper)

    def test_double_quotes_are_escaped_for_vbs_string_literal(self):
        self.assertIn("$PowerShellArguments.Replace('\"', '\"\"')", self.helper)


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
