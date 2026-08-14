import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from manager.claude_launcher import ClaudeLaunchError
from manager.codex_launcher import CodexLaunchError, LaunchOutcome, LaunchRequest
from manager.execution_runner import launch_task, run_execution
from manager.task_claims import check_task_execution_claim
from manager.tasks import TaskError
from manager.test_execution_lifecycle import build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import MemoryRegistry
from manager.worktree_locks import link_session


class Process:
    def __init__(self): self.live = True
    def poll(self): return None if self.live else 0


class DelayedProcess(Process):
    def wait(self, timeout=None):
        self.live = False


class Launcher:
    def __init__(self, outcome="completed", failure=None, close_stops=True, events=None):
        self.outcome, self.failure, self.close_stops = outcome, failure, close_stops
        self.events, self.process, self.prepared = events if events is not None else [], Process(), None

    def prepare(self, request):
        self.events.append("prepare")
        self.request = request
        if self.failure == "prepare":
            self.process.live = False  # CodexLauncher.prepare() owns cleanup before returning.
            raise CodexLaunchError("spawn_failed", "raw secret from prepare")
        client = SimpleNamespace(process=self.process)
        self.prepared = SimpleNamespace(thread_id="thread-1", session_path=None, pid=4242,
                                        process_creation_identity="test-process:4242",
                                        prepared_at="2026-08-13T13:00:00Z", _client=client)
        return self.prepared

    def start(self, prepared, prompt):
        self.events.append("start")
        if self.failure == "start": raise CodexLaunchError("protocol_error", "raw secret from start")
        return SimpleNamespace(prepared=prepared, started_at="2026-08-13T13:00:01Z")

    def set_heartbeat(self, running, callback):
        running.heartbeat = callback

    def wait(self, running):
        self.events.append("wait")
        if self.failure == "wait": raise CodexLaunchError("timeout", "raw secret from wait")
        if self.failure == "interrupt": raise KeyboardInterrupt()
        detail = "raw secret provider detail" if self.outcome != "completed" else None
        return LaunchOutcome(self.outcome, "thread-1", "turn-1", "2026-08-13T13:01:00Z", "turn_failed" if detail else None, detail)

    def close(self, handle):
        self.events.append("close")
        if self.close_stops: self.process.live = False


