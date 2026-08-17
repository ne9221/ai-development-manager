"""Tests for the Headless Antigravity fallback runner and fail-closed auth guard."""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from manager.ag_headless_runner import (
    AgHeadlessProcess,
    AgHeadlessRunner,
    resolve_ag_executable,
    sanitize_ag_environment,
    verify_auth_identity,
)
from manager.ag_runner import AgLaunchError, LaunchRequest


class TestAgHeadlessRunner(unittest.TestCase):
    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=False)
    @patch("pathlib.Path.is_file", return_value=True)
    @patch("pathlib.Path.open", new_callable=mock_open, read_data=json.dumps({"access_token": "ya29.fake-token", "expiry": "2099-01-01T00:00:00Z"}))
    def test_verify_auth_identity_success_with_parsed_credential_token(self, mock_file_open, mock_is_file, mock_cli_check):
        identity = verify_auth_identity()
        self.assertEqual(identity, "local_google_account_profile")

    @patch.dict("os.environ", {}, clear=True)
    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=False)
    @patch("pathlib.Path.is_dir", return_value=False)
    @patch("pathlib.Path.is_file", return_value=False)
    def test_verify_auth_identity_fail_closed_when_unproven(self, mock_is_file, mock_is_dir, mock_cli_check):
        with self.assertRaises(AgLaunchError) as ctx:
            verify_auth_identity()
        self.assertEqual(ctx.exception.classification, "unverified_identity")
        self.assertIn("Fail closed", ctx.exception.detail)

    @patch.dict("os.environ", {"GOOGLE_API_KEY": "AIzaSyFakeKeyThirdParty"}, clear=True)
    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=False)
    @patch("pathlib.Path.is_dir", return_value=False)
    @patch("pathlib.Path.is_file", return_value=False)
    def test_verify_auth_identity_fails_closed_when_api_key_provided_without_profile(self, mock_is_file, mock_is_dir, mock_cli_check):
        # GOOGLE_API_KEY is not proof of local profile; without local config it must fail closed
        with self.assertRaises(AgLaunchError) as ctx:
            verify_auth_identity()
        self.assertEqual(ctx.exception.classification, "unverified_identity")

    def test_sanitize_ag_environment_strips_secondary_billing(self):
        dirty_env = {
            "PATH": "/bin:/usr/bin",
            "HOME": "/home/user",
            "GOOGLE_API_KEY": "secret-api-key",
            "GEMINI_API_KEY": "secret-gemini-key",
            "VERTEX_PROJECT": "my-vertex-proj",
            "GOOGLE_CLOUD_PROJECT": "gcp-proj",
            "GCLOUD_PROJECT": "gcp-proj-legacy",
            "GCP_PROJECT": "gcp-proj-2",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/sa.json",
            "GOOGLE_GENAI_USE_VERTEXAI": "1",
            "VERTEXAI_PROJECT": "vertex-proj",
            "VERTEXAI_LOCATION": "us-central1",
            "CLOUDSDK_CORE_PROJECT": "cloudsdk-proj",
            "CLOUDSDK_AUTH_ACCESS_TOKEN": "fake-access-token",
            "CUSTOM_VAR": "keep-me",
        }
        clean_env = sanitize_ag_environment(dirty_env)
        self.assertNotIn("GOOGLE_API_KEY", clean_env)
        self.assertNotIn("GEMINI_API_KEY", clean_env)
        self.assertNotIn("VERTEX_PROJECT", clean_env)
        self.assertNotIn("GOOGLE_CLOUD_PROJECT", clean_env)
        self.assertNotIn("GCLOUD_PROJECT", clean_env)
        self.assertNotIn("GCP_PROJECT", clean_env)
        self.assertNotIn("GOOGLE_CLOUD_LOCATION", clean_env)
        self.assertNotIn("GOOGLE_GENAI_USE_VERTEXAI", clean_env)
        self.assertNotIn("VERTEXAI_PROJECT", clean_env)
        self.assertNotIn("VERTEXAI_LOCATION", clean_env)
        self.assertNotIn("CLOUDSDK_CORE_PROJECT", clean_env)
        self.assertNotIn("CLOUDSDK_AUTH_ACCESS_TOKEN", clean_env)
        self.assertNotEqual(clean_env["GOOGLE_APPLICATION_CREDENTIALS"], "/path/to/sa.json")
        self.assertFalse(Path(clean_env["GOOGLE_APPLICATION_CREDENTIALS"]).exists())
        self.assertEqual(clean_env["CUSTOM_VAR"], "keep-me")
        self.assertEqual(clean_env["PATH"], "/bin:/usr/bin")

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

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            running = runner.start(prepared, "Prompt text")
            self.assertEqual(prepared.pid, 4321)

            # Ensure subprocess environment was sanitized
            called_env = mock_popen.call_args[1].get("env")
            self.assertIsNotNone(called_env)
            self.assertNotIn("GOOGLE_API_KEY", called_env)

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
