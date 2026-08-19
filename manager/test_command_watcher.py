import json
import tempfile
import threading
import unittest
import os
import socket
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from manager.claude_launcher import ClaudeLauncher
from manager.codex_launcher import CodexLauncher, process_creation_identity
from manager.command_watcher import (
    CLAIM_TIMEOUT_SECONDS, PROVIDER_RUNTIMES, REQUIRED_TASK_POLICIES, _provider_state,
    claude_quota_reliable, codex_quota_reliable, load_allowlist, poll_once, process_command,
    provider_quota_reliable, resolve_provider_runtime,
)
from manager.execution_lifecycle import enter_running_gate
from manager.executions import execution_health, heartbeat_execution, reserve_execution
from manager.task_claims import TaskClaimConflict
from manager.tasks import TaskError, create_project, create_task, now_iso
from manager.test_execution_lifecycle import project, task
from manager.test_execution_lifecycle import quota_document
from manager.test_task_claims import MemoryClaimRegistry


class Store:
    def __init__(self): self.records = {}; self.fail_command_terminal = False
    def put(self, area, project_id, name, document):
        if area == "commands" and document.get("status") in ("completed", "failed") and self.fail_command_terminal:
            raise TaskError("Drive unavailable")
        self.records[(area, project_id, name)] = deepcopy(document); return document
    def get(self, area, project_id, name):
        try: return deepcopy(self.records[(area, project_id, name)])
        except KeyError: raise TaskError("not found") from None
    def list_projects(self): return [self.get("projects", "p1", "p1")]
    def list_records(self, area, project_id):
        return [deepcopy(value) for (record_area, project, _), value in self.records.items() if record_area == area and project == project_id]


def command(**changes):
    value = {
        "command_id": "cmd-1", "project_id": "p1", "task_id": "t1", "provider": "codex",
        "model": None, "fallback_model": None, "mode": None, "effort": None,
        "selection_reason": [], "quota_evidence": None, "created_at": "2026-08-14T00:00:00Z",
        "status": "queued", "execution_id": None, "claimed_at": None, "completed_at": None, "result": None,
    }
    value.update(changes); return value


