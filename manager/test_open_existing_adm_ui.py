import unittest

from manager.open_existing_adm_ui import focus_existing_adm_ui


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

    def test_opens_browser_for_running_service_without_window(self):
        api = FakeApi(ports=(True,))
        self.assertEqual("completed", focus_existing_adm_ui(api)["status"])
        self.assertEqual(0, api.spawned)
        self.assertEqual(1, api.opened)

    def test_starts_service_then_opens_browser(self):
        api = FakeApi(ports=(False, False, True))
        self.assertEqual("completed", focus_existing_adm_ui(api)["status"])
        self.assertEqual(1, api.spawned)
        self.assertEqual(1, api.opened)

    def test_fails_closed_when_service_never_becomes_reachable(self):
        api = FakeApi(ports=(False,))
        self.assertEqual("dashboard_start_timeout", focus_existing_adm_ui(api)["error_kind"])
        self.assertEqual(1, api.spawned)
        self.assertEqual(0, api.opened)

    def test_fails_closed_when_focus_is_denied(self):
        api = FakeApi(windows=((7, "ADM Unified Operations Dashboard"),), focused=False)
        self.assertEqual("dashboard_focus_denied", focus_existing_adm_ui(api)["error_kind"])


if __name__ == "__main__": unittest.main()
