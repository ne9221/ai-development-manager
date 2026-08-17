"""Bounded, Drive-backed command watcher that delegates every launch to execution_runner."""

import argparse
import json
import os
import socket
import time
import urllib.request
from datetime import datetime, timezone

from collectors.publish_drive import build_service
from manager.claude_launcher import ClaudeLauncher
from manager.codex_launcher import CodexLauncher, process_identity_state
from manager.execution_lifecycle import terminalize_execution
from manager.execution_runner import launch_task
from manager.executions import cancel_reserved_execution, execution_health, prepare_task_retry
from manager.gcs_lock_registry import GCSLockRegistry
from manager.quota_reader import read_drive_status, summarize
from manager.runtime_bridge import all_projects
from manager.task_claims import check_task_execution_claim, task_claim_registry
from manager.tasks import DriveRecords, TaskError, now_iso, validate


POLL_SECONDS = 60
MAX_POLL_SECONDS = 900
CLAIM_TIMEOUT_SECONDS = 20 * 60
MAX_COMMANDS_PER_POLL = 4

# A queued command may only be launched if its (project_id, task_id) is an
# exact entry in this allowlist AND the live Task still satisfies every one
# of these policies. Absence of a config (unset env var, missing/unreadable/
# malformed file) means an empty allowlist -- zero launches -- never "assume
# permissive because nothing else is queued."
REQUIRED_TASK_POLICIES = frozenset({"disposable", "read_only", "no_repo_writes", "no_external_writes"})


def execution_id(command):
    return f"command-{command['command_id']}"


def load_allowlist(path=None):
    """Read the hands-off launch allowlist; any failure degrades to empty (no launches)."""
    path = path or os.environ.get("ADM_WATCHER_ALLOWLIST_PATH")
    if not path:
        return frozenset()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return frozenset()
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return frozenset()
    allowed = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        project_id, task_id = entry.get("project_id"), entry.get("task_id")
        if isinstance(project_id, str) and project_id and isinstance(task_id, str) and task_id:
            allowed.add((project_id, task_id))
    return frozenset(allowed)


def _policy_satisfied(task):
    """Even an allowlisted task must independently prove it is still disposable/read-only."""
    if task.get("read_only") is not True:
        return False
    policies = task.get("execution_policies")
    return isinstance(policies, list) and REQUIRED_TASK_POLICIES.issubset(set(policies))


def session_center_healthy(url=None, timeout=2.0):
    """Fail-closed liveness probe: any unreachable/unexpected response is unhealthy.

    Deliberately hits only /health (no Drive dependency of its own), so this
    can never be confused with an already-running execution's authority --
    that is handled entirely by the existing recovery/lifecycle path and is
    never touched by this check.
    """
    url = url or os.environ.get("ADM_SESSION_CENTER_HEALTH_URL", "http://127.0.0.1:8765/health")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read())
    except Exception:
        return False
    return isinstance(body, dict) and body.get("status") == "ok"


def provider_quota_reliable(service, provider):
    """Fail-closed: stale/unknown/unreachable quota is never treated as
    "enough to launch", for any provider. Reuses quota_reader.summarize()'s
    own reliability computation unchanged -- no scoring/routing rework, just
    a pre-launch gate on the same has_reliable_quota signal dispatch()
    already computes but does not itself hard-block on. A provider's
    has_reliable_quota is read from that provider's own quota_reader entry
    only; one provider's fresh quota can never satisfy another's gate.
    """
    try:
        quota = summarize(read_drive_status(service=service), max_age_minutes=60)
    except Exception:
        return False
    entry = next((item for item in quota["providers"] if item["provider"] == provider), None)
    return bool(entry and entry.get("has_reliable_quota"))


def codex_quota_reliable(service):
    return provider_quota_reliable(service, "codex")


def claude_quota_reliable(service):
    return provider_quota_reliable(service, "claude")


# provider -> {launcher_factory, quota_check}. An unrecognized provider must
# never fall back to Codex's (or any other provider's) launcher or quota
# gate -- resolve_provider_runtime() returns None for it, which callers treat
# as an unconditional reject.
PROVIDER_RUNTIMES = {
    "codex": {"launcher_factory": CodexLauncher, "quota_check": codex_quota_reliable},
    "claude": {"launcher_factory": ClaudeLauncher, "quota_check": claude_quota_reliable},
}


def resolve_provider_runtime(provider):
    return PROVIDER_RUNTIMES.get(provider)


