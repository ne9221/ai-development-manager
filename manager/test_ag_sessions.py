"""Tests for Antigravity session discovery and normalization."""

import json
import tempfile
import unittest
from pathlib import Path

from manager.sessions import (
    AntigravitySessionAdapter,
    discover_antigravity_sessions,
    session_adapter,
)


class TestAgSessions(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_session_adapter_registration(self):
        adapter_ag = session_adapter("antigravity")
        self.assertIsInstance(adapter_ag, AntigravitySessionAdapter)
        adapter_gemini = session_adapter("gemini")
        self.assertIsInstance(adapter_gemini, AntigravitySessionAdapter)

    def test_discover_and_normalize_brain_transcript(self):
        conv_id = "a872c41c-cace-45ce-a0df-70a7f7306ca1"
        log_dir = self.root / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript.jsonl"

        lines = [
            {"step_index": 0, "timestamp": "2026-08-17T08:00:00Z", "type": "USER_INPUT", "content": "Project: test-proj\nTask: task-1\nAI: Antigravity\nAnalyze repo"},
            {"step_index": 1, "timestamp": "2026-08-17T08:00:05Z", "type": "PLANNER_RESPONSE", "content": "Checking files...", "model": "gemini-3.7-flash"},
            {"step_index": 2, "timestamp": "2026-08-17T08:00:10Z", "type": "PLANNER_RESPONSE", "content": "Done with analysis."},
        ]
        with transcript_file.open("w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")

        sessions = discover_antigravity_sessions(sessions_root=self.root)
        self.assertEqual(len(sessions), 1)

        sess = sessions[0]
        self.assertEqual(sess["provider"], "antigravity")
        self.assertEqual(sess["provider_session_id"], conv_id)
        self.assertEqual(sess["session_id"], f"antigravity:{conv_id}")
        self.assertEqual(sess["message_count"], 3)
        self.assertEqual(sess["started_at"], "2026-08-17T08:00:00Z")
        self.assertEqual(sess["updated_at"], "2026-08-17T08:00:10Z")
        self.assertEqual(sess["model"], "gemini-3.7-flash")
        self.assertIn("Analyze repo", sess["first_user_prompt"])
        self.assertIsNotNone(sess["_identity_header"])
        self.assertEqual(sess["_identity_header"]["project"], "test-proj")


if __name__ == "__main__":
    unittest.main()
