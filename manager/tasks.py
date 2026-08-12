#!/usr/bin/env python3
"""Minimal Drive-backed project, task, handoff, and history manager."""

import argparse
import io
import json
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from collectors.publish_drive import build_service
from manager.assignment import decide
from manager.quota_reader import read_drive_status, summarize
from jsonschema import Draft202012Validator, FormatChecker


ROOT_FOLDER_ID = "1pXvl8BglU05ZrXMHIVIDyK-lOWNShXSO"
ROOT_FOLDERS = {"tasks": "TASKS", "handoffs": "HANDOFFS", "history": "TASK-HISTORY", "projects": "PROJECTS", "executions": "EXECUTIONS", "sessions": "SESSIONS", "session_reviews": "SESSION-REVIEWS", "overviews": "OVERVIEWS", "worktree_locks": "WORKTREE-LOCKS"}
SCHEMAS = {name: Path(__file__).parents[1] / "schema" / f"{name}.schema.json" for name in ("project", "project_preview", "task", "handoff", "execution", "session", "session_review", "overview", "worktree_lock", "worktree_lock_registry")}
MIME_JSON = "application/json"
MIME_FOLDER = "application/vnd.google-apps.folder"


class TaskError(RuntimeError):
    pass


class DriveConflict(TaskError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate(kind, document):
    try:
        schema = json.loads(SCHEMAS[kind].read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise TaskError(f"invalid {kind}: {exc}") from exc


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise TaskError(f"unsafe record id: {value!r}")
    return value


class DriveRecords:
    def __init__(self, service):
        self.files = service.files()

    def children(self, parent, name=None):
        query = f"'{parent}' in parents and trashed=false"
        if name:
            query += f" and name='{name}'"
        items, token, seen = [], None, set()
        while True:
            options = {"q": query, "spaces": "drive", "fields": "nextPageToken,files(id,name,mimeType,parents,modifiedTime)", "pageSize": 100}
            if token:
                options["pageToken"] = token
            response = self.files.list(**options).execute()
            if not isinstance(response, dict) or not isinstance(response.get("files", []), list):
                raise TaskError("malformed Drive listing response")
            items.extend(response.get("files", []))
            next_token = response.get("nextPageToken")
            if next_token is None:
                return items
            if not isinstance(next_token, str) or not next_token or next_token in seen:
                raise TaskError("invalid or repeated Drive pagination token")
            seen.add(next_token)
            token = next_token

    @staticmethod
    def _execute_with_headers(request):
        if not hasattr(request, "postproc"):
            raise TaskError("Drive client does not expose response headers")
        headers = {}
        original = request.postproc

        def capture(response, content):
            headers.update({str(key).lower(): value for key, value in response.items()})
            return original(response, content)

        request.postproc = capture
        return request.execute(), headers

    def read_versioned_json(self, file_id):
        try:
            raw, headers = self._execute_with_headers(self.files.get_media(fileId=safe_id(file_id)))
            etag, date = headers.get("etag"), headers.get("date")
            if not etag or not date:
                raise TaskError("Drive conditional record is missing ETag or server Date")
            server_time = parsedate_to_datetime(date).astimezone(timezone.utc)
            document = json.loads(raw.decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("record is not a JSON object")
            return document, etag, server_time
        except TaskError:
            raise
        except Exception as exc:
            raise TaskError("could not read versioned Drive JSON record") from exc

    def update_versioned_json(self, file_id, expected_etag, document):
        from googleapiclient.http import MediaIoBaseUpload
        raw = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(raw), mimetype=MIME_JSON, resumable=False)
        request = self.files.update(fileId=safe_id(file_id), body={}, media_body=media, fields="id")
        if not hasattr(request, "headers"):
            raise TaskError("Drive client does not support conditional request headers")
        request.headers["If-Match"] = expected_etag
        try:
            request.execute()
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 412:
                raise DriveConflict("Drive record changed concurrently") from exc
            raise TaskError("Drive conditional update failed") from exc
        return document

    def provision_versioned_json(self, area, project_id, name, document):
        """Create one separately provisioned CAS record; never used implicitly by acquire."""
        from googleapiclient.http import MediaIoBaseUpload
        parent = self.project_folder(area, project_id)
        filename = f"{safe_id(name)}.json"
        matches = [item for item in self.children(parent, filename) if item.get("mimeType") == MIME_JSON]
        if len(matches) > 1:
            raise TaskError(f"duplicate Drive record: {filename}")
        if matches:
            return matches[0]["id"]
        response = self.files.generateIds(count=1, space="drive", type="files").execute()
        ids = response.get("ids", []) if isinstance(response, dict) else []
        if len(ids) != 1:
            raise TaskError("Drive did not generate one registry file ID")
        file_id = safe_id(ids[0])
        raw = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(raw), mimetype=MIME_JSON, resumable=False)
        self.files.create(body={"id": file_id, "name": filename, "parents": [parent], "mimeType": MIME_JSON}, media_body=media, fields="id").execute()
        final = [item for item in self.children(parent, filename) if item.get("mimeType") == MIME_JSON]
        if len(final) != 1 or final[0]["id"] != file_id:
            raise TaskError("Drive registry provisioning is ambiguous; do not acquire")
        return file_id

    def folder(self, parent, name, create=True):
        matches = [item for item in self.children(parent, name) if item.get("mimeType") == MIME_FOLDER]
        if len(matches) > 1:
            raise TaskError(f"duplicate Drive folder: {name}")
        if matches:
            return matches[0]["id"]
        if not create:
            raise TaskError(f"Drive folder not found: {name}")
        return self.files.create(body={"name": name, "parents": [parent], "mimeType": MIME_FOLDER}, fields="id").execute()["id"]

    def project_folder(self, area, project_id, create=True):
        root = self.folder(ROOT_FOLDER_ID, ROOT_FOLDERS[area], create)
        return self.folder(root, safe_id(project_id), create)

    def put(self, area, project_id, name, document):
        from googleapiclient.http import MediaIoBaseUpload
        parent = self.project_folder(area, project_id)
        filename = f"{safe_id(name)}.json"
        matches = [item for item in self.children(parent, filename) if item.get("mimeType") == MIME_JSON]
        if len(matches) > 1:
            raise TaskError(f"duplicate Drive record: {filename}")
        raw = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(raw), mimetype=MIME_JSON, resumable=False)
        if matches:
            file_id = matches[0]["id"]
            self.files.update(fileId=file_id, body={"name": filename}, media_body=media, fields="id").execute()
        else:
            file_id = self.files.create(body={"name": filename, "parents": [parent], "mimeType": MIME_JSON}, media_body=media, fields="id").execute()["id"]
        remote = self.files.get_media(fileId=file_id).execute()
        final = self.children(parent, filename)
        if remote != raw or len(final) != 1 or final[0]["id"] != file_id:
            raise TaskError(f"Drive verification failed: {filename}")
        return document

    def get(self, area, project_id, name):
        parent = self.project_folder(area, project_id, create=False)
        filename = f"{safe_id(name)}.json"
        matches = self.children(parent, filename)
        if len(matches) != 1:
            raise TaskError(f"expected one Drive record {filename}; found {len(matches)}")
        try:
            return json.loads(self.files.get_media(fileId=matches[0]["id"]).execute().decode("utf-8"))
        except Exception as exc:
            raise TaskError(f"could not read Drive record: {filename}") from exc

    def list_records(self, area, project_id):
        parent = self.project_folder(area, project_id, create=False)
        names = [item["name"][:-5] for item in self.children(parent) if item.get("name", "").endswith(".json")]
        return [self.get(area, project_id, name) for name in names]

    def latest(self, area, project_id, task_id):
        parent = self.project_folder(area, project_id, create=False)
        candidates = [item for item in self.children(parent) if item["name"].endswith(".json")]
        records = [self.get(area, project_id, item["name"][:-5]) for item in candidates]
        records = [item for item in records if item.get("task_id") == task_id]
        if not records:
            raise TaskError(f"no handoff found for task: {task_id}")
        return max(records, key=lambda item: item["created_at"])


