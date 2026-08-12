import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from manager.context_pack import recent_sessions
from manager.session_identity import session_provider_identity
from manager.sessions import CanonicalSession, ClaudeSessionAdapter, CodexSessionAdapter, REVIEW_PROJECT_ID, _repository_identity, assign_review, candidate_title, classify_project, discover_claude_sessions, discover_codex_sessions, extract_repository_urls, import_claude_sessions, import_codex_sessions, import_sessions, load_preview_projects, manager_session_key, parse_identity_header, parse_manager_session_key, preview_claude_sessions, preview_codex_sessions, project_preview_snapshot, review_queue, search_sessions
from manager.tasks import TaskError, validate


PROJECT = {"project_id": "ai-development-manager", "name": "AI Development Manager", "aliases": ["adm"], "repo": "https://github.com/ne9221/ai-development-manager", "default_branch": "main", "working_directory": "C:/work/ai-development-manager", "runtime_ssot": "Google Drive", "project_rules": [], "active_tasks": [], "current_phase": "1", "important_constraints": []}


class MemoryStore:
    def __init__(self): self.records = {}; self.deleted = []; self.versions = {}
    def list_projects(self): return [PROJECT]
    def put(self, area, project_id, name, document):
        key = (area, project_id, name)
        self.records[key] = deepcopy(document)
        self.versions[key] = self.versions.get(key, 0) + 1
        return document
    def get(self, area, project_id, name): return deepcopy(self.records[(area, project_id, name)])
    def get_with_token(self, area, project_id, name):
        key = (area, project_id, name)
        return deepcopy(self.records[key]), {"version": self.versions.get(key, 0), "document": deepcopy(self.records[key])}
    def list_records(self, area, project_id): return [deepcopy(value) for (record_area, record_project, _), value in self.records.items() if record_area == area and record_project == project_id]
    def delete(self, area, project_id, name, expected=None):
        key = (area, project_id, name)
        if expected is not None and (self.versions.get(key, 0) != expected["version"] or self.records.get(key) != expected["document"]):
            return False
        self.deleted.append(key)
        del self.records[key]
        self.versions.pop(key, None)
        return True


class StaticAdapter:
    def __init__(self, records): self.records = records
    def discover(self, _root): return deepcopy(self.records)


