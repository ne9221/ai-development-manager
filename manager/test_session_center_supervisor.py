import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from manager.refresh_status import RefreshError, runtime_lock
from manager.session_center_supervisor import (
    attempt_orphan_recovery, decide, evidence_path_for, find_active_command, kill, lock_path_for, main,
    port_available, read_evidence, read_state, run_once, target_execution_id, verify_adm_session_center_ownership,
    write_state,
)
from manager.tasks import create_project, create_task
from manager.test_command_watcher import Store as DriveStore, command as ingress_command
from manager.test_execution_lifecycle import project, task
from manager.test_task_claims import MemoryClaimRegistry
from manager.trusted_ingress import REQUIRED_TASK_POLICIES


class Store:
    def __init__(self, records):
        self.records = records  # {project_id: [command_dict, ...]}

    def list_records(self, area, project_id):
        assert area == "commands"
        return list(self.records.get(project_id, []))


def cmd(command_id="c1", project_id="p1", task_id="t1", status="queued", execution_id=None, created_at="2026-08-14T00:00:00Z"):
    return {"command_id": command_id, "project_id": project_id, "task_id": task_id,
            "status": status, "execution_id": execution_id, "created_at": created_at}


class FindActiveCommandTests(unittest.TestCase):
    def test_empty_allowlist_never_finds_a_target(self):
        store = Store({"p1": [cmd()]})
        self.assertIsNone(find_active_command(store, frozenset()))

    def test_only_allowlisted_project_task_pairs_are_candidates(self):
        store = Store({"p1": [cmd(task_id="t1"), cmd(command_id="c2", task_id="other-task")]})
        result = find_active_command(store, frozenset({("p1", "t1")}))
        self.assertEqual("c1", result["command_id"])

    def test_terminal_commands_are_never_candidates(self):
        store = Store({"p1": [cmd(status="completed"), cmd(command_id="c2", status="failed")]})
        self.assertIsNone(find_active_command(store, frozenset({("p1", "t1")})))

    def test_most_recently_created_active_command_wins(self):
        store = Store({"p1": [
            cmd(command_id="old", created_at="2026-08-14T00:00:00Z"),
            cmd(command_id="new", created_at="2026-08-14T01:00:00Z"),
        ]})
        result = find_active_command(store, frozenset({("p1", "t1")}))
        self.assertEqual("new", result["command_id"])

    def test_target_execution_id_falls_back_to_deterministic_command_prefix(self):
        self.assertEqual("command-c1", target_execution_id(cmd(execution_id=None)))
        self.assertEqual("real-exec-id", target_execution_id(cmd(execution_id="real-exec-id")))


