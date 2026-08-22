import unittest

from manager.tasks import ROOT_FOLDER_ID, DriveRecords, TaskError


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class PagedFiles:
    def __init__(self, responses): self.responses = responses; self.calls = []
    def list(self, **options):
        self.calls.append(options)
        return Request(self.responses[options.get("pageToken")])


class CommandFiles:
    """Fake Drive files() resource for a single pre-existing COMMANDS folder.

    Any generic list() (folder resolution, children()) resolves to the one
    COMMANDS folder; a "properties has"/"not properties has" query resolves
    to whatever `actionable`/`legacy` list holds, so tests can drive
    list_actionable_commands()/reconcile_legacy_command_properties() without
    re-implementing Drive's folder-creation/reconciliation flow.
    """
    def __init__(self, commands, actionable=None, legacy=None):
        self.commands = commands
        self.actionable = actionable if actionable is not None else [{"id": "queued", "name": "queued.json", "mimeType": "application/json"}]
        self.legacy = legacy if legacy is not None else []
        self.calls = []
        self.created_bodies = []
        self.updated_bodies = []

    def list(self, **options):
        self.calls.append(options)
        query = options["q"]
        if "not properties has" in query:
            return Request({"files": self.legacy})
        if "properties has" in query:
            return Request({"files": self.actionable})
        return Request({"files": [{"id": "commands", "name": "COMMANDS", "mimeType": "application/vnd.google-apps.folder"}]})

    def get_media(self, fileId):
        return Request(self.commands[fileId])

    def create(self, **options):
        self.created_bodies.append(options["body"])
        file_id = options["body"]["name"].removesuffix(".json") or "created"
        return Request({"id": file_id})

    def update(self, **options):
        self.updated_bodies.append(options["body"])
        return Request({"id": options["fileId"]})


class Service:
    def __init__(self, files): self._files = files
    def files(self): return self._files


import re as _re


