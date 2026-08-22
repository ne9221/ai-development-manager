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


class ReconcileFiles:
    """Simulates a project's Commands folder for createdTime-ordered reconciliation.

    ``entries`` is an ordered list of (created_time, file_id, body_bytes),
    already sorted oldest-first, standing in for real Drive files each with
    an immutable createdTime. Folder-name-resolution queries (no createdTime
    filter, no fullText) resolve to a fixed synthetic folder id.
    """
    def __init__(self, entries):
        self.entries = entries; self.calls = []
    def list(self, **options):
        self.calls.append(options)
        query = options["q"]
        if "createdTime >" not in query and "fullText contains" not in query and "mimeType=" not in query:
            return Request({"files": [{"id": "commands", "name": "COMMANDS", "mimeType": "application/vnd.google-apps.folder"}]})
        after = None
        if "createdTime >" in query:
            after = query.split("createdTime > '")[1].split("'")[0]
        candidates = [entry for entry in self.entries if after is None or entry[0] > after]
        page = candidates[:options["pageSize"]]
        return Request({"files": [{"id": file_id, "name": f"{file_id}.json", "mimeType": "application/json", "createdTime": created}
                                   for created, file_id, _ in page]})
    def get_media(self, fileId):
        for created, file_id, body in self.entries:
            if file_id == fileId:
                return Request(body)
        raise KeyError(fileId)


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

    def test_actionable_commands_ordered_oldest_first_not_most_recently_modified(self):
        """orderBy must be createdTime asc, not modifiedTime desc: under a
        persistent backlog, modifiedTime desc lets records with a fresher
        write (e.g. an attention->running recovery write) perpetually sort
        ahead of an old untouched queued/claimed/running record, starving it
        out of the bounded page. createdTime is immutable across a command's
        whole lifecycle (files are updated in place, never recreated), so
        sorting by it ascending guarantees the oldest waiter is always first
        until it actually reaches a terminal status."""
        files = CommandFiles({"queued": b'{"command_id":"queued", "status":"queued"}'})
        DriveRecords(Service(files)).list_actionable_commands("project-a", 4)
        query = next(call for call in files.calls if "fullText contains" in call["q"])
        self.assertEqual("createdTime asc", query["orderBy"])

    def test_reconcile_walks_forward_bounded_and_independent_of_fulltext(self):
        """The reconcile backstop must never download more than `limit`
        bodies in one call even with 1000 historical records ahead of the
        cursor, and must find a record fullText missed entirely (simulated
        here by a CommandFiles-style store where the fast path never
        returns it) purely from createdTime metadata."""
        entries = [(f"2026-08-{index:02d}T00:00:00Z", f"historic-{index}", b'{"command_id":"historic-%d","status":"completed"}' % index)
                   for index in range(1, 21)]
        entries.append(("2026-08-22T00:00:00Z", "missed-queued", b'{"command_id":"missed-queued","status":"queued"}'))
        files = ReconcileFiles(entries)
        store = DriveRecords(Service(files))
        batch = store.reconcile_actionable_commands("project-a", None, 4)
        self.assertEqual(4, len(batch["records"]))
        self.assertEqual(["historic-1", "historic-2", "historic-3", "historic-4"],
                          [r["command_id"] for r in batch["records"]])
        self.assertEqual("2026-08-04T00:00:00Z", batch["next_cursor"])
        reconcile_calls = [call for call in files.calls if "mimeType=" in call["q"]]
        self.assertEqual(1, len(reconcile_calls))
        self.assertEqual(4, reconcile_calls[0]["pageSize"])

    def test_reconcile_resumes_from_cursor_and_finds_record_fulltext_never_matched(self):
        entries = [(f"2026-08-{index:02d}T00:00:00Z", f"historic-{index}", b'{"command_id":"historic-%d","status":"completed"}' % index)
                   for index in range(1, 5)]
        entries.append(("2026-08-22T00:00:00Z", "missed-queued", b'{"command_id":"missed-queued","status":"queued"}'))
        files = ReconcileFiles(entries)
        store = DriveRecords(Service(files))
        first = store.reconcile_actionable_commands("project-a", None, 4)
        second = store.reconcile_actionable_commands("project-a", first["next_cursor"], 4)
        self.assertEqual(["missed-queued"], [r["command_id"] for r in second["records"]])
        self.assertEqual("2026-08-22T00:00:00Z", second["next_cursor"])

    def test_reconcile_empty_page_keeps_cursor_unchanged(self):
        files = ReconcileFiles([])
        batch = DriveRecords(Service(files)).reconcile_actionable_commands("project-a", "2026-08-01T00:00:00Z", 4)
        self.assertEqual([], batch["records"])
        self.assertEqual("2026-08-01T00:00:00Z", batch["next_cursor"])

if __name__ == "__main__": unittest.main()
