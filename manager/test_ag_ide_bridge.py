"""Tests for the Live Antigravity IDE Bridge."""

import queue
import time
import unittest
from unittest.mock import MagicMock, patch

from manager.ag_ide_bridge import AgIdeBridge, AgIdeClient
from manager.ag_runner import AgLaunchError, AgNormalizedEvent, LaunchRequest


class MockAgClient(AgIdeClient):
    def __init__(self, endpoint=None, timeout=30.0):
        super().__init__(endpoint=endpoint, timeout=timeout)
        self.sent_prompts = []

    def send_prompt(self, session_id: str, prompt: str, sandbox: str | None = "read-only") -> None:
        self.sent_prompts.append({"session_id": session_id, "prompt": prompt, "sandbox": sandbox})
        # Simulate streaming events
        self._queue.put(AgNormalizedEvent(event_type="thought", payload={"thought": "Planning..."}))
        self._queue.put(AgNormalizedEvent(event_type="tool_call", payload={"tool": "view_file", "args": {"path": "README.md"}}))
        self._queue.put(AgNormalizedEvent(event_type="tool_result", payload={"output": "# Title"}))
        self._queue.put(AgNormalizedEvent(event_type="result", payload={"response": "Verified title is # Title", "stats": {"tokens": 42}}))


class TestAgIdeBridge(unittest.TestCase):
    def setUp(self):
        self.bridge = AgIdeBridge(client_factory=MockAgClient)

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_is_alive_and_pid(self, mock_detect):
        mock_detect.return_value = [{"name": "Antigravity.exe", "pid": 12345}]
        self.assertTrue(self.bridge.is_alive())
        self.assertEqual(self.bridge.get_live_pid(), 12345)

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_prepare_when_offline_raises(self, mock_detect):
        mock_detect.return_value = []
        req = LaunchRequest(working_directory="/test")
        with self.assertRaises(AgLaunchError) as ctx:
            self.bridge.prepare(req)
        self.assertEqual(ctx.exception.classification, "live_ide_not_found")

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_lifecycle_success_with_heartbeats(self, mock_detect):
        mock_detect.return_value = [{"name": "Antigravity.exe", "pid": 9999}]
        req = LaunchRequest(working_directory="/test", turn_timeout_seconds=5.0)

        prepared = self.bridge.prepare(req)
        self.assertEqual(prepared.mode, "live_ide")
        self.assertEqual(prepared.pid, 9999)
        self.assertTrue(prepared.thread_id.startswith("ag-live-"))

        running = self.bridge.start(prepared, "Summarize README.md")
        self.assertTrue(running.turn_id.startswith("turn-"))

        heartbeats = []
        running._heartbeat = lambda ev: heartbeats.append(ev)

        outcome = self.bridge.wait(running)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.response_text, "Verified title is # Title")
        self.assertEqual(outcome.stats["tokens"], 42)
        self.assertGreater(len(heartbeats), 0)

        # Prohibit double start
        with self.assertRaises(AgLaunchError):
            self.bridge.start(prepared, "Second attempt")

        self.bridge.close(prepared)

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_error_event_handling(self, mock_detect):
        mock_detect.return_value = [{"name": "Antigravity.exe", "pid": 9999}]

        class ErrorAgClient(AgIdeClient):
            def send_prompt(self, session_id, prompt, sandbox=None):
                self._queue.put(AgNormalizedEvent(event_type="error", payload={"error": "Tool execution blocked", "code": "POLICY_DENIED"}))

        bridge = AgIdeBridge(client_factory=ErrorAgClient)
        req = LaunchRequest(working_directory="/test")
        prepared = bridge.prepare(req)
        running = bridge.start(prepared, "Dangerous prompt")
        outcome = bridge.wait(running)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.failure_classification, "POLICY_DENIED")
        self.assertIn("Tool execution blocked", outcome.failure_detail)


if __name__ == "__main__":
    unittest.main()
