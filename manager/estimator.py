#!/usr/bin/env python3
"""Explainable median estimates from completed execution history."""

import argparse
import json
import math
from statistics import median

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords


FEATURES = ("mode", "effort", "complexity", "needs_repo_edit", "needs_research", "needs_browser", "parallelizable")


def estimate(task, executions):
    candidates = [item for item in executions if item.get("status") == "completed" and item.get("provider") == task.get("provider") and item.get("task_snapshot", {}).get("task_type") == task.get("task_type") and item.get("elapsed_minutes") is not None]
    if not candidates:
        minutes = task.get("expected_minutes", 20)
        return {"estimated_minutes": minutes, "estimated_quota_delta": {"status": "unknown", "windows": []}, "sample_count": 0, "confidence": "none", "split_recommended": minutes > 20, "suggested_phases": max(1, math.ceil(minutes / 20)), "basis": "No matching completed executions; used task input only"}
    scored = [(sum((item.get(key) if key in ("mode", "effort") else item.get("task_snapshot", {}).get(key)) == task.get(key) for key in FEATURES), item) for item in candidates]
    best = max(score for score, _ in scored)
    samples = [item for score, item in scored if score >= best - 1]

    minutes = median(item["elapsed_minutes"] for item in samples)
    by_window = {}
    for item in samples:
        delta = item.get("quota_delta") or {}
        for window in delta.get("windows", []):
            if window.get("status") == "known" and window.get("used_percent_delta") is not None:
                by_window.setdefault(window["name"], []).append(window["used_percent_delta"])
    quota_windows = [{"name": name, "used_percent_delta": median(values), "sample_count": len(values)} for name, values in sorted(by_window.items())]
    count = len(samples)
    confidence = "low" if count < 3 else "medium" if count < 5 else "high"
    return {
        "estimated_minutes": round(minutes, 3),
        "estimated_quota_delta": {"status": "known" if quota_windows else "unknown", "windows": quota_windows},
        "sample_count": count, "confidence": confidence,
        "split_recommended": minutes > 20, "suggested_phases": max(1, math.ceil(minutes / 20)),
        "basis": f"Median of {count} completed executions matching provider/task_type and within one characteristic of the closest sample",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id"); parser.add_argument("--task-type", required=True); parser.add_argument("--provider", required=True)
    parser.add_argument("--mode"); parser.add_argument("--effort"); parser.add_argument("--complexity"); parser.add_argument("--expected-minutes", type=float, default=20)
    parser.add_argument("--needs-repo-edit", action="store_true"); parser.add_argument("--needs-research", action="store_true"); parser.add_argument("--needs-browser", action="store_true"); parser.add_argument("--parallelizable", action="store_true")
    args = parser.parse_args()
    from manager.executions import list_executions
    service = build_service(); store = DriveRecords(service)
    print(json.dumps(estimate(vars(args), list_executions(store, args.project_id)), indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
