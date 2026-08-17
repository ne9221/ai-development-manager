"""Comprehensive tests for Official Antigravity CLI dispatch adapter (OfficialAgCliRunner)."""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from manager.ag_cli_runner import (
    AgCliProcess,
    OfficialAgCliRunner,
    resolve_ag_cli_executable,
    resolve_ag_official_cli_executable,
    sanitize_ag_environment,
    verify_auth_identity,
)
from manager.ag_runner import AgLaunchError, LaunchRequest, normalize_event


class TestOfficialAgCliRunner(unittest.TestCase):
    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=False)
    @patch("pathlib.Path.is_file", return_value=True)
    @patch("pathlib.Path.open", new_callable=mock_open, read_data=json.dumps({"access_token": "ya29.fake-token", "expiry": "2099-01-01T00:00:00Z"}))
    def test_verify_auth_identity_success_with_parsed_credential_token(self, mock_file_open, mock_is_file, mock_cli_check):
        identity = verify_auth_identity()
        self.assertEqual(identity, "local_google_account_profile")

    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=True)
    @patch("pathlib.Path.is_file", return_value=False)
    def test_verify_auth_identity_success_via_cli_auth_status(self, mock_is_file, mock_cli_check):
        identity = verify_auth_identity()
        self.assertEqual(identity, "official_cli_auth_status")

    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=False)
    @patch("pathlib.Path.is_dir", return_value=True)
    @patch("pathlib.Path.is_file", return_value=False)
    def test_verify_auth_identity_fails_closed_on_empty_directory(self, mock_is_file, mock_is_dir, mock_cli_check):
        # A config/profile *directory* existing (but no parseable token file,
        # and no verified CLI auth-status) must NOT be accepted as proof.
        with self.assertRaises(AgLaunchError) as ctx:
            verify_auth_identity()
        self.assertEqual(ctx.exception.classification, "unverified_identity")
        self.assertIn("Fail closed", ctx.exception.detail)

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
    def test_verify_auth_identity_fails_closed_when_api_key_without_profile(self, mock_is_file, mock_is_dir, mock_cli_check):
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
            "SAFE_ENV_VAR": "keep-me",
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
        # GOOGLE_APPLICATION_CREDENTIALS is overridden (not merely removed)
        # to a guaranteed-nonexistent sentinel path, so ADC discovery fails
        # closed instead of silently falling through to gcloud SDK / GCE
        # metadata credentials.
        self.assertNotEqual(clean_env["GOOGLE_APPLICATION_CREDENTIALS"], "/path/to/sa.json")
        self.assertFalse(Path(clean_env["GOOGLE_APPLICATION_CREDENTIALS"]).exists())
        self.assertEqual(clean_env["SAFE_ENV_VAR"], "keep-me")
        self.assertEqual(clean_env["PATH"], "/bin:/usr/bin")

    def test_sanitize_ag_environment_does_not_strip_unrelated_provider_keys(self):
        # OPENAI_API_KEY / ANTHROPIC_API_KEY do not participate in
        # Google/Vertex billing-route selection, so they are out of scope
        # for this billing-isolation gate.
        dirty_env = {"OPENAI_API_KEY": "sk-fake", "ANTHROPIC_API_KEY": "sk-ant-fake"}
        clean_env = sanitize_ag_environment(dirty_env)
        self.assertEqual(clean_env["OPENAI_API_KEY"], "sk-fake")
        self.assertEqual(clean_env["ANTHROPIC_API_KEY"], "sk-ant-fake")

    @patch("shutil.which")
    def test_resolve_ag_cli_executable_raises_when_missing(self, mock_which):
        mock_which.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_ag_cli_executable()
            self.assertEqual(ctx.exception.classification, "executable_not_found")

    @patch("shutil.which")
    def test_resolve_ag_official_cli_executable_raises_route_unavailable_when_missing(self, mock_which):
        mock_which.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_ag_official_cli_executable()
            self.assertEqual(ctx.exception.classification, "route_unavailable")

    @patch("shutil.which")
    def test_resolve_ag_official_cli_executable_ignores_agentapi_and_gemini(self, mock_which):
        # Only a standalone `agy` counts -- agentapi/gemini being on PATH
        # must not make AG_OFFICIAL_CLI look available.
        mock_which.side_effect = lambda name: (
            "/usr/local/bin/agentapi" if name == "agentapi" else ("/usr/local/bin/gemini" if name == "gemini" else None)
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_ag_official_cli_executable()
            self.assertEqual(ctx.exception.classification, "route_unavailable")

    @patch("shutil.which")
    def test_resolve_ag_official_cli_executable_finds_standalone_agy(self, mock_which):
        mock_which.side_effect = lambda name: "/usr/local/bin/agy" if name == "agy" else None
        with patch.dict("os.environ", {}, clear=True):
            path, prefix = resolve_ag_official_cli_executable()
            self.assertIn("agy", path)
            self.assertEqual(prefix, [])

    @patch("shutil.which")
    def test_resolve_ag_cli_executable_finds_in_path(self, mock_which):
        mock_which.side_effect = lambda name: "/usr/local/bin/agentapi" if name == "agentapi" else None
        path, prefix = resolve_ag_cli_executable()
        self.assertIn("agentapi", path)
        self.assertEqual(prefix, [])

    def test_cli_lifecycle_agentapi_structured_success(self):
        mock_resolver = lambda: ("/opt/bin/agentapi", [])
        mock_auth = lambda: "local_profile"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)

        req = LaunchRequest(working_directory="/test", model="flash")
        prepared = runner.prepare(req)
        self.assertEqual(prepared.mode, "cli")
        self.assertTrue(prepared.thread_id.startswith("ag-cli-"))

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.stdout.readline.side_effect = [
            json.dumps({"response": "Analysis complete: README title is Project Overview", "stats": {"tokens": 42}}) + "\n",
            "",
        ]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            running = runner.start(prepared, "Read README header")
            self.assertEqual(prepared.pid, 9999)

            called_args = mock_popen.call_args[0][0]
            self.assertEqual(called_args[0], "/opt/bin/agentapi")
            self.assertIn("new-conversation", called_args)
            self.assertIn("--model", called_args)
            self.assertIn("flash", called_args)
            self.assertIn("Read README header", called_args)

            outcome = runner.wait(running)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.response_text, "Analysis complete: README title is Project Overview")
            self.assertEqual(outcome.stats["tokens"], 42)
            self.assertIsNone(outcome.failure_classification)

            runner.close(prepared)

    def test_cli_lifecycle_agentapi_structured_error(self):
        mock_resolver = lambda: ("/opt/bin/agentapi", [])
        mock_auth = lambda: "local_profile"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)

        req = LaunchRequest(working_directory="/test")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 9998
        mock_proc.stdout.readline.side_effect = [
            json.dumps({"response": {}, "error": "rpc error: code = PermissionDenied desc = access denied"}) + "\n",
            "",
        ]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "Run task")
            outcome = runner.wait(running)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.failure_classification, "provider_error")
            self.assertIn("access denied", outcome.failure_detail)
            self.assertIsNone(outcome.response_text)

    def test_cli_lifecycle_non_zero_exit_code_classified(self):
        mock_resolver = lambda: ("/opt/bin/agy", [])
        mock_auth = lambda: "local_profile"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)

        req = LaunchRequest(working_directory="/test")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 9997
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = ["fatal: authentication failed\n", ""]
        mock_proc.poll.return_value = 2
        mock_proc.returncode = 2

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "Run task")
            outcome = runner.wait(running)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.failure_classification, "exit_code_2")
            self.assertIn("authentication failed", outcome.failure_detail)
            self.assertIsNone(outcome.response_text)

    def test_cli_lifecycle_timeout_terminates_process(self):
        mock_resolver = lambda: ("/opt/bin/agentapi", [])
        mock_auth = lambda: "local_profile"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)

        req = LaunchRequest(working_directory="/test", turn_timeout_seconds=0.1)
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 9996
        # Simulate blocking read
        mock_proc.stdout.readline.side_effect = lambda: ""
        mock_proc.stderr.readline.side_effect = lambda: ""
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "Hang forever")
            outcome = runner.wait(running)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.failure_classification, "turn_timeout")
            mock_proc.terminate.assert_called()

    def test_cli_lifecycle_cancellation(self):
        mock_resolver = lambda: ("/opt/bin/agentapi", [])
        mock_auth = lambda: "local_profile"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)

        req = LaunchRequest(working_directory="/test")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 9995
        mock_proc.stdout.readline.side_effect = lambda: ""
        mock_proc.stderr.readline.side_effect = lambda: ""
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "Cancel me")
            running._cancelled = True
            outcome = runner.wait(running)
            self.assertEqual(outcome.status, "interrupted")
            self.assertEqual(outcome.failure_classification, "cancelled")
            mock_proc.terminate.assert_called()

    def test_cli_lifecycle_unstructured_stdout_accumulated(self):
        mock_resolver = lambda: ("/opt/bin/agy", [])
        mock_auth = lambda: "local_profile"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)

        req = LaunchRequest(working_directory="/test")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 9994
        mock_proc.stdout.readline.side_effect = [
            "Line 1: starting\n",
            "Line 2: processing\n",
            "Line 3: done\n",
            "",
        ]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "Run plain cli")
            outcome = runner.wait(running)
            self.assertEqual(outcome.status, "completed")
            self.assertIn("Line 1: starting", outcome.response_text)
            self.assertIn("Line 3: done", outcome.response_text)