class FindActiveCommandTrustedIngressTests(unittest.TestCase):
    """Session Center's active-command discovery must see the exact same set
    of launchable commands manager.command_watcher does -- a command Command
    Watcher will launch under the v1 trusted-ingress contract (see
    manager.trusted_ingress) must never be invisible here just because it is
    off the static ADM_WATCHER_ALLOWLIST_PATH allowlist. This reuses
    verify_trusted_ingress_admission directly (no re-implementation), so
    every adversarial case it already defends against is covered here too."""

    def setUp(self):
        self.store = DriveStore()
        create_project(self.store, project())
        self.registry = MemoryClaimRegistry()
        self.registry.document = {
            "schema_version": "0.1.0", "project_id": "p1", "request_id": "req-1",
            "task_id": "t1", "command_id": "cmd-1", "created_at": "2026-08-14T00:00:00Z",
        }
        self.registry.generation = 1

    def registry_factory(self, bucket, project_id, request_id):
        return self.registry

    def admitted_task(self, **overrides):
        built = task(read_only=True)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        built["source_context"] = {
            "origin": "direct_dispatch_ingress", "external_request_id": "req-1", "admission_version": "v1",
        }
        built.update(overrides)
        create_task(self.store, built, assign=False)
        return built

    @staticmethod
    def admitted_command(**overrides):
        value = ingress_command(created_via="direct_dispatch_ingress", admission_version="v1", request_id="req-1")
        value.update(overrides)
        return value

    def test_a_static_allowlist_path_still_works_unchanged_even_with_bucket_configured(self):
        """Requirement A: an ordinary allowlisted command (no trusted-ingress
        evidence at all) must still be discovered exactly as before, whether
        or not a GCS bucket is now also configured."""
        self.store.put("commands", "p1", "cmd-1", ingress_command())
        result = find_active_command(self.store, frozenset({("p1", "t1")}), bucket="test-bucket",
                                     ingress_registry_factory=self.registry_factory)
        self.assertEqual("cmd-1", result["command_id"])

    def test_b_valid_trusted_ingress_command_discovered_with_empty_static_allowlist(self):
        """Requirement B: a fully-evidenced trusted-ingress command must be
        found even though the static allowlist never names it."""
        self.admitted_task()
        self.store.put("commands", "p1", "cmd-1", self.admitted_command())
        result = find_active_command(self.store, frozenset(), bucket="test-bucket",
                                     ingress_registry_factory=self.registry_factory)
        self.assertIsNotNone(result)
        self.assertEqual("cmd-1", result["command_id"])

    def test_c_forged_evidence_without_real_idempotency_record_never_discovered(self):
        """Requirement C: created_via/admission_version/request_id alone --
        with no corroborating GCS record ever actually claimed through the
        authenticated ingress -- must not be enough."""
        self.admitted_task()
        self.registry.document = None  # nothing was ever really claimed through the ingress
        self.store.put("commands", "p1", "cmd-1", self.admitted_command())
        result = find_active_command(self.store, frozenset(), bucket="test-bucket",
                                     ingress_registry_factory=self.registry_factory)
        self.assertIsNone(result)

    def test_d_unrelated_stale_codex_command_excluded(self):
        """Requirement D: an ordinary queued command with no ingress evidence
        and off the static allowlist stays invisible, exactly like today."""
        self.store.put("commands", "p1", "cmd-1", ingress_command(provider="codex"))
        result = find_active_command(self.store, frozenset(), bucket="test-bucket",
                                     ingress_registry_factory=self.registry_factory)
        self.assertIsNone(result)

    def test_no_bucket_configured_never_evaluates_trusted_ingress_path(self):
        """Fail closed the other direction too: without ADM_LOCK_GCS_BUCKET
        configured, a trusted-ingress command is not discovered -- identical
        to pre-fix behavior, never a silent new default-allow."""
        self.admitted_task()
        self.store.put("commands", "p1", "cmd-1", self.admitted_command())
        result = find_active_command(self.store, frozenset(), bucket=None,
                                     ingress_registry_factory=self.registry_factory)
        self.assertIsNone(result)

    def test_trusted_ingress_command_never_double_counted_when_also_allowlisted(self):
        self.admitted_task()
        self.store.put("commands", "p1", "cmd-1", self.admitted_command())
        result = find_active_command(self.store, frozenset({("p1", "t1")}), bucket="test-bucket",
                                     ingress_registry_factory=self.registry_factory)
        self.assertEqual("cmd-1", result["command_id"])


class StateTests(unittest.TestCase):
    def test_read_state_fails_closed_on_missing_or_malformed_file(self):
        self.assertEqual((None, None, None), read_state("/no/such/file.json"))
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            bad.write_text("not json", encoding="utf-8")
            self.assertEqual((None, None, None), read_state(str(bad)))

            missing_identity = Path(directory) / "no_identity.json"
            missing_identity.write_text(json.dumps({"pid": 111, "execution_id": "x"}), encoding="utf-8")
            self.assertEqual((None, None, None), read_state(str(missing_identity)), "legacy state without identity must fail closed")

            wrong_types = Path(directory) / "wrong_types.json"
            wrong_types.write_text(json.dumps({"pid": "not-an-int", "execution_id": "x", "creation_identity": "y"}), encoding="utf-8")
            self.assertEqual((None, None, None), read_state(str(wrong_types)))

    def test_write_then_read_state_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            write_state(path, 4242, "exec-a", "windows-filetime:123")
            self.assertEqual((4242, "exec-a", "windows-filetime:123"), read_state(path))

    def test_atomic_write_failure_never_corrupts_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            write_state(path, 111, "exec-a", "identity-a")
            original = Path(path).read_text(encoding="utf-8")
            with patch("manager.session_center_supervisor.write_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_state(path, 222, "exec-b", "identity-b")
            self.assertEqual(original, Path(path).read_text(encoding="utf-8"))
            self.assertEqual((111, "exec-a", "identity-a"), read_state(path))

    def test_stale_or_malformed_temp_file_does_not_affect_real_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            (Path(directory) / "state.json.tmp").write_text("garbage from a prior crash", encoding="utf-8")
            write_state(str(path), 333, "exec-c", "identity-c")
            self.assertEqual((333, "exec-c", "identity-c"), read_state(str(path)))
            self.assertFalse((Path(directory) / "state.json.tmp").exists(), "temp file must not linger after a successful replace")

    def test_no_partial_json_is_ever_observable_after_a_successful_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_state(str(path), 1, "a", "id-1")
            write_state(str(path), 2, "b", "id-2")
            # Whatever is on disk right now must be one complete, valid record.
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"pid": 2, "execution_id": "b", "creation_identity": "id-2"}, data)


