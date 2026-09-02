"""Tests for the durable Antigravity run-state readback store."""

import json
import tempfile
import unittest
from pathlib import Path

from manager.ag_run_state import (
    list_run_states,
    read_run_state,
    run_state_dir,
    update_run_state,
    write_run_state,
)


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_write_read_round_trip_and_location(self):
        path = write_run_state({"thread_id": "ag-cli-abc", "status": "running", "conversation_id": "c1"}, self.home)
        self.assertEqual(Path(self.home, "runtime", "antigravity", "runs"), run_state_dir(self.home))
        self.assertEqual(path, run_state_dir(self.home) / "ag-cli-abc.json")
        state = read_run_state("ag-cli-abc", self.home)
        self.assertEqual(("running", "c1"), (state["status"], state["conversation_id"]))
        self.assertTrue(state["updated_at"].endswith("Z"))

    def test_update_merges_without_losing_fields(self):
        write_run_state({"thread_id": "t1", "status": "prepared", "conversation_id": None, "step_cursor": 0}, self.home)
        update_run_state("t1", self.home, status="running", conversation_id="c9")
        update_run_state("t1", self.home, step_cursor=7)
        state = read_run_state("t1", self.home)
        self.assertEqual(("running", "c9", 7), (state["status"], state["conversation_id"], state["step_cursor"]))

    def test_missing_and_corrupt_states_are_none_not_raising(self):
        self.assertIsNone(read_run_state("never-written", self.home))
        run_state_dir(self.home).mkdir(parents=True, exist_ok=True)
        (run_state_dir(self.home) / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_run_state("broken", self.home))
        self.assertEqual([], list_run_states(self.home))

    def test_listing_filters_terminal_states(self):
        write_run_state({"thread_id": "a", "status": "running"}, self.home)
        write_run_state({"thread_id": "b", "status": "completed"}, self.home)
        write_run_state({"thread_id": "c", "status": "cancelled"}, self.home)
        self.assertEqual({"a", "b", "c"}, {s["thread_id"] for s in list_run_states(self.home)})
        self.assertEqual({"a"}, {s["thread_id"] for s in list_run_states(self.home, include_terminal=False)})

    def test_thread_id_is_validated_against_path_traversal(self):
        for bad in ("", "../escape", "a/b", ".hidden", "with space", 5, None):
            with self.assertRaises(ValueError):
                read_run_state(bad, self.home)

    def test_write_is_atomic_leaving_no_temp_files(self):
        write_run_state({"thread_id": "t", "status": "running"}, self.home)
        write_run_state({"thread_id": "t", "status": "completed"}, self.home)
        files = sorted(p.name for p in run_state_dir(self.home).iterdir())
        self.assertEqual(["t.json"], files)
        self.assertEqual("completed", json.loads((run_state_dir(self.home) / "t.json").read_text(encoding="utf-8"))["status"])


if __name__ == "__main__":
    unittest.main()