class ClaudeStyleLauncher:
    """A launcher double shaped like ClaudeLauncher: prepare() returns
    provider_session_id/session_path (not Codex's thread_id/session_path
    naming), proving run_execution/_session() genuinely accept both via duck
    typing rather than one launcher's field names happening to work by luck."""

    def __init__(self, outcome="completed", prepare_failure=None):
        self.outcome, self.prepare_failure = outcome, prepare_failure
        self.events, self.process, self.prepared = [], Process(), None

    def prepare(self, request):
        self.events.append("prepare")
        self.request = request
        if self.prepare_failure:
            self.process.live = False
            raise ClaudeLaunchError("spawn_failed", "raw secret from claude prepare")
        client = SimpleNamespace(process=self.process)
        self.prepared = SimpleNamespace(provider_session_id="claude-session-1", session_path="/tmp/claude.jsonl",
                                        pid=4343, process_creation_identity="test-process:4343",
                                        prepared_at="2026-08-13T13:00:00Z", _client=client)
        return self.prepared

    def start(self, prepared, prompt):
        self.events.append("start")
        return SimpleNamespace(prepared=prepared, started_at="2026-08-13T13:00:01Z")

    def set_heartbeat(self, running, callback):
        running.heartbeat = callback

    def wait(self, running):
        self.events.append("wait")
        return LaunchOutcome(self.outcome, "claude-session-1", "turn-1", "2026-08-13T13:01:00Z", None, None)

    def close(self, handle):
        self.events.append("close")
        self.process.live = False


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.request = LaunchRequest(str(Path(self.temp.name).resolve()), model="gpt-test")

    def tearDown(self): self.temp.cleanup()

    def execute(self, read_only=False, launcher=None, store=None, provider="codex"):
        store = store or build_store(read_only=read_only, working_directory=self.request.working_directory, provider=provider)
        writer = None if read_only else MemoryRegistry()
        claim = MemoryClaimRegistry()
        launcher = launcher or Launcher()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = run_execution(store, object(), writer, claim, launcher, "p1", "t1", "exec-a", "secret prompt", self.request,
                                   access="read_only" if read_only else "production_write",
                                   baseline_head=None if read_only else "a" * 40, provider=provider)
        return store, writer, claim, launcher, result

    def test_prepare_link_start_wait_close_order_and_session_readback(self):
        events = []; launcher = Launcher(events=events)
        real_put = None
        store = build_store(working_directory=self.request.working_directory)
        real_put = store.put
        def ordered_put(area, project, name, document):
            if area == "sessions": events.append("session" if document["status"] == "active" else "session-terminal")
            if area == "executions" and document.get("session_id") and document.get("status") == "running": events.append("execution-link")
            return real_put(area, project, name, document)
        store.put = ordered_put
        with patch("manager.execution_runner.link_writer_session", side_effect=lambda *args, **kwargs: (events.append("writer-link"), link_session(*args, **kwargs))[1]):
            _, writer, _, _, result = self.execute(launcher=launcher, store=store)
        self.assertEqual(["prepare", "session", "execution-link", "writer-link", "start", "execution-link", "wait", "execution-link", "close", "session-terminal"], events)
        self.assertEqual("codex:thread-1", result["session"]["session_id"])
        self.assertEqual("completed", result["session"]["status"])
        self.assertEqual("codex:thread-1", next(iter(writer.document["locks"].values()))["session_id"])

    def test_session_link_failure_never_starts_and_closes(self):
        store = build_store(working_directory=self.request.working_directory); real_put = store.put
        def fail_link(area, project, name, document):
            if area == "executions" and document.get("session_id"): raise TaskError("session link failed")
            return real_put(area, project, name, document)
        store.put = fail_link; launcher = Launcher()
        with self.assertRaisesRegex(TaskError, "session link failed"):
            self.execute(launcher=launcher, store=store)
        self.assertNotIn("start", launcher.events); self.assertIn("close", launcher.events)
        self.assertFalse(launcher.process.live)
        self.assertEqual("interrupted", store.get("executions", "p1", "exec-a")["status"])

    def test_prepare_start_and_wait_failures_close_and_interrupt(self):
        for phase in ("prepare", "start", "wait", "interrupt"):
            with self.subTest(phase=phase):
                launcher = Launcher(failure=phase)
                store = build_store(working_directory=self.request.working_directory)
                expected = KeyboardInterrupt if phase == "interrupt" else CodexLaunchError
                with self.assertRaises(expected): self.execute(launcher=launcher, store=store)
                self.assertFalse(launcher.process.live)
                self.assertEqual("interrupted", store.get("executions", "p1", "exec-a")["status"])
                if phase != "prepare": self.assertIn("close", launcher.events)

    def test_success_and_provider_failure_map_outcomes_after_stop(self):
        for outcome in ("completed", "failed", "interrupted"):
            with self.subTest(outcome=outcome):
                store, _, _, launcher, result = self.execute(launcher=Launcher(outcome=outcome))
                self.assertFalse(launcher.process.live)
                self.assertEqual(outcome, result["terminal"]["execution"]["status"])

    def test_terminalize_is_never_called_while_process_live(self):
        launcher = Launcher(close_stops=False)
        with patch("manager.execution_runner.terminalize_execution") as terminalize, self.assertRaisesRegex(TaskError, "stop could not be proven"):
            self.execute(launcher=launcher)
        terminalize.assert_not_called()

    def test_close_wait_proves_delayed_provider_stop(self):
        launcher = Launcher(); launcher.process = DelayedProcess(); launcher.prepared = None
        store, _, _, _, result = self.execute(launcher=launcher)
        self.assertEqual("completed", result["terminal"]["execution"]["status"])

    def test_terminal_persistence_failure_retains_authority(self):
        store = build_store(working_directory=self.request.working_directory); real_put = store.put
        def fail_terminal(area, project, name, document):
            if area == "executions" and document.get("status") == "completed": raise TaskError("terminal persistence failed")
            return real_put(area, project, name, document)
        store.put = fail_terminal
        writer, claim, launcher = MemoryRegistry(), MemoryClaimRegistry(), Launcher()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             self.assertRaisesRegex(TaskError, "terminal persistence failed"):
            run_execution(store, object(), writer, claim, launcher, "p1", "t1", "exec-a", "secret prompt", self.request, baseline_head="a" * 40)
        self.assertFalse(launcher.process.live)
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])
        self.assertEqual("exec-a", check_task_execution_claim(claim, "p1", "t1")["execution_id"])
        self.assertEqual("active", next(iter(writer.document["locks"].values()))["status"])

    def test_read_only_and_production_authority_use_existing_cleanup(self):
        _, _, read_claim, _, read_result = self.execute(read_only=True)
        self.assertEqual("not_required", read_result["terminal"]["cleanup"]["writer_release"])
        self.assertIsNone(read_claim.document)
        _, writer, claim, _, write_result = self.execute()
        self.assertEqual("released", write_result["terminal"]["cleanup"]["writer_release"])
        self.assertEqual("released", write_result["terminal"]["cleanup"]["task_claim_release"])
        self.assertIsNone(claim.document)
        self.assertEqual("released", next(iter(writer.document["locks"].values()))["status"])

    def test_read_only_launch_is_explicitly_sandboxed_without_approval_prompts(self):
        store = build_store(read_only=True, working_directory=self.request.working_directory)
        launcher = Launcher()
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "codex", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox")
        self.assertEqual("read-only", launcher.request.sandbox)
        self.assertEqual("never", launcher.request.approval_policy)

    def test_prompt_transcript_stderr_and_raw_failure_are_not_persisted(self):
        store, _, _, _, _ = self.execute(launcher=Launcher(outcome="failed"))
        persisted = repr(store.records)
        for secret in ("secret prompt", "raw secret", "provider detail", "stderr"):
            self.assertNotIn(secret, persisted)

    # -- Claude provider-neutrality proofs (item 11: provider evidence remains "claude") --

    def test_claude_provider_evidence_flows_through_execution_and_session(self):
        store, _, _, _, result = self.execute(
            read_only=True, launcher=ClaudeStyleLauncher(), provider="claude",
            store=build_store(read_only=True, working_directory=self.request.working_directory, provider="claude"),
        )
        self.assertEqual("completed", result["terminal"]["execution"]["status"])
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("claude", execution["provider"])
        self.assertEqual("claude-session-1", execution["provider_session_id"])
        session = result["session"]
        self.assertEqual("claude:claude-session-1", session["session_id"])
        self.assertEqual("claude", session["provider"])
        self.assertEqual("claude-session-1", session["provider_session_id"])

    def test_provider_session_id_accepts_both_thread_id_and_provider_session_id_naming(self):
        # Codex's PreparedLaunch names this field thread_id; Claude's names it
        # provider_session_id. Both must resolve to the same downstream identity.
        codex_store = build_store(read_only=True, working_directory=self.request.working_directory, provider="codex")
        _, _, _, _, codex_result = self.execute(read_only=True, launcher=Launcher(), store=codex_store, provider="codex")
        self.assertEqual("codex:thread-1", codex_result["session"]["session_id"])

        claude_store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        _, _, _, _, claude_result = self.execute(read_only=True, launcher=ClaudeStyleLauncher(), store=claude_store, provider="claude")
        self.assertEqual("claude:claude-session-1", claude_result["session"]["session_id"])

    # -- item 9: launcher exception does not create a fake running Execution --

    def test_claude_launcher_prepare_failure_never_leaves_a_fake_running_execution(self):
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = ClaudeStyleLauncher(prepare_failure=True)
        with self.assertRaises(ClaudeLaunchError):
            self.execute(read_only=True, launcher=launcher, store=store, provider="claude")
        execution = store.get("executions", "p1", "exec-a")
        self.assertNotEqual("running", execution["status"])
        self.assertEqual("interrupted", execution["status"])
        self.assertIsNone(launcher.prepared)


if __name__ == "__main__":
    unittest.main()
