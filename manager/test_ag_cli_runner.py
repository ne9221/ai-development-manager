"""Comprehensive tests for Official Antigravity CLI dispatch adapter (OfficialAgCliRunner)."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from manager.ag_cli_runner import (
    AgCliProcess,
    OfficialAgCliRunner,
    resolve_ag_cli_executable,
    sanitize_ag_environment,
    verify_auth_identity,
)
from manager.ag_runner import AgLaunchError, LaunchRequest, normalize_event


class TestOfficialAgCliRunner(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.valid_cwd = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

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
    def test_verify_auth_identity_fails_closed_when_api_key_without_profile(self, mock_is_file, mock_is_dir):
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
            "SAFE_ENV_VAR": "keep-me",
        }
        clean_env = sanitize_ag_environment(dirty_env)
        self.assertNotIn("GOOGLE_API_KEY", clean_env)
        self.assertNotIn("GEMINI_API_KEY", clean_env)
        self.assertNotIn("VERTEX_PROJECT", clean_env)
        self.assertNotIn("GOOGLE_CLOUD_PROJECT", clean_env)
        self.assertNotIn("GCP_PROJECT", clean_env)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", clean_env)
        self.assertNotIn("GOOGLE_GENAI_USE_VERTEXAI", clean_env)
        self.assertEqual(clean_env["SAFE_ENV_VAR"], "keep-me")
        self.assertEqual(clean_env["PATH"], "/bin:/usr/bin")

    @patch("shutil.which")
    def test_resolve_ag_cli_executable_raises_when_missing(self, mock_which):
        mock_which.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_ag_cli_executable()
            self.assertEqual(ctx.exception.classification, "executable_not_found")

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

        req = LaunchRequest(working_directory=self.valid_cwd, model="flash")
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
            self.assertEqual(mock_popen.call_args.kwargs["cwd"], self.valid_cwd)

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

        req = LaunchRequest(working_directory=self.valid_cwd)
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

        req = LaunchRequest(working_directory=self.valid_cwd)
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

        req = LaunchRequest(working_directory=self.valid_cwd, turn_timeout_seconds=0.1)
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

        req = LaunchRequest(working_directory=self.valid_cwd)
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

        req = LaunchRequest(working_directory=self.valid_cwd)
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


class TestAgCliRunnerWorkingDirectoryFailClosed(unittest.TestCase):
    """Regression coverage: OfficialAgCliRunner.start() must never spawn the
    CLI subprocess with a missing/invalid working_directory, and must never
    fall back to the ambient process cwd (cwd=None) to do so."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.valid_cwd = self._tmpdir.name
        self.mock_resolver = lambda: ("/opt/bin/agentapi", [])
        self.mock_auth = lambda: "local_profile"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _prepared(self, working_directory):
        runner = OfficialAgCliRunner(executable_resolver=self.mock_resolver, auth_verifier=self.mock_auth)
        req = LaunchRequest(working_directory=working_directory)
        prepared = runner.prepare(req)
        return runner, prepared

    def test_none_working_directory_fails_closed_no_spawn(self):
        runner, prepared = self._prepared(None)
        with patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(AgLaunchError) as ctx:
                runner.start(prepared, "prompt")
            self.assertEqual(ctx.exception.classification, "invalid_request")
            mock_popen.assert_not_called()

    def test_empty_string_working_directory_fails_closed_no_spawn(self):
        runner, prepared = self._prepared("")
        with patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(AgLaunchError) as ctx:
                runner.start(prepared, "prompt")
            self.assertEqual(ctx.exception.classification, "invalid_request")
            mock_popen.assert_not_called()

    def test_relative_working_directory_fails_closed_no_spawn(self):
        runner, prepared = self._prepared("relative/path")
        with patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(AgLaunchError) as ctx:
                runner.start(prepared, "prompt")
            self.assertEqual(ctx.exception.classification, "invalid_request")
            mock_popen.assert_not_called()

    def test_nonexistent_absolute_working_directory_fails_closed_no_spawn(self):
        nonexistent = str(Path(self.valid_cwd) / "does-not-exist-xyz")
        runner, prepared = self._prepared(nonexistent)
        with patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(AgLaunchError) as ctx:
                runner.start(prepared, "prompt")
            self.assertEqual(ctx.exception.classification, "invalid_request")
            mock_popen.assert_not_called()

    def test_file_path_instead_of_directory_fails_closed_no_spawn(self):
        file_path = str(Path(self.valid_cwd) / "some_file.txt")
        Path(file_path).write_text("not a directory")
        runner, prepared = self._prepared(file_path)
        with patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(AgLaunchError) as ctx:
                runner.start(prepared, "prompt")
            self.assertEqual(ctx.exception.classification, "invalid_request")
            mock_popen.assert_not_called()

    def test_valid_absolute_existing_directory_launches_with_exact_cwd(self):
        runner, prepared = self._prepared(self.valid_cwd)
        mock_proc = MagicMock()
        mock_proc.pid = 4242
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            runner.start(prepared, "prompt")
            mock_popen.assert_called_once()
            self.assertEqual(mock_popen.call_args.kwargs["cwd"], self.valid_cwd)
            self.assertIsNotNone(mock_popen.call_args.kwargs["cwd"])

    def test_path_with_spaces_launches_with_exact_cwd(self):
        spaced = tempfile.mkdtemp(suffix=" with spaces", dir=self.valid_cwd)
        runner, prepared = self._prepared(spaced)
        mock_proc = MagicMock()
        mock_proc.pid = 4243
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            runner.start(prepared, "prompt")
            self.assertEqual(mock_popen.call_args.kwargs["cwd"], spaced)

    def test_invalid_working_directory_does_not_mark_prepared_as_started(self):
        # Since a LaunchRequest is frozen/immutable, an invalid working_directory
        # can never be retried on the same PreparedLaunch anyway, but this still
        # confirms the failure happens before the _started side effect, not after.
        runner, prepared = self._prepared(None)
        self.assertFalse(prepared._started)
        with patch("subprocess.Popen"):
            with self.assertRaises(AgLaunchError):
                runner.start(prepared, "prompt")
        self.assertFalse(prepared._started)


if __name__ == "__main__":
    unittest.main()
