#!/usr/bin/env python3
"""Provider-neutral session discovery with a read-only Codex adapter."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from collectors.publish_drive import build_service
from manager.runtime_bridge import all_projects as read_projects
from manager.session_identity import manager_session_key, parse_manager_session_key, session_manager_key, session_provider_identity
from manager.tasks import DriveRecords, TaskError, validate


SHORT_PROMPT_LIMIT = 1000
TITLE_LIMIT = 72
UNCLASSIFIED_PROJECT_ID = "_unclassified"
REVIEW_PROJECT_ID = "_review_queue"
PROJECT_SNAPSHOT_VERSION = 1


def _session_matches_reference(record, value):
    """Keep Codex CLI raw-ID selection compatible while accepting Manager keys."""
    return value in {record.get("session_id"), record.get("provider_session_id"), session_manager_key(record)}


@dataclass
class CanonicalSession:
    """Provider-neutral session metadata; never contains a source transcript.

    ``session_id`` is the Manager key derived only from provider identity.
    ``provider_session_id`` remains the provider-owned raw ID.  Legacy records
    without the latter field are read through ``session_provider_identity``.
    """

    session_id: str
    provider: str
    provider_session_id: str
    project_id: str | None = None
    task_id: str | None = None
    source_identifier: str | None = None
    source_path: str | None = None
    working_directory: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    model: str | None = None
    status: str | None = None
    parent_session_id: str | None = None
    title: str | None = None
    summary: str | None = None
    usage_ref: str | None = None
    resume_ref: str | None = None
    classification_method: str | None = None
    mapping_source: str | None = None
    classification_confidence: str | None = None
    classification_status: str | None = None
    content_hash: str | None = None
    conversation_label: str | None = None
    repository: str | None = None
    message_count: int | None = None
    first_user_prompt: str | None = None
    identity_header: dict | None = None
    repository_urls: list | None = None

    @property
    def provider_identity(self):
        return (self.provider, self.provider_session_id)

    def to_record(self):
        record = asdict(self)
        record["_identity_header"] = record.pop("identity_header")
        record["_repository_urls"] = record.pop("repository_urls") or []
        return record


class SessionAdapter(ABC):
    """Small provider boundary: discover raw data, parse it, then normalize it."""

    provider: str

    @abstractmethod
    def discover_raw_sessions(self, source_root=None, **kwargs):
        """Yield provider-owned raw session references without writing to them."""

    @abstractmethod
    def parse_raw_session(self, raw_session):
        """Parse one provider raw session into provider-shaped metadata."""

    @abstractmethod
    def normalize(self, parsed_session):
        """Convert provider-shaped metadata to ``CanonicalSession``."""

    def discover(self, source_root=None, **kwargs):
        sessions = []
        for raw_session in self.discover_raw_sessions(source_root, **kwargs):
            parsed = self.parse_raw_session(raw_session)
            if parsed:
                sessions.append(self.normalize(parsed).to_record())
        return sessions


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


def _parse_codex_session_raw(path, source_root, titles=None):
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
        "source_path": str(path),
        "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
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


def default_claude_sessions_root():
    return Path.home() / ".claude" / "projects"


class CodexSessionAdapter(SessionAdapter):
    """Read Codex JSONL sessions without altering Codex-owned files or indexes."""

    provider = "codex"

    def discover_raw_sessions(self, source_root=None, titles=None):
        root = Path(source_root or default_sessions_root())
        if not root.is_dir():
            raise TaskError(f"Codex sessions directory not found: {root}")
        if titles is None:
            titles = load_codex_titles(root.parent / "session_index.jsonl")
        return ({"path": path, "source_root": root, "titles": titles} for path in sorted(root.rglob("*.jsonl")))

    def parse_raw_session(self, raw_session):
        return _parse_codex_session_raw(raw_session["path"], raw_session["source_root"], raw_session["titles"])

    def normalize(self, parsed_session):
        return CanonicalSession(
            session_id=manager_session_key(self.provider, parsed_session["session_id"]),
            provider=self.provider,
            provider_session_id=parsed_session["session_id"],
            project_id=parsed_session.get("project_id"),
            task_id=parsed_session.get("task_id"),
            source_identifier=parsed_session.get("source_identifier"),
            source_path=parsed_session.get("source_path"),
            working_directory=parsed_session.get("working_directory"),
            started_at=parsed_session.get("started_at"),
            updated_at=parsed_session.get("updated_at"),
            model=parsed_session.get("model"),
            status=parsed_session.get("status"),
            parent_session_id=None,
            title=parsed_session.get("title"),
            summary=parsed_session.get("summary"),
            usage_ref=None,
            resume_ref=None,
            classification_method=parsed_session.get("classification_method"),
            mapping_source=None,
            classification_confidence=parsed_session.get("classification_confidence"),
            classification_status=parsed_session.get("classification_status"),
            content_hash=parsed_session.get("content_hash"),
            conversation_label=parsed_session.get("conversation_label"),
            repository=parsed_session.get("repository"),
            message_count=parsed_session.get("message_count"),
            first_user_prompt=parsed_session.get("first_user_prompt"),
            identity_header=parsed_session.get("_identity_header"),
            repository_urls=parsed_session.get("_repository_urls"),
        )


def parse_codex_session(path, source_root, titles=None):
    """Compatibility wrapper returning the historical metadata mapping."""
    parsed = _parse_codex_session_raw(path, source_root, titles)
    return CodexSessionAdapter().normalize(parsed).to_record() if parsed else None


def discover_codex_sessions(sessions_root=None, titles=None):
    return CodexSessionAdapter().discover(sessions_root, titles=titles)


class ClaudeSessionAdapter(SessionAdapter):
    """Read Claude Code project JSONL sessions without altering Claude files."""

    provider = "claude"

    def discover_raw_sessions(self, source_root=None):
        root = Path(source_root or default_claude_sessions_root())
        if not root.is_dir():
            raise TaskError(f"Claude projects directory not found: {root}")
        return ({"path": path, "source_root": root} for path in sorted(root.rglob("*.jsonl")))

    def parse_raw_session(self, raw_session):
        path, root = Path(raw_session["path"]), raw_session["source_root"]
        session_id = cwd = model = title = summary = parent_session_id = first_prompt = identity_header = None
        timestamps, messages, repository_urls = [], 0, []
        for item in _json_lines(path):
            timestamp = _safe_timestamp(item.get("timestamp"))
            if timestamp:
                timestamps.append(timestamp)
            if session_id is None and isinstance(item.get("sessionId"), str) and item["sessionId"]:
                session_id = item["sessionId"]
            if cwd is None and isinstance(item.get("cwd"), str):
                cwd = item["cwd"]
            if parent_session_id is None and isinstance(item.get("parentSessionId"), str):
                parent_session_id = item["parentSessionId"]
            if item.get("type") == "custom-title" and isinstance(item.get("customTitle"), str) and item["customTitle"].strip():
                title = item["customTitle"].strip()[:500]
            elif title is None and item.get("type") == "ai-title" and isinstance(item.get("aiTitle"), str) and item["aiTitle"].strip():
                title = item["aiTitle"].strip()[:500]
            if summary is None and isinstance(item.get("summary"), str) and item["summary"].strip():
                summary = item["summary"].strip()[:4000]
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            role = message.get("role")
            if item.get("type") in ("user", "assistant") and role in ("user", "assistant"):
                messages += 1
                if role == "assistant" and model is None and isinstance(message.get("model"), str):
                    model = message["model"]
                if role == "user":
                    text = _text_content(message.get("content"))
                    if first_prompt is None:
                        first_prompt = text
                    if identity_header is None:
                        identity_header = parse_identity_header(text)
                    repository_urls.extend(extract_repository_urls(text))
        if not session_id:
            return None
        try:
            source_identifier = path.relative_to(root).as_posix()
        except ValueError:
            source_identifier = str(path)
        return {
            "provider_session_id": session_id, "source_identifier": source_identifier,
            "source_path": str(path), "working_directory": cwd,
            "started_at": min(timestamps) if timestamps else None,
            "updated_at": max(timestamps) if timestamps else None,
            "model": model, "status": "unknown", "parent_session_id": parent_session_id,
            "title": title, "summary": summary, "message_count": messages,
            "first_user_prompt": first_prompt[:SHORT_PROMPT_LIMIT] if first_prompt else None,
            "_identity_header": identity_header,
            "_repository_urls": list(dict.fromkeys(repository_urls)),
            "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def normalize(self, parsed_session):
        provider_session_id = parsed_session["provider_session_id"]
        return CanonicalSession(
            session_id=manager_session_key(self.provider, provider_session_id),
            provider=self.provider, provider_session_id=provider_session_id,
            project_id=None, task_id=None,
            source_identifier=parsed_session.get("source_identifier"), source_path=parsed_session.get("source_path"),
            working_directory=parsed_session.get("working_directory"),
            started_at=parsed_session.get("started_at"), updated_at=parsed_session.get("updated_at"),
            model=parsed_session.get("model"), status=parsed_session.get("status"),
            parent_session_id=parsed_session.get("parent_session_id"), title=parsed_session.get("title"),
            summary=parsed_session.get("summary"), usage_ref=None, resume_ref=None,
            classification_method="unclassified", mapping_source=None, classification_confidence=None,
            classification_status="needs_review", content_hash=parsed_session.get("content_hash"),
            conversation_label=parsed_session.get("title"), repository=None,
            message_count=parsed_session.get("message_count"), first_user_prompt=parsed_session.get("first_user_prompt"),
            identity_header=parsed_session.get("_identity_header"), repository_urls=parsed_session.get("_repository_urls"),
        )


def discover_claude_sessions(sessions_root=None):
    return ClaudeSessionAdapter().discover(sessions_root)


def session_adapter(provider):
    adapters = {"codex": CodexSessionAdapter, "claude": ClaudeSessionAdapter}
    try:
        return adapters[provider]()
    except KeyError as exc:
        raise TaskError(f"unsupported session provider: {provider}") from exc


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
        result.update(project_id=None, classification_method="conflicting_deterministic_signals", mapping_source=None, classification_confidence=None, classification_status="needs_review")
    elif resolved:
        method, project = next(((method, project) for method, project in resolved if method == "explicit_project_header"), resolved[0])
        result.update(project_id=project["project_id"], classification_method=method, mapping_source=method, classification_confidence="high", classification_status="classified")
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


def preview_sessions(adapter, sessions_root=None, projects=None, needs_review=False, repository_lookup=_repository_identity):
    """Read one adapter and return a non-persistent preview; no Drive API is called."""
    projects = projects or []
    records = [classify_project(record, projects, repository_lookup) for record in adapter.discover(sessions_root)]
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


def preview_codex_sessions(sessions_root=None, projects=None, needs_review=False, repository_lookup=_repository_identity):
    return preview_sessions(CodexSessionAdapter(), sessions_root, projects, needs_review, repository_lookup)


def preview_claude_sessions(sessions_root=None, projects=None, needs_review=False, repository_lookup=_repository_identity):
    return preview_sessions(ClaudeSessionAdapter(), sessions_root, projects, needs_review, repository_lookup)


def prompt_snippet(record, limit=240):
    value = re.sub(r"\s+", " ", record.get("first_user_prompt") or "").strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def review_queue(records, review_records=()):
    """Build a non-persistent queue from unresolved records and manual mappings."""
    reviews = {session_provider_identity(record): record for record in review_records}
    items = []
    for record in records:
        review = reviews.get(session_provider_identity(record))
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
    key = session_manager_key(session)
    storage_key = key
    try:
        existing = store.get("session_reviews", REVIEW_PROJECT_ID, key)
        validate("session_review", existing)
    except (TaskError, KeyError):
        try:
            # Existing Codex review files were named with the raw provider ID.
            storage_key = session_provider_identity(session)[1]
            existing = store.get("session_reviews", REVIEW_PROJECT_ID, storage_key)
            validate("session_review", existing)
            if session_provider_identity(existing) != session_provider_identity(session):
                existing = None
        except (TaskError, KeyError):
            existing = None
            storage_key = key
    if existing and existing["project_id"] == project_id:
        return existing
    history = list(existing["assignment_history"]) if existing else []
    previous = existing["project_id"] if existing else session.get("project_id")
    history.append({"previous_project_id": previous, "new_project_id": project_id, "assigned_at": timestamp})
    record = {
        "session_id": existing["session_id"] if existing else key, "provider": session["provider"],
        "provider_session_id": session_provider_identity(session)[1], "project_id": project_id,
        "classification_method": "manual_review", "classification_status": "classified",
        "mapping_source": "manual_review",
        "source_identifier": session.get("source_identifier"), "assigned_at": timestamp,
        "assignment_history": history,
    }
    validate("session_review", record)
    return store.put("session_reviews", REVIEW_PROJECT_ID, storage_key, record)


def _search_text(value):
    return re.sub(r"\s+", " ", value or "").casefold().strip()


def _search_tokens(query):
    """Split a query into case-insensitive AND terms; tokens may match in any field."""
    return [token for token in _search_text(query).split(" ") if token]


def apply_manual_reviews(records, review_records=()):
    reviews = {session_provider_identity(record): record for record in review_records}
    result = []
    for record in records:
        item = dict(record)
        review = reviews.get(session_provider_identity(item))
        if review:
            item.update(project_id=review["project_id"], classification_method=review["classification_method"], mapping_source=review.get("mapping_source", review["classification_method"]), classification_status=review["classification_status"])
        result.append(item)
    return result


def search_sessions(records, query=None, project=None, task=None, status=None, since=None, review_records=()):
    """Deterministic, in-memory metadata and text search; no index is persisted."""
    since_date = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError as exc:
            raise TaskError("--since must use YYYY-MM-DD") from exc
    query_tokens = _search_tokens(query)
    project = _search_text(project)
    task = _search_text(task)
    status = _search_text(status)
    results = []
    for record in apply_manual_reviews(records, review_records):
        if project and _search_text(record.get("project_id")) != project:
            continue
        if task and task not in _search_text(record.get("task_id")):
            continue
        if status and _search_text(record.get("classification_status")) != status:
            continue
        record_date = (record.get("updated_at") or record.get("started_at") or "")[:10]
        if since_date and (not record_date or date.fromisoformat(record_date) < since_date):
            continue
        fields = ("session_id", "title", "first_user_prompt", "project_id", "task_id", "conversation_label", "provider", "working_directory", "repository", "classification_status", "started_at", "updated_at")
        joined = _search_text(" ".join(str(record.get(field) or "") for field in fields))
        if query_tokens and not all(token in joined for token in query_tokens):
            continue
        results.append({
            "session_id": record["session_id"], "date": record.get("updated_at") or record.get("started_at"),
            "project_id": record.get("project_id"), "task_id": record.get("task_id"),
            "title": record.get("title"), "prompt_snippet": prompt_snippet(record),
            "classification_status": record.get("classification_status"),
            "source_identifier": record.get("source_identifier"),
        })
    return results


def _project_records(store, area, project_id):
    try:
        return store.list_records(area, project_id)
    except (TaskError, KeyError):
        return []


def registry_sessions(store, project_ids=None):
    """Read persisted Session Registry metadata across projects; never reads provider transcripts.

    Results reflect whatever has already been imported via ``import-codex``/
    ``import-claude``; sessions not yet imported will not appear.
    """
    ids = list(project_ids) if project_ids is not None else [project["project_id"] for project in read_projects(store)]
    ordered_ids = list(dict.fromkeys([*[project_id for project_id in ids if project_id], UNCLASSIFIED_PROJECT_ID]))
    merged = {}
    for project_id in ordered_ids:
        for record in _project_records(store, "sessions", project_id):
            merged.setdefault(session_manager_key(record), record)
    return list(merged.values())


def _filter_provider(records, provider):
    if not provider or _search_text(provider) == "all":
        return records
    return [record for record in records if _search_text(record.get("provider")) == _search_text(provider)]


def related_session_metadata(store, record):
    """Bounded join: session -> task -> execution (exact link) and latest handoff (project+task).

    Execution is matched by the canonical session_id link only; handoff is
    matched by project_id + task_id, never by the free-text handoff.from_session
    field. Only whitelisted fields are returned; no transcript is read.
    """
    project_id, task_id = record.get("project_id"), record.get("task_id")
    execution = None
    if project_id:
        key = session_manager_key(record)
        for item in _project_records(store, "executions", project_id):
            if item.get("session_id") == key and (execution is None or (item.get("started_at") or "") > (execution.get("started_at") or "")):
                execution = item
    handoff = None
    if project_id and task_id:
        try:
            handoff = store.latest("handoffs", project_id, task_id)
        except (TaskError, KeyError):
            handoff = None
    return {
        "execution": {key: execution.get(key) for key in ("execution_id", "provider", "status", "started_at", "completed_at", "finished_at")} if execution else None,
        "handoff": {key: handoff.get(key) for key in ("handoff_id", "reason", "current_state", "next_action", "commits")} if handoff else None,
    }


REGISTRY_FRESHNESS_NOTE = "Results reflect the Manager's already-imported Session Registry on Drive; sessions not yet imported via 'import-codex'/'import-claude' will not appear until the next import."


def global_search_sessions(store, query=None, project=None, task=None, status=None, since=None, provider=None, project_ids=None, include_related=True):
    """Read-only search across the persisted Drive Session Registry: cross-project, cross-provider.

    Source is the Session Registry (SESSIONS/<project_id>/), not a live
    rescan of ~/.codex/sessions or ~/.claude/projects; see REGISTRY_FRESHNESS_NOTE.
    """
    records = _filter_provider(registry_sessions(store, project_ids), provider)
    reviews = list_review_records(store)
    results = search_sessions(records, query, project, task, status, since, reviews)
    if include_related:
        effective = {item["session_id"]: item for item in apply_manual_reviews(records, reviews)}
        for result in results:
            full = effective.get(result["session_id"])
            result["related"] = related_session_metadata(store, full) if full else None
    return {
        "source": "manager_import_registry",
        "freshness_note": REGISTRY_FRESHNESS_NOTE,
        "provider_filter": _search_text(provider) or "all",
        "results": results,
    }


def load_review_records_file(path):
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise TaskError("review records file must be a JSON array")
    for record in records:
        validate("session_review", record)
    return records


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


def import_sessions(store, adapter, sessions_root=None, projects=None, repository_lookup=_repository_identity, session_ids=None):
    projects = read_projects(store) if projects is None else projects
    requested = set(session_ids or [])
    records = []
    for record in adapter.discover(sessions_root):
        if requested and not any(_session_matches_reference(record, value) for value in requested):
            continue
        record = classify_project(record, projects, repository_lookup)
        record.pop("_candidate_signals", None)
        validate("session", record)
        store.put("sessions", record["project_id"] or UNCLASSIFIED_PROJECT_ID, session_manager_key(record), record)
        records.append(record)
    return records


def import_codex_sessions(store, sessions_root=None, projects=None, repository_lookup=_repository_identity, session_ids=None):
    return import_sessions(store, CodexSessionAdapter(), sessions_root, projects, repository_lookup, session_ids)


def import_claude_sessions(store, sessions_root=None, projects=None, repository_lookup=_repository_identity, session_ids=None):
    return import_sessions(store, ClaudeSessionAdapter(), sessions_root, projects, repository_lookup, session_ids)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import-codex", help="Read Codex JSONL sessions and write metadata to Drive")
    importer.add_argument("--sessions-root", type=Path, default=None)
    importer.add_argument("--session-id", action="append", default=[], help="Import only this Codex session ID (repeatable)")
    claude_importer = sub.add_parser("import-claude", help="Read Claude Code JSONL sessions and write metadata to Drive")
    claude_importer.add_argument("--sessions-root", type=Path, default=None)
    claude_importer.add_argument("--session-id", action="append", default=[], help="Import only this Claude session ID (repeatable)")
    preview = sub.add_parser("preview-codex", help="Read-only Codex session classification and title preview")
    preview.add_argument("--sessions-root", type=Path, default=None)
    preview.add_argument("--projects-file", type=Path, default=None, help="Temporary JSON project input; never persisted")
    preview.add_argument("--needs-review", action="store_true", help="Show only sessions requiring review")
    claude_preview = sub.add_parser("preview-claude", help="Read-only Claude session classification and title preview")
    claude_preview.add_argument("--sessions-root", type=Path, default=None)
    claude_preview.add_argument("--projects-file", type=Path, default=None, help="Temporary JSON project input; never persisted")
    claude_preview.add_argument("--needs-review", action="store_true", help="Show only sessions requiring review")
    sub.add_parser("export-project-preview", help="Read Drive projects and emit a temporary preview snapshot to stdout")
    review_list = sub.add_parser("review-list", help="List unresolved Codex sessions; manual mappings are read from Drive")
    review_list.add_argument("--sessions-root", type=Path, default=None)
    review_list.add_argument("--projects-file", type=Path, default=None)
    review_list.add_argument("--provider", choices=("codex", "claude"), default="codex")
    review_assign = sub.add_parser("review-assign", help="Assign one Codex session to a valid project in Drive")
    review_assign.add_argument("session_id"); review_assign.add_argument("project_id")
    review_assign.add_argument("--sessions-root", type=Path, default=None)
    review_assign.add_argument("--projects-file", type=Path, default=None)
    review_assign.add_argument("--provider", choices=("codex", "claude"), default="codex")
    search = sub.add_parser("search", help="Read-only deterministic Codex session search (live local rescan; unchanged)")
    search.add_argument("--query"); search.add_argument("--project"); search.add_argument("--task")
    search.add_argument("--status"); search.add_argument("--since")
    search.add_argument("--sessions-root", type=Path, default=None)
    search.add_argument("--projects-file", type=Path, default=None)
    search.add_argument("--reviews-file", type=Path, default=None, help="Temporary exported manual review records")
    search_registry = sub.add_parser("search-registry", help="Read-only search across the persisted Drive Session Registry; cross-project, cross-provider. Reflects already-imported sessions only, not a live rescan.")
    search_registry.add_argument("--query"); search_registry.add_argument("--project"); search_registry.add_argument("--task")
    search_registry.add_argument("--status"); search_registry.add_argument("--since")
    search_registry.add_argument("--provider", choices=("codex", "claude", "all"), default="all")
    args = parser.parse_args()
    try:
        if args.command == "export-project-preview":
            print(json.dumps(project_preview_snapshot(read_projects(DriveRecords(build_service()))), indent=2))
        elif args.command == "search":
            projects = load_preview_projects(args.projects_file) if args.projects_file else []
            records = [classify_project(record, projects) for record in discover_codex_sessions(args.sessions_root)]
            reviews = load_review_records_file(args.reviews_file) if args.reviews_file else []
            print(json.dumps(search_sessions(records, args.query, args.project, args.task, args.status, args.since, reviews), indent=2))
        elif args.command == "search-registry":
            store = DriveRecords(build_service())
            result = global_search_sessions(store, args.query, args.project, args.task, args.status, args.since, args.provider)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command in ("review-list", "review-assign"):
            store = DriveRecords(build_service())
            projects = load_preview_projects(args.projects_file) if args.projects_file else read_projects(store)
            records = [classify_project(record, projects) for record in session_adapter(args.provider).discover(args.sessions_root)]
            if args.command == "review-list":
                print(json.dumps(review_queue(records, list_review_records(store)), indent=2))
            else:
                session = next((record for record in records if _session_matches_reference(record, args.session_id)), None)
                if not session:
                    raise TaskError(f"Codex session not found: {args.session_id}")
                from manager.tasks import now_iso
                print(json.dumps(assign_review(store, session, args.project_id, projects, now_iso()), indent=2))
        elif args.command == "preview-codex":
            projects = load_preview_projects(args.projects_file) if args.projects_file else []
            print(json.dumps(preview_codex_sessions(args.sessions_root, projects, args.needs_review), indent=2))
        elif args.command == "preview-claude":
            projects = load_preview_projects(args.projects_file) if args.projects_file else []
            print(json.dumps(preview_claude_sessions(args.sessions_root, projects, args.needs_review), indent=2))
        elif args.command == "import-claude":
            records = import_claude_sessions(DriveRecords(build_service()), args.sessions_root, session_ids=args.session_id)
            print(json.dumps({"imported": len(records), "provider": "claude"}, indent=2))
        else:
            records = import_codex_sessions(DriveRecords(build_service()), args.sessions_root, session_ids=args.session_id)
            print(json.dumps({"imported": len(records), "provider": "codex"}, indent=2))
        return 0
    except (TaskError, OSError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
