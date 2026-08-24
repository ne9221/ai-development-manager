import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from manager.claude_account_selector import AccountSelectionError
from manager.claude_config_locks import ConfigLockBusyError, acquire_claude_config_lock, canonical_config_dir
from manager.claude_launcher import ClaudeLaunchError
from manager.codex_launcher import CodexLaunchError, LaunchOutcome, LaunchRequest
from manager.execution_runner import _resolve_working_directory, _stopped, launch_task, run_execution
from manager.production_guard import RuntimeGuardError, mark_production_path
from manager import provenance
from manager.task_claims import check_task_execution_claim
from manager.tasks import TaskError, create_project, create_task, now_iso, update_task
from manager.test_execution_lifecycle import MemoryStore, build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import HEAD, REPO, MemoryRegistry
from manager.worktree_locks import link_session


# Frozen at import time, always "now" as of test collection -- these tests
# exercise claude_accounts registry resolution, which (as of the P0
# claude-auth-routing-truth fix) enforces the same quota-freshness window
# every other reliability gate in this codebase uses, so a stale, years-old
# fixture timestamp would fail closed instead of reaching the scenario the
# test actually means to exercise.
FRESH_QUOTA_TIMESTAMP = now_iso()


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


class AccountAwareClaudeStyleLauncher:
    """Like ClaudeStyleLauncher, but accepts account_id/config_dir like the
    real ClaudeLauncher.prepare() does now, and records what it received so
    tests can assert run_execution() actually threads them through."""

    def __init__(self, outcome="completed"):
        self.outcome = outcome
        self.events, self.process, self.prepared = [], Process(), None
        self.received_account_id = self.received_config_dir = "unset"

    def prepare(self, request, account_id=None, config_dir=None):
        self.events.append("prepare")
        self.request = request
        self.received_account_id, self.received_config_dir = account_id, config_dir
        client = SimpleNamespace(process=self.process)
        self.prepared = SimpleNamespace(
            provider_session_id="claude-session-1", session_path="/tmp/claude.jsonl",
            pid=4343, process_creation_identity="test-process:4343",
            prepared_at="2026-08-13T13:00:00Z", _client=client,
            account_id=account_id, config_dir=config_dir,
        )
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
        # provider="claude" runs in this suite go through run_execution()'s
        # real acquire_claude_config_lock()/release_claude_config_lock()
        # (not mocked here -- ClaudeConfigLockWiringTests below is where the
        # lock's own behavior is exercised). Without this override they would
        # default to the real AI_MANAGER_HOME and read/write actual local ADM
        # state, which Phase 0 forbids touching from a test run.
        self.lock_home = tempfile.TemporaryDirectory()
        self._lock_home_patch = patch.dict(os.environ, {"AI_MANAGER_HOME": self.lock_home.name})
        self._lock_home_patch.start()

    def tearDown(self):
        self._lock_home_patch.stop()
        self.lock_home.cleanup()
        self.temp.cleanup()

    def execute(self, read_only=False, launcher=None, store=None, provider="codex", account_id=None, config_dir=None):
        store = store or build_store(read_only=read_only, working_directory=self.request.working_directory, provider=provider)
        writer = None if read_only else MemoryRegistry()
        claim = MemoryClaimRegistry()
        launcher = launcher or Launcher()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = run_execution(store, object(), writer, claim, launcher, "p1", "t1", "exec-a", "secret prompt", self.request,
                                   access="read_only" if read_only else "production_write",
                                   baseline_head=None if read_only else "a" * 40, provider=provider,
                                   account_id=account_id, config_dir=config_dir)
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

    def test_launch_task_bounds_dispatch_history_lookup_deadline(self):
        """P0 dispatch-two-tick-observability: launch_task() runs dispatch()
        AFTER a Command is already written "claimed" but BEFORE
        reserve_execution() -- a live HOME trace showed dispatch()'s
        unbounded historical-estimate lookup add ~4.5 minutes here, growing
        with total project execution history. When launch_task() does not
        receive a precomputed `executions` list (the real production path --
        manager.command_watcher never supplies one), it must forward a real,
        near-future time.monotonic() deadline to dispatch() so that lookup
        can never again grow unbounded."""
        import time as time_module
        from manager.execution_runner import DISPATCH_HISTORY_BUDGET_SECONDS

        store = build_store(read_only=True, working_directory=self.request.working_directory)
        launcher = Launcher()
        captured = {}

        def capturing_dispatch(store, service, request, quota_document=None, executions=None, history_deadline=None):
            captured["executions"] = executions
            captured["history_deadline"] = history_deadline
            return {"recommended_provider": "codex", "quota_evidence": {"source": "test"},
                   "mode": "auto", "effort": "medium", "generated_prompt": "bounded task"}

        before = time_module.monotonic()
        with patch("manager.execution_runner.dispatch", side_effect=capturing_dispatch), \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-deadline")
        after = time_module.monotonic()

        self.assertIsNone(captured["executions"])
        self.assertIsNotNone(captured["history_deadline"])
        # Bounded to roughly DISPATCH_HISTORY_BUDGET_SECONDS from now -- never
        # unbounded (None) and never absurdly far in the future.
        self.assertGreaterEqual(captured["history_deadline"], before + DISPATCH_HISTORY_BUDGET_SECONDS - 1.0)
        self.assertLessEqual(captured["history_deadline"], after + DISPATCH_HISTORY_BUDGET_SECONDS + 1.0)

    def test_launch_task_omits_history_deadline_when_executions_precomputed(self):
        """A caller that already supplies its own `executions` history list
        (e.g. an existing test/CLI path) must not have dispatch() silently
        switch to the bounded lookup -- history_deadline stays None so
        dispatch() uses the explicit list exactly as before this fix."""
        store = build_store(read_only=True, working_directory=self.request.working_directory)
        launcher = Launcher()
        captured = {}

        def capturing_dispatch(store, service, request, quota_document=None, executions=None, history_deadline=None):
            captured["executions"] = executions
            captured["history_deadline"] = history_deadline
            return {"recommended_provider": "codex", "quota_evidence": {"source": "test"},
                   "mode": "auto", "effort": "medium", "generated_prompt": "bounded task"}

        with patch("manager.execution_runner.dispatch", side_effect=capturing_dispatch), \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-precomputed",
                       executions=[])
        self.assertEqual([], captured["executions"])
        self.assertIsNone(captured["history_deadline"])

    def test_launch_task_resolves_claude_account_registry_and_threads_it_to_launcher(self):
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        document = {"schema_version": "0.1.0", "generated_at": "2026-08-15T02:00:00Z", "providers": [{
            "provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic",
            "source": "test", "source_type": "official", "confidence": "official",
            "last_updated": FRESH_QUOTA_TIMESTAMP, "status": "ok", "windows": [],
            "account_id": "account-b",
        }]}
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "claude", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner._claude_account_auth_ready", return_value=True):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                       provider="claude", quota_document=document, claude_accounts=registry)
        self.assertEqual("account-b", launcher.received_account_id)
        self.assertEqual(r"C:\accounts\b\.claude", launcher.received_config_dir)

    def test_launch_task_forwards_resolved_claude_account_id_to_dispatch(self):
        """The account_id resolved via claude_accounts (or supplied directly)
        is the one that will actually launch -- dispatch()'s quota summary
        must be computed for that same account, not the legacy provider-level
        representative, or the AI/log can be shown a different account's
        quota than the one it is actually running under."""
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        document = {"schema_version": "0.1.0", "generated_at": "2026-08-15T02:00:00Z", "providers": [{
            "provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic",
            "source": "test", "source_type": "official", "confidence": "official",
            "last_updated": FRESH_QUOTA_TIMESTAMP, "status": "ok", "windows": [],
            "account_id": "account-b",
        }]}
        captured = {}

        def fake_dispatch(store, service, request, quota_document=None, executions=None, history_deadline=None):
            captured["request"] = request
            return {
                "recommended_provider": "claude", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
                "generated_prompt": "bounded read-only task",
            }

        with patch("manager.execution_runner.dispatch", side_effect=fake_dispatch), \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner._claude_account_auth_ready", return_value=True):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                       provider="claude", quota_document=document, claude_accounts=registry)
        self.assertEqual("account-b", captured["request"].get("account_id"))

    def test_launch_task_claude_accounts_ambiguous_fails_closed_before_launching(self):
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        document = {"schema_version": "0.1.0", "generated_at": "2026-08-15T02:00:00Z", "providers": [
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": FRESH_QUOTA_TIMESTAMP,
             "status": "ok", "windows": [], "account_id": "account-a"},
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": FRESH_QUOTA_TIMESTAMP,
             "status": "ok", "windows": [], "account_id": "account-b"},
        ]}
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "claude", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner._claude_account_auth_ready", return_value=True):
            with self.assertRaises(AccountSelectionError):
                launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                           provider="claude", quota_document=document, claude_accounts=registry)
        self.assertEqual([], launcher.events)  # never even reached prepare()

    def test_launch_task_excludes_auth_stale_account_from_automatic_selection(self):
        """P0 claude-auth-routing-truth: both accounts show equally 'reliable'
        (frozen, unchanged) quota confidence, but only account-b is actually
        auth-ready right now. Automatic routing must pick account-b, never
        account-a, and must never even attempt to launch account-a."""
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        document = {"schema_version": "0.1.0", "generated_at": "2026-08-15T02:00:00Z", "providers": [
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": FRESH_QUOTA_TIMESTAMP,
             "status": "ok", "windows": [], "account_id": "account-a"},
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": FRESH_QUOTA_TIMESTAMP,
             "status": "ok", "windows": [], "account_id": "account-b"},
        ]}

        def fake_auth_ready(account):
            return account["account_id"] == "account-b"

        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "claude", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner._claude_account_auth_ready", side_effect=fake_auth_ready):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                       provider="claude", quota_document=document, claude_accounts=registry)
        self.assertEqual("account-b", launcher.received_account_id)
        self.assertEqual(r"C:\accounts\b\.claude", launcher.received_config_dir)

    def test_launch_task_all_accounts_auth_unready_fails_closed_before_launching(self):
        """Both accounts fail the live auth-readiness check (e.g. both
        AuthStaleError / ambiguous authentication_check_failed) -- automatic
        routing must fail closed rather than launching against either one."""
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        document = {"schema_version": "0.1.0", "generated_at": "2026-08-15T02:00:00Z", "providers": [
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": FRESH_QUOTA_TIMESTAMP,
             "status": "ok", "windows": [], "account_id": "account-a"},
            {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic", "source": "test",
             "source_type": "official", "confidence": "official", "last_updated": FRESH_QUOTA_TIMESTAMP,
             "status": "ok", "windows": [], "account_id": "account-b"},
        ]}
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "claude", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner._claude_account_auth_ready", return_value=False):
            with self.assertRaises(AccountSelectionError):
                launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                           provider="claude", quota_document=document, claude_accounts=registry)
        self.assertEqual([], launcher.events)  # never even reached prepare()

    def test_launch_task_explicit_claude_account_id_skips_auto_auth_precheck(self):
        """An explicit account_id must not be silently substituted for
        another account based on the auth precheck -- it still relies solely
        on the launcher's own preflight to fail closed, exactly like before
        this fix. The precheck helper must not even be consulted."""
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        document = {"schema_version": "0.1.0", "generated_at": "2026-08-15T02:00:00Z", "providers": [{
            "provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic",
            "source": "test", "source_type": "official", "confidence": "official",
            "last_updated": FRESH_QUOTA_TIMESTAMP, "status": "ok", "windows": [],
            "account_id": "account-a",
        }]}
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "claude", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner._claude_account_auth_ready") as auth_mock:
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                       provider="claude", quota_document=document, claude_accounts=registry, account_id="account-a")
        self.assertEqual("account-a", launcher.received_account_id)
        auth_mock.assert_not_called()

    def test_launch_task_codex_ignores_claude_accounts_registry_signature_unchanged(self):
        store = build_store(read_only=True, working_directory=self.request.working_directory)
        launcher = Launcher()
        registry = [{"account_id": "account-a", "enabled": True, "config_dir": None}]
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "codex", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                       claude_accounts=registry)
        self.assertEqual(["prepare", "start", "wait", "close"], launcher.events)

    # -- raw-passthrough account validation hardening (P2) --

    def test_raw_passthrough_unknown_account_id_without_registry_fails_closed(self):
        """Direct/raw launch_task() callers cannot bypass account validation
        by omitting claude_accounts: an explicit account_id for provider
        "claude" with no registry to check it against must fail closed
        before dispatch/reservation/spawn, never fall through to
        run_execution() -> launcher.prepare() with an unvalidated account_id
        (which would mean config_dir stays None -- ambient/default Claude
        config)."""
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        with patch("manager.execution_runner.dispatch") as dispatch_mock, \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            with self.assertRaises(AccountSelectionError) as ctx:
                launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                           provider="claude", account_id="totally-unknown-account", claude_accounts=None)
        # requested account_id is preserved in the error for audit evidence.
        self.assertIn("totally-unknown-account", str(ctx.exception))
        # fails before dispatch/reservation -- not merely before spawn.
        dispatch_mock.assert_not_called()
        # zero provider spawn: prepare() (the only thing that can Popen) never ran.
        self.assertEqual([], launcher.events)

    def test_raw_passthrough_disabled_account_with_registry_fails_closed(self):
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        registry = [{"account_id": "account-a", "enabled": False, "config_dir": None}]
        with patch("manager.execution_runner.dispatch") as dispatch_mock, \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            with self.assertRaises(AccountSelectionError):
                launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                           provider="claude", account_id="account-a", claude_accounts=registry,
                           quota_document={"schema_version": "0.1.0", "providers": []})
        dispatch_mock.assert_not_called()
        self.assertEqual([], launcher.events)

    def test_raw_passthrough_no_explicit_account_id_keeps_legacy_default_path(self):
        """provider="claude" with neither account_id nor claude_accounts
        supplied is the pre-P0.1.5 single-account default -- must remain
        completely unaffected by the new fail-closed branch."""
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        launcher = AccountAwareClaudeStyleLauncher()
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "claude", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                       provider="claude")
        self.assertIsNone(launcher.received_account_id)
        self.assertIsNone(launcher.received_config_dir)
        self.assertEqual(["prepare", "start", "wait", "close"], launcher.events)

    def test_raw_passthrough_codex_with_bare_account_id_unaffected(self):
        """The new fail-closed branch only applies to provider="claude" --
        a Codex caller passing a bare account_id (meaningless for Codex, but
        should never be rejected because of this Claude-only hardening) must
        reach run_execution() unchanged, not be intercepted by the new
        AccountSelectionError branch. run_execution() itself is stubbed out
        here because CodexLauncher/its test double never accepted an
        account_id kwarg even before this fix -- that pre-existing,
        unrelated shape mismatch is out of scope for this P2."""
        store = build_store(read_only=True, working_directory=self.request.working_directory)
        launcher = Launcher()
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "codex", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded read-only task",
        }), patch("manager.execution_runner.run_execution", return_value={
            "terminal": {"execution": {"status": "completed"}}, "session": {},
        }) as run_execution_mock, \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            launch_task(store, object(), None, MemoryClaimRegistry(), launcher, "p1", "t1", "exec-sandbox",
                       provider="codex", account_id="whatever", claude_accounts=None)
        run_execution_mock.assert_called_once()
        self.assertEqual("whatever", run_execution_mock.call_args.kwargs.get("account_id"))

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
        evidence = execution["provider_evidence"]
        for key in ("launcher_pid", "launcher_creation_identity", "provider_pid",
                    "provider_creation_identity", "provider_parent_identity"):
            self.assertTrue(evidence[key])
        self.assertIsNone(evidence["scheduler_invocation_id"])

    def test_account_id_and_config_dir_thread_through_to_launcher_and_session(self):
        launcher = AccountAwareClaudeStyleLauncher()
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider="claude")
        _, _, _, launcher, result = self.execute(
            read_only=True, launcher=launcher, store=store, provider="claude",
            account_id="account-b", config_dir=r"C:\accounts\b\.claude",
        )
        self.assertEqual("account-b", launcher.received_account_id)
        self.assertEqual(r"C:\accounts\b\.claude", launcher.received_config_dir)
        self.assertEqual("account-b", result["session"]["account_id"])
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("account-b", execution["account_id"])

    def test_no_account_id_leaves_codex_launcher_call_signature_unchanged(self):
        # CodexLauncher.prepare(request) takes no account_id/config_dir kwargs
        # at all; run_execution() must not pass them unless supplied.
        launcher = Launcher()
        self.execute(launcher=launcher)
        self.assertEqual(["prepare", "start", "wait", "close"], launcher.events)

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

    # -- regression coverage for 5d86fcd: _stopped() must duck-type Claude's
    # PreparedLaunch shape too, not only Codex's _client.process --

    def test_stopped_falls_back_to_process_attribute_when_no_client_is_present(self):
        # ClaudeLauncher's real PreparedLaunch holds the subprocess directly as
        # `_process` and has no `_client` field at all (unlike Codex's
        # app-server-wrapped `_client.process`). Before 5d86fcd, _stopped()
        # only ever read _client.process and silently returned False forever
        # for a real Claude execution even after a clean exit, so
        # terminalize_execution() could never be reached.
        process = Process()
        prepared = SimpleNamespace(_process=process)
        self.assertFalse(hasattr(prepared, "_client"))
        self.assertFalse(_stopped(prepared))
        process.live = False
        self.assertTrue(_stopped(prepared))


