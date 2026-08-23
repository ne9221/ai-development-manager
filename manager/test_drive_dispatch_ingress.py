import ast
import inspect
import json
import re
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import manager.drive_dispatch_ingress as drive_dispatch_ingress
from manager.drive_dispatch_ingress import (
    DEFAULT_MAX_METADATA_PAGES, FOLDER_NAME, METADATA_FIELDS,
    _fairness_slice_bounds, _modified_time_indicates_stale, _request_files,
    poll_drive_dispatch_requests, read_request, verify_ingress_folder,
)
from manager.tasks import MIME_FOLDER, MIME_JSON, TaskError


OWNER = "owner@example.com"
FOLDER_ID = "ingress-folder"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def private_owner():
    return {
        "owners": [{"emailAddress": OWNER, "permissionId": "owner-permission", "me": True}],
        "permissions": [{"id": "owner-permission", "emailAddress": OWNER, "type": "user", "role": "owner"}],
        "ownedByMe": True,
    }


def request(**changes):
    value = {
        "request_id": "drive-e2e-1", "project_id": "ai-development-manager",
        "title": "Harmless ingress proof", "goal": "Return a short status report without changing files.",
        "preferred_provider": "codex", "priority": "normal", "created_at": "2026-08-20T11:59:00Z",
    }
    value.update(changes)
    return value


class Call:
    def __init__(self, value): self.value = value
    def execute(self): return deepcopy(self.value) if not isinstance(self.value, bytes) else self.value


class Files:
    def __init__(self, document=None):
        self.document = request() if document is None else document
        raw = self.document if isinstance(self.document, bytes) else (json.dumps(self.document) + "\n").encode()
        self.raw = raw
        self.folder = {
            "id": FOLDER_ID, "name": FOLDER_NAME, "mimeType": MIME_FOLDER, "trashed": False,
            "parents": ["adm-root"], "driveId": None, **private_owner(),
        }
        self.file = {
            "id": "request-file", "name": "drive-e2e-1.json", "mimeType": MIME_JSON,
            "trashed": False, "parents": [FOLDER_ID], "size": str(len(raw)), "driveId": None,
            **private_owner(),
        }

    def get(self, fileId, fields): return Call(self.folder if fileId == FOLDER_ID else self.file)
    def list(self, **_kwargs): return Call({"files": [self.file]})
    def get_media(self, fileId): return Call(self.raw)


class About:
    def get(self, fields): return Call({"user": {"emailAddress": OWNER, "permissionId": "owner-permission"}})


class Service:
    def __init__(self, document=None): self._files = Files(document)
    def files(self): return self._files
    def about(self): return About()


class PagedFiles:
    def __init__(self, pages): self.pages, self.calls = pages, []
    def list(self, **kwargs):
        self.calls.append(kwargs)
        return Call(self.pages.get(kwargs.get("pageToken")))


class PagedService:
    def __init__(self, pages): self._files = PagedFiles(pages)
    def files(self): return self._files


