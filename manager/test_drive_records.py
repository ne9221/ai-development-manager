import unittest

from manager.tasks import DriveRecords, TaskError


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

if __name__ == "__main__": unittest.main()
