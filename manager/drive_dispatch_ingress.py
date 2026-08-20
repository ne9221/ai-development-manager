"""Admit private Drive JSON requests into ADM's existing trusted ingress."""

import json
import os
from datetime import datetime, timezone

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.dispatch_requests import dispatch_request_registry
from manager.tasks import MIME_FOLDER, MIME_JSON, TaskError, validate


FOLDER_NAME = "DISPATCH-REQUESTS"
FOLDER_ENV = "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID"
OWNER_ENV = "ADM_DRIVE_DISPATCH_INGRESS_OWNER"
MAX_AGE_SECONDS = 86400
MAX_FILE_BYTES = 16384
METADATA_FIELDS = "id,name,mimeType,trashed,parents,size,driveId,ownedByMe,owners(emailAddress,permissionId,me),permissions(id,type,role,emailAddress)"


def _identity_matches(identity, expected):
    return expected in {identity.get("emailAddress"), identity.get("permissionId")}


def _one_private_owner(metadata, expected):
    owners = metadata.get("owners")
    permissions = metadata.get("permissions")
    return (isinstance(owners, list) and len(owners) == 1 and _identity_matches(owners[0], expected)
            and isinstance(permissions, list) and len(permissions) == 1
            and permissions[0].get("type") == "user" and permissions[0].get("role") == "owner"
            and _identity_matches(permissions[0], expected))


def verify_ingress_folder(service, folder_id, expected_owner):
    if not folder_id or not expected_owner:
        raise TaskError(f"{FOLDER_ENV} and {OWNER_ENV} are required")
    about = service.about().get(fields="user(emailAddress,permissionId)").execute()
    user = about.get("user") if isinstance(about, dict) else None
    if not isinstance(user, dict) or not _identity_matches(user, expected_owner):
        raise TaskError("Drive ingress OAuth identity is missing or does not match configured owner")
    folder = service.files().get(fileId=folder_id, fields=METADATA_FIELDS).execute()
    if (not isinstance(folder, dict) or folder.get("name") != FOLDER_NAME
            or folder.get("mimeType") != MIME_FOLDER or folder.get("trashed") is not False
            or folder.get("driveId") is not None or folder.get("ownedByMe") is not True
            or not _one_private_owner(folder, expected_owner)):
        raise TaskError("Drive ingress folder provenance is ambiguous or unverifiable")
    return folder


def _request_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed=false"
    files, page_token, seen_tokens = [], None, set()
    while True:
        params = {
            "q": query, "spaces": "drive",
            "fields": f"nextPageToken,files({METADATA_FIELDS})", "pageSize": 100,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        response = service.files().list(**params).execute()
        page = response.get("files") if isinstance(response, dict) else None
        next_token = response.get("nextPageToken") if isinstance(response, dict) else None
        if (not isinstance(page, list)
                or (next_token is not None and (not isinstance(next_token, str)
                                                or not next_token or next_token in seen_tokens))):
            raise TaskError("malformed Drive ingress listing response")
        files.extend(page)
        if next_token is None:
            return files
        seen_tokens.add(next_token)
        page_token = next_token


def read_request(service, folder_id, expected_owner, metadata, now=None):
    if (not isinstance(metadata, dict) or metadata.get("mimeType") != MIME_JSON
            or metadata.get("parents") != [folder_id] or metadata.get("trashed") is not False
            or metadata.get("driveId") is not None or metadata.get("ownedByMe") is not True
            or not _one_private_owner(metadata, expected_owner)):
        raise TaskError("Drive request provenance is ambiguous or unverifiable")
    try:
        size = int(metadata.get("size"))
    except (TypeError, ValueError) as exc:
        raise TaskError("Drive request size is unverifiable") from exc
    if size < 2 or size > MAX_FILE_BYTES:
        raise TaskError("Drive request size is outside the accepted range")
    raw = service.files().get_media(fileId=metadata["id"]).execute()
    if not isinstance(raw, bytes) or len(raw) != size or len(raw) > MAX_FILE_BYTES:
        raise TaskError("Drive request content verification failed")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskError("Drive request is not valid UTF-8 JSON") from exc
    validate("dispatch_request", document)
    if metadata.get("name") != f'{document["request_id"]}.json':
        raise TaskError("Drive request filename does not match request_id")
    created = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    age = (current - created.astimezone(timezone.utc)).total_seconds()
    if age < -300 or age > MAX_AGE_SECONDS:
        raise TaskError("Drive request is stale or future-dated")
    return document


def poll_drive_dispatch_requests(store, service, bucket, folder_id=None, expected_owner=None, now=None,
                                 registry_factory=dispatch_request_registry):
    folder_id = folder_id or os.environ.get(FOLDER_ENV)
    expected_owner = expected_owner or os.environ.get(OWNER_ENV)
    verify_ingress_folder(service, folder_id, expected_owner)
    results = []
    for metadata in _request_files(service, folder_id):
        try:
            request = read_request(service, folder_id, expected_owner, metadata, now=now)
            payload = {
                "request_id": request["request_id"], "project_id": request["project_id"],
                "title": request["title"], "goal": request["goal"],
                "priority": request.get("priority") or "normal",
                "constraints": {"read_only": True},
            }
            if request.get("preferred_provider") is not None:
                payload["provider"] = request["preferred_provider"]
            if request.get("account_id") is not None:
                payload["account_id"] = request["account_id"]
            result = handle_dispatch(store, service, lambda project_id, request_id:
                                     registry_factory(bucket, project_id, request_id), payload)
            results.append({"file_id": metadata["id"], **result})
        except (TaskError, DispatchIngressError):
            results.append({"file_id": metadata.get("id"), "accepted": False})
    return results