class DriveDispatchIngressTests(unittest.TestCase):
    def test_request_files_single_page(self):
        service = PagedService({None: {"files": [{"id": "one"}]}})
        self.assertEqual([{"id": "one"}], _request_files(service, FOLDER_ID))
        self.assertNotIn("pageToken", service._files.calls[0])

    def test_request_files_exactly_100_stays_single_page(self):
        page = [{"id": str(index)} for index in range(100)]
        service = PagedService({None: {"files": page}})
        self.assertEqual(page, _request_files(service, FOLDER_ID))
        self.assertEqual(1, len(service._files.calls))

    def test_request_files_101_uses_second_page(self):
        first = [{"id": str(index)} for index in range(100)]
        service = PagedService({
            None: {"files": first, "nextPageToken": "page-2"},
            "page-2": {"files": [{"id": "100"}]},
        })
        self.assertEqual(first + [{"id": "100"}], _request_files(service, FOLDER_ID))
        self.assertEqual("page-2", service._files.calls[1]["pageToken"])

    def test_request_files_multiple_pages_keep_metadata_fields(self):
        service = PagedService({
            None: {"files": [{"id": "one"}], "nextPageToken": "page-2"},
            "page-2": {"files": [{"id": "two"}], "nextPageToken": "page-3"},
            "page-3": {"files": [{"id": "three"}]},
        })
        self.assertEqual([{"id": "one"}, {"id": "two"}, {"id": "three"}],
                         _request_files(service, FOLDER_ID))
        self.assertEqual([None, "page-2", "page-3"],
                         [call.get("pageToken") for call in service._files.calls])
        self.assertTrue(all(call["fields"] == f"nextPageToken,files({METADATA_FIELDS})"
                            and call["pageSize"] == 100 for call in service._files.calls))

    def test_request_files_malformed_page_or_token_fails_closed(self):
        malformed = [None, {}, {"files": "bad"}, {"files": [], "nextPageToken": ""},
                     {"files": [], "nextPageToken": 2}]
        for page in malformed:
            with self.subTest(page=page), self.assertRaises(TaskError):
                _request_files(PagedService({None: page}), FOLDER_ID)
        with self.assertRaises(TaskError):
            _request_files(PagedService({
                None: {"files": [], "nextPageToken": "page-2"}, "page-2": None,
            }), FOLDER_ID)

    def test_request_files_repeated_token_fails_closed(self):
        service = PagedService({
            None: {"files": [], "nextPageToken": "repeat"},
            "repeat": {"files": [], "nextPageToken": "repeat"},
        })
        with self.assertRaises(TaskError):
            _request_files(service, FOLDER_ID)
        self.assertEqual(2, len(service._files.calls))

    def test_valid_private_request_maps_only_allowed_fields(self):
        service = Service()
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1", "task_id": "dispatch-drive-e2e-1",
                                     "command_id": "dispatch-drive-e2e-1", "status": "queued"})
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            result = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                  registry_factory=lambda *_args: object())
        self.assertTrue(result[0]["accepted"])
        payload = handler.call_args.args[3]
        self.assertEqual("codex", payload["provider"])
        self.assertEqual({"read_only": True}, payload["constraints"])
        self.assertNotIn("created_at", payload)

    def test_supported_providers_and_null_validate(self):
        for provider in ("codex", "claude", "antigravity", None):
            with self.subTest(provider=provider):
                service = Service(request(preferred_provider=provider))
                self.assertEqual(provider, read_request(
                    service, FOLDER_ID, OWNER, service._files.file, NOW,
                )["preferred_provider"])

    def test_account_id_schema_accepts_known_logical_ids_and_omission(self):
        for account_id in ("account-a", "account-b"):
            with self.subTest(account_id=account_id):
                service = Service(request(preferred_provider="claude", account_id=account_id))
                self.assertEqual(account_id, read_request(
                    service, FOLDER_ID, OWNER, service._files.file, NOW,
                )["account_id"])
        service = Service(request(preferred_provider="claude"))
        self.assertNotIn("account_id", read_request(
            service, FOLDER_ID, OWNER, service._files.file, NOW,
        ))

    def test_malformed_account_id_types_reject_before_dispatch(self):
        for account_id in (False, 1, [], {}):
            with self.subTest(account_id=account_id):
                service = Service(request(preferred_provider="claude", account_id=account_id))
                handler = Mock()
                with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
                    result = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)
                self.assertFalse(result[0]["accepted"])
                handler.assert_not_called()

    def test_claude_account_id_reaches_existing_trusted_ingress_contract(self):
        service = Service(request(preferred_provider="claude", account_id="account-a"))
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1",
                                     "task_id": "dispatch-drive-e2e-1",
                                     "command_id": "dispatch-drive-e2e-1", "status": "queued"})
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            result = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                  registry_factory=lambda *_args: object())
        self.assertTrue(result[0]["accepted"])
        payload = handler.call_args.args[3]
        self.assertEqual("claude", payload["provider"])
        self.assertEqual("account-a", payload["account_id"])
        self.assertEqual({"read_only": True}, payload["constraints"])

    def test_antigravity_request_reaches_trusted_read_only_ingress(self):
        service = Service(request(preferred_provider="antigravity"))
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1",
                                     "task_id": "dispatch-drive-e2e-1",
                                     "command_id": "dispatch-drive-e2e-1", "status": "queued"})
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            result = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                  registry_factory=lambda *_args: object())
        self.assertTrue(result[0]["accepted"])
        self.assertEqual("antigravity", handler.call_args.args[3]["provider"])
        self.assertEqual({"read_only": True}, handler.call_args.args[3]["constraints"])

    def test_missing_priority_defaults_to_normal(self):
        document = {k: v for k, v in request().items() if k != "priority"}
        service = Service(document)
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1", "task_id": "dispatch-drive-e2e-1",
                                     "command_id": "dispatch-drive-e2e-1", "status": "queued"})
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            result = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                  registry_factory=lambda *_args: object())
        self.assertTrue(result[0]["accepted"])
        payload = handler.call_args.args[3]
        self.assertEqual("normal", payload["priority"])

    def test_governance_and_execution_authority_from_caller_are_rejected(self):
        for field in ("governance", "execution_policies", "created_via", "status", "command_id"):
            service = Service(request(**{field: "caller-value"}))
            service._files.file["size"] = str(len(service._files.raw))
            self.assertFalse(poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)[0]["accepted"], field)

    def test_malformed_missing_id_wrong_provider_and_stale_never_dispatch(self):
        cases = [b"{broken", {k: v for k, v in request().items() if k != "request_id"},
                 request(preferred_provider="gemini"), request(created_at="2026-08-18T00:00:00Z")]
        for document in cases:
            service = Service(document)
            handler = Mock()
            with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
                result = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)
            self.assertFalse(result[0]["accepted"])
            handler.assert_not_called()

    def test_wrong_folder_owner_oauth_or_shared_permissions_fail_closed(self):
        mutations = [
            lambda s: s._files.folder.update(name="OTHER"),
            lambda s: s._files.folder.update(owners=[]),
            lambda s: s._files.folder["permissions"].append({"type": "user", "role": "reader", "emailAddress": "other@example.com"}),
        ]
        for mutate in mutations:
            service = Service(); mutate(service)
            with self.assertRaises(TaskError): verify_ingress_folder(service, FOLDER_ID, OWNER)
        with self.assertRaises(TaskError): verify_ingress_folder(Service(), FOLDER_ID, "other@example.com")

    def test_file_parent_owner_size_filename_and_timestamp_are_verified(self):
        mutations = [
            lambda f: f.update(parents=["wrong"]),
            lambda f: f.update(owners=[]),
            lambda f: f.update(size="unknown"),
            lambda f: f.update(name="other.json"),
        ]
        for mutate in mutations:
            service = Service(); mutate(service._files.file)
            with self.assertRaises(TaskError):
                read_request(service, FOLDER_ID, OWNER, service._files.file, NOW)

    def test_duplicate_request_is_delegated_to_existing_gcs_idempotency(self):
        service = Service()
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1", "task_id": "dispatch-drive-e2e-1",
                                     "command_id": "dispatch-drive-e2e-1", "status": "completed"})
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            first = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)
            second = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)
        self.assertEqual(first, second)
        self.assertEqual(2, handler.call_count)


