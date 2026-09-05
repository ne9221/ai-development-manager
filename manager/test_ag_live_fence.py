"""The live Antigravity fence must hold for EVERY test runner, not only pytest.

Independent review of f4cf5cb: the root ``conftest.py`` fence is pytest-only.
Under ``python -m unittest`` an ordinary unit test that built
``AgRunner(cli_runner=...)`` without a dead IDE bridge discovered the live
cascade host, ran the real READY handshake and reached ``AddTrackedWorkspace``
-- one RPC short of a real model turn. These tests pin the runner-independent
fence in ``manager/ag_live_fence.py``:

* the four real-world entry points refuse BEFORE any OS call (no PowerShell,
  no netstat, no tasklist, no loopback socket) whenever a test framework is
  loaded, unless the process explicitly opted in;
* a fresh ``python -m unittest``-style interpreter (no conftest) is fenced;
* a fresh interpreter with NO test framework (the production processes) is
  NOT fenced -- the fence never fires in the Command Watcher, refresh_status,
  the collectors or the Layer-4 live smoke;
* the exact regression: a bare ``AgRunner`` fallback path cannot reach the IDE.

Every deliberate probe below runs inside ``expecting_refusal()`` because the
pytest fixture fails any test that reaches a fenced entry point by accident.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager import ag_ide_bridge, ag_language_server, ag_live_fence
from manager.ag_language_server import AgLsError
from manager.ag_runner import AgRunner, LaunchRequest
from manager.test_ag_execution import MockCliRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ENTRY_POINT_MODULES = ("manager.command_watcher", "manager.refresh_status", "collectors.antigravity",
                                  "manager.ag_live_smoke", "manager.ag_language_server", "manager.ag_cli_runner")


def _boom(*_args, **_kwargs):
    raise AssertionError("a fenced entry point reached the OS boundary")


def _run_fresh_interpreter(code, *, env_extra=None):
    env = {**os.environ, "AI_MANAGER_HOME": tempfile.mkdtemp(prefix="adm-fence-probe-home-")}
    env.pop(ag_live_fence.LIVE_OPT_IN_ENV, None)
    env.update(env_extra or {})
    completed = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()[-800:]


class FenceStateTests(unittest.TestCase):
    def test_fence_is_armed_in_this_test_process(self):
        self.assertTrue(ag_live_fence.test_framework_loaded())
        self.assertTrue(ag_live_fence.is_armed())

    def test_check_records_the_attempt_and_fails_like_an_absent_ide(self):
        with ag_live_fence.expecting_refusal():
            before = len(ag_live_fence.expected_attempts)
            with self.assertRaises(AgLsError) as ctx:
                ag_live_fence.check("probe.entry_point")
            recorded = ag_live_fence.expected_attempts[before:]
        self.assertEqual("ide_not_running", ctx.exception.classification)
        self.assertIn("fenced off", ctx.exception.detail)
        self.assertIn("probe.entry_point", ctx.exception.detail)
        self.assertEqual(["probe.entry_point"], [item["entry_point"] for item in recorded])
        self.assertIn("test_ag_live_fence", recorded[0]["stack"])

    def test_suspended_block_allows_live_access(self):
        with ag_live_fence.suspended():
            self.assertFalse(ag_live_fence.is_armed())
            ag_live_fence.check("probe.entry_point")  # must not raise
        self.assertTrue(ag_live_fence.is_armed())

    def test_explicit_environment_opt_in_disarms(self):
        with patch.dict(os.environ, {ag_live_fence.LIVE_OPT_IN_ENV: "1"}):
            self.assertFalse(ag_live_fence.is_armed())
            ag_live_fence.check("probe.entry_point")  # must not raise; no real access is attempted here
        with patch.dict(os.environ, {ag_live_fence.LIVE_OPT_IN_ENV: "yes"}):
            self.assertTrue(ag_live_fence.is_armed(), "only the exact value 1 opts in")


class EntryPointTests(unittest.TestCase):
    """Each real-world entry point refuses before touching the OS."""

    def test_all_four_entry_points_refuse_before_any_os_call(self):
        with patch.object(subprocess, "run", _boom), patch.object(subprocess, "Popen", _boom), \
             patch.object(subprocess, "check_output", _boom), \
             patch.object(ag_language_server.urllib.request, "urlopen", _boom), \
             ag_live_fence.expecting_refusal():
            before = len(ag_live_fence.expected_attempts)
            with self.assertRaises(AgLsError) as processes:
                ag_language_server.list_language_server_processes()
            with self.assertRaises(AgLsError) as ports:
                ag_language_server.list_listening_ports(4242)
            with self.assertRaises(AgLsError) as opener:
                ag_language_server._default_opener("http://127.0.0.1:1/x", b"{}", {}, 1.0)
            self.assertEqual([], ag_ide_bridge._detect_live_processes())
            recorded = [item["entry_point"] for item in ag_live_fence.expected_attempts[before:]]
        for ctx in (processes, ports, opener):
            self.assertEqual("ide_not_running", ctx.exception.classification)
        self.assertEqual(["ag_language_server.list_language_server_processes", "ag_language_server.list_listening_ports",
                          "ag_language_server._default_opener", "ag_ide_bridge._detect_live_processes"], recorded)

    def test_discovery_and_snapshot_degrade_on_the_ide_not_running_path(self):
        with patch.object(subprocess, "run", _boom), ag_live_fence.expecting_refusal():
            with self.assertRaises(AgLsError) as ctx:
                ag_language_server.discover_language_server()
            snapshot = ag_language_server.availability_snapshot(now="2026-09-05T00:00:00Z")
        self.assertEqual("ide_not_running", ctx.exception.classification)
        self.assertEqual(("unavailable", "ide_not_running", False),
                         (snapshot["status"], snapshot["reason"], snapshot["can_accept_new_task"]))

    def test_bare_agrunner_fallback_cannot_reach_the_ide(self):
        """The exact f4cf5cb hole: AgRunner(cli_runner=...) without a dead bridge auto-discovers the IDE."""
        cli_runner = MockCliRunner()
        # Built through __init__ on purpose: a literal AgRunner(cli_runner=...) is exactly what
        # test_ag_runner_construction_guard forbids, and this test proves the runtime fence
        # still holds when that static guard is bypassed.
        runner = AgRunner.__new__(AgRunner)
        AgRunner.__init__(runner, cli_runner=cli_runner)
        with tempfile.TemporaryDirectory() as workspace, patch.object(subprocess, "run", _boom), \
             patch.object(subprocess, "Popen", _boom), ag_live_fence.expecting_refusal():
            before = len(ag_live_fence.expected_attempts)
            prepared = runner.prepare(LaunchRequest(str(Path(workspace).resolve()), project_id="p1"))
            recorded = [item["entry_point"] for item in ag_live_fence.expected_attempts[before:]]
        self.assertEqual("cli", prepared.mode)
        self.assertEqual("live_ide_not_found", runner.last_fallback_reason)
        self.assertEqual(["prepare"], cli_runner.events)
        self.assertEqual(["ag_language_server.list_language_server_processes"], recorded)


class RunnerIndependenceTests(unittest.TestCase):
    """Fresh interpreters: no conftest, no pytest -- the fence still decides correctly."""

    FENCED_PROBE = (
        "import subprocess, unittest\n"
        "from manager import ag_language_server as ls, ag_live_fence as fence\n"
        "def boom(*a, **k): raise AssertionError('reached the OS boundary')\n"
        "subprocess.run = boom; subprocess.Popen = boom\n"
        "try:\n"
        "    ls.list_language_server_processes()\n"
        "except ls.AgLsError as exc:\n"
        "    print('refused', exc.classification, len(fence.attempts), fence.test_framework_loaded())\n"
    )
    PRODUCTION_PROBE = (
        "import subprocess, sys\n"
        "from manager import ag_language_server as ls, ag_live_fence as fence\n"
        "ls._hidden_run = lambda argv, timeout: subprocess.CompletedProcess(argv, 0, stdout='[]', stderr='')\n"
        "print('enumerated', ls.list_language_server_processes(), len(fence.attempts), fence.is_armed(),\n"
        "      sorted(name for name in sys.modules if name.split('.')[0] in fence.TEST_FRAMEWORK_MODULES))\n"
    )

    def test_unittest_style_process_without_conftest_is_fenced(self):
        code, out, err = _run_fresh_interpreter(self.FENCED_PROBE)
        self.assertEqual((0, "refused ide_not_running 1 True"), (code, out), err)

    def test_environment_opt_in_is_the_only_way_a_unittest_process_reaches_enumeration(self):
        # Same probe, opted in: the real enumeration path is taken (and fails
        # on the patched boundary, which is the proof it was reached).
        code, out, err = _run_fresh_interpreter(self.FENCED_PROBE, env_extra={ag_live_fence.LIVE_OPT_IN_ENV: "1"})
        self.assertNotEqual(0, code)
        self.assertIn("reached the OS boundary", err)
        self.assertEqual("", out)

    def test_process_without_a_test_framework_is_not_fenced(self):
        code, out, err = _run_fresh_interpreter(self.PRODUCTION_PROBE)
        self.assertEqual((0, "enumerated [] 0 False []"), (code, out), err)

    def test_production_entry_points_load_no_test_framework(self):
        """Regression guard for the fence heuristic: a production import that pulled in ``unittest``
        would silently fence Antigravity off in the Command Watcher / collectors."""
        code = ("import importlib, sys\n"
                f"for name in {PRODUCTION_ENTRY_POINT_MODULES!r}: importlib.import_module(name)\n"
                "from manager import ag_live_fence as fence\n"
                "print(fence.test_framework_loaded(), fence.is_armed(),\n"
                "      sorted(name for name in sys.modules if name.split('.')[0] in fence.TEST_FRAMEWORK_MODULES))\n")
        result, out, err = _run_fresh_interpreter(code)
        self.assertEqual((0, "False False []"), (result, out), err)


if __name__ == "__main__":
    unittest.main()
