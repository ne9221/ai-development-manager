#!/usr/bin/env python3
"""Recommend an AI provider from task characteristics and summarized quota."""

import argparse
import json
from datetime import datetime, timezone

from manager.quota_reader import parse_time, read_drive_status, summarize


CAPABILITIES = {
    "codex": {
        "task_types": {"implementation": 4, "debugging": 3, "testing": 3, "research": 1, "architecture": 1},
        "traits": {"needs_repo_edit": 3, "needs_research": 0, "needs_browser": 1},
        "mode": "code",
    },
    "claude": {
        "task_types": {"implementation": 2, "debugging": 3, "testing": 1, "research": 4, "architecture": 4},
        "traits": {"needs_repo_edit": 1, "needs_research": 3, "needs_browser": 0},
        "mode": "analysis",
    },
    "antigravity": {
        "task_types": {"implementation": 1, "debugging": 1, "testing": 1, "research": 1, "architecture": 1},
        "traits": {"needs_repo_edit": 0, "needs_research": 0, "needs_browser": 1},
        "mode": "interactive",
    },
    "gemini_app": {
        "task_types": {"implementation": 1, "debugging": 1, "testing": 1, "research": 2, "architecture": 1},
        "traits": {"needs_repo_edit": 0, "needs_research": 1, "needs_browser": 1},
        "mode": "interactive",
    },
}


def quota_score(provider, expected_minutes, now):
    evidence = {
        "freshness": provider["freshness"],
        "source_type": provider["source_type"],
        "confidence": provider["confidence"],
        "windows": provider["windows"],
        "nearest_reset_at": provider["nearest_reset_at"],
    }
    if not provider["has_reliable_quota"]:
        return -0.5, evidence
    remaining = [window["remaining_percent"] for window in provider["windows"] if window.get("remaining_percent") is not None]
    score = sum(remaining) / len(remaining) / 50
    reset = parse_time(provider["nearest_reset_at"])
    if reset:
        reset_minutes = max(0, (reset - now).total_seconds() / 60)
        score += max(0, 1 - reset_minutes / max(expected_minutes, 1))
    return score, evidence


def decide(task, quota, now=None):
    now = now or datetime.now(timezone.utc)
    expected = task.get("expected_minutes", 20)
    ranked = []
    warnings = []
    for provider in quota["providers"]:
        config = CAPABILITIES[provider["provider"]]
        score = config["task_types"].get(task.get("task_type", "implementation"), 0)
        for trait, weight in config["traits"].items():
            if task.get(trait, False):
                score += weight
        if task.get("complexity") == "high" and provider["provider"] == "claude":
            score += 1
        q_score, evidence = quota_score(provider, expected, now)
        score += q_score
        if not provider["has_reliable_quota"]:
            warnings.append(f"{provider['display_name']} quota is {provider['status']} or stale")
        ranked.append({"provider": provider["provider"], "score": round(score, 3), "evidence": evidence, "mode": config["mode"]})
    ranked.sort(key=lambda item: item["score"], reverse=True)

    split = expected > 20
    winner = ranked[0]
    return {
        "recommended_provider": None if split else winner["provider"],
        "recommended_mode": "split_task" if split else winner["mode"],
        "recommended_effort": "high" if task.get("complexity") == "high" else "medium",
        "alternatives": [item["provider"] for item in ranked[:3] if split or item is not winner],
        "reasons": ([f"Expected duration {expected} minutes exceeds the 20-minute task target; split before assignment"] if split else [
            f"Best combined capability and quota evidence score for {task.get('task_type', 'implementation')}",
            "Unknown quota remained eligible but carried uncertainty",
        ]),
        "quota_evidence": {item["provider"]: item["evidence"] for item in ranked},
        "warning": "; ".join(dict.fromkeys(warnings)) or None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-type", default="implementation")
    parser.add_argument("--complexity", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--expected-minutes", type=float, default=20)
    parser.add_argument("--needs-repo-edit", action="store_true")
    parser.add_argument("--needs-research", action="store_true")
    parser.add_argument("--needs-browser", action="store_true")
    parser.add_argument("--parallelizable", action="store_true")
    parser.add_argument("--max-age-minutes", type=float, default=60)
    args = parser.parse_args()
    task = vars(args)
    max_age = task.pop("max_age_minutes")
    quota = summarize(read_drive_status(), max_age)
    print(json.dumps(decide(task, quota), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
