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


class CommandFiles:
    def __init__(self, commands): self.commands = commands; self.calls = []
    def list(self, **options):
        self.calls.append(options)
        if "fullText contains" in options["q"]:
            return Request({"files": [{"id": "queued", "name": "queued.json", "mimeType": "application/json"}]})
        return Request({"files": [{"id": "commands", "name": "COMMANDS", "mimeType": "application/vnd.google-apps.folder"}]})
    def get_media(self, fileId): return Request(self.commands[fileId])


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

    def test_actionable_commands_use_one_bounded_server_side_listing(self):
        commands = {f"historic-{index}": b'{"status":"completed"}' for index in range(1000)}
        commands.update({"queued": b'{"command_id":"queued", "status":"queued"}'})
        files = CommandFiles(commands)
        records = DriveRecords(Service(files)).list_actionable_commands("project-a", 4)
        self.assertEqual(["queued"], [record["command_id"] for record in records])
        query = next(call for call in files.calls if "fullText contains" in call["q"])
        self.assertEqual(4, query["pageSize"])
        self.assertIn("fullText contains", query["q"])
        self.assertEqual(1, sum("fullText contains" in call["q"] for call in files.calls))

if __name__ == "__main__": unittest.main()
