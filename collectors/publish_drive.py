#!/usr/bin/env python3
"""Publish a schema-valid runtime status JSON to one Google Drive file."""

import argparse
import json
import os
import sys
from pathlib import Path


FOLDER_ID = "1JGc9_QdI3nkapmLt0HfvDyap4Jpn71k7"
FILE_NAME = "status.json"
MIME_TYPE = "application/json"
SCOPES = ["https://www.googleapis.com/auth/drive"]


class PublisherError(RuntimeError):
    pass


def load_status(path, schema_path):
    if not path.is_file():
        raise PublisherError(f"local status JSON not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        from jsonschema import Draft202012Validator, FormatChecker
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise PublisherError(f"schema validation failed: {exc}") from exc
    return document


def credentials():
    try:
        import google.auth
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise PublisherError("Google Drive dependencies unavailable; install collectors/requirements.txt") from exc

    token_path = Path(os.environ.get(
        "GOOGLE_DRIVE_TOKEN",
        Path.home() / ".config" / "ai-development-manager" / "google-drive-token.json",
    ))
    creds = Credentials.from_authorized_user_file(token_path, SCOPES) if token_path.is_file() else None
    save_token = False
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_token = True
    if not creds or not creds.valid:
        try:
            creds, _ = google.auth.default(scopes=SCOPES)
        except google.auth.exceptions.DefaultCredentialsError:
            client_secrets = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS")
            if not client_secrets:
                raise PublisherError(
                    "Google OAuth not authorized; set GOOGLE_OAUTH_CLIENT_SECRETS to an official Desktop OAuth client JSON"
                )
            creds = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES).run_local_server(port=0)
            save_token = True
    if save_token:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service():
    try:
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=credentials(), cache_discovery=False)
    except PublisherError:
        raise
    except Exception as exc:
        raise PublisherError(f"Google Drive API initialization failed: {exc}") from exc


def find_status_files(files, folder_id):
    query = f"name='{FILE_NAME}' and '{folder_id}' in parents and trashed=false"
    return files.list(q=query, spaces="drive", fields="files(id,name,mimeType,parents)", pageSize=10).execute().get("files", [])


def sync_drive(service, status_path, folder_id=FOLDER_ID, media_factory=None):
    files = service.files()
    try:
        folder = files.get(fileId=folder_id, fields="id,name,mimeType,capabilities(canAddChildren)").execute()
        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            raise PublisherError(f"Drive target is not a folder: {folder_id}")
        if not folder.get("capabilities", {}).get("canAddChildren", False):
            raise PublisherError(f"Drive folder is not writable: {folder_id}")

        matches = find_status_files(files, folder_id)
        if len(matches) > 1:
            raise PublisherError(f"multiple {FILE_NAME} files found in Drive folder")

        if media_factory is None:
            from googleapiclient.http import MediaFileUpload
            media_factory = lambda path: MediaFileUpload(path, mimetype=MIME_TYPE, resumable=False)
        media = media_factory(str(status_path))
        if matches:
            file_id = matches[0]["id"]
            files.update(fileId=file_id, body={"name": FILE_NAME}, media_body=media, fields="id").execute()
            action = "updated"
        else:
            created = files.create(
                body={"name": FILE_NAME, "parents": [folder_id], "mimeType": MIME_TYPE},
                media_body=media,
                fields="id",
            ).execute()
            file_id = created.get("id")
            if not file_id:
                raise PublisherError("Drive upload returned no file id")
            action = "created"

        metadata = files.get(fileId=file_id, fields="id,name,mimeType,parents").execute()
        final_matches = find_status_files(files, folder_id)
        remote = files.get_media(fileId=file_id).execute()
        local = status_path.read_bytes()
        if metadata.get("name") != FILE_NAME or folder_id not in metadata.get("parents", []):
            raise PublisherError("Drive upload metadata verification failed")
        if metadata.get("mimeType") != MIME_TYPE:
            raise PublisherError(f"Drive file MIME type is not {MIME_TYPE}")
        if len(final_matches) != 1 or final_matches[0].get("id") != file_id:
            raise PublisherError(f"Drive folder does not contain exactly one canonical {FILE_NAME}")
        if remote != local:
            raise PublisherError("Drive content verification failed")
        return {"id": file_id, "action": action, "metadata": metadata}
    except PublisherError:
        raise
    except Exception as exc:
        raise PublisherError(f"Google Drive API request failed: {exc}") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", type=Path, default=Path(__file__).with_name("codex.status.json"))
    parser.add_argument("--schema", type=Path, default=Path(__file__).parents[1] / "schema" / "status.schema.json")
    parser.add_argument("--folder-id", default=FOLDER_ID)
    args = parser.parse_args()
    try:
        load_status(args.status, args.schema)
        result = sync_drive(build_service(), args.status, args.folder_id)
        print(f"SYNCED {FILE_NAME} {result['action']} id={result['id']}")
    except (PublisherError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
