"""Ideas domain model and Cloud-first Drive SSOT store for ADM Dashboard."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATUS_PENDING = "pending"       # 待立案
STATUS_CONFIRMED = "confirmed"   # 已确认
STATUS_CONVERTED = "converted"   # 已立案
STATUS_DROPPED = "dropped"       # 已放弃

STATUS_DISPLAY_NAMES = {
    STATUS_PENDING: "待立案",
    STATUS_CONFIRMED: "已确认",
    STATUS_CONVERTED: "已立案",
    STATUS_DROPPED: "已放弃",
}

STATUS_ICONS = {
    STATUS_PENDING: "💡",
    STATUS_CONFIRMED: "✨",
    STATUS_CONVERTED: "🚀",
    STATUS_DROPPED: "📦",
}

ROOT_FOLDER_ID = "1pXvl8BglU05ZrXMHIVIDyK-lOWNShXSO"
MIME_JSON = "application/json"
MIME_FOLDER = "application/vnd.google-apps.folder"


@dataclass
class IdeaItem:
    idea_id: str
    title: str
    description: str
    status: str = STATUS_PENDING
    priority: str = "medium"  # "high" | "medium" | "low"
    project_id: str = "Unassigned"
    milestone_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: str = ""
    source: str = "User Chat"
    decision_note: str = ""
    converted_at: Optional[str] = None
    dropped_at: Optional[str] = None
    drop_reason: Optional[str] = None
    drop_problem: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> IdeaItem:
        return cls(
            idea_id=str(data.get("idea_id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", STATUS_PENDING)),
            priority=str(data.get("priority", "medium")),
            project_id=str(data.get("project_id") or "Unassigned"),
            milestone_id=data.get("milestone_id"),
            task_id=data.get("task_id"),
            created_at=str(data.get("created_at", "")),
            source=str(data.get("source", "User Chat")),
            decision_note=str(data.get("decision_note", "")),
            converted_at=data.get("converted_at"),
            dropped_at=data.get("dropped_at"),
            drop_reason=data.get("drop_reason"),
            drop_problem=data.get("drop_problem"),
        )


class IdeasStore:
    """Cloud-first Ideas backing store using Google Drive SSOT with local cache fallback."""

    def __init__(self, drive_service: Any = None, local_file_path: Optional[str] = None):
        self.drive_service = drive_service
        if local_file_path:
            self.local_file_path = Path(local_file_path)
        else:
            home = os.environ.get("AI_MANAGER_HOME") or os.path.expanduser("~/.ai-development-manager")
            self.local_file_path = Path(home) / "ideas.json"
        self._ideas: List[IdeaItem] = []
        self.is_degraded: bool = drive_service is None
        self.last_error: Optional[str] = None
        self.load()

    def _get_drive_folder(self, parent_id: str, name: str, create: bool = True) -> Optional[str]:
        if not self.drive_service:
            return None
        files_client = self.drive_service.files()
        query = f"'{parent_id}' in parents and name='{name}' and mimeType='{MIME_FOLDER}' and trashed=false"
        res = files_client.list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        if not create:
            return None
        created = files_client.create(
            body={"name": name, "parents": [parent_id], "mimeType": MIME_FOLDER},
            fields="id"
        ).execute()
        return created.get("id")

    def load(self) -> List[IdeaItem]:
        """Load ideas from Drive SSOT; fallback to local cache if unavailable."""
        self._ideas = []
        self.last_error = None

        if self.drive_service:
            try:
                ideas_root_id = self._get_drive_folder(ROOT_FOLDER_ID, "IDEAS", create=False)
                if not ideas_root_id:
                    self._ideas = []
                    self.is_degraded = False
                    self._save_local_cache()
                    return self._ideas

                files_client = self.drive_service.files()
                q_folders = f"'{ideas_root_id}' in parents and mimeType='{MIME_FOLDER}' and trashed=false"
                res_folders = files_client.list(q=q_folders, spaces="drive", fields="files(id, name)").execute()
                proj_folders = res_folders.get("files", [])

                loaded_dict: Dict[str, IdeaItem] = {}
                duplicates_found = []

                for pf in proj_folders:
                    pf_id = pf["id"]
                    q_files = f"'{pf_id}' in parents and mimeType='{MIME_JSON}' and trashed=false"
                    res_files = files_client.list(q=q_files, spaces="drive", fields="files(id, name)").execute()
                    for f in res_files.get("files", []):
                        raw = files_client.get_media(fileId=f["id"]).execute()
                        doc = json.loads(raw.decode("utf-8"))
                        item = IdeaItem.from_dict(doc)
                        if item.idea_id in loaded_dict:
                            duplicates_found.append(item.idea_id)
                            existing = loaded_dict[item.idea_id]
                            # Deterministic resolution: if new one has assigned project_id, prefer it
                            if item.project_id != "Unassigned" and existing.project_id == "Unassigned":
                                loaded_dict[item.idea_id] = item
                        else:
                            loaded_dict[item.idea_id] = item

                if duplicates_found:
                    self.last_error = f"Consistency Notice: Duplicate idea records detected for IDs {list(set(duplicates_found))}. Resolved deterministically."

                self._ideas = list(loaded_dict.values())
                self.is_degraded = False
                self._save_local_cache()
                return self._ideas
            except Exception as exc:
                self.last_error = f"Failed to load Ideas from Drive SSOT: {exc}"
                self.is_degraded = True
                self._load_local_cache()
                return self._ideas
        else:
            self.is_degraded = True
            self._load_local_cache()
            return self._ideas

    def _load_local_cache(self) -> None:
        if self.local_file_path.exists():
            try:
                raw = json.loads(self.local_file_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._ideas = [IdeaItem.from_dict(d) for d in raw]
                    return
            except Exception as exc:
                self.last_error = f"Corrupt local ideas cache: {exc}"
        self._ideas = []

    def _save_local_cache(self) -> None:
        try:
            self.local_file_path.parent.mkdir(parents=True, exist_ok=True)
            raw = [i.to_dict() for i in self._ideas]
            self.local_file_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self.last_error = f"Local cache write failed: {exc}"
            raise RuntimeError(f"Failed to write local ideas cache: {exc}") from exc

    def _write_to_drive(self, idea: IdeaItem) -> None:
        if not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")

        files_client = self.drive_service.files()
        ideas_root_id = self._get_drive_folder(ROOT_FOLDER_ID, "IDEAS", create=True)
        target_proj = idea.project_id or "Unassigned"
        target_folder_id = self._get_drive_folder(ideas_root_id, target_proj, create=True)

        filename = f"{idea.idea_id}.json"
        query = f"name='{filename}' and trashed=false"
        res = files_client.list(q=query, spaces="drive", fields="files(id, name, parents)").execute()
        matching_files = res.get("files", [])

        body_content = json.dumps(idea.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")

        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(body_content, mimetype=MIME_JSON, resumable=False)

        if matching_files:
            primary = matching_files[0]
            file_id = primary["id"]
            parents = primary.get("parents", [])

            if target_folder_id not in parents:
                remove_parents = ",".join(parents) if parents else None
                update_kwargs = {
                    "fileId": file_id,
                    "media_body": media,
                    "addParents": target_folder_id,
                }
                if remove_parents:
                    update_kwargs["removeParents"] = remove_parents
                files_client.update(**update_kwargs).execute()
            else:
                files_client.update(fileId=file_id, media_body=media).execute()

            # Remove any orphan duplicates
            for extra in matching_files[1:]:
                try:
                    files_client.delete(fileId=extra["id"]).execute()
                except Exception:
                    pass
        else:
            files_client.create(
                body={"name": filename, "parents": [target_folder_id], "mimeType": MIME_JSON},
                media_body=media,
                fields="id"
            ).execute()

    def list_ideas(self) -> List[IdeaItem]:
        return list(self._ideas)

    def get_by_id(self, idea_id: str) -> Optional[IdeaItem]:
        for i in self._ideas:
            if i.idea_id == idea_id:
                return i
        return None

    def add_idea(self, idea: IdeaItem) -> None:
        self.last_error = None
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")

        if not idea.title.strip():
            raise ValueError("Idea title cannot be empty.")

        try:
            self._write_to_drive(idea)
            self.is_degraded = False
        except Exception as exc:
            self.last_error = f"Drive SSOT write failed: {exc}"
            raise RuntimeError(f"Failed to persist Idea to Drive SSOT: {exc}") from exc

        self._ideas.append(idea)
        self._save_local_cache()

    def update_idea(self, idea: IdeaItem) -> None:
        self.last_error = None
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")

        try:
            self._write_to_drive(idea)
            self.is_degraded = False
        except Exception as exc:
            self.last_error = f"Drive SSOT write failed: {exc}"
            raise RuntimeError(f"Failed to update Idea in Drive SSOT: {exc}") from exc

        for idx, existing in enumerate(self._ideas):
            if existing.idea_id == idea.idea_id:
                self._ideas[idx] = idea
                self._save_local_cache()
                return
        self._ideas.append(idea)
        self._save_local_cache()

    def confirm_idea(self, idea_id: str, note: str = "") -> None:
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")
        idea = self.get_by_id(idea_id)
        if not idea:
            raise KeyError(f"Idea not found: {idea_id}")
        idea.status = STATUS_CONFIRMED
        if note:
            idea.decision_note = note
        self.update_idea(idea)

    def convert_idea(self, idea_id: str, project_id: str, milestone_id: Optional[str] = None, task_id: Optional[str] = None, note: str = "") -> None:
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")
        idea = self.get_by_id(idea_id)
        if not idea:
            raise KeyError(f"Idea not found: {idea_id}")
        if not project_id or project_id == "Unassigned":
            raise ValueError("Converted idea must be bound to a valid project_id.")
        idea.status = STATUS_CONVERTED
        idea.project_id = project_id
        idea.milestone_id = milestone_id
        idea.task_id = task_id
        idea.converted_at = datetime.now(timezone.utc).isoformat()
        if note:
            idea.decision_note = note
        self.update_idea(idea)

    def drop_idea(self, idea_id: str, drop_reason: str, drop_problem: str, note: str = "") -> None:
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")
        idea = self.get_by_id(idea_id)
        if not idea:
            raise KeyError(f"Idea not found: {idea_id}")
        if not drop_reason or not drop_reason.strip():
            raise ValueError("drop_reason is required to drop an idea.")
        if not drop_problem or not drop_problem.strip():
            raise ValueError("drop_problem is required to drop an idea.")

        idea.status = STATUS_DROPPED
        idea.dropped_at = datetime.now(timezone.utc).isoformat()
        idea.drop_reason = drop_reason.strip()
        idea.drop_problem = drop_problem.strip()
        if note:
            idea.decision_note = note
        self.update_idea(idea)

    def restore_idea(self, idea_id: str, note: str = "") -> None:
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Ideas are read-only until cloud connection is restored.")
        idea = self.get_by_id(idea_id)
        if not idea:
            raise KeyError(f"Idea not found: {idea_id}")
        idea.status = STATUS_PENDING
        if note:
            idea.decision_note = note
        self.update_idea(idea)


def get_default_ideas_store(drive_service: Any = None) -> IdeasStore:
    return IdeasStore(drive_service=drive_service)


def group_ideas_by_status(ideas: List[IdeaItem]) -> Dict[str, List[IdeaItem]]:
    grouped = {
        STATUS_PENDING: [],
        STATUS_CONFIRMED: [],
        STATUS_CONVERTED: [],
        STATUS_DROPPED: [],
    }
    for item in ideas:
        st = item.status if item.status in grouped else STATUS_PENDING
        grouped[st].append(item)
    return grouped


def get_ideas_summary(ideas: List[IdeaItem]) -> Dict[str, int]:
    grouped = group_ideas_by_status(ideas)
    return {
        "total": len(ideas),
        "pending": len(grouped[STATUS_PENDING]),
        "confirmed": len(grouped[STATUS_CONFIRMED]),
        "converted": len(grouped[STATUS_CONVERTED]),
        "dropped": len(grouped[STATUS_DROPPED]),
    }