def fixture(cwd="C:/work/ai-development-manager"):
    lines = [
        {"timestamp": "2026-08-10T01:00:00Z", "type": "session_meta", "payload": {"session_id": "session-123", "timestamp": "2026-08-10T01:00:00Z", "cwd": cwd}},
        {"timestamp": "2026-08-10T01:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {"timestamp": "2026-08-10T01:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Implement the registry"}]}},
        {"timestamp": "2026-08-10T01:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Working"}]}},
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def claude_fixture(cwd="C:/work/ai-development-manager", prompt="Implement the registry"):
    lines = [
        {"type": "user", "uuid": "u-1", "parentUuid": None, "sessionId": "claude-123", "timestamp": "2026-08-10T01:00:00Z", "cwd": cwd, "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "uuid": "a-1", "parentUuid": "u-1", "sessionId": "claude-123", "timestamp": "2026-08-10T01:00:01Z", "cwd": cwd, "message": {"role": "assistant", "model": "claude-sonnet-4", "content": [{"type": "text", "text": "Working"}]}},
        {"type": "custom-title", "sessionId": "claude-123", "customTitle": "Claude registry work"},
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def search_record(session_id="search-1", **changes):
    record = {"session_id": session_id, "provider": "codex", "project_id": "ai-development-manager", "task_id": "codex-session-organizer", "conversation_label": "Search", "title": "Quota session", "first_user_prompt": "Find quota data for 中文專案", "working_directory": "C:/work/ai-development-manager", "repository": "https://github.com/ne9221/ai-development-manager", "classification_status": "needs_review", "classification_method": "unclassified", "started_at": "2026-08-01T00:00:00Z", "updated_at": "2026-08-10T00:00:00Z", "source_identifier": "sessions/search-1.jsonl"}
    record.update(changes)
    return record


class SessionTests(unittest.TestCase):
    def session_file(self, content=None):
        temp = tempfile.TemporaryDirectory(); root = Path(temp.name) / "sessions" / "2026" / "08" / "10"; root.mkdir(parents=True)
        path = root / "rollout-2026-08-10-session-123.jsonl"; path.write_text(content or fixture(), encoding="utf-8")
        return temp, Path(temp.name) / "sessions", path

    def claude_session_file(self, content=None):
        temp = tempfile.TemporaryDirectory(); root = Path(temp.name) / "projects" / "C--work--ai-development-manager"; root.mkdir(parents=True)
        path = root / "claude-123.jsonl"; path.write_text(content or claude_fixture(), encoding="utf-8")
        return temp, Path(temp.name) / "projects", path

    def test_parse_valid_fixture_and_preserves_source(self):
        temp, root, path = self.session_file()
        with temp:
            before = path.read_bytes()
            record = discover_codex_sessions(root)[0]
            self.assertEqual("codex", record["provider"])
            self.assertEqual("codex:session-123", record["session_id"])
            self.assertEqual(2, record["message_count"])
            self.assertEqual("gpt-5-codex", record["model"])
            self.assertEqual("Implement the registry", record["first_user_prompt"])
            self.assertEqual(before, path.read_bytes())

    def test_codex_adapter_normalizes_raw_session_to_canonical_identity(self):
        temp, root, path = self.session_file()
        with temp:
            before = path.read_bytes()
            adapter = CodexSessionAdapter()
            raw = next(adapter.discover_raw_sessions(root))
            canonical = adapter.normalize(adapter.parse_raw_session(raw))
            self.assertIsInstance(canonical, CanonicalSession)
            self.assertEqual("codex", canonical.provider)
            self.assertEqual("session-123", canonical.provider_session_id)
            self.assertEqual(("codex", "session-123"), canonical.provider_identity)
            self.assertEqual("codex:session-123", canonical.session_id)
            self.assertEqual("2026/08/10/rollout-2026-08-10-session-123.jsonl", canonical.source_identifier)
            self.assertTrue(canonical.content_hash)
            self.assertEqual(before, path.read_bytes())

    def test_malformed_active_jsonl_tail_is_ignored_by_adapter(self):
        temp, root, path = self.session_file(fixture() + '{"timestamp":"incomplete"')
        with temp:
            before = path.read_bytes()
            records = CodexSessionAdapter().discover(root)
            self.assertEqual(1, len(records))
            self.assertEqual("session-123", records[0]["provider_session_id"])
            self.assertEqual(before, path.read_bytes())

    def test_claude_adapter_normalizes_verified_project_jsonl_read_only(self):
        temp, root, path = self.claude_session_file()
        with temp:
            before = path.read_bytes()
            record = discover_claude_sessions(root)[0]
            self.assertEqual("claude", record["provider"])
            self.assertEqual("claude-123", record["provider_session_id"])
            self.assertEqual("claude:claude-123", record["session_id"])
            self.assertNotEqual(manager_session_key("codex", "claude-123"), record["session_id"])
            self.assertEqual("C--work--ai-development-manager/claude-123.jsonl", record["source_identifier"])
            self.assertEqual("claude-sonnet-4", record["model"])
            self.assertEqual("Claude registry work", record["title"])
            self.assertEqual("working_directory", classify_project(record, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})["classification_method"])
            self.assertEqual(before, path.read_bytes())

    def test_claude_malformed_tail_identity_header_and_common_classification(self):
        prompt = "AI: Claude\nProject: adm\nTask: phase-2b\nConversation: Import\nImplement the Claude adapter"
        temp, root, path = self.claude_session_file(claude_fixture("C:/elsewhere", prompt) + '{"type":"user"')
        with temp:
            before = path.read_bytes()
            record = discover_claude_sessions(root)[0]
            classified = classify_project(record, [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("ai-development-manager", classified["project_id"])
            self.assertEqual("explicit_project_header", classified["classification_method"])
            self.assertEqual("phase-2b", classified["task_id"])
            self.assertEqual(before, path.read_bytes())

    def test_claude_cwd_classification_preview_review_and_import(self):
        temp, root, _ = self.claude_session_file(claude_fixture("C:/elsewhere"))
        with temp:
            unresolved = classify_project(discover_claude_sessions(root)[0], [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("needs_review", unresolved["classification_status"])
            self.assertEqual("claude", review_queue([unresolved])[0]["provider"])
            assigned = assign_review(MemoryStore(), unresolved, "ai-development-manager", [PROJECT], "2026-08-10T02:00:00Z")
            self.assertEqual([], review_queue([unresolved], [assigned]))
            preview = preview_claude_sessions(root, [PROJECT], repository_lookup=lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual(1, preview["needs_review_sessions"])
            store = MemoryStore()
            imported = import_claude_sessions(store, root, repository_lookup=lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("claude:claude-123", imported[0]["session_id"])
            validate("session", imported[0])

    def test_manager_key_is_provider_aware_reversible_and_stable(self):
        self.assertEqual("codex:abc123", manager_session_key("codex", "abc123"))
        self.assertEqual(manager_session_key("codex", "abc123"), manager_session_key("codex", "abc123"))
        self.assertNotEqual(manager_session_key("codex", "abc123"), manager_session_key("claude", "abc123"))
        self.assertEqual(("codex", "a/b:c"), parse_manager_session_key(manager_session_key("codex", "a/b:c")))
        values = ["abc:def", "abc%3Adef", "Unicode-é", "中文", "a/b", "a\\b", " spaced value "]
        keys = [manager_session_key("claude", value) for value in values]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(values, [parse_manager_session_key(key)[1] for key in keys])

    def test_legacy_identity_fallback_normalizes_canonical_session_id_once(self):
        self.assertEqual(("codex", "abc123"), session_provider_identity({"provider": "codex", "session_id": "abc123", "provider_session_id": None}))
        self.assertEqual(("claude", "abc123"), session_provider_identity({"provider": "claude", "session_id": "claude:abc123", "provider_session_id": None}))
        self.assertEqual(("claude", "codex:abc123"), session_provider_identity({"provider": "claude", "session_id": "codex:abc123", "provider_session_id": None}))
        self.assertEqual(("claude", "claude:%"), session_provider_identity({"provider": "claude", "session_id": "claude:%", "provider_session_id": None}))

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
            self.assertEqual("session-123", record["provider_session_id"])
            self.assertIn(("sessions", "ai-development-manager", "codex:session-123"), store.records)
            self.assertEqual("git_repository", record["classification_method"])

    def test_import_can_limit_to_requested_session(self):
        temp, root, _ = self.session_file()
        with temp:
            store = MemoryStore()
            records = import_codex_sessions(store, root, session_ids=["different-session"])
            self.assertEqual([], records)
            self.assertEqual({}, store.records)

    def test_reclassification_writes_new_location_and_retains_stale_copies(self):
        temp, root, _ = self.session_file(fixture("C:/elsewhere"))
        project_a = deepcopy(PROJECT)
        project_b = deepcopy(PROJECT); project_b.update(project_id="other-project", name="Other", aliases=[], repo="https://github.com/example/other", working_directory="C:/other")
        with temp:
            store = MemoryStore()
            no_match = lambda _cwd: {"git_root": None, "remote": None}
            origin = lambda _cwd: {"git_root": "C:/work/a", "remote": PROJECT["repo"]}
            other = lambda _cwd: {"git_root": "C:/work/b", "remote": project_b["repo"]}
            import_codex_sessions(store, root, [project_a, project_b], no_match)
            key = "codex:session-123"
            self.assertIn(("sessions", "_unclassified", key), store.records)

            import_codex_sessions(store, root, [project_a, project_b], origin)
            self.assertIn(("sessions", "_unclassified", key), store.records)
            self.assertIn(("sessions", project_a["project_id"], key), store.records)

            claude_key = "claude:session-123"
            claude = deepcopy(store.records[("sessions", project_a["project_id"], key)])
            claude.update(session_id=claude_key, provider="claude", provider_session_id="session-123")
            store.records[("sessions", project_a["project_id"], claude_key)] = claude
            import_codex_sessions(store, root, [project_a, project_b], other)
            self.assertIn(("sessions", project_a["project_id"], key), store.records)
            self.assertIn(("sessions", project_a["project_id"], claude_key), store.records)
            self.assertIn(("sessions", project_b["project_id"], key), store.records)
            self.assertEqual({key, claude_key}, {item["session_id"] for item in recent_sessions(store, project_a["project_id"], reviews=[])})
            self.assertEqual([key], [item["session_id"] for item in recent_sessions(store, project_b["project_id"], reviews=[])])

            before = deepcopy(store.records)
            import_codex_sessions(store, root, [project_a, project_b], other)
            self.assertEqual(before, store.records)
            self.assertEqual([], store.deleted)

    def test_reclassification_keeps_old_location_when_new_write_fails(self):
        temp, root, _ = self.session_file(fixture("C:/elsewhere"))
        project_b = deepcopy(PROJECT); project_b.update(project_id="other-project", name="Other", aliases=[], repo="https://github.com/example/other", working_directory="C:/other")
        with temp:
            store = MemoryStore()
            origin = lambda _cwd: {"git_root": "C:/work/a", "remote": PROJECT["repo"]}
            other = lambda _cwd: {"git_root": "C:/work/b", "remote": project_b["repo"]}
            import_codex_sessions(store, root, [PROJECT, project_b], origin)
            key = ("sessions", PROJECT["project_id"], "codex:session-123")
            original_put = store.put
            def fail_new_location(area, project_id, name, document):
                if area == "sessions" and project_id == project_b["project_id"]:
                    raise TaskError("write failed")
                return original_put(area, project_id, name, document)
            store.put = fail_new_location
            with self.assertRaisesRegex(TaskError, "write failed"):
                import_codex_sessions(store, root, [PROJECT, project_b], other)
            self.assertIn(key, store.records)
            self.assertEqual([], store.deleted)

    def test_reciprocal_relocation_never_deletes_either_partition(self):
        temp, root, _ = self.session_file(fixture("C:/elsewhere"))
        project_b = deepcopy(PROJECT); project_b.update(project_id="other-project", name="Other", aliases=[], repo="https://github.com/example/other", working_directory="C:/other")
        with temp:
            origin = lambda _cwd: {"git_root": "C:/work/a", "remote": PROJECT["repo"]}
            other = lambda _cwd: {"git_root": "C:/work/b", "remote": project_b["repo"]}
            key = "codex:session-123"

            store = MemoryStore()
            import_codex_sessions(store, root, [PROJECT, project_b], origin)
            stale_key = ("sessions", PROJECT["project_id"], key)
            import_codex_sessions(store, root, [PROJECT, project_b], other)
            other_key = ("sessions", project_b["project_id"], key)
            self.assertIn(stale_key, store.records)
            self.assertIn(other_key, store.records)

            fresher = deepcopy(store.records[stale_key])
            fresher.update(updated_at="2026-08-10T09:00:00Z", source_identifier="fresher.jsonl")
            result = import_sessions(store, StaticAdapter([fresher]), projects=[PROJECT, project_b], repository_lookup=origin)
            self.assertEqual("fresher.jsonl", result[0]["source_identifier"])
            self.assertIn(stale_key, store.records)
            self.assertIn(other_key, store.records)
            self.assertEqual([], store.deleted)

    def test_multiple_stale_copies_remain_readable_while_target_is_written(self):
        temp, root, _ = self.session_file(fixture("C:/elsewhere"))
        project_b = deepcopy(PROJECT); project_b.update(project_id="other-project", name="Other", aliases=[], repo="https://github.com/example/other", working_directory="C:/other")
        with temp:
            store = MemoryStore()
            no_match = lambda _cwd: {"git_root": None, "remote": None}
            other = lambda _cwd: {"git_root": "C:/work/b", "remote": project_b["repo"]}
            import_codex_sessions(store, root, [PROJECT, project_b], no_match)
            key = "codex:session-123"
            duplicate = deepcopy(store.records[("sessions", "_unclassified", key)])
            store.put("sessions", PROJECT["project_id"], key, duplicate)
            import_codex_sessions(store, root, [PROJECT, project_b], other)
            self.assertEqual(key, store.get("sessions", "_unclassified", key)["session_id"])
            self.assertEqual(key, store.get("sessions", PROJECT["project_id"], key)["session_id"])
            self.assertIn(("sessions", project_b["project_id"], key), store.records)
            self.assertEqual([], store.deleted)

    def test_duplicate_sources_and_registry_choose_deterministic_freshest_record(self):
        temp, root, _ = self.session_file()
        with temp:
            base = discover_codex_sessions(root)[0]
            older = deepcopy(base); older.update(updated_at="2026-08-10T02:00:00Z", source_identifier="z-older.jsonl")
            newer = deepcopy(base); newer.update(updated_at="2026-08-10T03:00:00Z", source_identifier="a-newer.jsonl")
            store = MemoryStore()
            result = import_sessions(store, StaticAdapter([newer, older]), projects=[PROJECT], repository_lookup=lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("a-newer.jsonl", result[0]["source_identifier"])

            first = deepcopy(base); first.update(updated_at="2026-08-10T04:00:00Z", source_identifier="a.jsonl", content_hash="a")
            second = deepcopy(base); second.update(updated_at="2026-08-10T04:00:00Z", source_identifier="b.jsonl", content_hash="b")
            result = import_sessions(MemoryStore(), StaticAdapter([second, first]), projects=[PROJECT], repository_lookup=lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("b.jsonl", result[0]["source_identifier"])

            registry = MemoryStore()
            existing = deepcopy(result[0])
            existing.update(updated_at="2026-08-11T00:00:00Z", source_identifier="registry-newer.jsonl", project_id=None, classification_method="unclassified", mapping_source=None, classification_confidence=None, classification_status="needs_review")
            registry.put("sessions", "_unclassified", existing["session_id"], existing)
            stale_registry_copy = deepcopy(result[0]); stale_registry_copy.update(updated_at="2026-08-10T01:00:00Z", source_identifier="registry-older.jsonl")
            registry.put("sessions", PROJECT["project_id"], existing["session_id"], stale_registry_copy)
            result = import_sessions(registry, StaticAdapter([older]), projects=[PROJECT], repository_lookup=lambda _cwd: {"git_root": None, "remote": None})
            self.assertEqual("registry-newer.jsonl", result[0]["source_identifier"])
            self.assertIn(("sessions", "_unclassified", existing["session_id"]), registry.records)
            self.assertIn(("sessions", PROJECT["project_id"], existing["session_id"]), registry.records)
            self.assertEqual([], registry.deleted)

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

    def test_review_queue_excludes_classified_and_preserves_source(self):
        temp, root, path = self.session_file(fixture("C:/elsewhere"))
        with temp:
            before = path.read_bytes()
            unresolved = classify_project(discover_codex_sessions(root)[0], [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            queue = review_queue([unresolved])
            self.assertEqual(1, len(queue))
            self.assertEqual("unclassified", queue[0]["classification_reason"])
            self.assertIn("prompt_snippet", queue[0])
            resolved = dict(unresolved, classification_status="classified")
            self.assertEqual([], review_queue([resolved]))
            self.assertEqual(before, path.read_bytes())

    def test_manual_assignment_is_validated_idempotent_and_audited(self):
        store = MemoryStore()
        session = {"session_id": "review-1", "provider": "codex", "project_id": None, "source_identifier": "sessions/test.jsonl"}
        first = assign_review(store, session, "ai-development-manager", [PROJECT], "2026-08-10T01:00:00Z")
        validate("session_review", first)
        self.assertEqual(REVIEW_PROJECT_ID, next(iter(store.records))[1])
        same = assign_review(store, session, "ai-development-manager", [PROJECT], "2026-08-10T02:00:00Z")
        self.assertEqual(1, len(same["assignment_history"]))
        with self.assertRaises(Exception): assign_review(store, session, "missing", [PROJECT], "2026-08-10T02:00:00Z")
        other = deepcopy(PROJECT); other["project_id"] = "other-project"
        changed = assign_review(store, session, "other-project", [PROJECT, other], "2026-08-10T03:00:00Z")
        self.assertEqual("other-project", changed["project_id"])
        self.assertEqual("manual_review", changed["mapping_source"])
        self.assertEqual({"previous_project_id": "ai-development-manager", "new_project_id": "other-project", "assigned_at": "2026-08-10T03:00:00Z"}, changed["assignment_history"][-1])

    def test_legacy_codex_review_is_readable_and_new_reviews_do_not_collide(self):
        legacy = {"session_id": "abc123", "provider": "codex", "project_id": "ai-development-manager", "classification_method": "manual_review", "classification_status": "classified", "source_identifier": "sessions/abc123.jsonl", "assigned_at": "2026-08-10T00:00:00Z", "assignment_history": [{"previous_project_id": None, "new_project_id": "ai-development-manager", "assigned_at": "2026-08-10T00:00:00Z"}]}
        codex = search_record("codex:abc123", provider_session_id="abc123", project_id=None)
        claude = search_record("claude:abc123", provider="claude", provider_session_id="abc123", project_id=None)
        self.assertEqual("ai-development-manager", search_sessions([codex], project="ai-development-manager", review_records=[legacy])[0]["project_id"])
        store = MemoryStore()
        first = assign_review(store, codex, "ai-development-manager", [PROJECT], "2026-08-10T01:00:00Z")
        second = assign_review(store, claude, "ai-development-manager", [PROJECT], "2026-08-10T01:00:00Z")
        validate("session_review", first); validate("session_review", second)
        self.assertEqual({"codex:abc123", "claude:abc123"}, {key[2] for key in store.records})
        self.assertEqual([], review_queue([codex, claude], [first, second]))
        legacy_store = MemoryStore(); legacy_store.records[("session_reviews", REVIEW_PROJECT_ID, "abc123")] = deepcopy(legacy)
        other = deepcopy(PROJECT); other["project_id"] = "other-project"
        updated = assign_review(legacy_store, codex, "other-project", [PROJECT, other], "2026-08-10T02:00:00Z")
        self.assertEqual("abc123", updated["session_id"])
        self.assertEqual({"abc123"}, {key[2] for key in legacy_store.records})
        collision_store = MemoryStore(); collision_store.records[("session_reviews", REVIEW_PROJECT_ID, "abc123")] = deepcopy(legacy)
        claude_review = assign_review(collision_store, claude, "other-project", [PROJECT, other], "2026-08-10T03:00:00Z")
        self.assertEqual({"abc123", "claude:abc123"}, {key[2] for key in collision_store.records})
        self.assertEqual("codex", collision_store.records[("session_reviews", REVIEW_PROJECT_ID, "abc123")]["provider"])
        self.assertEqual("claude", claude_review["provider"])
        self.assertEqual(1, len(claude_review["assignment_history"]))
        self.assertNotEqual(legacy["assignment_history"], claude_review["assignment_history"])

    def test_canonical_review_lookup_rejects_wrong_stored_identity(self):
        store = MemoryStore()
        claude = search_record("claude:abc123", provider="claude", provider_session_id="abc123", project_id=None)
        wrong = {
            "session_id": "codex:abc123", "provider": "codex", "provider_session_id": "abc123",
            "project_id": "ai-development-manager", "classification_method": "manual_review",
            "classification_status": "classified", "mapping_source": "manual_review",
            "source_identifier": "sessions/codex.jsonl", "assigned_at": "2026-08-10T00:00:00Z",
            "assignment_history": [{"previous_project_id": None, "new_project_id": "ai-development-manager", "assigned_at": "2026-08-10T00:00:00Z"}],
        }
        canonical_key = ("session_reviews", REVIEW_PROJECT_ID, "claude:abc123")
        store.records[canonical_key] = deepcopy(wrong)
        before = deepcopy(store.records)
        with self.assertRaisesRegex(TaskError, "identity conflict"):
            assign_review(store, claude, "ai-development-manager", [PROJECT], "2026-08-10T01:00:00Z")
        self.assertEqual(before, store.records)

    def test_search_filters_text_metadata_dates_and_manual_mapping(self):
        first = search_record()
        second = search_record("search-2", project_id=None, task_id="other-task", title="Other", first_user_prompt="Different", updated_at="2026-07-01T00:00:00Z")
        records = [first, second]
        self.assertEqual(["search-1"], [item["session_id"] for item in search_sessions(records, query="quota")])
        self.assertEqual(["search-1"], [item["session_id"] for item in search_sessions(records, query="中文")])
        self.assertEqual(["search-1"], [item["session_id"] for item in search_sessions(records, project="AI-DEVELOPMENT-MANAGER", task="session-organizer", status="needs_review", since="2026-08-01")])
        self.assertEqual([], search_sessions(records, query="no-result"))
        review = {"session_id": "search-2", "provider": "codex", "project_id": "ai-development-manager", "classification_method": "manual_review", "classification_status": "classified", "source_identifier": "sessions/search-2.jsonl", "assigned_at": "2026-08-10T00:00:00Z", "assignment_history": [{"previous_project_id": None, "new_project_id": "ai-development-manager", "assigned_at": "2026-08-10T00:00:00Z"}]}
        mapped = search_sessions(records, project="ai-development-manager", review_records=[review])
        self.assertEqual({"search-1", "search-2"}, {item["session_id"] for item in mapped})

    def test_search_is_read_only_for_source_fixture(self):
        temp, root, path = self.session_file()
        with temp:
            before = path.read_bytes()
            record = classify_project(discover_codex_sessions(root)[0], [PROJECT], lambda _cwd: {"git_root": None, "remote": None})
            search_sessions([record], query="registry")
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__": unittest.main()
