"""Tests for the Antigravity facade and event normalizer."""

import unittest
from unittest.mock import MagicMock

from manager.ag_runner import (
    AgLaunchError,
    AgNormalizedEvent,
    AgRunner,
    LaunchOutcome,
    LaunchRequest,
    PreparedLaunch,
    RunningLaunch,
    normalize_event,
)


class TestAgEventNormalizer(unittest.TestCase):
    def test_normalize_init(self):
        ev = normalize_event({"type": "init", "session_id": "ag-12345"})
        self.assertEqual(ev.event_type, "init")
        self.assertEqual(ev.payload["session_id"], "ag-12345")

    def test_normalize_thought(self):
        ev = normalize_event({"type": "thought", "thought": "Analyzing repository..."})
        self.assertEqual(ev.event_type, "thought")
        self.assertEqual(ev.payload["thought"], "Analyzing repository...")

    def test_normalize_tool_call(self):
        ev = normalize_event({"type": "tool_call", "tool": "view_file", "args": {"path": "README.md"}})
        self.assertEqual(ev.event_type, "tool_call")
        self.assertEqual(ev.payload["tool"], "view_file")
        self.assertEqual(ev.payload["args"]["path"], "README.md")

    def test_normalize_tool_result(self):
        ev = normalize_event({"type": "tool_result", "output": "File content"})
        self.assertEqual(ev.event_type, "tool_result")
        self.assertEqual(ev.payload["output"], "File content")

    def test_normalize_message(self):
        ev = normalize_event({"type": "message", "role": "assistant", "content": "Here is the plan"})
        self.assertEqual(ev.event_type, "message")
        self.assertEqual(ev.payload["content"], "Here is the plan")

    def test_normalize_result(self):
        ev = normalize_event({"type": "result", "response": "Task complete", "stats": {"tokens": 120}})
        self.assertEqual(ev.event_type, "result")
        self.assertEqual(ev.payload["response"], "Task complete")
        self.assertEqual(ev.payload["stats"]["tokens"], 120)

    def test_normalize_structured_response(self):
        ev = normalize_event({"response": "Structured result", "stats": {"tokens": 50}})
        self.assertEqual(ev.event_type, "result")
        self.assertEqual(ev.payload["response"], "Structured result")
        self.assertEqual(ev.payload["stats"]["tokens"], 50)

    def test_normalize_structured_error(self):
        ev = normalize_event({"response": {}, "error": "RPC failed", "code": "ERR_RPC"})
        self.assertEqual(ev.event_type, "error")
        self.assertEqual(ev.payload["error"], "RPC failed")
        self.assertEqual(ev.payload["code"], "ERR_RPC")

    def test_normalize_error(self):
        ev = normalize_event({"type": "error", "error": "Fatal runtime failure", "code": "ERR_RUNTIME"})
        self.assertEqual(ev.event_type, "error")
        self.assertEqual(ev.payload["error"], "Fatal runtime failure")
        self.assertEqual(ev.payload["code"], "ERR_RUNTIME")

    def test_normalize_malformed(self):
        ev = normalize_event("non-dict")
        self.assertEqual(ev.event_type, "error")


