#!/usr/bin/env python3
"""Build parallel execution batches without starting any AI provider."""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from collectors.publish_drive import build_service
from manager.assignment import CAPABILITIES
from manager.dispatcher import dispatch
from manager.executions import list_executions
from manager.quota_reader import parse_time, read_drive_status, summarize
from manager.tasks import DriveRecords, TaskError, validate


def capability(task, provider):
    config = CAPABILITIES[provider]
    score = config["task_types"].get(task.get("task_type"), 0)
    return score + sum(weight for trait, weight in config["traits"].items() if task.get(trait, False))


def edit_task(task):
    return task.get("needs_repo_edit", True) and not task.get("read_only", False)


def paths_overlap(left, right):
    for first in left:
        first = first.replace("\\", "/").rstrip("/").lower()
        for second in right:
            second = second.replace("\\", "/").rstrip("/").lower()
            if first == second or first.startswith(second + "/") or second.startswith(first + "/"):
                return True
    return False


def conflict(left, right, project):
    if not edit_task(left) or not edit_task(right):
        return None
    if paths_overlap(left.get("allowed_paths", []), right.get("allowed_paths", [])):
        return "overlapping file scope"
    left_dir = left.get("working_directory") or project.get("working_directory")
    right_dir = right.get("working_directory") or project.get("working_directory")
    if left_dir == right_dir:
        left_tree, right_tree = left.get("worktree_id"), right.get("worktree_id")
        if not left_tree or not right_tree or left_tree == right_tree:
            return "same repo working tree has concurrent edits"
    return None


def dependency_state(store, project_id, task, task_ids, scheduled):
    waiting = []
    for dependency in task.get("depends_on", []):
        if dependency in task_ids:
            if dependency not in scheduled:
                waiting.append(dependency)
        else:
            try:
                if store.get("tasks", project_id, dependency).get("status") != "completed":
                    waiting.append(dependency)
            except TaskError:
                waiting.append(dependency)
    return waiting


def reset_defer(task, result, quota, now):
    if task.get("priority") in ("urgent", "high"):
        return None
    provider = next(item for item in quota["providers"] if item["provider"] == result["recommended_provider"])
    remaining = [item["remaining_percent"] for item in provider["windows"] if item.get("remaining_percent") is not None]
    reset = parse_time(provider.get("nearest_reset_at"))
    if remaining and min(remaining) <= 20 and reset and now <= reset <= now + timedelta(minutes=30):
        return provider["nearest_reset_at"]
    return None


def scheduler_request(task):
    return {
        "project_id": task["project_id"], "task_id": task["task_id"], "title": task["title"],
        "task_type": task["task_type"], "complexity": task.get("complexity", "medium"), "expected_minutes": task["expected_minutes"],
        "scope": task.get("scope", []), "constraints": task.get("constraints", []), "acceptance_criteria": task.get("acceptance_criteria", []),
        "needs_repo_edit": task.get("needs_repo_edit", True), "needs_research": task.get("needs_research", False), "needs_browser": task.get("needs_browser", False),
        "preferred_provider": task.get("preferred_provider"), "excluded_provider": task.get("excluded_provider"),
    }