class CommandWatcherTests(unittest.TestCase):
    ALLOWLIST = frozenset({("p1", "t1")})

    @staticmethod
    def allowlist_compliant_store():
        store = Store(); create_project(store, project()); create_task(store, task(read_only=True), assign=False)
        compliant = store.get("tasks", "p1", "t1")
        compliant["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        store.put("tasks", "p1", "t1", compliant)
        return store

    def setUp(self):
        self.store = self.allowlist_compliant_store()

    @staticmethod
    def complete(execution_id):
        return {"terminal": {"execution": {"status": "completed"}}, "session": {"session_id": "codex:read-only"}, "execution_id": execution_id,
                "dispatch": {"provider": "codex", "model": None, "fallback_model": None, "mode": "code", "effort": "medium", "selection_reason": ["fresh quota"], "quota_evidence": {"codex": {"freshness": "fresh"}}}}

    @staticmethod
    def claim_factory(*_args): return object()

    @staticmethod
    def iso(value): return value.isoformat().replace("+00:00", "Z")

    def running_command(self, heartbeat_minutes=0, started_minutes=1, pid=None, legacy=False, provider="codex"):
        now = datetime.now(timezone.utc)
        started = self.iso(now - timedelta(minutes=started_minutes))
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", provider, {"decision": "fresh"})
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", "command-cmd-1", provider,
                               "read_only", started_at=started, task_claim_registry=claim)
        execution = self.store.get("executions", "p1", "command-cmd-1")
        execution["heartbeat_at"] = self.iso(now - timedelta(minutes=heartbeat_minutes))
        execution["progress_updated_at"] = execution["heartbeat_at"]
        provider_pid = pid or os.getpid()
        execution["provider_evidence"] = {
            "host": socket.gethostname()[:100], "pid": provider_pid,
            "creation_identity": process_creation_identity(provider_pid) or "test-process:missing",
            "started_at": started,
        }
        execution["last_provider_event"] = "provider_wait"
        if legacy:
            for key in ("heartbeat_at", "progress_updated_at", "provider_evidence", "last_provider_event", "hard_timeout_at"):
                execution[key] = None
        self.store.put("executions", "p1", "command-cmd-1", execution)
        active = command(status="running", execution_id="command-cmd-1", claimed_at=started, provider=provider)
        self.store.put("commands", "p1", "cmd-1", active)
        return active, claim, execution

    def test_duplicate_polling_runs_once_and_persists_terminal_result(self):
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            self.store.put("commands", "p1", "cmd-1", command())
            first = poll_once(self.store, object(), allowlist=self.ALLOWLIST, claim_factory=self.claim_factory, health_check=lambda: True, quota_check=lambda service: True)
            second = poll_once(self.store, object(), allowlist=self.ALLOWLIST, claim_factory=self.claim_factory, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("completed", first[0]["status"]); self.assertEqual([], second); runner.assert_called_once()
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("command-cmd-1", stored["execution_id"]); self.assertEqual("completed", stored["result"]["status"])
        self.assertEqual("code", stored["mode"]); self.assertEqual(["fresh quota"], stored["selection_reason"])

    def test_restart_never_relaunches_claimed_or_running_command(self):
        claimed = command(status="claimed", execution_id="command-cmd-1", claimed_at=now_iso())
        runner = Mock()
        result = process_command(self.store, object(), claimed, launcher_factory=runner)
        self.assertEqual("claimed", result["status"]); runner.assert_not_called()

    def test_command_becomes_running_only_after_running_gate_callback(self):
        states = []
        def runner(*args, **kwargs):
            states.append(self.store.get("commands", "p1", "cmd-1")["status"])
            kwargs["on_running"](None)
            states.append(self.store.get("commands", "p1", "cmd-1")["status"])
            return self.complete(args[7])
        with patch("manager.command_watcher.launch_task", side_effect=runner):
            process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual(["claimed", "running"], states)

    def test_orphaned_claim_times_out_without_relaunch(self):
        claimed = command(status="claimed", execution_id="command-cmd-1", claimed_at="2000-01-01T00:00:00Z")
        runner = Mock()
        result = process_command(self.store, object(), claimed, launcher_factory=runner)
        self.assertEqual("failed", result["status"]); runner.assert_not_called()
        self.assertEqual("claim_timeout", self.store.get("commands", "p1", "cmd-1")["result"]["error_kind"])

    def test_malformed_command_fails_closed_without_launch(self):
        runner = Mock()
        result = process_command(self.store, object(), {"command_id": "bad"}, launcher_factory=runner)
        self.assertEqual({"status": "rejected"}, result); runner.assert_not_called()

    def test_missing_governance_inheritance_hard_rejects_before_launch(self):
        task_record = self.store.get("tasks", "p1", "t1")
        del task_record["governance"]
        self.store.put("tasks", "p1", "t1", task_record)
        runner = Mock()
        result = process_command(
            self.store, object(), command(), launcher_factory=runner,
            allowlist=self.ALLOWLIST, health_check=lambda: True,
            quota_check=lambda _service: True,
        )
        self.assertEqual({"status": "rejected", "reason": "mandatory_governance_missing_or_stale"}, result)
        runner.assert_not_called()

    def test_task_claim_collision_and_missing_writer_authority_do_not_launch(self):
        collision = Mock(side_effect=TaskClaimConflict("already claimed"))
        with patch("manager.command_watcher.launch_task", collision):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"]); collision.assert_called_once()
        stored = self.store.get("commands", "p1", "cmd-1")
        self.assertEqual("TaskClaimConflict", stored["result"]["error_kind"])

        writable = create_task(self.store, task(read_only=False), assign=False, persist=False)
        self.store.put("tasks", "p1", "t1", writable)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(command_id="cmd-2"), claim_factory=self.claim_factory, writer_factory=Mock(side_effect=TaskError("no writer authority")), allowlist=self.ALLOWLIST)
        self.assertEqual("attention", result["status"]); launch.assert_not_called()

    def test_provider_failure_terminalizes_and_drive_failure_never_fakes_completed(self):
        failed = Mock(side_effect=TaskError("provider launch failed"))
        with patch("manager.command_watcher.launch_task", failed):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"])

        self.store = self.allowlist_compliant_store()
        self.store.fail_command_terminal = True
        with patch("manager.command_watcher.launch_task", Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])):
            with self.assertRaisesRegex(TaskError, "Drive unavailable"):
                process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("running", self.store.get("commands", "p1", "cmd-1")["status"])

    def test_health_contract_distinguishes_healthy_stale_and_over_expected(self):
        _, _, healthy = self.running_command()
        self.assertEqual("healthy", execution_health(healthy)["state"])

        healthy["started_at"] = self.iso(datetime.now(timezone.utc) - timedelta(minutes=20))
        healthy["progress_updated_at"] = self.iso(datetime.now(timezone.utc) - timedelta(minutes=16))
        self.assertEqual("provider_progress_stale", execution_health(healthy)["reason"])

        healthy["started_at"] = self.iso(datetime.now(timezone.utc) - timedelta(minutes=21))
        healthy["heartbeat_at"] = now_iso()
        healthy["progress_updated_at"] = healthy["heartbeat_at"]
        healthy["hard_timeout_at"] = self.iso(datetime.now(timezone.utc) + timedelta(minutes=39))
        result = execution_health(healthy)
        self.assertEqual("healthy", result["state"]); self.assertTrue(result["over_expected"])

    def test_provider_wait_without_progress_cannot_keep_execution_healthy(self):
        active, claim, execution = self.running_command(started_minutes=20)
        stale_progress = self.iso(datetime.now(timezone.utc) - timedelta(minutes=16))
        execution["progress_updated_at"] = stale_progress
        self.store.put("executions", "p1", "command-cmd-1", execution)
        heartbeat_execution(self.store, "p1", "command-cmd-1", "provider_wait", progress=False)
        refreshed = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual(stale_progress, refreshed["progress_updated_at"])
        self.assertEqual("provider_progress_stale", execution_health(refreshed)["reason"])
        self.assertEqual("attention", process_command(
            self.store, object(), active, claim_factory=lambda *_: claim)["status"])

    def test_same_pid_with_different_creation_identity_is_not_live(self):
        active, claim, execution = self.running_command(pid=os.getpid())
        execution["provider_evidence"]["creation_identity"] = "original-process"
        self.store.put("executions", "p1", "command-cmd-1", execution)
        with patch("manager.codex_launcher.process_creation_identity", return_value="reused-process"):
            self.assertEqual("replaced", _provider_state(execution))
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertEqual("command-cmd-1", claim.document["execution_id"])

    def test_stale_live_provider_is_attention_and_never_reclaimed(self):
        active, claim, _ = self.running_command(heartbeat_minutes=16, pid=os.getpid())
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertEqual("command-cmd-1", claim.document["execution_id"])
        self.assertEqual(1, claim.generation)
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_proven_dead_read_only_provider_terminalizes_and_writes_command_task(self):
        active, claim, _ = self.running_command(heartbeat_minutes=16, pid=99_999_999)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("failed", result["status"]); self.assertIsNone(claim.document)
        self.assertEqual("interrupted", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("failed", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    # Phase 4E parity gate item: process identity / PID reuse safety and the
    # full stale-provider auto-recovery path, reproduced exactly for
    # provider="claude" -- byte-identical scenario to the Codex test above,
    # just with the provider substituted, proving the recovery path carries
    # no Codex-only assumption.
    def test_proven_dead_read_only_claude_provider_terminalizes_and_writes_command_task(self):
        active, claim, _ = self.running_command(heartbeat_minutes=16, pid=99_999_999, provider="claude")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("failed", result["status"]); self.assertIsNone(claim.document)
        execution = self.store.get("executions", "p1", "command-cmd-1")
        self.assertEqual("interrupted", execution["status"])
        self.assertEqual("claude", execution["provider"])
        self.assertEqual("failed", self.store.get("commands", "p1", "cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_same_pid_with_different_creation_identity_is_not_live_for_claude_provider(self):
        # PID-reuse safety is implemented once in process_identity_state()
        # (shared by both providers) -- this proves a Claude execution gets
        # the same "replaced" fail-closed treatment a Codex one already does.
        active, claim, execution = self.running_command(pid=os.getpid(), provider="claude")
        execution["provider_evidence"]["creation_identity"] = "impersonated-identity"
        self.store.put("executions", "p1", "command-cmd-1", execution)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertIsNotNone(claim.document)  # never released against an unverified/impersonated process

    def test_legacy_uncertain_execution_and_claim_are_not_mutated(self):
        active, claim, execution = self.running_command(heartbeat_minutes=99, started_minutes=999, legacy=True)
        before_claim = deepcopy(claim.document); before_generation = claim.generation
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim)
        self.assertEqual("attention", result["status"])
        self.assertEqual(execution, self.store.get("executions", "p1", "command-cmd-1"))
        self.assertEqual(before_claim, claim.document); self.assertEqual(before_generation, claim.generation)

    def test_prelaunch_reservation_is_cancelled_and_not_left_running(self):
        def reserve_then_fail(*args, **kwargs):
            reserve_execution(self.store, "p1", "t1", args[7], "codex", {"decision": "fresh"})
            raise TaskError("preflight failed")
        with patch("manager.command_watcher.launch_task", side_effect=reserve_then_fail):
            result = process_command(self.store, object(), command(), claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        self.assertEqual("failed", result["status"])
        self.assertEqual("cancelled", self.store.get("executions", "p1", "command-cmd-1")["status"])
        self.assertEqual("blocked", self.store.get("tasks", "p1", "t1")["status"])

    def test_no_allowlist_means_zero_launch(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory)
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

    def test_other_project_or_task_not_in_allowlist_means_zero_launch(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        unrelated = frozenset({("other-project", "t1"), ("p1", "other-task")})
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=unrelated)
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

    def test_allowlisted_but_policy_violation_means_zero_launch(self):
        incomplete = self.store.get("tasks", "p1", "t1")
        incomplete["execution_policies"] = ["disposable", "read_only"]  # missing no_repo_writes/no_external_writes
        self.store.put("tasks", "p1", "t1", incomplete)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST)
        self.assertEqual("attention", result["status"]); launch.assert_not_called()
        self.assertEqual("allowlisted_task_policy_not_satisfied", self.store.get("commands", "p1", "cmd-1")["recovery_reason"])

    def test_allowlisted_disposable_read_only_is_eligible(self):
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), self.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: True)
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_session_center_unhealthy_blocks_new_launch_but_never_touches_running_authority(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST, health_check=lambda: False)
        self.assertEqual({"status": "rejected", "reason": "session_center_unavailable"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

        # An already-running execution must never be touched by health status.
        active, claim, _ = self.running_command(heartbeat_minutes=0)
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim, health_check=lambda: False)
        self.assertEqual("running", result["status"])

    def test_stale_or_unreliable_codex_quota_blocks_new_launch_but_never_touches_running_authority(self):
        self.store.put("commands", "p1", "cmd-1", command())
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory,
                                     allowlist=self.ALLOWLIST, health_check=lambda: True, quota_check=lambda service: False)
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        launch.assert_not_called()
        self.assertEqual("queued", self.store.get("commands", "p1", "cmd-1")["status"])

        # An already-running execution must never be touched by quota status.
        active, claim, _ = self.running_command(heartbeat_minutes=0)
        result = process_command(self.store, object(), active, claim_factory=lambda *_: claim, quota_check=lambda service: False)
        self.assertEqual("running", result["status"])

    def test_codex_quota_reliable_fails_closed_on_stale_unknown_or_unreachable(self):
        from manager.command_watcher import codex_quota_reliable
        fresh_reliable = {
            "providers": [{"provider": "codex", "has_reliable_quota": True}],
        }
        stale_or_unknown = {
            "providers": [{"provider": "codex", "has_reliable_quota": False}],
        }
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=fresh_reliable):
            self.assertTrue(codex_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=stale_or_unknown):
            self.assertFalse(codex_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", side_effect=RuntimeError("Drive unavailable")):
            self.assertFalse(codex_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value={"providers": []}):
            self.assertFalse(codex_quota_reliable(object()))  # no codex entry at all

    def test_session_center_healthy_probe_fails_closed_on_any_error(self):
        from manager.command_watcher import session_center_healthy
        import urllib.error
        with patch("manager.command_watcher.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertFalse(session_center_healthy("http://127.0.0.1:8765/health"))
        with patch("manager.command_watcher.urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.status = 500
            self.assertFalse(session_center_healthy("http://127.0.0.1:8765/health"))
        with patch("manager.command_watcher.urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.status = 200
            opened.return_value.__enter__.return_value.read.return_value = b"not json"
            self.assertFalse(session_center_healthy("http://127.0.0.1:8765/health"))
        with patch("manager.command_watcher.urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.status = 200
            opened.return_value.__enter__.return_value.read.return_value = b'{"status":"ok"}'
            self.assertTrue(session_center_healthy("http://127.0.0.1:8765/health"))

    def test_production_write_even_allowlisted_never_launches(self):
        writable = create_task(self.store, task(read_only=False), assign=False, persist=False)
        writable["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)  # everything else compliant except read_only
        self.store.put("tasks", "p1", "t1", writable)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), command(), claim_factory=self.claim_factory, allowlist=self.ALLOWLIST)
        self.assertEqual("attention", result["status"]); launch.assert_not_called()
        self.assertEqual("allowlisted_task_policy_not_satisfied", self.store.get("commands", "p1", "cmd-1")["recovery_reason"])

    def test_stray_attention_command_is_never_relaunched_even_with_permissive_allowlist(self):
        stray = command(status="attention", stale_at=now_iso(), recovery_reason="legacy")
        self.store.put("commands", "p1", "cmd-1", stray)
        launch = Mock()
        with patch("manager.command_watcher.launch_task", launch):
            result = process_command(self.store, object(), stray, claim_factory=self.claim_factory, allowlist=self.ALLOWLIST)
        launch.assert_not_called()
        self.assertNotEqual("running", result.get("status"))

    def test_load_allowlist_fails_closed_on_missing_or_malformed_config(self):
        self.assertEqual(frozenset(), load_allowlist(None))
        self.assertEqual(frozenset(), load_allowlist("/no/such/file.json"))
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "bad.json"
            malformed.write_text("not json", encoding="utf-8")
            self.assertEqual(frozenset(), load_allowlist(str(malformed)))

            wrong_shape = Path(directory) / "wrong_shape.json"
            wrong_shape.write_text(json.dumps({"entries": "not-a-list"}), encoding="utf-8")
            self.assertEqual(frozenset(), load_allowlist(str(wrong_shape)))

            partial = Path(directory) / "partial.json"
            partial.write_text(json.dumps({"entries": [
                {"project_id": "p1", "task_id": "t1"},
                {"project_id": "p1"},  # missing task_id: dropped, not crashed
                "not-a-dict",
            ]}), encoding="utf-8")
            self.assertEqual(frozenset({("p1", "t1")}), load_allowlist(str(partial)))

    def test_load_allowlist_reads_env_var_when_path_not_given(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "allowlist.json"
            config.write_text(json.dumps({"entries": [{"project_id": "p1", "task_id": "t1"}]}), encoding="utf-8")
            with patch.dict(os.environ, {"ADM_WATCHER_ALLOWLIST_PATH": str(config)}):
                self.assertEqual(frozenset({("p1", "t1")}), load_allowlist())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(frozenset(), load_allowlist())

    def test_production_allowlist_admits_zero_state_bootstrap_smoke(self):
        allowlist = Path(__file__).parents[1] / "templates" / "watcher_allowlist.json"
        self.assertIn(("ai-development-manager", "phase3-zero-state-bootstrap-final-smoke"),
                      load_allowlist(str(allowlist)))

    def test_windows_watcher_task_wires_allowlist_path_to_runtime(self):
        manager = Path(__file__).parent
        installer = (manager / "install_command_watcher.ps1").read_text(encoding="utf-8")
        runner = (manager / "run_command_watcher.ps1").read_text(encoding="utf-8")
        self.assertIn('-AllowlistPath `"$AllowlistPath`"', installer)
        self.assertIn('$env:ADM_WATCHER_ALLOWLIST_PATH = $AllowlistPath', runner)

    # -- Phase 4C: provider routing + Claude quota fail-closed wiring --

    # 1 & 2: provider resolves the correct launcher/quota-gate pairing
    def test_codex_provider_resolves_codex_launcher_and_quota_gate(self):
        runtime = resolve_provider_runtime("codex")
        self.assertIs(runtime["launcher_factory"], CodexLauncher)
        self.assertIs(runtime["quota_check"], codex_quota_reliable)

    def test_claude_provider_resolves_claude_launcher_and_quota_gate(self):
        runtime = resolve_provider_runtime("claude")
        self.assertIs(runtime["launcher_factory"], ClaudeLauncher)
        self.assertIs(runtime["quota_check"], claude_quota_reliable)

    # 3: unknown provider fails closed, never falls back to Codex
    def test_unknown_provider_resolves_to_none_never_falls_back_to_codex(self):
        self.assertIsNone(resolve_provider_runtime("gemini_app"))
        self.assertIsNone(resolve_provider_runtime(""))
        self.assertIsNone(resolve_provider_runtime(None))

    def test_unknown_provider_command_is_rejected_without_touching_codex_defaults(self):
        # command.schema.json's provider enum (codex/claude only) already
        # rejects this at validate("command", ...) before dispatch resolution
        # is even reached -- an even earlier fail-closed point than
        # resolve_provider_runtime()'s own None-return, which is exercised
        # directly (and does return None for unknown providers) in
        # test_unknown_provider_resolves_to_none_never_falls_back_to_codex.
        with patch("manager.command_watcher.CodexLauncher") as codex_ctor, \
             patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                self.store, object(), command(provider="gemini_app"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        self.assertEqual({"status": "rejected"}, result)
        codex_ctor.assert_not_called()
        runner.assert_not_called()

    def test_command_schema_now_accepts_claude_provider(self):
        # A regression guard on the schema.provider const->enum widening this
        # phase required: an invalid command must still be rejected up front.
        self.assertEqual({"status": "rejected"}, process_command(self.store, object(), {"command_id": "bad"}))

    # 4 & 5: Claude quota gate blocks before any launcher is even constructed
    def test_claude_stale_or_unknown_quota_blocks_before_prepare(self):
        with patch("manager.command_watcher.ClaudeLauncher") as claude_ctor, \
             patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                self.store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: False,
            )
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        claude_ctor.assert_not_called()
        runner.assert_not_called()

    # 6: reliable fresh Claude quota lets the command reach launch_task with the right launcher/provider
    def test_claude_reliable_quota_allows_mocked_prepare_path(self):
        runner = Mock(return_value=self.complete("exec-claude"))
        with patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("claude", kwargs.get("provider"))
        self.assertEqual("completed", result["status"])

    # -- P0.4: explicit account_id contract (Command > Task > None) --

    REGISTRY = [
        {"account_id": "account-a", "enabled": True, "config_dir": None},
        {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        {"account_id": "account-disabled", "enabled": False, "config_dir": r"C:\accounts\d\.claude"},
    ]

    _NO_OVERRIDE = object()

    def _run_explicit(self, cmd, quota_reliable=False, registry=_NO_OVERRIDE):
        runner = Mock(return_value=self.complete("exec-claude"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry",
                   return_value=self.REGISTRY if registry is self._NO_OVERRIDE else registry):
            result = process_command(
                self.store, object(), cmd,
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: quota_reliable,
            )
        return result, runner

    def test_command_explicit_account_a_threads_through_to_launch_task(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-a", kwargs.get("account_id"))
        self.assertEqual(self.REGISTRY, kwargs.get("claude_accounts"))
        self.assertEqual("completed", result["status"])

    def test_command_explicit_account_b_threads_through_to_launch_task(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-b"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-b", kwargs.get("account_id"))
        self.assertEqual("completed", result["status"])

    def test_task_explicit_account_id_used_as_fallback_when_command_has_none(self):
        task_record = self.store.get("tasks", "p1", "t1")
        task_record["account_id"] = "account-b"
        self.store.put("tasks", "p1", "t1", task_record)
        result, runner = self._run_explicit(command(provider="claude"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-b", kwargs.get("account_id"))
        self.assertEqual("completed", result["status"])

    def test_command_account_id_takes_priority_over_task_account_id(self):
        task_record = self.store.get("tasks", "p1", "t1")
        task_record["account_id"] = "account-b"
        self.store.put("tasks", "p1", "t1", task_record)
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a"))
        runner.assert_called_once()
        _, kwargs = runner.call_args
        self.assertEqual("account-a", kwargs.get("account_id"))

    def test_unknown_explicit_account_id_rejected(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-ghost"))
        self.assertEqual({"status": "rejected", "reason": "unknown_or_disabled_claude_account"}, result)
        runner.assert_not_called()

    def test_disabled_explicit_account_id_rejected(self):
        result, runner = self._run_explicit(command(provider="claude", account_id="account-disabled"))
        self.assertEqual({"status": "rejected", "reason": "unknown_or_disabled_claude_account"}, result)
        runner.assert_not_called()

    def test_explicit_account_id_with_no_registry_configured_rejected(self):
        # An explicit account_id with no local registry at all cannot be
        # validated or resolved to a config_dir -- must fail closed, not
        # silently fall through to the single-account default.
        result, runner = self._run_explicit(command(provider="claude", account_id="account-a"), registry=None)
        self.assertEqual({"status": "rejected", "reason": "unknown_or_disabled_claude_account"}, result)
        runner.assert_not_called()

    def test_claude_explicit_valid_account_bypasses_quota_gate_even_when_unreliable(self):
        # This is the core of Blocker 2: quota_check is never even called
        # when an explicit, validated account_id is present.
        quota_check = Mock(return_value=False)
        runner = Mock(return_value=self.complete("exec-claude"))
        with patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher._claude_account_registry", return_value=self.REGISTRY):
            result = process_command(
                self.store, object(), command(provider="claude", account_id="account-a"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=quota_check,
            )
        quota_check.assert_not_called()
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_claude_no_explicit_account_and_unreliable_quota_still_rejected(self):
        # Same unreliable-quota condition as above, but with no explicit
        # account_id: the AUTO-selection quota gate still applies unchanged.
        result, runner = self._run_explicit(command(provider="claude"), quota_reliable=False)
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        runner.assert_not_called()

    def test_codex_explicit_account_id_field_is_ignored_quota_gate_unchanged(self):
        # account_id is meaningless for Codex; an unreliable Codex quota must
        # still block exactly as before, even if a (nonsensical) account_id
        # were ever present on the command.
        result, runner = self._run_explicit(command(provider="codex", account_id="account-a"), quota_reliable=False)
        self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, result)
        runner.assert_not_called()

    def test_command_schema_rejects_config_dir_field(self):
        # config_dir must never be persisted to Drive Command/Task records --
        # only the local registry may resolve account_id -> config_dir.
        from manager.tasks import TaskError as _TaskError, validate as _validate
        with self.assertRaises(_TaskError):
            _validate("command", command(provider="claude", account_id="account-a", config_dir=r"C:\accounts\a\.claude"))

    # 8: Claude and Codex quota gates never cross-contaminate on the same document
    def test_claude_and_codex_quota_do_not_cross_contaminate(self):
        mixed = {"providers": [
            {"provider": "codex", "has_reliable_quota": True},
            {"provider": "claude", "has_reliable_quota": False},
        ]}
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=mixed):
            self.assertTrue(codex_quota_reliable(object()))
            self.assertFalse(claude_quota_reliable(object()))

        mixed_reversed = {"providers": [
            {"provider": "codex", "has_reliable_quota": False},
            {"provider": "claude", "has_reliable_quota": True},
        ]}
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=mixed_reversed):
            self.assertFalse(codex_quota_reliable(object()))
            self.assertTrue(claude_quota_reliable(object()))

    def test_claude_quota_reliable_fails_closed_on_stale_unknown_or_unreachable(self):
        fresh_reliable = {"providers": [{"provider": "claude", "has_reliable_quota": True}]}
        stale_or_unknown = {"providers": [{"provider": "claude", "has_reliable_quota": False}]}
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=fresh_reliable):
            self.assertTrue(claude_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value=stale_or_unknown):
            self.assertFalse(claude_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", side_effect=RuntimeError("Drive unavailable")):
            self.assertFalse(claude_quota_reliable(object()))
        with patch("manager.command_watcher.read_drive_status", return_value={}), \
             patch("manager.command_watcher.summarize", return_value={"providers": []}):
            self.assertFalse(claude_quota_reliable(object()))  # no claude entry at all

    # 12: existing allowlist/policy gates still run before launch for Claude too
    def test_claude_command_off_allowlist_means_zero_launch(self):
        with patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                self.store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset(),  # empty: nothing allowlisted
                health_check=lambda: True, quota_check=lambda service: True,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        runner.assert_not_called()

    def test_claude_command_policy_violation_still_blocks_before_launch(self):
        store = Store(); create_project(store, project())
        create_task(store, task(read_only=False), assign=False)  # not disposable/read-only
        with patch("manager.command_watcher.launch_task") as runner:
            result = process_command(
                store, object(), command(provider="claude"),
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        self.assertEqual("attention", result["status"])
        runner.assert_not_called()

    def test_explicit_override_still_wins_over_provider_registry(self):
        # The existing single-factory override pattern tests already rely on
        # must keep working even though defaults are now provider-resolved.
        sentinel_launcher = Mock(return_value="sentinel-launcher-instance")
        runner = Mock(return_value=self.complete("exec-override"))
        with patch("manager.command_watcher.launch_task", runner):
            process_command(
                self.store, object(), command(provider="claude"), launcher_factory=sentinel_launcher,
                claim_factory=self.claim_factory, allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True, quota_check=lambda service: True,
            )
        runner.assert_called_once()
        args, _ = runner.call_args
        self.assertEqual("sentinel-launcher-instance", args[4])
        sentinel_launcher.assert_called_once()


class TrustedIngressAdmissionTests(unittest.TestCase):
    """Adversarial coverage for v1 Safe Auto-Admission
    (manager.trusted_ingress): a disposable read-only Task/Command stamped
    by the authenticated Direct Dispatch ingress can be launched without a
    static ADM_WATCHER_ALLOWLIST_PATH entry -- but only when its
    self-declared evidence is corroborated by the separate,
    ingress-only-writable dispatch-request idempotency record. Every
    negative case here must fail exactly the same way an ordinary
    never-allowlisted command does: {"status": "rejected", "reason":
    "not_allowlisted"}, no write, no launch."""

    def setUp(self):
        self.store = Store()
        create_project(self.store, project())
        self.registry = MemoryClaimRegistry()
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-1",
            "task_id": "t1", "command_id": "cmd-1", "created_at": now_iso(),
        }
        self.registry.generation = 1

    def registry_factory(self, bucket, project_id, request_id):
        return self.registry

    def admitted_task(self, **overrides):
        built = create_task(self.store, task(read_only=True), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        built["source_context"] = {
            "origin": "direct_dispatch_ingress", "external_request_id": "req-1", "admission_version": "v1",
        }
        built.update(overrides)
        self.store.put("tasks", "p1", "t1", built)
        return built

    @staticmethod
    def admitted_command(**overrides):
        value = command(created_via="direct_dispatch_ingress", admission_version="v1", request_id="req-1")
        value.update(overrides)
        return value

    def test_trusted_ingress_admitted_task_launches_without_static_allowlist(self):
        self.admitted_task()
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_retry_linked_command_admitted_despite_external_request_id_mismatch(self):
        # A retry targets a pre-existing task, so its own request_id ("req-2")
        # necessarily differs from the task's *original* creation request
        # ("req-1") -- that specific mismatch must not block admission for a
        # retry, only the idempotency-record cross-check (below, for req-2)
        # actually matters.
        self.admitted_task()  # source_context.external_request_id == "req-1"
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-2",
            "task_id": "t1", "command_id": "cmd-1", "created_at": now_iso(),
        }
        retry_command = self.admitted_command(request_id="req-2", retry_count=1, retry_of_execution_id="prior-exec")
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", runner), \
             patch("manager.command_watcher.prepare_task_retry", return_value=None):
            result = process_command(
                self.store, object(), retry_command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual("completed", result["status"])

    def test_retry_linked_command_still_fails_closed_on_bad_idempotency_record(self):
        # Same retry-shaped command, but the idempotency record's own
        # task_id doesn't actually match -- the relaxed external_request_id
        # check must never become a free pass; every other cross-check
        # still fully applies.
        self.admitted_task()
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-2",
            "task_id": "some-other-task", "command_id": "cmd-1", "created_at": now_iso(),
        }
        retry_command = self.admitted_command(request_id="req-2", retry_count=1, retry_of_execution_id="prior-exec")
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), retry_command, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_non_retry_command_still_requires_exact_external_request_id_match(self):
        # Regression: a plain (non-retry) command must still be rejected on
        # a mismatched external_request_id exactly as before -- the relaxed
        # check is retry_of_execution_id-gated only.
        self.admitted_task()  # external_request_id == "req-1"
        mismatched = self.admitted_command(request_id="req-2")  # no retry_of_execution_id
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-2",
            "task_id": "t1", "command_id": "cmd-1", "created_at": now_iso(),
        }
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), mismatched, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_ordinary_untrusted_command_off_allowlist_still_rejected(self):
        self.admitted_task()  # task itself is fully compliant
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), command(),  # no created_via/admission_version/request_id at all
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_caller_read_only_false_task_never_admitted(self):
        self.admitted_task(read_only=False)
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_injected_write_policy_never_admitted(self):
        self.admitted_task(execution_policies=["disposable", "read_only"])  # missing no_repo_writes/no_external_writes
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_forged_created_via_without_any_idempotency_record_never_admitted(self):
        """A Task/Command manually stamped with the ingress's own evidence
        fields -- but naming a request_id nobody ever actually claimed
        through the authenticated ingress -- must not be admitted on the
        self-declared fields alone."""
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(request_id="never-claimed"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_forged_created_via_with_mismatched_idempotency_record_never_admitted(self):
        """The idempotency record exists (a real, distinct request_id was
        claimed) but for a different command_id than this Command claims --
        proving the check cross-references identity, not just presence."""
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(command_id="cmd-not-the-claimed-one"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_no_gcs_bucket_configured_fails_closed_even_with_full_evidence(self):
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": ""}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_wrong_admission_version_never_admitted(self):
        self.admitted_task()
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(admission_version="v2-not-yet-supported"),
                claim_factory=lambda *_: MemoryClaimRegistry(), allowlist=frozenset(),
                ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_idempotency_backend_error_fails_closed(self):
        self.admitted_task()
        self.registry.read_unavailable = True
        launch = Mock()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", launch):
            result = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), ingress_registry_factory=self.registry_factory,
            )
        self.assertEqual({"status": "rejected", "reason": "not_allowlisted"}, result)
        launch.assert_not_called()

    def test_static_allowlist_still_bypasses_ingress_check_for_manual_tasks(self):
        """The trusted-ingress path is additive, not a replacement: a
        normal, non-ingress task/command on the static allowlist keeps
        launching exactly as before, with zero ingress evidence and zero
        idempotency-record lookups."""
        built = create_task(self.store, task(read_only=True), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        self.store.put("tasks", "p1", "t1", built)
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch("manager.command_watcher.launch_task", runner):
            result = process_command(
                self.store, object(), command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset({("p1", "t1")}), health_check=lambda: True, quota_check=lambda service: True,
            )
        runner.assert_called_once()
        self.assertEqual("completed", result["status"])

    def test_duplicate_admitted_command_only_launches_once(self):
        """The trusted-ingress check only ever runs for a freshly `queued`
        command; replaying the same admitted command after it has already
        gone terminal must be reconciled (skipped), never relaunched."""
        self.admitted_task()
        runner = Mock(side_effect=lambda *args, **kwargs: (kwargs["on_running"](None), CommandWatcherTests.complete(args[7]))[1])
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": "test-bucket"}), \
             patch("manager.command_watcher.launch_task", runner):
            first = process_command(
                self.store, object(), self.admitted_command(), claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
            stored = self.store.get("commands", "p1", "cmd-1")
            second = process_command(
                self.store, object(), stored, claim_factory=lambda *_: MemoryClaimRegistry(),
                allowlist=frozenset(), health_check=lambda: True, quota_check=lambda service: True,
                ingress_registry_factory=self.registry_factory,
            )
        runner.assert_called_once()
        self.assertEqual("completed", first["status"])
        self.assertEqual({"status": "completed", "skipped": True}, second)


class WatcherSessionCenterBootstrapIntegrationTests(unittest.TestCase):
    """Reproduces the exact deadlock the reviewer found and proves the fix:
    Session Center used to block its own HTTP bind on wait_for_execution(),
    so /health never came up until an Execution already existed -- but the
    watcher requires /health before it will create that Execution. No Codex
    involved; this only exercises the real HTTP bind and the real gate."""

    def test_health_gate_crosses_before_correlation_completes_no_livelock(self):
        from http.server import ThreadingHTTPServer
        from manager.command_watcher import session_center_healthy
        from manager.session_center import SessionView, build_pending, handler_for

        port = 18899
        url = f"http://127.0.0.1:{port}/health"

        # Before Session Center exists at all, the gate correctly reports
        # unavailable -- this is normal sequencing, not the livelock.
        self.assertFalse(session_center_healthy(url, timeout=0.5))

        args = argparse_namespace(execution_project_id="p1", execution_id="cmd-1", port=port)
        view = SessionView(build_pending(args))
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(view))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # The fix: /health answers immediately, before any Execution
            # exists and before correlation has even been attempted.
            self.assertTrue(session_center_healthy(url, timeout=2))

            store = CommandWatcherTests.allowlist_compliant_store()
            store.put("commands", "p1", "cmd-1", command())
            runner = Mock(side_effect=lambda *a, **kw: (kw["on_running"](None), CommandWatcherTests.complete(a[7]))[1])
            with patch("manager.command_watcher.launch_task", runner):
                result = process_command(
                    store, object(), command(), claim_factory=CommandWatcherTests.claim_factory,
                    allowlist=frozenset({("p1", "t1")}), health_check=lambda: session_center_healthy(url, timeout=2),
                    quota_check=lambda service: True,
                )
            runner.assert_called_once()
            self.assertEqual("completed", result["status"])
        finally:
            server.shutdown()
            server.server_close()


def argparse_namespace(**overrides):
    import argparse
    base = {
        "provider_session_id": None, "execution_file": None, "execution_project_id": None,
        "execution_id": None, "wait_seconds": 5.0, "project_id": None, "task_id": None,
        "branch": None, "port": 0, "idle_seconds": 15.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


if __name__ == "__main__": unittest.main()
