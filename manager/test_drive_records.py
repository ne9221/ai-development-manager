import sys
import types
import unittest
from unittest.mock import patch

from manager.tasks import DriveConflict, DriveRecords, TaskError


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class PagedFiles:
    def __init__(self, responses): self.responses = responses; self.calls = []
    def list(self, **options):
        self.calls.append(options)
        return Request(self.responses[options.get("pageToken")])


class Service:
    def __init__(self, files): self._files = files
    def files(self): return self._files


class Media:
    def __init__(self, stream, mimetype, resumable=False): self.raw = stream.read()


class HeaderRequest:
    def __init__(self, response, content, action=None):
        self.response, self.content, self.action = response, content, action
        self.headers = {}
        self.postproc = lambda _response, value: value
    def execute(self):
        if self.action: self.action(self)
        return self.postproc(self.response, self.content)


class PreconditionError(Exception):
    def __init__(self): self.resp = types.SimpleNamespace(status=412)


class VersionedFiles:
    def __init__(self): self.etag = '"v1"'; self.raw = b'{"value": 1}\n'; self.last_headers = None
    def get_media(self, fileId):
        return HeaderRequest({"etag": self.etag, "date": "Wed, 12 Aug 2026 00:00:00 GMT"}, self.raw)
    def update(self, fileId, body, media_body, fields):
        def action(request):
            self.last_headers = dict(request.headers)
            if request.headers.get("If-Match") != self.etag: raise PreconditionError()
            self.raw = media_body.raw; self.etag = '"v2"'
        return HeaderRequest({"etag": '"v2"', "date": "Wed, 12 Aug 2026 00:00:01 GMT"}, {"id": fileId}, action)


class DriveRecordTests(unittest.TestCase):
    def test_full_pagination_finds_active_record_on_second_page(self):
        first = [{"id": str(index), "name": f"released-{index}.json"} for index in range(100)]
        second = [{"id": str(index), "name": f"released-{index}.json"} for index in range(100, 150)] + [{"id": "active", "name": "active-conflict.json"}]
        files = PagedFiles({None: {"files": first, "nextPageToken": "page-2"}, "page-2": {"files": second}})
        records = DriveRecords(Service(files)).children("parent")
        self.assertEqual(151, len(records)); self.assertEqual("active", records[-1]["id"])
        self.assertEqual("page-2", files.calls[1]["pageToken"])
        self.assertIn("nextPageToken", files.calls[0]["fields"])

    def test_empty_page_continues_and_repeated_token_fails_closed(self):
        files = PagedFiles({None: {"nextPageToken": "next"}, "next": {"files": [{"id": "one", "name": "one.json"}]}})
        self.assertEqual("one", DriveRecords(Service(files)).children("parent")[0]["id"])
        repeated = PagedFiles({None: {"files": [], "nextPageToken": "again"}, "again": {"files": [], "nextPageToken": "again"}})
        with self.assertRaisesRegex(TaskError, "repeated"):
            DriveRecords(Service(repeated)).children("parent")

    def test_malformed_page_fails_closed(self):
        for response in (None, {"files": None}, {"files": {}, "nextPageToken": "x"}, {"files": [], "nextPageToken": 3}):
            with self.assertRaises(TaskError):
                DriveRecords(Service(PagedFiles({None: response}))).children("parent")

    def test_versioned_read_and_if_match_update(self):
        files = VersionedFiles(); store = DriveRecords(Service(files))
        document, etag, server_time = store.read_versioned_json("registry-id")
        self.assertEqual({"value": 1}, document); self.assertEqual('"v1"', etag); self.assertEqual(0, server_time.hour)
        google = types.ModuleType("googleapiclient"); http = types.ModuleType("googleapiclient.http"); http.MediaIoBaseUpload = Media
        with patch.dict(sys.modules, {"googleapiclient": google, "googleapiclient.http": http}):
            store.update_versioned_json("registry-id", etag, {"value": 2})
            self.assertEqual('"v1"', files.last_headers["If-Match"])
            with self.assertRaises(DriveConflict): store.update_versioned_json("registry-id", '"stale"', {"value": 3})

    def test_missing_etag_or_server_date_fails_closed(self):
        for headers in ({"date": "Wed, 12 Aug 2026 00:00:00 GMT"}, {"etag": '"v1"'}):
            class Files:
                def get_media(self, fileId): return HeaderRequest(headers, b'{}')
            with self.assertRaisesRegex(TaskError, "ETag or server Date"):
                DriveRecords(Service(Files())).read_versioned_json("registry-id")

    def test_duplicate_registry_record_fails_closed(self):
        store = DriveRecords(Service(object()))
        store.project_folder = lambda *_args: "parent"
        store.children = lambda *_args: [{"id": "one", "mimeType": "application/json"}, {"id": "two", "mimeType": "application/json"}]
        with self.assertRaisesRegex(TaskError, "duplicate Drive record"):
            store.provision_versioned_json("worktree_locks", "_global", "registry", {"schema_version": "0.2.0", "locks": {}})


if __name__ == "__main__": unittest.main()
