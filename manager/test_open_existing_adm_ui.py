import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from manager.open_existing_adm_ui import (
    DASHBOARD_LAUNCHER, DASHBOARD_PORT, DASHBOARD_PORT_TIMEOUT_SECONDS, DASHBOARD_WINDOW_TIMEOUT_SECONDS,
    TITLE_MARKERS, _find_matching_window, _spawn_dashboard, focus_existing_adm_ui,
)


class FakeApi:
    def __init__(self, interactive=True, windows=(), focused=True, ports=(), windows_after_open=None):
        self._interactive, self._windows, self._focused = interactive, windows, focused
        self._ports = list(ports)
        # Simulates a Dashboard-titled window only becoming enumerable
        # AFTER a real browser open/spawn actually happened -- real window
        # creation is asynchronous relative to ShellExecute/Popen returning.
        # None means "never appears", the exact live-discovered bug shape.
        self._windows_after_open = windows_after_open
        self.focused, self.spawned, self.opened, self.now = [], 0, 0, 0

    def interactive(self): return self._interactive
    def windows(self):
        if self._windows_after_open is not None and (self.opened or self.spawned):
            return self._windows_after_open
        return self._windows
    def focus(self, hwnd):
        self.focused.append(hwnd)
        return self._focused
    def port_open(self):
        return self._ports.pop(0) if len(self._ports) > 1 else (self._ports[0] if self._ports else False)
    def spawn_dashboard(self): self.spawned += 1
    def open_browser(self): self.opened += 1
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


class TimedApi(FakeApi):
    """FakeApi whose port and window become available at wall-clock offsets,
    so cold-start timing (measured live: ~17s to a bound port) can be
    modelled against the bounded stage timeouts exactly."""

    def __init__(self, port_after=None, window_after=None, window=(9, "AI 開發管理器｜工作台 - Google Chrome"), **kwargs):
        super().__init__(**kwargs)
        self.port_after, self.window_after, self.window = port_after, window_after, window
        self.spawned_at = None

    def spawn_dashboard(self):
        self.spawned += 1
        self.spawned_at = self.now

    def port_open(self):
        return self.port_after is not None and self.spawned_at is not None and self.now >= self.spawned_at + self.port_after

    def windows(self):
        if self.window_after is None or self.spawned_at is None:
            return ()
        return (self.window,) if self.now >= self.spawned_at + self.window_after else ()