class MiniDrive:
    """Small in-memory Drive files() double: enough real query semantics
    (parents, name=, properties has / not properties has) to exercise put()
    and list_actionable_commands()/reconcile_legacy_command_properties()
    end-to-end without a live Drive connection."""

    def __init__(self):
        self._next_id = 0
        self.items = {}  # id -> {name, mimeType, parents, properties, content, createdTime}

    def _new_id(self, prefix="f"):
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    def seed_folder(self, folder_id, name, parent=None):
        self.items[folder_id] = {"name": name, "mimeType": "application/vnd.google-apps.folder",
                                  "parents": [parent] if parent else [], "properties": {}, "content": b"",
                                  "createdTime": self._next_id}
        self._next_id += 1
        return folder_id

    def list(self, **options):
        query = options.get("q", "")
        parent_match = _re.search(r"'([^']+)' in parents", query)
        parent = parent_match.group(1) if parent_match else None
        name_match = _re.search(r"name='([^']*)'", query)
        name = name_match.group(1) if name_match else None
        prop_values = set(_re.findall(r"key='status' and value='([^']+)'", query))
        negate_props = "not properties has" in query

        results = []
        for item_id, item in self.items.items():
            if parent is not None and parent not in item.get("parents", []):
                continue
            if name is not None and item.get("name") != name:
                continue
            if prop_values and not negate_props:
                if item.get("properties", {}).get("status") not in prop_values:
                    continue
            if negate_props:
                if "status" in item.get("properties", {}):
                    continue
            results.append(item_id)

        order_by = options.get("orderBy")
        if order_by == "createdTime asc":
            results.sort(key=lambda i: self.items[i]["createdTime"])
        elif order_by == "createdTime desc":
            results.sort(key=lambda i: -self.items[i]["createdTime"])

        offset = int(options["pageToken"]) if options.get("pageToken") else 0
        page_size = options.get("pageSize")
        page = results[offset:offset + page_size] if page_size else results[offset:]
        next_token = str(offset + page_size) if page_size and offset + page_size < len(results) else None

        files = [{"id": item_id, "name": self.items[item_id]["name"], "mimeType": self.items[item_id]["mimeType"]}
                 for item_id in page]
        response = {"files": files}
        if next_token:
            response["nextPageToken"] = next_token
        return Request(response)

    @staticmethod
    def _media_bytes(media_body):
        if media_body is None:
            return b""
        return media_body.getbytes(0, media_body.size())

    def create(self, **options):
        body = options["body"]
        item_id = self._new_id()
        self.items[item_id] = {"name": body["name"], "mimeType": body.get("mimeType", "application/json"),
                                "parents": body.get("parents", []), "properties": dict(body.get("properties", {})),
                                "content": self._media_bytes(options.get("media_body")), "createdTime": self._next_id}
        self._next_id += 1
        return Request({"id": item_id})

    def update(self, **options):
        item = self.items[options["fileId"]]
        body = options.get("body") or {}
        if "properties" in body:
            item["properties"] = dict(body["properties"])
        content = self._media_bytes(options.get("media_body"))
        if content:
            item["content"] = content
        return Request({"id": options["fileId"]})

    def get_media(self, fileId):
        return Request(self.items[fileId]["content"])


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
        query = next(call for call in files.calls if "properties has" in call["q"])
        self.assertEqual(4, query["pageSize"])
        self.assertEqual("createdTime asc", query["orderBy"])
        self.assertNotIn("fullText", query["q"])
        self.assertEqual(1, sum("properties has" in call["q"] for call in files.calls))

    def test_actionable_commands_query_is_exact_property_match_not_fulltext(self):
        """Regression guard for the false-positive fullText finding: a
        *completed* command whose free-text fields happen to contain the
        words "status queued" must never be treated as actionable. The
        query itself must ask Drive for an exact properties.status match, so
        this class of false positive cannot occur server-side."""
        files = CommandFiles({"queued": b'{"command_id":"queued", "status":"queued"}'})
        DriveRecords(Service(files)).list_actionable_commands("project-a", 4)
        query = next(call for call in files.calls if "properties has" in call["q"])
        self.assertIn("key='status' and value='queued'", query["q"])
        self.assertIn("key='status' and value='claimed'", query["q"])
        self.assertIn("key='status' and value='running'", query["q"])

    def test_put_stamps_command_status_as_drive_property(self):
        drive = MiniDrive()
        DriveRecords(Service(drive)).put("commands", "project-a", "cmd-1", {"status": "queued"})
        stamped = [item for item in drive.items.values() if item["name"] == "cmd-1.json"]
        self.assertEqual(1, len(stamped))
        self.assertEqual({"status": "queued"}, stamped[0]["properties"])

    def test_put_restamps_property_when_status_changes(self):
        drive = MiniDrive()
        records = DriveRecords(Service(drive))
        records.put("commands", "project-a", "cmd-1", {"status": "queued"})
        records.put("commands", "project-a", "cmd-1", {"status": "completed"})
        stamped = [item for item in drive.items.values() if item["name"] == "cmd-1.json"]
        self.assertEqual(1, len(stamped))
        self.assertEqual({"status": "completed"}, stamped[0]["properties"])

    def test_legacy_command_missing_property_is_backfilled_bounded(self):
        drive = MiniDrive()
        commands_folder = drive.seed_folder("commands-folder", "COMMANDS", parent=ROOT_FOLDER_ID)
        project_folder = drive.seed_folder("project-folder", "project-a", parent="commands-folder")
        drive.items["legacy-1"] = {"name": "legacy-1.json", "mimeType": "application/json",
                                    "parents": [project_folder], "properties": {},
                                    "content": b'{"status": "queued"}\n', "createdTime": 1}

        migrated = DriveRecords(Service(drive)).reconcile_legacy_command_properties("project-a")
        self.assertEqual(1, migrated)
        self.assertEqual({"status": "queued"}, drive.items["legacy-1"]["properties"])

    def test_legacy_backfill_is_bounded_even_with_unbounded_history(self):
        """Hard page cap holds regardless of how much legacy history exists --
        this can never degrade into an unbounded per-poll scan."""
        drive = MiniDrive()
        drive.seed_folder("commands-folder", "COMMANDS", parent=ROOT_FOLDER_ID)
        project_folder = drive.seed_folder("project-folder", "project-a", parent="commands-folder")
        for index in range(10000):
            drive.items[f"legacy-{index}"] = {"name": f"legacy-{index}.json", "mimeType": "application/json",
                                               "parents": [project_folder], "properties": {},
                                               "content": b'{"status": "queued"}\n', "createdTime": index}

        migrated = DriveRecords(Service(drive)).reconcile_legacy_command_properties("project-a", page_size=50, max_pages=5)
        self.assertEqual(250, migrated)  # 5 pages * 50 -- never scans all 10000

if __name__ == "__main__": unittest.main()
