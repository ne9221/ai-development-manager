"""Focus an already-running ADM Dashboard window; never launch one."""

import ctypes
import os


TITLE_MARKERS = ("ADM Unified Operations Dashboard", "localhost:8501")


class _User32:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("interactive Windows desktop is unavailable")
        self.user32 = ctypes.windll.user32

    def interactive(self):
        # A desktop handle proves this process has an interactive input desktop.
        handle = self.user32.OpenInputDesktop(0, False, 0x0100)
        if not handle:
            return False
        self.user32.CloseDesktop(handle)
        return True

    def windows(self):
        found = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        def callback(hwnd, _lparam):
            if self.user32.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(512)
                self.user32.GetWindowTextW(hwnd, title, len(title))
                found.append((hwnd, title.value))
            return True

        self.user32.EnumWindows(callback_type(callback), 0)
        return found

    def focus(self, hwnd):
        self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(self.user32.SetForegroundWindow(hwnd))


def focus_existing_adm_ui(api=None):
    """Return a bounded result suitable for a command result record."""
    try:
        api = api or _User32()
        if not api.interactive():
            return {"status": "failed", "error_kind": "no_interactive_desktop"}
        for hwnd, title in api.windows():
            if any(marker.casefold() in title.casefold() for marker in TITLE_MARKERS):
                if api.focus(hwnd):
                    return {"status": "completed", "window_title": title}
                return {"status": "failed", "error_kind": "dashboard_focus_denied"}
        return {"status": "failed", "error_kind": "adm_dashboard_not_running"}
    except Exception:
        return {"status": "failed", "error_kind": "desktop_unavailable"}
