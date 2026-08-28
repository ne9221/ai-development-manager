"""Focus or open the ADM Dashboard without creating duplicate services."""

import ctypes
import os
import socket
import subprocess
import time
from pathlib import Path


DASHBOARD_PORT = 8501
DASHBOARD_URL = f"http://localhost:{DASHBOARD_PORT}/"
DASHBOARD_LAUNCHER = Path(__file__).resolve().parent.parent / "desktop" / "Start-Dashboard.ps1"
# "ADM 營運儀表板" is the real observed MainWindowTitle on this production
# desktop -- confirmed live: a Chrome window opened against the Dashboard's
# own dashboard.py st.set_page_config(page_title="ADM Unified Operations
# Dashboard") reports MainWindowTitle "ADM 營運儀表板 - Google Chrome", not
# the English page_title string at all (almost certainly a Chrome app-mode/
# PWA shortcut pinned under a custom Traditional-Chinese name by whoever set
# up this desktop, independent of the page's own <title> tag -- Windows
# reports the SHORTCUT's/app-window's own title, not the page's). Without
# this marker, every invocation on this machine failed to recognize its own
# previously-opened window and opened an unbounded number of duplicate tabs
# instead of focusing the existing one. Kept alongside the English markers
# (never replacing them) since a different desktop/locale may still show
# the literal page_title or URL instead.
TITLE_MARKERS = ("ADM Unified Operations Dashboard", f"localhost:{DASHBOARD_PORT}", "ADM 營運儀表板")
START_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.5


def _port_open(host="127.0.0.1", port=DASHBOARD_PORT):
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _open_browser(url=DASHBOARD_URL):
    if os.name == "nt":
        os.startfile(url)
    else:
        import webbrowser
        webbrowser.open(url)


def _spawn_dashboard():
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(DASHBOARD_LAUNCHER), "-Port", str(DASHBOARD_PORT)],
        creationflags=flags, close_fds=True,
    )


class _User32:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("interactive Windows desktop is unavailable")
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

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
        # SetForegroundWindow() alone is denied by Windows' foreground-lock
        # when called from a process that does not itself currently own
        # keyboard focus -- which is exactly this caller's situation, since
        # it runs from a hidden Scheduled Task process, never the
        # user's own foreground app. Confirmed live: this whole action
        # would otherwise return dashboard_focus_denied on every single
        # invocation in real production use, making "already open -> focus"
        # never actually work. AttachThreadInput() is the standard,
        # documented Win32 workaround (used by taskbars, alt-tab switchers,
        # and countless other legitimate foreground-window callers): briefly
        # share input state with whichever thread currently owns the
        # foreground so this process is treated as if it does too, for the
        # duration of the SetForegroundWindow() call only.
        self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        foreground_hwnd = self.user32.GetForegroundWindow()
        current_thread_id = self.kernel32.GetCurrentThreadId()
        foreground_thread_id = self.user32.GetWindowThreadProcessId(foreground_hwnd, None) if foreground_hwnd else 0
        attached = False
        if foreground_thread_id and foreground_thread_id != current_thread_id:
            attached = bool(self.user32.AttachThreadInput(current_thread_id, foreground_thread_id, True))
        try:
            return bool(self.user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                self.user32.AttachThreadInput(current_thread_id, foreground_thread_id, False)

    def port_open(self):
        return _port_open()

    def spawn_dashboard(self):
        return _spawn_dashboard()

    def open_browser(self):
        return _open_browser()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


def focus_existing_adm_ui(api=None):
    """Focus, open, or start the Dashboard; each invocation opens at most once."""
    try:
        api = api or _User32()
        if not api.interactive():
            return {"status": "failed", "error_kind": "no_interactive_desktop"}
        for hwnd, title in api.windows():
            if any(marker.casefold() in title.casefold() for marker in TITLE_MARKERS):
                if api.focus(hwnd):
                    return {"status": "completed", "window_title": title}
                return {"status": "failed", "error_kind": "dashboard_focus_denied"}
        if api.port_open():
            api.open_browser()
            return {"status": "completed", "window_title": "ADM Dashboard"}

        try:
            api.spawn_dashboard()
        except Exception:
            return {"status": "failed", "error_kind": "dashboard_start_failed"}

        # desktop/Start-Dashboard.ps1 runs Streamlit with --server.headless
        # false, which makes Streamlit's own startup sequence open a browser
        # tab itself once it is ready -- calling api.open_browser() here too
        # would open a SECOND tab for a genuine cold start, violating the
        # "never more than one new browser window per invocation" contract.
        # Only the already-running-service branch above (state 2) needs an
        # explicit open, since nothing else is about to open one for it.
        deadline = api.monotonic() + START_TIMEOUT_SECONDS
        while api.monotonic() < deadline:
            if api.port_open():
                return {"status": "completed", "window_title": "ADM Dashboard"}
            api.sleep(POLL_INTERVAL_SECONDS)
        return {"status": "failed", "error_kind": "dashboard_start_timeout"}
    except Exception:
        return {"status": "failed", "error_kind": "desktop_unavailable"}