class TestAgAuthAndCredentialHardening(unittest.TestCase):
    """Direct unit tests for hardened token expiry parsing, CLI positive auth proof, and canonical path resolution."""

    def test_parse_timestamp_formats(self):
        from manager.ag_cli_runner import _parse_timestamp
        self.assertIsNone(_parse_timestamp(None))
        self.assertIsNone(_parse_timestamp(True))
        self.assertIsNone(_parse_timestamp(False))
        self.assertIsNone(_parse_timestamp("invalid-date"))
        self.assertIsNone(_parse_timestamp(""))

        # Epoch seconds
        self.assertEqual(_parse_timestamp(1700000000), 1700000000.0)
        self.assertEqual(_parse_timestamp("1700000000"), 1700000000.0)

        # Epoch milliseconds
        self.assertEqual(_parse_timestamp(1700000000000), 1700000000.0)
        self.assertEqual(_parse_timestamp("1700000000000"), 1700000000.0)

        # ISO format
        ts = _parse_timestamp("2030-01-01T00:00:00Z")
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 1800000000)

    def test_parse_local_credential_token_expiry_checks(self):
        from manager.ag_cli_runner import _parse_local_credential_token

        # Valid future expiry with token
        valid_json = json.dumps({"access_token": "ya29.valid", "expiry": "2099-01-01T00:00:00Z"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=valid_json):
            self.assertTrue(_parse_local_credential_token(Path("/fake/oauth.json")))

        # Expired token
        expired_json = json.dumps({"access_token": "ya29.valid", "expiry": "2020-01-01T00:00:00Z"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=expired_json):
            self.assertFalse(_parse_local_credential_token(Path("/fake/oauth.json")))

        # Malformed expiry
        malformed_json = json.dumps({"access_token": "ya29.valid", "expiry": "malformed"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=malformed_json):
            self.assertFalse(_parse_local_credential_token(Path("/fake/oauth.json")))

        # Missing expiry metadata
        no_expiry_json = json.dumps({"access_token": "ya29.valid"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=no_expiry_json):
            self.assertFalse(_parse_local_credential_token(Path("/fake/oauth.json")))

        # Empty token string
        empty_token_json = json.dumps({"access_token": "  ", "expiry": "2099-01-01T00:00:00Z"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=empty_token_json):
            self.assertFalse(_parse_local_credential_token(Path("/fake/oauth.json")))

    def test_cli_auth_status_check_positive_proof(self):
        from manager.ag_cli_runner import _cli_auth_status_check

        def _run_with_stdout(stdout: str, rc: int = 0):
            mock_proc = MagicMock()
            mock_proc.returncode = rc
            mock_proc.stdout = stdout
            mock_proc.stderr = ""
            with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/bin/agy", [])), \
                 patch("subprocess.run", return_value=mock_proc):
                return _cli_auth_status_check()

        # Reject blank
        self.assertFalse(_run_with_stdout(""))
        self.assertFalse(_run_with_stdout("   \n  "))

        # Reject exit code != 0
        self.assertFalse(_run_with_stdout("Logged in as user@example.com", rc=1))

        # Reject guest session
        self.assertFalse(_run_with_stdout("Guest session active"))
        self.assertFalse(_run_with_stdout(json.dumps({"status": "guest", "authenticated": True})))

        # Reject Chinese unauthenticated
        self.assertFalse(_run_with_stdout("未登入"))
        self.assertFalse(_run_with_stdout("請先登入帳號"))

        # Reject generic unknown success text
        self.assertFalse(_run_with_stdout("Success! Process finished."))
        self.assertFalse(_run_with_stdout("OK 200"))

        # Accept positive patterns
        self.assertTrue(_run_with_stdout("Logged in as test_user@google.com"))
        self.assertTrue(_run_with_stdout("Authenticated as alice"))
        self.assertTrue(_run_with_stdout("Auth status: Authenticated"))
        self.assertTrue(_run_with_stdout(json.dumps({"authenticated": True, "user": "alice@gmail.com"})))


if __name__ == "__main__":
    unittest.main()
