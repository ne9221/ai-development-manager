"""Tests for the Headless Antigravity fallback runner and fail-closed auth guard."""

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from manager.ag_headless_runner import (
    AgHeadlessProcess,
    AgHeadlessRunner,
    resolve_ag_executable,
    verify_auth_identity,
)
from manager.ag_runner import AgLaunchError, LaunchRequest


class TestAgHeadlessRunner(unittest.TestCase):
    @patch("pathlib.Path.is_dir")
    @patch("pathlib.Path.is_file")
    def test_verify_auth_identity_success_with_local_profile(self, mock_is_file, mock_is_dir):
        mock_is_dir.return_value = True
        mock_is_file.return_value = False
        identity = verify_auth_identity()
        self.assertEqual(identity, "local_google_account_profile")

    @patch.dict("os.environ", {}, clear=True)
    @patch("pathlib.Path.is_dir")
    @patch("pathlib.Path.is_file")
    def test_verify_auth_identity_fail_closed_when_unproven(self, mock_is_file, mock_is_dir):
        mock_is_dir.return_value = False
        mock_is_file.return_value = False
        with self.assertRaises(AgLaunchError) as ctx:
            verify_auth_identity()
        self.assertEqual(ctx.exception.classification, "unverified_identity")
        self.assertIn("Fail closed", ctx.exception.detail)

    @patch("shutil.which")
    def test_resolve_ag_executable_raises_when_missing(self, mock_which):
        mock_which.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_ag_executable()
            self.assertEqual(ctx.exception.classification, "executable_not_found")

    def test_headless_lifecycle_mocked(self):
        mock_resolver = lambda: "/mock/bin/gemini"
        mock_auth = lambda: "local_profile"
        runner = AgHeadlessRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)

        req = LaunchRequest(working_directory="/test", model="gemini-3.7-flash")
        prepared = runner.prepare(req)
        self.assertEqual(prepared.mode, "headless")
        self.assertTrue(prepared.thread_id.startswith("ag-headless-"))

        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_proc.stdout.readline.side_effect = [
            json.dumps({"type": "init", "session_id": "s-123"}) + "\n",
            json.dumps({"type": "thought", "thought": "Reading repo..."}) + "\n",
            json.dumps({"type": "result", "response": "Finished", "stats": {"tokens": 10}}) + "\n",
            "",
        ]
        mock_proc.poll.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "Prompt text")
            self.assertEqual(prepared.pid, 4321)

            events = []
            running._heartbeat = lambda ev: events.append(ev)

            outcome = runner.wait(running)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.response_text, "Finished")
            self.assertEqual(outcome.stats["tokens"], 10)
            self.assertGreater(len(events), 0)

            runner.close(prepared)


if __name__ == "__main__":
    unittest.main()
