import re
import threading
import unittest
from copy import deepcopy
from unittest.mock import patch

from manager.tasks import MIME_FOLDER, ROOT_FOLDER_ID, ROOT_FOLDERS, DriveRecords, TaskError, complete_task, create_handoff, create_project, create_task, logical_record_id, record_storage_id, update_task, validate


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document):
        self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name): return deepcopy(self.records[(area, project, name)])
    def latest(self, area, project, task):
        items = [value for (a, p, _), value in self.records.items() if a == area and p == project and value.get("task_id") == task]
        return max(items, key=lambda item: item["created_at"])


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value() if callable(self.value) else self.value


class FakeDriveFiles:
    """In-memory fake Drive backend. `on_list`, if set, is invoked with the raw
    query string right after a list() call has snapshotted its result but
    before that result is handed back -- used by concurrency tests to pause a
    caller *after* it has observed "missing" so a second caller can observe
    the same stale "missing" snapshot, forcing a real TOCTOU interleaving
    (pausing before the snapshot would let CPython's GIL scheduling resolve
    the two callers sequentially by luck instead of racing them)."""
    def __init__(self, on_list=None): self.items = {}; self.next_id = 1; self.lock = threading.Lock(); self.on_list = on_list
    def list(self, q, **_kwargs):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        name_match = re.search(r" and name='([^']*)'", q)
        name = name_match.group(1) if name_match else None
        def result():
            with self.lock:
                values = [deepcopy(item["meta"]) for item in self.items.values() if parent in item["meta"].get("parents", []) and (name is None or item["meta"]["name"] == name)]
            if self.on_list:
                self.on_list(q)
            return {"files": values}
        return Request(result)
    def create(self, body, media_body=None, **_kwargs):
        def result():
            with self.lock:
                created_seq = self.next_id; self.next_id += 1
                file_id = f"file-{created_seq:06d}"
                meta = dict(body, id=file_id, createdTime=f"{created_seq:012d}")
                raw = media_body.getbytes(0, media_body.size()) if media_body else b""
                self.items[file_id] = {"meta": meta, "raw": raw}
            return {"id": file_id}
        return Request(result)
    def update(self, fileId, body, media_body, **_kwargs):
        def result():
            with self.lock:
                self.items[fileId]["meta"].update(body)
                self.items[fileId]["raw"] = media_body.getbytes(0, media_body.size())
            return {"id": fileId}
        return Request(result)
    def get_media(self, fileId): return Request(lambda: self.items[fileId]["raw"])
    def delete(self, fileId):
        def result():
            with self.lock:
                self.items.pop(fileId)
            return {}
        return Request(result)


class FakeDriveService:
    def __init__(self, on_list=None): self.transport = FakeDriveFiles(on_list=on_list)
    def files(self): return self.transport


def task_input():
    return {
        "task_id": "phase-5", "project_id": "ai-development-manager", "title": "Phase 5",
        "task_type": "implementation", "expected_minutes": 20,
        "scope": ["TASKS and HANDOFFS"], "constraints": ["No automatic AI execution"],
        "acceptance_criteria": ["Drive round trip passes"], "source_context": {"phase": 5},
    }


def handoff_input(reason="provider switch"):
    return {
        "handoff_id": "phase-5-codex-claude", "task_id": "phase-5", "project_id": "ai-development-manager",
        "from_provider": "codex", "to_provider": "claude", "from_session": "session-a", "reason": reason,
        "completed_work": ["Schemas implemented"], "current_state": "Tests pending", "files_changed": ["schema/task.schema.json"],
        "commits": [], "tests": ["unit tests pending"], "known_issues": [], "do_not_touch": ["Phase 1-4.5 collectors"],
        "next_action": "Run Drive round trip", "acceptance_criteria": ["Drive round trip passes"],
        "minimal_context": "Phase 5 schemas and manager are implemented; validate Drive persistence next."
    }


class TaskTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore()

    def create(self):
        decision = {"recommended_provider": "codex", "recommended_mode": "code", "recommended_effort": "medium", "quota_evidence": {"codex": {"freshness": "fresh"}}}
        with patch("manager.tasks.read_drive_status", return_value={}), patch("manager.tasks.summarize", return_value={}), patch("manager.tasks.decide", return_value=decision):
            return create_task(self.store, task_input(), service=object())

    def test_create_assignment_update_and_block(self):
        task = self.create()
        self.assertEqual("codex", task["recommended_provider"])
        task = update_task(self.store, task["project_id"], task["task_id"], status="in_progress", current_progress="Schemas done", assigned_provider="codex")
        self.assertEqual("in_progress", task["status"])
        task = update_task(self.store, task["project_id"], task["task_id"], status="blocked", blocked_reason="Needs handoff")
        self.assertEqual("Needs handoff", task["blocked_reason"])

    def test_provider_and_session_handoffs_are_minimal(self):
        first_data = handoff_input(); first_data["created_at"] = "2026-08-09T00:00:00Z"
        first = create_handoff(self.store, first_data)
        second = handoff_input("session switch"); second.update(handoff_id="phase-5-session-b", from_provider="claude", to_provider="claude", from_session="session-a", created_at="2026-08-09T00:01:00Z")
        create_handoff(self.store, second)
        self.assertEqual("session switch", self.store.latest("handoffs", "ai-development-manager", "phase-5")["reason"])
        self.assertLess(len(first["minimal_context"]), 4000)
        self.assertNotIn("README", first["minimal_context"])

    def test_complete_preserves_task_history_and_final_handoff(self):
        self.create()
        report = {
            "ai": "Codex", "project": "ai-development-manager", "task": "phase-5",
            "conversation": "test", "session": "session-b", "current_progress": "Complete",
            "overall_project_progress": "Phase 5 complete", "milestone_progress": "Complete",
            "estimated_remaining": "0 minutes", "waiting_blocker": "None",
            "actual_ai_provider_running_now": "None", "rule_evidence": {},
        }
        task, handoff = complete_task(self.store, "ai-development-manager", "phase-5", "All acceptance criteria passed", "codex", "session-b", report)
        self.assertEqual("completed", task["status"])
        self.assertTrue(any(key[0] == "history" for key in self.store.records))
        self.assertEqual("completed", handoff["reason"])

    def test_status_report_requires_mandatory_fields(self):
        self.create()
        with self.assertRaises(TaskError) as ctx:
            complete_task(self.store, "ai-development-manager", "phase-5", "Done", "codex", "session-b", status_report={"current_progress": "done"})
        self.assertIn("status report missing mandatory fields", str(ctx.exception))
        task, _ = complete_task(self.store, "ai-development-manager", "phase-5", "Done", "codex", "session-b", status_report={
            "current_progress": "done", "overall_project_progress": "80%", "milestone_progress": "M3 complete",
            "estimated_remaining": "none", "waiting_blocker": "none", "actual_ai_provider_running": "codex",
        })
        self.assertEqual("completed", task["status"])

    def test_malformed_records_rejected(self):
        with self.assertRaises(TaskError): validate("task", {"task_id": "broken"})
        broken = handoff_input(); broken["minimal_context"] = "x" * 4001
        with self.assertRaises(TaskError): create_handoff(self.store, broken)

    def test_project_record(self):
        project = {"project_id": "ai-development-manager", "name": "AI Development Manager", "repo": "https://github.com/ne9221/ai-development-manager", "default_branch": "main", "runtime_ssot": "Google Drive/AI Development Manager", "project_rules": ["Drive is runtime SSOT"], "active_tasks": ["phase-5"], "current_phase": "Phase 5", "important_constraints": ["Do not auto-start AI"]}
        self.assertEqual(project, create_project(self.store, project))

    def test_project_governance_fields_match_drive_ssot_and_remain_strict(self):
        project = {
            "project_id": "ai-development-manager", "name": "AI Development Manager",
            "repo": "https://github.com/ne9221/ai-development-manager", "default_branch": "main",
            "runtime_ssot": "Google Drive/AI Development Manager", "project_rules": [],
            "active_tasks": [], "current_phase": "Phase 3C", "important_constraints": [],
            "updated_at": "2026-08-13T00:31:00+08:00",
            "priority_roadmap": [{"priority": "P0", "order": 1, "id": "task-centric-foundation",
                                  "title": "Task-centric foundation", "goal": "Make tasks the primary unit."}],
            "task_management_policy": {
                "relationship": "Project -> Task -> Execution -> Session", "primary_unit": "task",
                "conversation_role": "metadata_only", "default_task_minutes_max": 20,
                "quota_read_required_before_dispatch": True,
                "parallel_write_requires_isolated_worktree": True,
                "execution_loop": ["compile context", "launch provider", "persist terminal state"],
                "human_intervention_only_when": ["requirements conflict"],
            },
            "reference_architecture": [
                {"project": "Agent Orchestrator", "use_for": ["worktree isolation"]},
                {"project": "CAS", "use_for": ["context model"],
                 "storage_policy": "reference only; do not copy local-first SSOT"},
            ],
        }
        validate("project", project)

        malformed = []
        unexpected = deepcopy(project); unexpected["unexpected"] = True; malformed.append(unexpected)
        roadmap = deepcopy(project); roadmap["priority_roadmap"][0]["order"] = "first"; malformed.append(roadmap)
        policy = deepcopy(project); policy["task_management_policy"]["quota_read_required_before_dispatch"] = "yes"; malformed.append(policy)
        architecture = deepcopy(project); architecture["reference_architecture"][0]["unknown"] = True; malformed.append(architecture)
        for record in malformed:
            with self.assertRaises(TaskError):
                validate("project", record)

    def test_drive_records_round_trip_logical_ids_without_filename_collisions(self):
        service = FakeDriveService(); store = DriveRecords(service)
        values = ["abc123", "claude:abc123", "codex:abc123", "abc:def", "abc%3Adef", "Unicode-é", "中文", "a/b", "a\\b", " spaced value "]
        stems = [record_storage_id(value) for value in values]
        self.assertEqual(len(stems), len(set(stems)))
        self.assertEqual(values, [logical_record_id(value) for value in stems])
        self.assertEqual("abc123", stems[0])
        self.assertTrue(all(not re.search(r"[\\/ ]", stem) for stem in stems))
        for index, value in enumerate(values):
            document = {"logical_id": value, "index": index}
            self.assertEqual(document, store.put("sessions", "project-a", value, document))
            self.assertEqual(document, store.get("sessions", "project-a", value))
        self.assertEqual(values, [item["logical_id"] for item in store.list_records("sessions", "project-a")])
        for value in values:
            self.assertTrue(store.delete("sessions", "project-a", value))
        self.assertEqual([], store.list_records("sessions", "project-a"))

    def test_drive_conditional_delete_preserves_replacement(self):
        service = FakeDriveService(); store = DriveRecords(service)
        logical_id = "claude:abc123"
        store.put("sessions", "project-a", logical_id, {"version": 1})
        _, token = store.get_with_token("sessions", "project-a", logical_id)
        store.put("sessions", "project-a", logical_id, {"version": 2})
        self.assertFalse(store.delete("sessions", "project-a", logical_id, expected=token))
        self.assertEqual({"version": 2}, store.get("sessions", "project-a", logical_id))

        _, token = store.get_with_token("sessions", "project-a", logical_id)
        parent = store.project_folder("sessions", "project-a", create=False)
        filename = store.record_filename(logical_id)
        old_id = store.children(parent, filename)[0]["id"]
        service.transport.items.pop(old_id)
        store.put("sessions", "project-a", logical_id, {"version": 3})
        self.assertFalse(store.delete("sessions", "project-a", logical_id, expected=token))
        self.assertEqual({"version": 3}, store.get("sessions", "project-a", logical_id))


