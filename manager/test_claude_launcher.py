import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from manager.claude_launcher import (
    ClaudeLaunchError, ClaudeLauncher, PreparedLaunch,
    check_claude_auth_ready, resolve_claude_executable, _build_argv,
    _encode_stream_json_input, _extract_stream_json_result, _permission_profile,
    _read_output_text,
)
from manager.codex_launcher import LaunchOutcome, LaunchRequest, RunningLaunch, process_creation_identity


class FakeProcess:
    """Minimal process double. pid defaults to this real test process's own
    pid so process_creation_identity() succeeds against a genuinely live OS
    process, exercising the real Windows/Linux identity code exactly like
    test_codex_launcher.py's FakeProcess does -- no ctypes mocking needed."""

    def __init__(self, pid=None, exit_immediately_with=None):
        self.pid = pid if pid is not None else os.getpid()
        self.returncode = exit_immediately_with
        self.stdin = _FakeStdin()
        self.terminate_count = self.kill_count = 0
        self.wait_timeout = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_count += 1
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.kill_count += 1
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.returncode


class _FakeStdin:
    def __init__(self, fail_write=False, fail_flush=False):
        self.closed = False
        self.written = []
        self.flush_count = 0
        self.fail_write = fail_write
        self.fail_flush = fail_flush

    def write(self, data):
        if self.fail_write:
            raise BrokenPipeError("simulated broken pipe on write")
        self.written.append(data)
        return len(data)

    def flush(self):
        if self.fail_flush:
            raise OSError("simulated broken pipe on flush")
        self.flush_count += 1

    def close(self):
        self.closed = True


class ClaudeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.temp.name).resolve())
        self.calls = []
        self.process = None

    def tearDown(self):
        self.temp.cleanup()

    def _popen(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.process

    def launcher(self, log_dir=None, auth_check=None):
        return ClaudeLauncher(executable=__file__, popen=self._popen, log_dir=log_dir or self.temp.name,
                               auth_check=auth_check or (lambda *a, **k: True))

    def request(self, cwd=None, model="claude-sonnet-5", sandbox="read-only", approval_policy="never"):
        return LaunchRequest(cwd or self.cwd, model=model, sandbox=sandbox, approval_policy=approval_policy)

    def prepare(self, pid=None, exit_immediately_with=None, **request_kwargs):
        self.process = FakeProcess(pid=pid, exit_immediately_with=exit_immediately_with)
        return self.launcher().prepare(self.request(**request_kwargs))

    # 1. session UUID pre-generated + 3. provider_session_id == UUID
    def test_session_id_is_pre_generated_valid_uuid_and_matches_provider_session_id(self):
        prepared = self.prepare()
        parsed = uuid.UUID(prepared.provider_session_id)
        self.assertEqual(str(parsed), prepared.provider_session_id)

    # 2. --session-id correctly in argv
    def test_session_id_flag_present_in_argv_matching_provider_session_id(self):
        prepared = self.prepare()
        self.assertIn("--session-id", prepared.argv)
        idx = prepared.argv.index("--session-id")
        self.assertEqual(prepared.argv[idx + 1], prepared.provider_session_id)

    # 4. model flag correct
    def test_model_flag_passed_through(self):
        prepared = self.prepare(model="claude-opus-5")
        self.assertIn("--model", prepared.argv)
        idx = prepared.argv.index("--model")
        self.assertEqual(prepared.argv[idx + 1], "claude-opus-5")
        self.assertEqual(prepared.model, "claude-opus-5")

    def test_model_flag_omitted_when_not_requested(self):
        prepared = self.prepare(model=None)
        self.assertNotIn("--model", prepared.argv)

    # 5. cwd correct passed to subprocess
    def test_cwd_passed_to_subprocess(self):
        prepared = self.prepare()
        _, kwargs = self.calls[0]
        self.assertEqual(kwargs["cwd"], self.cwd)
        self.assertEqual(prepared.cwd, self.cwd)

    # 6. no shell=True
    def test_never_uses_shell_true_and_argv_is_a_list(self):
        args, kwargs = self.calls[0] if self.calls else (None, None)
        self.prepare()
        args, kwargs = self.calls[-1]
        self.assertFalse(kwargs.get("shell", False))
        self.assertIsInstance(args[0], list)
        for element in args[0]:
            self.assertIsInstance(element, str)

    # 7. read-only permission mapping
    def test_read_only_profile_maps_to_plan_mode_with_safe_tools(self):
        prepared = self.prepare(sandbox="read-only", approval_policy="never")
        self.assertEqual(prepared.mode, "plan")
        self.assertIn("--permission-mode", prepared.argv)
        idx = prepared.argv.index("--permission-mode")
        self.assertEqual(prepared.argv[idx + 1], "plan")
        self.assertIn("--allowed-tools", prepared.argv)
        tools_idx = prepared.argv.index("--allowed-tools")
        tools = prepared.argv[tools_idx + 1].split(",")
        for dangerous in ("Edit", "Write", "Bash", "NotebookEdit"):
            self.assertNotIn(dangerous, tools)

    # 8. production_write fail closed (and any other unrecognized profile)
    def test_production_write_shape_fails_closed(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.prepare(sandbox=None, approval_policy=None)
        self.assertEqual(ctx.exception.classification, "unsupported_policy")

    def test_unknown_sandbox_value_fails_closed_not_permissive(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.prepare(sandbox="workspace-write", approval_policy="never")
        self.assertEqual(ctx.exception.classification, "unsupported_policy")

    def test_no_process_spawned_when_policy_unsupported(self):
        with self.assertRaises(ClaudeLaunchError):
            self.prepare(sandbox="danger-full-access", approval_policy="never")
        self.assertEqual(self.calls, [])

    # 9. process identity captured
    def test_process_identity_captured_matches_real_os_process(self):
        prepared = self.prepare(pid=os.getpid())
        expected = process_creation_identity(os.getpid())
        self.assertEqual(prepared.process_creation_identity, expected)
        self.assertIsNotNone(prepared.process_creation_identity)
        self.assertEqual(prepared.pid, os.getpid())

    # missing process identity: pid that cannot be verified as live -> fail closed, process killed
    def test_missing_process_identity_fails_closed_and_kills_process(self):
        unlikely_pid = 999_999_999
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.prepare(pid=unlikely_pid)
        self.assertEqual(ctx.exception.classification, "protocol_error")
        self.assertEqual(self.process.kill_count, 1)
        self.assertTrue(self.process.stdin.closed)

    # 10. executable missing failure
    def test_executable_missing_raises_without_spawning(self):
        launcher = ClaudeLauncher(executable=str(Path(self.temp.name) / "no-such-claude-binary"),
                                   popen=self._popen)
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.prepare(self.request())
        self.assertEqual(ctx.exception.classification, "executable_not_found")
        self.assertEqual(self.calls, [])

    def test_resolve_executable_prefers_explicit_path(self):
        self.assertEqual(str(Path(__file__).resolve()), resolve_claude_executable(__file__))

    # 11. spawn failure
    def test_spawn_failure_raises_and_leaves_no_prepared_launch(self):
        def failing_popen(*args, **kwargs):
            raise OSError("no such file or directory")
        launcher = ClaudeLauncher(executable=__file__, popen=failing_popen, log_dir=self.temp.name,
                                  auth_check=lambda *a, **k: True)
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.prepare(self.request())
        self.assertEqual(ctx.exception.classification, "spawn_failed")

    # 12. immediate process failure
    def test_process_exits_immediately_fails_closed(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.prepare(exit_immediately_with=1)
        self.assertEqual(ctx.exception.classification, "spawn_failed")
        self.assertIn("exited immediately", ctx.exception.detail)

    # 13. argv/path-with-spaces safety
    def test_working_directory_with_spaces_is_a_single_argv_element_not_concatenated(self):
        spaced = Path(self.temp.name) / "dir with spaces"
        spaced.mkdir()
        prepared = self.prepare(cwd=str(spaced))
        _, kwargs = self.calls[-1]
        self.assertEqual(kwargs["cwd"], str(spaced))
        # cwd must never be folded into the argv command line itself
        self.assertNotIn(str(spaced), prepared.argv)

    def test_invalid_working_directory_raises_before_spawn(self):
        launcher = self.launcher()
        missing = str(Path(self.temp.name) / "does-not-exist")
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.prepare(LaunchRequest(missing, sandbox="read-only", approval_policy="never"))
        self.assertEqual(ctx.exception.classification, "invalid_request")
        self.assertEqual(self.calls, [])

    # 14. PreparedLaunch shape compatible with the existing watcher-facing contract
    def test_prepared_launch_shape_has_required_evidence_fields(self):
        prepared = self.prepare()
        self.assertIsInstance(prepared, PreparedLaunch)
        for attr in ("provider", "provider_session_id", "pid", "process_creation_identity",
                     "cwd", "branch", "prepared_at", "model", "mode", "argv",
                     "stdout_path", "stderr_path", "session_path"):
            self.assertTrue(hasattr(prepared, attr))
        self.assertEqual(prepared.provider, "claude")

    def test_session_path_is_deterministic_and_includes_session_id(self):
        prepared = self.prepare()
        self.assertIsNotNone(prepared.session_path)
        self.assertIn(prepared.provider_session_id, prepared.session_path)
        self.assertTrue(prepared.session_path.endswith(".jsonl"))

    def test_branch_evidence_threaded_through_when_supplied(self):
        self.process = FakeProcess()
        prepared = self.launcher().prepare(self.request(), branch="feature/example")
        self.assertEqual(prepared.branch, "feature/example")

    def test_no_account_id_or_config_dir_is_fully_backward_compatible(self):
        # Default (single-account) behavior: env=None means the child
        # inherits this process's environment unchanged, exactly as before
        # account_id/config_dir existed.
        prepared = self.prepare()
        _, kwargs = self.calls[-1]
        self.assertIsNone(kwargs["env"])
        self.assertIsNone(prepared.account_id)
        self.assertIsNone(prepared.config_dir)

    def test_config_dir_sets_claude_config_dir_in_child_env_only(self):
        self.process = FakeProcess()
        prepared = self.launcher().prepare(
            self.request(), account_id="account-b", config_dir=r"C:\accounts\b\.claude",
        )
        _, kwargs = self.calls[-1]
        env = kwargs["env"]
        self.assertIsNotNone(env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], r"C:\accounts\b\.claude")
        # Every other inherited variable must survive untouched.
        for key, value in os.environ.items():
            if key != "CLAUDE_CONFIG_DIR":
                self.assertEqual(env.get(key), value)
        self.assertEqual(prepared.account_id, "account-b")
        self.assertEqual(prepared.config_dir, r"C:\accounts\b\.claude")

    def test_two_accounts_get_isolated_non_overlapping_envs(self):
        self.process = FakeProcess()
        launcher = self.launcher()
        prepared_a = launcher.prepare(self.request(), account_id="account-a", config_dir=r"C:\accounts\a\.claude")
        self.process = FakeProcess()
        prepared_b = launcher.prepare(self.request(), account_id="account-b", config_dir=r"C:\accounts\b\.claude")
        env_a = self.calls[-2][1]["env"]
        env_b = self.calls[-1][1]["env"]
        self.assertNotEqual(env_a["CLAUDE_CONFIG_DIR"], env_b["CLAUDE_CONFIG_DIR"])
        self.assertNotEqual(prepared_a.provider_session_id, prepared_b.provider_session_id)
        self.assertNotEqual(prepared_a.account_id, prepared_b.account_id)

    def test_empty_config_dir_fails_closed(self):
        launcher = self.launcher()
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.prepare(self.request(), config_dir="   ")
        self.assertEqual(ctx.exception.classification, "invalid_request")
        self.assertEqual(self.calls, [])

    def test_stdout_stderr_use_file_sink_not_pipe(self):
        prepared = self.prepare()
        _, kwargs = self.calls[-1]
        # PIPE would risk a deadlock if the child fills the buffer with no reader;
        # a file sink avoids that entirely.
        self.assertNotEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertNotEqual(kwargs["stderr"], subprocess.PIPE)
        self.assertTrue(Path(prepared.stdout_path).exists())
        self.assertTrue(Path(prepared.stderr_path).exists())

    def test_close_terminates_still_running_process_idempotently(self):
        prepared = self.prepare()
        launcher = self.launcher()
        launcher.close(prepared)
        self.assertEqual(self.process.kill_count, 1)
        launcher.close(prepared)  # idempotent: no double-kill
        self.assertEqual(self.process.kill_count, 1)

    def test_close_is_noop_for_already_exited_process(self):
        self.process = FakeProcess()
        prepared = self.launcher().prepare(self.request())
        self.process.returncode = 0
        launcher = self.launcher()
        launcher.close(prepared)
        self.assertEqual(self.process.kill_count, 0)


class AuthPreflightTests(unittest.TestCase):
    """P0.2: an account can be registered/enabled/config_dir-set yet not
    actually logged in (never authenticated, expired, logged out). These
    tests cover the FAIL-before contract for a preflight authentication
    readiness gate in ClaudeLauncher.prepare(), before P0.2 implementation
    exists -- every test in this class is expected to fail against baseline
    ed1d40e (no `auth_check` parameter, no authentication_* classifications)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.temp.name).resolve())
        self.calls = []
        self.process = None
        self.auth_check_calls = []

    def tearDown(self):
        self.temp.cleanup()

    def _popen(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.process

    def _recording_auth_check(self, result):
        def _check(executable, env):
            self.auth_check_calls.append((executable, env))
            if isinstance(result, Exception):
                raise result
            return result
        return _check

    def launcher(self, auth_check):
        return ClaudeLauncher(executable=__file__, popen=self._popen, log_dir=self.temp.name, auth_check=auth_check)

    def request(self, cwd=None, model="claude-sonnet-5"):
        return LaunchRequest(cwd or self.cwd, model=model, sandbox="read-only", approval_policy="never")

    # Case 1: registered + enabled + auth ready -> normal launch, unaffected.
    def test_auth_ready_allows_normal_launch(self):
        self.process = FakeProcess()
        prepared = self.launcher(self._recording_auth_check(True)).prepare(
            self.request(), account_id="account-a", config_dir=r"C:\accounts\a\.claude",
        )
        self.assertEqual("account-a", prepared.account_id)
        self.assertEqual(1, len(self.calls))  # the real task subprocess was spawned

    # Case 2: registered + enabled + auth unavailable -> fail closed before the
    # real task subprocess is ever spawned, classified distinctly.
    def test_auth_unavailable_fails_closed_before_spawn(self):
        self.process = FakeProcess()
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.launcher(self._recording_auth_check(False)).prepare(
                self.request(), account_id="account-a", config_dir=r"C:\accounts\a\.claude",
            )
        self.assertEqual("authentication_unavailable", ctx.exception.classification)
        self.assertEqual([], self.calls)  # no task subprocess spawned

    # Case 3: Account B auth failure -> never falls back to Account A. Since
    # prepare() is called once per resolved account, proving no task
    # subprocess was spawned proves no fallback launch happened either.
    def test_account_b_auth_failure_does_not_fall_back_to_account_a(self):
        self.process = FakeProcess()
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.launcher(self._recording_auth_check(False)).prepare(
                self.request(), account_id="account-b", config_dir=r"C:\accounts\b\.claude",
            )
        self.assertEqual("authentication_unavailable", ctx.exception.classification)
        self.assertEqual([], self.calls)
        self.assertEqual(1, len(self.auth_check_calls))  # only account B's env was ever checked
        self.assertEqual(r"C:\accounts\b\.claude", self.auth_check_calls[0][1]["CLAUDE_CONFIG_DIR"])

    # Case 4: symmetric -- Account A auth failure never falls back to B.
    def test_account_a_auth_failure_does_not_fall_back_to_account_b(self):
        self.process = FakeProcess()
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.launcher(self._recording_auth_check(False)).prepare(
                self.request(), account_id="account-a", config_dir=r"C:\accounts\a\.claude",
            )
        self.assertEqual("authentication_unavailable", ctx.exception.classification)
        self.assertEqual([], self.calls)
        self.assertEqual(r"C:\accounts\a\.claude", self.auth_check_calls[0][1]["CLAUDE_CONFIG_DIR"])

    # Case 5/6: the auth gate takes no quota input at all -- it cannot be
    # confused by unknown or stale quota confidence, because prepare()/
    # check_claude_auth_ready never receive a quota document or confidence
    # value. Auth-ready must allow the launch regardless.
    def test_auth_ready_allows_launch_with_no_quota_context_supplied(self):
        self.process = FakeProcess()
        prepared = self.launcher(self._recording_auth_check(True)).prepare(self.request())
        self.assertIsNotNone(prepared)
        # the injected auth_check signature carries no quota/confidence argument
        executable, env = self.auth_check_calls[0]
        self.assertIsInstance(executable, str)

    # Case 8: legacy single-account path (no account_id/config_dir) keeps
    # working when auth is ready, and is now also protected when it is not --
    # extending coverage, not breaking the existing default behavior.
    def test_legacy_single_account_path_auth_ready_unaffected(self):
        self.process = FakeProcess()
        prepared = self.launcher(self._recording_auth_check(True)).prepare(self.request())
        self.assertIsNone(prepared.account_id)
        self.assertIsNone(prepared.config_dir)
        self.assertIsNone(self.auth_check_calls[0][1])  # env=None: ambient/default config dir checked

    def test_legacy_single_account_path_auth_unavailable_fails_closed(self):
        self.process = FakeProcess()
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.launcher(self._recording_auth_check(False)).prepare(self.request())
        self.assertEqual("authentication_unavailable", ctx.exception.classification)
        self.assertEqual([], self.calls)

    # Case 9: a generic crash after a ready auth check must keep its own
    # classification -- never mislabeled as an authentication failure.
    def test_generic_spawn_crash_after_auth_ready_is_not_mislabeled_as_auth_failure(self):
        def failing_popen(*args, **kwargs):
            raise OSError("no such file or directory")
        launcher = ClaudeLauncher(executable=__file__, popen=failing_popen, log_dir=self.temp.name,
                                  auth_check=self._recording_auth_check(True))
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.prepare(self.request())
        self.assertEqual("spawn_failed", ctx.exception.classification)
        self.assertEqual(1, len(self.auth_check_calls))  # the gate ran, and then allowed the real attempt

    def test_immediate_process_exit_after_auth_ready_is_not_mislabeled_as_auth_failure(self):
        self.process = FakeProcess(exit_immediately_with=1)
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.launcher(self._recording_auth_check(True)).prepare(self.request())
        self.assertEqual("spawn_failed", ctx.exception.classification)

    # Ambiguous/unverifiable auth-check outcomes fail closed too, but under a
    # distinct classification from a confirmed "not logged in" -- so ops can
    # tell "definitely not authenticated" apart from "could not determine".
    def test_auth_check_raising_ambiguous_error_fails_closed_distinctly(self):
        self.process = FakeProcess()
        ambiguous = ClaudeLaunchError("authentication_check_failed", "Claude auth status check returned unparseable output")
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.launcher(self._recording_auth_check(ambiguous)).prepare(self.request())
        self.assertEqual("authentication_check_failed", ctx.exception.classification)
        self.assertEqual([], self.calls)

    # Case 10: credential/config contents never leak into the raised error.
    def test_authentication_unavailable_error_never_leaks_config_dir(self):
        self.process = FakeProcess()
        secret_looking_dir = r"C:\accounts\b\.claude-secret-profile-xyz"
        with self.assertRaises(ClaudeLaunchError) as ctx:
            self.launcher(self._recording_auth_check(False)).prepare(
                self.request(), account_id="account-b", config_dir=secret_looking_dir,
            )
        self.assertNotIn(secret_looking_dir, ctx.exception.detail)
        self.assertNotIn("token", ctx.exception.detail.lower())
        self.assertNotIn("secret", ctx.exception.detail.lower())

    # No account is available to preflight-check without an explicit gate:
    # this documents that the auth gate runs strictly before any UUID/log
    # file allocation, keeping a rejected launch side-effect-free.
    def test_auth_gate_runs_before_session_id_and_log_file_allocation(self):
        self.process = FakeProcess()
        launcher = self.launcher(self._recording_auth_check(False))
        with self.assertRaises(ClaudeLaunchError):
            launcher.prepare(self.request())
        self.assertEqual(0, len(list(Path(self.temp.name).glob("claude-*.stdout.log"))))


class CheckClaudeAuthReadyTests(unittest.TestCase):
    """Direct unit tests for the standalone auth-status subprocess wrapper,
    with `run` fully injected (no real `claude` invocation)."""

    class _CompletedProcess:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def _run(self, result):
        self.run_calls = []

        def _fake_run(argv, **kwargs):
            self.run_calls.append((argv, kwargs))
            if isinstance(result, Exception):
                raise result
            return result
        return _fake_run

    def test_logged_in_true_returns_true(self):
        result = check_claude_auth_ready(
            "claude.exe", {"CLAUDE_CONFIG_DIR": "x"},
            run=self._run(self._CompletedProcess(0, json.dumps({"loggedIn": True, "email": "user@example.com"}))),
        )
        self.assertTrue(result)

    def test_logged_in_false_returns_false(self):
        result = check_claude_auth_ready(
            "claude.exe", None,
            run=self._run(self._CompletedProcess(1, json.dumps({"loggedIn": False, "authMethod": "none"}))),
        )
        self.assertFalse(result)

    def test_forwards_executable_and_env_to_run_unchanged(self):
        env = {"CLAUDE_CONFIG_DIR": r"C:\accounts\b\.claude"}
        check_claude_auth_ready("claude.exe", env, run=self._run(self._CompletedProcess(0, json.dumps({"loggedIn": True}))))
        argv, kwargs = self.run_calls[0]
        self.assertEqual(["claude.exe", "auth", "status", "--json"], argv)
        self.assertIs(kwargs["env"], env)
        self.assertFalse(kwargs.get("shell", False))

    def test_unexpected_exit_code_fails_closed_distinctly(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready("claude.exe", None, run=self._run(self._CompletedProcess(2, "{}")))
        self.assertEqual("authentication_check_failed", ctx.exception.classification)

    def test_unparseable_output_fails_closed_and_does_not_echo_it(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready("claude.exe", None, run=self._run(self._CompletedProcess(0, "not json at all")))
        self.assertEqual("authentication_check_failed", ctx.exception.classification)
        self.assertNotIn("not json at all", ctx.exception.detail)

    def test_missing_logged_in_field_fails_closed(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready("claude.exe", None, run=self._run(self._CompletedProcess(0, json.dumps({"authMethod": "none"}))))
        self.assertEqual("authentication_check_failed", ctx.exception.classification)

    def test_subprocess_timeout_fails_closed(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready("claude.exe", None, run=self._run(subprocess.TimeoutExpired("claude", 10)))
        self.assertEqual("authentication_check_failed", ctx.exception.classification)

    def test_subprocess_os_error_fails_closed(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready("claude.exe", None, run=self._run(OSError("no such file or directory")))
        self.assertEqual("authentication_check_failed", ctx.exception.classification)

    def test_error_detail_never_contains_raw_stdout_payload(self):
        payload = json.dumps({"loggedIn": True, "email": "user@example.com", "orgId": "org-secret-id"})
        # A malformed shape (loggedIn not a bool) still must not echo the payload.
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready("claude.exe", None, run=self._run(self._CompletedProcess(0, json.dumps({"loggedIn": "yes"}))))
        self.assertNotIn("user@example.com", ctx.exception.detail)
        self.assertNotIn("org-secret-id", ctx.exception.detail)
        self.assertNotIn(payload, ctx.exception.detail)

    # Only two exit-code/body combinations are ever self-consistent: a
    # successful check (rc=0) reporting loggedIn=True, and a clean "not
    # logged in" check (rc=1) reporting loggedIn=False. Every other
    # combination -- including the two below, where the process exit code
    # and the reported loggedIn boolean disagree -- must fail closed with
    # "authentication_check_failed" rather than trust either signal alone.

    def test_exit_1_with_logged_in_true_fails_closed_not_ready(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready(
                "claude.exe", None,
                run=self._run(self._CompletedProcess(1, json.dumps({"loggedIn": True}))),
            )
        self.assertEqual("authentication_check_failed", ctx.exception.classification)

    def test_exit_0_with_logged_in_false_fails_closed(self):
        with self.assertRaises(ClaudeLaunchError) as ctx:
            check_claude_auth_ready(
                "claude.exe", None,
                run=self._run(self._CompletedProcess(0, json.dumps({"loggedIn": False}))),
            )
        self.assertEqual("authentication_check_failed", ctx.exception.classification)


class StartWaitTests(unittest.TestCase):
    """start()/wait() -- the execution half of the launcher, all against a
    mocked process/file sink; no real Claude invocation this round."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.temp.name).resolve())
        self.process = None

    def tearDown(self):
        self.temp.cleanup()

    def _popen(self, *args, **kwargs):
        return self.process

    def launcher(self):
        return ClaudeLauncher(executable=__file__, popen=self._popen, log_dir=self.temp.name,
                               auth_check=lambda *a, **k: True)

    def request(self):
        return LaunchRequest(self.cwd, model="claude-sonnet-5", sandbox="read-only", approval_policy="never")

    def prepared(self, fail_write=False, fail_flush=False):
        self.process = FakeProcess(pid=os.getpid())
        self.process.stdin = _FakeStdin(fail_write=fail_write, fail_flush=fail_flush)
        launcher = self.launcher()
        return launcher, launcher.prepare(self.request())

    @staticmethod
    def write_result_line(prepared, event):
        with open(prepared.stdout_path, "ab") as handle:
            handle.write((json.dumps(event) + "\n").encode("utf-8"))

    # 1. start writes the correct stream-json frame
    def test_start_writes_correct_stream_json_frame(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "do the task")
        self.assertEqual(1, len(self.process.stdin.written))
        frame = json.loads(self.process.stdin.written[0].decode("utf-8"))
        self.assertEqual(
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "do the task"}]}},
            frame,
        )
        self.assertIsInstance(running, RunningLaunch)
        self.assertIs(running.prepared, prepared)

    def test_start_frame_matches_encode_helper_exactly(self):
        launcher, prepared = self.prepared()
        launcher.start(prepared, "consistent encoding")
        self.assertEqual(_encode_stream_json_input("consistent encoding"), self.process.stdin.written[0])

    # 2. Unicode/multiline prompt
    def test_unicode_and_multiline_prompt_survive_encoding_as_one_ndjson_line(self):
        launcher, prepared = self.prepared()
        prompt = "line one\nline two 你好 emoji \U0001F389 \"quoted\""
        launcher.start(prepared, prompt)
        raw = self.process.stdin.written[0]
        self.assertEqual(1, raw.count(b"\n"))  # exactly one trailing newline: one ndjson line
        frame = json.loads(raw.decode("utf-8"))
        self.assertEqual(prompt, frame["message"]["content"][0]["text"])

    # 3. stdin flush/close semantics
    def test_stdin_flushed_and_closed_after_start(self):
        launcher, prepared = self.prepared()
        launcher.start(prepared, "task")
        self.assertEqual(1, self.process.stdin.flush_count)
        self.assertTrue(self.process.stdin.closed)

    # 4. broken pipe
    def test_broken_pipe_on_write_fails_closed_and_kills_process(self):
        launcher, prepared = self.prepared(fail_write=True)
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.start(prepared, "task")
        self.assertEqual("spawn_failed", ctx.exception.classification)
        self.assertEqual(1, self.process.kill_count)

    def test_broken_pipe_on_flush_fails_closed_and_kills_process(self):
        launcher, prepared = self.prepared(fail_flush=True)
        with self.assertRaises(ClaudeLaunchError):
            launcher.start(prepared, "task")
        self.assertEqual(1, self.process.kill_count)

    # 5. process already exited before start
    def test_process_already_exited_before_start_fails_closed(self):
        launcher, prepared = self.prepared()
        self.process.returncode = 1
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.start(prepared, "task")
        self.assertEqual("protocol_error", ctx.exception.classification)

    def test_start_twice_raises_invalid_state(self):
        launcher, prepared = self.prepared()
        launcher.start(prepared, "task")
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.start(prepared, "task again")
        self.assertEqual("invalid_state", ctx.exception.classification)

    def test_start_rejects_empty_prompt(self):
        launcher, prepared = self.prepared()
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.start(prepared, "   ")
        self.assertEqual("invalid_request", ctx.exception.classification)

    # 6. wait success exit=0
    def test_wait_success_returns_completed_outcome(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        self.write_result_line(prepared, {"type": "system", "subtype": "init"})
        self.write_result_line(prepared, {"type": "result", "is_error": False,
                                          "session_id": prepared.provider_session_id, "result": "done"})
        self.process.returncode = 0
        outcome = launcher.wait(running)
        self.assertIsInstance(outcome, LaunchOutcome)
        self.assertEqual("completed", outcome.status)
        self.assertIsNone(outcome.failure_classification)

    # 7. wait nonzero exit
    def test_wait_nonzero_exit_fails_closed(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        self.process.returncode = 1
        outcome = launcher.wait(running)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("provider_error", outcome.failure_classification)

    # 8. malformed output
    def test_wait_malformed_output_classified_and_fails_closed(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        with open(prepared.stdout_path, "ab") as handle:
            handle.write(b"not valid json at all\n{also not json}\n")
        self.process.returncode = 0
        outcome = launcher.wait(running)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("malformed_output", outcome.failure_classification)

    # 9. empty output
    def test_wait_empty_output_classified_and_fails_closed(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        self.process.returncode = 0
        outcome = launcher.wait(running)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("malformed_output", outcome.failure_classification)

    # 10. final result extraction: last result event wins over earlier noise
    def test_wait_extracts_the_last_result_event_among_stream_noise(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        self.write_result_line(prepared, {"type": "assistant", "message": {"content": []}})
        self.write_result_line(prepared, {"type": "result", "is_error": False,
                                          "session_id": prepared.provider_session_id, "result": "superseded"})
        self.write_result_line(prepared, {"type": "result", "is_error": False,
                                          "session_id": prepared.provider_session_id, "result": "final"})
        self.process.returncode = 0
        outcome = launcher.wait(running)
        self.assertEqual("completed", outcome.status)

    # 11. session-id mismatch fails closed; the assigned UUID is never replaced
    def test_wait_session_id_mismatch_fails_closed(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        self.write_result_line(prepared, {"type": "result", "is_error": False,
                                          "session_id": "not-the-assigned-uuid", "result": "done"})
        self.process.returncode = 0
        outcome = launcher.wait(running)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("session_id_mismatch", outcome.failure_classification)
        self.assertEqual(prepared.provider_session_id, outcome.thread_id)  # never overwritten by output

    def test_wait_is_error_true_fails_closed(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        self.write_result_line(prepared, {"type": "result", "is_error": True,
                                          "session_id": prepared.provider_session_id, "result": "provider refused"})
        self.process.returncode = 0
        outcome = launcher.wait(running)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("turn_failed", outcome.failure_classification)

    # 12. stdout/stderr file-sink behavior
    def test_wait_reads_output_from_file_sink_path_not_a_pipe(self):
        launcher, prepared = self.prepared()
        launcher.start(prepared, "task")
        self.assertTrue(Path(prepared.stdout_path).exists())
        self.assertEqual("", _read_output_text(prepared.stdout_path))  # nothing written by the fake provider yet
        self.write_result_line(prepared, {"type": "result", "is_error": False, "session_id": prepared.provider_session_id})
        self.assertIn("result", _read_output_text(prepared.stdout_path))

    # 13. close idempotent after wait, and accepts a RunningLaunch (mirrors CodexLauncher.close())
    def test_close_idempotent_after_wait_and_accepts_running_launch(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        self.write_result_line(prepared, {"type": "result", "is_error": False, "session_id": prepared.provider_session_id})
        self.process.returncode = 0
        launcher.wait(running)
        launcher.close(running)
        launcher.close(prepared)  # idempotent regardless of which handle shape is passed
        self.assertEqual(0, self.process.kill_count)  # already exited: no kill needed
        self.assertTrue(prepared._closed)

    # 14. failure cleanup: no orphan process left behind
    def test_wait_timeout_kills_process_no_orphan(self):
        launcher, prepared = self.prepared()
        running = launcher.start(prepared, "task")
        with self.assertRaises(ClaudeLaunchError) as ctx:
            launcher.wait(running)  # returncode stays None: FakeProcess.wait() raises TimeoutExpired
        self.assertEqual("timeout", ctx.exception.classification)
        self.assertEqual(1, self.process.kill_count)

    # 15. execution_runner-shaped fake integration: prepare -> start -> wait end to end
    def test_full_prepare_start_wait_cycle_matches_execution_runner_contract(self):
        launcher, prepared = self.prepared()
        self.assertIsInstance(prepared, PreparedLaunch)
        running = launcher.start(prepared, "read the file and summarize it")
        self.assertEqual(prepared.provider_session_id, running.turn_id)
        self.write_result_line(prepared, {"type": "result", "is_error": False,
                                          "session_id": prepared.provider_session_id, "result": "summary text"})
        self.process.returncode = 0
        outcome = launcher.wait(running)
        self.assertEqual("completed", outcome.status)
        self.assertEqual(prepared.provider_session_id, outcome.thread_id)
        launcher.close(running)
        self.assertTrue(prepared._closed)


class PermissionProfileTests(unittest.TestCase):
    def test_read_only_profile_helper_matches_launcher_behavior(self):
        request = LaunchRequest("C:/x", sandbox="read-only", approval_policy="never")
        mode, tools = _permission_profile(request)
        self.assertEqual(mode, "plan")
        self.assertIn("Read", tools)

    def test_argv_builder_uses_kebab_case_flags_seen_in_claude_help(self):
        argv = _build_argv("claude.exe", "sid-1", "plan", ("Read",), "claude-sonnet-5")
        self.assertEqual(argv[0], "claude.exe")
        self.assertIn("-p", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        # 7ca8708: real claude.exe rejects --print/-p --output-format=stream-json
        # without --verbose ("requires --verbose"); every real launch failed at
        # spawn before this flag was added.
        self.assertIn("--verbose", argv)


if __name__ == "__main__":
    unittest.main()
