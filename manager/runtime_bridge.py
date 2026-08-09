#!/usr/bin/env python3
"""Stable one-call runtime contract for ChatGPT and future clients."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from collectors.publish_drive import build_service
from manager.dispatcher import clean, dispatch
from manager.executions import list_executions
from manager.quota_reader import read_drive_status, summarize
from manager.scheduler import schedule
from manager.tasks import DriveRecords, MIME_FOLDER, ROOT_FOLDER_ID, ROOT_FOLDERS, TaskError, validate


RULES_PATH = Path(__file__).parents[1] / "AI-DEVELOPMENT-RULES.md"


def compact(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())


def generated_task_id(value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or f"task-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:8]}"


def load_shared_rules(path=RULES_PATH):
    selected = {2, 5, 6, 9, 10}
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"(\d+)\.\s+(.+)", line)
        if match and int(match.group(1)) in selected:
            rules.append(match.group(2))
    return rules


def all_projects(store):
    if hasattr(store, "list_projects"):
        return store.list_projects()
    root = store.folder(ROOT_FOLDER_ID, ROOT_FOLDERS["projects"], create=False)
    projects = []
    for item in store.children(root):
        if item.get("mimeType") == MIME_FOLDER:
            projects.append(store.get("projects", item["name"], item["name"]))
    return projects


def resolve_project(store, project_ref=None, user_request=""):
    if project_ref:
        try:
            project = store.get("projects", project_ref, project_ref); validate("project", project); return project
        except TaskError:
            pass
    needle = compact(project_ref or user_request)
    matches = []
    for project in all_projects(store):
        names = [project["project_id"], project["name"], *project.get("aliases", [])]
        if any(compact(name) == needle or (not project_ref and compact(name) in needle) for name in names):
            matches.append(project)
    if len(matches) != 1:
        raise TaskError(f"project resolution expected one match; found {len(matches)}")
    validate("project", matches[0]); return matches[0]


def active_task(store, project, task_id, user_request):
    if task_id:
        task = store.get("tasks", project["project_id"], task_id); validate("task", task); return task
    candidates = []
    for active_id in project.get("active_tasks", []):
        try:
            task = store.get("tasks", project["project_id"], active_id); validate("task", task)
            if task["status"] not in ("completed", "cancelled"):
                candidates.append(task)
        except TaskError:
            continue
    needle = compact(user_request)
    matches = [task for task in candidates if compact(task["task_id"]) in needle or compact(task["title"]) in needle]
    if len(matches) == 1:
        return matches[0]
    return candidates[0] if len(candidates) == 1 else None


def handoff_summary(store, project_id, task):
    if not task:
        return None
    try:
        handoff = store.latest("handoffs", project_id, task["task_id"])
        return {key: handoff.get(key) for key in ("handoff_id", "from_provider", "to_provider", "reason", "current_state", "next_action", "minimal_context")}
    except TaskError:
        return None


def compact_task(task):
    if not task:
        return None
    return {key: task.get(key) for key in ("task_id", "title", "status", "current_progress", "next_action", "assigned_provider", "recommended_provider")}


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return clean(value) if isinstance(value, str) else value


def request_type(user_request, task, multi_task):
    if multi_task:
        return "scheduling"
    if any(word in user_request.lower() for word in ("status", "progress", "狀態", "状态", "進度", "进度")):
        return "status"
    return "continuation" if task else "new_task"


def runtime_bridge(store, service, request, quota_document=None, executions=None, dispatch_func=dispatch, schedule_func=schedule, rules_path=RULES_PATH):
    if not isinstance(request, dict) or not isinstance(request.get("user_request"), str) or not request["user_request"].strip():
        raise TaskError("user_request is required")
    project = resolve_project(store, request.get("project_id"), request["user_request"])
    project_id = project["project_id"]
    task = active_task(store, project, request.get("task_id"), request["user_request"])
    raw_quota = quota_document or read_drive_status(service=service)
    quota = summarize(raw_quota, 60)
    history = executions if executions is not None else list_executions(store, project_id)
    shared_rules = load_shared_rules(rules_path)

    def shared_dispatch(local_store, local_service, local_request, local_quota, local_history):
        return dispatch_func(local_store, local_service, {**local_request, "shared_rules": shared_rules, "ponytail_available": request.get("ponytail_available")}, local_quota, local_history)

    kind = request_type(request["user_request"], task, request.get("multi_task", False))
    batches = []
    if request.get("multi_task"):
        tasks = []
        for task_id in project.get("active_tasks", []):
            try:
                candidate = store.get("tasks", project_id, task_id)
                if candidate.get("status") == "ready":
                    tasks.append(candidate)
            except TaskError:
                continue
        if not tasks:
            raise TaskError("no ready active tasks for multi_task request")
        plan = schedule_func(store, service, project_id, tasks, raw_quota, history, dispatch_func=shared_dispatch)
        batches = plan["execution_batches"]
        warnings = plan["warnings"]
        first = batches[0]["tasks"][0] if batches and batches[0]["tasks"] else None
        result = first["dispatcher_result"] if first else {}
        next_action = "Review Batch 1 and copy the generated prompts; do not auto-start providers"
    else:
        new_task_id = request.get("task_id") or generated_task_id(request["user_request"])
        dispatch_request = {
            "project_id": project_id, "task_id": task["task_id"] if task else new_task_id,
            "title": task["title"] if task else request["user_request"],
            "task_type": task["task_type"] if task else request.get("task_type", "implementation"),
            "complexity": task.get("complexity", "medium") if task else request.get("complexity", "medium"),
            "expected_minutes": task["expected_minutes"] if task else request.get("expected_minutes"),
            "scope": task.get("scope", []) if task else [request["user_request"]],
            "constraints": task.get("constraints", []) if task else [],
            "acceptance_criteria": task.get("acceptance_criteria", []) if task else ["Relevant validation passes"],
            "needs_repo_edit": task.get("needs_repo_edit", True) if task else True,
            "needs_research": task.get("needs_research", False) if task else False,
            "needs_browser": task.get("needs_browser", False) if task else False,
            "preferred_provider": request.get("preferred_provider"), "excluded_provider": request.get("excluded_provider"),
            "shared_rules": shared_rules, "ponytail_available": request.get("ponytail_available"),
        }
        result = dispatch_func(store, service, dispatch_request, raw_quota, history)
        warnings = result["warnings"]
        if not task:
            task = store.get("tasks", project_id, new_task_id)
        next_action = task.get("next_action") or "Copy the generated prompt to the recommended provider"

    provider = result.get("recommended_provider")
    provider_quota = next((item for item in quota["providers"] if item["provider"] == provider), None)
    if provider_quota and provider_quota["stale"]:
        warnings = list(dict.fromkeys([*warnings, f"{provider_quota['display_name']} quota is stale; recommendation is degraded"]))
    return redact({
        "contract_version": "1.0",
        "project": {key: project.get(key) for key in ("project_id", "name", "repo", "default_branch", "working_directory", "baseline_commit")},
        "request_type": kind, "active_task": compact_task(task), "latest_handoff_summary": handoff_summary(store, project_id, task),
        "recommended_provider": provider, "mode": result.get("mode"), "effort": result.get("effort"), "estimated_minutes": result.get("estimated_minutes"),
        "split_recommended": result.get("split_recommended", False), "alternatives": result.get("alternatives", []), "quota_summary": result.get("quota_summary"),
        "quota_freshness": provider_quota["freshness"] if provider_quota else "unknown", "warnings": warnings,
        "next_action": next_action, "generated_prompt": result.get("generated_prompt"), "execution_batches": batches,
    })


def human_summary(result):
    alternatives = ", ".join(result["alternatives"]) or "none"
    return f"推荐：{result['recommended_provider']}\nEffort：{result['effort']}\n预计：{result['estimated_minutes']} 分钟\nQuota：{result['quota_summary']}\n备选：{alternatives}\n下一步：{result['next_action']}"


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--project-id"); parser.add_argument("--request", required=True); parser.add_argument("--task-id"); parser.add_argument("--task-type"); parser.add_argument("--complexity", choices=["low", "medium", "high"]); parser.add_argument("--preferred-provider"); parser.add_argument("--excluded-provider"); parser.add_argument("--multi-task", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(); data = {key: value for key, value in vars(args).items() if key != "json" and value is not None}; data["user_request"] = data.pop("request")
    try:
        service = build_service(); result = runtime_bridge(DriveRecords(service), service, data)
        print(json.dumps(result, ensure_ascii=False) if args.json else human_summary(result) + (f"\n\n```\n{result['generated_prompt']}\n```" if result["generated_prompt"] else "")); return 0
    except (TaskError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False) if args.json else f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
