import os
import threading
import unittest
from unittest.mock import patch

from collectors.codex import AppServer, CollectorError, normalize


class NormalizeTest(unittest.TestCase):
    def test_null_and_multiple_windows(self):
        empty = normalize({"result": {"rateLimits": {"primary": None, "secondary": None}}})
        self.assertEqual(empty["providers"][0]["windows"], [])

        multiple = normalize({"result": {"rateLimits": {
            "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1786250000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1786300000},
        }}})
        self.assertEqual(len(multiple["providers"][0]["windows"]), 2)
        self.assertEqual(multiple["providers"][0]["windows"][0]["remaining_percent"], 80)
        self.assertEqual(multiple["providers"][0]["metadata"]["raw_resets_at"]["primary"], 1786250000)


class FakeProcess:
    """Minimal stand-in for subprocess.Popen's return value -- drives
    AppServer's real _read()/send()/request() logic against controlled
    stdout/stderr text. No real subprocess is ever spawned."""

    def __init__(self, stdout_lines=(), stderr_text="", returncode=None):
        self.stdin = _NullWriter()
        self.stdout = iter(list(stdout_lines))
        self.stderr = _TextReader(stderr_text)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class _NullWriter:
    def write(self, _text):
        pass

    def flush(self):
        pass


class _TextReader:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


class _BlockForever:
    """Simulates a process that is still running and has produced no
    output at all yet -- AppServer's reader thread blocks inside this
    iterator's __next__ forever (it is a daemon thread, harmless to leave
    blocked past test teardown), matching a genuine "no reply within
    timeout" condition rather than a closed-stdout/EOF one."""

    def __iter__(self):
        return self

    def __next__(self):
        threading.Event().wait()


def _make_app_server(stdout_lines=(), stderr_text="", returncode=None, timeout=5, popen_target=None):
    fake = FakeProcess(stdout_lines=stdout_lines, stderr_text=stderr_text, returncode=returncode)
    with patch("subprocess.Popen", return_value=fake) as mock_popen:
        server = AppServer(timeout)
    return server, fake, mock_popen


class AppServerCommandConstructionTests(unittest.TestCase):
    """Regression coverage for the Windows `.cmd` shim double-quoting bug:
    codex app-server was previously invoked via a hand-built
    `cmd.exe /d /s /c "..."` STRING passed as one argv element with
    shell=False, which Python's own list2cmdline() then re-quoted/escaped a
    second time, corrupting it (live-reproduced: 100% failure, "is not
    recognized as an internal or external command"). The fix never builds
    that wrapper string -- it passes the plain [executable, "app-server"]
    list and lets shell=True (Windows-only, .cmd/.bat-only) do the quoting
    exactly once, correctly."""

    def test_cmd_shim_uses_shell_true_with_a_plain_two_element_list(self):
        cmd_path = r"C:\Users\EE\AppData\Roaming\npm\codex.cmd"
        with patch("os.name", "nt"), \
             patch("shutil.which", side_effect=lambda name: cmd_path if name == "codex.cmd" else None), \
             patch("subprocess.Popen", return_value=FakeProcess()) as mock_popen:
            AppServer(timeout=5)
        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], [cmd_path, "app-server"])
        self.assertTrue(kwargs["shell"])
        # never a hand-built cmd.exe wrapper string anywhere in argv
        self.assertNotIn("cmd.exe", " ".join(args[0]).lower())

    def test_cmd_shim_path_containing_spaces_stays_a_clean_list(self):
        spaced_path = r"C:\Program Files\nodejs\codex.cmd"
        with patch("os.name", "nt"), \
             patch("shutil.which", side_effect=lambda name: spaced_path if name == "codex.cmd" else None), \
             patch("subprocess.Popen", return_value=FakeProcess()) as mock_popen:
            AppServer(timeout=5)
        args, kwargs = mock_popen.call_args
        # list2cmdline (invoked once, by Python itself, under shell=True)
        # is what safely quotes a spaced path -- this code must never try
        # to pre-quote it itself.
        self.assertEqual(args[0], [spaced_path, "app-server"])
        self.assertTrue(kwargs["shell"])

    def test_non_cmd_windows_executable_uses_shell_false(self):
        exe_path = r"C:\tools\codex.exe"
        with patch("os.name", "nt"), \
             patch("shutil.which", side_effect=lambda name: exe_path if name == "codex.exe" else None), \
             patch("subprocess.Popen", return_value=FakeProcess()) as mock_popen:
            AppServer(timeout=5)
        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], [exe_path, "app-server"])
        self.assertFalse(kwargs["shell"])

    def test_posix_path_never_uses_shell_true(self):
        with patch("os.name", "posix"), \
             patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch("subprocess.Popen", return_value=FakeProcess()) as mock_popen:
            AppServer(timeout=5)
        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], ["/usr/local/bin/codex", "app-server"])
        self.assertFalse(kwargs["shell"])

    def test_codex_bin_env_override_still_gates_shell_on_its_own_extension(self):
        with patch("os.name", "nt"), \
             patch.dict(os.environ, {"CODEX_BIN": r"D:\custom\codex.cmd"}), \
             patch("subprocess.Popen", return_value=FakeProcess()) as mock_popen:
            AppServer(timeout=5)
        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], [r"D:\custom\codex.cmd", "app-server"])
        self.assertTrue(kwargs["shell"])

    def test_no_executable_found_raises_collector_error(self):
        env_without_codex_bin = {k: v for k, v in os.environ.items() if k != "CODEX_BIN"}
        with patch.dict(os.environ, env_without_codex_bin, clear=True), \
             patch("shutil.which", return_value=None):
            with self.assertRaises(CollectorError):
                AppServer(timeout=5)