def schedule(store, service, project_id, tasks, quota_document=None, executions=None, now=None, dispatch_func=dispatch):
    now = now or datetime.now(timezone.utc)
    project = store.get("projects", project_id, project_id); validate("project", project)
    for task in tasks:
        validate("task", task)
        if task["project_id"] != project_id or task["status"] != "ready":
            raise TaskError(f"task is not ready for project {project_id}: {task.get('task_id')}")
    ids = [task["task_id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise TaskError("duplicate task_id in scheduler input")
    raw_quota = quota_document or read_drive_status(service=service)
    quota = summarize(raw_quota, 60, now)
    history = executions if executions is not None else list_executions(store, project_id)
    task_by_id = {task["task_id"]: task for task in tasks}
    cache = {}

    def dispatched(task, preferred=None):
        key = (task["task_id"], preferred)
        if key not in cache:
            request = scheduler_request(task)
            if preferred:
                request["preferred_provider"] = preferred
            cache[key] = dispatch_func(store, service, request, raw_quota, history)
        return cache[key]

    batches, deferred, warnings, scheduled, pending = [], [], [], set(), list(ids)
    prior_reasons = {}
    while pending:
        batch, used_providers = [], set()
        for task_id in list(pending):
            task = task_by_id[task_id]
            waiting = dependency_state(store, project_id, task, set(ids), scheduled)
            if waiting:
                prior_reasons[task_id] = f"depends on unfinished task(s): {', '.join(waiting)}"
                continue
            result = dispatched(task)
            provider = result["recommended_provider"]
            if provider is None:
                # DASHBOARD_TRUTH_CONNECTED gate 1: manager.dispatcher.dispatch()
                # now admits the Task even when no provider currently has
                # usable quota (waiting_quota) instead of raising -- this must
                # not crash batch scheduling (reset_defer() below assumes a
                # real provider name) nor silently drop the task from
                # `pending`; it is deferred with the real reason, exactly like
                # an unresolved dependency or a scope conflict, and retried on
                # the next round in case another task's scheduling changes
                # nothing relevant (quota does not change mid-batch).
                prior_reasons[task_id] = next(iter(result.get("warnings", [])), "no provider has usable quota")
                continue
            if provider in used_providers and not task.get("preferred_provider"):
                alternative = next((item for item in result["alternatives"] if item not in used_providers and item != task.get("excluded_provider") and capability(task, item) >= capability(task, provider) - 1), None)
                if alternative:
                    result = dispatched(task, alternative); provider = alternative
            if provider in used_providers:
                prior_reasons[task_id] = f"provider {provider} already allocated in this batch"
                continue
            reason = next((value for item in batch if (value := conflict(task, task_by_id[item["task_id"]], project))), None)
            if reason:
                prior_reasons[task_id] = reason
                continue
            defer_until = reset_defer(task, result, quota, now)
            if defer_until:
                deferred.append({"task_id": task_id, "recommendation": "defer_until_reset", "defer_until": defer_until, "reason": "low reliable quota resets within 30 minutes", "dispatcher_result": result})
                pending.remove(task_id); continue
            batch.append({
                "task_id": task_id, "scheduled_unit": "Phase 1" if result["split_recommended"] else "whole task",
                "recommended_provider": provider, "mode": result["mode"], "effort": result["effort"], "estimated_minutes": min(20, result["estimated_minutes"]),
                "dependency_reason": prior_reasons.get(task_id) if "depends on" in prior_reasons.get(task_id, "") else None,
                "conflict_reason": prior_reasons.get(task_id) if prior_reasons.get(task_id) and "depends on" not in prior_reasons[task_id] else None,
                "dispatcher_result": result,
            })
            used_providers.add(provider); pending.remove(task_id)
        if batch:
            batches.append({"batch": len(batches) + 1, "tasks": batch}); scheduled.update(item["task_id"] for item in batch)
        elif pending:
            for task_id in pending:
                deferred.append({"task_id": task_id, "recommendation": "blocked", "reason": prior_reasons.get(task_id, "dependency cycle or unresolved dependency")})
            break
    for item in quota["providers"]:
        if item["stale"] or not item["has_reliable_quota"]:
            warnings.append(f"{item['display_name']} quota is stale or unknown")
    return {"execution_batches": batches, "deferred_tasks": deferred, "warnings": warnings}


def load_ready_tasks(store, project_id, task_ids):
    return [store.get("tasks", project_id, task_id) for task_id in task_ids]


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--project-id", required=True); parser.add_argument("--task-id", action="append", required=True)
    args = parser.parse_args()
    try:
        service = build_service(); store = DriveRecords(service); result = schedule(store, service, args.project_id, load_ready_tasks(store, args.project_id, args.task_id))
        print(json.dumps(result, indent=2)); return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
