import json
import tempfile
import time
import unittest
from pathlib import Path

from manager.session_center import (
    LiveSession, SessionCenterError, file_status_source, load_execution, read_codex_meta, wait_for_execution,
)


class SessionCenterTest(unittest.TestCase):
    def session_file(self, directory: str) -> Path:
        path = Path(directory) / "session.jsonl"
        path.write_text(json.dumps({
            "timestamp": "2026-08-14T09:31:49.290Z", "type": "session_meta",
            "payload": {"id": "provider-1", "cwd": directory},
        }) + "\n", encoding="utf-8")
        return path

    def test_live_snapshot_tracks_file_growth_without_transcript_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.session_file(directory)
            meta = read_codex_meta(path, "provider-1")
            session = LiveSession("provider-1", path, meta["cwd"], meta["started_at"], "adm", "task-1", None, "branch", idle_seconds=.01)
            time.sleep(.02)
            self.assertEqual(session.snapshot()["current_state"], "waiting")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("activity\n")
            snapshot = session.snapshot()
            self.assertEqual(snapshot["current_state"], "running")
            self.assertEqual(snapshot["execution_id"], "UNLINKED")

    def test_execution_requires_exact_provider_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            record = {
                "provider": "codex", "project_id": "adm", "task_id": "task-1", "execution_id": "run-1",
                "provider_session_id": "provider-1", "task_snapshot": {"working_directory": directory, "branch": "main"},
            }
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(load_execution(path, "provider-1", directory)["execution_id"], "run-1")
            with self.assertRaisesRegex(SessionCenterError, "provider_session_id"):
                load_execution(path, "provider-2", directory)

    def test_wait_for_execution_requires_native_session_link(self):
        linked = {"execution_id": "run-1", "provider_session_id": "provider-1"}
        class Store:
            calls = 0
            def get(self, *_args):
                self.calls += 1
                return {} if self.calls == 1 else linked
        self.assertIs(wait_for_execution(Store(), "adm", "run-1", 1), linked)

    def linked_session(self, directory: str, status_source):
        path = self.session_file(directory)
        meta = read_codex_meta(path, "provider-1")
        return LiveSession(
            "provider-1", path, meta["cwd"], meta["started_at"], "adm", "task-1", "run-1", "branch",
            idle_seconds=.01, correlated=True, status_source=status_source,
        )

    def test_completed_execution_with_idle_transcript_shows_completed_not_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: "completed")
            time.sleep(.02)
            snapshot = session.snapshot()
            self.assertEqual("completed", snapshot["current_state"])
            self.assertNotEqual("waiting", snapshot["current_state"])

    def test_terminal_statuses_are_shown_verbatim_not_translated_to_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            for status in ("failed", "cancelled", "interrupted"):
                session = self.linked_session(directory, lambda status=status: status)
                time.sleep(.02)
                self.assertEqual(status, session.snapshot()["current_state"])

    def test_active_execution_with_recent_activity_shows_running(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: "running")
            self.assertEqual("running", session.snapshot()["current_state"])

    def test_active_execution_with_idle_transcript_shows_waiting_not_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: "running")
            time.sleep(.02)
            self.assertEqual("waiting", session.snapshot()["current_state"])

    def test_unlinked_session_never_fakes_a_terminal_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.session_file(directory)
            meta = read_codex_meta(path, "provider-1")
            session = LiveSession("provider-1", path, meta["cwd"], meta["started_at"], "adm", "task-1", None, "branch", idle_seconds=.01)
            time.sleep(.02)
            snapshot = session.snapshot()
            self.assertEqual("waiting", snapshot["current_state"])
            self.assertNotIn(snapshot["current_state"], ("completed", "failed", "cancelled", "interrupted"))
            self.assertFalse(snapshot["correlated"])

    def test_unknown_or_unavailable_authoritative_status_falls_back_to_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: None)
            time.sleep(.02)
            self.assertEqual("waiting", session.snapshot()["current_state"])
            with session.session_file.open("a", encoding="utf-8") as handle:
                handle.write("activity\n")
            self.assertEqual("running", session.snapshot()["current_state"])

    def test_drive_status_source_degrades_to_none_on_missing_or_broken_record(self):
        from manager.session_center import drive_status_source
        class MissingStore:
            def get(self, *_args):
                raise KeyError("no such record")
        self.assertIsNone(drive_status_source(MissingStore(), "adm", "run-1")())

        class ErrorStore:
            def get(self, *_args):
                raise RuntimeError("expected one Drive record for executions/adm/run-1; found 0")
        self.assertIsNone(drive_status_source(ErrorStore(), "adm", "run-1")())

        class OkStore:
            def get(self, *_args):
                return {"status": "completed"}
        self.assertEqual("completed", drive_status_source(OkStore(), "adm", "run-1")())

    def test_file_status_source_rereads_status_and_degrades_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            execution_path = Path(directory) / "execution.json"
            record = {
                "provider": "codex", "project_id": "adm", "task_id": "task-1", "execution_id": "run-1",
                "provider_session_id": "provider-1", "status": "running",
                "task_snapshot": {"working_directory": directory, "branch": "main"},
            }
            execution_path.write_text(json.dumps(record), encoding="utf-8")
            source = file_status_source(execution_path, "provider-1", directory)
            self.assertEqual("running", source())
            execution_path.write_text(json.dumps({**record, "status": "completed"}), encoding="utf-8")
            self.assertEqual("completed", source())
            execution_path.write_text("not json", encoding="utf-8")
            self.assertIsNone(source())


if __name__ == "__main__":
    unittest.main()
