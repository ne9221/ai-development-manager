#!/usr/bin/env python3
"""Read-only Codex session discovery and Drive-backed session registry."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords, ROOT_FOLDER_ID, ROOT_FOLDERS, TaskError, validate


SHORT_PROMPT_LIMIT = 1000
UNCLASSIFIED_PROJECT_ID = "_unclassified"


def _json_lines(path):
    """Yield valid JSON objects only; an active Codex file may end mid-write."""
    with Path(path).open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item


def _text_content(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return None
    texts = [item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
    value = "\n".join(text.strip() for text in texts if text.strip()).strip()
    return value or None


def _safe_timestamp(value):
    return value if isinstance(value, str) and value else None


def parse_codex_session(path, source_root, titles=None):
    """Return reliable metadata from one Codex JSONL file without changing it."""
    path = Path(path)
    metadata = None
    timestamps, messages, model, first_prompt = [], 0, None, None
    for item in _json_lines(path):
        timestamp = _safe_timestamp(item.get("timestamp"))
        if timestamp:
            timestamps.append(timestamp)
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("type") == "session_meta" and metadata is None:
            metadata = payload
        if item.get("type") == "turn_context" and isinstance(payload.get("model"), str) and model is None:
            model = payload["model"]
        if item.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
            messages += 1
            if payload.get("role") == "user" and first_prompt is None:
                first_prompt = _text_content(payload.get("content"))
    if not metadata:
        return None
    session_id = metadata.get("session_id") or metadata.get("id")
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        source_identifier = path.relative_to(source_root).as_posix()
    except ValueError:
        source_identifier = str(path)
    title = (titles or {}).get(session_id)
    return {
        "session_id": session_id, "provider": "codex", "project_id": None, "task_id": None,
        "conversation_label": title, "title": title, "summary": None,
        "started_at": _safe_timestamp(metadata.get("timestamp")) or (min(timestamps) if timestamps else None),
        "updated_at": max(timestamps) if timestamps else None,
        "working_directory": metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None,
        "repository": None, "source_identifier": source_identifier,
        "classification_method": "unclassified", "classification_confidence": None,
        "classification_status": "needs_review", "status": "unknown",
        "message_count": messages, "model": model,
        "first_user_prompt": first_prompt[:SHORT_PROMPT_LIMIT] if first_prompt else None,
    }


def load_codex_titles(index_path):
    """Best-effort lookup from Codex's optional session index; malformed lines are ignored."""
    titles = {}
    if not Path(index_path).is_file():
        return titles
    for item in _json_lines(index_path):
        session_id, title = item.get("id"), item.get("thread_name")
        if isinstance(session_id, str) and isinstance(title, str) and title.strip():
            titles[session_id] = title.strip()[:500]
    return titles


def default_sessions_root():
    return Path.home() / ".codex" / "sessions"


def discover_codex_sessions(sessions_root=None, titles=None):
    root = Path(sessions_root or default_sessions_root())
    if not root.is_dir():
        raise TaskError(f"Codex sessions directory not found: {root}")
    if titles is None:
        titles = load_codex_titles(root.parent / "session_index.jsonl")
    records = []
    for path in sorted(root.rglob("*.jsonl")):
        record = parse_codex_session(path, root, titles)
        if record:
            records.append(record)
    return records


def _normal_path(value):
    return os.path.normcase(os.path.normpath(value)) if isinstance(value, str) and value else None


def _repository_identity(cwd):
    if not cwd or not Path(cwd).is_dir():
        return {"git_root": None, "remote": None}
    try:
        root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], text=True, capture_output=True, check=True).stdout.strip()
        remote = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], text=True, capture_output=True).stdout.strip() or None
        return {"git_root": root or None, "remote": remote}
    except (OSError, subprocess.SubprocessError):
        return {"git_root": None, "remote": None}


def _normal_repo(value):
    if not isinstance(value, str):
        return None
    return value.strip().lower().removesuffix(".git").replace("git@github.com:", "https://github.com/").rstrip("/")


def classify_project(record, projects, repository_lookup=_repository_identity):
    """Classify only with deterministic repository, cwd, or alias signals."""
    result = dict(record)
    cwd = result.get("working_directory")
    identity = repository_lookup(cwd)
    result["repository"] = identity.get("remote") or identity.get("git_root")
    remote = _normal_repo(identity.get("remote"))
    repo_matches = [p for p in projects if remote and _normal_repo(p.get("repo")) == remote]
    if len(repo_matches) == 1:
        project = repo_matches[0]
        result.update(project_id=project["project_id"], classification_method="git_repository", classification_confidence="high", classification_status="classified")
        return result
    current = _normal_path(cwd)
    cwd_matches = [p for p in projects if _normal_path(p.get("working_directory")) and (current == _normal_path(p["working_directory"]) or current.startswith(_normal_path(p["working_directory"]) + os.sep))]
    if len(cwd_matches) == 1:
        project = cwd_matches[0]
        result.update(project_id=project["project_id"], classification_method="working_directory", classification_confidence="high", classification_status="classified")
        return result
    basename = os.path.basename(current) if current else ""
    alias_matches = [p for p in projects if basename and any(_normal_path(alias) == basename for alias in [p.get("project_id"), p.get("name"), *p.get("aliases", [])])]
    if len(alias_matches) == 1:
        project = alias_matches[0]
        result.update(project_id=project["project_id"], classification_method="project_alias", classification_confidence="high", classification_status="classified")
    return result


def all_projects(store):
    if hasattr(store, "list_projects"):
        return store.list_projects()
    root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["projects"], create=False)
    projects = []
    for item in store.children(root):
        if item.get("mimeType") == "application/vnd.google-apps.folder":
            project = store.get("projects", item["name"], item["name"])
            validate("project", project)
            projects.append(project)
    return projects


def import_codex_sessions(store, sessions_root=None, projects=None, repository_lookup=_repository_identity):
    projects = all_projects(store) if projects is None else projects
    records = []
    for record in discover_codex_sessions(sessions_root):
        record = classify_project(record, projects, repository_lookup)
        validate("session", record)
        store.put("sessions", record["project_id"] or UNCLASSIFIED_PROJECT_ID, record["session_id"], record)
        records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import-codex", help="Read Codex JSONL sessions and write metadata to Drive")
    importer.add_argument("--sessions-root", type=Path, default=None)
    args = parser.parse_args()
    try:
        records = import_codex_sessions(DriveRecords(build_service()), args.sessions_root)
        print(json.dumps({"imported": len(records), "provider": "codex"}, indent=2))
        return 0
    except (TaskError, OSError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
