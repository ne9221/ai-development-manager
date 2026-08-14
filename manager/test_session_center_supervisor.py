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
    decide, find_active_command, kill, lock_path_for, main, port_available,
    read_state, run_once, target_execution_id, write_state,
)


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
        self.assertEqual({"action": "respawn", "execution_id": "fresh-exec", "project_id": "p1", "kill_pid": None}, result)

    def test_same_target_but_process_stopped_respawns_without_killing(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="stopped"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-a", "project_id": "p1", "kill_pid": None}, result)

    def test_same_target_but_pid_reused_by_different_process_respawns_without_killing_the_impostor(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="replaced"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-a", "project_id": "p1", "kill_pid": None}, result)

    def test_same_target_but_identity_unreadable_fails_closed_and_respawns_without_killing(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="unknown"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-a", "project_id": "p1", "kill_pid": None}, result)

    def test_same_target_and_verified_live_is_idempotent_noop(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="live"):
            result = decide((111, "exec-a", "id-111"), cmd(execution_id="exec-a"))
        self.assertEqual({"action": "noop"}, result)

    def test_target_switch_with_verified_old_child_kills_old_and_spawns_new(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="live"):
            result = decide((111, "exec-old", "id-111"), cmd(execution_id="exec-new"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-new", "project_id": "p1", "kill_pid": 111}, result)

    def test_target_switch_with_unverified_old_child_spawns_new_without_killing(self):
        with patch("manager.session_center_supervisor.process_identity_state", return_value="unknown"):
            result = decide((111, "exec-old", "id-111"), cmd(execution_id="exec-new"))
        self.assertEqual({"action": "respawn", "execution_id": "exec-new", "project_id": "p1", "kill_pid": None}, result)


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