class ClaudeConfigLockWiringTests(unittest.TestCase):
    """P0.2: run_execution() must acquire the local Claude config-dir lock
    before launcher.prepare() and release it after launcher.close() for
    provider="claude", and must never touch it at all for any other
    provider. Each test points AI_MANAGER_HOME at an isolated tempdir so no
    real ADM state (and no real Claude config) is ever read or written."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.request = LaunchRequest(str(Path(self.temp.name).resolve()), model="gpt-test")
        self.home = tempfile.TemporaryDirectory()
        self._env_patch = patch.dict(os.environ, {"AI_MANAGER_HOME": self.home.name})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.home.cleanup()
        self.temp.cleanup()

    def execute(self, launcher, provider="claude", account_id=None, config_dir=None):
        store = build_store(read_only=True, working_directory=self.request.working_directory, provider=provider)
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            return run_execution(store, object(), None, claim, launcher, "p1", "t1", "exec-a", "secret prompt",
                                 self.request, access="read_only", baseline_head=None, provider=provider,
                                 account_id=account_id, config_dir=config_dir), store, claim

    def test_claude_provider_acquires_before_prepare_and_releases_after_close(self):
        events = []
        launcher = AccountAwareClaudeStyleLauncher()
        real_prepare, real_close = launcher.prepare, launcher.close
        launcher.prepare = lambda *a, **k: (events.append("prepare"), real_prepare(*a, **k))[1]
        launcher.close = lambda *a, **k: (events.append("close"), real_close(*a, **k))[1]
        with patch("manager.execution_runner.acquire_claude_config_lock",
                   side_effect=lambda *a, **k: (events.append("acquire"), {"lock_id": "x", "pid": 1, "creation_identity": "y"})[1]) as acquire, \
             patch("manager.execution_runner.release_claude_config_lock",
                   side_effect=lambda *a, **k: events.append("release")) as release:
            self.execute(launcher, account_id="account-a", config_dir=r"C:\accounts\a\.claude")
        self.assertEqual(["acquire", "prepare", "close", "release"], events)
        acquire.assert_called_once()
        self.assertEqual(r"C:\accounts\a\.claude", acquire.call_args.kwargs.get("config_dir") or acquire.call_args.args[0])
        release.assert_called_once()

    def test_codex_provider_never_touches_the_config_lock(self):
        launcher = Launcher()
        with patch("manager.execution_runner.acquire_claude_config_lock") as acquire, \
             patch("manager.execution_runner.release_claude_config_lock") as release:
            self.execute(launcher, provider="codex")
        acquire.assert_not_called()
        release.assert_not_called()

    def test_release_still_runs_when_the_provider_turn_fails(self):
        launcher = AccountAwareClaudeStyleLauncher(outcome="failed")
        with patch("manager.execution_runner.acquire_claude_config_lock",
                   return_value={"lock_id": "x", "pid": 1, "creation_identity": "y"}), \
             patch("manager.execution_runner.release_claude_config_lock") as release:
            self.execute(launcher)
        release.assert_called_once()

    def test_release_still_runs_when_prepare_raises(self):
        launcher = ClaudeStyleLauncher(prepare_failure=True)
        with patch("manager.execution_runner.acquire_claude_config_lock",
                   return_value={"lock_id": "x", "pid": 1, "creation_identity": "y"}), \
             patch("manager.execution_runner.release_claude_config_lock") as release, \
             self.assertRaises(ClaudeLaunchError):
            self.execute(launcher)
        release.assert_called_once()

    def test_busy_config_dir_fails_closed_and_still_releases_task_claim_and_lease(self):
        # A real held lock (not mocked), owned by a genuinely different, live
        # OS process (a throwaway subprocess), simulating another live ADM
        # execution already using this exact config directory -- the actual
        # incident scenario. Using this test process's own pid would instead
        # exercise the same-owner idempotent path (covered separately below).
        import subprocess as _subprocess
        import sys as _sys
        from manager.codex_launcher import process_creation_identity as _identity
        other = _subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.kill)
        try:
            other_identity = _identity(other.pid)
            self.assertIsNotNone(other_identity)
            acquire_claude_config_lock(r"C:\accounts\a\.claude", account_id="account-a",
                                       execution_id="other-exec", pid=other.pid, creation_identity=other_identity)
            launcher = AccountAwareClaudeStyleLauncher()
            with self.assertRaises(ConfigLockBusyError):
                self.execute(launcher, account_id="account-a", config_dir=r"C:\accounts\a\.claude")
            # launcher.prepare() must never have been reached.
            self.assertEqual([], launcher.events)
        finally:
            other.kill()
            other.wait(timeout=5)

    def test_same_owner_retry_after_incomplete_release_is_idempotent_not_busy(self):
        # Covers requirement 6 (duplicate/retry same execution idempotent):
        # if a prior run_execution() call in this same process acquired the
        # lock but its release did not complete (or a caller re-enters with
        # the same execution before releasing), a second acquire from the
        # exact same (pid, creation_identity) must succeed, not deadlock
        # against itself.
        first = acquire_claude_config_lock(r"C:\accounts\a\.claude", account_id="account-a", execution_id="exec-a")
        second = acquire_claude_config_lock(r"C:\accounts\a\.claude", account_id="account-a", execution_id="exec-a")
        self.assertEqual(first["lock_id"], second["lock_id"])
        self.assertEqual(first["pid"], second["pid"])

    def test_two_sequential_claude_launches_on_the_same_config_dir_do_not_deadlock(self):
        # Not concurrent (run_execution is synchronous), but proves acquire
        # ->release is fully symmetric: a second, later launch against the
        # exact same config_dir must succeed once the first has completed.
        for _ in range(2):
            launcher = AccountAwareClaudeStyleLauncher()
            self.execute(launcher, account_id="account-a", config_dir=r"C:\accounts\a\.claude")
            self.assertEqual(["prepare", "start", "wait", "close"], launcher.events)

    def test_different_config_dirs_do_not_contend(self):
        first = acquire_claude_config_lock(r"C:\accounts\a\.claude", account_id="account-a", execution_id="other-exec")
        self.addCleanup(lambda: None)
        launcher = AccountAwareClaudeStyleLauncher()
        # A different, still-held config_dir must not block this launch.
        self.execute(launcher, account_id="account-b", config_dir=r"C:\accounts\b\.claude")
        self.assertEqual(["prepare", "start", "wait", "close"], launcher.events)

    def test_explicit_and_registry_resolved_account_id_use_identical_lock_key(self):
        # canonical_config_dir() is the sole authority for the lock resource;
        # account_id is attribution only. Two accounts (mis)configured to the
        # same real directory must canonicalize identically.
        self.assertEqual(canonical_config_dir(r"C:\accounts\a\.claude"), canonical_config_dir(r"c:\ACCOUNTS\a\.CLAUDE"))


class WorkingDirectoryContractTests(unittest.TestCase):
    """P0 regression: manager.execution_runner.launch_task() must never do a
    bare task["working_directory"] dict access (KeyError on any Task created
    before/without this field -- exactly what Direct Dispatch's Task shape
    was doing). It must resolve the Task's own snapshot first, fall back to
    the Task's Project only for a legacy Task missing the field, validate
    the resolved value (string, absolute, existing directory) before any
    provider spawn or execution-reservation side effect, and never fall
    back to the launching process's own ambient cwd."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.valid_dir = str(Path(self.temp.name).resolve())
        self.lock_home = tempfile.TemporaryDirectory()
        self._lock_home_patch = patch.dict(os.environ, {"AI_MANAGER_HOME": self.lock_home.name})
        self._lock_home_patch.start()

    def tearDown(self):
        self._lock_home_patch.stop()
        self.lock_home.cleanup()
        self.temp.cleanup()

    def _project(self, working_directory="unset"):
        document = {
            "project_id": "p1", "name": "Project", "repo": REPO, "default_branch": "main",
            "runtime_ssot": "Drive", "project_rules": [], "active_tasks": ["t1"],
            "current_phase": "Phase 3C", "important_constraints": [],
        }
        if working_directory != "unset":
            document["working_directory"] = working_directory
        return document

    def _legacy_task(self, **overrides):
        # Deliberately shaped like a pre-fix Task: no working_directory key
        # at all unless a test explicitly adds one via overrides -- this is
        # what every Task persisted by manager.dispatcher.dispatch() before
        # this P0 fix (and every Direct-Dispatch-created Task before it)
        # actually looks like on disk.
        document = {
            "task_id": "t1", "project_id": "p1", "title": "Legacy task", "task_type": "implementation",
            "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True,
            "needs_research": False, "needs_browser": False, "parallelizable": False,
            "read_only": False, "scope": ["manager/executions.py"], "constraints": [],
            "acceptance_criteria": ["gate"], "branch": "refs/heads/main", "baseline_head": HEAD,
            "allowed_paths": ["manager/executions.py"], "execution_policies": [],
        }
        document.update(overrides)
        return document

    def _store(self, project_working_directory="unset", task_overrides=None):
        store = MemoryStore()
        create_project(store, self._project(project_working_directory))
        create_task(store, self._legacy_task(**(task_overrides or {})), assign=False)
        return store

    def _launch(self, store, launcher=None, retry_count=0, retry_of_execution_id=None,
               execution_id="exec-a", provider="codex"):
        launcher = launcher or Launcher()
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": provider, "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = launch_task(store, object(), MemoryRegistry(), MemoryClaimRegistry(), launcher,
                                 "p1", "t1", execution_id, retry_count=retry_count,
                                 retry_of_execution_id=retry_of_execution_id, provider=provider)
        return result, launcher

    # -- B2: legacy Task missing working_directory falls back to its Project --

    def test_legacy_task_falls_back_to_project_working_directory(self):
        store = self._store(project_working_directory=self.valid_dir)
        result, launcher = self._launch(store)
        self.assertEqual(self.valid_dir, launcher.request.working_directory)
        self.assertEqual("completed", result["terminal"]["execution"]["status"])

    def _activated_runtime(self):
        runtime = Path(self.temp.name) / "production-runtime"
        runtime.mkdir()
        for args in (("init",), ("config", "user.email", "test@example.invalid"),
                     ("config", "user.name", "test")):
            subprocess.run(["git", *args], cwd=runtime, check=True, capture_output=True)
        (runtime / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=runtime, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=runtime, check=True, capture_output=True)
        manager_home = Path(self.lock_home.name)
        provenance.capture_tested(runtime, manager_home)
        provenance.activate(runtime, manager_home)
        return runtime

    def test_direct_launch_from_foreign_cwd_fails_closed_for_bad_production_evidence(self):
        runtime = self._activated_runtime()
        evidence = Path(self.lock_home.name) / "provenance" / "activated_sha.json"
        document = json.loads(evidence.read_text(encoding="utf-8"))
        document["tested_sha"] = "0" * 40
        evidence.write_text(json.dumps(document), encoding="utf-8")
        store = self._store(task_overrides={"working_directory": str(runtime), "read_only": True,
                                            "needs_repo_edit": False})
        launcher = Launcher()
        foreign = Path(self.temp.name) / "foreign-cwd"; foreign.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(foreign)
            with self.assertRaises(RuntimeGuardError):
                self._launch(store, launcher=launcher)
        finally:
            os.chdir(previous)
        self.assertEqual([], launcher.events)
        with self.assertRaises((TaskError, KeyError)):
            store.get("executions", "p1", "exec-a")

    def test_direct_launch_from_foreign_cwd_accepts_valid_production_identity(self):
        runtime = self._activated_runtime()
        store = self._store(task_overrides={"working_directory": str(runtime), "read_only": True,
                                            "needs_repo_edit": False})
        foreign = Path(self.temp.name) / "foreign-cwd"; foreign.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(foreign)
            result, launcher = self._launch(store)
        finally:
            os.chdir(previous)
        self.assertEqual("completed", result["terminal"]["execution"]["status"])
        self.assertEqual(["prepare", "start", "wait", "close"], launcher.events)

    # -- Production checkout drift guard: a legacy repo-edit Task must never
    # fall back onto a checkout manager.provenance.activate() has marked as
    # a protected production runtime path.

    def test_legacy_repo_edit_fallback_rejects_marked_production_path(self):
        mark_production_path(self.valid_dir, "a" * 40, self.lock_home.name)
        store = self._store(project_working_directory=self.valid_dir)
        with self.assertRaisesRegex(TaskError, "PRODUCTION_PATH_PROTECTED"):
            _resolve_working_directory(store, store.get("tasks", "p1", "t1"))

    def test_legacy_read_only_fallback_still_allowed_onto_marked_production_path(self):
        # Reading (not writing) inside the production checkout is not the
        # drift this guard exists to prevent -- only a developer/write task
        # is rejected.
        mark_production_path(self.valid_dir, "a" * 40, self.lock_home.name)
        store = self._store(project_working_directory=self.valid_dir,
                             task_overrides={"read_only": True, "needs_repo_edit": False})
        resolved = _resolve_working_directory(store, store.get("tasks", "p1", "t1"))
        self.assertEqual(self.valid_dir, resolved)

    def test_legacy_fallback_unaffected_when_directory_is_not_marked_production(self):
        # No marker present -- normal Hands-off behavior must be unchanged.
        store = self._store(project_working_directory=self.valid_dir)
        resolved = _resolve_working_directory(store, store.get("tasks", "p1", "t1"))
        self.assertEqual(self.valid_dir, resolved)

    def test_legacy_fallback_is_backfilled_onto_the_task_as_its_own_snapshot(self):
        # So this Task behaves exactly like a post-fix, dispatch-time
        # resolved Task from now on -- see the Retry test below, which
        # relies on this to avoid re-deriving from Project on every attempt.
        store = self._store(project_working_directory=self.valid_dir)
        self._launch(store)
        self.assertEqual(self.valid_dir, store.get("tasks", "p1", "t1")["working_directory"])

    @staticmethod
    def _init_matching_checkout(path, remote="https://github.com/ne9221/ai-development-manager.git"):
        """A real, minimal git checkout at `path` whose origin remote
        matches the real Global Project Registry's registered repo for
        ai-development-manager -- needed because verify_checkout_repo_
        identity() (R2) now actually inspects the resolved workspace
        pointer's git remote, not just a bare directory."""
        os.makedirs(path, exist_ok=True)
        subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, capture_output=True, text=True, check=True)

    def test_legacy_fallback_for_a_registered_project_uses_registry_not_stale_literal(self):
        """P0 regression (fix/direct-dispatch-working-directory-authority-p0-
        20260822): a legacy Task (no working_directory of its own) whose
        project_id IS registered in the Global Project Registry, with the
        registry's workspace env var configured on this machine, must
        resolve via the registry -- never via the Project record's own
        working_directory literal, which is exactly what let a Task launch
        inside a two-day-stale scratch checkout in production."""
        store = MemoryStore()
        project = self._project(working_directory="C:/two-days-stale/scratch-checkout")
        project["project_id"] = "ai-development-manager"
        create_project(store, project)
        task = self._legacy_task(project_id="ai-development-manager", read_only=True, needs_repo_edit=False)
        create_task(store, task, assign=False)
        with tempfile.TemporaryDirectory() as workspace_root, patch.dict(os.environ, {"ADM_WORKSPACE_ROOT": workspace_root}):
            checkout = os.path.join(workspace_root, "ai-development-manager")
            self._init_matching_checkout(checkout)
            resolved = _resolve_working_directory(store, store.get("tasks", "ai-development-manager", "t1"))
        self.assertNotEqual("C:/two-days-stale/scratch-checkout", resolved)
        self.assertEqual(os.path.join(workspace_root, "ai-development-manager"), resolved)

    # -- R2: independent review of the R1 fix found it still reproduced the
    # P0 in the real topology -- cloud.dispatch_ingress/manager.dispatcher.
    # dispatch() run in Cloud Run, which has no HOME-local ADM_WORKSPACE_ROOT.
    # A registered project's Task must therefore reach HOME with
    # working_directory already None (never the Drive literal -- see
    # test_dispatcher.py's matching cloud-context test), and HOME must be
    # the one that actually resolves + verifies it, using its own local
    # ADM_WORKSPACE_ROOT.

    def test_cloud_then_home_topology_registered_project_never_uses_stale_literal(self):
        """The exact reproduction the reviewer asked for: a Task exactly as
        cloud.dispatch_ingress/dispatcher.dispatch() would create it for a
        registered project when ADM_WORKSPACE_ROOT isn't set (Cloud Run) --
        working_directory already None -- then resolved on a simulated HOME
        execution host where ADM_WORKSPACE_ROOT *is* configured."""
        store = MemoryStore()
        project = self._project(working_directory="C:/two-days-stale/scratch-checkout")
        project["project_id"] = "ai-development-manager"
        create_project(store, project)
        task = self._legacy_task(project_id="ai-development-manager", read_only=True, needs_repo_edit=False,
                                  working_directory=None)
        create_task(store, task, assign=False)
        with tempfile.TemporaryDirectory() as workspace_root, patch.dict(os.environ, {"ADM_WORKSPACE_ROOT": workspace_root}):
            self._init_matching_checkout(os.path.join(workspace_root, "ai-development-manager"))
            resolved = _resolve_working_directory(store, store.get("tasks", "ai-development-manager", "t1"))
        self.assertEqual(os.path.join(workspace_root, "ai-development-manager"), resolved)
        self.assertNotEqual("C:/two-days-stale/scratch-checkout", resolved)
        # Immutable snapshot: a retry/second launch reuses HOME's proven value.
        self.assertEqual(resolved, store.get("tasks", "ai-development-manager", "t1")["working_directory"])

    def test_registered_project_fails_closed_when_home_workspace_root_also_missing(self):
        store = MemoryStore()
        project = self._project(working_directory="C:/two-days-stale/scratch-checkout")
        project["project_id"] = "ai-development-manager"
        create_project(store, project)
        task = self._legacy_task(project_id="ai-development-manager", read_only=True, needs_repo_edit=False,
                                  working_directory=None)
        create_task(store, task, assign=False)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADM_WORKSPACE_ROOT", None)
            with self.assertRaises(TaskError):
                _resolve_working_directory(store, store.get("tasks", "ai-development-manager", "t1"))

    def test_registered_project_fails_closed_when_resolved_path_is_wrong_repo(self):
        """A workspace pointer that resolves to a real, existing directory
        which is nonetheless a checkout of a *different* repo (a mis-pointed
        junction, a leftover from another project) must never be trusted."""
        store = MemoryStore()
        project = self._project(working_directory="C:/two-days-stale/scratch-checkout")
        project["project_id"] = "ai-development-manager"
        create_project(store, project)
        task = self._legacy_task(project_id="ai-development-manager", read_only=True, needs_repo_edit=False,
                                  working_directory=None)
        create_task(store, task, assign=False)
        with tempfile.TemporaryDirectory() as workspace_root, patch.dict(os.environ, {"ADM_WORKSPACE_ROOT": workspace_root}):
            self._init_matching_checkout(
                os.path.join(workspace_root, "ai-development-manager"),
                remote="https://github.com/example/some-other-project.git",
            )
            with self.assertRaises(TaskError):
                _resolve_working_directory(store, store.get("tasks", "ai-development-manager", "t1"))

    def test_task_snapshot_working_directory_takes_priority_over_project(self):
        store = self._store(
            project_working_directory=self.valid_dir,
            task_overrides={"working_directory": self.valid_dir},
        )
        # A second, different Project directory must never override an
        # already-resolved Task snapshot.
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        store.records[("projects", "p1", "p1")]["working_directory"] = str(Path(other.name).resolve())
        result, launcher = self._launch(store)
        self.assertEqual(self.valid_dir, launcher.request.working_directory)

    # -- Slice C: a genuine v2-repo-write Task never falls back to the
    # project's shared canonical checkout; it launches inside its own
    # materialized, isolated worktree instead. --

    def _repo_write_task(self, **overrides):
        document = {
            "task_id": "t1", "project_id": "p1", "title": "Bounded write task", "task_type": "implementation",
            "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True,
            "needs_research": False, "needs_browser": False, "parallelizable": False,
            "read_only": False, "scope": ["manager/executions.py"], "constraints": [],
            "acceptance_criteria": ["gate"], "baseline_head": HEAD,
            "allowed_paths": ["manager/executions.py"],
            "execution_policies": ["disposable", "bounded_repo_write", "no_external_writes"],
            "source_context": {"repo": REPO, "admission_version": "v2-repo-write"},
        }
        document.update(overrides)
        return document

    def test_repo_write_task_launches_in_materialized_worktree_not_canonical_checkout(self):
        from manager.project_registry import ProjectMetadata

        worktree_dir = tempfile.TemporaryDirectory()
        self.addCleanup(worktree_dir.cleanup)
        materialized = str(Path(worktree_dir.name).resolve())
        self.assertNotEqual(self.valid_dir, materialized)

        fake_project = ProjectMetadata(
            project_id="p1", display_name="Project", aliases=(), repo={"canonical_url": REPO},
            default_branch="main", baseline_resolution_policy={}, common_governance={}, project_rules={},
            working_directory_policy={}, isolation_policy={"mode": "worktree_per_task"}, provider_restrictions={},
            protected_paths=(), default_write_boundaries=("*",), pointer_rules={},
            status="enabled", resolution_status="verified",
        )

        store = MemoryStore()
        create_project(store, self._project(self.valid_dir))
        create_task(store, self._repo_write_task(), assign=False)

        materialized_result = {"working_directory": materialized, "branch": "refs/heads/adm-worktree/p1/t1",
                               "worktree_id": "p1--t1", "baseline_head": HEAD}

        def fake_materialize(store_arg, project_arg, task_arg, canonical_checkout, workspace_root, **kwargs):
            # A real materialize_worktree() persists onto the Task before
            # returning (Slice C's own read-back contract, tested in
            # manager.test_worktree_materializer) -- mirror that here so the
            # downstream running-gate's task_snapshot() sees a consistent
            # branch/working_directory, exactly as it would in production.
            update_task(store_arg, "p1", "t1", **materialized_result)
            return dict(materialized_result)

        with patch("manager.execution_runner.get_global_registry") as get_registry, \
             patch("manager.execution_runner.materialize_worktree", side_effect=fake_materialize) as materialize, \
             patch("manager.execution_runner.enforce_allowed_paths", return_value=[]) as enforce:
            # Slice D's actual allowed_paths enforcement (real git diff
            # against an isolated worktree) is covered end-to-end in
            # manager.test_repo_write_enforcement; this test's own concern is
            # purely the working_directory wiring, so it stubs enforcement
            # to a clean no-op rather than needing a real git repo here too.
            get_registry.return_value.get_project.return_value = fake_project
            result, launcher = self._launch(store)

        get_registry.return_value.get_project.assert_called_once_with("p1")
        materialize.assert_called_once()
        enforce.assert_called_once_with(materialized, HEAD, ["manager/executions.py"])
        self.assertEqual(materialized, launcher.request.working_directory)
        self.assertNotEqual(self.valid_dir, launcher.request.working_directory)
        self.assertEqual("completed", result["terminal"]["execution"]["status"])

    def test_repo_write_task_materialization_failure_never_falls_back_to_canonical_checkout(self):
        from manager.worktree_materializer import WorktreeMaterializationError

        store = MemoryStore()
        create_project(store, self._project(self.valid_dir))
        create_task(store, self._repo_write_task(), assign=False)
        launcher = Launcher()

        with patch("manager.execution_runner.get_global_registry") as get_registry, \
             patch("manager.execution_runner.materialize_worktree") as materialize:
            get_registry.return_value.get_project.return_value = object()
            materialize.side_effect = WorktreeMaterializationError("baseline_lineage_mismatch", "nope")
            with self.assertRaises(WorktreeMaterializationError):
                self._launch(store, launcher=launcher)
        # Must fail closed rather than silently falling back to Project's
        # own canonical working_directory: no provider process is ever
        # spawned, and no Execution reservation is ever persisted.
        self.assertEqual([], launcher.events)
        with self.assertRaises((TaskError, KeyError)):
            store.get("executions", "p1", "exec-a")

    # -- B3: missing everywhere fails closed --

    def test_missing_task_and_project_working_directory_fails_closed(self):
        store = self._store(project_working_directory="unset")
        with self.assertRaises(TaskError):
            self._launch(store)

    def test_null_project_working_directory_fails_closed(self):
        store = self._store(project_working_directory=None)
        with self.assertRaises(TaskError):
            self._launch(store)

    # -- B4: invalid path shapes fail closed --

    def test_relative_working_directory_fails_closed(self):
        store = self._store(task_overrides={"working_directory": "relative/path"})
        with self.assertRaisesRegex(TaskError, "absolute"):
            self._launch(store)

    def test_nonexistent_working_directory_fails_closed(self):
        missing = os.path.join(self.valid_dir, "does-not-exist")
        store = self._store(task_overrides={"working_directory": missing})
        with self.assertRaisesRegex(TaskError, "does not exist|not a directory"):
            self._launch(store)

    def test_working_directory_pointing_to_a_file_fails_closed(self):
        file_path = os.path.join(self.valid_dir, "not-a-dir.txt")
        Path(file_path).write_text("x")
        store = self._store(task_overrides={"working_directory": file_path})
        with self.assertRaisesRegex(TaskError, "not a directory"):
            self._launch(store)

    # -- B5: invalid working_directory fails before reservation or spawn side effects --

    def test_invalid_working_directory_fails_before_reservation_and_spawn(self):
        store = self._store(project_working_directory="unset")
        launcher = Launcher()
        with self.assertRaises(TaskError):
            self._launch(store, launcher=launcher)
        self.assertEqual([], launcher.events)  # prepare() (the only thing that can spawn) never ran
        with self.assertRaises((TaskError, KeyError)):
            store.get("executions", "p1", "exec-a")  # no Execution reservation was ever persisted

    # -- B6: trusted retry of a legacy task resolves working_directory on the second attempt --

    def test_retry_of_legacy_task_resolves_working_directory(self):
        store = self._store(project_working_directory=self.valid_dir)
        result, launcher = self._launch(
            store, retry_count=1, retry_of_execution_id="exec-a-prior", execution_id="exec-a-retry",
        )
        self.assertEqual(self.valid_dir, launcher.request.working_directory)
        self.assertEqual("completed", result["terminal"]["execution"]["status"])

    def test_retry_reuses_backfilled_snapshot_without_redriving_from_a_changed_project(self):
        # First (failed) attempt backfills the Task's own snapshot from
        # Project. If Project.working_directory is edited afterward (e.g. by
        # an unrelated later dispatch), a genuine second attempt of this same
        # Task -- taken through the real prepare_task_retry() blocked->ready
        # transition, not a fresh Task -- must keep using the value already
        # resolved onto it, never silently re-derive from the changed Project.
        from manager.executions import prepare_task_retry
        store = self._store(project_working_directory=self.valid_dir, task_overrides={
            "read_only": True, "needs_repo_edit": False,
        })
        claim = MemoryClaimRegistry()
        failing_launcher = Launcher(failure="prepare")
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "codex", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             self.assertRaises(CodexLaunchError):
            launch_task(store, object(), None, claim, failing_launcher, "p1", "t1", "exec-a")
        self.assertEqual("interrupted", store.get("executions", "p1", "exec-a")["status"])
        # Backfilled onto the Task by the first attempt.
        self.assertEqual(self.valid_dir, store.get("tasks", "p1", "t1")["working_directory"])

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        store.records[("projects", "p1", "p1")]["working_directory"] = str(Path(other.name).resolve())
        prepare_task_retry(store, claim, "p1", "t1", "exec-a", retry_count=1)
        self.assertEqual("ready", store.get("tasks", "p1", "t1")["status"])

        retry_launcher = Launcher()
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "codex", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            launch_task(store, object(), None, claim, retry_launcher, "p1", "t1", "exec-a-retry",
                       retry_count=1, retry_of_execution_id="exec-a")
        self.assertEqual(self.valid_dir, retry_launcher.request.working_directory)

    # -- B7: Claude and Codex both receive the identical resolved absolute directory --

    def test_claude_and_codex_launch_requests_receive_the_same_resolved_directory(self):
        for provider, launcher in (("codex", Launcher()), ("claude", ClaudeStyleLauncher())):
            with self.subTest(provider=provider):
                store = self._store(project_working_directory=self.valid_dir)
                self._launch(store, launcher=launcher, provider=provider)
                self.assertEqual(self.valid_dir, launcher.request.working_directory)


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


