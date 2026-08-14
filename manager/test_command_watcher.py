import json
import tempfile
import unittest
import os
import socket
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from manager.codex_launcher import process_creation_identity
from manager.command_watcher import (
    CLAIM_TIMEOUT_SECONDS, REQUIRED_TASK_POLICIES, _provider_state, load_allowlist, poll_once, process_command,
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

    def running_command(self, heartbeat_minutes=0, started_minutes=1, pid=None, legacy=False):
        now = datetime.now(timezone.utc)
        started = self.iso(now - timedelta(minutes=started_minutes))
        reserve_execution(self.store, "p1", "t1", "command-cmd-1", "codex", {"decision": "fresh"})
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", "command-cmd-1", "codex",
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
        active = command(status="running", execution_id="command-cmd-1", claimed_at=started)
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


if __name__ == "__main__": unittest.main()