def _result(status, execution_id_value, session_id=None, error_kind=None):
    return {"status": status, "execution_id": execution_id_value, "session_id": session_id, "error_kind": error_kind}


def _write(store, command):
    validate("command", command)
    return store.put("commands", command["project_id"], command["command_id"], command)


def _claimed(command):
    # ponytail: deterministic claim content lets competing watchers converge; task claim remains launch authority.
    return {**command, "status": "claimed", "execution_id": execution_id(command),
            "claimed_at": now_iso(), "completed_at": None, "result": None}


def _terminal(command, status, result):
    return {**command, "status": status, "completed_at": now_iso(), "result": result,
            "recovery_reason": command.get("recovery_reason"), "stale_at": command.get("stale_at")}


def _attention(store, command, execution, reason):
    timestamp = command.get("stale_at") or now_iso()
    # Legacy uncertain executions have no heartbeat/process contract. Surface
    # their Command, but do not rewrite the execution authority record.
    if execution and (execution.get("heartbeat_at") or execution.get("provider_evidence")):
        execution.update(stale_at=execution.get("stale_at") or timestamp, recovery_reason=reason)
        validate("execution", execution)
        store.put("executions", command["project_id"], command["execution_id"], execution)
        task = store.get("tasks", command["project_id"], command["task_id"])
        if (task.get("source_context") or {}).get("active_execution_id") == command["execution_id"]:
            task.update(status="blocked", updated_at=timestamp,
                        blocked_reason=f"Execution recovery required: {reason}",
                        current_progress="Execution requires attention",
                        next_action="Verify provider/process and authority evidence; do not start a duplicate")
            validate("task", task)
            store.put("tasks", command["project_id"], command["task_id"], task)
    marked = {**command, "status": "attention", "stale_at": timestamp,
              "recovery_reason": reason, "completed_at": None, "result": None}
    _write(store, marked)
    return {"status": "attention", "execution_id": command.get("execution_id"), "recovery_reason": reason}


def _provider_state(execution):
    evidence = execution.get("provider_evidence") or {}
    if evidence.get("host") != socket.gethostname()[:100]:
        return "unknown"
    return process_identity_state(evidence.get("pid"), evidence.get("creation_identity"))


def _block_prelaunch_task(store, command, reason):
    task = store.get("tasks", command["project_id"], command["task_id"])
    task.update(status="blocked", blocked_reason=f"Execution failed before provider authority: {reason}",
                updated_at=now_iso(), current_progress="Execution did not start",
                next_action="Correct the task contract and submit a linked bounded retry")
    validate("task", task)
    store.put("tasks", command["project_id"], command["task_id"], task)


def _claim_registry(command, claim_factory):
    return claim_factory(os.environ.get("ADM_LOCK_GCS_BUCKET"), command["project_id"], command["task_id"])


