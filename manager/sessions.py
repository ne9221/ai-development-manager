#!/usr/bin/env python3
"""Read-only Codex session discovery and Drive-backed session registry."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from collectors.publish_drive import build_service
from manager.runtime_bridge import all_projects as read_projects
from manager.tasks import DriveRecords, TaskError, validate


SHORT_PROMPT_LIMIT = 1000
TITLE_LIMIT = 72
UNCLASSIFIED_PROJECT_ID = "_unclassified"
REVIEW_PROJECT_ID = "_review_queue"
PROJECT_SNAPSHOT_VERSION = 1


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


def parse_identity_header(text):
    """Parse the explicit AI work identity block, never ordinary prose."""
    if not isinstance(text, str):
        return None
    values = {}
    names = {"ai": "ai", "project": "project", "task": "task", "conversation": "conversation", "run/session": "run_session"}
    for line in text.splitlines():
        match = re.fullmatch(r"(AI|Project|Task|Conversation|Run/Session):[ \t]*(.+?)[ \t]*", line, re.I)
        if not match:
            continue
        key, value = names[match.group(1).lower()], match.group(2)
        if key in values and values[key] != value:
            return None
        values[key] = value
    return values if all(values.get(key) for key in ("ai", "project", "task")) else None


def extract_repository_urls(text):
    if not isinstance(text, str):
        return []
    values = re.findall(r"(?:https?://[^\s<>\]\[)]+|git@github\.com:[^\s<>\]\[)]+)", text, re.I)
    return list(dict.fromkeys(value.rstrip(".,;:") for value in values))


def parse_codex_session(path, source_root, titles=None):
    """Return reliable metadata from one Codex JSONL file without changing it."""
    path = Path(path)
    metadata = None
    timestamps, messages, model, first_prompt, identity_header, repository_urls = [], 0, None, None, None, []
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
            if payload.get("role") == "user" and identity_header is None:
                identity_header = parse_identity_header(_text_content(payload.get("content")))
            if payload.get("role") == "user":
                repository_urls.extend(extract_repository_urls(_text_content(payload.get("content"))))
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
        "_identity_header": identity_header,
        "_repository_urls": list(dict.fromkeys(repository_urls)),
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
        root_result = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], text=True, encoding="utf-8", errors="replace", capture_output=True, check=True)
        remote_result = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], text=True, encoding="utf-8", errors="replace", capture_output=True)
        root = (root_result.stdout or "").strip()
        remote = (remote_result.stdout or "").strip() or None
        return {"git_root": root or None, "remote": remote}
    except (OSError, subprocess.SubprocessError):
        return {"git_root": None, "remote": None}


def _normal_repo(value):
    if not isinstance(value, str):
        return None
    return value.strip().lower().removesuffix(".git").replace("git@github.com:", "https://github.com/").rstrip("/")


def _header_matches(project_ref, projects):
    needle = project_ref.strip().casefold() if isinstance(project_ref, str) else ""
    return [project for project in projects if needle and any(isinstance(value, str) and value.strip().casefold() == needle for value in [project.get("project_id"), project.get("name"), *project.get("aliases", [])])]


def _working_directory_matches(value, projects):
    current = _normal_path(value)
    return [project for project in projects if current and _normal_path(project.get("working_directory")) and (current == _normal_path(project["working_directory"]) or current.startswith(_normal_path(project["working_directory"]) + os.sep))]


def _repo_name(value):
    normalized = _normal_repo(value)
    return normalized.rsplit("/", 1)[-1] if normalized else None


def classify_project(record, projects, repository_lookup=_repository_identity):
    """Classify only with deterministic repository, cwd, or alias signals."""
    result = dict(record)
    header = result.pop("_identity_header", None)
    repository_urls = result.pop("_repository_urls", [])
    if header:
        result["task_id"] = header["task"]
        if header.get("conversation"):
            result["conversation_label"] = header["conversation"][:500]
    cwd = result.get("working_directory")
    identity = repository_lookup(cwd)
    result["repository"] = identity.get("remote") or identity.get("git_root")
    remote = _normal_repo(identity.get("remote"))
    repo_matches = [p for p in projects if remote and _normal_repo(p.get("repo")) == remote]
    cwd_matches = _working_directory_matches(cwd, projects)
    root_matches = _working_directory_matches(identity.get("git_root"), projects)
    root_name = _repo_name(identity.get("git_root"))
    root_name_matches = [p for p in projects if root_name and _repo_name(p.get("repo")) == root_name]
    message_repo_matches = []
    for url in repository_urls:
        matches = [p for p in projects if _normal_repo(p.get("repo")) == _normal_repo(url)]
        if len(matches) == 1:
            message_repo_matches.append(matches[0])
    current = _normal_path(cwd)
    basename = os.path.basename(current) if current else ""
    alias_matches = [p for p in projects if basename and any(_normal_path(alias) == basename for alias in [p.get("project_id"), p.get("name"), *p.get("aliases", [])])]
    header_matches = _header_matches(header.get("project"), projects) if header else []
    signals = [("git_repository", repo_matches), ("git_root", root_matches), ("git_repository_name", root_name_matches), ("working_directory", cwd_matches), ("project_alias", alias_matches), ("session_repository_url", message_repo_matches), ("explicit_project_header", header_matches)]
    result["_candidate_signals"] = [{"method": method, "project_ids": [project["project_id"] for project in matches]} for method, matches in signals if matches]
    resolved = [(method, matches[0]) for method, matches in signals if len(matches) == 1]
    project_ids = {project["project_id"] for _, project in resolved}
    if len(project_ids) > 1:
        result.update(project_id=None, classification_method="conflicting_deterministic_signals", classification_confidence=None, classification_status="needs_review")
    elif resolved:
        method, project = next(((method, project) for method, project in resolved if method == "explicit_project_header"), resolved[0])
        result.update(project_id=project["project_id"], classification_method=method, classification_confidence="high", classification_status="classified")
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


def prompt_snippet(record, limit=240):
    value = re.sub(r"\s+", " ", record.get("first_user_prompt") or "").strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def review_queue(records, review_records=()):
    """Build a non-persistent queue from unresolved records and manual mappings."""
    reviews = {record["session_id"]: record for record in review_records}
    items = []
    for record in records:
        review = reviews.get(record["session_id"])
        if review or record.get("classification_status") != "needs_review":
            continue
        items.append({
            "session_id": record["session_id"], "provider": record["provider"],
            "started_at": record["started_at"], "updated_at": record["updated_at"],
            "title": record["title"], "prompt_snippet": prompt_snippet(record),
            "working_directory": display_working_directory(record["working_directory"]),
            "repository": record["repository"],
            "classification_reason": record["classification_method"],
            "candidate_deterministic_signals": record.get("_candidate_signals", []),
        })
    return items


def list_review_records(store):
    try:
        records = store.list_records("session_reviews", REVIEW_PROJECT_ID)
    except (TaskError, KeyError):
        return []
    for record in records:
        validate("session_review", record)
    return records


def assign_review(store, session, project_id, projects, timestamp):
    matches = [project for project in projects if project.get("project_id") == project_id]
    if len(matches) != 1:
        raise TaskError(f"manual assignment requires one valid project_id: {project_id}")
    try:
        existing = store.get("session_reviews", REVIEW_PROJECT_ID, session["session_id"])
        validate("session_review", existing)
    except (TaskError, KeyError):
        existing = None
    if existing and existing["project_id"] == project_id:
        return existing
    history = list(existing["assignment_history"]) if existing else []
    previous = existing["project_id"] if existing else session.get("project_id")
    history.append({"previous_project_id": previous, "new_project_id": project_id, "assigned_at": timestamp})
    record = {
        "session_id": session["session_id"], "provider": session["provider"], "project_id": project_id,
        "classification_method": "manual_review", "classification_status": "classified",
        "source_identifier": session.get("source_identifier"), "assigned_at": timestamp,
        "assignment_history": history,
    }
    validate("session_review", record)
    return store.put("session_reviews", REVIEW_PROJECT_ID, record["session_id"], record)


def project_preview_snapshot(projects):
    """Create the smallest non-secret project input needed by session preview."""
    document = {"snapshot_type": "project_preview", "version": PROJECT_SNAPSHOT_VERSION, "projects": [{
        "project_id": project.get("project_id"), "name": project.get("name"),
        "aliases": project.get("aliases", []), "repo": project.get("repo"),
        "working_directory": project.get("working_directory"),
    } for project in projects]}
    validate("project_preview", document)
    return document


def load_preview_projects(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    validate("project_preview", document)
    return document["projects"]


def import_codex_sessions(store, sessions_root=None, projects=None, repository_lookup=_repository_identity, session_ids=None):
    projects = read_projects(store) if projects is None else projects
    requested = set(session_ids or [])
    records = []
    for record in discover_codex_sessions(sessions_root):
        if requested and record["session_id"] not in requested:
            continue
        record = classify_project(record, projects, repository_lookup)
        record.pop("_candidate_signals", None)
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
    sub.add_parser("export-project-preview", help="Read Drive projects and emit a temporary preview snapshot to stdout")
    review_list = sub.add_parser("review-list", help="List unresolved Codex sessions; manual mappings are read from Drive")
    review_list.add_argument("--sessions-root", type=Path, default=None)
    review_list.add_argument("--projects-file", type=Path, default=None)
    review_assign = sub.add_parser("review-assign", help="Assign one Codex session to a valid project in Drive")
    review_assign.add_argument("session_id"); review_assign.add_argument("project_id")
    review_assign.add_argument("--sessions-root", type=Path, default=None)
    review_assign.add_argument("--projects-file", type=Path, default=None)
    args = parser.parse_args()
    try:
        if args.command == "export-project-preview":
            print(json.dumps(project_preview_snapshot(read_projects(DriveRecords(build_service()))), indent=2))
        elif args.command in ("review-list", "review-assign"):
            store = DriveRecords(build_service())
            projects = load_preview_projects(args.projects_file) if args.projects_file else read_projects(store)
            records = [classify_project(record, projects) for record in discover_codex_sessions(args.sessions_root)]
            if args.command == "review-list":
                print(json.dumps(review_queue(records, list_review_records(store)), indent=2))
            else:
                session = next((record for record in records if record["session_id"] == args.session_id), None)
                if not session:
                    raise TaskError(f"Codex session not found: {args.session_id}")
                from manager.tasks import now_iso
                print(json.dumps(assign_review(store, session, args.project_id, projects, now_iso()), indent=2))
        elif args.command == "preview-codex":
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
