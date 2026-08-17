"""Tests for the Live Antigravity IDE Bridge and fail-closed transport checks."""

import queue
import time
import unittest
from unittest.mock import MagicMock, patch

from manager.ag_ide_bridge import AgIdeBridge, AgIdeClient
from manager.ag_runner import AgLaunchError, AgNormalizedEvent, LaunchRequest


class MockVerifiedAgClient(AgIdeClient):
    """Mock client simulating a working local IPC connection for unit test verification."""

    def __init__(self, endpoint=None, timeout=30.0):
        super().__init__(endpoint=endpoint, timeout=timeout)
        self.sent_prompts = []

    def is_available(self) -> bool:
        return True

    def send_prompt(self, session_id: str, prompt: str, sandbox: str | None = "read-only") -> None:
        if self._closed:
            raise AgLaunchError("bridge_closed", "Cannot send prompt to closed bridge")
        self.sent_prompts.append({"session_id": session_id, "prompt": prompt, "sandbox": sandbox})
        self._queue.put(AgNormalizedEvent(event_type="thought", payload={"thought": "Planning..."}))
        self._queue.put(AgNormalizedEvent(event_type="tool_call", payload={"tool": "view_file", "args": {"path": "README.md"}}))
        self._queue.put(AgNormalizedEvent(event_type="tool_result", payload={"output": "# Title"}))
        self._queue.put(AgNormalizedEvent(event_type="result", payload={"response": "Verified title is # Title", "stats": {"tokens": 42}}))

    def read_events(self, timeout: float = 1.0):
        while not self._closed:
            try:
                event = self._queue.get(timeout=timeout)
                yield event
                if event.event_type in ("result", "error"):
                    break
            except queue.Empty:
                if self._closed:
                    break


class TestAgIdeBridge(unittest.TestCase):
    def test_default_client_fails_closed_no_transport(self):
        client = AgIdeClient()
        self.assertFalse(client.is_available())
        with self.assertRaises(AgLaunchError) as ctx:
            client.send_prompt("sess-1", "test prompt")
        self.assertEqual(ctx.exception.classification, "live_ide_transport_unavailable")

        with self.assertRaises(AgLaunchError) as ctx2:
            list(client.read_events())
        self.assertEqual(ctx2.exception.classification, "live_ide_transport_unavailable")

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_bridge_with_default_client_fails_closed_on_prepare(self, mock_detect):
        mock_detect.return_value = [{"name": "Antigravity.exe", "pid": 12345}]
        bridge = AgIdeBridge()
        self.assertTrue(bridge.is_alive())
        self.assertFalse(bridge.is_transport_available())

        req = LaunchRequest(working_directory="/test")
        with self.assertRaises(AgLaunchError) as ctx:
            bridge.prepare(req)
        self.assertEqual(ctx.exception.classification, "live_ide_transport_unavailable")
        self.assertIn("direct IPC transport is unavailable", ctx.exception.detail)

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_prepare_when_offline_raises_not_found(self, mock_detect):
        mock_detect.return_value = []
        bridge = AgIdeBridge()
        self.assertFalse(bridge.is_alive())
        req = LaunchRequest(working_directory="/test")
        with self.assertRaises(AgLaunchError) as ctx:
            bridge.prepare(req)
        self.assertEqual(ctx.exception.classification, "live_ide_not_found")

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_mock_verified_client_lifecycle_and_heartbeats(self, mock_detect):
        mock_detect.return_value = [{"name": "Antigravity.exe", "pid": 9999}]
        bridge = AgIdeBridge(client_factory=MockVerifiedAgClient)
        self.assertTrue(bridge.is_transport_available())

        req = LaunchRequest(working_directory="/test", turn_timeout_seconds=5.0)
        prepared = bridge.prepare(req)
        self.assertEqual(prepared.mode, "live_ide")
        self.assertEqual(prepared.pid, 9999)

        running = bridge.start(prepared, "Summarize README.md")
        heartbeats = []
        running._heartbeat = lambda ev: heartbeats.append(ev)

        outcome = bridge.wait(running)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.response_text, "Verified title is # Title")
        self.assertGreater(len(heartbeats), 0)

        # Prohibit double start
        with self.assertRaises(AgLaunchError):
            bridge.start(prepared, "Second attempt")

        bridge.close(prepared)

    @patch("manager.ag_ide_bridge._detect_live_processes")
    def test_error_event_handling_with_mock_client(self, mock_detect):
        mock_detect.return_value = [{"name": "Antigravity.exe", "pid": 9999}]

        class ErrorAgClient(MockVerifiedAgClient):
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