class TestAgRunnerRouter(unittest.TestCase):
    def setUp(self):
        self.mock_bridge = MagicMock()
        self.mock_headless = MagicMock()
        self.mock_cli = MagicMock()
        self.runner = AgRunner(
            ide_bridge=self.mock_bridge,
            headless_runner=self.mock_headless,
            cli_runner=self.mock_cli,
        )

    def test_forced_cli_mode(self):
        req = LaunchRequest(working_directory="/test", force_mode="cli")
        self.mock_cli.prepare.return_value = PreparedLaunch(
            thread_id="ag-c1", session_path=None, pid=150, process_creation_identity="c1",
            prepared_at="2026-08-17T00:00:00Z", mode="cli", _target=None, _request=req,
        )
        prep = self.runner.prepare(req)
        self.assertEqual(prep.mode, "cli")
        self.mock_cli.prepare.assert_called_once_with(req)
        self.mock_bridge.prepare.assert_not_called()

    def test_forced_headless_mode(self):
        req = LaunchRequest(working_directory="/test", force_mode="headless")
        self.mock_headless.prepare.return_value = PreparedLaunch(
            thread_id="ag-h1", session_path=None, pid=100, process_creation_identity="h1",
            prepared_at="2026-08-17T00:00:00Z", mode="headless", _target=None, _request=req,
        )
        prep = self.runner.prepare(req)
        self.assertEqual(prep.mode, "headless")
        self.mock_headless.prepare.assert_called_once_with(req)
        self.mock_bridge.prepare.assert_not_called()

    def test_forced_live_ide_mode_when_alive(self):
        self.mock_bridge.is_alive.return_value = True
        req = LaunchRequest(working_directory="/test", force_mode="live_ide")
        self.mock_bridge.prepare.return_value = PreparedLaunch(
            thread_id="ag-l1", session_path=None, pid=200, process_creation_identity="l1",
            prepared_at="2026-08-17T00:00:00Z", mode="live_ide", _target=None, _request=req,
        )
        prep = self.runner.prepare(req)
        self.assertEqual(prep.mode, "live_ide")
        self.mock_bridge.prepare.assert_called_once_with(req)

    def test_forced_live_ide_mode_when_not_alive_raises(self):
        self.mock_bridge.is_alive.return_value = False
        req = LaunchRequest(working_directory="/test", force_mode="live_ide")
        with self.assertRaises(AgLaunchError) as ctx:
            self.runner.prepare(req)
        self.assertEqual(ctx.exception.classification, "live_ide_not_found")

    def test_forced_live_ide_mode_when_transport_unavailable_raises(self):
        self.mock_bridge.is_alive.return_value = True
        self.mock_bridge.prepare.side_effect = AgLaunchError("live_ide_transport_unavailable", "no IPC transport")
        req = LaunchRequest(working_directory="/test", force_mode="live_ide")
        with self.assertRaises(AgLaunchError) as ctx:
            self.runner.prepare(req)
        self.assertEqual(ctx.exception.classification, "live_ide_transport_unavailable")

    def test_hybrid_auto_selects_live_ide_when_transport_ready(self):
        self.mock_bridge.is_alive.return_value = True
        req = LaunchRequest(working_directory="/test")
        self.mock_bridge.prepare.return_value = PreparedLaunch(
            thread_id="ag-l2", session_path=None, pid=300, process_creation_identity="l2",
            prepared_at="2026-08-17T00:00:00Z", mode="live_ide", _target=None, _request=req,
        )
        prep = self.runner.prepare(req)
        self.assertEqual(prep.mode, "live_ide")
        self.mock_bridge.prepare.assert_called_once_with(req)
        self.mock_cli.prepare.assert_not_called()

    def test_hybrid_auto_fallbacks_to_cli_when_ide_offline(self):
        self.mock_bridge.is_alive.return_value = False
        req = LaunchRequest(working_directory="/test")
        self.mock_cli.prepare.return_value = PreparedLaunch(
            thread_id="ag-c2", session_path=None, pid=400, process_creation_identity="c2",
            prepared_at="2026-08-17T00:00:00Z", mode="cli", _target=None, _request=req,
        )
        prep = self.runner.prepare(req)
        self.assertEqual(prep.mode, "cli")
        self.assertEqual(self.runner.last_fallback_reason, "live_ide_not_found")
        self.mock_cli.prepare.assert_called_once_with(req)

    def test_hybrid_auto_fallbacks_to_headless_when_cli_not_provided_and_transport_unavailable(self):
        runner = AgRunner(ide_bridge=self.mock_bridge, headless_runner=self.mock_headless)
        self.mock_bridge.is_alive.return_value = True
        self.mock_bridge.prepare.side_effect = AgLaunchError("live_ide_transport_unavailable", "no IPC transport")
        req = LaunchRequest(working_directory="/test")
        self.mock_headless.prepare.return_value = PreparedLaunch(
            thread_id="ag-h3", session_path=None, pid=401, process_creation_identity="h3",
            prepared_at="2026-08-17T00:00:00Z", mode="headless", _target=None, _request=req,
        )
        prep = runner.prepare(req)
        self.assertEqual(prep.mode, "headless")
        self.assertEqual(runner.last_fallback_reason, "live_ide_transport_unavailable")
        self.mock_headless.prepare.assert_called_once_with(req)

    def test_hybrid_auto_does_not_swallow_unexpected_critical_errors(self):
        self.mock_bridge.is_alive.return_value = True
        self.mock_bridge.prepare.side_effect = AgLaunchError("security_denied", "Unauthorized token")
        req = LaunchRequest(working_directory="/test")
        with self.assertRaises(AgLaunchError) as ctx:
            self.runner.prepare(req)
        self.assertEqual(ctx.exception.classification, "security_denied")
        self.mock_cli.prepare.assert_not_called()

    def test_start_and_wait_delegates_by_mode(self):
        req = LaunchRequest(working_directory="/test")
        live_prep = PreparedLaunch(
            thread_id="ag-l3", session_path=None, pid=500, process_creation_identity="l3",
            prepared_at="2026-08-17T00:00:00Z", mode="live_ide", _target=None, _request=req,
        )
        self.mock_bridge.start.return_value = RunningLaunch(prepared=live_prep, turn_id="t1", started_at="now")
        running = self.runner.start(live_prep, "test prompt")
        self.mock_bridge.start.assert_called_once_with(live_prep, "test prompt")

        self.mock_bridge.wait.return_value = LaunchOutcome(
            status="completed", thread_id="ag-l3", turn_id="t1", completed_at="now", response_text="done"
        )
        outcome = self.runner.wait(running)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.response_text, "done")

    def test_start_and_wait_delegates_cli_mode(self):
        req = LaunchRequest(working_directory="/test")
        cli_prep = PreparedLaunch(
            thread_id="ag-c3", session_path=None, pid=600, process_creation_identity="c3",
            prepared_at="2026-08-17T00:00:00Z", mode="cli", _target=None, _request=req,
        )
        self.mock_cli.start.return_value = RunningLaunch(prepared=cli_prep, turn_id="t2", started_at="now")
        running = self.runner.start(cli_prep, "test cli prompt")
        self.mock_cli.start.assert_called_once_with(cli_prep, "test cli prompt")

        self.mock_cli.wait.return_value = LaunchOutcome(
            status="completed", thread_id="ag-c3", turn_id="t2", completed_at="now", response_text="cli done"
        )
        outcome = self.runner.wait(running)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.response_text, "cli done")


if __name__ == "__main__":
    unittest.main()
