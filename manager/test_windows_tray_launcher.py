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

    def test_start_dashboard_streamlit_headless_true(self):
        # Streamlit must never pop its own browser tab -- the tray/app-window
        # layer (Open-AdmAppWindow) is the only allowed product entry point.
        content = (DESKTOP_DIR / "Start-Dashboard.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("--server.headless true", content)
        self.assertNotIn("--server.headless false", content)

    def test_tray_menu_localized_traditional_chinese(self):
        content = (DESKTOP_DIR / "AdmTrayLauncher.ps1").read_text(encoding="utf-8", errors="ignore")
        for label in ["開啟 ADM", "系統狀態", "啟動服務", "重新啟動服務", "結束 ADM"]:
            self.assertIn(label, content, f"Expected tray menu label {label!r} in AdmTrayLauncher.ps1")

    def test_no_raw_start_process_on_adm_urls_in_product_entry_scripts(self):
        # Normal path must route dashboard/session-center opens through
        # Open-AdmAppWindow (Edge app-window host), never a raw Start-Process
        # on the http:// URL -- that would show the user a browser address
        # bar / raw localhost URL.
        for filename in ["AdmTrayLauncher.ps1", "Start-ADM.ps1"]:
            content = (DESKTOP_DIR / filename).read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn('Start-Process "$AdmDashboardUrl', content, filename)
            self.assertNotIn('Start-Process "$AdmSessionCenterUrl', content, filename)

    def test_open_adm_app_window_and_find_edge_path_are_defined(self):
        script = f'''
$ErrorActionPreference = "Stop"
. "{DESKTOP_DIR / 'AdmCommon.ps1'}"
$cmd1 = Get-Command Open-AdmAppWindow -ErrorAction SilentlyContinue
$cmd2 = Get-Command Find-AdmEdgePath -ErrorAction SilentlyContinue
[PSCustomObject]@{{ HasOpenWindow = [bool]$cmd1; HasFindEdge = [bool]$cmd2 }} | ConvertTo-Json -Compress
'''
        cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, res.returncode, f"PowerShell failed: {res.stderr}")
        import json
        data = json.loads(res.stdout.strip())
        self.assertTrue(data["HasOpenWindow"])
        self.assertTrue(data["HasFindEdge"])

    def test_open_adm_app_window_truthful_fallback_when_edge_missing(self):
        # Simulate Edge not being installed: Open-AdmAppWindow must degrade
        # truthfully (BrowserFallback) rather than silently no-op or claim
        # an app window opened when it didn't.
        tmp_html = Path(tempfile.gettempdir()) / "adm-test-fallback-status.html"
        tmp_html.write_text("<html><body>adm test</body></html>", encoding="utf-8")
        try:
            script = f'''
$ErrorActionPreference = "Stop"
. "{DESKTOP_DIR / 'AdmCommon.ps1'}"
function Find-AdmEdgePath {{ return $null }}
$result = Open-AdmAppWindow -Url "{tmp_html}"
$result | ConvertTo-Json -Compress
'''
            cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, res.returncode, f"PowerShell failed: {res.stderr}")
            import json
            data = json.loads(res.stdout.strip())
            self.assertEqual("BrowserFallback", data["Mode"])
            self.assertTrue(data["Success"])
        finally:
            tmp_html.unlink(missing_ok=True)

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


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell is required")
class DashboardSelfHealTests(unittest.TestCase):
    """P1-G Global Self-Heal: Confirm-AdmDashboardAlive. Every case here
    overrides Get-AdmDashboardHealth / Start-AdmDashboardBackground /
    Get-AdmPortOwnerPid with in-process fakes defined *after* dot-sourcing
    AdmCommon.ps1 -- no real port, process, or subprocess restart is ever
    touched by these tests. Only the evidence-recording call at the end
    (via the real -PythonPath/sys.executable) is real, writing to a
    tempdir-scoped -ManagerHome, never the real machine's ADM home.
    """

    def _run(self, body, manager_home):
        script = f'''
$ErrorActionPreference = "Stop"
. "{DESKTOP_DIR / 'AdmCommon.ps1'}"
{body}
$result = Confirm-AdmDashboardAlive -RepositoryPath "{REPO_ROOT}" -ManagerHome "{manager_home}" -PythonPath "{sys.executable}"
$result | ConvertTo-Json -Compress
'''
        cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, res.returncode, f"PowerShell failed: {res.stderr}")
        import json
        return json.loads(res.stdout.strip().splitlines()[-1])

    def test_already_healthy_never_calls_restart_no_duplicate_process(self):
        with tempfile.TemporaryDirectory() as directory:
            body = '''
function Get-AdmDashboardHealth { param($Url = $AdmDashboardUrl) [PSCustomObject]@{ Listening = $true; Url = $Url } }
function Start-AdmDashboardBackground { param($RepositoryPath) throw "must never be called when already healthy" }
function Get-AdmPortOwnerPid { param($Port) 4242 }
'''
            result = self._run(body, directory)
            self.assertEqual("healthy", result["State"])
            self.assertFalse(result.get("DegradedReason"))  # PowerShell $null -> JSON null -> Python None

            from manager.health_evidence import evidence_store_path, read_component
            latest = read_component(evidence_store_path(directory), "dashboard")["latest"]
            self.assertEqual("healthy", latest["state"])
            self.assertEqual(4242, latest["observed_pid"])
            self.assertIsNone(latest["degraded_reason"])

    def test_missing_triggers_one_bounded_recovery_attempt_that_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            body = '''
$script:calls = 0
function Get-AdmDashboardHealth {
    param($Url = $AdmDashboardUrl)
    $script:calls++
    [PSCustomObject]@{ Listening = ($script:calls -gt 1); Url = $Url }
}
$script:startCalls = 0
function Start-AdmDashboardBackground { param($RepositoryPath) $script:startCalls++; return $true }
function Get-AdmPortOwnerPid { param($Port) 5555 }
'''
            result = self._run(body, directory)
            self.assertEqual("healthy", result["State"])
            self.assertEqual("recovered", result["RemediationResult"])

            from manager.health_evidence import evidence_store_path, read_component
            latest = read_component(evidence_store_path(directory), "dashboard")["latest"]
            self.assertEqual("healthy", latest["state"])
            self.assertEqual("recovered", latest["remediation_result"])

    def test_recovery_failure_reports_truthful_degraded_evidence_not_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            body = '''
function Get-AdmDashboardHealth { param($Url = $AdmDashboardUrl) [PSCustomObject]@{ Listening = $false; Url = $Url } }
$script:startCalls = 0
function Start-AdmDashboardBackground { param($RepositoryPath) $script:startCalls++; return $true }
function Get-AdmPortOwnerPid { param($Port) $null }
'''
            result = self._run(body, directory)
            # Process was started, but the post-check still shows not
            # listening -- must never be reported as healthy/usable just
            # because a start was attempted.
            self.assertEqual("degraded", result["State"])
            self.assertEqual("dashboard_process_missing", result["DegradedReason"])

            from manager.health_evidence import evidence_store_path, read_component
            latest = read_component(evidence_store_path(directory), "dashboard")["latest"]
            self.assertEqual("degraded", latest["state"])
            self.assertIsNotNone(latest["unresolved_blocker"])

    def test_protected_port_occupied_by_unrelated_process_fails_closed(self):
        # An unrelated process holds the port: Start-AdmDashboardBackground
        # is exactly as safe here as the "recovery failure" case above --
        # it never kills the occupant, it just cannot bind, and the health
        # check afterward truthfully still reports not-listening.
        with tempfile.TemporaryDirectory() as directory:
            body = '''
function Get-AdmDashboardHealth { param($Url = $AdmDashboardUrl) [PSCustomObject]@{ Listening = $false; Url = $Url } }
$script:killCalled = $false
function Start-AdmDashboardBackground { param($RepositoryPath) return $true }  # spawn attempted, fails to bind internally
function Get-AdmPortOwnerPid { param($Port) 9999 }  # the unrelated occupant's pid
function Stop-Process { param($Id) $script:killCalled = $true; throw "Confirm-AdmDashboardAlive must never kill anything" }
'''
            result = self._run(body, directory)
            self.assertEqual("degraded", result["State"])
            self.assertEqual("dashboard_process_missing", result["DegradedReason"])
            # The unrelated occupant's pid is recorded as evidence, never acted on.
            from manager.health_evidence import evidence_store_path, read_component
            latest = read_component(evidence_store_path(directory), "dashboard")["latest"]
            self.assertEqual(9999, latest["observed_pid"])

    def test_confirm_adm_dashboard_alive_never_references_kill_or_taskkill(self):
        content = (DESKTOP_DIR / "AdmCommon.ps1").read_text(encoding="utf-8", errors="ignore")
        start = content.index("function Confirm-AdmDashboardAlive")
        end = content.index("\nfunction ", start + 1)
        body = content[start:end]
        # Checks for actual kill mechanisms, not the word "kill" in
        # explanatory comments (this function's own docstring legitimately
        # says "never kills anything").
        self.assertNotIn("taskkill", body.lower())
        self.assertNotIn("stop-process", body.lower())

    def test_dashboard_poll_tick_is_wired_into_the_existing_tray_timer(self):
        # No new Scheduled Task, no second Python tray -- the existing 60s
        # poll timer is Dashboard's periodic supervisor.
        content = (DESKTOP_DIR / "AdmTrayLauncher.ps1").read_text(encoding="utf-8", errors="ignore")
        tick_start = content.index("$pollTimer.add_Tick(")
        tick_end = content.index("$pollTimer.Start()")
        tick_body = content[tick_start:tick_end]
        self.assertIn("Confirm-AdmDashboardAlive", tick_body)


if __name__ == "__main__":
    unittest.main()
