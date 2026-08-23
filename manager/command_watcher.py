"""Bounded, Drive-backed command watcher that delegates every launch to execution_runner."""

import argparse
import json
import os
import socket
import time
import urllib.request
from datetime import datetime, timezone

from collectors.publish_drive import build_service
from manager.claude_account_selector import load_claude_accounts
from manager.ag_runner import AgRunner
from manager.claude_launcher import ClaudeLauncher
from manager.codex_launcher import CodexLauncher, process_identity_state
from manager.execution_lifecycle import terminalize_execution
from manager.execution_runner import launch_task
from manager.executions import cancel_reserved_execution, execution_health, prepare_task_retry
from manager.gcs_lock_registry import GCSLockRegistry
from manager.governance import validate_task_enforcement
from manager.quota_reader import read_drive_status, summarize
from manager.runtime_bridge import all_projects
from manager.task_claims import check_task_execution_claim, task_claim_registry
from manager.tasks import DriveRecords, TaskError, now_iso, validate
from manager.trusted_ingress import (
    ADMISSION_VERSION_V1, REQUIRED_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN, task_policy_satisfied,
    task_policy_satisfied_for_admission, verify_trusted_ingress_admission,
)
from manager.dispatch_requests import dispatch_request_registry


POLL_SECONDS = 60
MAX_POLL_SECONDS = 900
CLAIM_TIMEOUT_SECONDS = 20 * 60
MAX_COMMANDS_PER_POLL = 4

# How much of one scheduled --once tick's PRE_LAUNCH/POLLING phase (listing
# projects and commands, before any command is claimed) is allowed to
# consume, leaving headroom under the 60-second Scheduled Task cadence for
# manager.provenance verify-running plus process startup overhead. This is
# a cooperative budget checked only between projects/commands in
# poll_once() -- it never applies once a command has been claimed and
# process_command() -> launch_task() has actually started; that call always
# runs to its natural completion (minutes to a couple of hours for a real
# provider), governed only by the Scheduled Task's own 125-minute
# ExecutionTimeLimit, same as before this existed. Real HOME evidence: a
# live reproduction of all_projects()+list_records() alone (zero commands
# admitted, every individual Drive call succeeding) took several minutes --
# collectors.publish_drive.build_service()'s per-request timeout bounds any
# single stalled call, but does not bound cumulative volume across many
# projects, which is what this budget addresses instead.
POLL_TIME_BUDGET_SECONDS = 40


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


def _policy_satisfied(task, admission_version=ADMISSION_VERSION_V1):
    return task_policy_satisfied_for_admission(task, admission_version)


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


def provider_quota_reliable(service, provider, account_id=None):
    """Fail-closed: stale/unknown/unreachable quota is never treated as
    "enough to launch", for any provider. Reuses quota_reader.summarize()'s
    own reliability computation unchanged -- no scoring/routing rework, just
    a pre-launch gate on the same has_reliable_quota signal dispatch()
    already computes but does not itself hard-block on. A provider's
    has_reliable_quota is read from that provider's own quota_reader entry
    only; one provider's fresh quota can never satisfy another's gate.
    When account_id is provided, checks that specific account's reliability
    and ensures no quota window is exhausted.
    """
    try:
        quota = summarize(read_drive_status(service=service), max_age_minutes=60)
    except Exception:
        return False
    if account_id is not None:
        entry = next((item for item in quota.get("accounts", []) if item.get("provider") == provider and item.get("account_id") == account_id), None)
        return bool(entry and entry.get("has_usable_quota", entry.get("has_reliable_quota")))
    entry = next((item for item in quota.get("providers", []) if item.get("provider") == provider), None)
    return bool(entry and entry.get("has_usable_quota", entry.get("has_reliable_quota")))


def codex_quota_reliable(service, account_id=None):
    return provider_quota_reliable(service, "codex", account_id=account_id)


def claude_quota_reliable(service, account_id=None):
    return provider_quota_reliable(service, "claude", account_id=account_id)


# provider -> {launcher_factory, quota_check}. An unrecognized provider must
# never fall back to Codex's (or any other provider's) launcher or quota
# gate -- resolve_provider_runtime() returns None for it, which callers treat
# as an unconditional reject.
def ag_availability_check(service):
    """Fail-closed AG availability gate.

    AG has no reliable headless quota source; this gate proves only what is
    actually observable on the local machine: that a resolvable AG CLI binary
    exists and that a verified local Google account identity can be confirmed.
    Returns True only when both are satisfied; False in all other cases,
    including any unexpected exception.  The `service` argument is accepted
    for interface parity with the quota_check contract but is not used --
    AG availability is a local host concern, not a Drive-readable signal.
    """
    try:
        from manager.ag_cli_runner import resolve_ag_cli_executable, verify_auth_identity
        verify_auth_identity()
        resolve_ag_cli_executable()
        return True
    except Exception:
        return False


