#!/usr/bin/env python3
"""Publish a schema-valid runtime status JSON to one Google Drive file."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


FOLDER_ID = "1JGc9_QdI3nkapmLt0HfvDyap4Jpn71k7"
FILE_NAME = "status.json"
MIME_TYPE = "application/json"
SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_REQUEST_TIMEOUT_SECONDS = 45


class PublisherError(RuntimeError):
    pass


REQUIRED_INSTALLED_FIELDS = ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris")


def _validate_installed_client_config(config, source):
    """Validate a Google OAuth client config is a well-formed Desktop (installed) client.

    Fails closed (raises PublisherError) on anything malformed, empty, or of the
    wrong client type (e.g. a Web client). Never accepts a config silently.
    """
    if not isinstance(config, dict) or not config:
        raise PublisherError(f"{source} OAuth client configuration is malformed or empty")
    if "installed" not in config:
        if "web" in config:
            raise PublisherError(
                f"{source} OAuth client is a Web application client; "
                "a Desktop (installed) OAuth client is required"
            )
        raise PublisherError(f"{source} OAuth client configuration is malformed: missing 'installed' section")
    installed = config["installed"]
    if not isinstance(installed, dict):
        raise PublisherError(f"{source} OAuth client configuration is malformed: 'installed' is not an object")
    missing = [field for field in REQUIRED_INSTALLED_FIELDS if not installed.get(field)]
    if missing:
        raise PublisherError(
            f"{source} OAuth client configuration is malformed: missing {', '.join(missing)}"
        )
    return installed


def _load_bundled_oauth_config():
    from manager.default_oauth_config import UNPROVISIONED_SENTINEL, load_default_oauth_config

    config = load_default_oauth_config()
    installed = _validate_installed_client_config(config, source="ADM bundled default")
    if installed.get("client_id") == UNPROVISIONED_SENTINEL or installed.get("client_secret") == UNPROVISIONED_SENTINEL:
        raise PublisherError("ADM Desktop OAuth client configuration not provisioned")
    return config


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


def token_path():
    return Path(os.environ.get(
        "GOOGLE_DRIVE_TOKEN",
        Path.home() / ".config" / "ai-development-manager" / "google-drive-token.json",
    ))


def _oauth_imports():
    try:
        import google.auth
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.exceptions import DefaultCredentialsError, RefreshError
    except ImportError as exc:
        raise PublisherError("Google Drive dependencies unavailable; install collectors/requirements.txt") from exc
    return {"google_auth": google.auth, "Request": Request, "Credentials": Credentials,
            "InstalledAppFlow": InstalledAppFlow, "DefaultCredentialsError": DefaultCredentialsError,
            "RefreshError": RefreshError}


def _write_token(path, creds):
    """Replace a token only after a successful refresh or interactive authorization."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = creds.to_json()
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(payload)
        Path(temporary).replace(path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def credentials_with_source(allow_interactive=False, oauth=None, persist_refreshed_token=True):
    """Resolve Drive OAuth credentials.

    persist_refreshed_token controls whether a successful in-memory token
    refresh is written back to disk. Desktop/normal callers keep the
    original write-back behavior (default True); a caller whose token store
    is a read-only mount (e.g. a Cloud Run Secret Manager volume) must pass
    False so a refresh still succeeds without attempting a doomed disk write.
    """
    oauth = oauth or _oauth_imports()
    path = token_path()
    creds = None
    source = None
    token_problem = None
    if path.is_file():
        try:
            creds = oauth["Credentials"].from_authorized_user_file(path, SCOPES)
            source = "existing_token"
        except Exception:
            token_problem = "malformed_token"
    if creds and creds.expired:
        if not creds.refresh_token:
            token_problem = "expired_without_refresh_token"; creds = None
        else:
            try:
                creds.refresh(oauth["Request"]())
                if persist_refreshed_token:
                    _write_token(path, creds)
                source = "refreshed_token"
            except oauth["RefreshError"]:
                token_problem = "invalid_refresh_token"; creds = None
            except Exception as exc:
                raise PublisherError(f"Google OAuth token refresh failed: {exc}") from exc
    if creds and creds.valid:
        return creds, source

    try:
        creds, _ = oauth["google_auth"].default(scopes=SCOPES)
        if creds and creds.valid:
            return creds, "application_default"
    except oauth["DefaultCredentialsError"]:
        pass

    if not allow_interactive:
        detail = f" ({token_problem})" if token_problem else ""
        raise PublisherError(f"Google OAuth reauthorization required{detail}. Run python -m manager.drive_auth authorize.")

    client_secrets = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS")
    if client_secrets:
        secrets_path = Path(client_secrets)
        if not secrets_path.is_file():
            raise PublisherError(f"GOOGLE_OAUTH_CLIENT_SECRETS file not found: {client_secrets}")
        try:
            env_config = json.loads(secrets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PublisherError(f"GOOGLE_OAUTH_CLIENT_SECRETS file is malformed: {exc}") from exc
        _validate_installed_client_config(env_config, source="GOOGLE_OAUTH_CLIENT_SECRETS")
        flow = oauth["InstalledAppFlow"].from_client_secrets_file(str(secrets_path), SCOPES)
    else:
        bundled_config = _load_bundled_oauth_config()
        flow = oauth["InstalledAppFlow"].from_client_config(bundled_config, SCOPES)

    try:
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        raise PublisherError(f"Google Desktop OAuth authorization failed: {exc}") from exc
    if not creds or not creds.valid:
        raise PublisherError("Google Desktop OAuth did not return valid credentials")
    _write_token(path, creds)
    return creds, "desktop_oauth"


def credentials(allow_interactive=False):
    return credentials_with_source(allow_interactive=allow_interactive)[0]


def build_service():
    try:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build
        http = AuthorizedHttp(credentials(), http=httplib2.Http(timeout=DRIVE_REQUEST_TIMEOUT_SECONDS))
        return build("drive", "v3", http=http, cache_discovery=False)
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
