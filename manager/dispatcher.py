#!/usr/bin/env python3
"""Compose existing runtime signals into an AI-ready prompt without executing it."""

import argparse
import json
import math
import re
import sys

from collectors.publish_drive import build_service
from manager.assignment import CAPABILITIES, decide
from manager.estimator import estimate
from manager.executions import list_executions
from manager.quota_reader import read_drive_status, summarize
from manager.tasks import DriveRecords, TaskError, create_task, safe_id, validate


ADAPTATION = {
    "codex": "Work directly in the named repo and scope. Run required tests and git status. Commit/push only when explicitly requested.",
    "claude": "Read only the named context first, explain root cause before broad changes, and preserve the handoff/current state.",
    "antigravity": "Use the shared scope and validation checklist; report any manual or unknown quota limitation before work.",
    "gemini_app": "Use the shared scope and validation checklist; report any manual or unknown quota limitation before work.",
}


def clean(value):
    text = str(value)
    return re.sub(r"(?i)\b(token|secret|credential|cookie|password)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)


def request_ok(request):
    required = {"project_id": str, "title": str, "task_type": str, "complexity": str}
    for key, kind in required.items():
        if not isinstance(request.get(key), kind) or not request[key].strip():
            raise TaskError(f"invalid dispatcher input: {key}")
    if request["complexity"] not in ("low", "medium", "high"):
        raise TaskError("invalid dispatcher input: complexity")
    if request.get("expected_minutes") is not None and (not isinstance(request["expected_minutes"], (int, float)) or request["expected_minutes"] <= 0):
        raise TaskError("invalid dispatcher input: expected_minutes")
    if request.get("preferred_provider") and request.get("preferred_provider") == request.get("excluded_provider"):
        raise TaskError("preferred_provider cannot also be excluded")


def quota_line(provider):
    windows = [f"{item['name']}: {item.get('remaining_percent')}% remaining" for item in provider["windows"] if item.get("remaining_percent") is not None]
    detail = ", ".join(windows) if windows else "quota unknown"
    return f"{detail}; {provider['source_type']}; {provider['freshness']}; confidence {provider['confidence']}"


def phase_goals(scope, count):
    scope = scope or ["Implement and validate the requested goal"]
    groups = [[] for _ in range(count)]
    for index, item in enumerate(scope):
        groups[min(index * count // len(scope), count - 1)].append(item)
    return ["; ".join(group) if group else "Integrate and validate the preceding phase" for group in groups]


def prompt_for(project, task, handoff, provider, estimate_result, quota_summary, warnings, shared_rules=None, ponytail_available=None):
    forbidden = list(project.get("important_constraints", [])) + list(task.get("constraints", []))
    if handoff:
        forbidden += handoff.get("do_not_touch", [])
    policies = set(project.get("execution_policies", [])) | set(task.get("execution_policies", []))
    coding = task.get("needs_repo_edit", True) and task.get("task_type") in ("implementation", "bugfix", "debugging", "regression", "testing")
    ponytail = "ponytail" in policies and coding
    minutes = estimate_result["estimated_minutes"]
    count = estimate_result["suggested_phases"] if estimate_result["split_recommended"] else 1
    phases = phase_goals(task.get("scope", []), count)
    baseline = project.get("baseline_commit") or task.get("source_context", {}).get("baseline_commit") or "not recorded"
    handoff_text = "none"
    if handoff:
        handoff_text = f"{handoff.get('minimal_context', '')} Current state: {handoff.get('current_state', '')}. Next: {handoff.get('next_action', '')}."
    lines = [
        f"AI: {provider.replace('_', ' ').title()}",
        f"Project: {project['project_id']}",
        f"Task: {task['task_id']}", "",
        "AI Development Manager execution brief", "",
        f"Provider adaptation: {ADAPTATION[provider]}",
        f"Project name: {project['name']}", f"Repo: {project['repo']}",
        f"Working directory: {project.get('working_directory') or 'resolve from repo'}",
        f"Branch: {project['default_branch']}", f"Baseline commit: {baseline}",
        f"Task goal: {task['title']}", f"Current state: {task.get('current_progress', 'Not started')}",
        f"Next action: {task.get('next_action', '')}", f"Latest handoff: {handoff_text}",
        "Rule priority (highest first):",
        *[f"- Project business / acceptance: {item}" for item in project.get("project_rules", [])],
        *[f"- Project business / acceptance: {item}" for item in task.get("acceptance_criteria", [])],
        *[f"- AI Development Manager scope / protection: {item}" for item in (shared_rules or [])],
        *[f"- AI Development Manager scope / protection: {item}" for item in task.get("constraints", [])],
        "Allowed scope:", *[f"- {item}" for item in task.get("scope", [])],
        "Forbidden scope / do not touch:", *[f"- {item}" for item in dict.fromkeys(forbidden)],
        "Acceptance criteria:", *[f"- {item}" for item in task.get("acceptance_criteria", [])],
        f"Timebox: approximately {minutes} minutes total; {count} phase(s), each <=20 minutes.",
    ]
    if count > 1:
        lines += ["Phase plan:", *[f"- Phase {index + 1}: {goal}" for index, goal in enumerate(phases)], "Execute Phase 1 only, then report before continuing."]
    if ponytail:
        skill = "Enable the local Ponytail skill. " if ponytail_available is not False else "Ponytail skill is unavailable; use the equivalent text policy. "
        lines += ["Ponytail minimal-change preference (lower priority than requirements above):", skill + "Use Ponytail/minimal-change principles: make the smallest safe change that satisfies the acceptance criteria; do not refactor unrelated code. Necessary tests, schema changes, compatibility fixes, correctness, and regression protection remain required."]
    lines += [
        f"Quota summary: {quota_summary}",
        f"Warnings: {'; '.join(warnings) if warnings else 'none'}",
        "Required validation:", "- Run the tests/checks named by the acceptance criteria.", "- Run git diff --check and inspect git status when this is a repo-edit task.",
        "Completion report format:", "- Work completed", "- Files changed", "- Tests/checks and results", "- Remaining issues or blockers", "- Commit hash/push status if requested", "Do not start another phase automatically.",
    ]
    return clean("\n".join(lines))


def dispatch(store, service, request, quota_document=None, executions=None):
    request_ok(request)
    project = store.get("projects", request["project_id"], request["project_id"]); validate("project", project)
    quota = summarize(quota_document or read_drive_status(service=service), 60)
    history = executions if executions is not None else list_executions(store, request["project_id"])

    task = None
    if request.get("task_id"):
        try:
            task = store.get("tasks", request["project_id"], request["task_id"]); validate("task", task)
        except TaskError as exc:
            if "found 0" not in str(exc) and "not found" not in str(exc):
                raise
    else:
        for active_id in project.get("active_tasks", []):
            try:
                candidate = store.get("tasks", request["project_id"], active_id)
            except TaskError:
                continue
            if candidate.get("title") == request["title"]:
                task = candidate; validate("task", task); break
    task_input = task or {
        "task_id": safe_id(request.get("task_id") or re.sub(r"[^a-z0-9]+", "-", request["title"].lower()).strip("-")),
        "project_id": request["project_id"], "title": request["title"], "task_type": request["task_type"], "complexity": request["complexity"],
        "expected_minutes": request.get("expected_minutes") or 20, "needs_repo_edit": request.get("needs_repo_edit", True), "needs_research": request.get("needs_research", False), "needs_browser": request.get("needs_browser", False), "parallelizable": request.get("parallelizable", False),
        "scope": request.get("scope", []), "constraints": request.get("constraints", []), "acceptance_criteria": request.get("acceptance_criteria", []), "source_context": request.get("source_context", {}),
        "current_progress": "Not started", "next_action": "Confirm dispatch recommendation",
    }
    estimates = {provider: estimate({**task_input, "provider": provider, "mode": CAPABILITIES[provider]["mode"], "effort": "high" if task_input.get("complexity") == "high" else "medium"}, history) for provider in CAPABILITIES}
    decision = decide(task_input, quota, estimates=estimates)
    selected = request.get("preferred_provider") or decision["recommended_provider"] or decision["alternatives"][0]
    excluded = request.get("excluded_provider")
    if selected == excluded:
        selected = next((item for item in decision["alternatives"] if item != excluded), None)
    if selected not in CAPABILITIES:
        raise TaskError("no eligible provider")
    alternatives = [item for item in [decision["recommended_provider"], *decision["alternatives"]] if item and item not in (selected, excluded)]
    selected_estimate = estimates[selected]
    if decision["recommended_mode"] == "split_task" and not selected_estimate["split_recommended"]:
        minutes = task_input["expected_minutes"]
        selected_estimate = {**selected_estimate, "estimated_minutes": minutes, "split_recommended": True, "suggested_phases": math.ceil(minutes / 20), "basis": selected_estimate["basis"] + "; task input exceeds 20 minutes"}
    selected_quota = next(item for item in quota["providers"] if item["provider"] == selected)
    warnings = [item for item in [decision.get("warning")] if item]
    if request.get("preferred_provider"):
        warnings.append(f"Preferred provider override selected: {selected}")
    if selected_quota["stale"] or not selected_quota["has_reliable_quota"]:
        warnings.append(f"{selected_quota['display_name']} quota is stale or unknown")
    handoff = None
    if task:
        try:
            handoff = store.latest("handoffs", request["project_id"], task["task_id"])
        except TaskError:
            pass
    if not task:
        task_input.update(recommended_provider=selected, mode=CAPABILITIES[selected]["mode"], effort=decision["recommended_effort"], quota_evidence=decision["quota_evidence"])
        persist_task = request.get("persist_task", True)
        task = create_task(store, task_input, service, assign=False, persist=persist_task)
        if persist_task and task["task_id"] not in project["active_tasks"]:
            project["active_tasks"].append(task["task_id"]); store.put("projects", project["project_id"], project["project_id"], project)
    summary = quota_line(selected_quota)
    generated = prompt_for(project, task, handoff, selected, selected_estimate, summary, warnings, request.get("shared_rules"), request.get("ponytail_available"))
    return {
        "recommended_provider": selected, "mode": CAPABILITIES[selected]["mode"], "effort": decision["recommended_effort"],
        "quota_evidence": decision["quota_evidence"],
        "estimated_minutes": selected_estimate["estimated_minutes"], "split_recommended": selected_estimate["split_recommended"], "phase_count": selected_estimate["suggested_phases"],
        "alternatives": alternatives, "quota_summary": summary, "warnings": warnings, "generated_prompt": generated,
    }


def human_summary(result):
    alternative = ", ".join(result["alternatives"]) or "none"
    return f"Recommended: {result['recommended_provider']}\nMode: {result['mode']}\nEffort: {result['effort']}\nEstimate: {result['estimated_minutes']} minutes\nQuota: {result['quota_summary']}\nAlternatives: {alternative}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True); parser.add_argument("--task-id"); parser.add_argument("--title", required=True); parser.add_argument("--task-type", required=True); parser.add_argument("--complexity", choices=["low", "medium", "high"], default="medium"); parser.add_argument("--expected-minutes", type=float)
    parser.add_argument("--scope", action="append", default=[]); parser.add_argument("--constraint", action="append", default=[]); parser.add_argument("--acceptance", action="append", default=[]); parser.add_argument("--preferred-provider", choices=list(CAPABILITIES)); parser.add_argument("--excluded-provider", choices=list(CAPABILITIES)); parser.add_argument("--needs-research", action="store_true"); parser.add_argument("--needs-browser", action="store_true"); parser.add_argument("--no-repo-edit", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(); request = vars(args); as_json = request.pop("json"); request["constraints"] = request.pop("constraint"); request["acceptance_criteria"] = request.pop("acceptance"); request["needs_repo_edit"] = not request.pop("no_repo_edit")
    try:
        service = build_service(); result = dispatch(DriveRecords(service), service, request)
        print(json.dumps(result, indent=2) if as_json else human_summary(result) + "\n\n" + result["generated_prompt"]); return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
