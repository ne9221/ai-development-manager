import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.session_center_supervisor import (
    decide, find_active_command, read_state, target_execution_id, write_state,
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


class SupervisorTests(unittest.TestCase):
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

    def test_read_state_fails_closed_on_missing_or_malformed_file(self):
        self.assertEqual((None, None), read_state("/no/such/file.json"))
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            bad.write_text("not json", encoding="utf-8")
            self.assertEqual((None, None), read_state(str(bad)))

            wrong_shape = Path(directory) / "wrong.json"
            wrong_shape.write_text(json.dumps({"pid": "not-an-int", "execution_id": "x"}), encoding="utf-8")
            self.assertEqual((None, None), read_state(str(wrong_shape)))

    def test_write_then_read_state_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            write_state(path, 4242, "exec-a")
            self.assertEqual((4242, "exec-a"), read_state(path))

    def test_decide_no_target_and_no_prior_state_does_nothing(self):
        store = Store({})
        should_respawn, execution_id, project_id = decide(store, frozenset({("p1", "t1")}), None, None)
        self.assertFalse(should_respawn); self.assertIsNone(execution_id); self.assertIsNone(project_id)

    def test_decide_no_target_but_prior_state_exists_clears_it(self):
        store = Store({})
        should_respawn, execution_id, project_id = decide(store, frozenset({("p1", "t1")}), 999, "stale-exec")
        self.assertTrue(should_respawn); self.assertIsNone(execution_id); self.assertIsNone(project_id)

    def test_decide_new_target_different_from_state_respawns(self):
        store = Store({"p1": [cmd(execution_id="fresh-exec")]})
        should_respawn, execution_id, project_id = decide(store, frozenset({("p1", "t1")}), None, None)
        self.assertTrue(should_respawn); self.assertEqual("fresh-exec", execution_id); self.assertEqual("p1", project_id)

    def test_decide_same_target_but_process_dead_respawns(self):
        store = Store({"p1": [cmd(execution_id="exec-a")]})
        with patch("manager.session_center_supervisor.process_alive", return_value=False):
            should_respawn, execution_id, _ = decide(store, frozenset({("p1", "t1")}), 111, "exec-a")
        self.assertTrue(should_respawn); self.assertEqual("exec-a", execution_id)

    def test_decide_same_target_and_process_alive_does_nothing(self):
        store = Store({"p1": [cmd(execution_id="exec-a")]})
        with patch("manager.session_center_supervisor.process_alive", return_value=True):
            should_respawn, execution_id, _ = decide(store, frozenset({("p1", "t1")}), 111, "exec-a")
        self.assertFalse(should_respawn); self.assertEqual("exec-a", execution_id)

    def test_process_alive_reflects_real_current_process(self):
        from manager.session_center_supervisor import process_alive
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(None))


if __name__ == "__main__":
    unittest.main()
