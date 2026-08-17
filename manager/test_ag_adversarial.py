"""Adversarial regression tests for the AG official CLI remediation
(session ag-official-cli-remediation-r1-20260817): billing isolation,
auth false-positive, route authenticity, silent fallback, Windows
invocation/lifecycle, and false-COMPLETED normalization.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from manager.ag_cli_runner import (
    AgCliProcess,
    OfficialAgCliRunner,
    resolve_ag_cli_executable,
    resolve_ag_official_cli_executable,
    resolve_canonical_gemini_home,
    sanitize_ag_environment,
    verify_auth_identity,
)
from manager.ag_headless_runner import AgHeadlessRunner
from manager.ag_ide_bridge import AgIdeBridge
from manager.ag_runner import (
    AgLaunchError,
    AgRunner,
    LaunchRequest,
    ROUTE_GEMINI_CLI_FALLBACK,
    ROUTE_LIVE_IDE_IPC,
    ROUTE_OFFICIAL_CLI,
)


class TestAdvAuthEmptyDirectoryFailsClosed(unittest.TestCase):
    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=False)
    @patch("pathlib.Path.is_dir", return_value=True)
    @patch("pathlib.Path.is_file", return_value=False)
    def test_adv_auth_empty_directory_fails_closed(self, mock_is_file, mock_is_dir, mock_cli_check):
        """~/.gemini/config, antigravity-ide, etc. existing as bare (empty)
        directories -- with no parseable credential file and no verified CLI
        auth-status -- must not be accepted as identity proof."""
        with self.assertRaises(AgLaunchError) as ctx:
            verify_auth_identity()
        self.assertEqual(ctx.exception.classification, "unverified_identity")

    @patch("manager.ag_cli_runner._cli_auth_status_check", return_value=False)
    @patch("pathlib.Path.is_file", return_value=True)
    @patch("pathlib.Path.open", new_callable=mock_open, read_data="{}")
    def test_adv_auth_credential_file_without_token_fields_fails_closed(self, mock_open_call, mock_is_file, mock_cli_check):
        """A credential file that exists but has no real token field is not
        proof either -- only presence, not content, would be the old bug."""
        with self.assertRaises(AgLaunchError) as ctx:
            verify_auth_identity()
        self.assertEqual(ctx.exception.classification, "unverified_identity")


class TestAdvBillingAdcOverrideAndGcpVars(unittest.TestCase):
    def test_adv_billing_adc_override_and_gcp_vars(self):
        dirty_env = {
            "GOOGLE_CLOUD_PROJECT": "leak-proj",
            "GCLOUD_PROJECT": "leak-proj-legacy",
            "CLOUDSDK_CORE_PROJECT": "leak-cloudsdk-proj",
            "CLOUDSDK_AUTH_ACCESS_TOKEN": "leak-token",
            "VERTEXAI_PROJECT": "leak-vertex-proj",
            "VERTEXAI_LOCATION": "us-central1",
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
            "GOOGLE_APPLICATION_CREDENTIALS": "/real/service-account.json",
        }
        clean_env = sanitize_ag_environment(dirty_env)
        for var in (
            "GOOGLE_CLOUD_PROJECT",
            "GCLOUD_PROJECT",
            "CLOUDSDK_CORE_PROJECT",
            "CLOUDSDK_AUTH_ACCESS_TOKEN",
            "VERTEXAI_PROJECT",
            "VERTEXAI_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
        ):
            self.assertNotIn(var, clean_env, f"{var} must be stripped from the child environment")

        # GOOGLE_APPLICATION_CREDENTIALS must be overridden to a
        # provably-nonexistent path (verified against the installed
        # google-auth library: an explicit env var pointing at a missing
        # file raises DefaultCredentialsError immediately in
        # google.auth.default(), rather than falling through to gcloud SDK
        # cached credentials or the GCE metadata server).
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", clean_env)
        self.assertNotEqual(clean_env["GOOGLE_APPLICATION_CREDENTIALS"], "/real/service-account.json")
        adc_path = clean_env["GOOGLE_APPLICATION_CREDENTIALS"]
        self.assertFalse(os.path.exists(adc_path))

        import google.auth
        import google.auth.exceptions

        with patch.dict(os.environ, clean_env, clear=True):
            with self.assertRaises(google.auth.exceptions.DefaultCredentialsError):
                google.auth.default()


class TestAdvRouteNoSilentFallbackOnAuthFail(unittest.TestCase):
    def test_adv_route_no_silent_fallback_on_auth_fail_forced_official(self):
        """force_mode='cli' (AG_OFFICIAL_CLI): an auth failure must propagate,
        never silently fall back to another runner."""
        runner = AgRunner()
        req = LaunchRequest(working_directory=".", force_mode="cli")
        with patch(
            "manager.ag_cli_runner.verify_auth_identity",
            side_effect=AgLaunchError("unverified_identity", "no proof"),
        ):
            with self.assertRaises(AgLaunchError) as ctx:
                runner.prepare(req)
        self.assertEqual(ctx.exception.classification, "unverified_identity")

    def test_adv_route_no_silent_fallback_on_auth_fail_auto_mode(self):
        """auto mode: binary resolves fine but auth fails -- must stop, not
        silently fall back to a different runner/route."""
        mock_bridge = MagicMock()
        mock_bridge.is_alive.return_value = False
        mock_headless = MagicMock()
        mock_headless.prepare.side_effect = AgLaunchError("unverified_identity", "no proof")
        runner = AgRunner(ide_bridge=mock_bridge, headless_runner=mock_headless)
        req = LaunchRequest(working_directory=".")
        with self.assertRaises(AgLaunchError) as ctx:
            runner.prepare(req)
        self.assertEqual(ctx.exception.classification, "unverified_identity")
        # Evidence: fallback_reason was set to route-not-found (live IDE
        # offline caused the hop to the fallback runner), but that fallback
        # runner's OWN auth failure must not trigger yet another fallback.
        self.assertEqual(runner.last_fallback_reason, "live_ide_not_found")


class TestAdvRouteDistinctFromLiveIde(unittest.TestCase):
    def test_adv_route_distinct_from_live_ide(self):
        official_runner = OfficialAgCliRunner(
            executable_resolver=lambda: ("/opt/bin/agy", []),
            auth_verifier=lambda: "verified",
        )
        official_prep = official_runner.prepare(LaunchRequest(working_directory="."))
        self.assertEqual(official_prep.route_used, ROUTE_OFFICIAL_CLI)

        headless_runner = AgHeadlessRunner(
            executable_resolver=lambda: ("/opt/bin/agentapi", []),
            auth_verifier=lambda: "verified",
        )
        headless_prep = headless_runner.prepare(LaunchRequest(working_directory="."))
        self.assertEqual(headless_prep.route_used, ROUTE_GEMINI_CLI_FALLBACK)

        self.assertNotEqual(official_prep.route_used, headless_prep.route_used)
        self.assertNotEqual(official_prep.route_used, ROUTE_LIVE_IDE_IPC)
        self.assertNotEqual(headless_prep.route_used, ROUTE_LIVE_IDE_IPC)

    def test_adv_official_cli_default_resolver_never_accepts_agentapi_or_gemini(self):
        """OfficialAgCliRunner() with no injected resolver (the real default
        wiring for AG_OFFICIAL_CLI) must use the strict standalone-agy-only
        resolver, not the permissive agentapi/gemini fallback resolver."""
        runner = OfficialAgCliRunner(auth_verifier=lambda: "verified")
        with patch("shutil.which", return_value=None), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                runner.prepare(LaunchRequest(working_directory="."))
        self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_adv_fallback_never_reports_official_cli_after_transition(self):
        """After a Live-IDE -> fallback transition in auto mode, evidence
        must never claim AG_OFFICIAL_CLI -- the fallback runner must be the
        permissive GEMINI_CLI_FALLBACK resolver, not the strict
        official-only resolver silently substituted in."""
        mock_bridge = MagicMock()
        mock_bridge.is_alive.return_value = False
        runner = AgRunner(ide_bridge=mock_bridge)
        req = LaunchRequest(working_directory=".")
        with patch("manager.ag_cli_runner.verify_auth_identity", return_value="verified"), \
             patch("shutil.which", return_value=None), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                runner.prepare(req)
        # The broad/fallback resolver raises "executable_not_found"; the
        # strict AG_OFFICIAL_CLI-only resolver would instead raise
        # "route_unavailable". Getting this specific classification proves
        # the fallback runner (headless) resolved via the permissive path.
        self.assertEqual(ctx.exception.classification, "executable_not_found")
        self.assertEqual(runner.last_fallback_reason, "live_ide_not_found")


class TestAdvWindowsCmdResolutionAndOrphanCleanup(unittest.TestCase):
    def test_adv_windows_cmd_resolution_wraps_bat_through_comspec(self):
        mock_resolver = lambda: ("C:/fake/antigravity-ide/bin/agentapi.bat", [])
        mock_auth = lambda: "verified"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 5555
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            runner.start(prepared, "some prompt with & | metacharacters")
            called_args = mock_popen.call_args[0][0]
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            self.assertEqual(called_args[0], comspec)
            self.assertEqual(called_args[1], "/c")
            self.assertEqual(called_args[2], "C:/fake/antigravity-ide/bin/agentapi.bat")
            # The prompt remains one single argv element, never concatenated
            # into a shell string -- this is why shell=True is avoided.
            self.assertIn("some prompt with & | metacharacters", called_args)
            self.assertFalse(mock_popen.call_args[1].get("shell", False))

    def test_adv_windows_cmd_resolution_leaves_exe_untouched(self):
        mock_resolver = lambda: ("C:/fake/bin/agy.exe", [])
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=lambda: "verified")
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 5556
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            runner.start(prepared, "prompt")
            called_args = mock_popen.call_args[0][0]
            self.assertEqual(called_args[0], "C:/fake/bin/agy.exe")

    @unittest.skipUnless(os.name == "nt", "Windows-only real process-tree cleanup check")
    def test_adv_windows_orphan_cleanup_real_process_tree(self):
        """Spawn a real parent process that itself spawns a real grandchild
        (simulating a .bat wrapper spawning node.exe), call terminate(), and
        verify BOTH are actually gone -- not just the immediate pid."""
        marker = Path(os.environ["TEMP"]) / f"adm_ag_adv_grandchild_pid_{os.getpid()}.txt"
        if marker.exists():
            marker.unlink()

        parent_script = (
            "import subprocess, sys, time, os;"
            f"gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            f"open(r'{marker}', 'w').write(str(gc.pid));"
            "time.sleep(60)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        cli_proc = AgCliProcess.__new__(AgCliProcess)
        cli_proc.process = proc

        try:
            deadline = time.time() + 10
            grandchild_pid = None
            while time.time() < deadline and grandchild_pid is None:
                if marker.exists():
                    content = marker.read_text().strip()
                    if content:
                        grandchild_pid = int(content)
                time.sleep(0.2)
            self.assertIsNotNone(grandchild_pid, "grandchild pid marker was never written")

            cli_proc.terminate()

            def _pid_alive(pid: int) -> bool:
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                )
                return str(pid) in out.stdout

            deadline = time.time() + 10
            while time.time() < deadline and (_pid_alive(proc.pid) or _pid_alive(grandchild_pid)):
                time.sleep(0.3)

            self.assertFalse(_pid_alive(proc.pid), "parent process was not cleaned up")
            self.assertFalse(_pid_alive(grandchild_pid), "grandchild process was orphaned")
        finally:
            if marker.exists():
                marker.unlink()
            try:
                proc.kill()
            except Exception:
                pass
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass


class TestAdvNormalizerDetectsExit0JsonError(unittest.TestCase):
    def _run_with_stdout_lines(self, lines):
        mock_resolver = lambda: ("/opt/bin/agentapi", [])
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=lambda: "verified")
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 7777
        mock_proc.stdout.readline.side_effect = [*lines, ""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "do the task")
            return runner.wait(running)

    def test_adv_normalizer_detects_exit0_json_error_via_status_field(self):
        line = json.dumps({"type": "result", "status": "error", "message": "quota exceeded for project"}) + "\n"
        outcome = self._run_with_stdout_lines([line])
        self.assertEqual(outcome.status, "failed")
        self.assertIn("quota", outcome.failure_detail.lower())

    def test_adv_normalizer_detects_exit0_json_error_via_response_text(self):
        line = json.dumps({"type": "result", "response": "Error: unauthorized -- please re-authenticate"}) + "\n"
        outcome = self._run_with_stdout_lines([line])
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.failure_classification, "unauthorized")

    def test_adv_normalizer_detects_exit0_unstructured_text_error(self):
        outcome = self._run_with_stdout_lines(["Fatal: quota exceeded, aborting\n"])
        self.assertEqual(outcome.status, "failed")
        self.assertIn("quota_exceeded", outcome.failure_classification)

    def test_adv_normalizer_leaves_genuine_success_completed(self):
        line = json.dumps({"type": "result", "response": "Task finished with no issues", "stats": {}}) + "\n"
        outcome = self._run_with_stdout_lines([line])
        self.assertEqual(outcome.status, "completed")


class TestAdvFinding1GeminiHomeBypass(unittest.TestCase):
    """Adversarial regression suite for Finding 1 (P0: GEMINI_HOME billing/account bypass)."""

    def test_arbitrary_external_gemini_home_rejected(self):
        with patch.dict(os.environ, {"GEMINI_HOME": "/external/attacker/gemini_home"}, clear=False):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_canonical_gemini_home()
            self.assertEqual(ctx.exception.classification, "unverified_identity")
            self.assertIn("Untrusted GEMINI_HOME", ctx.exception.detail)

            with self.assertRaises(AgLaunchError) as ctx:
                verify_auth_identity()
            self.assertEqual(ctx.exception.classification, "unverified_identity")

    def test_another_user_profile_path_rejected(self):
        other_user = "C:\\Users\\AnotherUser\\.gemini" if os.name == "nt" else "/home/anotheruser/.gemini"
        with patch.dict(os.environ, {"GEMINI_HOME": other_user}, clear=False):
            with self.assertRaises(AgLaunchError) as ctx:
                resolve_canonical_gemini_home()
            self.assertEqual(ctx.exception.classification, "unverified_identity")
            self.assertIn("Untrusted GEMINI_HOME", ctx.exception.detail)

    def test_valid_canonical_expected_profile_accepted(self):
        expected = (Path.home().resolve() / ".gemini").resolve()
        with patch.dict(os.environ, {"GEMINI_HOME": str(expected)}, clear=False):
            resolved = resolve_canonical_gemini_home()
            self.assertEqual(resolved, expected)

    def test_equivalent_non_canonical_path_canonicalized(self):
        expected = (Path.home().resolve() / ".gemini").resolve()
        # Equivalent path using traversal / relative parts
        equivalent = str(Path.home().resolve() / "Documents" / ".." / ".gemini")
        with patch.dict(os.environ, {"GEMINI_HOME": equivalent}, clear=False):
            resolved = resolve_canonical_gemini_home()
            self.assertEqual(resolved, expected)

    def test_spawn_env_gemini_home_equals_verified_canonical_path(self):
        expected = (Path.home().resolve() / ".gemini").resolve()
        dirty_env = {"GEMINI_HOME": "/untrusted/inherited/path"}
        # sanitize_ag_environment strips/overrides untrusted GEMINI_HOME with canonical path
        sanitized = sanitize_ag_environment(dirty_env)
        self.assertEqual(sanitized["GEMINI_HOME"], str(expected))

    def test_start_spawn_passes_canonical_gemini_home(self):
        expected = (Path.home().resolve() / ".gemini").resolve()
        mock_resolver = lambda: ("/opt/bin/agy", [])
        mock_auth = lambda: "verified"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 1234
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            runner.start(prepared, "test prompt")
            called_env = mock_popen.call_args[1].get("env", {})
            self.assertEqual(called_env.get("GEMINI_HOME"), str(expected))


class TestAdvFinding2ExpiredLocalCredential(unittest.TestCase):
    """Adversarial regression suite for Finding 2 (P1: Expired local credential accepted)."""

    def test_expired_iso_timestamp_credential_rejected(self):
        fake_path = Path("/mock/oauth_credentials.json")
        expired_data = json.dumps({"access_token": "ya29.old_token", "expiry": "2020-01-01T00:00:00Z"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=expired_data):
            from manager.ag_cli_runner import _parse_local_credential_token
            self.assertFalse(_parse_local_credential_token(fake_path))

    def test_expired_epoch_seconds_credential_rejected(self):
        fake_path = Path("/mock/oauth_credentials.json")
        expired_data = json.dumps({"access_token": "ya29.old_token", "expires_at": 1577836800})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=expired_data):
            from manager.ag_cli_runner import _parse_local_credential_token
            self.assertFalse(_parse_local_credential_token(fake_path))

    def test_malformed_expiry_string_fails_closed(self):
        fake_path = Path("/mock/oauth_credentials.json")
        malformed_data = json.dumps({"access_token": "ya29.token", "expiry": "not-a-valid-date-or-epoch"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=malformed_data):
            from manager.ag_cli_runner import _parse_local_credential_token
            self.assertFalse(_parse_local_credential_token(fake_path))

    def test_malformed_expiry_type_fails_closed(self):
        fake_path = Path("/mock/oauth_credentials.json")
        malformed_data = json.dumps({"access_token": "ya29.token", "expiry": ["invalid", "list"]})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=malformed_data):
            from manager.ag_cli_runner import _parse_local_credential_token
            self.assertFalse(_parse_local_credential_token(fake_path))

    def test_missing_expiry_metadata_downgrades_and_returns_false(self):
        """Token existence alone without verifiable expiry metadata cannot be
        elevated to strong proof -- must downgrade to CLI check or fail closed."""
        fake_path = Path("/mock/oauth_credentials.json")
        no_expiry_data = json.dumps({"access_token": "ya29.token_without_expiry"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=no_expiry_data):
            from manager.ag_cli_runner import _parse_local_credential_token
            self.assertFalse(_parse_local_credential_token(fake_path))

    def test_future_iso_timestamp_credential_allowed(self):
        fake_path = Path("/mock/oauth_credentials.json")
        valid_data = json.dumps({"access_token": "ya29.valid_token", "expiry": "2099-01-01T00:00:00Z"})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=valid_data):
            from manager.ag_cli_runner import _parse_local_credential_token
            self.assertTrue(_parse_local_credential_token(fake_path))

    def test_future_epoch_milliseconds_credential_allowed(self):
        fake_path = Path("/mock/oauth_credentials.json")
        valid_data = json.dumps({"access_token": "ya29.valid_token", "expires_at": 4102444800000})
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.open", new_callable=mock_open, read_data=valid_data):
            from manager.ag_cli_runner import _parse_local_credential_token
            self.assertTrue(_parse_local_credential_token(fake_path))


class TestAdvFinding3CliAuthStatusPositiveProof(unittest.TestCase):
    """Adversarial regression suite for Finding 3 (P1: CLI auth status false-positive)."""

    def _mock_cli_run(self, stdout: str, stderr: str = "", returncode: int = 0):
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.stdout = stdout
        mock_proc.stderr = stderr
        return mock_proc

    def test_blank_output_exit_0_rejected(self):
        mock_res = self._mock_cli_run(stdout="", stderr="", returncode=0)
        with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/opt/bin/agy", [])), \
             patch("subprocess.run", return_value=mock_res):
            from manager.ag_cli_runner import _cli_auth_status_check
            self.assertFalse(_cli_auth_status_check())

    def test_guest_session_text_rejected(self):
        mock_res = self._mock_cli_run(stdout="Guest session active\nSession ID: 12345", returncode=0)
        with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/opt/bin/agy", [])), \
             patch("subprocess.run", return_value=mock_res):
            from manager.ag_cli_runner import _cli_auth_status_check
            self.assertFalse(_cli_auth_status_check())

    def test_guest_session_json_rejected(self):
        mock_res = self._mock_cli_run(stdout=json.dumps({"status": "guest", "authenticated": False}), returncode=0)
        with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/opt/bin/agy", [])), \
             patch("subprocess.run", return_value=mock_res):
            from manager.ag_cli_runner import _cli_auth_status_check
            self.assertFalse(_cli_auth_status_check())

    def test_chinese_unauthenticated_text_rejected(self):
        mock_res = self._mock_cli_run(stdout="目前未登入，請先使用 agy auth login 進行認證", returncode=0)
        with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/opt/bin/agy", [])), \
             patch("subprocess.run", return_value=mock_res):
            from manager.ag_cli_runner import _cli_auth_status_check
            self.assertFalse(_cli_auth_status_check())

    def test_unknown_success_text_rejected(self):
        mock_res = self._mock_cli_run(stdout="Operation completed successfully. Status code 200.", returncode=0)
        with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/opt/bin/agy", [])), \
             patch("subprocess.run", return_value=mock_res):
            from manager.ag_cli_runner import _cli_auth_status_check
            self.assertFalse(_cli_auth_status_check())

    def test_known_authenticated_positive_text_accepted(self):
        mock_res = self._mock_cli_run(stdout="Logged in as developer@gmail.com\nProfile: Default", returncode=0)
        with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/opt/bin/agy", [])), \
             patch("subprocess.run", return_value=mock_res):
            from manager.ag_cli_runner import _cli_auth_status_check
            self.assertTrue(_cli_auth_status_check())

    def test_known_authenticated_positive_json_accepted(self):
        mock_res = self._mock_cli_run(stdout=json.dumps({"authenticated": True, "account": "developer@google.com"}), returncode=0)
        with patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("/opt/bin/agy", [])), \
             patch("subprocess.run", return_value=mock_res):
            from manager.ag_cli_runner import _cli_auth_status_check
            self.assertTrue(_cli_auth_status_check())


class TestAdvR3GcloudAccountAndConfigIsolation(unittest.TestCase):
    """Adversarial regression suite for R3 Finding 1 (P1: gcloud account / config isolation)."""

    def test_r3_cloudsdk_core_account_and_config_stripped(self):
        dirty_env = {
            "CLOUDSDK_CORE_ACCOUNT": "attacker-account@example.com",
            "CLOUDSDK_CONFIG": "/untrusted/gcloud/config/dir",
            "CLOUDSDK_BILLING_QUOTA_PROJECT": "leak-quota-proj",
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": "/untrusted/creds.json",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "sa@attacker.iam.gserviceaccount.com",
            "GOOGLE_CLOUD_QUOTA_PROJECT": "leak-gcp-quota-proj",
            "CLOUDSDK_CORE_PROJECT": "leak-cloudsdk-proj",
            "CLOUDSDK_AUTH_ACCESS_TOKEN": "leak-token",
            "GCLOUD_PROJECT": "leak-legacy-proj",
            "SAFE_VAR": "keep-me",
        }
        clean_env = sanitize_ag_environment(dirty_env)
        for var in (
            "CLOUDSDK_CORE_ACCOUNT",
            "CLOUDSDK_CONFIG",
            "CLOUDSDK_BILLING_QUOTA_PROJECT",
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
            "GOOGLE_CLOUD_QUOTA_PROJECT",
            "CLOUDSDK_CORE_PROJECT",
            "CLOUDSDK_AUTH_ACCESS_TOKEN",
            "GCLOUD_PROJECT",
        ):
            self.assertNotIn(var, clean_env, f"{var} must be stripped from sanitized child env")
        self.assertEqual(clean_env.get("SAFE_VAR"), "keep-me")

    def test_r3_spawn_env_never_reintroduces_cloudsdk_account(self):
        mock_resolver = lambda: ("/opt/bin/agy", [])
        mock_auth = lambda: "verified"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 2345
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch.dict(os.environ, {"CLOUDSDK_CORE_ACCOUNT": "other@example.com", "CLOUDSDK_CONFIG": "/other/config"}):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                runner.start(prepared, "test prompt")
                called_env = mock_popen.call_args[1].get("env", {})
                self.assertNotIn("CLOUDSDK_CORE_ACCOUNT", called_env)
                self.assertNotIn("CLOUDSDK_CONFIG", called_env)


class TestAdvR3OfficialCliRouteAuthenticity(unittest.TestCase):
    """Adversarial regression suite for R3 Finding 2 (P1: Official CLI route authenticity)."""

    def test_r3_temp_dir_fake_agy_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake_agy = Path(td) / ("agy.bat" if os.name == "nt" else "agy")
            fake_agy.write_text("@echo fake", encoding="utf-8")

            with patch("shutil.which", return_value=str(fake_agy)), \
                 patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(AgLaunchError) as ctx:
                    resolve_ag_official_cli_executable()
                self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_r3_arbitrary_path_prepended_fake_agy_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake_bin_dir = Path(td) / "custom_bin"
            fake_bin_dir.mkdir()
            fake_agy = fake_bin_dir / ("agy.exe" if os.name == "nt" else "agy")
            fake_agy.write_text("fake binary", encoding="utf-8")

            with patch("shutil.which", return_value=str(fake_agy)), \
                 patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(AgLaunchError) as ctx:
                    resolve_ag_official_cli_executable()
                self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_r3_unknown_explicit_install_root_rejected(self):
        with self.assertRaises(AgLaunchError) as ctx:
            resolve_ag_official_cli_executable(explicit="/untrusted/custom/path/to/agy")
        self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_r3_trusted_canonical_install_root_accepted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            trusted_root = Path(td) / "trusted_vendor"
            trusted_root.mkdir()
            valid_agy = trusted_root / ("agy.exe" if os.name == "nt" else "agy")
            valid_agy.write_text("valid binary", encoding="utf-8")

            path, prefix = resolve_ag_official_cli_executable(
                explicit=str(valid_agy),
                extra_trusted_roots=[trusted_root],
            )
            self.assertEqual(path, str(valid_agy.resolve()))
            self.assertEqual(prefix, [])

    def test_r3_unverified_binary_never_tagged_official_cli(self):
        """OfficialAgCliRunner default resolver must fail closed when binary is unverified,
        never proceeding to claim AG_OFFICIAL_CLI."""
        runner = OfficialAgCliRunner(auth_verifier=lambda: "verified")
        with patch("shutil.which", return_value="/tmp/untrusted/agy"), \
             patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                runner.prepare(LaunchRequest(working_directory="."))
            self.assertEqual(ctx.exception.classification, "route_unavailable")


class TestAdvR3MaskedFailureSplitFields(unittest.TestCase):
    """Adversarial regression suite for R3 Finding 3 (P2: Masked failure split-field detection)."""

    def _run_with_stdout_lines(self, lines):
        mock_resolver = lambda: ("/opt/bin/agentapi", [])
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=lambda: "verified")
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 8888
        mock_proc.stdout.readline.side_effect = [*lines, ""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "run task")
            return runner.wait(running)

    def test_r3_split_quota_exceeded_detected(self):
        line = json.dumps({"part_a": "quota", "part_b": "exceeded"}) + "\n"
        outcome = self._run_with_stdout_lines([line])
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.failure_classification, "quota_exceeded")

    def test_r3_split_unauthorized_detected(self):
        line = json.dumps({"status_code": 403, "header": "Auth", "body": "unauthorized access denied"}) + "\n"
        outcome = self._run_with_stdout_lines([line])
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.failure_classification, "unauthorized")

    def test_r3_normal_success_payload_not_misidentified(self):
        line = json.dumps({"status": "ok", "part_a": "Analysis complete", "part_b": "All checks passed"}) + "\n"
        outcome = self._run_with_stdout_lines([line])
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(outcome.failure_classification)



class TestAdvR4TrustedRootEnforcement(unittest.TestCase):
    """Adversarial regression suite for R4 P1-A: Trusted-root allowlist enforcement."""

    def test_r4_fake_agy_in_appdata_npm_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            appdata_dir = Path(td) / "AppData" / "Roaming"
            npm_dir = appdata_dir / "npm"
            npm_dir.mkdir(parents=True)
            fake_agy = npm_dir / ("agy.cmd" if os.name == "nt" else "agy")
            fake_agy.write_text("@echo fake agy in npm", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata_dir)}, clear=False), \
                 patch("shutil.which", return_value=str(fake_agy)):
                with self.assertRaises(AgLaunchError) as ctx:
                    resolve_ag_official_cli_executable()
                self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_r4_fake_agy_in_localappdata_programs_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            local_appdata = Path(td) / "AppData" / "Local"
            programs_dir = local_appdata / "Programs"
            programs_dir.mkdir(parents=True)
            fake_agy = programs_dir / ("agy.exe" if os.name == "nt" else "agy")
            fake_agy.write_text("fake binary in localappdata", encoding="utf-8")

            with patch.dict(os.environ, {"LOCALAPPDATA": str(local_appdata)}, clear=False), \
                 patch("shutil.which", return_value=str(fake_agy)):
                with self.assertRaises(AgLaunchError) as ctx:
                    resolve_ag_official_cli_executable()
                self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_r4_fake_agy_in_gemini_home_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            gemini_home = Path(td) / ".gemini"
            bin_dir = gemini_home / "antigravity-ide" / "bin"
            bin_dir.mkdir(parents=True)
            fake_agy = bin_dir / ("agy.cmd" if os.name == "nt" else "agy")
            fake_agy.write_text("@echo fake agy in gemini home", encoding="utf-8")

            with patch("manager.ag_cli_runner.resolve_canonical_gemini_home", return_value=gemini_home), \
                 patch("shutil.which", return_value=str(fake_agy)):
                with self.assertRaises(AgLaunchError) as ctx:
                    resolve_ag_official_cli_executable()
                self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_r4_arbitrary_path_fake_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake_dir = Path(td) / "user_custom_bin"
            fake_dir.mkdir(parents=True)
            fake_agy = fake_dir / ("agy.exe" if os.name == "nt" else "agy")
            fake_agy.write_text("fake binary", encoding="utf-8")

            with patch("shutil.which", return_value=str(fake_agy)), \
                 patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(AgLaunchError) as ctx:
                    resolve_ag_official_cli_executable()
                self.assertEqual(ctx.exception.classification, "route_unavailable")

    def test_r4_remaining_trusted_system_root_mock_accepted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sys_root = Path(td) / "ProgramFiles"
            sys_root.mkdir(parents=True)
            valid_agy = sys_root / ("agy.exe" if os.name == "nt" else "agy")
            valid_agy.write_text("valid system binary", encoding="utf-8")

            path, prefix = resolve_ag_official_cli_executable(
                explicit=str(valid_agy),
                extra_trusted_roots=[sys_root],
            )
            self.assertEqual(path, str(valid_agy.resolve()))
            self.assertEqual(prefix, [])

    def test_r4_unverified_executable_never_tagged_official_cli(self):
        runner = OfficialAgCliRunner(auth_verifier=lambda: "verified")
        with patch("shutil.which", return_value="/untrusted/user/bin/agy"), \
             patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AgLaunchError) as ctx:
                runner.prepare(LaunchRequest(working_directory="."))
            self.assertEqual(ctx.exception.classification, "route_unavailable")


class TestAdvR4CloudsdkAccessTokenFileSanitization(unittest.TestCase):
    """Adversarial regression suite for R4 P1-B: CLOUDSDK_AUTH_ACCESS_TOKEN_FILE sanitization."""

    def test_r4_cloudsdk_auth_access_token_file_stripped(self):
        dirty_env = {
            "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": "/path/to/untrusted/token.json",
            "CLOUDSDK_AUTH_ACCESS_TOKEN": "untrusted-token-value",
            "CLOUDSDK_CORE_ACCOUNT": "untrusted@attacker.com",
            "CLOUDSDK_CONFIG": "/path/to/untrusted/gcloud/config",
            "SAFE_SYSTEM_VAR": "preserved",
        }
        clean_env = sanitize_ag_environment(dirty_env)
        self.assertNotIn("CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", clean_env)
        self.assertNotIn("CLOUDSDK_AUTH_ACCESS_TOKEN", clean_env)
        self.assertNotIn("CLOUDSDK_CORE_ACCOUNT", clean_env)
        self.assertNotIn("CLOUDSDK_CONFIG", clean_env)
        self.assertEqual(clean_env.get("SAFE_SYSTEM_VAR"), "preserved")

    def test_r4_cloudsdk_auth_access_token_file_not_reintroduced_in_spawn(self):
        mock_resolver = lambda: ("/opt/bin/agy", [])
        mock_auth = lambda: "verified"
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=mock_auth)
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch.dict(os.environ, {"CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": "/tmp/secret_token.json"}):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                runner.start(prepared, "run prompt")
                called_env = mock_popen.call_args[1].get("env", {})
                self.assertNotIn("CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", called_env)

    def test_r4_all_secondary_billing_vars_stripped_without_regression(self):
        from manager.ag_cli_runner import SECONDARY_BILLING_ENV_VARS

        self.assertIn("CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", SECONDARY_BILLING_ENV_VARS)
        self.assertIn("CLOUDSDK_AUTH_ACCESS_TOKEN", SECONDARY_BILLING_ENV_VARS)
        self.assertIn("CLOUDSDK_CORE_ACCOUNT", SECONDARY_BILLING_ENV_VARS)
        self.assertIn("CLOUDSDK_CONFIG", SECONDARY_BILLING_ENV_VARS)
        self.assertIn("CLOUDSDK_BILLING_QUOTA_PROJECT", SECONDARY_BILLING_ENV_VARS)
        self.assertIn("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", SECONDARY_BILLING_ENV_VARS)
        self.assertIn("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", SECONDARY_BILLING_ENV_VARS)
        self.assertIn("GOOGLE_CLOUD_QUOTA_PROJECT", SECONDARY_BILLING_ENV_VARS)


class TestAdvR4MaskedFailureParser(unittest.TestCase):
    """Adversarial regression suite for R4 P2: Enhanced Masked Failure Parser."""

    def _run_with_stdout_lines(self, lines):
        mock_resolver = lambda: ("/opt/bin/agentapi", [])
        runner = OfficialAgCliRunner(executable_resolver=mock_resolver, auth_verifier=lambda: "verified")
        req = LaunchRequest(working_directory=".")
        prepared = runner.prepare(req)

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.stdout.readline.side_effect = [*lines, ""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            running = runner.start(prepared, "run task")
            return runner.wait(running)

    def test_r4_nested_dict_and_list_quota_exceeded_detected(self):
        payload = {
            "level1": {
                "details": [
                    {"code": 429, "type": "quota", "message": "exceeded by caller"}
                ]
            }
        }
        outcome = self._run_with_stdout_lines([json.dumps(payload) + "\n"])
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.failure_classification, "quota_exceeded")

    def test_r4_punctuation_split_quota_exceeded_detected(self):
        for val in ["quota:exceeded", "quota-exceeded", "quota_exceeded", "quota / exceeded"]:
            payload = {"error_reason": val}
            outcome = self._run_with_stdout_lines([json.dumps(payload) + "\n"])
            self.assertEqual(outcome.status, "failed", f"Failed to detect {val}")
            self.assertEqual(outcome.failure_classification, "quota_exceeded", f"Failed classification on {val}")

    def test_r4_three_way_split_quota_exceeded_detected(self):
        for phrase in ["quota limit exceeded", "quota has been exceeded", "quota is exceeded"]:
            payload = {"message": phrase}
            outcome = self._run_with_stdout_lines([json.dumps(payload) + "\n"])
            self.assertEqual(outcome.status, "failed", f"Failed to detect {phrase}")
            self.assertEqual(outcome.failure_classification, "quota_exceeded")

    def test_r4_unauthorized_detected_without_substring_false_positive(self):
        # Detected unauthorized
        unauth_payload = {"error": "unauthorized access attempt"}
        outcome = self._run_with_stdout_lines([json.dumps(unauth_payload) + "\n"])
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.failure_classification, "unauthorized")

        # Benign word containing 'author' should NOT fail
        benign_payload = {"status": "ok", "author": "John Doe", "authority": "standard"}
        outcome2 = self._run_with_stdout_lines([json.dumps(benign_payload) + "\n"])
        self.assertEqual(outcome2.status, "completed")
        self.assertIsNone(outcome2.failure_classification)

if __name__ == "__main__":
    unittest.main()