PROVIDER_RUNTIMES = {
    "codex": {"launcher_factory": CodexLauncher, "quota_check": codex_quota_reliable},
    "claude": {"launcher_factory": ClaudeLauncher, "quota_check": claude_quota_reliable},
    "antigravity": {"launcher_factory": AgRunner, "quota_check": ag_availability_check},
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


def _explicit_account_id(command, task):
    """P0 claude-auth-routing-truth: for a command created via the Direct
    Dispatch trusted ingress (`created_via == TRUSTED_INGRESS_ORIGIN`),
    `command.account_id` is NOT proof of caller intent -- cloud/dispatch_ingress.py
    stamps it with whatever manager.dispatcher.dispatch() automatically
    resolved even when the caller asked for nothing (`account_id` in the
    original request was omitted/null). Only `command.requested_account_id`
    (preserved verbatim from the caller's own payload, null when they left
    account selection to automatic routing) proves a real explicit ask for
    that ingress path. Treating the dispatcher's automatic recommendation as
    "explicit" here would route it straight to launch_task(account_id=...)
    and skip the live auth-ready/quota-freshness re-check automatic
    selection is supposed to get at launch time -- see
    execution_runner.launch_task's account_id=None branch. The same signal
    also applies whenever `requested_account_id` is present on the command
    dict at all (even set to None) -- that key is only ever populated by
    something that understands the provisional-vs-explicit distinction, so
    its presence is as reliable a signal as the origin stamp, and covers
    non-ingress test/production doubles that carry the field without
    stamping `created_via`.

    Every other command origin (no Direct Dispatch ingress stamp and no
    `requested_account_id` key at all -- e.g. a manually/allowlist-created
    Command) predates that distinction; for those, command.account_id itself
    is still the real, hand-set explicit choice, exactly as before this fix
    -- unaffected here.

    Command's own (requested-or-plain) account_id wins over Task's -- Command
    is the concrete per-launch intent (closer to "what should run right
    now"), Task is a standing default. Both are optional/nullable; None
    means "no explicit choice", which is a real, different state from an
    invalid one and must fall through to automatic selection, not be
    rejected."""
    if command.get("created_via") == TRUSTED_INGRESS_ORIGIN or "requested_account_id" in command:
        command_account_id = command.get("requested_account_id")
    else:
        command_account_id = command.get("account_id")
    return command_account_id or task.get("account_id")


def _explicit_account_is_valid(registry, account_id):
    """True only if a local registry is actually configured and the id
    names one of its enabled accounts. No registry configured (None) can
    never validate an explicit id -- there would be no config_dir to
    resolve it to, so this fails closed rather than trusting the bare id."""
    if registry is None:
        return False
    return any(account["account_id"] == account_id and account["enabled"] for account in registry)


def _claude_account_registry():
    """Load the Claude account registry from CLAUDE_ACCOUNTS_CONFIG if set,
    else None -- not []. None means "no registry configured", which keeps
    launch_task() on its pre-P0.1.5 single-account path untouched (today's
    already-logged-in account, no CLAUDE_CONFIG_DIR override); an explicitly
    configured-but-empty registry is a real, different state (nothing
    enabled) and correctly fails closed instead."""
    path = os.environ.get("CLAUDE_ACCOUNTS_CONFIG")
    return load_claude_accounts(path) if path else None


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
                    quota_check=None, ingress_registry_factory=dispatch_request_registry):
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
    admitted_task = None
    # Static-allowlist admission is always evaluated under v1 read-only
    # semantics, never whatever admission_version the Command itself claims
    # -- a static allowlist entry alone must never be able to grant
    # repo-write. Only a command that independently passes
    # verify_trusted_ingress_admission() below gets its own claimed
    # admission_version (and therefore access to the v2-repo-write policy
    # gate).
    admission_version = ADMISSION_VERSION_V1
    if (command["project_id"], command["task_id"]) not in allowlist:
        # Off the static allowlist is not automatically out of scope: a
        # command stamped by the authenticated Direct Dispatch ingress can
        # still be safely auto-admitted under a narrow, explicitly versioned
        # trusted-ingress contract (v1 disposable-read-only, or v2-repo-write
        # bounded-write), evidence cross-checked against the
        # ingress-only-writable idempotency record -- see
        # manager.trusted_ingress. Anything that satisfies neither path is
        # left untouched (no write), same as before.
        admitted_task = verify_trusted_ingress_admission(
            store, command, os.environ.get("ADM_LOCK_GCS_BUCKET"), ingress_registry_factory)
        if admitted_task is None:
            return {"status": "rejected", "reason": "not_allowlisted"}
        admission_version = command.get("admission_version")
    try:
        candidate_task = admitted_task or store.get("tasks", command["project_id"], command["task_id"])
        validate("task", candidate_task)
    except TaskError:
        return _attention(store, command, None, "allowlisted_task_missing_or_invalid")
    try:
        validate_task_enforcement(candidate_task)
    except TaskError:
        return {"status": "rejected", "reason": "mandatory_governance_missing_or_stale"}
    if not _policy_satisfied(candidate_task, admission_version):
        return _attention(store, command, None, "allowlisted_task_policy_not_satisfied")
    if not health_check():
        # Transient/recoverable, not a policy problem: leave queued untouched,
        # no write, so this is retried automatically on the next poll.
        return {"status": "rejected", "reason": "session_center_unavailable"}

    explicit_account_id = _explicit_account_id(command, candidate_task) if command["provider"] == "claude" else None
    claude_accounts = _claude_account_registry()
    if explicit_account_id is not None:
        # Human/system explicit account selection must validate against registry
        # and enforce the fail-closed quota truth gate.
        if not _explicit_account_is_valid(claude_accounts, explicit_account_id):
            return {"status": "rejected", "reason": "unknown_or_disabled_claude_account"}
        quota_gate_fn = quota_check or runtime["quota_check"]
        account_quota_ok = False
        try:
            import inspect
            sig = inspect.signature(quota_gate_fn)
            if "account_id" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                account_quota_ok = quota_gate_fn(service, account_id=explicit_account_id)
            else:
                account_quota_ok = quota_gate_fn(service)
        except TypeError:
            account_quota_ok = quota_gate_fn(service)
        if not account_quota_ok:
            return {"status": "rejected", "reason": "quota_unreliable"}
    elif not (quota_check(service) if quota_check is not None else runtime["quota_check"](service)):
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
                              on_running=lambda _execution: _write(store, running), provider=claimed["provider"],
                              claude_accounts=claude_accounts, account_id=explicit_account_id, **retry)
        terminal = outcome["terminal"]["execution"]
        dispatch = outcome["dispatch"]
        # P0 claude-auth-routing-truth: `running` was captured before
        # launch_task() resolved which Claude account actually launched
        # (explicit_account_id, for a Direct Dispatch command, may only be
        # the dispatcher's provisional automatic recommendation -- see
        # _explicit_account_id). dispatch["account_id"] is that launch's
        # real, resolved identity (None for a non-Claude provider or the
        # single/legacy-account path, unchanged from before); the terminal
        # Command record must reflect it truthfully rather than silently
        # keeping whatever account_id the Command happened to carry before
        # this launch, so a dashboard/audit never sees "account-a" for a
        # launch that actually ran under account-b.
        selected = {**running, "provider": dispatch["provider"], "account_id": dispatch.get("account_id"),
                    "model": dispatch["model"] or claimed["model"],
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
            # No Execution record exists at all (as opposed to one that was
            # reserved/running/already-terminal) means launch_task() raised
            # before provider authority was ever established -- the Task
            # must truthfully report that execution never started, same as
            # _block_prelaunch_task's existing reserved-execution contract
            # (see test_prelaunch_reservation_is_cancelled_and_not_left_running),
            # rather than being silently left "ready"/"Not started" while the
            # Command is terminal "failed".
            no_execution_created = False
            try:
                existing = store.get("executions", claimed["project_id"], claimed["execution_id"])
                if existing.get("status") in ("reserved", "running"):
                    return _reconcile_active(store, service, running, claim_factory)
            except TaskError:
                no_execution_created = True
            # error_kind classification is unchanged from before this fix
            # (bare exception class name, or an explicit .classification
            # attribute when the exception sets one) -- never the exception's
            # own message text, which could carry an absolute filesystem
            # path or other non-sensitive-but-unbounded content. The only
            # new behavior is truthfully blocking the Task when no Execution
            # was ever created for this launch attempt.
            kind = getattr(exc, "classification", None) or type(exc).__name__
            if no_execution_created:
                _block_prelaunch_task(store, claimed, kind)
            final = _terminal(claimed, "failed", _result("error", claimed["execution_id"], error_kind=str(kind)[:100]))
    _write(store, final)
    return {"status": final["status"], "execution_id": claimed["execution_id"]}


def _enumerate_project_ids(store, deadline=None):
    """Cheap project-id enumeration for poll_once()'s pre-launch phase.

    Prefers DriveRecords.list_project_ids() when the store supports it: a
    project folder's own name is already its project_id, so this needs no
    per-project Drive get() at all (unlike all_projects()/list_projects(),
    which fully hydrates every project's JSON document just to find its
    id) -- for N real projects this is O(1) Drive round trips (one
    paginated folder listing) instead of O(N). It also forwards `deadline`
    so pagination itself can stop early rather than running unbounded.

    Falls back to all_projects(store) + extracting project_id for any
    store that doesn't implement list_project_ids (e.g. test doubles),
    reproducing prior behavior for those callers unchanged."""
    if hasattr(store, "list_project_ids"):
        return store.list_project_ids(deadline=deadline)
    return [project["project_id"] for project in all_projects(store)]


# Real production numbers: DRIVE_REQUEST_TIMEOUT_SECONDS (45s, the shared
# service's per-request transport timeout used for every write and every
# active-lifecycle Drive call) is LARGER than POLL_TIME_BUDGET_SECONDS
# (40s). If pre-launch discovery reads used that same 45s bound as their
# own "don't start a request with less than this much budget left" margin,
# the margin alone would exceed the entire budget and discovery could never
# start a single request at all -- a real, provably self-defeating bug
# caught by this commit's own tests before it ever reached a real HOME
# tick. The fix is Option B from the task brief: discovery reads get their
# own, genuinely SHORTER transport timeout, wired through a separate Drive
# service+DriveRecords built only for this read-only phase (see main()) --
# never the 45s service used for writes/active lifecycle. With a 10s
# discovery timeout and a 40s budget, worst case total is budget(40) +
# one worst-case discovery request(10) = 50s, with 10s to spare before the
# next 60s Scheduled Task trigger.
WATCHER_DISCOVERY_TIMEOUT_SECONDS = 10


def _rotated_project_ids(project_ids, now=None):
    """Deterministic, cross-process round-robin rotation of the project
    enumeration order, based purely on wall-clock time -- never a
    process-local counter, since each `--once` invocation is a fresh
    process with no memory of prior ticks.

    Why this exists: once command hydration itself is bounded per-project
    (list_records_bounded below), a single project with a very large
    historical Command backlog can still consume the *entire* remaining
    poll budget on its own hydration, before the outer loop's deadline
    check ever gets to look at the next project. Left unrotated, whichever
    project happens to sort first in Drive's folder listing would be that
    project on every single tick, forever -- permanently starving every
    other project's commands from ever being discovered. Rotating the
    starting point by `int(now // POLL_SECONDS) % len(project_ids)`
    advances "which project goes first" by one approximately every
    scheduled tick, which gives a provable bound: every project is first
    at least once within any len(project_ids)-tick window, so no project
    can be starved forever, without needing any persisted state at all."""
    if not project_ids:
        return project_ids
    now = now if now is not None else time.time()
    offset = int(now // POLL_SECONDS) % len(project_ids)
    return project_ids[offset:] + project_ids[:offset]


def _enumerate_commands(store, project_id, deadline=None):
    """Deadline-aware, bounded-hydration command enumeration for one
    project. Prefers DriveRecords.list_records_bounded() when the store
    supports it -- see that method's docstring for why the unbounded
    list_records() this replaced could itself run for minutes on a project
    with a large historical Command backlog, uninterruptible by any
    project-level deadline check once started. Falls back to
    store.list_records("commands", project_id) for any store that doesn't
    implement the bounded variant (e.g. test doubles), reproducing prior
    behavior for those callers unchanged.

    `store` here is expected to be whatever discovery-purposed store the
    caller passed (see poll_once's `discovery_store` parameter) -- its own
    per-request transport timeout, not this function's business, is what
    actually determines the real worst case for each get_media() call.
    single_request_worst_case is passed through unchanged so callers can
    match it to that store's actual configured timeout."""
    if hasattr(store, "list_records_bounded"):
        return store.list_records_bounded("commands", project_id, deadline=deadline,
                                           single_request_worst_case=WATCHER_DISCOVERY_TIMEOUT_SECONDS)
    return store.list_records("commands", project_id)


def poll_once(store, service, allowlist=None, deadline=None, discovery_store=None, **factories):
    """`deadline`, if given, is a `time.monotonic()` value after which this
    call stops STARTING new project/command work and returns whatever it has
    so far -- any project/command not yet reached this tick is picked up on
    a later poll, with no state lost (nothing here writes anything before a
    command is actually claimed inside process_command). This never
    interrupts a process_command() call already in progress: the deadline is
    only ever checked between iterations, before the next project's
    list_records() or the next command's process_command(), so a real
    provider lifecycle -- once process_command has been called for that
    command -- always runs to its natural completion regardless of how long
    it legitimately takes.

    `discovery_store`, if given, is used ONLY for the read-only
    project/command enumeration below -- `store` (and `service`) remain
    exactly what process_command()/launch_task() use for every claim,
    write, and active-lifecycle Drive call, completely unaffected. This
    matters because a real DriveRecords's configured per-request transport
    timeout is a property of the *service* it was built from, not
    something this function can override per-call -- main() builds
    discovery_store from a separate, genuinely-shorter-timeout Drive
    service (see WATCHER_DISCOVERY_TIMEOUT_SECONDS) specifically so the
    margin checks in list_project_ids()/list_records_bounded() are checking
    against a timeout that is actually smaller than the poll budget, not
    just a smaller number used for bookkeeping while the real transport
    could still block for the shared 45s. Defaults to `store` when omitted
    (every existing caller/test), reproducing prior behavior exactly.

    Project enumeration itself (_enumerate_project_ids above) is also
    deadline-aware and does not hydrate full project documents -- see its
    docstring for why the earlier all_projects()-based version of this loop
    could not be bounded by this same deadline check at all: the full
    listing+hydration ran to completion before the loop's first iteration
    was ever reached. Command enumeration within each project
    (_enumerate_commands) is likewise deadline-aware and never starts a new
    per-record Drive hydration that could itself finish after the deadline
    -- see its docstring and list_records_bounded()'s for the full
    reasoning. Project order is rotated deterministically by wall-clock
    time (_rotated_project_ids) so a project with a very large historical
    Command backlog cannot permanently starve every other project's
    commands from ever being reached."""
    if allowlist is None:
        allowlist = load_allowlist()
    if deadline is None:
        deadline = time.monotonic() + POLL_TIME_BUDGET_SECONDS
    if discovery_store is None:
        discovery_store = store
    results = []
    project_ids = _rotated_project_ids(_enumerate_project_ids(discovery_store, deadline=deadline))
    for project_id in project_ids:
        if time.monotonic() >= deadline:
            break
        try:
            commands = _enumerate_commands(discovery_store, project_id, deadline=deadline)
        except TaskError:
            continue
        for command in commands:
            if command.get("status") in ("completed", "failed"):
                continue
            if len(results) == MAX_COMMANDS_PER_POLL:
                return results
            if time.monotonic() >= deadline:
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
            store = DriveRecords(service)
            # Separate, shorter-timeout service+store used ONLY for
            # poll_once()'s own read-only project/command discovery -- see
            # WATCHER_DISCOVERY_TIMEOUT_SECONDS and poll_once()'s
            # `discovery_store` docstring for why this must be a genuinely
            # different transport, not just a smaller number checked
            # against the same 45s-timeout `service` used for writes and
            # active provider lifecycle below.
            discovery_service = build_service(timeout=WATCHER_DISCOVERY_TIMEOUT_SECONDS)
            discovery_store = DriveRecords(discovery_service)
            ingress = []
            if os.environ.get("ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID"):
                from manager.drive_dispatch_ingress import poll_drive_dispatch_requests
                ingress = poll_drive_dispatch_requests(store, service, os.environ.get("ADM_LOCK_GCS_BUCKET"))
            result = poll_once(store, service, discovery_store=discovery_store)
            print(json.dumps({"status": "ok", "host": socket.gethostname()[:100], "ingress": ingress,
                              "commands": result}, separators=(",", ":")))
        except Exception:
            print(json.dumps({"status": "unavailable"}, separators=(",", ":")))
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    from manager.win_background_guard import install_hidden_subprocess_guard
    install_hidden_subprocess_guard()
    raise SystemExit(main())
