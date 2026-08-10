#!/usr/bin/env python3
"""Non-sensitive Google Drive OAuth health and explicit authorization commands."""

import argparse
import json
import sys

from collectors.publish_drive import PublisherError, credentials_with_source, token_path
from manager.tasks import ROOT_FOLDER_ID


def _token_flags(path):
    result = {"token_exists": path.is_file(), "valid": False, "expired": None, "has_refresh_token": False}
    if not path.is_file():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result["has_refresh_token"] = bool(data.get("refresh_token"))
        result["expired"] = None
    except (OSError, ValueError):
        result["error_category"] = "malformed_token"
    return result


def _category(error):
    message = str(error).lower()
    if "dependencies unavailable" in message: return "dependency_missing"
    if "reauthorization required" in message: return "reauth_required"
    if "api" in message or "network" in message: return "api_unreachable"
    return "authentication_error"


def status():
    path = token_path()
    result = {"token_path": str(path), "credential_source": None, "drive_api_reachable": False,
              "target_root_accessible": False, "reauth_required": False, "error_category": None,
              **_token_flags(path)}
    try:
        creds, source = credentials_with_source()
        result.update(credential_source=source, valid=bool(creds.valid), expired=bool(creds.expired), has_refresh_token=bool(getattr(creds, "refresh_token", None)))
        from googleapiclient.discovery import build
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        result["drive_api_reachable"] = True
        service.files().get(fileId=ROOT_FOLDER_ID, fields="id,name,mimeType").execute()
        result["target_root_accessible"] = True
    except (PublisherError, OSError, ValueError) as exc:
        result["error_category"] = _category(exc)
        result["reauth_required"] = result["error_category"] == "reauth_required"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "authorize"))
    args = parser.parse_args()
    try:
        if args.command == "status": result = status()
        else:
            creds, source = credentials_with_source(allow_interactive=True)
            result = {"token_path": str(token_path()), "credential_source": source, "valid": bool(creds.valid)}
        print(json.dumps(result, indent=2)); return 0
    except (PublisherError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
