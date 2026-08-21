"""Read-only Action Center snapshot for the Windows tray; Drive remains the SSOT."""
import json
from pathlib import Path

from collectors.publish_drive import build_service
from manager.actions import ActionsStore, STATUS_ACKNOWLEDGED, STATUS_OPEN


def actionable_snapshot(actions):
    items = [a for a in actions if a.status in (STATUS_OPEN, STATUS_ACKNOWLEDGED) and a.need_user_action]
    rank = {"high": 3, "medium": 2, "low": 1}
    return {"state": "ok", "count": len(items), "highest_severity": max((a.severity for a in items), key=lambda x: rank.get(x, 0), default="Unknown"),
            "actions": [{"id": a.action_id, "status": a.status, "timestamp": a.acknowledged_at or a.created_at, "title": a.title, "severity": a.severity} for a in items]}


def main():
    try:
        store = ActionsStore(drive_service=build_service())
        if store.is_degraded:
            raise RuntimeError(store.last_error or "Action SSOT unavailable")
        print(json.dumps(actionable_snapshot(store.list_actions()), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"state": "unknown", "error": str(exc)}))


if __name__ == "__main__":
    main()