class DriveFolderRaceTests(unittest.TestCase):
    """Regression coverage for the first-write TOCTOU race on Drive folder
    creation: two processes both see "folder missing" and both create it,
    leaving two same-named folders. folder()/project_folder() must resolve
    every racer to one canonical folder instead."""

    def test_single_writer_first_create_is_normal_path(self):
        store = DriveRecords(FakeDriveService())
        folder_id = store.project_folder("sessions", "project-a")
        self.assertEqual(folder_id, store.project_folder("sessions", "project-a", create=False))
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["sessions"], create=False)
        self.assertEqual(1, len(store.children(root, "project-a")))

    def test_folder_already_exists_is_reused_not_duplicated(self):
        store = DriveRecords(FakeDriveService())
        first = store.project_folder("commands", "project-a")
        second = store.project_folder("commands", "project-a")
        self.assertEqual(first, second)
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["commands"], create=False)
        self.assertEqual(1, len(store.children(root, "project-a")))

    def test_pre_existing_duplicate_folders_still_fail_closed(self):
        store = DriveRecords(FakeDriveService())
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["sessions"])
        store.files.create(body={"name": "project-a", "parents": [root], "mimeType": MIME_FOLDER}, fields="id").execute()
        store.files.create(body={"name": "project-a", "parents": [root], "mimeType": MIME_FOLDER}, fields="id").execute()
        with self.assertRaisesRegex(TaskError, "duplicate Drive folder"):
            store.project_folder("sessions", "project-a")
        with self.assertRaisesRegex(TaskError, "duplicate Drive folder"):
            store.project_folder("sessions", "project-a", create=False)

    def _race_two_first_creates(self, area, project_id):
        """Hand-orchestrate the exact TOCTOU interleaving: both racers observe
        "no folder" before either creates, then both create. Deterministic and
        reproducible without relying on real thread scheduling."""
        store = DriveRecords(FakeDriveService())
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS[area])
        self.assertEqual([], store.children(root, project_id))
        id_a = store.files.create(body={"name": project_id, "parents": [root], "mimeType": MIME_FOLDER}, fields="id").execute()["id"]
        id_b = store.files.create(body={"name": project_id, "parents": [root], "mimeType": MIME_FOLDER}, fields="id").execute()["id"]
        resolved_a = store._reconcile_created_folder(root, project_id, id_a)
        resolved_b = store._reconcile_created_folder(root, project_id, id_b)
        return store, root, resolved_a, resolved_b

    def test_two_concurrent_writers_same_project_and_area_resolve_to_one_canonical_folder(self):
        store, root, resolved_a, resolved_b = self._race_two_first_creates("sessions", "project-a")
        self.assertEqual(resolved_a, resolved_b)
        remaining = store.children(root, "project-a")
        self.assertEqual(1, len(remaining))
        self.assertEqual(resolved_a, remaining[0]["id"])
        self.assertEqual(resolved_a, store.project_folder("sessions", "project-a", create=False))

    def test_concurrent_commands_area_resolves_to_one_canonical_folder(self):
        store, root, resolved_a, resolved_b = self._race_two_first_creates("commands", "project-a")
        self.assertEqual(resolved_a, resolved_b)
        self.assertEqual(1, len(store.children(root, "project-a")))

    def test_concurrent_sessions_area_resolves_to_one_canonical_folder(self):
        store, root, resolved_a, resolved_b = self._race_two_first_creates("sessions", "project-a")
        self.assertEqual(resolved_a, resolved_b)
        self.assertEqual(1, len(store.children(root, "project-a")))

    def test_two_concurrent_writers_different_areas_do_not_interfere(self):
        store = DriveRecords(FakeDriveService())
        commands_id = store.project_folder("commands", "project-a")
        sessions_id = store.project_folder("sessions", "project-a")
        self.assertNotEqual(commands_id, sessions_id)
        commands_root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["commands"], create=False)
        sessions_root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["sessions"], create=False)
        self.assertEqual(1, len(store.children(commands_root, "project-a")))
        self.assertEqual(1, len(store.children(sessions_root, "project-a")))

    def test_two_concurrent_writers_different_projects_do_not_interfere(self):
        store = DriveRecords(FakeDriveService())
        a_id = store.project_folder("sessions", "project-a")
        b_id = store.project_folder("sessions", "project-b")
        self.assertNotEqual(a_id, b_id)
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["sessions"], create=False)
        self.assertEqual(1, len(store.children(root, "project-a")))
        self.assertEqual(1, len(store.children(root, "project-b")))

    def test_real_threaded_concurrent_first_create_does_not_duplicate_or_fail(self):
        """Same race as above, but driven by actual OS threads paused at a
        barrier inside list(), so both really do observe "missing" at the
        same time -- not just a hand-sequenced simulation."""
        barrier = threading.Barrier(2, timeout=5)
        seen = {"n": 0}
        seen_lock = threading.Lock()

        def on_list(query):
            if "name='project-a'" not in query:
                return
            with seen_lock:
                seen["n"] += 1
                should_wait = seen["n"] <= 2
            if should_wait:
                barrier.wait()

        store = DriveRecords(FakeDriveService(on_list=on_list))
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["sessions"])

        results, errors = {}, []

        def racer(key):
            try:
                results[key] = store.folder(root, "project-a")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=racer, args=(key,)) for key in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(results["a"], results["b"])
        self.assertEqual(1, len(store.children(root, "project-a")))


