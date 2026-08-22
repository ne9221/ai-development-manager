import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collectors.publish_drive import DRIVE_REQUEST_TIMEOUT_SECONDS, FILE_NAME, MIME_TYPE, PublisherError, build_service, sync_drive


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeFiles:
    def __init__(self, content, duplicate=False):
        self.content = content
        self.file_id = None
        self.duplicate = duplicate

    def get(self, fileId, fields):
        if "capabilities" in fields:
            return Request({"id": fileId, "mimeType": "application/vnd.google-apps.folder", "capabilities": {"canAddChildren": True}})
        return Request({"id": fileId, "name": FILE_NAME, "mimeType": MIME_TYPE, "parents": ["folder"]})

    def list(self, **_kwargs):
        if self.duplicate:
            return Request({"files": [{"id": "one"}, {"id": "two"}]})
        return Request({"files": [] if self.file_id is None else [{"id": self.file_id}]})

    def create(self, **_kwargs):
        self.file_id = "status-id"
        return Request({"id": self.file_id})

    def update(self, fileId, **_kwargs):
        self.file_id = fileId
        return Request({"id": fileId})

    def get_media(self, fileId):
        return Request(self.content)


class FakeDrive:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class PublisherTest(unittest.TestCase):
    def test_build_service_bounds_each_drive_request_without_bounding_provider_lifecycle(self):
        credentials, transport, authorized, service = object(), object(), object(), object()
        with patch("collectors.publish_drive.credentials", return_value=credentials), \
             patch("httplib2.Http", return_value=transport) as http_factory, \
             patch("google_auth_httplib2.AuthorizedHttp", return_value=authorized) as authorized_http, \
             patch("googleapiclient.discovery.build", return_value=service) as build:
            self.assertIs(service, build_service())
        http_factory.assert_called_once_with(timeout=DRIVE_REQUEST_TIMEOUT_SECONDS)
        authorized_http.assert_called_once_with(credentials, http=transport)
        build.assert_called_once_with("drive", "v3", http=authorized, cache_discovery=False)

    def test_create_and_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / FILE_NAME
            path.write_bytes(b'{"ok":true}\n')
            result = sync_drive(FakeDrive(FakeFiles(path.read_bytes())), path, "folder", lambda _path: object())
            self.assertEqual(result["action"], "created")

    def test_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / FILE_NAME
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(PublisherError, "multiple status.json"):
                sync_drive(FakeDrive(FakeFiles(path.read_bytes(), duplicate=True)), path, "folder", lambda _path: object())


if __name__ == "__main__":
    unittest.main()
