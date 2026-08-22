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
        self.assertIn('-ClaudeAccountsConfig `"$ClaudeAccountsConfig`"', installer)
        self.assertIn('$env:CLAUDE_ACCOUNTS_CONFIG = $ClaudeAccountsConfig', runner)
        self.assertIn('-WindowStyle Hidden', installer)


if __name__ == "__main__":
    unittest.main()

@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell required")
class CommandWatcherRunnerClaudeAccountsConfigTest(unittest.TestCase):
    """Tests that run_command_watcher.ps1 correctly resolves and validates
    CLAUDE_ACCOUNTS_CONFIG before invoking command_watcher."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.manager_home = self.tmp_path / ".ai-development-manager"
        self.manager_home.mkdir(parents=True, exist_ok=True)
        self.config_dir = self.manager_home / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.default_config = self.config_dir / "claude_accounts.json"
        self.default_config.write_text('{"accounts":[{"account_id":"account-b","enabled":true,"config_dir":"C:\\\\fake"}]}', encoding="utf-8")

        # Fake repo with dummy provenance output so provenance check passes
        self.fake_repo = self.tmp_path / "repo"
        (self.fake_repo / "manager").mkdir(parents=True, exist_ok=True)
        shutil.copy(MANAGER_DIR / "run_command_watcher.ps1", self.fake_repo / "manager" / "run_command_watcher.ps1")
        
        self.fake_python = self.tmp_path / "fake_python.bat"
        # Script that prints env:CLAUDE_ACCOUNTS_CONFIG when called with -m manager.command_watcher
        bat_content = (
            '@echo off\n'
            'if "%1"=="-m" if "%2"=="-c" ( echo {"running_sha":"sha1","tested_sha":"sha1","activated_sha":"sha1"} & exit /b 0 )\n'
            'if "%1"=="-m" if "%2"=="manager.provenance" ( echo {"running_sha":"sha1","tested_sha":"sha1","activated_sha":"sha1"} & exit /b 0 )\n'
            'if "%1"=="-m" if "%2"=="manager.command_watcher" ( echo CLAUDE_CONFIG=%CLAUDE_ACCOUNTS_CONFIG% & exit /b 0 )\n'
            'exit /b 0\n'
        )
        self.fake_python.write_text(bat_content, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_watcher(self, extra_args=""):
        runner = self.fake_repo / "manager" / "run_command_watcher.ps1"
        allowlist = self.tmp_path / "allowlist.json"
        allowlist.write_text("{}", encoding="utf-8")
        cmd = f'& "{runner}" -PythonPath "{self.fake_python}" -RepositoryPath "{self.fake_repo}" -ManagerHome "{self.manager_home}" -AllowlistPath "{allowlist}" -GcsBucket "b" -GcsObject "o" {extra_args}'
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=20,
        )

    def test_default_config_path_exported_when_present(self):
        res = self._run_watcher()
        self.assertEqual(0, res.returncode, f"stderr: {res.stderr}")
        self.assertIn(f"CLAUDE_CONFIG={self.default_config}", res.stdout)

    def test_explicit_config_path_exported(self):
        custom_config = self.tmp_path / "custom_accounts.json"
        custom_config.write_text('{"accounts":[]}', encoding="utf-8")
        res = self._run_watcher(f'-ClaudeAccountsConfig "{custom_config}"')
        self.assertEqual(0, res.returncode, f"stderr: {res.stderr}")
        self.assertIn(f"CLAUDE_CONFIG={custom_config}", res.stdout)

    def test_missing_config_fails_closed(self):
        self.default_config.unlink()
        res = self._run_watcher()
        self.assertNotEqual(0, res.returncode)
        self.assertIn("CLAUDE_ACCOUNTS_CONFIG_MISSING", res.stderr)

    def test_unicode_and_spaces_in_config_path(self):
        unicode_dir = self.tmp_path / "test_dir_with_spaces"
        unicode_dir.mkdir(parents=True, exist_ok=True)
        unicode_config = unicode_dir / "claude_accounts.json"
        unicode_config.write_text('{"accounts":[]}', encoding="utf-8")
        res = self._run_watcher(f'-ClaudeAccountsConfig "{unicode_config}"')
        self.assertEqual(0, res.returncode, f"stderr: {res.stderr}")
        self.assertIn(f"CLAUDE_CONFIG={unicode_config}", res.stdout)

    def test_runner_leaves_active_provider_lifecycle_to_the_125_minute_task_limit(self):
        runner = (MANAGER_DIR / "run_command_watcher.ps1").read_text(encoding="utf-8")
        self.assertIn('& $PythonPath -m manager.command_watcher --once', runner)
        self.assertNotIn('Start-Process -FilePath $PythonPath', runner)
        self.assertNotIn('WatcherTickTimeoutSeconds', runner)
