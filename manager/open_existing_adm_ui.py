"""Focus or open the ADM Dashboard without creating duplicate services."""

import ctypes
import json
import os
import socket
import time
from pathlib import Path

from manager.detached_process import popen_detached


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
#
# "AI 開發管理器｜工作台" is dashboard.py's CURRENT st.set_page_config(page_title=
# ...) (the zh-TW workbench rename) and therefore the real browser window
# title today ("AI 開發管理器｜工作台 - Google Chrome", observed live
# 20260902 while the helper still reported dashboard_window_not_found
# against it). Every marker that ever matched a real window stays listed;
# test_title_markers_recognize_dashboard_py_current_page_title pins this
# tuple to dashboard.py's actual page_title so the two can never drift
# apart silently again.
TITLE_MARKERS = (
    "AI 開發管理器｜工作台",
    "ADM Unified Operations Dashboard",
    f"localhost:{DASHBOARD_PORT}",
    "ADM 營運儀表板",
)
# Cold start: desktop/Start-Dashboard.ps1 -> Streamlit binding DASHBOARD_PORT.
# Measured live 20260902 on this machine: 17 seconds from launch to a bound
# port (the previous 15s bound could never have succeeded). Bounded, never
# open-ended: a launcher that has not bound the port by then is reported as
# dashboard_start_timeout with the seconds actually waited.
DASHBOARD_PORT_TIMEOUT_SECONDS = 45.0
# Window: from "port bound" (or "browser open requested") to a real,
# focusable Dashboard-titled top-level window. Streamlit's own headless=false
# browser launch, plus Chrome start-up, sit inside this bound.
DASHBOARD_WINDOW_TIMEOUT_SECONDS = 30.0
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
    # Same detached + CREATE_BREAKAWAY_FROM_JOB contract (with the same
    # OSError fallback) as manager.command_watcher._spawn_claimed_worker,
    # via the single shared launcher. Without breakaway the Dashboard dies
    # with the Scheduled Task's job object as soon as the spawning tick
    # ends -- see manager.detached_process for the live 20260902 evidence.
    return popen_detached(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(DASHBOARD_LAUNCHER), "-Port", str(DASHBOARD_PORT)],
        close_fds=True,
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


def _find_matching_window(api):
    for hwnd, title in api.windows():
        if any(marker.casefold() in title.casefold() for marker in TITLE_MARKERS):
            return hwnd, title
    return None, None


def _wait_for_window_and_focus(api, timeout_seconds=DASHBOARD_WINDOW_TIMEOUT_SECONDS, stages=None):
    """Poll for a Dashboard-titled top-level window to appear and actively
    focus it via api.focus() (AttachThreadInput + SetForegroundWindow), the
    same real foreground-lock workaround used for an already-open window.

    P0 regression (2026-08-29 live E2E): a fresh os.startfile()/ShellExecute
    browser open -- whether a new tab in an already-running browser, or a
    brand-new browser process a cold Streamlit start triggers itself -- is
    NOT guaranteed to steal foreground focus from whatever app the user is
    currently using; Windows' own foreground-lock applies to ShellExecute-
    launched windows exactly like it does to SetForegroundWindow() calls.
    Confirmed live: after a real dispatch's on_running callback ran this
    function's OLD unconditional "opened -> completed" version, port 8501
    was genuinely listening but no Dashboard-titled window existed anywhere
    on the real desktop afterward -- "completed" was reported even though
    the user could never actually see it, exactly the failure mode
    AUTO_OPEN_ADM exists to prevent (a live backend is not the same thing
    as a visible window). This waits for the real window to actually
    appear, then runs it through the exact same focus() call an
    already-open window gets, and only reports "completed" once that
    succeeds -- never merely because the action of opening a tab/process was
    attempted."""
    stages = dict(stages or {})
    started = api.monotonic()
    deadline = started + timeout_seconds
    while api.monotonic() < deadline:
        hwnd, title = _find_matching_window(api)
        if hwnd is not None:
            stages["window_seconds"] = round(api.monotonic() - started, 1)
            if api.focus(hwnd):
                return {"status": "completed", "window_title": title, **stages}
            return {"status": "failed", "error_kind": "dashboard_focus_denied", "stage": "focus",
                    "window_title": title, **stages}
        api.sleep(POLL_INTERVAL_SECONDS)
    stages["window_wait_seconds"] = round(api.monotonic() - started, 1)
    return {"status": "failed", "error_kind": "dashboard_window_not_found", "stage": "window", **stages}


def focus_existing_adm_ui(api=None):
    """Focus, open, or start the Dashboard; each invocation opens at most once."""
    try:
        api = api or _User32()
        if not api.interactive():
            return {"status": "failed", "error_kind": "no_interactive_desktop"}
        hwnd, title = _find_matching_window(api)
        if hwnd is not None:
            if api.focus(hwnd):
                return {"status": "completed", "window_title": title, "reused_existing_window": True}
            return {"status": "failed", "error_kind": "dashboard_focus_denied", "stage": "focus",
                    "window_title": title}
        if api.port_open():
            api.open_browser()
            return _wait_for_window_and_focus(api, stages={"service": "already_running"})

        try:
            api.spawn_dashboard()
        except Exception:
            return {"status": "failed", "error_kind": "dashboard_start_failed", "stage": "spawn"}

        # desktop/Start-Dashboard.ps1 runs Streamlit with --server.headless
        # false, which makes Streamlit's own startup sequence open a browser
        # tab itself once it is ready -- calling api.open_browser() here too
        # would open a SECOND tab for a genuine cold start, violating the
        # "never more than one new browser window per invocation" contract.
        # Only the already-running-service branch above (state 2) needs an
        # explicit open, since nothing else is about to open one for it.
        #
        # Port and window are recorded as SEPARATE stages so a failure log
        # line says which one did not happen (port never bound vs. port
        # bound but no window) and how long each actually took.
        spawned_at = api.monotonic()
        deadline = spawned_at + DASHBOARD_PORT_TIMEOUT_SECONDS
        while api.monotonic() < deadline:
            if api.port_open():
                port_seconds = round(api.monotonic() - spawned_at, 1)
                return _wait_for_window_and_focus(
                    api, stages={"service": "cold_started", "port_seconds": port_seconds})
            api.sleep(POLL_INTERVAL_SECONDS)
        return {"status": "failed", "error_kind": "dashboard_start_timeout", "stage": "port",
                "port_wait_seconds": round(api.monotonic() - spawned_at, 1)}
    except Exception:
        return {"status": "failed", "error_kind": "desktop_unavailable"}


def main(argv=None):
    """Detached-helper entry point (see command_watcher._focus_adm_ui_best_
    effort): runs the full focus-or-launch-or-noop logic in its own process
    so the watcher's claim path never blocks on desktop work. Best-effort
    by contract -- the result is printed for observability and the exit
    code is always 0, because no auto-open outcome may ever look like a
    dispatch failure to anything supervising this process."""
    del argv
    try:
        result = focus_existing_adm_ui()
    except BaseException as exc:  # noqa: BLE001 -- outcome must always be printable
        # focus_existing_adm_ui() already converts its own failures to
        # result dicts; this catches anything above it (interpreter-level
        # surprises) so the spawner's log always records SOME structured
        # outcome instead of a bare traceback.
        result = {"status": "failed", "error_kind": "helper_unexpected_error", "detail": str(exc)[:200]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
