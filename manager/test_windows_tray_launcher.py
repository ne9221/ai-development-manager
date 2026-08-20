import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_DIR = REPO_ROOT / "desktop"


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell is required")
class WindowsTrayLauncherTests(unittest.TestCase):
    def test_tray_scripts_exist(self):
        expected_files = [
            "AdmCommon.ps1",
            "AdmTrayLauncher.ps1",
            "Start-ADM-Tray.vbs",
            "Install-AdmStartup.ps1",
            "Uninstall-AdmStartup.ps1",
            "Start-Dashboard.ps1",
            "README.md"
        ]
        for filename in expected_files:
            p = DESKTOP_DIR / filename
            self.assertTrue(p.exists(), f"Expected {filename} to exist in desktop/")

    def test_vbs_launcher_properties(self):
        vbs_path = DESKTOP_DIR / "Start-ADM-Tray.vbs"
        content = vbs_path.read_text(encoding="utf-8", errors="ignore")
        self.assertIn("AdmTrayLauncher.ps1", content)
        self.assertIn("-WindowStyle Hidden", content)
        self.assertIn("-ExecutionPolicy Bypass", content)

    def test_adm_comprehensive_health_function(self):
        script = f'''
$ErrorActionPreference = "Stop"
. "{DESKTOP_DIR / 'AdmCommon.ps1'}"
$h = Get-AdmComprehensiveHealth
[PSCustomObject]@{{
    DashboardStatus     = $h.DashboardStatus
    SessionCenterStatus = $h.SessionCenterStatus
    WatcherStatus       = $h.WatcherStatus
    SupervisorStatus    = $h.SupervisorStatus
}} | ConvertTo-Json -Compress
'''
        cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, res.returncode, f"PowerShell failed: {res.stderr}")
        import json
        data = json.loads(res.stdout.strip())
        self.assertIn(data["DashboardStatus"], ["running", "unavailable", "unknown"])
        self.assertIn(data["SessionCenterStatus"], ["running", "idle", "unavailable", "unknown"])
        self.assertIn(data["WatcherStatus"], ["running", "ready", "disabled", "missing", "unknown"])
        self.assertIn(data["SupervisorStatus"], ["running", "ready", "disabled", "missing", "unknown"])

    def test_start_adm_dashboard_background_function_when_already_running(self):
        script = f'''
$ErrorActionPreference = "Stop"
. "{DESKTOP_DIR / 'AdmCommon.ps1'}"
$started = Start-AdmDashboardBackground -RepositoryPath "{REPO_ROOT}"
[PSCustomObject]@{{ Started = $started }} | ConvertTo-Json -Compress
'''
        cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, res.returncode, f"PowerShell failed: {res.stderr}")
        import json
        data = json.loads(res.stdout.strip())
        self.assertTrue(data["Started"])

    def test_cold_start_calls_service_and_dashboard_confirmation(self):
        launcher_path = DESKTOP_DIR / "AdmTrayLauncher.ps1"
        content = launcher_path.read_text(encoding="utf-8", errors="ignore")
        self.assertIn("Start-AdmServicesSafe", content)
        self.assertIn("Start-AdmDashboardBackground", content)
        
        # Test Start-AdmServicesSafe execution idempotency
        script = f'''
$ErrorActionPreference = "Stop"
. "{DESKTOP_DIR / 'AdmCommon.ps1'}"
Confirm-AdmTaskEnabled -TaskName $AdmSupervisorTask
Confirm-AdmTaskEnabled -TaskName $AdmWatcherTask
Start-ScheduledTask -TaskName $AdmSupervisorTask -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $AdmWatcherTask -ErrorAction SilentlyContinue
Start-AdmDashboardBackground -RepositoryPath "{REPO_ROOT}" -ErrorAction SilentlyContinue
'''
        cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, res.returncode, f"Cold start service trigger failed: {res.stderr}")

    def test_single_instance_mutex_second_launch_exits_immediately(self):
        # Start a mock mutex holder in a background PowerShell process
        holder_script = '''
$mutex = New-Object System.Threading.Mutex($true, "Local\\ADM_Windows_Tray_Launcher_Mutex")
Start-Sleep -Seconds 5
$mutex.ReleaseMutex()
$mutex.Close()
'''
        holder = subprocess.Popen([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", holder_script])
        try:
            import time
            time.sleep(0.8)
            
            # Now run AdmTrayLauncher.ps1 - it should detect mutex, try to open dashboard, and exit immediately
            launcher_script = DESKTOP_DIR / "AdmTrayLauncher.ps1"
            cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(launcher_script)]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, res.returncode)
        finally:
            holder.terminate()
            holder.wait()

    def test_install_and_uninstall_shortcuts(self):
        # Run installer
        install_script = DESKTOP_DIR / "Install-AdmStartup.ps1"
        cmd_install = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(install_script)]
        res_install = subprocess.run(cmd_install, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, res_install.returncode, f"Installer failed: {res_install.stderr}")
        self.assertIn("installation complete", res_install.stdout)

        # Re-run installer (idempotency check)
        res_install_2 = subprocess.run(cmd_install, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, res_install_2.returncode, "Installer must be idempotent")

        # Verify shortcut files exist
        desktop_lnk = Path(os.environ["USERPROFILE"]) / "Desktop" / "AI Development Manager.lnk"
        self.assertTrue(desktop_lnk.exists(), "Desktop shortcut should exist after install")

        # Run uninstaller
        uninstall_script = DESKTOP_DIR / "Uninstall-AdmStartup.ps1"
        cmd_uninstall = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(uninstall_script)]
        res_uninstall = subprocess.run(cmd_uninstall, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, res_uninstall.returncode, f"Uninstaller failed: {res_uninstall.stderr}")
        self.assertFalse(desktop_lnk.exists(), "Desktop shortcut should be removed after uninstall")

        # Re-install at the end so machine retains the shortcut
        subprocess.run(cmd_install, capture_output=True, text=True, timeout=15)
        self.assertTrue(desktop_lnk.exists(), "Desktop shortcut should exist after final install")


if __name__ == "__main__":
    unittest.main()
