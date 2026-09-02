"""Tests for the Headless Antigravity fallback runner and fail-closed auth guard."""

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from manager.ag_headless_runner import (
    AgHeadlessProcess,
    AgHeadlessRunner,
    resolve_ag_executable,
    sanitize_ag_environment,
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

    @patch.dict("os.environ", {"GOOGLE_API_KEY": "AIzaSyFakeKeyThirdParty"}, clear=True)
    @patch("pathlib.Path.is_dir")
    @patch("pathlib.Path.is_file")
    def test_verify_auth_identity_fails_closed_when_api_key_provided_without_profile(self, mock_is_file, mock_is_dir):
        # GOOGLE_API_KEY is not proof of local profile; without local config it must fail closed
        mock_is_dir.return_value = False
        mock_is_file.return_value = False
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
            "GCP_PROJECT": "gcp-proj-2",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/sa.json",
            "GOOGLE_GENAI_USE_VERTEXAI": "1",
            "CUSTOM_VAR": "keep-me",
        }
        clean_env = sanitize_ag_environment(dirty_env)
        self.assertNotIn("GOOGLE_API_KEY", clean_env)
        self.assertNotIn("GEMINI_API_KEY", clean_env)
        self.assertNotIn("VERTEX_PROJECT", clean_env)
        self.assertNotIn("GOOGLE_CLOUD_PROJECT", clean_env)
        self.assertNotIn("GCP_PROJECT", clean_env)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", clean_env)
        self.assertNotIn("GOOGLE_GENAI_USE_VERTEXAI", clean_env)
        self.assertEqual(clean_env["CUSTOM_VAR"], "keep-me")
        self.assertEqual(clean_env["PATH"], "/bin:/usr/bin")

    @patch("shutil.which")
    def test_resolve_ag_executable_raises_when_missing(self, mock_which):
        mock_which.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_ag_executable()
            self.assertEqual(ctx.exception.classification, "executable_not_found")

    def test_headless_route_is_the_language_server_route(self):
        """The headless fallback shares the language-server adapter and fails
        closed exactly like it when no Antigravity language server is running
        (there is no standalone gemini/agy CLI route on this machine)."""
        from manager.ag_language_server import AgLsError

        def missing(timeout):
            raise AgLsError("ide_not_running", "no process")

        runner = AgHeadlessRunner(executable_resolver=lambda: "/mock/bin/gemini", auth_verifier=lambda: "local_profile")
        runner._discover = missing
        self.assertEqual("headless", runner.default_mode)
        import tempfile
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaises(AgLaunchError) as ctx:
                runner.prepare(LaunchRequest(working_directory=workdir, model="flash"))
        self.assertEqual("ide_not_running", ctx.exception.classification)


if __name__ == "__main__":
    unittest.main()