def _reconcile_active(store, service, command, claim_factory):
    terminal = _existing_terminal(store, command)
    if terminal:
        _write(store, terminal)
        return {"status": terminal["status"], "reconciled": True}
    try:
        execution = store.get("executions", command["project_id"], command["execution_id"])
        validate("execution", execution)
    except TaskError:
        if command["status"] == "claimed" and not _claim_expired(command):
            return {"status": "claimed", "skipped": True}
        if command["status"] == "claimed" and _claim_expired(command):
            failed = _terminal(command, "failed", _result("error", command["execution_id"], error_kind="claim_timeout"))
            _write(store, failed)
            return {"status": "failed", "reconciled": True}
        return _attention(store, command, None, "execution_record_missing_or_invalid")

    claim_registry = _claim_registry(command, claim_factory)
    if execution["status"] == "reserved":
        try:
            cancelled = cancel_reserved_execution(
                store, claim_registry, command["project_id"], command["execution_id"],
                "prelaunch failure left a reservation without provider authority",
            )
        except TaskError:
            return _attention(store, command, execution, "reserved_execution_authority_inconsistent")
        _block_prelaunch_task(store, command, "prelaunch_contract_or_gate_failure")
        failed = _terminal(command, "failed", _result("error", cancelled["execution_id"], error_kind="prelaunch_failed"))
        failed["recovery_reason"] = "prelaunch_contract_or_gate_failure"
        _write(store, failed)
        return {"status": "failed", "reconciled": True}
    if execution["status"] == "cancelled":
        _block_prelaunch_task(store, command, "prelaunch_execution_cancelled")
        failed = _terminal(command, "failed", _result("error", command["execution_id"], error_kind="prelaunch_failed"))
        _write(store, failed)
        return {"status": "failed", "reconciled": True}
    if execution["status"] != "running":
        return _attention(store, command, execution, "execution_lifecycle_inconsistent")

    health = execution_health(execution)
    provider = _provider_state(execution)
    if health["state"] == "healthy" and provider == "live":
        if command["status"] == "attention":
            healthy = {**command, "status": "running", "stale_at": None, "recovery_reason": None}
            _write(store, healthy)
            task = store.get("tasks", command["project_id"], command["task_id"])
            if (task.get("source_context") or {}).get("active_execution_id") == command["execution_id"]:
                task.update(status="in_progress", updated_at=now_iso(), blocked_reason=None,
                            current_progress=f"Execution {command['execution_id']} running",
                            next_action="Continue provider supervision")
                validate("task", task)
                store.put("tasks", command["project_id"], command["task_id"], task)
        return {"status": "running", "healthy": True, "over_expected": health["over_expected"]}

    if provider == "stopped" and health["state"] == "healthy":
        health = {**health, "state": "attention", "reason": "provider_process_stopped"}
    try:
        claim = check_task_execution_claim(claim_registry, command["project_id"], command["task_id"])
    except TaskError:
        claim = None
    exact_claim = claim and claim.get("execution_id") == execution["execution_id"]
    if provider == "stopped" and exact_claim and execution.get("access") == "read_only":
        terminalize_execution(
            store, service, None, claim_registry, command["project_id"], command["task_id"],
            execution["execution_id"], execution["provider"], "interrupted", claim["generation"], True,
            summary=f"Recovery: {health['reason']}; provider stop proven on owning host",
        )
        terminal = _existing_terminal(store, command)
        _write(store, terminal)
        return {"status": terminal["status"], "reconciled": True, "provider_state": provider}
    reason = health["reason"] or "provider_state_inconsistent"
    if provider == "stopped" and execution.get("access") == "production_write":
        reason = "provider_stopped_writer_authority_retained"
    elif not exact_claim:
        reason = "task_claim_missing_or_mismatched"
    elif provider == "replaced":
        reason = "provider_process_identity_replaced"
    elif provider != "stopped":
        reason = f"{reason}_provider_{provider}"
    return _attention(store, command, execution, reason)


def _claim_expired(command, now=None):
    try:
        claimed = datetime.fromisoformat(command["claimed_at"].replace("Z", "+00:00"))
        return (now or datetime.now(timezone.utc) - claimed).total_seconds() > CLAIM_TIMEOUT_SECONDS
    except (AttributeError, TypeError, ValueError):
        return True


def _existing_terminal(store, command):
    try:
        execution = store.get("executions", command["project_id"], command["execution_id"])
        validate("execution", execution)
    except TaskError:
        return None
    if execution.get("status") not in ("completed", "failed", "interrupted"):
        return None
    return _terminal(command, "completed" if execution["status"] == "completed" else "failed",
                     _result(execution["status"], command["execution_id"], execution.get("session_id")))