class PaginatedFakeDriveFiles:
    """A minimal fake supporting real multi-page pagination (unlike
    FakeDriveFiles above, which always returns everything in one page) --
    needed to exercise children()'s deadline-between-pages behavior.
    `get_media` raises if called at all, so any test using this fake that
    calls it proves a code path fetched a full record it shouldn't have."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.list_calls = 0

    def list(self, **_kwargs):
        index = min(self.list_calls, len(self.pages) - 1)
        self.list_calls += 1
        return Request(self.pages[index])

    def get_media(self, fileId):
        raise AssertionError(f"get_media must never be called by list_project_ids (fileId={fileId!r})")


class PaginatedFakeDriveService:
    def __init__(self, pages):
        self._files = PaginatedFakeDriveFiles(pages)

    def files(self):
        return self._files


def project_folder_item(name, file_id):
    return {"id": file_id, "name": name, "mimeType": MIME_FOLDER, "parents": [ROOT_FOLDER_ID]}


class ProjectIdEnumerationTests(unittest.TestCase):
    """Covers the Watcher pre-launch enumeration fix: list_project_ids()
    must be a cheap, deadline-aware, get()-free alternative to
    list_projects()'s full per-project hydration, and must not change
    list_projects()/children()'s existing behavior for any other caller."""

    def test_list_project_ids_returns_folder_names_with_no_get_calls(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["projects"])
        for project_id in ("p1", "p2", "p3"):
            store.folder(root, project_id)

        original_get_media = service.transport.get_media

        def guarded_get_media(fileId):
            raise AssertionError("list_project_ids must never call get_media (i.e. never hydrate a project document)")

        service.transport.get_media = guarded_get_media
        try:
            ids = store.list_project_ids()
        finally:
            service.transport.get_media = original_get_media

        self.assertEqual({"p1", "p2", "p3"}, set(ids))

    def test_list_project_ids_matches_list_projects_project_id_set(self):
        """N-project scenario: list_project_ids() must find exactly the
        same projects list_projects() would, just without hydrating any of
        them -- proving the fast path is a real substitute, not a
        different/narrower enumeration."""
        service = FakeDriveService()
        store = DriveRecords(service)
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["projects"])
        for project_id in ("alpha", "beta", "gamma", "delta"):
            store.folder(root, project_id)
            store.put("projects", project_id, project_id, {
                "project_id": project_id, "name": project_id, "created_at": "2026-08-22T00:00:00Z", "aliases": [],
            })

        fast_ids = set(store.list_project_ids())
        full_ids = {project["project_id"] for project in store.list_projects()}
        self.assertEqual(full_ids, fast_ids)

    def test_deadline_stops_mid_pagination_and_returns_partial(self):
        pages = [
            {"files": [project_folder_item("root", "root-id")], "nextPageToken": None},  # folder() resolving PROJECTS root
            {"files": [project_folder_item("p1", "f1")], "nextPageToken": "token-2"},     # page 1 of children(root)
            {"files": [project_folder_item("p2", "f2")], "nextPageToken": None},          # page 2, never reached
        ]
        service = PaginatedFakeDriveService(pages)
        store = DriveRecords(service)

        call_count = {"n": 0}

        def fake_monotonic():
            call_count["n"] += 1
            # 1st check: before page 1 of children(root) -- under budget.
            # 2nd check: before page 2 -- budget now spent, stop.
            return 0.0 if call_count["n"] <= 1 else 100.0

        with patch("manager.tasks.time.monotonic", side_effect=fake_monotonic):
            ids = store.list_project_ids(deadline=50.0)

        self.assertEqual(["p1"], ids)
        # Only 2 list() calls happened: resolving the PROJECTS root folder,
        # then page 1 of its children -- page 2 was never requested.
        self.assertEqual(2, service._files.list_calls)

    def test_no_deadline_is_unbounded_like_before(self):
        pages = [
            {"files": [project_folder_item("root", "root-id")], "nextPageToken": None},
            {"files": [project_folder_item("p1", "f1")], "nextPageToken": "token-2"},
            {"files": [project_folder_item("p2", "f2")], "nextPageToken": None},
        ]
        service = PaginatedFakeDriveService(pages)
        store = DriveRecords(service)
        ids = store.list_project_ids()
        self.assertEqual({"p1", "p2"}, set(ids))

    def test_children_default_deadline_none_is_byte_for_byte_unchanged(self):
        """Every existing caller of children() (folder(), get(),
        list_records(), list_projects(), put(), etc.) calls it with no
        deadline argument at all -- this proves that path is completely
        untouched by the new parameter's default."""
        service = FakeDriveService()
        store = DriveRecords(service)
        root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["projects"])
        store.folder(root, "p1")
        self.assertEqual(1, len(store.children(root)))


