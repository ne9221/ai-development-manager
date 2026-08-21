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

    def test_list_projects_reads_each_project_without_relisting_projects_root(self):
        service = FakeDriveService()
        writer = DriveRecords(service)
        for project_id in ("project-a", "project-b", "project-c"):
            writer.put("projects", project_id, project_id, {"project_id": project_id})
        projects_root = writer.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["projects"], create=False)
        queries = []
        service.transport.on_list = queries.append
        self.assertEqual(["project-a", "project-b", "project-c"],
                         [item["project_id"] for item in DriveRecords(service).list_projects()])
        self.assertEqual(1, sum(f"'{projects_root}' in parents" in query for query in queries))

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


if __name__ == "__main__": unittest.main()
