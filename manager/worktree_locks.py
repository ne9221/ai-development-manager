#!/usr/bin/env python3
"""Atomic GCS-backed coarse repository writer leases."""

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from manager.gcs_lock_registry import GCSLockRegistry, RegistryConflict
from manager.session_identity import parse_manager_session_key
from manager.tasks import TaskError, validate


DEFAULT_TTL_MINUTES = 60
MAX_TTL_MINUTES = 120
TOKEN_ENV = "AI_MANAGER_LEASE_TOKEN"
REGISTRY_VERSION = "0.2.0"
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
GITHUB_PART_RE = re.compile(r"[A-Za-z0-9_.-]+")
GLOB_RE = re.compile(r"[*?\[\]{}]")


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise TaskError("lock timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def canonical_repository(value):
    if not isinstance(value, str) or not value.strip():
        raise TaskError("production repository remote is required")
    value = value.strip()
    scp = re.fullmatch(r"git@github\.com:([^/]+)/(.+)", value, re.I)
    if scp:
        owner, repo = scp.groups()
    else:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("https", "ssh") or (parsed.hostname or "").lower() != "github.com":
            raise TaskError("production repository must be a GitHub HTTPS or SSH remote")
        if parsed.username not in (None, "git") or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise TaskError("repository remote contains credentials or unsupported URL components")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            raise TaskError("repository remote must identify exactly owner/repository")
        owner, repo = parts
    repo = repo.removesuffix(".git")
    if not GITHUB_PART_RE.fullmatch(owner) or not GITHUB_PART_RE.fullmatch(repo):
        raise TaskError("repository owner/name contains unsupported characters")
    return f"github:{owner.lower()}/{repo.lower()}"


def repository_lock_id(repository):
    canonical = canonical_repository(repository) if not repository.startswith("github:") else repository
    if not re.fullmatch(r"github:[a-z0-9_.-]+/[a-z0-9_.-]+", canonical):
        raise TaskError("repository identity is not canonical")
    return "repo-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_branch(value):
    if not isinstance(value, str) or not value.strip():
        raise TaskError("production branch is required")
    value = value.strip()
    if value == "HEAD" or COMMIT_RE.fullmatch(value.lower()):
        raise TaskError("detached HEAD is not supported")
    if value.startswith("refs/") and not value.startswith("refs/heads/"):
        raise TaskError("production branch must be a local heads ref")
    ref = value if value.startswith("refs/heads/") else f"refs/heads/{value}"
    tail = ref.removeprefix("refs/heads/")
    if not tail or "\\" in tail or ".." in tail or "@{" in tail or tail.startswith("/") or tail.endswith(("/", ".", ".lock")) or "//" in tail or any(ord(char) < 32 or char in " ~^:?*[" for char in tail):
        raise TaskError("invalid canonical branch ref")
    return ref


def canonical_scope(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise TaskError("production scope must contain repo-relative paths")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise TaskError("scope paths must be non-empty strings")
        path = value.strip().replace("\\", "/").rstrip("/") or "."
        if path == ".":
            normalized.append(path); continue
        if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or "://" in path or GLOB_RE.search(path):
            raise TaskError("scope must not contain absolute paths, URLs, or globs")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise TaskError("scope contains unresolved or escaping path segments")
        normalized.append("/".join(parts))
    if "." in normalized:
        return ["."]
    return sorted(set(normalized))


def canonical_baseline(value):
    value = value.lower() if isinstance(value, str) else ""
    if not COMMIT_RE.fullmatch(value):
        raise TaskError("baseline_head must be a full Git commit hash")
    return value


def _git(cwd, *arguments, runner=subprocess.run):
    result = runner(["git", "-C", str(cwd), *arguments], text=True, encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode:
        raise TaskError(f"local Git preflight failed: {' '.join(arguments)}")
    return (result.stdout or "").strip()


def validate_local_preflight(working_directory, repository, branch, baseline_head, runner=subprocess.run):
    if not working_directory or not Path(working_directory).is_dir():
        raise TaskError("production acquire requires an existing working directory")
    remote_values = [line for line in _git(working_directory, "remote", "get-url", "--all", "origin", runner=runner).splitlines() if line]
    if len(remote_values) != 1 or canonical_repository(remote_values[0]) != repository:
        raise TaskError("working clone origin does not match requested repository")
    actual_branch = canonical_branch(_git(working_directory, "symbolic-ref", "--quiet", "HEAD", runner=runner))
    if actual_branch != branch:
        raise TaskError("working clone branch does not match requested branch")
    actual_head = canonical_baseline(_git(working_directory, "rev-parse", "HEAD", runner=runner))
    if actual_head != baseline_head:
        raise TaskError("working clone HEAD does not match declared baseline_head")
    return {"repository": repository, "branch": branch, "baseline_head": baseline_head}


def validate_ttl(minutes):
    if not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes <= 0 or minutes > MAX_TTL_MINUTES:
        raise TaskError(f"ttl_minutes must be between 0 and {MAX_TTL_MINUTES}")
    return float(minutes)


def token_hash(token):
    if not isinstance(token, str) or len(token) < 32:
        raise TaskError("valid lease owner token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def owner_fields(project_id, task_id, execution_id, provider, session_id=None):
    values = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise TaskError("project/task/execution/provider owner fields are required")
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise TaskError("session metadata must be a non-empty string")
    return values


def semantic_lock(lock, key=None):
    validate("worktree_lock", lock)
    if key is not None and (key != lock["lock_id"] or key != repository_lock_id(lock["repository"])):
        raise TaskError("registry key does not match canonical repository lock ID")
    if canonical_repository(lock["repository"].replace("github:", "https://github.com/", 1)) != lock["repository"]:
        raise TaskError("lock repository is not canonical")
    if canonical_branch(lock["branch"]) != lock["branch"] or canonical_scope(lock["scope"]) != lock["scope"] or canonical_baseline(lock["baseline_head"]) != lock["baseline_head"]:
        raise TaskError("lock branch/scope/baseline is not canonical")
    created, updated, expires = map(parse_time, (lock["created_at"], lock["updated_at"], lock["expires_at"]))
    if not created <= updated or not created < expires:
        raise TaskError("lock timestamp ordering is invalid")
    if lock["status"] == "active" and not updated < expires:
        raise TaskError("active lock must expire after its last update")
    if lock["released_at"] is not None and parse_time(lock["released_at"]) < created:
        raise TaskError("released_at precedes created_at")
    return lock


def validate_registry(document):
    validate("worktree_lock_registry", document)
    for key, lock in document["locks"].items():
        semantic_lock(lock, key)
    return document


def active(lock, at):
    return lock["status"] == "active" and parse_time(lock["expires_at"]) > at


def public_lock(lock, at, lease_token=None):
    result = {key: value for key, value in lock.items() if key != "lease_token_hash"}
    result["effective_status"] = "active" if active(lock, at) else "expired" if lock["status"] == "active" else "released"
    if lease_token is not None:
        result["lease_token"] = lease_token
    return result


def same_owner(lock, owner, lease_token):
    return all(lock[key] == value for key, value in owner.items()) and secrets.compare_digest(lock["lease_token_hash"], token_hash(lease_token))


def read_registry(registry):
    document, etag, server_time = registry.read()
    if not isinstance(server_time, datetime) or server_time.tzinfo is None:
        raise TaskError("registry did not provide timezone-aware server time")
    return validate_registry(document), etag, server_time.astimezone(timezone.utc)


def candidate(project_id, task_id, execution_id, provider, session_id=None, repository=None, branch=None, scope=None, baseline_head=None):
    owner = owner_fields(project_id, task_id, execution_id, provider, session_id)
    repository = canonical_repository(repository)
    return {**owner, "session_id": session_id, "lock_id": repository_lock_id(repository), "repository": repository, "branch": canonical_branch(branch), "scope": canonical_scope(scope), "baseline_head": canonical_baseline(baseline_head), "access": "production"}


def check(registry, requested, working_directory=None, preflight_func=validate_local_preflight):
    if requested.get("access") not in ("production", "read_only"):
        raise TaskError("access must be production or read_only")
    if requested.get("access") == "read_only":
        owner_fields(*(requested.get(key) for key in ("project_id", "task_id", "execution_id", "provider", "session_id")))
        return {"authority": "advisory_only", "safe": True, "conflicts": [], "note": "read-only cannot be upgraded; production writes require acquire"}
    item = candidate(*(requested.get(key) for key in ("project_id", "task_id", "execution_id", "provider", "session_id", "repository", "branch", "scope", "baseline_head")))
    preflight_func(working_directory, item["repository"], item["branch"], item["baseline_head"])
    document, _, now = read_registry(registry)
    existing = document["locks"].get(item["lock_id"])
    conflicts = [] if not existing or not active(existing, now) else [{"lock_id": item["lock_id"], "reason": "repository already has an active production writer"}]
    return {"authority": "advisory_only", "safe": not conflicts, "conflicts": conflicts, "candidate": {key: value for key, value in item.items() if key != "lease_token_hash"}}


def acquire(registry, project_id, task_id, execution_id, provider, session_id=None, repository=None, branch=None, scope=None, baseline_head=None, working_directory=None, ttl_minutes=DEFAULT_TTL_MINUTES, lease_token=None, access="production", preflight_func=validate_local_preflight, attempts=5):
    if access != "production":
        raise TaskError("read-only work cannot acquire or upgrade a writer lease")
    ttl = validate_ttl(ttl_minutes)
    item = candidate(project_id, task_id, execution_id, provider, session_id, repository, branch, scope, baseline_head)
    preflight_func(working_directory, item["repository"], item["branch"], item["baseline_head"])
    owner = owner_fields(project_id, task_id, execution_id, provider, session_id)
    supplied_token = lease_token
    lease_token = lease_token or secrets.token_urlsafe(32)
    digest = token_hash(lease_token)
    for _ in range(attempts):
        document, etag, now = read_registry(registry)
        existing = document["locks"].get(item["lock_id"])
        if existing and active(existing, now):
            if supplied_token and same_owner(existing, owner, supplied_token):
                return {"authority": "acquired", **public_lock(existing, now, supplied_token)}
            raise TaskError("repository already has an active production writer")
        generation = (existing or {}).get("generation", 0) + 1
        record = {**item, "status": "active", "generation": generation, "lease_token_hash": digest, "created_at": iso(now), "updated_at": iso(now), "expires_at": iso(now + timedelta(minutes=ttl)), "released_at": None}
        semantic_lock(record, item["lock_id"])
        updated = {**document, "locks": {**document["locks"], item["lock_id"]: record}}
        try:
            registry.cas(etag, updated)
            return {"authority": "acquired", **public_lock(record, now, lease_token)}
        except RegistryConflict:
            continue
    raise TaskError("writer lease contention did not settle; production write blocked")


def _owned_update(registry, lock_id, owner, lease_token, action, ttl_minutes=None, session_id=None, attempts=5):
    digest = token_hash(lease_token)
    ttl = validate_ttl(ttl_minutes) if ttl_minutes is not None else None
    for _ in range(attempts):
        document, etag, now = read_registry(registry)
        lock = document["locks"].get(lock_id)
        if not lock or not all(lock[key] == value for key, value in owner.items()) or not secrets.compare_digest(lock["lease_token_hash"], digest):
            raise TaskError("lease owner verification failed")
        if action == "renew":
            if not active(lock, now):
                raise TaskError("expired lease cannot be renewed; acquire a new generation")
            changed = {**lock, "updated_at": iso(now), "expires_at": iso(now + timedelta(minutes=ttl))}
        elif action == "release":
            if lock["status"] == "released":
                return public_lock(lock, now)
            changed = {**lock, "status": "released", "updated_at": iso(now), "released_at": iso(now)}
        elif action == "link session":
            if not active(lock, now):
                raise TaskError("session cannot be linked to an inactive lease")
            if lock.get("session_id") == session_id:
                return public_lock(lock, now)
            if lock.get("session_id") is not None:
                raise TaskError("lease is already linked to another session")
            changed = {**lock, "session_id": session_id, "updated_at": iso(now)}
        else:
            raise TaskError("invalid lease owner action")
        semantic_lock(changed, lock_id)
        updated = {**document, "locks": {**document["locks"], lock_id: changed}}
        try:
            registry.cas(etag, updated)
            return public_lock(changed, now)
        except RegistryConflict:
            continue
    raise TaskError(f"lease {action} contention did not settle; production write blocked")


def renew(registry, lock_id, project_id, task_id, execution_id, provider, session_id=None, lease_token=None, ttl_minutes=DEFAULT_TTL_MINUTES):
    return _owned_update(registry, lock_id, owner_fields(project_id, task_id, execution_id, provider, session_id), lease_token, "renew", ttl_minutes)


def release(registry, lock_id, project_id, task_id, execution_id, provider, session_id=None, lease_token=None):
    return _owned_update(registry, lock_id, owner_fields(project_id, task_id, execution_id, provider, session_id), lease_token, "release")


def reconcile_unlinked_terminal_lease(registry, lock_id, project_id, task_id, execution_id, provider,
                                     terminal_status, attempts=5):
    """Release a writer lease left by a proven prelaunch terminal rollback.

    This is deliberately narrower than ``release``: it accepts no token and
    can only CAS an exact owner match whose lease has never been linked to a
    provider Session and whose execution is already terminal before the
    caller invokes this governance recovery path.  Running/completed leases,
    linked leases, and owner mismatches remain refused.
    """
    if terminal_status not in {"cancelled", "failed", "interrupted"}:
        raise TaskError("terminal lease reconciliation requires a non-running terminal status")
    owner = owner_fields(project_id, task_id, execution_id, provider, None)
    for _ in range(attempts):
        document, etag, now = read_registry(registry)
        lock = document["locks"].get(lock_id)
        if not lock:
            return {"status": "clean", "released": False, "reason": "lock_not_found"}
        if not all(lock.get(key) == value for key, value in owner.items() if key != "session_id"):
            raise TaskError("terminal lease reconciliation owner mismatch")
        if lock.get("session_id") is not None:
            raise TaskError("terminal lease reconciliation refuses a linked provider session")
        if lock.get("status") == "released":
            return public_lock(lock, now)
        if lock.get("status") != "active":
            raise TaskError("terminal lease reconciliation found an invalid lock status")
        changed = {**lock, "status": "released", "updated_at": iso(now), "released_at": iso(now)}
        semantic_lock(changed, lock_id)
        updated = {**document, "locks": {**document["locks"], lock_id: changed}}
        try:
            registry.cas(etag, updated)
            return public_lock(changed, now)
        except RegistryConflict:
            continue
    raise TaskError("terminal lease reconciliation contention did not settle")


def verify_released_terminal_lease(registry, lock_id, project_id, task_id, execution_id, provider,
                                   generation, session_id=None):
    """Prove that an exact writer generation is already safely released.

    This is used only after an owning provider process has been independently
    proven stopped.  It never changes the registry and refuses a missing,
    active, mismatched, or differently-linked generation.
    """
    if not isinstance(generation, int) or generation < 1:
        raise TaskError("released lease verification requires a valid generation")
    document, _, now = read_registry(registry)
    lock = document["locks"].get(lock_id)
    if not lock:
        raise TaskError("released lease verification found no lock")
    owner = owner_fields(project_id, task_id, execution_id, provider, lock.get("session_id"))
    if any(lock.get(key) != value for key, value in owner.items()):
        raise TaskError("released lease verification owner mismatch")
    if lock.get("generation") != generation:
        raise TaskError("released lease verification generation mismatch")
    if lock.get("session_id") not in (None, session_id):
        raise TaskError("released lease verification session mismatch")
    if lock.get("status") != "released":
        raise TaskError("released lease verification requires a released lock")
    return public_lock(lock, now)


def link_session(registry, lock_id, project_id, task_id, execution_id, provider, session_id, lease_token, attempts=5):
    owner = owner_fields(project_id, task_id, execution_id, provider, session_id)
    parsed = parse_manager_session_key(session_id)
    if not parsed or parsed[0] != provider:
        raise TaskError("canonical session provider does not match lease provider")
    return _owned_update(registry, lock_id, owner, lease_token, "link session", session_id=session_id, attempts=attempts)


def inspect(registry, lock_id):
    document, _, now = read_registry(registry)
    lock = document["locks"].get(lock_id)
    if not lock:
        raise TaskError(f"lock not found: {lock_id}")
    return public_lock(lock, now)


def list_locks(registry, project_id=None):
    document, _, now = read_registry(registry)
    locks = document["locks"].values()
    return [public_lock(lock, now) for lock in sorted(locks, key=lambda value: value["lock_id"]) if not project_id or lock["project_id"] == project_id]


def add_registry_arguments(parser):
    parser.add_argument("--gcs-bucket")
    parser.add_argument("--gcs-object")


def registry(args):
    return GCSLockRegistry.from_environment(args.gcs_bucket, args.gcs_object)


def add_owner_arguments(parser, session_required=False):
    parser.add_argument("project_id"); parser.add_argument("task_id"); parser.add_argument("execution_id")
    parser.add_argument("--provider", required=True); parser.add_argument("--session-id", required=session_required)


def add_candidate_arguments(parser, read_only=False):
    add_owner_arguments(parser)
    parser.add_argument("--repository"); parser.add_argument("--branch"); parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--baseline-head"); parser.add_argument("--working-directory")
    if read_only:
        parser.add_argument("--read-only", action="store_true")
    add_registry_arguments(parser)


def main():
    parser = argparse.ArgumentParser(description="GCS generation-CAS repository writer leases; check is advisory, acquire is authoritative")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("registry-init", help="create the GCS registry object with ifGenerationMatch=0"); add_registry_arguments(init)
    acquire_parser = sub.add_parser("acquire"); add_candidate_arguments(acquire_parser); acquire_parser.add_argument("--ttl-minutes", type=float, default=DEFAULT_TTL_MINUTES)
    check_parser = sub.add_parser("check", help="advisory preview only; never authorizes writes"); add_candidate_arguments(check_parser, True)
    for command in ("renew", "release"):
        item = sub.add_parser(command); item.add_argument("lock_id"); add_owner_arguments(item); add_registry_arguments(item)
        if command == "renew": item.add_argument("--ttl-minutes", type=float, default=DEFAULT_TTL_MINUTES)
    link = sub.add_parser("link-session"); link.add_argument("lock_id"); add_owner_arguments(link, session_required=True); add_registry_arguments(link)
    read = sub.add_parser("inspect"); read.add_argument("lock_id"); add_registry_arguments(read)
    listing = sub.add_parser("list"); listing.add_argument("--project-id"); add_registry_arguments(listing)
    args = parser.parse_args()
    try:
        backend = registry(args)
        if args.command == "registry-init":
            result = {"generation": backend.create_if_absent({"schema_version": REGISTRY_VERSION, "locks": {}}), "bucket": backend.bucket, "object": backend.object_name}
        else:
            if args.command in ("acquire", "check"):
                data = {key: getattr(args, key) for key in ("project_id", "task_id", "execution_id", "provider", "session_id", "repository", "branch", "scope", "baseline_head")}
                if args.command == "acquire":
                    result = acquire(backend, **data, working_directory=args.working_directory, ttl_minutes=args.ttl_minutes, lease_token=os.environ.get(TOKEN_ENV))
                else:
                    result = check(backend, {**data, "access": "read_only" if args.read_only else "production"}, args.working_directory)
            elif args.command in ("renew", "release", "link-session"):
                token = os.environ.get(TOKEN_ENV)
                if not token: raise TaskError(f"lease token required via {TOKEN_ENV}")
                owner = {key: getattr(args, key) for key in ("project_id", "task_id", "execution_id", "provider", "session_id")}
                if args.command == "renew": result = renew(backend, args.lock_id, **owner, lease_token=token, ttl_minutes=args.ttl_minutes)
                elif args.command == "release": result = release(backend, args.lock_id, **owner, lease_token=token)
                else: result = link_session(backend, args.lock_id, **owner, lease_token=token)
            elif args.command == "inspect": result = inspect(backend, args.lock_id)
            else: result = list_locks(backend, args.project_id)
        print(json.dumps(result, indent=2)); return 0
    except (TaskError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
