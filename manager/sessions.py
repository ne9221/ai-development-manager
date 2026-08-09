#!/usr/bin/env python3
"""Read-only Codex session discovery and Drive-backed session registry."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords, ROOT_FOLDER_ID, ROOT_FOLDERS, TaskError, validate


SHORT_PROMPT_LIMIT = 1000
TITLE_LIMIT = 72
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


def candidate_title(record):
    """Return a short, deterministic label without summarizing or changing source data."""
    if isinstance(record.get("title"), str) and record["title"].strip():
        value = re.sub(r"\s+", " ", record["title"]).strip()
        return value if len(value) <= TITLE_LIMIT else value[:TITLE_LIMIT - 1].rstrip() + "…"
    prompt = record.get("first_user_prompt")
    if not isinstance(prompt, str):
        return None
    lines = [line.strip() for line in prompt.splitlines()]
    lines = [line for line in lines if line and not re.match(r"^(AI|Project|Task|Mode|Effort|Conversation|Run/Session):", line, re.I)]
    value = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not value:
        return None
    return value if len(value) <= TITLE_LIMIT else value[:TITLE_LIMIT - 1].rstrip() + "…"


def display_working_directory(value):
    if not isinstance(value, str) or not value:
        return None
    parts = [part for part in re.split(r"[\\\\/]", value) if part]
    return "/".join(parts[-2:]) if parts else value


def preview_codex_sessions(sessions_root=None, projects=None, needs_review=False, repository_lookup=_repository_identity):
    """Read sessions and return a non-persistent preview; no Drive API is called."""
    projects = projects or []
    records = [classify_project(record, projects, repository_lookup) for record in discover_codex_sessions(sessions_root)]
    grouped = {}
    for record in records:
        project_id = record.get("project_id") or "_unclassified"
        grouped[project_id] = grouped.get(project_id, 0) + 1
    shown = [record for record in records if not needs_review or record["classification_status"] == "needs_review"]
    return {
        "total_sessions": len(records),
        "classified_sessions": sum(record["classification_status"] == "classified" for record in records),
        "needs_review_sessions": sum(record["classification_status"] == "needs_review" for record in records),
        "projects": [{"project_id": key, "session_count": grouped[key]} for key in sorted(grouped)],
        "sessions": [{
            "session_id": record["session_id"], "project_id": record["project_id"],
            "started_at": record["started_at"], "updated_at": record["updated_at"],
            "existing_title": record["title"], "candidate_title": candidate_title(record),
            "classification_method": record["classification_method"],
            "classification_status": record["classification_status"],
            "working_directory": display_working_directory(record["working_directory"]),
        } for record in shown],
    }


def load_preview_projects(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise TaskError("preview projects file must be a JSON array of project records")
    return document


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


def import_codex_sessions(store, sessions_root=None, projects=None, repository_lookup=_repository_identity, session_ids=None):
    projects = all_projects(store) if projects is None else projects
    requested = set(session_ids or [])
    records = []
    for record in discover_codex_sessions(sessions_root):
        if requested and record["session_id"] not in requested:
            continue
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
    importer.add_argument("--session-id", action="append", default=[], help="Import only this Codex session ID (repeatable)")
    preview = sub.add_parser("preview-codex", help="Read-only Codex session classification and title preview")
    preview.add_argument("--sessions-root", type=Path, default=None)
    preview.add_argument("--projects-file", type=Path, default=None, help="Temporary JSON project input; never persisted")
    preview.add_argument("--needs-review", action="store_true", help="Show only sessions requiring review")
    args = parser.parse_args()
    try:
        if args.command == "preview-codex":
            projects = load_preview_projects(args.projects_file) if args.projects_file else []
            print(json.dumps(preview_codex_sessions(args.sessions_root, projects, args.needs_review), indent=2))
        else:
            records = import_codex_sessions(DriveRecords(build_service()), args.sessions_root, session_ids=args.session_id)
            print(json.dumps({"imported": len(records), "provider": "codex"}, indent=2))
        return 0
    except (TaskError, OSError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
