"""GCS generation-CAS backend for the worktree lock registry."""

import json
import os
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from manager.tasks import TaskError


GCS_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
BUCKET_ENV = "ADM_LOCK_GCS_BUCKET"
OBJECT_ENV = "ADM_LOCK_GCS_OBJECT"


class RegistryConflict(TaskError):
    pass


class GCSLockRegistry:
    def __init__(self, bucket, object_name, session=None):
        if not bucket or not object_name or object_name.startswith("/"):
            raise TaskError("GCS lock bucket and repo-relative object name are required")
        self.bucket, self.object_name = bucket, object_name
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=[GCS_SCOPE])
            session = AuthorizedSession(credentials)
        self.session = session
        encoded_bucket, encoded_object = quote(bucket, safe=""), quote(object_name, safe="")
        self.metadata_url = f"https://storage.googleapis.com/storage/v1/b/{encoded_bucket}/o/{encoded_object}"
        self.upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{encoded_bucket}/o"

    @classmethod
    def from_environment(cls, bucket=None, object_name=None):
        return cls(bucket or os.environ.get(BUCKET_ENV), object_name or os.environ.get(OBJECT_ENV))

    @staticmethod
    def _server_time(response):
        value = response.headers.get("Date")
        if not value:
            raise TaskError("GCS response is missing server Date")
        return parsedate_to_datetime(value)

    def read(self):
        try:
            metadata = self.session.get(self.metadata_url, timeout=30)
            if metadata.status_code != 200:
                raise TaskError(f"GCS registry metadata read failed: HTTP {metadata.status_code}")
            generation = int(metadata.json()["generation"])
            content = self.session.get(self.metadata_url, params={"alt": "media", "ifGenerationMatch": generation}, timeout=30)
            if content.status_code == 412:
                raise RegistryConflict("GCS registry changed during read")
            if content.status_code != 200:
                raise TaskError(f"GCS registry content read failed: HTTP {content.status_code}")
            document = content.json()
            if not isinstance(document, dict):
                raise ValueError("registry is not a JSON object")
            return document, generation, self._server_time(metadata)
        except (TaskError, RegistryConflict):
            raise
        except Exception as exc:
            raise TaskError("GCS registry read failed") from exc

    def compare_and_swap(self, expected_generation, document):
        return self._write(expected_generation, document)

    def cas(self, expected_generation, document):
        return self.compare_and_swap(expected_generation, document)

    def create_if_absent(self, document):
        return self._write(0, document)

    def _write(self, expected_generation, document):
        try:
            response = self.session.post(
                self.upload_url,
                params={"uploadType": "media", "name": self.object_name, "ifGenerationMatch": int(expected_generation)},
                headers={"Content-Type": "application/json"},
                data=(json.dumps(document, indent=2) + "\n").encode("utf-8"),
                timeout=30,
            )
            if response.status_code == 412:
                raise RegistryConflict("GCS generation precondition failed")
            if response.status_code not in (200, 201):
                raise TaskError(f"GCS registry conditional write failed: HTTP {response.status_code}")
            return int(response.json()["generation"])
        except (TaskError, RegistryConflict):
            raise
        except Exception as exc:
            raise TaskError("GCS registry conditional write failed") from exc