def make_item(file_id, request_id, created_time, created_at=None, extra=None, modified_time=None):
    """One (metadata, raw_bytes) DISPATCH-REQUESTS candidate. `created_time`
    is Drive's own file metadata createdTime (a datetime); `created_at`
    defaults to the same instant for the request *body* unless overridden
    -- letting tests deliberately diverge metadata createdTime from the
    body's created_at to prove the metadata check is optimization-only.
    `modified_time` is Drive's own file metadata modifiedTime and defaults
    to `created_time` unless overridden -- letting tests deliberately
    diverge it from createdTime to prove the metadata-only skip decision is
    driven by modifiedTime, not createdTime."""
    document = request(request_id=request_id,
                       created_at=(created_at or created_time).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if extra:
        document.update(extra)
    raw = (json.dumps(document) + "\n").encode()
    metadata = {
        "id": file_id, "name": f"{request_id}.json", "mimeType": MIME_JSON, "trashed": False,
        "parents": [FOLDER_ID], "size": str(len(raw)), "driveId": None,
        "createdTime": created_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modifiedTime": (modified_time or created_time).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **private_owner(),
    }
    return metadata, raw


_CREATED_TIME_RANGE_RE = re.compile(r"createdTime > '([^']+)' and createdTime < '([^']+)'")


class MultiFiles:
    """A folder plus an explicit, caller-ordered list of candidate files --
    list() returns them in exactly the order given (as a real Drive
    `orderBy="createdTime desc"` listing would for pre-sorted fixtures),
    recording every list()/get_media() call so tests can assert on
    ordering, pagination bounds, and which files were ever downloaded. A
    `q` carrying a `createdTime > '...' and createdTime < '...'` range (as
    `_fairness_slice_metadata` builds) is honored by filtering to that
    range, exactly like the real Drive API would -- letting the fairness-
    slice query's own targeting be exercised realistically even in tests
    that otherwise don't care about it."""

    def __init__(self, items=None, folder=None):
        self.items = items or []
        self.folder = folder or {
            "id": FOLDER_ID, "name": FOLDER_NAME, "mimeType": MIME_FOLDER, "trashed": False,
            "parents": ["adm-root"], "driveId": None, **private_owner(),
        }
        self.list_calls = []
        self.get_media_calls = []

    def get(self, fileId, fields):
        if fileId == FOLDER_ID:
            return Call(self.folder)
        for metadata, _raw in self.items:
            if metadata["id"] == fileId:
                return Call(metadata)
        raise AssertionError(f"unknown fileId {fileId}")

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        candidates = [metadata for metadata, _raw in self.items]
        match = _CREATED_TIME_RANGE_RE.search(kwargs.get("q", ""))
        if match:
            start, end = match.groups()
            candidates = [m for m in candidates if start < m.get("createdTime", "") < end]
            candidates = candidates[:kwargs.get("pageSize", 100)]
        return Call({"files": candidates})

    def get_media(self, fileId):
        self.get_media_calls.append(fileId)
        for metadata, raw in self.items:
            if metadata["id"] == fileId:
                return Call(raw)
        raise AssertionError(f"unknown fileId {fileId}")


class MultiService:
    def __init__(self, items=None, folder=None):
        self._files = MultiFiles(items=items, folder=folder)

    def files(self): return self._files
    def about(self): return About()


class DeepFolderFiles:
    """A folder plus a large, realistically PAGINATED and QUERY-AWARE list
    of candidate files, standing in for real Drive behavior in ways
    `MultiFiles` (which returns everything in one unpaginated, query-blind
    shot) deliberately does not: `items` is provided already sorted newest
    first, exactly like a real `orderBy="createdTime desc"` listing would
    return; the plain newest-first `list()` call is paginated by
    `pageSize`/`pageToken` exactly like the real API; and a `list()` call
    whose `q` carries a `createdTime > '...' and createdTime < '...'`
    range (as `_fairness_slice_metadata` builds) is answered by filtering
    server-side on that range instead of by page position -- letting a
    test prove a deep item unreachable via pagination is reachable via the
    range query."""

    _RANGE_RE = re.compile(r"createdTime > '([^']+)' and createdTime < '([^']+)'")

    def __init__(self, items, folder=None):
        self.items = items
        self.folder = folder or {
            "id": FOLDER_ID, "name": FOLDER_NAME, "mimeType": MIME_FOLDER, "trashed": False,
            "parents": ["adm-root"], "driveId": None, **private_owner(),
        }
        self.list_calls = []
        self.get_media_calls = []

    def get(self, fileId, fields):
        if fileId == FOLDER_ID:
            return Call(self.folder)
        for metadata, _raw in self.items:
            if metadata["id"] == fileId:
                return Call(metadata)
        raise AssertionError(f"unknown fileId {fileId}")

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        candidates = [metadata for metadata, _raw in self.items]
        match = self._RANGE_RE.search(kwargs.get("q", ""))
        if match:
            start, end = match.groups()
            filtered = [m for m in candidates if start < m["createdTime"] < end]
            page_size = kwargs.get("pageSize", 100)
            return Call({"files": filtered[:page_size]})
        page_size = kwargs.get("pageSize", 100)
        page_token = kwargs.get("pageToken")
        start_index = int(page_token) if page_token else 0
        page = candidates[start_index:start_index + page_size]
        response = {"files": page}
        next_index = start_index + page_size
        if next_index < len(candidates):
            response["nextPageToken"] = str(next_index)
        return Call(response)

    def get_media(self, fileId):
        self.get_media_calls.append(fileId)
        for metadata, raw in self.items:
            if metadata["id"] == fileId:
                return Call(raw)
        raise AssertionError(f"unknown fileId {fileId}")


class DeepFolderService:
    def __init__(self, items, folder=None):
        self._files = DeepFolderFiles(items, folder)

    def files(self): return self._files
    def about(self): return About()


class DriveDispatchIngressBoundedTests(unittest.TestCase):
    def _accepting_handler(self):
        calls = []

        def handler(_store, _service, _registry_factory, payload):
            calls.append(payload["request_id"])
            return {"accepted": True, "request_id": payload["request_id"],
                    "task_id": f"dispatch-{payload['request_id']}",
                    "command_id": f"dispatch-{payload['request_id']}", "status": "queued"}
        return handler, calls

    def test_recent_first_evaluated_before_history(self):
        items = [make_item(f"file-{i}", f"req-{i}", NOW - timedelta(minutes=i)) for i in range(3)]
        service = MultiService(items)
        handler, order = self._accepting_handler()
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                    registry_factory=lambda *_a: object())
        self.assertEqual(["req-0", "req-1", "req-2"], order)
        self.assertTrue(all(r["accepted"] for r in results))
        self.assertIn("orderBy", service._files.list_calls[0])
        self.assertEqual("createdTime desc", service._files.list_calls[0]["orderBy"])

    def test_stale_metadata_skips_get_media(self):
        stale = make_item("stale-file", "stale-req", NOW - timedelta(days=3))
        fresh = make_item("fresh-file", "fresh-req", NOW - timedelta(minutes=1))
        service = MultiService([stale, fresh])
        handler, order = self._accepting_handler()
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                    registry_factory=lambda *_a: object())
        self.assertNotIn("stale-file", service._files.get_media_calls)
        self.assertIn("fresh-file", service._files.get_media_calls)
        self.assertEqual(["fresh-req"], order)
        self.assertFalse(any(r.get("file_id") == "stale-file" for r in results))

    def test_metadata_optimism_never_overrides_authoritative_body_check(self):
        # Drive metadata createdTime looks fresh (never skipped by the
        # optimization) but the request BODY's created_at is genuinely
        # stale -- read_request()'s own authoritative check must still
        # reject it.
        metadata, raw = make_item("file-1", "req-1", created_time=NOW,
                                  created_at=NOW - timedelta(days=3))
        service = MultiService([(metadata, raw)])
        handler = Mock()
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)
        self.assertIn("file-1", service._files.get_media_calls)
        handler.assert_not_called()
        self.assertFalse(results[0]["accepted"])

    def test_max_candidates_bound_enforced(self):
        items = [make_item(f"file-{i}", f"req-{i}", NOW - timedelta(minutes=i)) for i in range(10)]
        service = MultiService(items)
        handler, order = self._accepting_handler()
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                         registry_factory=lambda *_a: object(),
                                         max_candidates=3, recent_candidates=3)
        self.assertEqual(3, len(service._files.get_media_calls))
        self.assertEqual(3, len(order))

    def test_deadline_stops_new_reads(self):
        items = [make_item(f"file-{i}", f"req-{i}", NOW - timedelta(minutes=i)) for i in range(5)]
        service = MultiService(items)
        expired_deadline = time.monotonic() - 1
        results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                               deadline=expired_deadline)
        self.assertEqual([], results)
        self.assertEqual([], service._files.get_media_calls)

    def test_malformed_newest_request_does_not_block_next_valid_request(self):
        bad, bad_raw = make_item("file-bad", "req-bad", NOW - timedelta(minutes=0),
                                 extra={"preferred_provider": "claude", "account_id": []})
        good, good_raw = make_item("file-good", "req-good", NOW - timedelta(minutes=1))
        service = MultiService([(bad, bad_raw), (good, good_raw)])
        handler, order = self._accepting_handler()
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                    registry_factory=lambda *_a: object())
        self.assertEqual(["req-good"], order)
        by_id = {r.get("file_id"): r for r in results}
        self.assertFalse(by_id["file-bad"]["accepted"])
        self.assertTrue(by_id["file-good"]["accepted"])

    def test_request_files_max_pages_bounds_pagination(self):
        service = PagedService({
            None: {"files": [{"id": "one"}], "nextPageToken": "page-2"},
            "page-2": {"files": [{"id": "two"}], "nextPageToken": "page-3"},
            "page-3": {"files": [{"id": "three"}]},
        })
        result = _request_files(service, FOLDER_ID, max_pages=2)
        self.assertEqual([{"id": "one"}, {"id": "two"}], result)
        self.assertEqual(2, len(service._files.calls))

    def test_request_files_deadline_bounds_pagination(self):
        service = PagedService({
            None: {"files": [{"id": "one"}], "nextPageToken": "page-2"},
            "page-2": {"files": [{"id": "two"}], "nextPageToken": "page-3"},
            "page-3": {"files": [{"id": "three"}]},
        })
        result = _request_files(service, FOLDER_ID, deadline=time.monotonic() - 1)
        self.assertEqual([], result)
        self.assertEqual(0, len(service._files.calls))

    def test_fairness_rotation_eventually_services_older_valid_request(self):
        # recent_candidates=2 keeps only the two newest in the "recent"
        # window every tick; 4 more still-valid (<24h) requests sit just
        # behind it and would be starved forever by continuous new
        # arrivals without the rotating fairness pass.
        older_valid = [make_item(f"older-{i}", f"older-req-{i}", NOW - timedelta(hours=i + 1))
                       for i in range(4)]
        handler, _ = self._accepting_handler()
        serviced = set()
        for minute_offset in range(6):
            tick_now = NOW + timedelta(minutes=minute_offset)
            newest = [make_item(f"new-{minute_offset}-{i}", f"new-req-{minute_offset}-{i}",
                                tick_now - timedelta(seconds=i)) for i in range(2)]
            service = MultiService(newest + older_valid)
            with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
                poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, tick_now,
                                             registry_factory=lambda *_a: object(),
                                             recent_candidates=2, max_candidates=3)
            serviced.update(fid for fid in service._files.get_media_calls if fid.startswith("older-"))
        self.assertEqual({"older-0", "older-1", "older-2", "older-3"}, serviced)

    def test_fairness_slice_query_reaches_request_beyond_metadata_page_bound(self):
        # ISSUE 2 regression: a still-valid (<24h) request sitting deeper
        # than the bounded newest-first metadata listing
        # (DEFAULT_MAX_METADATA_PAGES * 100 == 300 entries) can never be
        # covered by the recent-first + in-listing-tail fairness passes
        # alone -- it never even appears in that listing. Prove the new
        # deep-slice fairness query actually reaches it regardless.
        tick_now = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(3, DEFAULT_MAX_METADATA_PAGES)  # this test's math assumes the real bound
        slice_start, slice_end = _fairness_slice_bounds(tick_now)
        # This is the OLDEST hour of the 24h window at this tick_now (see
        # _fairness_slice_bounds: at an exact-midnight tick_now, slice
        # index 0 lands at window_start), far from the 305 recent filler
        # items below -- so there is no accidental overlap.
        target_time = slice_start + timedelta(seconds=1800)
        filler = [make_item(f"filler-{i}", f"filler-req-{i}", tick_now - timedelta(seconds=i + 1))
                 for i in range(300 + 5)]  # 305 > DEFAULT_MAX_METADATA_PAGES * 100 (300)
        target = make_item("deep-file", "deep-req", target_time)
        items = filler + [target]  # already newest-first: filler, then the oldest (target), last
        service = DeepFolderService(items)
        handler, order = self._accepting_handler()

        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, tick_now,
                                                    registry_factory=lambda *_a: object(),
                                                    max_candidates=12, recent_candidates=8)

        # The bounded newest-first listing (3 pages of 100) never reaches
        # index 305 -- confirm the fixture actually exercises that bound.
        self.assertEqual(3, sum(1 for call in service._files.list_calls if "createdTime >" not in call.get("q", "")))
        self.assertIn("deep-file", service._files.get_media_calls)
        self.assertIn("deep-req", order)
        by_id = {r.get("file_id"): r for r in results}
        self.assertTrue(by_id["deep-file"]["accepted"])

    def test_duplicate_request_remains_idempotent_under_bounded_poll(self):
        service = Service()
        handler = Mock(return_value={"accepted": True, "request_id": "drive-e2e-1", "task_id": "dispatch-drive-e2e-1",
                                     "command_id": "dispatch-drive-e2e-1", "status": "completed"})
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            first = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)
            second = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)
        self.assertEqual(first, second)

    def test_no_archive_delete_move_api_calls_in_source(self):
        source = Path(inspect.getfile(drive_dispatch_ingress)).read_text(encoding="utf-8")
        for forbidden in ("files().delete(", "files().update(", ".trash(", "files().copy(", ".emptyTrash("):
            self.assertNotIn(forbidden, source, f"drive_dispatch_ingress must never call {forbidden}")

    def test_no_provider_launch_imports_in_source(self):
        source = Path(inspect.getfile(drive_dispatch_ingress)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden_modules = {"manager.claude_launcher", "manager.codex_launcher", "manager.ag_runner",
                             "manager.execution_runner", "manager.command_watcher"}
        self.assertFalse(imported & forbidden_modules, imported & forbidden_modules)

    def test_folder_owner_provenance_still_fail_closed_under_bounded_poll(self):
        service = MultiService([], folder={
            "id": FOLDER_ID, "name": "WRONG", "mimeType": MIME_FOLDER, "trashed": False,
            "parents": ["adm-root"], "driveId": None, **private_owner(),
        })
        with self.assertRaises(TaskError):
            poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW)

    def test_modified_time_indicates_stale_helper_is_conservative(self):
        self.assertFalse(_modified_time_indicates_stale({}, NOW))
        self.assertFalse(_modified_time_indicates_stale({"modifiedTime": "not-a-date"}, NOW))
        self.assertFalse(_modified_time_indicates_stale(
            {"modifiedTime": (NOW - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")}, NOW))
        self.assertTrue(_modified_time_indicates_stale(
            {"modifiedTime": (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}, NOW))
        # createdTime alone must have zero effect on this decision -- only
        # modifiedTime is consulted.
        self.assertFalse(_modified_time_indicates_stale(
            {"createdTime": (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}, NOW))

    def test_old_created_time_fresh_modified_time_forces_download_not_skip(self):
        # ISSUE 1 regression: an old createdTime with a fresh modifiedTime
        # and a fresh body created_at must NOT be skipped based on
        # createdTime alone -- the file must be downloaded and evaluated.
        old_created = NOW - timedelta(days=3)
        fresh_modified = NOW - timedelta(minutes=1)
        item = make_item("file-1", "req-1", created_time=old_created,
                         created_at=NOW - timedelta(minutes=1), modified_time=fresh_modified)
        service = MultiService([item])
        handler, order = self._accepting_handler()
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                    registry_factory=lambda *_a: object())
        self.assertIn("file-1", service._files.get_media_calls)
        self.assertEqual(["req-1"], order)
        self.assertTrue(results[0]["accepted"])

    def test_old_created_time_and_old_modified_time_may_skip_get_media(self):
        # ISSUE 1 regression: an old createdTime *and* an old modifiedTime
        # together safely prove the content has not changed, so the
        # get_media() download may be skipped.
        old_time = NOW - timedelta(days=3)
        item = make_item("file-1", "req-1", created_time=old_time, modified_time=old_time)
        service = MultiService([item])
        handler, order = self._accepting_handler()
        with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
            results = poll_drive_dispatch_requests(object(), service, "bucket", FOLDER_ID, OWNER, NOW,
                                                    registry_factory=lambda *_a: object())
        self.assertNotIn("file-1", service._files.get_media_calls)
        self.assertEqual([], order)
        self.assertFalse(any(r.get("file_id") == "file-1" for r in results))

    def test_runner_contract_three_positional_args_still_work(self):
        # manager.drive_dispatch_watcher.run_once() (the frozen bounded
        # runner lane) calls poll_drive_dispatch_requests(store, service,
        # bucket) with exactly these three positional arguments and
        # nothing else, relying on folder_id/expected_owner env-var
        # defaulting -- this must keep working unchanged.
        import os
        real_now = datetime.now(timezone.utc)
        items = [make_item("file-0", "req-0", real_now - timedelta(minutes=1))]
        service = MultiService(items)
        handler, order = self._accepting_handler()
        os.environ["ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID"] = FOLDER_ID
        os.environ["ADM_DRIVE_DISPATCH_INGRESS_OWNER"] = OWNER
        try:
            with unittest.mock.patch("manager.drive_dispatch_ingress.handle_dispatch", handler):
                results = poll_drive_dispatch_requests(object(), service, "bucket")
        finally:
            del os.environ["ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID"]
            del os.environ["ADM_DRIVE_DISPATCH_INGRESS_OWNER"]
        self.assertEqual(["req-0"], order)
        self.assertTrue(results[0]["accepted"])


if __name__ == "__main__": unittest.main()
