"""Ideas domain model, persistence, and summary helpers for ADM Dashboard."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


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
            title=str(data.get("title", "Untitled Idea")),
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


def get_sample_ideas() -> List[IdeaItem]:
    """Provide realistic initial seed ideas for ADM."""
    return [
        IdeaItem(
            idea_id="IDEA-001",
            title="Auto-capture Ideas from AI conversation transcripts",
            description="Listen to session transcripts for phrases like '以后要做', '之后加', '记一下' and automatically extract candidate ideas into 待立案.",
            status=STATUS_PENDING,
            priority="high",
            project_id="ai-development-manager",
            created_at="2026-08-20T10:15:00Z",
            source="Chat with Antigravity",
            decision_note="Needs NLP filter to avoid false positives from ordinary task explanations.",
        ),
        IdeaItem(
            idea_id="IDEA-002",
            title="Mobile / WeChat notification bridge for urgent blockers",
            description="Send webhook / Telegram / WeChat message when an active AI Fleet task hits attention/stale or requires manual quota replenishment.",
            status=STATUS_PENDING,
            priority="medium",
            project_id="Unassigned",
            created_at="2026-08-19T14:30:00Z",
            source="User note",
            decision_note="Evaluate privacy and token security before sending project names.",
        ),
        IdeaItem(
            idea_id="IDEA-003",
            title="Dark mode high-contrast color scheme toggle in Dashboard",
            description="Provide an accessibility toggle for OLED displays and high-contrast ambient environments.",
            status=STATUS_PENDING,
            priority="low",
            project_id="ai-development-manager",
            created_at="2026-08-18T09:00:00Z",
            source="User feedback",
            decision_note="CSS custom variables ready; needs UI switch in Settings.",
        ),
        IdeaItem(
            idea_id="IDEA-004",
            title="Fleet Quota Auto-switching on reset window boundary",
            description="Automatically schedule pending tasks to execute when Claude 5h reset or Codex weekly reset boundary arrives.",
            status=STATUS_CONFIRMED,
            priority="high",
            project_id="ai-development-manager",
            milestone_id="M2-Quota-Routing",
            created_at="2026-08-17T11:20:00Z",
            source="Sprint planning",
            decision_note="Confirmed for M2 milestone. Needs deterministic reset time forecasting.",
        ),
        IdeaItem(
            idea_id="IDEA-005",
            title="Daily brief export to Markdown & PDF report",
            description="One-click export of daily brief and cost/quota trends for weekly review meetings.",
            status=STATUS_CONFIRMED,
            priority="medium",
            project_id="Unassigned",
            created_at="2026-08-16T16:45:00Z",
            source="Operations team",
            decision_note="Confirmed. Will integrate with Streamlit download button.",
        ),
        IdeaItem(
            idea_id="IDEA-006",
            title="Windows Native Launcher & System Tray Entry (P0)",
            description="One-click Windows login launcher with system tray icon and automatic dashboard/task readiness polling.",
            status=STATUS_CONVERTED,
            priority="high",
            project_id="ai-development-manager",
            milestone_id="M1-Windows-Launcher",
            task_id="adm-windows-launcher-p0",
            created_at="2026-08-15T08:00:00Z",
            source="P0 User Requirement",
            decision_note="Converted into formal Project M1 Milestone task. Production fast-forward merged on 2026-08-21.",
            converted_at="2026-08-21T01:37:00Z",
        ),
        IdeaItem(
            idea_id="IDEA-007",
            title="Direct Electron app wrapping for ADM desktop",
            description="Bundle the entire python dashboard into a heavy Electron standalone binary.",
            status=STATUS_DROPPED,
            priority="low",
            project_id="ai-development-manager",
            created_at="2026-08-14T10:00:00Z",
            source="Architecture spike",
            decision_note="Dropped in favor of native lightweight .NET NotifyIcon + Streamlit browser launcher. Saves 300MB binary footprint.",
            dropped_at="2026-08-20T18:00:00Z",
            drop_reason="Overkill for local dashboard; violates Ponytail minimal-change rule",
            drop_problem="Heavy packaging maintenance, binary distribution complexity",
        ),
    ]


class IdeasStore:
    """JSON and in-memory backing store for Ideas."""

    def __init__(self, file_path: Optional[str] = None):
        if file_path:
            self.file_path = Path(file_path)
        else:
            home = os.environ.get("AI_MANAGER_HOME") or os.path.expanduser("~/.ai-development-manager")
            self.file_path = Path(home) / "ideas.json"
        self._ideas: List[IdeaItem] = []
        self.load()

    def load(self) -> None:
        if self.file_path.exists():
            try:
                raw = json.loads(self.file_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._ideas = [IdeaItem.from_dict(d) for d in raw]
                    return
            except Exception:
                pass
        # Default fallback
        self._ideas = get_sample_ideas()

    def save(self) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            raw = [i.to_dict() for i in self._ideas]
            self.file_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def list_ideas(self) -> List[IdeaItem]:
        return list(self._ideas)

    def get_by_id(self, idea_id: str) -> Optional[IdeaItem]:
        for i in self._ideas:
            if i.idea_id == idea_id:
                return i
        return None

    def add_idea(self, idea: IdeaItem) -> None:
        self._ideas.append(idea)
        self.save()

    def update_idea(self, idea: IdeaItem) -> None:
        for idx, existing in enumerate(self._ideas):
            if existing.idea_id == idea.idea_id:
                self._ideas[idx] = idea
                self.save()
                return
        self.add_idea(idea)


def get_default_ideas_store() -> IdeasStore:
    return IdeasStore()


def group_ideas_by_status(ideas: List[IdeaItem]) -> Dict[str, List[IdeaItem]]:
    """Group list of ideas into four fixed status categories."""
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
    """Compute summary counts for Overview card."""
    grouped = group_ideas_by_status(ideas)
    return {
        "total": len(ideas),
        "pending": len(grouped[STATUS_PENDING]),
        "confirmed": len(grouped[STATUS_CONFIRMED]),
        "converted": len(grouped[STATUS_CONVERTED]),
        "dropped": len(grouped[STATUS_DROPPED]),
    }
