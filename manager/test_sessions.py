import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from manager.sessions import candidate_title, classify_project, discover_codex_sessions, import_codex_sessions, preview_codex_sessions
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

    def test_import_can_limit_to_requested_session(self):
        temp, root, _ = self.session_file()
        with temp:
            store = MemoryStore()
            records = import_codex_sessions(store, root, session_ids=["different-session"])
            self.assertEqual([], records)
            self.assertEqual({}, store.records)

    def test_existing_title_wins_over_prompt_candidate(self):
        temp, root, _ = self.session_file()
        with temp:
            record = discover_codex_sessions(root, titles={"session-123": "Existing title"})[0]
            self.assertEqual("Existing title", candidate_title(record))
            self.assertEqual("t" * 71 + "…", candidate_title({"title": "t" * 100, "first_user_prompt": "ignored"}))

    def test_prompt_candidate_is_short_and_omits_metadata_lines(self):
        prompt = "AI: Codex\nProject: ai-development-manager\nTask: test\nBuild a concise preview command for session records"
        self.assertEqual("Build a concise preview command for session records", candidate_title({"title": None, "first_user_prompt": prompt}))
        self.assertEqual("x" * 71 + "…", candidate_title({"title": None, "first_user_prompt": "x" * 100}))

    def test_preview_groups_classification_without_persisting(self):
        temp, root, path = self.session_file()
        with temp:
            before = path.read_bytes()
            lookup = lambda _cwd: {"git_root": None, "remote": None}
            preview = preview_codex_sessions(root, [PROJECT], repository_lookup=lookup)
            self.assertEqual(1, preview["total_sessions"])
            self.assertEqual(1, preview["classified_sessions"])
            self.assertEqual(0, preview["needs_review_sessions"])
            self.assertEqual([{"project_id": "ai-development-manager", "session_count": 1}], preview["projects"])
            self.assertNotIn("first_user_prompt", preview["sessions"][0])
            self.assertEqual(before, path.read_bytes())

    def test_preview_needs_review_filter_and_counts(self):
        temp, root, _ = self.session_file(fixture("C:/elsewhere"))
        with temp:
            preview = preview_codex_sessions(root, [PROJECT], needs_review=True, repository_lookup=lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual(0, preview["classified_sessions"])
            self.assertEqual(1, preview["needs_review_sessions"])
            self.assertEqual(1, len(preview["sessions"]))


if __name__ == "__main__": unittest.main()
