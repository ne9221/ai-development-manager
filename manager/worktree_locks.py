#!/usr/bin/env python3
"""Drive-backed logical leases for production working-tree access."""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from collectors.publish_drive import build_service
from manager.tasks import DriveRecords, TaskError, now_iso, validate


DEFAULT_TTL_MINUTES = 60
LOCK_NAMESPACE = "_global"


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def normalize(value):
    return value.replace("\\", "/").rstrip("/").lower() if value else None


def repository_key(value):
    return normalize(value).removesuffix(".git") if value else None


def paths_overlap(left, right):
    for first in map(normalize, left):
        for second in map(normalize, right):
            if first == second or first.startswith(second + "/") or second.startswith(first + "/"):
                return True
    return False


def active(lock, at):
    return lock["status"] == "active" and parse_time(lock["expires_at"]) > at


def list_locks(store, project_id=None):
    try:
        locks = store.list_records("worktree_locks", LOCK_NAMESPACE)
    except TaskError as exc:
        if "folder not found" in str(exc):
            return []
        raise
    for lock in locks:
        validate("worktree_lock", lock)
    if project_id:
        locks = [lock for lock in locks if lock["project_id"] == project_id]
    return sorted(locks, key=lambda item: item["created_at"])


def conflict_reason(candidate, existing):
    if candidate["access"] == "read_only" or existing["access"] == "read_only":
        return None
    if not candidate.get("repository") or not existing.get("repository"):
        return "unknown repository for production write"
    if repository_key(candidate["repository"]) != repository_key(existing["repository"]):
        return None
    if not candidate.get("branch") or not existing.get("branch"):
        return "unknown branch for production write"
    if normalize(candidate["branch"]) == normalize(existing["branch"]):
        return "same repository and branch"
    if not candidate.get("scope") or not existing.get("scope"):
        return "unknown file scope for production write"
    if paths_overlap(candidate["scope"], existing["scope"]):
        return "overlapping file scope"
    return None


def check(store, candidate, at=None, exclude_lock_id=None):
    at = at or datetime.now(timezone.utc)
    missing = [key for key in ("repository", "branch", "baseline_head", "scope") if candidate["access"] == "production" and not candidate.get(key)]
    if missing:
        return {"safe": False, "conflicts": [{"lock_id": None, "reason": f"unknown production write fields: {', '.join(missing)}"}]}
    conflicts = []
    for lock in list_locks(store):
        if lock["lock_id"] == exclude_lock_id or not active(lock, at):
            continue
        reason = conflict_reason(candidate, lock)
        if reason:
            conflicts.append({"lock_id": lock["lock_id"], "reason": reason})
    return {"safe": not conflicts, "conflicts": conflicts}


def acquire(store, lock_id, project_id, task_id, execution_id, provider, repository=None, branch=None, scope=None, baseline_head=None, access="production", session_id=None, ttl_minutes=DEFAULT_TTL_MINUTES, at=None):
    at = at or datetime.now(timezone.utc)
    if ttl_minutes <= 0:
        raise TaskError("ttl_minutes must be positive")
    scope = list(dict.fromkeys(scope or []))
    if access == "production" and not all((repository, branch, baseline_head, scope)):
        raise TaskError("production lock requires repository, branch, baseline_head, and scope")
    candidate = {
        "lock_id": lock_id, "project_id": project_id, "task_id": task_id,
        "execution_id": execution_id, "provider": provider, "session_id": session_id,
        "repository": repository, "branch": branch, "scope": scope,
        "baseline_head": baseline_head, "access": access, "status": "active",
        "created_at": at.isoformat().replace("+00:00", "Z"),
        "expires_at": (at + timedelta(minutes=ttl_minutes)).isoformat().replace("+00:00", "Z"),
        "released_at": None,
    }
    validate("worktree_lock", candidate)
    try:
        existing = store.get("worktree_locks", LOCK_NAMESPACE, lock_id)
    except TaskError:
        existing = None
    if existing:
        identity = ("project_id", "task_id", "execution_id", "provider", "session_id", "repository", "branch", "scope", "baseline_head", "access")
        if active(existing, at) and all(existing[key] == candidate[key] for key in identity):
            return existing
        raise TaskError(f"lock_id already exists: {lock_id}")
    result = check(store, candidate, at)
    if not result["safe"]:
        raise TaskError(f"working tree conflict: {json.dumps(result['conflicts'])}")
    store.put("worktree_locks", LOCK_NAMESPACE, lock_id, candidate)
    raced = check(store, candidate, at, exclude_lock_id=lock_id)
    if not raced["safe"]:
        release(store, project_id, lock_id, at)
        raise TaskError(f"working tree conflict after acquire: {json.dumps(raced['conflicts'])}")
    return candidate