class DecideTests(unittest.TestCase):
    def test_no_target_and_no_prior_state_does_nothing(self):
        self.assertEqual({"action": "noop"}, decide((None, None, None), None))

    def test_no_target_but_unverified_prior_state_clears_without_killing(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="stopped"):
            result = decide((999, "stale-exec", "old-identity"), None)
        self.assertEqual({"action": "clear", "kill_pid": None}, result)

    def test_no_target_but_verified_prior_state_clears_and_kills(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="live"):
            result = decide((999, "stale-exec", "id-999"), None)
        self.assertEqual({"action": "clear", "kill_pid": 999}, result)

    def test_new_target_with_no_prior_state_respawns_without_killing(self):
        target = cmd(execution_id="fresh-exec")
        result = decide((None, None, None), target)
        self.assertEqual({"action": "respawn", "execution_id": "fresh-exec", "project_id": "p1", "provider": "codex", "kill_pid": None}, result)

    def test_same_target_but_process_stopped_respawns_without_killing(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="stopped"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-a", "project_id": "p1", "provider": "codex", "kill_pid": None}, result)

    def test_same_target_but_pid_reused_by_different_process_respawns_without_killing_the_impostor(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="replaced"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-a", "project_id": "p1", "provider": "codex", "kill_pid": None}, result)

    def test_same_target_but_identity_unreadable_fails_closed_and_respawns_without_killing(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="unknown"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-a", "project_id": "p1", "provider": "codex", "kill_pid": None}, result)

    def test_same_target_and_verified_live_is_idempotent_noop(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="live"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "noop"}, result)

    def test_target_switch_with_verified_old_child_kills_old_and_spawns_new(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="live"):
            result = decide((111, "exec-old", "id-111"), cmd(execution_id="exec-new"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-new", "project_id": "p1", "provider": "codex", "kill_pid": 111}, result)

    def test_target_switch_with_unverified_old_child_spawns_new_without_killing(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="unknown"):
            result = decide((111, "exec-old", "id-111"), cmd(execution_id="exec-new"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-new", "project_id": "p1", "provider": "codex", "kill_pid": None}, result)


class PortProbeTests(unittest.TestCase):
    def test_port_available_reflects_real_occupancy(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            self.assertFalse(port_available("127.0.0.1", port))
        finally:
            holder.close()
        self.assertTrue(port_available("127.0.0.1", port))


class RunOnceTests(unittest.TestCase):
    def test_port_occupied_by_unverified_process_fails_closed_and_never_spawns(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                state_path = str(Path(directory) / "state.json")
                store = Store({"p1": [cmd(execution_id="exec-a")]})
                spawn = Mock()
                with patch("manager.session_center_supervisor.spawn_session_center", spawn):
                    result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", port, 60)
                self.assertEqual({"status": "attention", "reason": "port_occupied_unverified"}, result)
                spawn.assert_not_called()
                self.assertFalse(Path(state_path).exists())
        finally:
            holder.close()

    def test_correct_child_already_bound_is_idempotent_no_respawn(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, os.getpid(), "exec-a", "sentinel-identity")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            spawn = Mock()
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.spawn_session_center", spawn):
                result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60)
            self.assertEqual({"status": "unchanged", "execution_id": "exec-a"}, result)
            spawn.assert_not_called()

    def test_target_switch_terminates_old_verified_child_and_spawns_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-old", "id-111")
            store = Store({"p1": [cmd(execution_id="exec-new")]})
            spawned = Mock()
            spawned.pid = 222
            spawn = Mock(return_value=spawned)
            killed = []
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.kill", side_effect=lambda pid: killed.append(pid)), \
                 patch("manager.session_center_supervisor.port_available", return_value=True), \
                 patch("manager.session_center_supervisor.process_creation_identity", return_value="id-222"), \
                 patch("manager.session_center_supervisor.spawn_session_center", spawn):
                result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60)
            self.assertEqual([111], killed)
            spawn.assert_called_once()
            self.assertEqual({"status": "spawned", "execution_id": "exec-new", "pid": 222}, result)
            self.assertEqual((222, "exec-new", "id-222"), read_state(state_path))

    def test_no_target_clears_and_stops_verified_old_child(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-old", "id-111")
            store = Store({})
            killed = []
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.kill", side_effect=lambda pid: killed.append(pid)):
                result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60)
            self.assertEqual([111], killed)
            self.assertEqual({"status": "idle"}, result)
            self.assertEqual((None, None, None), read_state(state_path))


def _genuine_spawn_cmdline(port=8765, project_id="ai-development-manager", execution_id="exec-a", provider="codex",
                            python="python.exe"):
    """Exactly the shape spawn_session_center() actually produces."""
    return (f'"{python}" -m manager.session_center --execution-project-id {project_id} '
            f'--execution-id {execution_id} --wait-seconds 1800.0 --port {port} --provider {provider}')


class VerifyAdmSessionCenterOwnershipTests(unittest.TestCase):
    """P1-G orphan recovery: positive verification only -- see
    verify_adm_session_center_ownership's own docstring. Every case here is
    a pure function call, no real process/subprocess involved."""

    def test_accepts_a_genuine_spawn_session_center_command_line(self):
        self.assertTrue(verify_adm_session_center_ownership(_genuine_spawn_cmdline(port=8765), 8765))

    def test_rejects_wrong_port(self):
        self.assertFalse(verify_adm_session_center_ownership(_genuine_spawn_cmdline(port=8765), 9999))

    def test_rejects_unrelated_process(self):
        self.assertFalse(verify_adm_session_center_ownership('"C:\\nginx\\nginx.exe" -p 8765', 8765))

    def test_rejects_missing_project_identity_argument(self):
        cmdline = '"python.exe" -m manager.session_center --port 8765 --provider codex'
        self.assertFalse(verify_adm_session_center_ownership(cmdline, 8765))

    def test_rejects_missing_provider_argument(self):
        cmdline = '"python.exe" -m manager.session_center --execution-project-id p1 --port 8765'
        self.assertFalse(verify_adm_session_center_ownership(cmdline, 8765))

    def test_rejects_wrong_module(self):
        cmdline = '"python.exe" -m manager.command_watcher --port 8765'
        self.assertFalse(verify_adm_session_center_ownership(cmdline, 8765))

    def test_rejects_empty_or_none_command_line(self):
        self.assertFalse(verify_adm_session_center_ownership("", 8765))
        self.assertFalse(verify_adm_session_center_ownership(None, 8765))


class AttemptOrphanRecoveryTests(unittest.TestCase):
    """Every case here injects fake find_owner_pid/read_cmdline/kill_fn --
    no real subprocess, network, or OS process is ever touched by these
    tests, matching the incident's safety rule: only deterministic
    fake/mock/subprocess-isolated tests for anything process-lifecycle
    shaped."""

    def test_verified_adm_orphan_is_killed_exactly_once_and_port_freed_is_reported(self):
        kill_fn = Mock()
        with patch("manager.session_center_supervisor.port_available", return_value=True):
            result = attempt_orphan_recovery(
                "127.0.0.1", 8765,
                find_owner_pid=lambda host, port: 9999,
                read_cmdline=lambda pid: _genuine_spawn_cmdline(port=8765),
                kill_fn=kill_fn,
            )
        kill_fn.assert_called_once_with(9999)
        self.assertEqual(
            {"verified": True, "owner_pid": 9999, "action": "killed",
             "reason": "verified_adm_orphan_session_center", "port_freed": True},
            result,
        )

    def test_unrelated_process_is_never_killed(self):
        kill_fn = Mock()
        result = attempt_orphan_recovery(
            "127.0.0.1", 8765,
            find_owner_pid=lambda host, port: 4242,
            read_cmdline=lambda pid: '"C:\\nginx\\nginx.exe" -p 8765',
            kill_fn=kill_fn,
        )
        kill_fn.assert_not_called()
        self.assertFalse(result["verified"])
        self.assertEqual(4242, result["owner_pid"])
        self.assertEqual("command_line_not_adm_session_center", result["reason"])

    def test_unreadable_command_line_is_never_killed(self):
        kill_fn = Mock()
        result = attempt_orphan_recovery(
            "127.0.0.1", 8765,
            find_owner_pid=lambda host, port: 4242,
            read_cmdline=lambda pid: None,
            kill_fn=kill_fn,
        )
        kill_fn.assert_not_called()
        self.assertFalse(result["verified"])
        self.assertEqual("command_line_unreadable", result["reason"])

    def test_unknown_port_owner_is_never_killed(self):
        kill_fn = Mock()
        result = attempt_orphan_recovery(
            "127.0.0.1", 8765,
            find_owner_pid=lambda host, port: None,
            read_cmdline=lambda pid: _genuine_spawn_cmdline(),
            kill_fn=kill_fn,
        )
        kill_fn.assert_not_called()
        self.assertFalse(result["verified"])
        self.assertIsNone(result["owner_pid"])
        self.assertEqual("port_owner_unknown", result["reason"])


class RunOnceOrphanRecoveryIntegrationTests(unittest.TestCase):
    """run_once()'s own recover_orphan defaults to a no-op (see
    _no_orphan_recovery) so these must explicitly inject a fake to exercise
    the wiring -- no real subprocess call happens in this class either."""

    def test_unrelated_occupied_port_stays_fail_closed_and_never_spawns(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                state_path = str(Path(directory) / "state.json")
                store = Store({"p1": [cmd(execution_id="exec-a")]})
                spawn = Mock()
                fake_recover = Mock(return_value={"verified": False, "owner_pid": 4242, "action": "none",
                                                   "reason": "command_line_not_adm_session_center"})
                with patch("manager.session_center_supervisor.spawn_session_center", spawn):
                    result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", port, 60,
                                       recover_orphan=fake_recover)
                self.assertEqual({"status": "attention", "reason": "port_occupied_unverified"}, result)
                spawn.assert_not_called()
                self.assertFalse(Path(state_path).exists())
                evidence = read_evidence(evidence_path_for(state_path))
                self.assertEqual("port_occupied_unverified", evidence["degraded_reason"])
                self.assertEqual("orphan_check", evidence["last_remediation"]["event"])
                self.assertFalse(evidence["last_remediation"]["verified"])
        finally:
            holder.close()

    def test_verified_orphan_is_recovered_and_a_fresh_session_center_is_spawned(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                state_path = str(Path(directory) / "state.json")
                store = Store({"p1": [cmd(execution_id="exec-a")]})
                spawned = Mock()
                spawned.pid = 777
                spawn = Mock(return_value=spawned)
                fake_recover = Mock(return_value={"verified": True, "owner_pid": 9999, "action": "killed",
                                                   "reason": "verified_adm_orphan_session_center", "port_freed": True})
                # Deliberately no port_available patch: the real `holder`
                # socket above already makes the port genuinely occupied on
                # entry, which is what should trigger the orphan-recovery
                # branch in the first place. recover_orphan itself is
                # faked (see AttemptOrphanRecoveryTests for its own real
                # port-freeing behavior), so run_once is only being tested
                # here on whether it *trusts* a verified+port_freed result.
                with patch("manager.session_center_supervisor.spawn_session_center", spawn), \
                     patch("manager.session_center_supervisor.process_creation_identity", return_value="id-777"):
                    result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", port, 60,
                                       recover_orphan=fake_recover)
                spawn.assert_called_once()
                self.assertEqual({"status": "spawned", "execution_id": "exec-a", "pid": 777}, result)
                evidence = read_evidence(evidence_path_for(state_path))
                self.assertEqual("orphan_recovery", evidence["last_remediation"]["event"])
                # last_remediation still shows the orphan-kill that made this
                # possible; recovery_result reflects this cycle's overall
                # outcome, which is the subsequent successful spawn.
                self.assertEqual("spawned", evidence["recovery_result"])
        finally:
            holder.close()


class ReobserveTests(unittest.TestCase):
    """P1-G correlation re-observe: correlation_failed must not be a
    permanent dead end, but this must never re-dispatch a Command or spawn
    a second provider session -- see decide()'s and run_once()'s
    docstrings. read_session_state defaults to a no-op in run_once(), so
    every test here injects a deterministic fake instead of touching a
    real port."""

    def test_correlation_failed_triggers_kill_and_respawn_of_the_same_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-a", "id-111")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            spawned = Mock()
            spawned.pid = 222
            spawn = Mock(return_value=spawned)
            killed = []
            fake_session_state = Mock(return_value={
                "current_state": "correlation_failed", "error": "timed out", "failed_at": "2026-08-01T00:00:00+00:00",
            })
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.kill", side_effect=lambda pid: killed.append(pid)), \
                 patch("manager.session_center_supervisor.port_available", return_value=True), \
                 patch("manager.session_center_supervisor.process_creation_identity", return_value="id-222"), \
                 patch("manager.session_center_supervisor.spawn_session_center", spawn):
                result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60,
                                   read_session_state=fake_session_state)
            # Same execution_id, same project -- a re-observe of the exact
            # thing that failed, never a new dispatch or a different target.
            self.assertEqual([111], killed)
            spawn.assert_called_once_with("python", ".", "p1", "exec-a", 8765, 60, provider="codex")
            self.assertEqual({"status": "spawned", "execution_id": "exec-a", "pid": 222}, result)

    def test_healthy_correlated_session_is_left_alone_not_reobserved(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-a", "id-111")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            spawn = Mock()
            fake_session_state = Mock(return_value={"current_state": "running", "correlated": True})
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.spawn_session_center", spawn):
                result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60,
                                   read_session_state=fake_session_state)
            spawn.assert_not_called()
            self.assertEqual({"status": "unchanged", "execution_id": "exec-a"}, result)

    def test_unreadable_session_state_never_regresses_below_pid_liveness_behavior(self):
        # read_session_state returning None (unreachable/malformed) must
        # behave identically to never having probed at all.
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-a", "id-111")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            spawn = Mock()
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.spawn_session_center", spawn):
                result = run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60,
                                   read_session_state=lambda host, port: None)
            spawn.assert_not_called()
            self.assertEqual({"status": "unchanged", "execution_id": "exec-a"}, result)

    def test_reobserve_records_original_failure_before_the_process_is_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-a", "id-111")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            spawned = Mock()
            spawned.pid = 222
            fake_session_state = Mock(return_value={
                "current_state": "correlation_failed", "error": "timed out", "failed_at": "2026-08-01T00:00:00+00:00",
            })
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.kill"), \
                 patch("manager.session_center_supervisor.port_available", return_value=True), \
                 patch("manager.session_center_supervisor.process_creation_identity", return_value="id-222"), \
                 patch("manager.session_center_supervisor.spawn_session_center", return_value=spawned):
                run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60,
                         read_session_state=fake_session_state)
            evidence = read_evidence(evidence_path_for(state_path))
            entry = next(e for e in evidence["history"] if e["event"] == "correlation_failed_detected")
            self.assertEqual("timed out", entry["error"])
            self.assertEqual("2026-08-01T00:00:00+00:00", entry["failed_at"])
            self.assertEqual("exec-a", entry["execution_id"])
            self.assertEqual({"execution_id": "exec-a", "failed_at": "2026-08-01T00:00:00+00:00", "error": "timed out"},
                              evidence["open_degradation"])

    def test_recovery_appends_new_history_entry_without_rewriting_the_original_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            evidence_path = evidence_path_for(state_path)

            # Cycle 1: correlation_failed detected, killed, respawned as pid 222.
            write_state(state_path, 111, "exec-a", "id-111")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            spawned_222 = Mock(); spawned_222.pid = 222
            failed_state = Mock(return_value={
                "current_state": "correlation_failed", "error": "timed out", "failed_at": "2026-08-01T00:00:00+00:00",
            })
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.kill"), \
                 patch("manager.session_center_supervisor.port_available", return_value=True), \
                 patch("manager.session_center_supervisor.process_creation_identity", return_value="id-222"), \
                 patch("manager.session_center_supervisor.spawn_session_center", return_value=spawned_222):
                run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60,
                         read_session_state=failed_state)
            original_entry = next(e for e in read_evidence(evidence_path)["history"]
                                   if e["event"] == "correlation_failed_detected")

            # Cycle 2: the new process (pid 222) has now correlated successfully.
            recovered_state = Mock(return_value={"current_state": "running", "correlated": True})
            spawn2 = Mock()
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.spawn_session_center", spawn2):
                run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60,
                         read_session_state=recovered_state)
            spawn2.assert_not_called()  # healthy now -- must not respawn again

            evidence = read_evidence(evidence_path)
            # The original failure entry is untouched, byte for byte.
            replayed_original = next(e for e in evidence["history"] if e["event"] == "correlation_failed_detected")
            self.assertEqual(original_entry, replayed_original)
            # A separate "recovered" entry was appended alongside it.
            recovered_entry = next(e for e in evidence["history"] if e["event"] == "recovered")
            self.assertEqual("2026-08-01T00:00:00+00:00", recovered_entry["original_failed_at"])
            self.assertEqual("timed out", recovered_entry["original_error"])
            self.assertIsNone(evidence["open_degradation"])
            self.assertEqual("recovered", evidence["recovery_result"])

    def test_reobserve_never_dispatches_a_second_execution_or_writes_to_the_drive_store(self):
        """Duplicate safety: a reobserve respawn must target the exact same
        execution_id/project_id already on record, and the read-only Store
        stub used throughout this file structurally cannot accept writes --
        it only ever offers list_records()."""
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-a", "id-111")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            self.assertFalse(hasattr(store, "create") or hasattr(store, "write") or hasattr(store, "update"))
            spawned = Mock(); spawned.pid = 222
            spawn = Mock(return_value=spawned)
            fake_session_state = Mock(return_value={"current_state": "correlation_failed", "error": "x", "failed_at": "t"})
            with patch("manager.session_center_supervisor.process_identity_state", return_value="live"), \
                 patch("manager.session_center_supervisor.kill"), \
                 patch("manager.session_center_supervisor.port_available", return_value=True), \
                 patch("manager.session_center_supervisor.process_creation_identity", return_value="id-222"), \
                 patch("manager.session_center_supervisor.spawn_session_center", spawn):
                run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60,
                         read_session_state=fake_session_state)
            spawn.assert_called_once()
            _, _, _, spawned_execution_id, *_ = spawn.call_args[0]
            self.assertEqual("exec-a", spawned_execution_id)  # never a fresh/second execution id


