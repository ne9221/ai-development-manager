import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from manager.claude_launcher import (
    ClaudeLaunchError, ClaudeLauncher, PreparedLaunch,
    resolve_claude_executable, _build_argv, _encode_stream_json_input,
    _extract_stream_json_result, _permission_profile, _read_output_text,
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

    def launcher(self, log_dir=None):
        return ClaudeLauncher(executable=__file__, popen=self._popen, log_dir=log_dir or self.temp.name)

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
        launcher = ClaudeLauncher(executable=__file__, popen=failing_popen, log_dir=self.temp.name)
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
        return ClaudeLauncher(executable=__file__, popen=self._popen, log_dir=self.temp.name)

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


if __name__ == "__main__":
    unittest.main()
