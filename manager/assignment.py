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
        # ClaudeLauncher v1 only supports the read-only safe profile
        # (manager/claude_launcher.py's _READ_ONLY_SANDBOX/_READ_ONLY_APPROVAL
        # check) -- it cannot execute a repo-write turn at all, so it must
        # never be ranked as a candidate for one. This is a hard capability
        # fact about the launcher, not a scoring preference; see
        # `repo_write_capable` below and decide()'s own filter.
        "repo_write_capable": False,
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
    if not provider.get("has_usable_quota", provider["has_reliable_quota"]):
        return -0.5, evidence
    remaining = [window["remaining_percent"] for window in provider["windows"] if window.get("remaining_percent") is not None]
    score = sum(remaining) / len(remaining) / 50
    reset = parse_time(provider["nearest_reset_at"])
    if reset:
        reset_minutes = max(0, (reset - now).total_seconds() / 60)
        score += max(0, 1 - reset_minutes / max(expected_minutes, 1))
    return score, evidence


def decide(task, quota, now=None, estimates=None):
    now = now or datetime.now(timezone.utc)
    estimates = estimates or {}
    expected = task.get("expected_minutes", 20)
    # A repo-write task (needs_repo_edit=True, the default -- see
    # manager/dispatcher.py's task_input construction) requires a launcher
    # that can actually execute an unattended write turn. This is a hard
    # eligibility gate, checked BEFORE quota (so an incompatible provider's
    # quota is never even consulted) and before scoring (so it can never
    # win purely on quota/task-type score) -- routing order is task
    # requirements -> capability compatibility -> quota reliability ->
    # score/rank, never "quota score wins, capability discovered at launch
    # time" (see manager/claude_launcher.py's unsupported_policy fail-close,
    # which this filter exists to make unreachable for routing purposes).
    needs_write = task.get("needs_repo_edit", True)
    ranked = []
    warnings = []
    for provider in quota["providers"]:
        config = CAPABILITIES[provider["provider"]]
        if needs_write and not config.get("repo_write_capable", True):
            warnings.append(f"{provider['display_name']} does not support repo-write tasks (capability mismatch)")
            continue
        if not provider.get("has_usable_quota", provider["has_reliable_quota"]):
            warnings.append(f"{provider['display_name']} quota is {provider['status']} or stale")
            continue
        score = config["task_types"].get(task.get("task_type", "implementation"), 0)
        for trait, weight in config["traits"].items():
            if task.get(trait, False):
                score += weight
        if task.get("complexity") == "high" and provider["provider"] == "claude":
            score += 1
        q_score, evidence = quota_score(provider, expected, now)
        evidence["historical_estimate"] = estimates.get(provider["provider"])
        score += q_score
        ranked.append({"provider": provider["provider"], "score": round(score, 3), "evidence": evidence, "mode": config["mode"]})
    ranked.sort(key=lambda item: item["score"], reverse=True)

    split = expected > 20
    winner = ranked[0] if ranked else None
    historical = estimates.get(winner["provider"]) if winner else None
    reasons = (["No provider has fresh reliable quota; automatic routing is unavailable"] if winner is None else
               [f"Expected duration {expected} minutes exceeds the 20-minute task target; split before assignment"] if split else [
        f"Best combined capability and quota evidence score for {task.get('task_type', 'implementation')}",
    ])
    if historical:
        reasons.append(f"History: {historical['estimated_minutes']} minutes from {historical['sample_count']} matching executions ({historical['confidence']} confidence)")
    return {
        "recommended_provider": None if split or winner is None else winner["provider"],
        "recommended_mode": "split_task" if split else winner["mode"] if winner else None,
        "recommended_effort": "high" if task.get("complexity") == "high" else "medium",
        "alternatives": [item["provider"] for item in ranked[:3] if split or item is not winner],
        "reasons": reasons,
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
    parser.add_argument("--project-id")
    args = parser.parse_args()
    task = vars(args)
    max_age = task.pop("max_age_minutes")
    project_id = task.pop("project_id")
    quota = summarize(read_drive_status(), max_age)
    estimates = {}
    if project_id:
        from collectors.publish_drive import build_service
        from manager.estimator import estimate
        from manager.executions import list_executions
        from manager.tasks import DriveRecords
        service = build_service(); history = list_executions(DriveRecords(service), project_id)
        for provider in CAPABILITIES:
            estimates[provider] = estimate({**task, "provider": provider}, history)
    print(json.dumps(decide(task, quota, estimates=estimates), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