def create_project(store, document):
    validate("project", document)
    return store.put("projects", document["project_id"], document["project_id"], document)


def create_task(store, document, service=None, assign=True, persist=True):
    timestamp = now_iso()
    document = dict(document)
    document.setdefault("status", "ready")
    document.setdefault("priority", "normal")
    document.setdefault("created_at", timestamp)
    document.setdefault("updated_at", timestamp)
    for key in ("scope", "constraints", "acceptance_criteria", "depends_on"):
        document.setdefault(key, [])
    for key in ("recommended_provider", "assigned_provider", "mode", "effort", "blocked_reason"):
        document.setdefault(key, None)
    document.setdefault("source_context", {})
    document.setdefault("current_progress", "Not started")
    document.setdefault("next_action", "Confirm assignment and begin")
    if assign:
        quota = summarize(read_drive_status(service=service), max_age_minutes=60)
        decision = decide(document, quota)
        document.update({
            "recommended_provider": decision["recommended_provider"],
            "mode": decision["recommended_mode"],
            "effort": decision["recommended_effort"],
            "quota_evidence": decision["quota_evidence"],
        })
    validate("task", document)
    return store.put("tasks", document["project_id"], document["task_id"], document) if persist else document


def update_task(store, project_id, task_id, **changes):
    task = store.get("tasks", project_id, task_id)
    task.update({key: value for key, value in changes.items() if value is not None})
    task["updated_at"] = now_iso()
    if task["status"] == "blocked" and not task.get("blocked_reason"):
        raise TaskError("blocked task requires blocked_reason")
    validate("task", task)
    return store.put("tasks", project_id, task_id, task)


