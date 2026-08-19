import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

from manager.drive_dispatch_ingress import (
    FOLDER_NAME, poll_drive_dispatch_requests, read_request, verify_ingress_folder,
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


class DriveDispatchIngressTests(unittest.TestCase):
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


if __name__ == "__main__": unittest.main()
