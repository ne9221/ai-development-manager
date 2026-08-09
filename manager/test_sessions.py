import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from manager.sessions import classify_project, discover_codex_sessions, import_codex_sessions
from manager.tasks import validate


PROJECT = {"project_id": "ai-development-manager", "name": "AI Development Manager", "aliases": ["adm"], "repo": "https://github.com/ne9221/ai-development-manager", "default_branch": "main", "working_directory": "C:/work/ai-development-manager", "runtime_ssot": "Google Drive", "project_rules": [], "active_tasks": [], "current_phase": "1", "important_constraints": []}


class MemoryStore:
    def __init__(self): self.records = {}
    def list_projects(self): return [PROJECT]
    def put(self, area, project_id, name, document):
        self.records[(area, project_id, name)] = deepcopy(document)
        return document


def fixture(cwd="C:/work/ai-development-manager"):
    lines = [
        {"timestamp": "2026-08-10T01:00:00Z", "type": "session_meta", "payload": {"session_id": "session-123", "timestamp": "2026-08-10T01:00:00Z", "cwd": cwd}},
        {"timestamp": "2026-08-10T01:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {"timestamp": "2026-08-10T01:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Implement the registry"}]}},
        {"timestamp": "2026-08-10T01:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Working"}]}},
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


class SessionTests(unittest.TestCase):
    def session_file(self, content=None):
        temp = tempfile.TemporaryDirectory(); root = Path(temp.name) / "sessions" / "2026" / "08" / "10"; root.mkdir(parents=True)
        path = root / "rollout-2026-08-10-session-123.jsonl"; path.write_text(content or fixture(), encoding="utf-8")
        return temp, Path(temp.name) / "sessions", path

    def test_parse_valid_fixture_and_preserves_source(self):
        temp, root, path = self.session_file()
        with temp:
            before = path.read_bytes()
            record = discover_codex_sessions(root)[0]
            self.assertEqual("codex", record["provider"])
            self.assertEqual("session-123", record["session_id"])
            self.assertEqual(2, record["message_count"])
            self.assertEqual("gpt-5-codex", record["model"])
            self.assertEqual("Implement the registry", record["first_user_prompt"])
            self.assertEqual(before, path.read_bytes())

    def test_missing_cwd_is_retained_as_null(self):
        temp, root, _ = self.session_file(fixture(cwd=None).replace(', "cwd": null', ''))
        with temp:
            record = discover_codex_sessions(root)[0]
            self.assertIsNone(record["working_directory"])

    def test_cwd_classifies_without_git_repository(self):
        temp, root, _ = self.session_file()
        with temp:
            record = discover_codex_sessions(root)[0]
            classified = classify_project(record, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("ai-development-manager", classified["project_id"])
            self.assertEqual("working_directory", classified["classification_method"])

    def test_unclassified_needs_review_when_signals_are_missing(self):
        temp, root, _ = self.session_file(fixture("C:/elsewhere"))
        with temp:
            record = discover_codex_sessions(root)[0]
            classified = classify_project(record, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            self.assertIsNone(classified["project_id"])
            self.assertEqual("needs_review", classified["classification_status"])

    def test_import_is_idempotent_and_schema_valid(self):
        temp, root, _ = self.session_file()
        with temp:
            store = MemoryStore()
            lookup = lambda _cwd: {"git_root": "C:/work/ai-development-manager", "remote": "https://github.com/ne9221/ai-development-manager.git"}
            first = import_codex_sessions(store, root, repository_lookup=lookup)
            second = import_codex_sessions(store, root, repository_lookup=lookup)
            self.assertEqual(1, len(first)); self.assertEqual(1, len(second)); self.assertEqual(1, len(store.records))
            record = next(iter(store.records.values()))
            validate("session", record)
            self.assertEqual("codex", record["provider"])
            self.assertEqual("git_repository", record["classification_method"])


if __name__ == "__main__": unittest.main()