class LockTests(unittest.TestCase):
    def test_second_overlapping_invocation_cannot_acquire_lock_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            lock = lock_path_for(state_path)
            with runtime_lock(lock):
                with self.assertRaises(RefreshError):
                    with runtime_lock(lock):
                        pass
            self.assertFalse(Path(state_path).exists())

    def test_two_real_concurrent_invocations_resolve_to_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            lock = lock_path_for(state_path)
            results = []
            barrier = threading.Barrier(2)

            def attempt():
                barrier.wait()
                try:
                    with runtime_lock(lock):
                        time.sleep(0.2)
                        results.append("acquired")
                except RefreshError:
                    results.append("blocked")

            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.assertEqual(["acquired", "blocked"], sorted(results))

    def test_main_reports_locked_and_spawns_nothing_when_another_invocation_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            lock = lock_path_for(state_path)
            spawn = Mock()
            with runtime_lock(lock):
                with patch("collectors.publish_drive.build_service", return_value=object()), \
                     patch("manager.session_center_supervisor.DriveRecords", return_value=Store({})), \
                     patch("manager.session_center_supervisor.spawn_session_center", spawn), \
                     patch("builtins.print"):
                    main(["--python-path", "python", "--repository-path", ".", "--state-file", state_path])
            spawn.assert_not_called()
            self.assertFalse(Path(state_path).exists())


class ProcessIdentityStateReuseTests(unittest.TestCase):
    def test_kill_only_ever_called_on_a_pid_this_run_verified_as_live(self):
        # Adversarial: a *different* process now owns the recorded pid (PID
        # reuse). The supervisor must never call kill() on it.
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            write_state(state_path, 111, "exec-a", "original-identity")
            store = Store({"p1": [cmd(execution_id="exec-a")]})
            killed = []
            with patch("manager.session_center_supervisor.process_identity_state", return_value="replaced"), \
                 patch("manager.session_center_supervisor.kill", side_effect=lambda pid: killed.append(pid)), \
                 patch("manager.session_center_supervisor.port_available", return_value=True), \
                 patch("manager.session_center_supervisor.process_creation_identity", return_value="new-identity"), \
                 patch("manager.session_center_supervisor.spawn_session_center", return_value=Mock(pid=222)):
                run_once(store, frozenset({("p1", "t1")}), state_path, "python", ".", 8765, 60)
            self.assertEqual([], killed, "must never kill a pid whose identity did not match")


if __name__ == "__main__":
    unittest.main()
