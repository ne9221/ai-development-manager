import re
import sys
import types
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from manager.tasks import DriveRecords, MIME_FOLDER, ROOT_FOLDER_ID, TaskError
from manager.worktree_locks import LOCK_NAMESPACE, acquire, check, inspect, release


NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document): self.records[(area, project, name)] = deepcopy(document); return deepcopy(document)
    def get(self, area, project, name):
        if (area, project, name) not in self.records: raise TaskError("not found")
        return deepcopy(self.records[(area, project, name)])
    def list_records(self, area, project): return [deepcopy(value) for (stored_area, stored_project, _), value in self.records.items() if (stored_area, stored_project) == (area, project)]


def lock(store, lock_id, branch="feature/a", scope=None, access="production", at=NOW, repository="https://github.com/example/repo"):
    return acquire(store, lock_id, "p1", f"task-{lock_id}", f"exec-{lock_id}", "codex", repository, branch, scope or [f"src/{lock_id}.py"], "abc123", access, at=at)


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class FakeMedia:
    def __init__(self, stream, mimetype, resumable=False): self.raw = stream.read()


class FakeFiles:
    def __init__(self):
        self.next_id = 1
        self.items = {ROOT_FOLDER_ID: {"id": ROOT_FOLDER_ID, "name": "root", "mimeType": MIME_FOLDER, "parents": []}}
        self.content = {}

    def list(self, q, **kwargs):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        name_match = re.search(r"and name='([^']+)'", q)
        values = [item for item in self.items.values() if parent in item.get("parents", []) and (not name_match or item["name"] == name_match.group(1))]
        return Request({"files": deepcopy(values)})

    def create(self, body, media_body=None, **kwargs):
        file_id = f"id-{self.next_id}"; self.next_id += 1
        self.items[file_id] = {"id": file_id, **deepcopy(body)}
        if media_body: self.content[file_id] = media_body.raw
        return Request({"id": file_id})

    def update(self, fileId, body, media_body, **kwargs):
        self.items[fileId].update(deepcopy(body)); self.content[fileId] = media_body.raw
        return Request({"id": fileId})

    def get_media(self, fileId): return Request(self.content[fileId])


class FakeDrive:
    def __init__(self): self.api = FakeFiles()
    def files(self): return self.api


class WorktreeLockTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore()

    def test_same_repo_same_branch_conflicts_even_when_disjoint(self):
        lock(self.store, "one", scope=["src/a.py"])
        with self.assertRaisesRegex(TaskError, "same repository and branch"): lock(self.store, "two", scope=["src/b.py"])

    def test_same_repo_conflicts_across_project_ids(self):
        lock(self.store, "one")
        with self.assertRaisesRegex(TaskError, "same repository and branch"):
            acquire(self.store, "two", "p2", "task-two", "exec-two", "claude", "https://github.com/example/repo.git", "feature/a", ["other.py"], "def456", at=NOW)

    def test_overlapping_files_conflict_across_branches(self):
        lock(self.store, "one", scope=["manager"])
        with self.assertRaisesRegex(TaskError, "overlapping file scope"): lock(self.store, "two", branch="feature/b", scope=["manager/sessions.py"])

    def test_disjoint_files_on_separate_branches_are_allowed(self):
        lock(self.store, "one", scope=["manager/a.py"])
        self.assertEqual("two", lock(self.store, "two", branch="feature/b", scope=["manager/b.py"])["lock_id"])

    def test_read_only_is_allowed_and_unknown_production_is_blocked(self):
        lock(self.store, "one")
        readonly = acquire(self.store, "read", "p1", "task-read", "exec-read", "claude", access="read_only", at=NOW)
        self.assertEqual("read_only", readonly["access"])
        self.assertFalse(check(self.store, {**readonly, "lock_id": "bad", "access": "production"}, NOW)["safe"])

    def test_stale_lock_does_not_conflict(self):
        lock(self.store, "old", at=NOW - timedelta(hours=2))
        self.assertEqual("expired", inspect(self.store, "p1", "old", NOW)["effective_status"])
        self.assertEqual("new", lock(self.store, "new")["lock_id"])

    def test_release_removes_conflict_and_is_idempotent(self):
        lock(self.store, "one")
        first = release(self.store, "p1", "one", NOW)
        self.assertEqual(first, release(self.store, "p1", "one", NOW + timedelta(minutes=1)))
        self.assertEqual("two", lock(self.store, "two")["lock_id"])

    def test_duplicate_acquire_is_idempotent(self):
        first = lock(self.store, "one")
        self.assertEqual(first, lock(self.store, "one"))

    def test_drive_records_persist_and_read_back_lock(self):
        drive = FakeDrive(); store = DriveRecords(drive)
        google = types.ModuleType("googleapiclient"); http = types.ModuleType("googleapiclient.http"); http.MediaIoBaseUpload = FakeMedia
        with patch.dict(sys.modules, {"googleapiclient": google, "googleapiclient.http": http}):
            saved = lock(store, "drive")
            self.assertEqual(saved, store.get("worktree_locks", LOCK_NAMESPACE, "drive"))
            self.assertTrue(any(item["name"] == "WORKTREE-LOCKS" for item in drive.api.items.values()))


if __name__ == "__main__": unittest.main()