def process_command(store, service, command, launcher_factory=None, writer_factory=GCSLockRegistry.from_environment,
                    claim_factory=task_claim_registry, allowlist=frozenset(), health_check=session_center_healthy,
                    quota_check=None):
    """Claim/reconcile one command; a claimed command is never automatically relaunched.

    launcher_factory/quota_check are explicit-override escape hatches (tests
    use them directly); when not given, both resolve from PROVIDER_RUNTIMES
    by the command's own provider. An unrecognized provider is rejected here
    -- it never silently falls back to Codex's launcher or quota gate.
    """
    try:
        validate("command", command)
    except TaskError:
        return {"status": "rejected"}
    if command["status"] in ("completed", "failed"):
        return {"status": command["status"], "skipped": True}
    if command["status"] in ("claimed", "running", "attention"):
        # Already-running authority is governed entirely by existing
        # recovery/lifecycle; Session Center health never factors in here.
        return _reconcile_active(store, service, command, claim_factory)
    if command["status"] != "queued":
        return {"status": "rejected"}
    runtime = resolve_provider_runtime(command["provider"])
    if runtime is None:
        return {"status": "rejected", "reason": "unsupported_provider"}
    launcher_factory = launcher_factory or runtime["launcher_factory"]
    quota_check = quota_check or runtime["quota_check"]
    if (command["project_id"], command["task_id"]) not in allowlist:
        # Out of scope, not an anomaly: leave the command untouched (no write)
        # so unrelated projects/tasks are never mutated by this watcher.
        return {"status": "rejected", "reason": "not_allowlisted"}
    try:
        candidate_task = store.get("tasks", command["project_id"], command["task_id"])
        validate("task", candidate_task)
    except TaskError:
        return _attention(store, command, None, "allowlisted_task_missing_or_invalid")
    if not _policy_satisfied(candidate_task):
        return _attention(store, command, None, "allowlisted_task_policy_not_satisfied")
    if not health_check():
        # Transient/recoverable, not a policy problem: leave queued untouched,
        # no write, so this is retried automatically on the next poll.
        return {"status": "rejected", "reason": "session_center_unavailable"}
    if not quota_check(service):
        # Same treatment: stale/unknown Codex quota is transient, not a
        # policy violation -- retried automatically once quota data refreshes.
        return {"status": "rejected", "reason": "quota_unreliable"}

    retry_count = command.get("retry_count", 0)
    retry_of = command.get("retry_of_execution_id")
    if retry_count:
        try:
            prepare_task_retry(store, _claim_registry(command, claim_factory), command["project_id"],
                               command["task_id"], retry_of, retry_count)
        except (TaskError, KeyError):
            failed = _terminal(command, "failed", _result("error", None, error_kind="retry_refused"))
            failed["recovery_reason"] = "retry_authority_or_linkage_not_proven"
            _write(store, failed)
            return {"status": "failed", "reconciled": True}
    claimed = _claimed(command)
    _write(store, claimed)
    try:
        task = store.get("tasks", claimed["project_id"], claimed["task_id"])
        validate("task", task)
        claim_registry = claim_factory(os.environ.get("ADM_LOCK_GCS_BUCKET"), claimed["project_id"], claimed["task_id"])
        writer_registry = None if task.get("read_only") else writer_factory()
        running = {**claimed, "status": "running"}
        retry = ({"retry_count": retry_count, "retry_of_execution_id": retry_of} if retry_count else {})
        outcome = launch_task(store, service, writer_registry, claim_registry, launcher_factory(),
                              claimed["project_id"], claimed["task_id"], claimed["execution_id"], claimed["model"],
                              on_running=lambda _execution: _write(store, running), provider=claimed["provider"], **retry)
        terminal = outcome["terminal"]["execution"]
        dispatch = outcome["dispatch"]
        selected = {**running, "provider": dispatch["provider"], "model": dispatch["model"] or claimed["model"],
                    "fallback_model": dispatch["fallback_model"] or claimed["fallback_model"], "mode": dispatch["mode"],
                    "effort": dispatch["effort"], "selection_reason": dispatch["selection_reason"],
                    "quota_evidence": dispatch["quota_evidence"]}
        final = _terminal(selected, "completed" if terminal["status"] == "completed" else "failed",
                          _result(terminal["status"], claimed["execution_id"], outcome["session"].get("session_id")))
    except Exception as exc:
        terminal = _existing_terminal(store, claimed)
        if terminal:
            final = terminal
        else:
            try:
                existing = store.get("executions", claimed["project_id"], claimed["execution_id"])
                if existing.get("status") in ("reserved", "running"):
                    return _reconcile_active(store, service, running, claim_factory)
            except TaskError:
                pass
            kind = getattr(exc, "classification", None) or type(exc).__name__
            final = _terminal(claimed, "failed", _result("error", claimed["execution_id"], error_kind=str(kind)[:100]))
    _write(store, final)
    return {"status": final["status"], "execution_id": claimed["execution_id"]}


def poll_once(store, service, allowlist=None, **factories):
    if allowlist is None:
        allowlist = load_allowlist()
    results = []
    for project in all_projects(store):
        try:
            commands = store.list_records("commands", project["project_id"])
        except TaskError:
            continue
        for command in commands:
            if command.get("status") in ("completed", "failed"):
                continue
            if len(results) == MAX_COMMANDS_PER_POLL:
                return results
            results.append(process_command(store, service, command, allowlist=allowlist, **factories))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll Drive commands and run Codex through ADM execution_runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=POLL_SECONDS)
    args = parser.parse_args(argv)
    if not 10 <= args.interval_seconds <= MAX_POLL_SECONDS:
        raise SystemExit("interval-seconds must be from 10 to 900")
    while True:
        try:
            service = build_service()
            result = poll_once(DriveRecords(service), service)
            print(json.dumps({"status": "ok", "host": socket.gethostname()[:100], "commands": result}, separators=(",", ":")))
        except Exception:
            print(json.dumps({"status": "unavailable"}, separators=(",", ":")))
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