class RealGitAllowedPathsEnforcementIntegrationTests(unittest.TestCase):
    """End-to-end proof (real git, real launch_task() pipeline, no mocked
    enforcement) of Slice D: a provider that actually writes outside its
    Task's allowed_paths inside its isolated worktree can never have that
    execution -- or its Task -- persisted as successfully completed."""

    def setUp(self):
        self.repo_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo_dir.cleanup)
        root = Path(self.repo_dir.name).resolve()
        _git(root, "init")
        _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        (root / "manager").mkdir()
        (root / "other").mkdir()
        (root / "manager" / "foo.py").write_text("original\n", encoding="utf-8")
        (root / "other" / "bar.py").write_text("original\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "init")
        self.worktree = root
        self.baseline_head = _git(root, "rev-parse", "HEAD")

        self.lock_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.lock_home.cleanup)
        self._lock_home_patch = patch.dict(os.environ, {"AI_MANAGER_HOME": self.lock_home.name})
        self._lock_home_patch.start()
        self.addCleanup(self._lock_home_patch.stop)

    def _repo_write_task(self):
        return {
            "task_id": "t1", "project_id": "p1", "title": "Bounded write task", "task_type": "implementation",
            "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True,
            "needs_research": False, "needs_browser": False, "parallelizable": False,
            "read_only": False, "scope": ["manager/foo.py"], "constraints": [],
            "acceptance_criteria": ["gate"], "branch": "refs/heads/main", "baseline_head": self.baseline_head,
            "working_directory": str(self.worktree), "worktree_id": "p1--t1",
            "allowed_paths": ["manager/foo.py"],
            "execution_policies": ["disposable", "bounded_repo_write", "no_external_writes"],
        }

    def _store(self):
        store = MemoryStore()
        create_project(store, {
            "project_id": "p1", "name": "Project", "repo": REPO, "default_branch": "main",
            "runtime_ssot": "Drive", "project_rules": [], "active_tasks": ["t1"],
            "current_phase": "Phase 3C", "important_constraints": [],
        })
        create_task(store, self._repo_write_task(), assign=False)
        return store

    def _launch(self, store, launcher):
        with patch("manager.execution_runner.dispatch", return_value={
            "recommended_provider": "codex", "quota_evidence": {"source": "test"}, "mode": "auto", "effort": "medium",
            "generated_prompt": "bounded task",
        }), patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            return launch_task(store, object(), MemoryRegistry(), MemoryClaimRegistry(), launcher,
                               "p1", "t1", "exec-a", provider="codex")

    def test_out_of_scope_write_is_rejected_and_never_completes(self):
        class OutOfScopeLauncher(Launcher):
            def wait(self, running):
                (Path(self.request.working_directory) / "other" / "bar.py").write_text("hacked\n", encoding="utf-8")
                return super().wait(running)

        store = self._store()
        result = self._launch(store, OutOfScopeLauncher())

        self.assertEqual("failed", result["terminal"]["execution"]["status"])
        self.assertNotEqual("completed", result["terminal"]["execution"]["status"])
        self.assertIn("other/bar.py", result["terminal"]["execution"]["notes"][-1])
        # The Task itself must not be persisted as completed either.
        self.assertNotEqual("completed", store.get("tasks", "p1", "t1")["status"])

    def test_out_of_scope_write_blocks_downstream_success_style_hook(self):
        """No future commit/push step -- represented here by a stub only a
        genuinely successful launch_task() call would be free to invoke --
        can ever run after an out-of-scope write."""
        class OutOfScopeLauncher(Launcher):
            def wait(self, running):
                (Path(self.request.working_directory) / "escaped.py").write_text("hacked\n", encoding="utf-8")
                return super().wait(running)

        store = self._store()
        result = self._launch(store, OutOfScopeLauncher())

        commit_and_push_calls = []
        if result["terminal"]["execution"]["status"] == "completed":
            commit_and_push_calls.append("called")  # would be the real future hook
        self.assertEqual([], commit_and_push_calls)

    def test_in_scope_write_completes_normally(self):
        class InScopeLauncher(Launcher):
            def wait(self, running):
                (Path(self.request.working_directory) / "manager" / "foo.py").write_text("edited\n", encoding="utf-8")
                return super().wait(running)

        store = self._store()
        result = self._launch(store, InScopeLauncher())

        self.assertEqual("completed", result["terminal"]["execution"]["status"])
        self.assertEqual("completed", store.get("tasks", "p1", "t1")["status"])


if __name__ == "__main__":
    unittest.main()