def create_handoff(store, document):
    document = dict(document)
    document.setdefault("created_at", now_iso())
    for key in ("completed_work", "files_changed", "commits", "tests", "known_issues", "do_not_touch", "acceptance_criteria"):
        document.setdefault(key, [])
    validate("handoff", document)
    return store.put("handoffs", document["project_id"], document["handoff_id"], document)


def complete_task(store, project_id, task_id, summary, provider=None, session=None):
    task = store.get("tasks", project_id, task_id)
    timestamp = now_iso()
    task.update(status="completed", completed_at=timestamp, updated_at=timestamp, blocked_reason=None, current_progress=summary, next_action="")
    validate("task", task)
    store.put("tasks", project_id, task_id, task)
    store.put("history", project_id, f"{task_id}-{timestamp[:10]}", task)
    handoff = create_handoff(store, {
        "handoff_id": f"{task_id}-final-{timestamp.replace(':', '').replace('-', '')}",
        "task_id": task_id, "project_id": project_id, "created_at": timestamp,
        "from_provider": provider or task.get("assigned_provider"), "to_provider": None,
        "from_session": session, "reason": "completed", "completed_work": [summary],
        "current_state": "completed", "next_action": "", "minimal_context": summary,
        "acceptance_criteria": task["acceptance_criteria"],
    })
    return task, handoff


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("project-put", "task-create", "handoff-create"):
        item = sub.add_parser(command); item.add_argument("input")
    read = sub.add_parser("task-read"); read.add_argument("project_id"); read.add_argument("task_id")
    update = sub.add_parser("task-update"); update.add_argument("project_id"); update.add_argument("task_id"); update.add_argument("--status"); update.add_argument("--progress"); update.add_argument("--next-action"); update.add_argument("--blocked-reason"); update.add_argument("--assigned-provider")
    latest = sub.add_parser("handoff-latest"); latest.add_argument("project_id"); latest.add_argument("task_id")
    complete = sub.add_parser("task-complete"); complete.add_argument("project_id"); complete.add_argument("task_id"); complete.add_argument("--summary", required=True); complete.add_argument("--provider"); complete.add_argument("--session")
    args = parser.parse_args()
    try:
        service = build_service(); store = DriveRecords(service)
        if args.command == "project-put": result = create_project(store, load_json(args.input))
        elif args.command == "task-create": result = create_task(store, load_json(args.input), service)
        elif args.command == "task-read": result = store.get("tasks", args.project_id, args.task_id); validate("task", result)
        elif args.command == "task-update": result = update_task(store, args.project_id, args.task_id, status=args.status, current_progress=args.progress, next_action=args.next_action, blocked_reason=args.blocked_reason, assigned_provider=args.assigned_provider)
        elif args.command == "handoff-create": result = create_handoff(store, load_json(args.input))
        elif args.command == "handoff-latest": result = store.latest("handoffs", args.project_id, args.task_id); validate("handoff", result)
        else: result = complete_task(store, args.project_id, args.task_id, args.summary, args.provider, args.session)[0]
        print(json.dumps(result, indent=2))
        return 0
    except (TaskError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
