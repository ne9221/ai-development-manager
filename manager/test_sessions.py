import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from manager.sessions import _repository_identity, candidate_title, classify_project, discover_codex_sessions, extract_repository_urls, import_codex_sessions, load_preview_projects, parse_identity_header, preview_codex_sessions, project_preview_snapshot
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

    def test_project_snapshot_contract_allows_missing_optional_signals(self):
        no_repo = deepcopy(PROJECT); no_repo.pop("repo")
        no_cwd = deepcopy(PROJECT); no_cwd.pop("working_directory")
        snapshot = project_preview_snapshot([no_repo, no_cwd])
        self.assertEqual("project_preview", snapshot["snapshot_type"])
        self.assertIsNone(snapshot["projects"][0]["repo"])
        self.assertIsNone(snapshot["projects"][1]["working_directory"])

    def test_malformed_project_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "projects.json"; path.write_text('{"projects": []}', encoding="utf-8")
            with self.assertRaises(Exception): load_preview_projects(path)

    def test_alias_and_ambiguous_project_signals_are_conservative(self):
        record = {"working_directory": "C:/work/adm"}
        alias_project = deepcopy(PROJECT); alias_project["working_directory"] = None
        classified = classify_project(record, [alias_project], lambda _cwd: {"git_root": None, "remote": None})
        self.assertEqual("project_alias", classified["classification_method"])
        duplicate = deepcopy(alias_project); duplicate["project_id"] = "other-project"; duplicate["aliases"] = ["adm"]
        ambiguous = classify_project(record, [alias_project, duplicate], lambda _cwd: {"git_root": None, "remote": None})
        self.assertIsNone(ambiguous.get("project_id"))

    @patch("manager.sessions.Path.is_dir", return_value=True)
    @patch("manager.sessions.subprocess.run")
    def test_repository_probe_tolerates_missing_decoded_stdout(self, run, _is_dir):
        run.side_effect = [subprocess.CompletedProcess([], 0, stdout=None), subprocess.CompletedProcess([], 0, stdout=None)]
        self.assertEqual({"git_root": None, "remote": None}, _repository_identity("C:/work/project"))

    def test_strict_identity_header_matches_id_name_and_alias_only(self):
        header = parse_identity_header("AI: Codex\nProject: ai-development-manager\nTask: phase-2c\nConversation: Preview\nRun/Session: run-1")
        self.assertEqual("ai-development-manager", header["project"])
        self.assertEqual("phase-2c", header["task"])
        self.assertEqual("Preview", header["conversation"])
        self.assertIsNone(parse_identity_header("Please look at the ai-development-manager project"))
        self.assertIsNone(parse_identity_header("Project: ai-development-manager\nTask: phase-2c"))
        for reference in ("AI Development Manager", "adm"):
            record = classify_project({"working_directory": None, "_identity_header": {"ai": "Codex", "project": reference, "task": "t"}}, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("ai-development-manager", record["project_id"])
            self.assertEqual("explicit_project_header", record["classification_method"])

    def test_identity_header_agrees_or_conflicts_with_repo_and_cwd(self):
        header = {"ai": "Codex", "project": "ai-development-manager", "task": "phase-2c"}
        same = classify_project({"working_directory": "C:/work/a", "_identity_header": header}, [PROJECT], lambda _cwd: {"git_root": "C:/work/a", "remote": "https://github.com/ne9221/ai-development-manager.git"})
        self.assertEqual("ai-development-manager", same["project_id"])
        self.assertEqual("explicit_project_header", same["classification_method"])
        other = deepcopy(PROJECT); other.update(project_id="other", name="Other", aliases=[], repo="https://github.com/example/other", working_directory="C:/work/other")
        repo_conflict = classify_project({"working_directory": "C:/work/other", "_identity_header": header}, [PROJECT, other], lambda _cwd: {"git_root": "C:/work/other", "remote": "https://github.com/example/other.git"})
        self.assertEqual("conflicting_deterministic_signals", repo_conflict["classification_method"])
        cwd_conflict = classify_project({"working_directory": "C:/work/other", "_identity_header": header}, [PROJECT, other], lambda _cwd: {"git_root": None, "remote": None})
        self.assertEqual("needs_review", cwd_conflict["classification_status"])

    def test_identity_header_maps_task_and_conversation(self):
        record = classify_project({"working_directory": None, "conversation_label": None, "_identity_header": {"ai": "Codex", "project": "adm", "task": "phase-2c", "conversation": "Preview"}}, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
        self.assertEqual("phase-2c", record["task_id"])
        self.assertEqual("Preview", record["conversation_label"])

    def test_exact_session_repository_url_maps_and_normalizes(self):
        record = classify_project({"working_directory": None, "_repository_urls": ["git@github.com:ne9221/ai-development-manager.git"]}, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
        self.assertEqual("ai-development-manager", record["project_id"])
        self.assertEqual("session_repository_url", record["classification_method"])
        self.assertEqual([], extract_repository_urls("Please look at the ai-development-manager project"))
        unresolved = classify_project({"working_directory": None, "classification_status": "needs_review", "_repository_urls": ["https://github.com/example/unrelated"]}, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
        self.assertEqual("needs_review", unresolved["classification_status"])

    def test_multi_message_repository_url_is_read_only_signal(self):
        extra = {"timestamp": "2026-08-10T01:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Repository: https://github.com/ne9221/ai-development-manager"}]}}
        temp, root, path = self.session_file(fixture("C:/elsewhere") + json.dumps(extra) + "\n")
        with temp:
            before = path.read_bytes()
            parsed = discover_codex_sessions(root)[0]
            self.assertEqual(["https://github.com/ne9221/ai-development-manager"], parsed["_repository_urls"])
            classified = classify_project(parsed, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("session_repository_url", classified["classification_method"])
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__": unittest.main()
