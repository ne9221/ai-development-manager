import json
import tempfile
import time
import unittest
from pathlib import Path

from manager.session_center import LiveSession, SessionCenterError, load_execution, read_codex_meta


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


if __name__ == "__main__":
    unittest.main()
