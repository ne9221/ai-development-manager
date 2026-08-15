import os
import tempfile
import sqlite3
import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from manager.telemetry_adapter import (
    collect_local_telemetry,
    collect_codex_telemetry,
    collect_claude_telemetry_for_root,
    collect_antigravity_telemetry,
    format_timestamp,
    map_cwd_to_project,
    sanitize_record
)

class TestTelemetryAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_format_timestamp(self):
        # 1. Float epoch (seconds)
        self.assertEqual(format_timestamp(1771112400), "2026-02-14T23:40:00Z")
        # 2. Float epoch (milliseconds)
        self.assertEqual(format_timestamp(1771112400000), "2026-02-14T23:40:00Z")
        # 3. String digit millisecond
        self.assertEqual(format_timestamp("1771112400000"), "2026-02-14T23:40:00Z")
        # 4. Standard ISO string
        self.assertEqual(format_timestamp("2026-08-15T11:00:00Z"), "2026-08-15T11:00:00Z")
        # 5. Invalid format returns None or self
        self.assertIsNone(format_timestamp(None))

    def test_map_cwd_to_project(self):
        projects = [
            {"project_id": "proj-1", "working_directory": str(self.temp_path / "work-1")},
            {"project_id": "proj-2", "working_directory": str(self.temp_path / "work-2")}
        ]
        # Identical
        self.assertEqual(map_cwd_to_project(str(self.temp_path / "work-1"), projects), "proj-1")
        # Subfolder
        self.assertEqual(map_cwd_to_project(str(self.temp_path / "work-2" / "subdir"), projects), "proj-2")
        # Non-matching
        self.assertIsNone(map_cwd_to_project(str(self.temp_path / "other"), projects))

    def test_no_credentials_leak(self):
        raw_record = {
            "session_id": "sess-1",
            "model": "claude-3-5-sonnet",
            "activity": "Sending turn with API_KEY=sk-1234567abcde",
            "source": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "project": "Bearer token-value"
        }
        sanitized = sanitize_record(raw_record)
        
        # Verify JWT-only is redacted
        self.assertEqual(sanitized["source"], "[REDACTED JWT]")
        
        # Verify api_key is redacted/censored
        self.assertEqual(sanitized["activity"], "[REDACTED CREDENTIAL]")
        
        # Verify Bearer keyword triggers complete credential redaction
        self.assertEqual(sanitized["project"], "[REDACTED CREDENTIAL]")

    def test_codex_db_missing(self):
        # Empty folder
        results = collect_codex_telemetry(codex_home=str(self.temp_path))
        self.assertEqual(results, [])

    def test_codex_read_only_and_dynamic_schema(self):
        # Set up mock Codex state_5.sqlite database
        db_path = self.temp_path / "state_5.sqlite"
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Schema with reasoning_effort and tokens_used missing (legacy schema scenario)
        cursor.execute("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                created_at INTEGER,
                updated_at INTEGER,
                model TEXT,
                cwd TEXT,
                title TEXT,
                source TEXT
            )
        """)
        cursor.execute("""
            INSERT INTO threads (id, created_at, updated_at, model, cwd, title, source)
            VALUES ('thread-123', 1771112400000, 1771112500000, 'o1-preview', 'C:\\Users\\EE\\project', 'Implement Telemetry', 'copilot')
        """)
        conn.commit()
        conn.close()
        
        # Call collector with legacy schema (missing reasoning_effort & tokens_used)
        # Verify it runs successfully without crashing, and fallback values are populated
        results = collect_codex_telemetry(codex_home=str(self.temp_path))
        
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["session_id"], "thread-123")
        self.assertEqual(r["model"], "o1-preview")
        self.assertEqual(r["started_at"], "2026-02-14T23:40:00Z")
        self.assertIsNone(r["tokens"])  # fallback to None, not 0
        self.assertIsNone(r["account_id"])  # must not hardcode or decode secrets
        self.assertIsNone(r["reasoning_effort"])  # fallback
        
        # Assert database connection check: URI contains mode=ro (strictly read-only)
        with patch("sqlite3.connect", wraps=sqlite3.connect) as mock_connect:
            collect_codex_telemetry(codex_home=str(self.temp_path))
            called_args, called_kwargs = mock_connect.call_args
            self.assertIn("mode=ro", called_args[0])
            self.assertTrue(called_kwargs.get("uri"))

    def test_codex_db_locked(self):
        # Setup mock DB
        db_path = self.temp_path / "state_5.sqlite"
        db_path.write_text("dummy database")
        
        # Mock sqlite3.connect to raise OperationalError (database locked / unreadable)
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                collect_codex_telemetry(codex_home=str(self.temp_path))

    def test_codex_no_transcript_text_leak(self):
        # Codex `threads.title` is user/model-authored free text (real captures show
        # 55/93 titles >200 chars, some ~74KB, and prompt/transcript-shaped content).
        # activity must never echo it, truncated or not - only a fixed structured label.
        db_path = self.temp_path / "state_5.sqlite"
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                created_at INTEGER,
                updated_at INTEGER,
                model TEXT,
                tokens_used INTEGER,
                cwd TEXT,
                title TEXT,
                source TEXT
            )
        """)

        long_title = "A" * 5000  # 1. very long title
        prompt_title = "Please write a function that connects to the database using password hunter2 and return all user emails"  # 2. prompt-shaped
        multiline_title = "Fix bug in login flow\nStack trace:\n  at foo()\n  at bar()\nSee attached transcript"  # 3. multiline
        unicode_title = "修復登入流程 🔒 emoji test 日本語タイトル"  # 4. unicode
        code_title = 'def handler():\n    return "SELECT * FROM users WHERE token=\'abc123\'"'  # 5. code/quotes

        rows = [
            ("thread-long", long_title),
            ("thread-prompt", prompt_title),
            ("thread-multiline", multiline_title),
            ("thread-unicode", unicode_title),
            ("thread-code", code_title),
            ("thread-notitle", None),  # 6. missing/null title
        ]
        for i, (thread_id, title) in enumerate(rows):
            cursor.execute(
                "INSERT INTO threads (id, created_at, updated_at, model, tokens_used, cwd, title, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (thread_id, 1771112400000 + i, 1771112500000 + i, "gpt-5-codex", 1234, "/proj", title, "cli"),
            )
        conn.commit()
        conn.close()

        results = collect_codex_telemetry(codex_home=str(self.temp_path))
        self.assertEqual(len(results), len(rows))

        by_id = {r["session_id"]: r for r in results}

        suspicious_fragments = [
            "AAAA", "hunter2", "connects to the database", "Stack trace", "at foo()",
            "at bar()", "修復登入流程", "SELECT * FROM users", "abc123", "token=",
        ]
        for r in results:
            activity = r["activity"]
            # 8. activity contains no raw title substring
            for fragment in suspicious_fragments:
                self.assertNotIn(fragment, activity)
            self.assertNotIn("\n", activity)  # no multiline leakage
            # 9. activity bounded length - fixed short label, not proportional to title size
            self.assertLessEqual(len(activity), 64)
            # activity must be the fixed structured label, never derived from title content
            self.assertEqual(activity, "Codex thread")

        # 7. normal, non-content telemetry metadata is still preserved
        r = by_id["thread-long"]
        self.assertEqual(r["model"], "gpt-5-codex")
        self.assertEqual(r["tokens"], 1234)
        self.assertEqual(r["session_id"], "thread-long")
        self.assertIn("confidence", r)
        self.assertEqual(r["confidence"], "confirmed")

        # missing title must not crash and must still produce the fixed label
        r_no_title = by_id["thread-notitle"]
        self.assertEqual(r_no_title["activity"], "Codex thread")

    def test_claude_ab_roots_isolation(self):
        # Create default root ~/.claude (account-a) and ~/.claude-b (account-b)
        root_a = self.temp_path / "claude-a"
        root_b = self.temp_path / "claude-b"
        
        # Setup files for a
        proj_dir_a = root_a / "projects" / "project-a-encoded"
        proj_dir_a.mkdir(parents=True)
        jsonl_a = proj_dir_a / "sess-a1.jsonl"
        jsonl_a.write_text(json.dumps({
            "timestamp": "2026-08-15T11:00:00Z",
            "sessionId": "sess-a1",
            "cwd": "/path/to/project-a",
            "type": "assistant",
            "model": "claude-3-5",
            "message": {"role": "assistant", "usage": {"input_tokens": 10, "output_tokens": 5}}
        }) + "\n")
        
        # Setup files for b (no model, no tokens reported)
        proj_dir_b = root_b / "projects" / "project-b-encoded"
        proj_dir_b.mkdir(parents=True)
        jsonl_b = proj_dir_b / "sess-b1.jsonl"
        jsonl_b.write_text(json.dumps({
            "timestamp": "2026-08-15T12:00:00Z",
            "sessionId": "sess-b1",
            "cwd": "/path/to/project-b",
            "type": "user",
            "message": "User prompt text content"
        }) + "\n")
        
        # Scan with collect_local_telemetry
        claude_roots = {
            "account-a": str(root_a),
            "account-b": str(root_b)
        }
        
        results = collect_local_telemetry(codex_home=str(self.temp_path / "missing-codex"), claude_roots=claude_roots)
        
        # We expect: 1 session for a, 1 for b, and 1 static for antigravity
        claude_records = [r for r in results if r["provider"] == "claude"]
        self.assertEqual(len(claude_records), 2)
        
        rec_a = next(r for r in claude_records if r["account_id"] == "account-a")
        rec_b = next(r for r in claude_records if r["account_id"] == "account-b")
        
        self.assertEqual(rec_a["session_id"], "sess-a1")
        self.assertEqual(rec_a["project"], "_unclassified")
        self.assertEqual(rec_a["tokens"], 15)
        self.assertEqual(rec_a["model"], "claude-3-5")
        self.assertEqual(rec_a["confidence"], "derived")
        
        self.assertEqual(rec_b["session_id"], "sess-b1")
        self.assertEqual(rec_b["model"], "unknown")
        self.assertEqual(rec_b["confidence"], "unknown") # no model -> confidence unknown
        self.assertIsNone(rec_b["tokens"]) # no token usage reported -> None, not 0

    def test_claude_malformed_jsonl_line_isolation(self):
        root_path = self.temp_path / "claude"
        proj_dir = root_path / "projects" / "proj-encoded"
        proj_dir.mkdir(parents=True)
        jsonl_file = proj_dir / "sess-123.jsonl"
        
        # Write lines: line 1 ok, line 2 malformed, line 3 ok with token updates
        jsonl_file.write_text("\n".join([
            json.dumps({"timestamp": "2026-08-15T10:00:00Z", "sessionId": "sess-123", "cwd": "/project"}),
            "THIS_IS_NOT_VALID_JSON",
            json.dumps({"timestamp": "2026-08-15T10:15:00Z", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}})
        ]) + "\n")
        
        records = collect_claude_telemetry_for_root(str(root_path), "account-a")
        
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["session_id"], "sess-123")
        self.assertEqual(r["started_at"], "2026-08-15T10:00:00Z")
        self.assertEqual(r["updated_at"], "2026-08-15T10:15:00Z")
        self.assertEqual(r["tokens"], 150) # isolated turn read successfully!

    def test_confidence_semantics_and_antigravity(self):
        # 1. Antigravity static response
        ag_results = collect_antigravity_telemetry()
        self.assertEqual(len(ag_results), 1)
        self.assertEqual(ag_results[0]["provider"], "antigravity")
        self.assertEqual(ag_results[0]["confidence"], "unavailable")
        self.assertEqual(ag_results[0]["model"], "unknown")
        self.assertIsNone(ag_results[0]["tokens"]) # Antigravity tokens must be None
        
        # 2. Confidence mappings overall check
        # Codex = confirmed, Claude = derived (if model present), Antigravity = unavailable
        root_a = self.temp_path / "claude-a"
        proj_dir_a = root_a / "projects" / "project-a-encoded"
        proj_dir_a.mkdir(parents=True)
        (proj_dir_a / "sess.jsonl").write_text(json.dumps({
            "timestamp": "2026-08-15T11:00:00Z",
            "sessionId": "sess-a",
            "model": "claude-3-sonnet"
        }) + "\n")
        
        db_path = self.temp_path / "state_5.sqlite"
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, created_at INTEGER, updated_at INTEGER, model TEXT)")
        cursor.execute("INSERT INTO threads (id, created_at, updated_at, model) VALUES ('sess-x', 1771112400000, 1771112400000, 'gpt-4')")
        conn.commit()
        conn.close()
        
        claude_roots = {"account-a": str(root_a)}
        results = collect_local_telemetry(codex_home=str(self.temp_path), claude_roots=claude_roots)
        
        codex_recs = [r for r in results if r["provider"] == "codex"]
        claude_recs = [r for r in results if r["provider"] == "claude"]
        ag_recs = [r for r in results if r["provider"] == "antigravity"]
        
        self.assertEqual(codex_recs[0]["confidence"], "confirmed")
        self.assertEqual(claude_recs[0]["confidence"], "derived")
        self.assertEqual(ag_recs[0]["confidence"], "unavailable")
        
        # Verify that all confidence values in the result are within the vocabulary:
        # confirmed, derived, unknown, unavailable
        vocab = {"confirmed", "derived", "unknown", "unavailable"}
        for r in results:
            self.assertIn(r["confidence"], vocab)
            self.assertNotEqual(r["confidence"], "official") # confidence vocabulary has no official

    def test_claude_no_transcript_text_leak(self):
        # Even if JSONL message contains secrets or text body, activity must only come from structured event/tool
        root_path = self.temp_path / "claude"
        proj_dir = root_path / "projects" / "proj-encoded"
        proj_dir.mkdir(parents=True)
        jsonl_file = proj_dir / "sess-123.jsonl"
        
        jsonl_file.write_text(json.dumps({
            "timestamp": "2026-08-15T10:00:00Z",
            "sessionId": "sess-123",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "This is secret output snippet sk-abcdef12345"}]
            }
        }) + "\n")
        
        records = collect_claude_telemetry_for_root(str(root_path), "account-a")
        self.assertEqual(len(records), 1)
        r = records[0]
        
        # Activity must not contain transcript text
        self.assertNotIn("secret", r["activity"])
        self.assertNotIn("sk-abcdef", r["activity"])
        self.assertEqual(r["activity"], "Event: assistant")

if __name__ == "__main__":
    unittest.main()
