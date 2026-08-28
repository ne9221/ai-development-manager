import unittest
from unittest.mock import patch

from manager.open_existing_adm_ui import DASHBOARD_LAUNCHER, DASHBOARD_PORT, _spawn_dashboard, focus_existing_adm_ui


class FakeApi:
    def __init__(self, interactive=True, windows=(), focused=True, ports=()):
        self._interactive, self._windows, self._focused = interactive, windows, focused
        self._ports = list(ports)
        self.focused, self.spawned, self.opened, self.now = [], 0, 0, 0

    def interactive(self): return self._interactive
    def windows(self): return self._windows
    def focus(self, hwnd):
        self.focused.append(hwnd)
        return self._focused
    def port_open(self):
        return self._ports.pop(0) if len(self._ports) > 1 else (self._ports[0] if self._ports else False)
    def spawn_dashboard(self): self.spawned += 1
    def open_browser(self): self.opened += 1
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


class OpenExistingAdmUiTests(unittest.TestCase):
    def test_focuses_existing_dashboard_window(self):
        api = FakeApi(windows=((7, "ADM Unified Operations Dashboard - Google Chrome"),))
        self.assertEqual("completed", focus_existing_adm_ui(api)["status"])
        self.assertEqual([7], api.focused)

    def test_fails_closed_without_interactive_desktop(self):
        self.assertEqual("no_interactive_desktop", focus_existing_adm_ui(FakeApi(False))["error_kind"])

    def test_focuses_existing_window_with_the_real_observed_localized_title(self):
        """P0 regression, discovered via a real production E2E: the actual
        MainWindowTitle observed on this desktop is "ADM 營運儀表板 - Google
        Chrome" -- NOT dashboard.py's own English page_title
        ("ADM Unified Operations Dashboard"), almost certainly because this
        desktop's Chrome window is a pinned app-mode/PWA shortcut under a
        custom Traditional-Chinese name, independent of the page's own
        <title> tag. Without this marker, every invocation failed to
        recognize its own previously-opened window and opened an unbounded
        number of duplicate tabs on this exact machine instead of focusing
        the existing one -- true idempotency (invoking twice in a row opens
        only one tab total) depends on this."""
        api = FakeApi(windows=((7, "ADM 營運儀表板 - Google Chrome"),))
        self.assertEqual("completed", focus_existing_adm_ui(api)["status"])
        self.assertEqual([7], api.focused)

    def test_opens_browser_for_running_service_without_window(self):
        api = FakeApi(ports=(True,))
        self.assertEqual("completed", focus_existing_adm_ui(api)["status"])
        self.assertEqual(0, api.spawned)
        self.assertEqual(1, api.opened)

    def test_starts_service_but_never_opens_a_second_browser_tab(self):
        """P0 regression: desktop/Start-Dashboard.ps1 runs Streamlit with
        --server.headless false, which makes Streamlit's OWN startup
        sequence open a browser tab once it is ready. Calling
        api.open_browser() again here would open a second tab for a cold
        start, violating the "never more than one new browser window"
        contract -- this state must spawn and wait for the port, but never
        call open_browser() itself."""
        api = FakeApi(ports=(False, False, True))
        self.assertEqual("completed", focus_existing_adm_ui(api)["status"])
        self.assertEqual(1, api.spawned)
        self.assertEqual(0, api.opened)

    def test_fails_closed_when_service_never_becomes_reachable(self):
        api = FakeApi(ports=(False,))
        self.assertEqual("dashboard_start_timeout", focus_existing_adm_ui(api)["error_kind"])
        self.assertEqual(1, api.spawned)
        self.assertEqual(0, api.opened)

    def test_fails_closed_when_focus_is_denied(self):
        api = FakeApi(windows=((7, "ADM Unified Operations Dashboard"),), focused=False)
        self.assertEqual("dashboard_focus_denied", focus_existing_adm_ui(api)["error_kind"])


class SpawnDashboardArgsTests(unittest.TestCase):
    """P0 regression: the real _spawn_dashboard() PowerShell argument list is
    never exercised by FakeApi-based tests above (they inject a fake
    spawn_dashboard() entirely) -- a real invalid-flag typo here
    (-Non-Interactive instead of -NonInteractive) would make powershell.exe
    itself fail immediately, before ever reaching Start-Dashboard.ps1, and
    every FakeApi test would still report "completed" regardless, since
    they never build or run the real argument list at all. Verified live on
    this machine: `powershell.exe -Non-Interactive ...` errors with "term
    not recognized" and never runs the target script."""

    def test_spawn_uses_valid_powershell_flags_and_launcher_path(self):
        with patch("manager.open_existing_adm_ui.subprocess.Popen") as popen:
            _spawn_dashboard()
        args = popen.call_args.args[0]
        self.assertEqual("powershell.exe", args[0])
        # Every flag must be one PowerShell actually recognizes -- no
        # extra/misplaced hyphens.
        self.assertIn("-NoProfile", args)
        self.assertIn("-NonInteractive", args)
        self.assertNotIn("-Non-Interactive", args)
        self.assertIn("-ExecutionPolicy", args)
        self.assertIn("Bypass", args)
        self.assertIn("-File", args)
        self.assertIn(str(DASHBOARD_LAUNCHER), args)
        self.assertIn("-Port", args)
        self.assertIn(str(DASHBOARD_PORT), args)


if __name__ == "__main__": unittest.main()