class OpenExistingAdmUiTests(unittest.TestCase):
    def test_title_markers_recognize_dashboard_py_current_page_title(self):
        """Drift guard (live 20260902): dashboard.py had been renamed to
        page_title="AI 開發管理器｜工作台" while TITLE_MARKERS still only knew
        the old English title and an older zh-TW shortcut name, so the real
        window ("<page_title> - Google Chrome") was never recognized and
        never focused. This reads the ACTUAL page_title out of dashboard.py
        and proves the matcher recognizes the window title Chrome derives
        from it -- a future rename must update TITLE_MARKERS or fail here."""
        source = (Path(__file__).resolve().parent.parent / "dashboard.py").read_text(encoding="utf-8")
        match = re.search(r'page_title\s*=\s*"([^"]+)"', source)
        self.assertIsNotNone(match, "dashboard.py must declare st.set_page_config(page_title=...)")
        page_title = match.group(1)
        hwnd, title = _find_matching_window(FakeApi(windows=((7, f"{page_title} - Google Chrome"),)))
        self.assertEqual(7, hwnd, f"TITLE_MARKERS {TITLE_MARKERS!r} do not recognize {page_title!r}")
        self.assertIn(page_title, TITLE_MARKERS)

    def test_focuses_and_reuses_existing_window_with_current_zh_tw_title(self):
        api = FakeApi(windows=((7, "AI 開發管理器｜工作台 - Google Chrome"),))
        result = focus_existing_adm_ui(api)
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["reused_existing_window"])
        self.assertEqual([7], api.focused)
        self.assertEqual(0, api.spawned)
        self.assertEqual(0, api.opened)

    def test_cold_start_slower_than_old_15s_bound_succeeds_within_new_bound(self):
        # Live measurement 20260902: 17s from launch to a bound port. The
        # previous 15s bound could never succeed; the new port bound must,
        # while still being finite.
        api = TimedApi(port_after=17.0, window_after=21.0)
        result = focus_existing_adm_ui(api)
        self.assertEqual("completed", result["status"], result)
        self.assertEqual(1, api.spawned)
        self.assertEqual(0, api.opened)
        self.assertEqual([9], api.focused)
        # Stages recorded separately, for the log.
        self.assertEqual("cold_started", result["service"])
        self.assertGreaterEqual(result["port_seconds"], 17.0)
        self.assertLess(result["port_seconds"], 18.0)
        self.assertGreaterEqual(result["window_seconds"], 3.5)
        self.assertGreater(DASHBOARD_PORT_TIMEOUT_SECONDS, 17.0)

    def test_cold_start_port_wait_is_bounded_and_reports_the_stage(self):
        api = TimedApi(port_after=None)
        result = focus_existing_adm_ui(api)
        self.assertEqual("dashboard_start_timeout", result["error_kind"])
        self.assertEqual("port", result["stage"])
        self.assertAlmostEqual(DASHBOARD_PORT_TIMEOUT_SECONDS, result["port_wait_seconds"], delta=1.0)
        self.assertEqual(1, api.spawned)

    def test_window_wait_after_port_is_bounded_and_reports_the_stage(self):
        api = TimedApi(port_after=5.0, window_after=None)
        result = focus_existing_adm_ui(api)
        self.assertEqual("dashboard_window_not_found", result["error_kind"])
        self.assertEqual("window", result["stage"])
        self.assertEqual("cold_started", result["service"])
        self.assertAlmostEqual(5.0, result["port_seconds"], delta=0.6)
        self.assertAlmostEqual(DASHBOARD_WINDOW_TIMEOUT_SECONDS, result["window_wait_seconds"], delta=1.0)

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
        api = FakeApi(ports=(True,), windows_after_open=((9, "ADM Unified Operations Dashboard"),))
        result = focus_existing_adm_ui(api)
        self.assertEqual("completed", result["status"])
        self.assertEqual(0, api.spawned)
        self.assertEqual(1, api.opened)
        self.assertEqual([9], api.focused)

    def test_fails_closed_when_opened_tab_never_becomes_a_visible_window(self):
        """P0 regression, live-discovered 2026-08-29: a real natural
        production dispatch's AUTO_OPEN_ADM call reported "completed" (port
        8501 was genuinely listening, api.open_browser() was genuinely
        called) but no Dashboard-titled window ever actually existed on the
        real desktop afterward -- os.startfile()'s new tab was opened, but
        Windows' foreground-lock silently kept it out of view, and nothing
        ever confirmed the user could actually see it. "the backend is
        listening" must never again be reported as "completed" on its own;
        a real, focusable window must actually be found."""
        api = FakeApi(ports=(True,))
        result = focus_existing_adm_ui(api)
        self.assertEqual("dashboard_window_not_found", result["error_kind"])
        self.assertEqual(1, api.opened)
        self.assertEqual([], api.focused)

    def test_starts_service_but_never_opens_a_second_browser_tab(self):
        """P0 regression: desktop/Start-Dashboard.ps1 runs Streamlit with
        --server.headless false, which makes Streamlit's OWN startup
        sequence open a browser tab once it is ready. Calling
        api.open_browser() again here would open a second tab for a cold
        start, violating the "never more than one new browser window"
        contract -- this state must spawn and wait for the port, but never
        call open_browser() itself."""
        api = FakeApi(ports=(False, False, True), windows_after_open=((9, "ADM Unified Operations Dashboard"),))
        result = focus_existing_adm_ui(api)
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, api.spawned)
        self.assertEqual(0, api.opened)
        self.assertEqual([9], api.focused)

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

    @unittest.skipUnless(os.name == "nt", "Windows job-object semantics")
    def test_spawn_dashboard_breaks_away_from_the_scheduler_job(self):
        """Live 20260902: the Dashboard launched from inside the Command
        Watcher's Scheduled Task job never bound port 8501 and its process
        vanished (killed with the job when the tick ended); the identical
        launcher started outside the job bound in <=17s and stayed up. The
        spawn must therefore carry CREATE_BREAKAWAY_FROM_JOB -- through the
        exact same shared launcher _spawn_claimed_worker already proved --
        and keep the same OSError fallback."""
        with patch("manager.detached_process.subprocess.Popen", return_value=Mock(pid=1)) as popen:
            _spawn_dashboard()
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(flags & subprocess.DETACHED_PROCESS)
        from manager.command_watcher import _spawn_claimed_worker
        with patch("manager.detached_process.subprocess.Popen", return_value=Mock(pid=2)) as worker_popen:
            _spawn_claimed_worker({"project_id": "p", "task_id": "t", "execution_id": "e"})
        self.assertEqual(worker_popen.call_args.kwargs["creationflags"], flags,
                         "Dashboard and claimed-worker spawns must use one identical flag contract")

    def test_spawn_uses_valid_powershell_flags_and_launcher_path(self):
        with patch("manager.detached_process.subprocess.Popen") as popen:
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


class DetachedHelperMainTests(unittest.TestCase):
    def test_main_prints_result_and_always_exits_zero(self):
        # The detached-helper contract (command_watcher spawns
        # `python -m manager.open_existing_adm_ui` at claim time): the
        # outcome is printed for observability, and the exit code is 0 for
        # BOTH success and failure -- no auto-open outcome may ever look
        # like a dispatch failure to anything supervising this process.
        import io, json
        from contextlib import redirect_stdout
        from manager.open_existing_adm_ui import main
        for outcome in ({"status": "completed", "window_title": "ADM"},
                        {"status": "failed", "error_kind": "no_interactive_desktop"}):
            with self.subTest(outcome=outcome):
                buffer = io.StringIO()
                with patch("manager.open_existing_adm_ui.focus_existing_adm_ui", return_value=outcome), \
                     redirect_stdout(buffer):
                    exit_code = main([])
                self.assertEqual(0, exit_code)
                self.assertEqual(outcome, json.loads(buffer.getvalue()))

    def test_main_records_structured_outcome_even_on_unexpected_crash(self):
        # Observability contract with command_watcher's detached spawn: the
        # spawner pipes this process's stdout into the durable auto-open
        # log, so even an exception ABOVE focus_existing_adm_ui's own
        # error handling must still print a structured outcome (and exit
        # 0) rather than dying with only a traceback.
        import io, json
        from contextlib import redirect_stdout
        from manager.open_existing_adm_ui import main
        buffer = io.StringIO()
        with patch("manager.open_existing_adm_ui.focus_existing_adm_ui",
                   side_effect=KeyboardInterrupt("supervisor interrupt")), \
             redirect_stdout(buffer):
            exit_code = main([])
        self.assertEqual(0, exit_code)
        printed = json.loads(buffer.getvalue())
        self.assertEqual("failed", printed["status"])
        self.assertEqual("helper_unexpected_error", printed["error_kind"])


if __name__ == "__main__": unittest.main()