def inspect(store, project_id, lock_id, at=None):
    lock = store.get("worktree_locks", LOCK_NAMESPACE, lock_id)
    validate("worktree_lock", lock)
    if lock["project_id"] != project_id:
        raise TaskError(f"lock does not belong to project: {project_id}")
    return {**lock, "effective_status": "active" if active(lock, at or datetime.now(timezone.utc)) else "expired" if lock["status"] == "active" else "released"}


def release(store, project_id, lock_id, at=None):
    lock = store.get("worktree_locks", LOCK_NAMESPACE, lock_id)
    validate("worktree_lock", lock)
    if lock["project_id"] != project_id:
        raise TaskError(f"lock does not belong to project: {project_id}")
    if lock["status"] == "released":
        return lock
    lock.update(status="released", released_at=(at or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"))
    validate("worktree_lock", lock)
    return store.put("worktree_locks", LOCK_NAMESPACE, lock_id, lock)


def candidate_from_args(args):
    return {
        "lock_id": getattr(args, "lock_id", None) or "preflight", "project_id": args.project_id,
        "task_id": args.task_id, "execution_id": args.execution_id,
        "provider": args.provider, "session_id": args.session_id,
        "repository": args.repository, "branch": args.branch,
        "scope": args.scope, "baseline_head": args.baseline_head,
        "access": "read_only" if args.read_only else "production",
        "status": "active", "created_at": now_iso(), "expires_at": now_iso(),
        "released_at": None,
    }


def add_candidate_arguments(parser, lock_id=True):
    if lock_id:
        parser.add_argument("lock_id")
    parser.add_argument("project_id"); parser.add_argument("task_id"); parser.add_argument("execution_id")
    parser.add_argument("--provider", required=True); parser.add_argument("--session-id")
    parser.add_argument("--repository"); parser.add_argument("--branch"); parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--baseline-head"); parser.add_argument("--read-only", action="store_true")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    add_candidate_arguments(sub.add_parser("acquire")); add_candidate_arguments(sub.add_parser("check"), False)
    read = sub.add_parser("inspect"); read.add_argument("project_id"); read.add_argument("lock_id")
    release_parser = sub.add_parser("release"); release_parser.add_argument("project_id"); release_parser.add_argument("lock_id")
    listing = sub.add_parser("list"); listing.add_argument("project_id")
    sub.choices["acquire"].add_argument("--ttl-minutes", type=float, default=DEFAULT_TTL_MINUTES)
    args = parser.parse_args()
    try:
        store = DriveRecords(build_service())
        if args.command == "acquire":
            data = candidate_from_args(args); result = acquire(store, ttl_minutes=args.ttl_minutes, **{key: data[key] for key in ("lock_id", "project_id", "task_id", "execution_id", "provider", "repository", "branch", "scope", "baseline_head", "access", "session_id")})
        elif args.command == "check": result = check(store, candidate_from_args(args))
        elif args.command == "inspect": result = inspect(store, args.project_id, args.lock_id)
        elif args.command == "release": result = release(store, args.project_id, args.lock_id)
        else: result = [inspect(store, args.project_id, item["lock_id"]) for item in list_locks(store, args.project_id)]
        print(json.dumps(result, indent=2)); return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