class ListRecordsBoundedTests(unittest.TestCase):
    """Covers the real HOME E2E blocker: list_records() for one project
    with a large historical Command backlog took 141.66s in a live
    reproduction, uninterruptible by any deadline once started (get() per
    record, no bound). list_records_bounded() must never unbounded-hydrate
    a project's full history, and must respect the 45s-transport-timeout
    vs 40s-poll-budget boundary explicitly, not just check the deadline
    after it has already passed."""

    def _seed_commands(self, store, project_id, count):
        for index in range(count):
            command_id = f"cmd-{index:03d}"
            store.put("commands", project_id, command_id, {
                "command_id": command_id, "project_id": project_id, "task_id": "t1", "provider": "codex",
                "model": None, "fallback_model": None, "mode": None, "effort": None,
                "selection_reason": [], "quota_evidence": None, "created_at": "2026-08-14T00:00:00Z",
                "status": "completed", "execution_id": None, "claimed_at": None,
                "completed_at": "2026-08-14T00:05:00Z", "result": {"status": "completed"},
            })

    # 1. A project with a large historical backlog is not unbounded-hydrated.
    def test_large_historical_backlog_is_not_unbounded_hydrated(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        self._seed_commands(store, "p1", 20)

        get_media_calls = {"n": 0}
        original = service.transport.get_media

        def counting_get_media(fileId):
            get_media_calls["n"] += 1
            return original(fileId)

        service.transport.get_media = counting_get_media
        try:
            # Deadline already exhausted before the very first hydration.
            records = store.list_records_bounded("commands", "p1", deadline=0.0)
        finally:
            service.transport.get_media = original

        self.assertEqual([], records)
        self.assertEqual(0, get_media_calls["n"])

    # 2. Deadline exhausted mid-hydration -> fully-read records returned, no half JSON.
    def test_deadline_mid_hydration_returns_only_fully_read_records(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        self._seed_commands(store, "p1", 5)

        call_count = {"n": 0}

        def fake_monotonic():
            call_count["n"] += 1
            # Allow the first 2 pre-hydration checks through, then expire.
            return 0.0 if call_count["n"] <= 2 else 1000.0

        with patch("manager.tasks.time.monotonic", side_effect=fake_monotonic):
            records = store.list_records_bounded("commands", "p1", deadline=500.0, single_request_worst_case=1.0)

        self.assertLess(len(records), 5)
        for record in records:
            # Every returned record is a real, fully-parsed document --
            # never a partial/truncated JSON fragment.
            self.assertIn("command_id", record)
            self.assertIn("status", record)

    # 3. Deadline already past -> zero new get_media calls at all.
    def test_expired_deadline_yields_zero_get_media_calls(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        self._seed_commands(store, "p1", 10)

        get_media_calls = {"n": 0}
        original = service.transport.get_media

        def counting_get_media(fileId):
            get_media_calls["n"] += 1
            return original(fileId)

        service.transport.get_media = counting_get_media
        try:
            records = store.list_records_bounded("commands", "p1", deadline=-1.0)
        finally:
            service.transport.get_media = original

        self.assertEqual([], records)
        self.assertEqual(0, get_media_calls["n"])

    # 4. Timing-boundary contract: a hydration is never started if the
    #    remaining budget is smaller than one worst-case single request,
    #    even though the deadline has not technically passed yet -- this is
    #    exactly what prevents a 40s poll budget + 45s transport timeout
    #    from silently composing into an 85s tick.
    def test_hydration_never_starts_inside_the_single_request_worst_case_margin(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        self._seed_commands(store, "p1", 3)

        get_media_calls = {"n": 0}
        original = service.transport.get_media

        def counting_get_media(fileId):
            get_media_calls["n"] += 1
            return original(fileId)

        service.transport.get_media = counting_get_media

        def fake_monotonic():
            # Remaining budget is exactly 10s, less than the 45s worst-case
            # single-request bound -- deadline itself has NOT passed.
            return 40.0

        try:
            with patch("manager.tasks.time.monotonic", side_effect=fake_monotonic):
                records = store.list_records_bounded("commands", "p1", deadline=50.0, single_request_worst_case=45)
        finally:
            service.transport.get_media = original

        # deadline (50.0) had NOT technically passed at "now" (40.0) --
        # only the worst-case-margin check should have stopped this.
        self.assertEqual([], records, "must not start a hydration with insufficient worst-case headroom")
        self.assertEqual(0, get_media_calls["n"])

    # 5. Malformed individual record is skipped, others still returned --
    #    unlike list_records(), one bad record must not fail the whole call.
    def test_malformed_record_is_skipped_not_fatal(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        self._seed_commands(store, "p1", 3)
        # Corrupt one record's raw bytes directly in the fake backend.
        for file_id, entry in service.transport.items.items():
            if entry["meta"]["name"].startswith("cmd-001"):
                entry["raw"] = b"not valid json{{{"
                break

        records = store.list_records_bounded("commands", "p1")
        self.assertEqual(2, len(records))

    # No-deadline call is unbounded, like list_records(), and matches its result set.
    def test_no_deadline_matches_list_records_result_set(self):
        service = FakeDriveService()
        store = DriveRecords(service)
        self._seed_commands(store, "p1", 6)

        bounded = {record["command_id"] for record in store.list_records_bounded("commands", "p1")}
        unbounded = {record["command_id"] for record in store.list_records("commands", "p1")}
        self.assertEqual(unbounded, bounded)


if __name__ == "__main__": unittest.main()