class AppServerProtocolTests(unittest.TestCase):
    """Not touched by this fix, but exercised here to prove the surrounding
    fail-closed JSON-RPC contract is unaffected by the shell/argv change."""

    def test_successful_request_parses_matching_id_response(self):
        server, _fake, _popen = _make_app_server(stdout_lines=['{"id":1,"result":{"ok":true}}\n'])
        reply = server.request(1, "initialize", {"a": 1})
        self.assertEqual(reply["result"], {"ok": True})

    def test_response_with_wrong_id_is_skipped_until_matching_one_arrives(self):
        server, _fake, _popen = _make_app_server(stdout_lines=[
            '{"id":99,"result":{"stale":true}}\n',
            '{"id":1,"result":{"ok":true}}\n',
        ])
        reply = server.request(1, "initialize")
        self.assertEqual(reply["result"], {"ok": True})

    def test_json_rpc_error_response_raises_collector_error(self):
        server, _fake, _popen = _make_app_server(stdout_lines=['{"id":1,"error":{"message":"bad request"}}\n'])
        with self.assertRaises(CollectorError):
            server.request(1, "initialize")

    def test_missing_result_key_raises_collector_error(self):
        server, _fake, _popen = _make_app_server(stdout_lines=['{"id":1}\n'])
        with self.assertRaises(CollectorError):
            server.request(1, "initialize")

    def test_process_already_exited_raises_collector_error(self):
        server, _fake, _popen = _make_app_server(stdout_lines=[], returncode=1, stderr_text="boom")
        with self.assertRaises(CollectorError):
            server.request(1, "initialize")

    def test_closed_stdout_with_no_matching_reply_raises_collector_error(self):
        server, _fake, _popen = _make_app_server(stdout_lines=[])
        with self.assertRaises(CollectorError):
            server.request(1, "initialize")

    def test_request_times_out_when_process_never_responds(self):
        fake = FakeProcess()
        fake.stdout = _BlockForever()
        with patch("subprocess.Popen", return_value=fake):
            server = AppServer(timeout=0.2)
        with self.assertRaises(CollectorError) as ctx:
            server.request(1, "initialize")
        self.assertIn("did not respond within", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
