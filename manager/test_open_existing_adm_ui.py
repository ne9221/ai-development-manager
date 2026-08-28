import unittest

from manager.open_existing_adm_ui import focus_existing_adm_ui


class FakeApi:
    def __init__(self, interactive=True, windows=(), focused=True):
        self._interactive, self._windows, self._focused = interactive, windows, focused
        self.focused = []

    def interactive(self): return self._interactive
    def windows(self): return self._windows
    def focus(self, hwnd):
        self.focused.append(hwnd)
        return self._focused


class OpenExistingAdmUiTests(unittest.TestCase):
    def test_focuses_existing_dashboard_window(self):
        api = FakeApi(windows=((7, "ADM Unified Operations Dashboard - Google Chrome"),))
        self.assertEqual("completed", focus_existing_adm_ui(api)["status"])
        self.assertEqual([7], api.focused)

    def test_fails_closed_without_interactive_desktop(self):
        self.assertEqual("no_interactive_desktop", focus_existing_adm_ui(FakeApi(False))["error_kind"])

    def test_fails_closed_when_dashboard_is_not_running(self):
        self.assertEqual("adm_dashboard_not_running", focus_existing_adm_ui(FakeApi(windows=((7, "Not ADM"),)))["error_kind"])

    def test_fails_closed_when_focus_is_denied(self):
        api = FakeApi(windows=((7, "ADM Unified Operations Dashboard"),), focused=False)
        self.assertEqual("dashboard_focus_denied", focus_existing_adm_ui(api)["error_kind"])


if __name__ == "__main__": unittest.main()
