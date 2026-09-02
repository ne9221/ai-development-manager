"""Bounded, Drive-backed command watcher that delegates every launch to execution_runner."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import zlib
from datetime import datetime, timezone

from collectors.publish_drive import build_service
from manager.claude_account_selector import load_claude_accounts
from manager.ag_runner import AgRunner
from manager.claude_launcher import ClaudeLauncher
from manager.codex_launcher import CodexLauncher, process_creation_identity, process_identity_state
from manager.detached_process import popen_detached
from manager.execution_lifecycle import merge_cleanup_evidence, retry_incomplete_terminal_persistence, terminalize_execution
from manager.execution_runner import launch_task
from manager.open_existing_adm_ui import focus_existing_adm_ui
from manager.executions import cancel_reserved_execution, execution_health, prepare_task_retry
from manager.gcs_lock_registry import GCSLockRegistry
from manager.manager_home import ManagerHomeError, resolve_manager_home
from manager.governance import validate_task_enforcement
from manager.quota_reader import read_drive_status, summarize
from manager.runtime_bridge import all_projects
from manager.runtime_supervisor import try_check_and_recover
from manager.task_claims import task_claim_registry, TaskClaimConflict
from manager.task_root import read_task_root_or_legacy_claim, release_runtime_claim
from manager.tasks import DriveRecords, TaskError, now_iso, validate
from manager.trusted_ingress import (
    ADMISSION_VERSION_V1, REQUIRED_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN, task_policy_satisfied,
    task_policy_satisfied_for_admission, verify_trusted_ingress_admission,
)
from manager.worktree_locks import (canonical_repository, reconcile_stopped_provider_terminal_lease,
                                    reconcile_unlinked_terminal_lease, repository_lock_id)
from manager.dispatch_requests import dispatch_request_registry
from manager.production_guard import RuntimeGuardError, require_runtime_guard


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

# Explicit true strings recognized by embedded_ingress_enabled() below;
# everything else (including recognized false strings and malformed input)
# resolves to disabled -- see that function's docstring.
_TRUE_STRINGS = frozenset({"1", "true", "yes"})


def embedded_ingress_enabled(raw=None):
    """Explicit switch gating Command Watcher's own embedded Drive dispatch
    ingress poll (poll_drive_dispatch_requests()), independent of whether
    ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID happens to be set.

    Migration contract (see fix/command-watcher-embedded-ingress-decouple-
    20260823): once the dedicated Drive Dispatch Ingress Scheduled Task
    (manager/run_drive_dispatch_ingress.ps1, frozen at 6d62cea) is the sole
    polling authority for a given install, that install's Command Watcher
    must set ADM_COMMAND_WATCHER_EMBEDDED_INGRESS=0 so it never also calls
    poll_drive_dispatch_requests() in parallel -- a stray leftover folder-id
    env var must not silently re-enable duplicate polling.

    Unset (raw is None, i.e. the env var itself is not present) defaults to
    enabled, reproducing exactly the pre-migration behavior of every
    existing install: embedded ingress runs whenever
    ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID is present, same as before this
    switch existed. This default is itself an explicit, tested choice
    (test_embedded_ingress_enabled_defaults_to_enabled_when_unset), not an
    accidental fallthrough.

    Any recognized false string ("0"/"false"/"no", case-insensitive,
    surrounding whitespace ignored) disables embedded ingress
    unconditionally, regardless of the folder-id env var. Any value that is
    neither a recognized true string nor a recognized false string --
    including empty string, garbage text, or "2" -- fails closed to
    disabled: an ambiguous config must never be interpreted as "keep the
    duplicate-ingress poll alive."
    """
    if raw is None:
        raw = os.environ.get("ADM_COMMAND_WATCHER_EMBEDDED_INGRESS")
    if raw is None:
        return True
    normalized = raw.strip().lower()
    if normalized in _TRUE_STRINGS:
        return True
    return False


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

    Two independent proofs, both required, neither guessed:
    1. the Drive SSOT carries FRESH, official, non-exhausted Antigravity quota
       (collectors/antigravity.py via the same `provider_quota_reliable()`
       rule Codex and Claude already use -- stale/unknown/exhausted rejects);
    2. the Antigravity IDE's language server -- the only execution transport
       the adapter has (manager/ag_language_server.py) -- is running and
       answering on this host right now.
    False on any failure, including unexpected exceptions.
    """
    try:
        if not provider_quota_reliable(service, "antigravity"):
            return False
        from manager.ag_language_server import discover_language_server
        discover_language_server()
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
    """Never let a terminal write downgrade an already-terminal Command's
    canonical result: a stale worker/exception fallback that resolves to a
    generic {status:'error', session_id:None} must not clobber a Command
    that already carries real Execution truth (a specific terminal status
    and a real session_id). Only the specific null-session-over-real-session
    downgrade is guarded -- a first-time terminal write (existing status not
    yet terminal), a write whose existing terminal result ALSO has no real
    session_id to lose, or any write that itself carries a real session_id
    (a genuinely newer/better canonical truth) all proceed unconditionally,
    so legitimate monotonic enrichment (cleanup evidence convergence,
    recovery_reason refinement, timestamp completion) is never blocked."""
    validate("command", command)
    try:
        current = store.get("commands", command["project_id"], command["command_id"])
    except TaskError:
        current = None

    if current and current.get("status") in ("completed", "failed"):
        # Current command in store is already terminal.
        # 1. Non-terminal downgrade (attention, queued, claimed, running) must NEVER overwrite terminal.
        if command.get("status") not in ("completed", "failed"):
            return current
        # 2. Null session_id must never overwrite a real session_id.
        if (command.get("result") or {}).get("session_id") is None and (current.get("result") or {}).get("session_id") is not None:
            return current

    return store.put("commands", command["project_id"], command["command_id"], command)


def _on_execution_running(store, running_command):
    # AUTO_OPEN_ADM moved to the queued -> claimed transition in
    # process_command (see _focus_adm_ui_best_effort there): opening at
    # claim time is strictly earlier user visibility, covers launches that
    # die during prelaunch and never reach this callback at all (live
    # incident 20260901), and firing it in exactly one lifecycle stage
    # guarantees a single focus event per Command -- claim-then-running
    # double focus would re-steal the foreground from whatever the user
    # switched to during prelaunch.
    _write(store, running_command)


def _focus_adm_ui_best_effort(stage):
    """Best-effort, detached, never-raising open/focus of the ADM Dashboard.

    Runs manager.open_existing_adm_ui in a detached helper process instead
    of calling focus_existing_adm_ui() inline: the helper owns the full
    focus-or-launch-or-noop logic AND its cost (dashboard cold start plus
    window discovery can poll for ~30s worst-case), so a watcher tick's
    claim path is never blocked by desktop work and can never overrun the
    scheduled cadence because of it. The helper inherits this Scheduled
    Task's interactive logon session, so Win32 focus works exactly as it
    did inline; with no interactive desktop it fails closed inside the
    helper. Fired from exactly one lifecycle stage (the queued -> claimed
    transition, once per Command), so repeated ticks and later stages never
    re-steal focus, and focus_existing_adm_ui()'s own single-instance logic
    still guarantees no duplicate Dashboard/browser. Any spawn failure is
    only ever logged: user visibility must never affect dispatch or
    terminal truth.
    """
    log_handle = None
    try:
        # Durable observability for a process whose outcome the watcher
        # never waits on (delta-review finding): the helper's stdout (its
        # JSON outcome) and stderr (import failures, tracebacks) append to
        # a log under AI_MANAGER_HOME instead of vanishing into DEVNULL.
        # Log-file trouble degrades to DEVNULL rather than losing the
        # spawn itself -- visibility never outranks the dispatch.
        try:
            # resolve_manager_home() rather than os.getcwd(): the cwd
            # fallback put a logs/ directory inside whatever checkout the
            # process started in. An unresolvable home degrades to DEVNULL
            # like any other log-file trouble -- visibility never outranks
            # the dispatch.
            log_dir = os.path.join(resolve_manager_home(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_handle = open(os.path.join(log_dir, "auto-open-adm.log"), "ab")
            log_handle.write(f"AUTO_OPEN_ADM[{stage}] helper spawned at {now_iso()}\n".encode("utf-8"))
            log_handle.flush()
        except (OSError, ManagerHomeError):
            log_handle = None
        sink = log_handle if log_handle is not None else subprocess.DEVNULL
        # Shared detached + CREATE_BREAKAWAY_FROM_JOB launcher (same contract
        # as _spawn_claimed_worker and _spawn_dashboard): the helper now
        # waits up to ~75s across its port/window stages, longer than a
        # tick may live, so it must not die with the tick's job object.
        popen_detached([sys.executable, "-m", "manager.open_existing_adm_ui"],
                       cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, close_fds=True)
    except Exception as exc:
        print(f"AUTO_OPEN_ADM[{stage}]: helper spawn failed: {exc}", file=sys.stderr)
    finally:
        # The child owns its inherited handle copy; the parent's must close
        # either way (spawn success or failure) or the watcher leaks one
        # per claim.
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass


def _claimed(command):
    # ponytail: deterministic claim content lets competing watchers converge; task claim remains launch authority.
    return {**command, "status": "claimed", "execution_id": execution_id(command),
            "claimed_at": now_iso(), "completed_at": None, "result": None}


def _terminal(command, status, result):
    return {**command, "status": status, "completed_at": now_iso(), "result": result,
            "recovery_reason": command.get("recovery_reason"), "stale_at": command.get("stale_at")}


def _terminal_cleanup_confirmed(execution):
    """Terminal status is publishable only after durable cleanup evidence."""
    evidence = execution.get("cleanup_evidence")
    if (not isinstance(evidence, dict)
            or evidence.get("provider_outcome") != execution.get("status")
            or evidence.get("persistence") != "complete"
            or evidence.get("persisted") != ["execution", "handoff", "task"]
            or evidence.get("task_claim_release") != "released"):
        return False
    writer_release = evidence.get("writer_release")
    return (writer_release in ("released", "not_required")
            if execution.get("access") == "read_only" else writer_release == "released")


_RECOVERABLE_PERSISTENCE_VALUES = ("partial", "incomplete")
_RECOVERABLE_TASK_CLAIM_RELEASE_VALUES = ("retained", "release_pending")


def _terminal_command_needs_recovery(store, command):
    """R17: a Command already marked completed/failed must not be trusted
    on its own -- its linked Execution may still have durable cleanup work
    outstanding (persistence incomplete, or the task claim not yet
    released), exactly the case where the old unconditional fast-path skip
    below left a Task blocked forever with no route back to convergence.
    Narrower than _terminal_cleanup_confirmed (which additionally requires
    provider_outcome/persisted/writer_release to all agree): a harmless
    cleanup_evidence variation with its own dedicated recovery path must
    not pull an otherwise fully-converged Command back into reconciliation
    on every tick.

    P0-B fix: eligibility requires AFFIRMATIVE durable evidence of
    outstanding recovery work -- never inferred from missing/ambiguous
    evidence. Before this fix, `execution.get("cleanup_evidence") or {}`
    turned a genuinely absent/null cleanup_evidence into `{}`, and
    `{}.get("persistence") != "complete"` then evaluated True: "we don't
    know" was silently treated as "definitely still incomplete", which is
    not something this durable record has actually proven. The real R17
    shape (cleanup_evidence.persistence="partial",
    task_claim_release="retained") is still eligible -- it is exactly the
    affirmative case this function exists to catch -- but a Command whose
    Execution has no cleanup_evidence at all, an empty one, or an
    unrecognized/unexpected value for either field is now NOT eligible:
    only a recognized, explicitly-recorded "still incomplete" value on
    EITHER field is accepted (checked against a fixed allowlist of known
    values, never a not-equal-to-known-good check, so an unknown enum
    value fails closed the same way missing evidence does).

    Any exception from the Execution lookup itself (a genuinely missing
    record, a transport/timeout-shaped failure, or anything else) is
    treated as "not eligible this tick", not raised -- see
    _terminal_recovery_candidates()'s own per-candidate fail-closed
    handling, which this defers to for anything beyond a plain missing
    record."""
    execution_id = command.get("execution_id")
    if not execution_id:
        return False
    try:
        execution = store.get("executions", command["project_id"], execution_id)
    except TaskError:
        return False
    if execution.get("status") not in ("completed", "failed", "interrupted"):
        return False
    evidence = execution.get("cleanup_evidence")
    if not isinstance(evidence, dict) or not evidence:
        return False
    if evidence.get("persistence") in _RECOVERABLE_PERSISTENCE_VALUES:
        return True
    if evidence.get("task_claim_release") in _RECOVERABLE_TASK_CLAIM_RELEASE_VALUES:
        return True
    return False


def _attention(store, command, execution, reason):
    # Authority Fence: Check if the authoritative Command in Drive is already terminal.
    try:
        current_cmd = store.get("commands", command["project_id"], command["command_id"])
    except TaskError:
        current_cmd = command

    if current_cmd.get("status") in ("completed", "failed"):
        # The command in Drive is already terminal. Stale reconciliation must not downgrade it.
        return {"status": current_cmd["status"], "execution_id": current_cmd.get("execution_id"), "reconciled": True}

    timestamp = command.get("stale_at") or now_iso()
    # Legacy uncertain executions have no heartbeat/process contract. Surface
    # their Command, but do not rewrite the execution authority record if execution is already terminal.
    if execution and (execution.get("heartbeat_at") or execution.get("provider_evidence")):
        try:
            curr_exec = store.get("executions", command["project_id"], command["execution_id"])
        except TaskError:
            curr_exec = execution
        should_block_task = (
            curr_exec.get("status") not in ("completed", "failed", "interrupted")
            or (curr_exec.get("cleanup_evidence") or {}).get("persistence") != "complete"
        )
        if curr_exec.get("status") not in ("completed", "failed", "interrupted"):
            unchanged_exec = dict(curr_exec)
            curr_exec.update(stale_at=curr_exec.get("stale_at") or timestamp, recovery_reason=reason)
            validate("execution", curr_exec)
            if curr_exec != unchanged_exec:
                store.put("executions", command["project_id"], command["execution_id"], curr_exec)
        if should_block_task:
            try:
                task = store.get("tasks", command["project_id"], command["task_id"])
                if (task.get("source_context") or {}).get("active_execution_id") == command["execution_id"]:
                    unchanged_task = dict(task)
                    task.update(status="blocked", updated_at=timestamp,
                                blocked_reason=f"Execution recovery required: {reason}",
                                current_progress="Execution requires attention",
                                next_action="Verify provider/process and authority evidence; do not start a duplicate")
                    validate("task", task)
                    if task != unchanged_task:
                        store.put("tasks", command["project_id"], command["task_id"], task)
            except TaskError:
                pass
    marked = {**command, "status": "attention", "stale_at": timestamp,
              "recovery_reason": reason, "completed_at": None, "result": None}
    # No-op suppression (live 20260902 starvation): an attention Command
    # whose stored record is ALREADY byte-identical to what would be written
    # is not rewritten. Before this, every tick rewrote it unchanged, which
    # refreshed its Drive modifiedTime, which kept it permanently at rank 0
    # of the `modifiedTime desc` recent sweep (RECENT_COMMANDS_PER_PROJECT=2)
    # -- a self-perpetuating monopoly that starved the rank-1 record for
    # 60+ minutes of healthy ticks. Any real change (new reason, new
    # stale_at, any field) still persists exactly as before; only the
    # authoritative store's own current record (never the caller's possibly
    # stale copy) is trusted as "already identical".
    if current_cmd is not command and current_cmd == marked:
        return {"status": "attention", "execution_id": command.get("execution_id"),
                "recovery_reason": reason, "unchanged": True}
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


def _reconcile_terminal_writer_lease(store, command, execution):
    """Reconcile a prelaunch terminal rollback's unlinked writer lease."""
    task = store.get("tasks", command["project_id"], command["task_id"])
    validate("task", task)
    if task.get("read_only") is True:
        return {"status": "clean", "released": False, "reason": "read_only_no_writer_lease"}
    project = store.get("projects", command["project_id"], command["project_id"])
    validate("project", project)
    lock_id = repository_lock_id(canonical_repository(project["repo"]))
    # foreign_owner_clean: the registry keeps one lock slot per repository,
    # so a slot owned by ANY other task/execution proves this prelaunch
    # rollback holds no lease there -- nothing of ours to release. Before
    # this, a foreign owner (even one already "released" -- live incident
    # 20260901: the previous task's released lock) raised
    # "owner mismatch" -> attention on EVERY tick, and the Command could
    # never terminalize (terminal_writer_authority_reconciliation_unknown
    # forever). Matching-owner refusals (linked session, invalid status)
    # are unchanged.
    return reconcile_unlinked_terminal_lease(
        GCSLockRegistry.from_environment(), lock_id,
        command["project_id"], command["task_id"], execution["execution_id"], execution["provider"],
        execution["status"], foreign_owner_clean=True,
    )


def _release_orphan_pre_execution_claim(store, command, claim_registry):
    """Autonomous recovery for a GCS task claim whose owning worker crashed
    before it could write its Execution record.

    Safety fences (all must be satisfied before any CAS delete is attempted):
      1. CLAIM_TIMEOUT_SECONDS already elapsed (caller's responsibility --
         checked by _claim_expired before this is ever called).
      2. No Execution record exists for the claimed execution_id (caller's
         responsibility -- checked by _reconcile_active's TaskError path).
      3. The current GCS claim still names exactly the same execution_id
         this Command carries -- a newer owner having replaced the claim
         (claim["execution_id"] != command["execution_id"]) is NOT an orphan
         from this command's point of view; leave it alone.
      4. No Session record exists for the task -- any Session is evidence
         the worker advanced past reserve_execution() (possibly just before
         crashing with the Execution write pending), so the state is
         ambiguous and unsafe to auto-release.
      5. CAS delete (delete_if_generation_matches) -- generation-locked,
         ABA-safe, idempotent across concurrent reconcilers: if two watcher
         ticks race here, exactly one will win the GCS precondition; the
         loser sees TaskClaimConflict and falls back to attention (correct
         -- the winner already requeued the Command).

    On success: CAS-releases the GCS claim, resets the Command back to
    `queued` (execution_id=None, claimed_at=None) with recovery provenance
    so the next natural watcher tick can re-admit and launch it.
    Returns a process_command-compatible outcome dict.
    """
    try:
        existing = read_task_root_or_legacy_claim(claim_registry, command["project_id"], command["task_id"])
    except TaskError:
        return _attention(store, command, None, "execution_record_missing_claim_state_unknown")
    if existing is None:
        # Claim is already gone (earlier tick or concurrent reconciler released it).
        try:
            current = store.get("commands", command["project_id"], command["command_id"])
            if current.get("status") in ("queued", "completed", "failed"):
                return {"status": current["status"], "reconciled": True}
        except TaskError:
            pass
        failed = _terminal(command, "failed", _result("error", command["execution_id"], error_kind="claim_timeout"))
        _write(store, failed)
        return {"status": "failed", "reconciled": True}
    # Fence 3: execution_id must match exactly.
    if existing.get("execution_id") != command.get("execution_id"):
        # A newer execution already claimed the slot; our command is stale.
        return _attention(store, command, None, "execution_record_missing_claim_replaced_by_newer")
    # Fence 4: absence of any Session for this task/project is required.
    # A Session record is evidence the worker progressed past reserve_execution();
    # do not release a potentially-active claim in that case.
    try:
        sessions = store.list_records("sessions", command["project_id"])
        task_sessions = [s for s in sessions if s.get("task_id") == command["task_id"]]
    except (TaskError, Exception):  # noqa: BLE001 -- session check is best-effort; ambiguity → refuse
        task_sessions = [object()]  # non-empty → refuse
    if task_sessions:
        return _attention(store, command, None, "execution_record_missing_session_evidence_present")
    # Fence 5: Worker liveness check.
    # Worker identity (PID + creation identity) must be known. If unknown (e.g. legacy
    # record), fail closed to avoid releasing a claim for a running unmonitored worker.
    worker_pid = command.get("worker_pid")
    worker_creation = command.get("worker_creation_identity")
    if worker_pid is None:
        return _attention(store, command, None, "execution_record_missing_claim_retained_worker_liveness_unknown")
    worker_state = process_identity_state(worker_pid, worker_creation)
    if worker_state == "live":
        return _attention(store, command, None, "execution_record_missing_worker_process_live")
    if worker_state == "unknown":
        return _attention(store, command, None, "execution_record_missing_worker_state_unknown")
    if worker_state not in ("stopped", "replaced"):
        return _attention(store, command, None, f"execution_record_missing_worker_{worker_state}")
    # Fence 6: CAS delete — generation-locked, safe for concurrent reconcilers.
    requeued = {
        **command,
        "status": "queued",
        "execution_id": None,
        "claimed_at": None,
        "completed_at": None,
        "result": None,
        "worker_pid": None,
        "worker_creation_identity": None,
        "worker_spawned_at": None,
        "stale_at": command.get("stale_at") or now_iso(),
        "recovery_reason": "orphaned_pre_execution_claim_released",
    }
    validate("command", requeued)
    _write(store, requeued)
    try:
        released = release_runtime_claim(
            claim_registry, command["project_id"], command["task_id"],
            existing["execution_id"], existing["generation"],
        )
    except TaskClaimConflict:
        return {"status": "queued", "reconciled": True, "orphan_claim_released": True}
    except TaskError:
        return _attention(store, command, None, "execution_record_missing_claim_state_unknown")
    return {"status": "queued", "reconciled": True, "orphan_claim_released": True}


def _reconcile_active(store, service, command, claim_factory, launch_failure_observed=False):
    """Reconcile a claimed/running/attention Command against its Execution.

    ``launch_failure_observed`` is set only by the launch path's own
    exception handlers (_run_claimed_command/process_command), which have
    first-hand proof that launch_task() already raised for this exact
    claimed command -- for them a still-reserved Execution really is a
    prelaunch failure and is cancelled immediately, exactly as before. A
    watcher tick observing from outside has no such proof: a reservation
    whose Command claim is still fresh is the NORMAL in-flight prelaunch
    shape (reserve_execution() -> enter_running_gate() spans real Drive/GCS
    latency, minutes on a slow tick -- live incident 20260901: a reservation
    24 seconds old, worker PID alive, was cancelled as 'prelaunch failure'
    by the next tick), so it is left alone until the claim itself expires.
    """
    try:
        execution = store.get("executions", command["project_id"], command["execution_id"])
        validate("execution", execution)
    except TaskError:
        if command["status"] == "claimed" and not _claim_expired(command):
            return {"status": "claimed", "skipped": True}
        if command["status"] == "claimed" and _claim_expired(command):
            try:
                claim_registry = _claim_registry(command, claim_factory)
            except TaskError:
                return _attention(store, command, None, "execution_record_missing_claim_state_unknown")
            return _release_orphan_pre_execution_claim(store, command, claim_registry)
        return _attention(store, command, None, "execution_record_missing_or_invalid")

    try:
        claim_registry = _claim_registry(command, claim_factory)
    except TaskError:
        return _attention(store, command, execution, "task_claim_backend_unavailable")
    if execution["status"] in ("completed", "failed", "interrupted"):
        evidence = execution.get("cleanup_evidence") or {}
        if evidence.get("persistence") != "complete":
            drive_file_id_factory = getattr(store, "generate_record_file_id", None)
            if retry_incomplete_terminal_persistence(store, command["project_id"], command["task_id"], command["execution_id"],
                                                     claim_registry=claim_registry, drive_file_id_factory=drive_file_id_factory):
                execution = store.get("executions", command["project_id"], command["execution_id"])
                evidence = execution.get("cleanup_evidence") or {}
        try:
            claim = read_task_root_or_legacy_claim(claim_registry, command["project_id"], command["task_id"])
            if claim is not None:
                from manager.execution_recovery import recover_task_claim
                recovered = recover_task_claim(store, claim_registry, command["project_id"], command["task_id"])
                if recovered.get("status") not in ("clean", "released"):
                    return _attention(store, command, execution, "terminal_cleanup_not_confirmed")
                if recovered.get("status") == "released":
                    # recover_task_claim() only ever touches the raw GCS
                    # claim -- it never syncs the Execution record's own
                    # cleanup_evidence.task_claim_release field, so without
                    # this, _terminal_cleanup_confirmed() below would keep
                    # seeing 'retained' forever even though the real claim
                    # is now genuinely gone.
                    refreshed = store.get("executions", command["project_id"], command["execution_id"])
                    refreshed["cleanup_evidence"] = merge_cleanup_evidence(refreshed.get("cleanup_evidence"), {"task_claim_release": "released"})
                    validate("execution", refreshed)
                    store.put("executions", command["project_id"], command["execution_id"], refreshed)
                    execution = refreshed
            else:
                # Claim is explicitly ABSENT from authoritative registry read.
                # If execution is terminal, cleanup persistence is complete,
                # writer lease (if write access) is released, provider process is dead,
                # and no newer execution owns the task, converge cleanup_evidence.task_claim_release to "released".
                if (evidence.get("persistence") == "complete"
                        and evidence.get("persisted") == ["execution", "handoff", "task"]
                        and evidence.get("provider_outcome") == execution.get("status")):
                    if execution.get("access") != "production_write" or evidence.get("writer_release") == "released":
                        provider_ev = execution.get("provider_evidence") or {}
                        pid = provider_ev.get("pid")
                        creation = provider_ev.get("creation_identity")
                        if pid is not None and process_identity_state(pid, creation) == "live":
                            return _attention(store, command, execution, "terminal_cleanup_provider_still_live")
                        try:
                            task = store.get("tasks", command["project_id"], command["task_id"])
                            active_exec = (task.get("source_context") or {}).get("active_execution_id")
                            if active_exec and active_exec != execution["execution_id"]:
                                return _attention(store, command, execution, "terminal_cleanup_task_reclaimed_by_newer_execution")
                        except TaskError:
                            pass
                        if evidence.get("task_claim_release") != "released":
                            refreshed = store.get("executions", command["project_id"], command["execution_id"])
                            refreshed["cleanup_evidence"] = merge_cleanup_evidence(refreshed.get("cleanup_evidence"), {"task_claim_release": "released"})
                            validate("execution", refreshed)
                            store.put("executions", command["project_id"], command["execution_id"], refreshed)
                            execution = refreshed
        except TaskError:
            return _attention(store, command, execution, "terminal_cleanup_reconciliation_unknown")
        terminal = _existing_terminal(store, command)
        if terminal:
            _write(store, terminal)
            return {"status": terminal["status"], "reconciled": True}
        return _attention(store, command, execution, "terminal_cleanup_not_confirmed")
    if execution["status"] == "reserved":
        # In-flight prelaunch protection: without first-hand launch-failure
        # proof, a reservation under a still-fresh Command claim belongs to
        # a live worker somewhere between reserve_execution() and the
        # running gate's own "running" write. Cancelling it here races that
        # worker and orphans the launch. The claim window
        # (CLAIM_TIMEOUT_SECONDS) is the designed bound on the whole
        # prelaunch: once it expires with the Execution still reserved, the
        # cancel below runs and the Command converges to a terminal
        # "failed" -- never an unbounded wait.
        if not launch_failure_observed:
            if not _claim_expired(command):
                return {"status": command["status"], "skipped": True, "reason": "prelaunch_in_flight"}
            # Claim expiry alone is a schedule bound, not proof the worker is
            # gone: a live recorded worker delayed past CLAIM_TIMEOUT_SECONDS
            # (degraded Drive/GCS mid-prelaunch) must still never have its
            # reservation cancelled underneath it -- the same liveness fence
            # _release_orphan_pre_execution_claim already applies to the
            # missing-execution shape. Only a worker proven stopped/replaced
            # (or a legacy record that never carried worker identity at all,
            # where the pre-existing cancel is the only route to
            # convergence) may fall through to the cancel below; live or
            # unknown state surfaces as attention and reconverges on a later
            # tick once the process is provably gone.
            observed_worker_pid = command.get("worker_pid")
            if observed_worker_pid is not None:
                worker_state = process_identity_state(observed_worker_pid, command.get("worker_creation_identity"))
                if worker_state == "live":
                    return _attention(store, command, execution, "reserved_execution_worker_still_live")
                if worker_state not in ("stopped", "replaced"):
                    return _attention(store, command, execution, "reserved_execution_worker_state_unknown")
        try:
            cancelled = cancel_reserved_execution(
                store, claim_registry, command["project_id"], command["execution_id"],
                "prelaunch failure left a reservation without provider authority",
            )
        except TaskError:
            return _attention(store, command, execution, "reserved_execution_authority_inconsistent")
        try:
            _reconcile_terminal_writer_lease(store, command, cancelled)
        except TaskError:
            return _attention(store, command, execution, "terminal_writer_authority_reconciliation_unknown")
        _block_prelaunch_task(store, command, "prelaunch_contract_or_gate_failure")
        failed = _terminal(command, "failed", _result("error", cancelled["execution_id"], error_kind="prelaunch_failed"))
        failed["recovery_reason"] = "prelaunch_contract_or_gate_failure"
        _write(store, failed)
        return {"status": "failed", "reconciled": True}
    if execution["status"] == "cancelled":
        try:
            _reconcile_terminal_writer_lease(store, command, execution)
        except TaskError:
            return _attention(store, command, execution, "terminal_writer_authority_reconciliation_unknown")
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
        claim = read_task_root_or_legacy_claim(claim_registry, command["project_id"], command["task_id"])
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
    if provider == "stopped" and exact_claim and execution.get("access") == "production_write":
        try:
            lease = execution.get("lease_evidence") or {}
            reconcile_stopped_provider_terminal_lease(
                GCSLockRegistry.from_environment(), lease.get("lock_id"), command["project_id"],
                command["task_id"], execution["execution_id"], execution["provider"],
                lease.get("generation"), execution.get("session_id"), True,
            )
            terminalize_execution(
                store, service, GCSLockRegistry.from_environment(), claim_registry,
                command["project_id"], command["task_id"], execution["execution_id"],
                execution["provider"], "interrupted", claim["generation"], True,
                summary=f"Recovery: {health['reason']}; provider stop and released writer generation proven",
                writer_authority_released=True,
            )
            terminal = _existing_terminal(store, command)
            if terminal:
                _write(store, terminal)
                return {"status": terminal["status"], "reconciled": True, "provider_state": provider}
        except TaskError:
            pass
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
        # Parenthesise the `or` operands explicitly: without parens Python
        # binds `-` tighter than `or`, so the original
        # `now or datetime.now(timezone.utc) - claimed` would be parsed as
        # `now or (datetime.now(timezone.utc) - claimed)` -- fine when
        # now=None (evaluates the timedelta sub-expression), but when now IS
        # a datetime the expression short-circuits to the datetime itself and
        # `.total_seconds()` raises AttributeError (datetime has no such
        # method), caught below and returning True (wrong: every injected-now
        # claim appears expired). Fix: force the operands of `or` to be the
        # two datetime values, so the subtraction always produces a timedelta.
        return ((now or datetime.now(timezone.utc)) - claimed).total_seconds() > CLAIM_TIMEOUT_SECONDS
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
    if not _terminal_cleanup_confirmed(execution):
        return None
    try:
        task = store.get("tasks", command["project_id"], command["task_id"])
        validate("task", task)
    except TaskError:
        return None
    expected_task_status = "completed" if execution["status"] == "completed" else "blocked"
    if (task.get("status") != expected_task_status
            or (task.get("source_context") or {}).get("active_execution_id") != command["execution_id"]):
        return None
    # Monotonic confirmation, not a fresh derivation: this call site only
    # ever CONFIRMS durable cleanup for a Command that may already be
    # terminal (see process_command's completed/failed reconciliation gate
    # above) -- it never re-classifies a real outcome. Preserve the
    # Command's own already-recorded completed_at/error_kind rather than
    # restamping now_iso()/None, or every reconciliation pass on an
    # already-terminal Command would silently drift its timestamp and wipe
    # its error classification.
    existing_error_kind = (command.get("result") or {}).get("error_kind")
    reconciled = _terminal(command, "completed" if execution["status"] == "completed" else "failed",
                           _result(execution["status"], command["execution_id"], execution.get("session_id"),
                                   error_kind=existing_error_kind))
    if command.get("completed_at"):
        reconciled["completed_at"] = command["completed_at"]
    return reconciled


def _run_claimed_command(store, service, claimed, launcher_factory, writer_factory,
                         claim_factory, explicit_account_id, claude_accounts, origin,
                         retry_count=0, retry_of=None):
    """Run the existing claimed -> Execution -> Session lifecycle."""
    try:
        task = store.get("tasks", claimed["project_id"], claimed["task_id"])
        validate("task", task)
        claim_registry = claim_factory(os.environ.get("ADM_LOCK_GCS_BUCKET"), claimed["project_id"], claimed["task_id"])
        writer_registry = None if task.get("read_only") else writer_factory()
        running = {**claimed, "status": "running"}
        retry = ({"retry_count": retry_count, "retry_of_execution_id": retry_of} if retry_count else {})
        outcome = launch_task(store, service, writer_registry, claim_registry, launcher_factory(),
                              claimed["project_id"], claimed["task_id"], claimed["execution_id"], claimed["model"],
                              on_running=lambda _execution: _on_execution_running(store, running),
                              provider=claimed["provider"],
                              claude_accounts=claude_accounts, account_id=explicit_account_id, provenance=origin, **retry)
        terminal = outcome["terminal"]["execution"]
        dispatch = outcome["dispatch"]
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
            no_execution_created = False
            try:
                existing = store.get("executions", claimed["project_id"], claimed["execution_id"])
                if existing.get("status") in ("reserved", "running"):
                    return _reconcile_active(store, service, {**claimed, "status": "running"}, claim_factory,
                                             launch_failure_observed=True)
                if existing.get("status") in ("completed", "failed", "interrupted"):
                    # Execution genuinely reached terminal status (real
                    # session, real provider outcome) but _existing_terminal
                    # refused it above -- cleanup_evidence isn't fully
                    # confirmed yet (e.g. a transient task-persistence
                    # readback glitch, see retry_incomplete_terminal_persistence).
                    # The caller's own exception (a worker teardown error, a
                    # provider-stopped TaskError) is strictly LESS
                    # authoritative than this already-terminal Execution
                    # record -- deriving the Command's result from it
                    # instead of the generic error/TaskError fallback below
                    # is what keeps real session_id/status from degrading to
                    # a generic null-session snapshot.
                    terminal_status = "completed" if existing["status"] == "completed" else "failed"
                    selected = {**claimed}
                    if existing.get("account_id"):
                        selected["account_id"] = existing["account_id"]
                    if existing.get("provider"):
                        selected["provider"] = existing["provider"]
                    final = _terminal(selected, terminal_status,
                                      _result(existing["status"], claimed["execution_id"], existing.get("session_id")))
                    _write(store, final)
                    return {"status": final["status"], "execution_id": claimed["execution_id"]}
            except TaskError:
                no_execution_created = True
            kind = getattr(exc, "classification", None) or type(exc).__name__
            if no_execution_created:
                _block_prelaunch_task(store, claimed, kind)
            final = _terminal(claimed, "failed", _result("error", claimed["execution_id"], error_kind=str(kind)[:100]))
    _write(store, final)
    return {"status": final["status"], "execution_id": claimed["execution_id"]}


def _spawn_claimed_worker(claimed):
    """Continue provider work outside the one-minute watcher invocation.

    The detached + CREATE_BREAKAWAY_FROM_JOB flag contract (and its OSError
    fallback) this function proved in production now lives once, in
    manager.detached_process.popen_detached, shared with the AUTO_OPEN_ADM
    helper and the Dashboard launcher so the three can never drift apart.
    """
    process = popen_detached(
        [sys.executable, "-m", "manager.command_watcher_worker",
         claimed["project_id"], claimed["task_id"], claimed["execution_id"]],
        cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
    )
    return process.pid


def queued_command_pending_only_health(store, command, allowlist=frozenset(), bucket=None,
                                       ingress_registry_factory=dispatch_request_registry):
    """Read-only replay of process_command's own admission gate for a still-
    `queued` Command, covering everything process_command checks up to (but
    never including) its Session Center health_check() gate.

    This exists only so manager.session_center_supervisor can tell whether a
    queued Command is real enough -- same governance, same trusted-ingress
    admission, same task-policy gate process_command itself enforces -- to
    justify starting Session Center for it before it has ever been claimed.
    process_command remains the sole authority that actually claims a
    Command: this never writes, never claims, and grants no authority
    process_command doesn't independently re-derive on its own next tick --
    it can only ever be more conservative than process_command, never less,
    since any error here fails closed to False (not eligible).
    """
    if command.get("status") != "queued":
        return False
    try:
        validate("command", command)
    except TaskError:
        return False
    if command.get("action") == "OPEN_EXISTING_ADM_UI":
        return False
    if resolve_provider_runtime(command.get("provider")) is None:
        return False
    admitted_task = None
    admission_version = ADMISSION_VERSION_V1
    if (command.get("project_id"), command.get("task_id")) not in allowlist:
        admitted_task = verify_trusted_ingress_admission(store, command, bucket, ingress_registry_factory)
        if admitted_task is None:
            return False
        admission_version = command.get("admission_version")
    try:
        candidate_task = admitted_task or store.get("tasks", command["project_id"], command["task_id"])
        validate("task", candidate_task)
    except (TaskError, AttributeError, KeyError):
        return False
    try:
        validate_task_enforcement(candidate_task)
    except TaskError:
        return False
    return _policy_satisfied(candidate_task, admission_version)


def process_command(store, service, command, launcher_factory=None, writer_factory=GCSLockRegistry.from_environment,
                    claim_factory=task_claim_registry, allowlist=frozenset(), health_check=session_center_healthy,
                    quota_check=None, ingress_registry_factory=dispatch_request_registry, origin_context=None,
                    async_launch=False, terminal_recovery_prevalidation=None):
    """Claim/reconcile one command; a claimed command is never automatically relaunched.

    launcher_factory/quota_check are explicit-override escape hatches (tests
    use them directly); when not given, both resolve from PROVIDER_RUNTIMES
    by the command's own provider. An unrecognized provider is rejected here
    -- it never silently falls back to Codex's launcher or quota gate.

    `terminal_recovery_prevalidation`, if given, must be a
    _TerminalRecoveryPrevalidation from _terminal_recovery_candidates()'s
    own bounded, short-timeout classification lookup for THIS exact
    command this tick (see poll_once()'s Phase 2c) -- blocker-1 fix
    (third residual-P0-A review): reusing it here means the terminal
    eligibility check happens exactly once per attempt, never twice (the
    prior round's classification_store lookup plus a second, unbudgeted
    lookup against the long-timeout primary `store` right here). Every
    identity field is still re-validated against the CURRENT command
    (_prevalidation_matches) before being trusted at all; any mismatch, or
    any caller passing anything other than a genuine
    _TerminalRecoveryPrevalidation, falls straight back to the normal,
    full eligibility check below -- there is no bare boolean/truthy bypass
    of the eligibility gate."""
    require_runtime_guard()
    try:
        validate("command", command)
    except TaskError:
        return {"status": "rejected"}
    if command["status"] in ("completed", "failed"):
        if _prevalidation_matches(terminal_recovery_prevalidation, command) or _terminal_command_needs_recovery(store, command):
            return _reconcile_active(store, service, command, claim_factory)
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
    if command.get("action") == "OPEN_EXISTING_ADM_UI":
        claimed = _claimed(command)
        _write(store, claimed)
        result = focus_existing_adm_ui()
        final = _terminal(claimed, result["status"], _result(
            result["status"], claimed["execution_id"], error_kind=result.get("error_kind")))
        _write(store, final)
        return {"status": final["status"], "execution_id": claimed["execution_id"], "error_kind": result.get("error_kind")}
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
    from manager.scheduler_provenance import command_origin
    origin = command_origin(origin_context)
    claimed = {**_claimed(command), "process_provenance": origin}
    _write(store, claimed)
    # AUTO_OPEN_ADM at claim time, for both the async and inline launch
    # paths: the user should see the Dashboard as soon as a real dispatch
    # enters execution (queued -> claimed), not only after prelaunch
    # (minutes of Drive/GCS latency) finally reaches "running" -- and a
    # launch that dies during prelaunch never reaches on_running at all
    # (live incident 20260901: no window ever appeared). This is the ONLY
    # lifecycle stage that fires it, and this transition happens exactly
    # once per Command; subsequent ticks reconcile and never re-enter it.
    _focus_adm_ui_best_effort("claimed")
    if async_launch:
        try:
            worker_pid = _spawn_claimed_worker(claimed)
            worker_creation = process_creation_identity(worker_pid) if worker_pid else None
            worker_spawned_at = now_iso()
            claimed_with_worker = {
                **claimed,
                "worker_pid": worker_pid,
                "worker_creation_identity": worker_creation,
                "worker_spawned_at": worker_spawned_at,
            }
            validate("command", claimed_with_worker)
            _write(store, claimed_with_worker)
            return {"status": "claimed", "execution_id": claimed["execution_id"], "worker_pid": worker_pid}
        except Exception as exc:
            kind = getattr(exc, "classification", None) or type(exc).__name__
            _block_prelaunch_task(store, claimed, kind)
            final = _terminal(claimed, "failed", _result("error", claimed["execution_id"], error_kind=str(kind)[:100]))
            final["recovery_reason"] = "provider_worker_spawn_failed"
            _write(store, final)
            return {"status": final["status"], "execution_id": claimed["execution_id"]}
    try:
        task = store.get("tasks", claimed["project_id"], claimed["task_id"])
        validate("task", task)
        claim_registry = claim_factory(os.environ.get("ADM_LOCK_GCS_BUCKET"), claimed["project_id"], claimed["task_id"])
        writer_registry = None if task.get("read_only") else writer_factory()
        running = {**claimed, "status": "running"}
        retry = ({"retry_count": retry_count, "retry_of_execution_id": retry_of} if retry_count else {})
        outcome = launch_task(store, service, writer_registry, claim_registry, launcher_factory(),
                              claimed["project_id"], claimed["task_id"], claimed["execution_id"], claimed["model"],
                              on_running=lambda _execution: _on_execution_running(store, running),
                              provider=claimed["provider"],
                              claude_accounts=claude_accounts, account_id=explicit_account_id, provenance=origin, **retry)
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
                    return _reconcile_active(store, service, running, claim_factory,
                                             launch_failure_observed=True)
                if existing.get("status") in ("completed", "failed", "interrupted"):
                    # Same terminal-truth-over-generic-fallback derivation as
                    # _run_claimed_command's async path above -- see its
                    # comment for the full rationale.
                    terminal_status = "completed" if existing["status"] == "completed" else "failed"
                    selected = {**claimed}
                    if existing.get("account_id"):
                        selected["account_id"] = existing["account_id"]
                    if existing.get("provider"):
                        selected["provider"] = existing["provider"]
                    final = _terminal(selected, terminal_status,
                                      _result(existing["status"], claimed["execution_id"], existing.get("session_id")))
                    _write(store, final)
                    return {"status": final["status"], "execution_id": claimed["execution_id"]}
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


def _within_project_record_rotation_offset(now=None, stride=1, project_id=None):
    """Deterministic, cross-process, wall-clock-only rotation offset for
    ONE project's own historical Command or Task listing (see
    DriveRecords.list_records_bounded's `rotate_offset` parameter).

    Why this exists: `_rotated_project_ids` above already prevents one
    project with a huge backlog from starving every OTHER project's
    discovery forever, by rotating which project goes first each tick. It
    does nothing for a record stuck *inside* that one large project's own
    backlog: bounded hydration walks Drive's own listing order and simply
    stops once its per-tick budget runs out -- so whichever records happen
    to land after that cutoff point would be unreachable forever without
    rotation. A live HOME canary (a queued repo-write Command for a
    project with a large, self-inflicted historical Command backlog from
    repeated acceptance runs) reproduced this: still unclaimed after 50+
    minutes of continuous natural ticks, confirmed via a direct Drive read
    showing status="queued", claimed_at=null the entire time.

    `stride`, when > 1 (e.g. matching the bounded scan window K for
    waiting_quota tasks -- see WAITING_QUOTA_DISCOVERY_WINDOW), advances
    the starting offset by K positions per tick. Because intervals of size
    K placed at offsets t*K are contiguous on the unwrapped circle, every
    index in [0, N-1] is guaranteed to be visited in at most ceil(N / K)
    ticks for any arbitrary integer N >= 1, with zero starvation even when
    gcd(N, K) > 1, while strictly preserving the single-tick Drive API
    bound. Default stride=1 preserves the original one-position-per-tick
    behavior for every existing caller (e.g. _enumerate_commands).

    This returns a non-negative integer (never reduced modulo a record
    count the caller doesn't know yet -- `list_records_bounded` itself takes
    `% len(items)`). Without `project_id` it is the raw tick counter times
    `stride`, advancing approximately every POLL_SECONDS; with `project_id`
    it is a per-(tick, project) stable hash instead -- see the inline
    comment below for the aliasing defect that made the raw counter unsafe
    for the multi-project full-sweep path. Either way it is purely a
    function of wall-clock time (and the project's own id), never a
    process-local counter, since every `--once` invocation is a fresh
    process with no memory of prior ticks."""
    now = now if now is not None else time.time()
    tick = int(now // POLL_SECONDS)
    if project_id is not None:
        # P1 delta fix (independent adversarial review of 95768752,
        # finding 2): a purely arithmetic offset progression aliases
        # against multi-project rotation. When K projects rotate the
        # front hydration slot, one given project is only actually
        # ENUMERATED every K-th tick, so the offsets it ever sees form
        # the arithmetic progression {0, K, 2K, ...} (mod N) -- and for
        # any N sharing a factor with K, whole residue classes of that
        # project's records land permanently outside every reachable
        # hydration window, no matter how many ticks pass. (Concrete
        # failing schedule from the review: 3 large-history projects, a
        # per-visit window of 2 adjacent records, N divisible by 3 --
        # offsets 2, 5, 8, ... are never returned, ever.)
        #
        # Fix: decorrelate the offset per (tick, project) with a stable
        # hash instead of the raw tick counter. crc32 is deterministic
        # across processes and platforms (same reason the raw wall-clock
        # tick was chosen originally: every --once invocation is a fresh
        # process), so concurrent/replayed invocations inside one tick
        # still agree on the same offset -- but consecutive VISITS to the
        # same project now start at effectively independent positions, so
        # no revisit period can ever alias into a fixed unreachable
        # residue class again: every record's per-visit reach probability
        # is at least window/N, making permanent exclusion impossible
        # rather than merely less likely. Callers using `stride` (the
        # waiting_quota K-window sweep) pass no project_id and keep the
        # exact original contiguous-interval guarantee documented above.
        return zlib.crc32(("%d:%s" % (tick, project_id)).encode("utf-8"))
    return tick * stride


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
    match it to that store's actual configured timeout.

    Passes a wall-clock-derived `rotate_offset` (see
    `_within_project_record_rotation_offset`'s own docstring for the real
    production trace this closes) so a project whose Command backlog
    exceeds one tick's bounded hydration budget cannot permanently strand
    a specific record past every tick's cutoff point -- unlike the
    recent-command sweep below, this is the ONLY enumeration path with no
    `order_by` of its own, so it is the one that needed this."""
    if hasattr(store, "list_records_bounded"):
        return store.list_records_bounded("commands", project_id, deadline=deadline,
                                           single_request_worst_case=WATCHER_DISCOVERY_TIMEOUT_SECONDS,
                                           rotate_offset=_within_project_record_rotation_offset(project_id=project_id))
    return store.list_records("commands", project_id)


RECENT_COMMANDS_PER_PROJECT = 2
RECENT_COMMAND_SWEEP_BUDGET_SECONDS = 25  # Phase 2's guaranteed 40s - 15s floor.
RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS = 3


def _enumerate_recent_commands(store, project_id, deadline=None):
    """Hydrate only the newest command records for the fast claim sweep.

    The regular full bounded sweep still runs afterwards for stale recovery.
    This first pass prevents terminal history in earlier projects from using
    the whole poll budget before a newly queued command is even inspected.
    """
    if hasattr(store, "list_records_bounded"):
        try:
            return store.list_records_bounded(
                "commands", project_id, deadline=deadline,
                single_request_worst_case=RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS,
                max_records=RECENT_COMMANDS_PER_PROJECT,
                order_by="modifiedTime desc",
            )
        except TypeError:
            pass
    return _enumerate_commands(store, project_id, deadline=deadline)


# DASHBOARD_TRUTH_CONNECTED gate 1/4: how many waiting_quota Tasks one
# poll_once() tick will attempt to promote into a real Command. Bounded
# exactly like MAX_COMMANDS_PER_POLL, for the same reason -- a project with
# many waiting_quota Tasks must not be able to consume the whole poll
# budget re-attempting dispatch() for all of them in one tick; the rest are
# simply picked up on a later natural tick, same as deferred Commands
# already are.
MAX_WAITING_QUOTA_PROMOTIONS_PER_POLL = 4

# Bounded task hydration window per project for the waiting_quota Phase 1
# sweep. Matches MAX_WAITING_QUOTA_PROMOTIONS_PER_POLL so single-tick Drive
# get_media() calls are strictly capped (at most K calls), and rotation
# advances by K positions per natural tick, yielding provable ceil(N / K)
# full-cycle reachability for any arbitrary N >= 1 backlog -- closing the
# residual gap where the plain (stride=1) rotate_offset fix guaranteed only
# eventual, not bounded-fast, reachability for a project with a very large
# pre-existing Tasks backlog (confirmed live: a real waiting_quota Task in
# ai-development-manager's own 181-record backlog still hadn't been reached
# after 3+ hours of continuous natural ticks under stride=1).
WAITING_QUOTA_DISCOVERY_WINDOW = MAX_WAITING_QUOTA_PROMOTIONS_PER_POLL

# Real P0 (2026-08-29): Phase 1 (the waiting_quota sweep, see poll_once())
# used to share the tick's full POLL_TIME_BUDGET_SECONDS deadline with
# Phase 2 (regular command processing) -- fine when each project's sweep is
# cheap, but live-reproduced with a project whose Task backlog had grown
# large over a long session: enumerating that ONE project's waiting_quota
# Tasks alone took 20+ real seconds, leaving under 10s of the 40s budget
# for Phase 2 -- below WATCHER_DISCOVERY_TIMEOUT_SECONDS's own per-request
# safety margin, so Phase 2's command discovery returned zero records
# EVERY tick, indefinitely, even for a project reached early in rotation
# with nothing ahead of it. A claimed Command past its claim timeout, and a
# genuinely queued Command, both sat frozen for hours as a result -- ticks
# kept completing successfully (exit 0), just never touching either.
# Capping Phase 1 to its own shorter sub-budget guarantees Phase 2 a real
# floor (POLL_TIME_BUDGET_SECONDS - PHASE_1_TIME_BUDGET_SECONDS, currently
# 25s) regardless of how expensive any project's waiting_quota sweep turns
# out to be -- Phase 1 already tolerates being cut off mid-sweep (nothing
# it does is destructive; an unreached project's waiting_quota Tasks are
# simply picked up on a later tick, same guarantee as before this cap
# existed), so bounding it costs nothing but promotion latency under a
# large backlog, in exchange for Phase 2 never again starving completely.
PHASE_1_TIME_BUDGET_SECONDS = 15


def _enumerate_waiting_quota_tasks(store, project_id, deadline=None, rotate_offset=None):
    """Deadline-aware, bounded-hydration enumeration of one project's
    waiting_quota Tasks -- the exact mirror of _enumerate_commands() above,
    generic DriveRecords.list_records_bounded()/list_records() over the
    "tasks" area instead of "commands". Filtering to the real waiting_quota
    signature (see _promote_waiting_quota_task()'s docstring for why
    recommended_provider is None AND quota_evidence is not None is the only
    non-guessed way to identify it) happens here, once, so callers never
    duplicate that evidence check.

    When `rotate_offset` is provided (e.g. from Phase-1 actual-invocation cursor),
    it uses that exact per-visit offset. Otherwise falls back to
    _within_project_record_rotation_offset."""
    if rotate_offset is None:
        rotate_offset = _within_project_record_rotation_offset(stride=WAITING_QUOTA_DISCOVERY_WINDOW)
    if hasattr(store, "list_records_bounded"):
        tasks = store.list_records_bounded(
            "tasks", project_id, deadline=deadline,
            single_request_worst_case=WATCHER_DISCOVERY_TIMEOUT_SECONDS,
            max_records=WAITING_QUOTA_DISCOVERY_WINDOW,
            rotate_offset=rotate_offset,
        )
    else:
        tasks = store.list_records("tasks", project_id)
    return [task for task in tasks
            if task.get("recommended_provider") is None and task.get("quota_evidence") is not None
            and task.get("status") not in ("completed", "cancelled", "blocked")]


# DASHBOARD_TRUTH_CONNECTED gate 3: how long one waiting_quota promotion
# attempt's own historical-estimate lookup (manager.dispatcher.dispatch()'s
# list_executions_bounded() call) may take, mirroring cloud.dispatch_ingress.
# INGRESS_DISPATCH_HISTORY_BUDGET_SECONDS's identical purpose for the
# original admission path. Deliberately smaller than that 15s ingress
# budget: up to MAX_WAITING_QUOTA_PROMOTIONS_PER_POLL promotions can run in
# one poll_once() tick sharing the same overall POLL_TIME_BUDGET_SECONDS=40s
# budget as command processing, so each individual promotion's own history
# lookup must stay small.
WAITING_QUOTA_PROMOTION_HISTORY_BUDGET_SECONDS = 5.0


def _promote_waiting_quota_task(store, service, task, quota_document):
    """Re-attempt automatic provider selection for one Task that
    manager.dispatcher.dispatch() previously admitted with no eligible
    provider (recommended_provider=None, a real quota_evidence assignment
    attempt recorded -- the waiting_quota state DASHBOARD_TRUTH_CONNECTED
    gate 1 requires; see manager.dashboard_core.compute_dispatch_state()'s
    own identical evidence check). Reuses the SAME task_id (dispatch()
    finds the existing Task and never creates a duplicate) and, only if a
    real provider is now eligible, creates exactly one Command for it.

    The re-dispatch request restores the ORIGINAL caller's own dispatch
    intent from the Task record itself -- preferred_provider,
    excluded_provider, account_id, needs_repo_edit -- rather than falling
    back to unconstrained automatic routing. This matters for provenance
    continuity (the promoted Command's requested_provider/requested_
    account_id must still reflect what was actually asked for, not
    fabricated None) and for correctness: a caller who explicitly asked for
    one specific (still-unavailable) provider must keep waiting for THAT
    provider, never be silently rerouted to a different one just because
    this sweep re-ran automatic selection instead. Live quota truth is
    still the hard gate either way -- dispatcher.dispatch() itself refuses
    (returns waiting_quota again, no Command) if the restored preference is
    still not actually eligible; restoring the preference never bypasses
    that.

    command_id is set to task_id: cloud.dispatch_ingress.py's original
    admission always reserves `task_id = command_id = f"dispatch-{request_
    id}"` for a Direct Dispatch request (see its own handle_dispatch()) --
    reusing that same identity here (rather than inventing a new command_id
    scheme) means a same-request_id ingress replay that polls for that
    exact reserved command_id finds the promoted Command once this runs,
    instead of permanently reporting waiting_quota. Only Tasks admitted via
    that trusted ingress path (source_context.origin ==
    TRUSTED_INGRESS_ORIGIN) carry this identity contract; anything else
    (manager.scheduler.schedule()'s own batch path, a manual `task-create`
    CLI/adm_create_task Task) is out of scope for this sweep -- their own
    callers already observe a waiting_quota dispatcher result directly and
    can act on it themselves.

    A Command is a required, non-null `provider` field by schema (see
    schema/command.schema.json) -- never created speculatively -- so this
    is a strict no-op (returns None) whenever dispatch() still reports
    waiting_quota this tick, or when a Command already exists for this
    identity (idempotent: a later tick, or another concurrent sweep, may
    already have promoted it). The existence check immediately before the
    write narrows -- it does not fully close, matching this codebase's
    existing accepted Drive-record-creation race class (see the Drive
    Execution reservation race backlog note) -- the window for two
    concurrent sweeps to both decide to promote the same Task; a real fix
    for that whole class of race is out of this task's scope.

    On success, the Task's own recommended_provider/mode/effort are also
    persisted (manager.tasks.update_task()) to reflect the real outcome --
    without this, a Task stays permanently misclassified as still-waiting
    (recommended_provider still None) even after promotion, forcing every
    later tick to needlessly re-discover and re-check it via a wasted
    Command-existence lookup forever."""
    project_id, task_id = task["project_id"], task["task_id"]
    if task.get("source_context", {}).get("origin") != TRUSTED_INGRESS_ORIGIN:
        return None
    try:
        store.get("commands", project_id, task_id)
        return None  # already promoted
    except TaskError:
        pass
    from manager.dispatcher import dispatch as dispatcher_dispatch
    request = {
        "project_id": project_id, "task_id": task_id, "title": task["title"],
        "task_type": task.get("task_type") or "general", "complexity": task.get("complexity", "medium"),
        "expected_minutes": task.get("expected_minutes") or 20,
        "needs_repo_edit": task.get("needs_repo_edit", True),
    }
    if task.get("preferred_provider"):
        request["preferred_provider"] = task["preferred_provider"]
    if task.get("excluded_provider"):
        request["excluded_provider"] = task["excluded_provider"]
    if task.get("account_id"):
        request["account_id"] = task["account_id"]
    # Deliberately no `executions` argument here (never `[]`): passing `[]`
    # positionally would make dispatch() treat "already have the data,
    # nothing to look up" (executions is not None) and silently discard
    # this project's real completed-execution history, fabricating
    # quota_evidence[provider]["historical_estimate"] as "no matching
    # completed executions" even when real history exists -- a Dashboard
    # Truth violation caught by parallel validation of this same function.
    # history_deadline instead makes dispatch() call its own bounded
    # list_executions_bounded(store, project_id, ...), the same real,
    # bounded history lookup cloud.dispatch_ingress.py's original admission
    # already uses.
    result = dispatcher_dispatch(store, service, request, quota_document,
                                 history_deadline=time.monotonic() + WAITING_QUOTA_PROMOTION_HISTORY_BUDGET_SECONDS)
    if result.get("waiting_quota") or not result.get("provider"):
        return None  # still no eligible provider this tick
    try:
        store.get("commands", project_id, task_id)
        return None  # a concurrent sweep already won -- do not overwrite it
    except TaskError:
        pass
    command = {
        "command_id": task_id, "project_id": project_id, "task_id": task_id,
        "provider": result["provider"], "account_id": result.get("account_id"),
        "requested_provider": task.get("preferred_provider"), "requested_account_id": task.get("account_id"),
        "model": result.get("model"), "fallback_model": result.get("fallback_model"),
        "mode": result.get("mode"), "effort": result.get("effort"), "selection_reason": result.get("selection_reason", []),
        "quota_evidence": result.get("quota_evidence"), "created_at": now_iso(), "status": "queued",
        "execution_id": None, "claimed_at": None, "completed_at": None, "result": None,
        "created_via": TRUSTED_INGRESS_ORIGIN,
    }
    # admission_version/request_id are non-nullable (plain "string") in
    # schema/command.schema.json, unlike requested_provider/
    # requested_account_id above -- only stamp them when the originating
    # Task's own source_context actually recorded a real value, rather than
    # ever writing an explicit null a validator would reject.
    source_context = task.get("source_context", {})
    if isinstance(source_context.get("admission_version"), str) and source_context["admission_version"]:
        command["admission_version"] = source_context["admission_version"]
    if isinstance(source_context.get("external_request_id"), str) and source_context["external_request_id"]:
        command["request_id"] = source_context["external_request_id"]
    validate("command", command)
    store.put("commands", project_id, task_id, command)
    from manager.tasks import update_task
    update_task(store, project_id, task_id, recommended_provider=result["provider"],
               mode=result.get("mode"), effort=result.get("effort"))
    return command


_COMMAND_PRIORITY = {"claimed": 0, "running": 0, "queued": 1, "attention": 2}


def _prioritized_nonterminal_commands(commands, attention_rotation=0):
    """Stable-reorder one project's already-hydrated, already-hydration-bounded
    Command batch so active-lifecycle authority (claimed/running) is always
    considered first, actionable new work (queued) second, and stale
    backlog (attention) last. Pure and zero-remote-lookup: every completed/
    failed Command is dropped here unconditionally, exactly like the
    original inline filter poll_once() always had -- terminal-recovery
    eligibility (which requires a real Execution lookup) is a SEPARATE,
    deliberately later concern, see _terminal_recovery_candidates().

    P0-A fix: this function previously (briefly) also decided terminal-
    recovery eligibility inline, via an optional `store` argument -- that
    made ordinary priority sorting do a synchronous primary-store Execution
    lookup for every terminal record in the batch, which could let one
    slow/failing lookup consume the tick's time budget or raise before
    claimed/running/queued/attention work -- work that needs ZERO remote
    calls -- ever got its chance to run. This function is now provably
    lookup-free: it only ever reads fields already present on the
    in-memory `commands` list.

    This changes only the ORDER process_command() is called in for an
    already-returned batch; it does not change which records are in the
    batch (list_records_bounded's own deadline-bounded hydration is
    untouched) or how many get processed (MAX_COMMANDS_PER_POLL is still
    enforced by the caller).

    Why the attention-vs-queued ordering rule exists at all: a stale
    `attention` Command sitting ahead of a `queued` one in Drive's own
    (unspecified, effectively arbitrary) listing order could consume the one
    process_command() slot a tight poll budget leaves after discovery,
    starving genuinely actionable queued work behind old recovery backlog
    indefinitely -- see the real production trace this fixes. Sorting is
    stable (Python's sorted()), so relative order within each priority group
    is preserved unchanged from Drive's own return order."""
    ordered = sorted(
        (c for c in commands if c.get("status") not in ("completed", "failed")),
        key=lambda c: _COMMAND_PRIORITY.get(c.get("status"), len(_COMMAND_PRIORITY)),
    )
    # Bounded fairness INSIDE the attention group (live 20260902): with two
    # attention records in one hydrated batch, the stable sort always put
    # the same one first, and a tick whose remaining recent-sweep budget
    # was consumed by that first record's own reconciliation broke out of
    # the loop before the second one every single tick. Rotating the
    # attention suffix by `attention_rotation` (poll_once passes the
    # wall-clock tick counter) guarantees every attention record in a batch
    # of n is first within n consecutive ticks -- independent of how much
    # budget its neighbors burn. claimed/running/queued ordering is
    # untouched, and attention still never precedes them.
    attention_start = next((i for i, c in enumerate(ordered) if c.get("status") == "attention"), len(ordered))
    attention = ordered[attention_start:]
    if len(attention) > 1 and attention_rotation:
        shift = attention_rotation % len(attention)
        attention = attention[shift:] + attention[:shift]
    return ordered[:attention_start] + attention


TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS = 10

# LIVE P0 fix (terminal recovery budget starvation, observed on a real
# production tick): a large project's FULL-sweep hydration
# (_enumerate_commands(), one real Drive request per record within its own
# bounded budget) can legitimately consume nearly the ENTIRE shared poll
# deadline by itself -- a real live measurement against ai-development-
# manager's 234-record Command history took ~28s of the ~40s
# POLL_TIME_BUDGET_SECONDS. Phase 2c's own headroom guard
# (deadline - now >= TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS) then
# correctly, safely refuses to even attempt a classification lookup once
# hydration alone has already spent the budget down to under that many
# seconds of headroom -- which is the RIGHT thing for that guard to do in
# isolation, but meant a genuinely reachable, already-cached terminal
# candidate (confirmed present in the batch, at the front of it, with zero
# competing nonterminal work) still got ZERO classification attempts,
# tick after tick, forever: full hydration was never leaving Phase 2c
# enough of the shared deadline to ever pass its own safety check.
#
# Fix: reserve a slice of the poll deadline for terminal recovery,
# specifically by making Phase 2a/2b's HYDRATION calls (never their
# processing of already-discovered nonterminal work, which must keep
# using the full `deadline` -- see poll_once()) stop starting further
# optional enumeration once fewer than this many seconds remain before
# the real poll deadline. `TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS` alone
# is the bare minimum a single classification lookup could need; a few
# seconds of margin absorb the ordinary interpreter/bookkeeping overhead
# between hydration yielding and Phase 2c's own headroom check running.
TERMINAL_RECOVERY_RESERVED_SECONDS = TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS + 5

# Budget for the INDEPENDENT terminal_recovery_once() pass (round-2 delta
# review finding): the pass's hydration slice must be usable through the
# real DriveRecords.list_records_bounded() guard, which refuses to START
# hydrating a record unless a full single_request_worst_case
# (WATCHER_DISCOVERY_TIMEOUT_SECONDS) still fits before the deadline.
# A 15s pass minus 10s classification headroom left a 5s hydration slice
# -- strictly smaller than that guard's 10s reserve, so with the real
# production store the pass could never hydrate a single record (every
# test double that skipped the guard hid this). Partition explicitly:
# hydration gets the guard's own worst-case reserve PLUS a genuinely
# usable start window, then classification keeps its full headroom on
# top. All of it bounds STARTS only, like every deadline in this module.
TERMINAL_RECOVERY_PASS_HYDRATION_SECONDS = WATCHER_DISCOVERY_TIMEOUT_SECONDS + 10
TERMINAL_RECOVERY_PASS_BUDGET_SECONDS = (
    TERMINAL_RECOVERY_PASS_HYDRATION_SECONDS + TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS)


class _TerminalRecoveryPrevalidation:
    """Internal-only proof that _terminal_recovery_candidates() already
    performed exactly one bounded, short-timeout eligibility lookup for
    this exact terminal Command this tick -- constructed ONLY inside that
    function, for a genuinely eligible candidate, never by a plain
    boolean/truthy value an arbitrary caller could pass to skip
    process_command()'s eligibility gate on their own say-so (see
    _prevalidation_matches() below, which process_command() uses to
    re-validate every identity field against the CURRENT command before
    trusting this at all).

    Blocker-1 fix (third residual-P0-A review): without this, a candidate
    _terminal_recovery_candidates() already paid one bounded classification_
    store lookup to validate got a SECOND, entirely separate, unbudgeted
    lookup against the shared ~45s-timeout primary `store` the instant
    process_command() ran its own independent eligibility gate --
    defeating both TERMINAL_LOOKUP_ATTEMPT_BUDGET_ENFORCED (the budget
    only ever counted the first lookup) and the whole point of
    classification_store's short timeout (the second lookup used the
    primary store's long one). Threading this object from classification
    through to process_command() means the SAME lookup's result is reused,
    never re-derived -- "eligibility classification" happens exactly once
    per attempt; any further reads process_command()'s existing recovery
    path (_reconcile_active(), retry_incomplete_terminal_persistence())
    performs afterward are ordinary idempotent RECOVERY reads, not a
    second classification decision, and are unaffected by this."""
    __slots__ = ("project_id", "command_id", "execution_id", "status")

    def __init__(self, project_id, command_id, execution_id, status):
        self.project_id = project_id
        self.command_id = command_id
        self.execution_id = execution_id
        self.status = status


def _prevalidation_matches(prevalidation, command):
    """Fail-closed identity check: a prevalidation object is only ever
    trusted when EVERY identity field it recorded still matches the
    CURRENT command exactly. Anything else -- wrong type (so a stray
    True/False/None a careless caller passed is silently ignored, never
    crashes), a mismatched project_id/command_id/execution_id, or a
    status that has somehow changed since classification -- returns False,
    which sends process_command() back through its own normal, full
    _terminal_command_needs_recovery() eligibility check exactly as if no
    prevalidation had ever been offered. This is the only path by which a
    prevalidation can ever grant anything: there is no bare boolean bypass
    a caller could substitute for real classification."""
    if not isinstance(prevalidation, _TerminalRecoveryPrevalidation):
        return False
    return (prevalidation.project_id == command.get("project_id")
            and prevalidation.command_id == command.get("command_id")
            and prevalidation.execution_id == command.get("execution_id")
            and prevalidation.status == command.get("status"))


def _terminal_recovery_candidates(commands, store, deadline, lookup_budget):
    """The SECOND, deliberately later, GLOBAL pass -- run only once EVERY
    project's nonterminal work for this tick has already had its turn (see
    poll_once()'s Phase 2c) -- over an already-hydrated, already-bounded
    Command batch: pick out completed/failed records whose linked
    Execution still needs cleanup/materialization recovery
    (_terminal_command_needs_recovery), so process_command()'s existing
    _reconcile_active() logic (added for R17, never able to fire from
    poll_once() before this fix) actually gets a chance to run.

    Residual-P0-A fix: `lookup_budget` bounds LOOKUP ATTEMPTS, not eligible
    results -- every completed/failed record this function actually
    examines (i.e. calls _terminal_command_needs_recovery for) consumes one
    unit of budget REGARDLESS of the outcome (eligible, not eligible,
    ambiguous, or an exception). Before this fix, a `remaining_slots` that
    bounded only the COUNT OF ELIGIBLE RESULTS meant an unbounded number of
    non-eligible/ambiguous candidates could each still trigger a real
    Execution lookup -- 5 ambiguous terminal records with only 1 real
    process_command() slot left still cost 5 lookups, not 1. Returns
    `(candidates, remaining_lookup_budget)` so a caller iterating multiple
    projects' cached batches in the same tick (poll_once()'s Phase 2c) can
    thread the SAME shrinking budget across all of them, in rotated project
    order, for cross-project terminal-recovery fairness -- see that
    function's own docstring.

    Still bounded and fail-closed exactly as before:

    - `lookup_budget` <= 0 short-circuits to `(candidates, lookup_budget)`
      immediately -- no lookups are attempted once the tick has no budget
      left to spend on them.
    - Before every single candidate's lookup, this checks BOTH
      `time.monotonic() >= deadline` AND (residual-P0-A fix)
      `deadline - time.monotonic() < TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS`
      -- a lookup is never even started unless there is enough headroom left
      for its own worst-case duration, the same "never start a request whose
      own worst case could run past the deadline" rule
      DriveRecords.list_records_bounded() already uses for hydration. This
      still cannot make an already-started call return early -- see `store`
      below for the actual hard bound on that.
    - `store` is expected to be a store built from a genuinely SHORT,
      dedicated transport timeout (TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS,
      not the ~45s default every write/active-lifecycle call uses) -- see
      poll_once()'s `classification_store` parameter and main()'s
      construction of it. This is what actually solves "a single lookup can
      block past the whole poll budget": a real OS-level socket timeout
      (the same httplib2 mechanism WATCHER_DISCOVERY_TIMEOUT_SECONDS/
      RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS already rely on elsewhere in
      this module) bounds how long the call itself can possibly run,
      independent of any monotonic-clock check here.
    - Each candidate's eligibility check is individually wrapped: ANY
      exception (TaskError, the classification store's own bounded-timeout
      transport failure, or anything else) is treated as "not eligible this
      tick" and moves on to the NEXT candidate -- it never aborts this
      function, and therefore never aborts the whole poll_once() tick.
    - Never a new/separate/unbounded scan: `commands` is exactly one of the
      same small batches (RECENT_COMMANDS_PER_PROJECT=2 for the recent
      sweep, or whatever a project's bounded hydration window returned for
      the full sweep) already fetched and cached by the caller.

    Returned in Drive's own original relative order (not re-sorted) -- by
    construction every kept record already shares the same "no explicit
    priority tier" default rank in _COMMAND_PRIORITY, and this pass never
    runs until every higher-priority record has already had its turn, so
    no additional sort is meaningful here.

    Blocker-1 fix: returns `(command, prevalidation)` pairs, not bare
    commands -- `prevalidation` is a _TerminalRecoveryPrevalidation caller
    must thread through to process_command() (see its
    `terminal_recovery_prevalidation` parameter) so the ONE bounded,
    short-timeout lookup already performed here is reused rather than
    process_command() re-deriving the same answer with a second, entirely
    unbudgeted lookup against the primary store."""
    candidates = []
    if lookup_budget <= 0:
        return candidates, lookup_budget
    for command in commands:
        if command.get("status") not in ("completed", "failed"):
            continue
        if lookup_budget <= 0:
            break
        if time.monotonic() >= deadline or deadline - time.monotonic() < TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS:
            break
        lookup_budget -= 1
        try:
            eligible = _terminal_command_needs_recovery(store, command)
        except Exception:
            # Fail closed FOR THIS CANDIDATE ONLY (P0-A): an ambiguous or
            # failed lookup is never treated as "needs recovery", and never
            # propagates -- the next candidate (and every already-processed
            # nonterminal command) is completely unaffected. The attempt is
            # still consumed above regardless of this outcome.
            eligible = False
        if eligible:
            prevalidation = _TerminalRecoveryPrevalidation(
                project_id=command.get("project_id"), command_id=command.get("command_id"),
                execution_id=command.get("execution_id"), status=command.get("status"))
            candidates.append((command, prevalidation))
    return candidates, lookup_budget


def poll_once(store, service, allowlist=None, deadline=None, discovery_store=None, recent_store=None,
              classification_store=None, origin_context=None, async_launch=False, cursor_path=None, **factories):
    """`deadline`, if given, is a `time.monotonic()` value after which this
    call stops STARTING new project/command work and returns whatever it has
    so far -- any project/command not yet reached this tick is picked up on
    a later poll, with no state lost. (Sole, bounded exception: Phase 2c
    terminal-recovery classification may still start inside its own
    reserved window of at most TERMINAL_RECOVERY_RESERVED_SECONDS past
    this deadline -- see the phase2c_deadline comment below.) (nothing here writes anything before a
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
    `classification_store` is the same idea, dedicated to terminal-recovery
    Execution lookups (see TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS/Phase 2c
    below) -- also defaults to `store` when omitted.

    Project enumeration itself (_enumerate_project_ids above) is also
    deadline-aware and does not hydrate full project documents.
    Phase 1 waiting_quota sweep and Phase 2 command processing use a durable
    actual-invocation cursor (manager.phase1_cursor), advancing one project
    per actual invocation (M=1) and advancing within-project record offsets
    contiguously on each visit. This eliminates cross-project time starvation
    and orbital GCD record starvation caused by wall-clock aliasing.

    Within one project's already-returned bounded batch, nonterminal
    commands are processed in _prioritized_nonterminal_commands() order
    (claimed/running, then queued, then attention -- stable within each
    group), not Drive's own return order, so a tight remaining budget after
    discovery spends its one available process_command() slot on active-
    lifecycle authority or new actionable work before stale attention
    backlog. See _prioritized_nonterminal_commands()'s docstring for the
    production trace that motivated this.

    Terminal-recovery classification (a completed/failed Command whose
    Execution still needs cleanup/materialization recovery) is a separate,
    GLOBAL, deliberately LAST phase (2c): every rotated project's
    nonterminal work (both the recent sweep and the full sweep) is given
    its full turn first, across ALL projects, before ANY terminal
    Execution lookup is attempted for ANY project -- not per-project, which
    an earlier round of this fix still did and which could let a slow or
    failing lookup in an early-rotated project delay a later-rotated
    project's genuinely actionable queued/running/claimed work. See
    _terminal_recovery_candidates()'s docstring for the full contract
    (lookup-attempt budgeting, deadline headroom, and the dedicated
    short-timeout `classification_store` that actually hard-bounds a
    single lookup's own worst-case duration)."""
    if allowlist is None:
        allowlist = load_allowlist()
    if deadline is None:
        deadline = time.monotonic() + POLL_TIME_BUDGET_SECONDS
    if discovery_store is None:
        discovery_store = store
    if recent_store is None:
        recent_store = discovery_store
    if classification_store is None:
        classification_store = store
    results = []
    # DASHBOARD_TRUTH_CONNECTED gate 1/4: waiting_quota Task promotion is
    # this tick's OWN natural retry -- lazily fetched at most once per
    # poll_once() call (never per-project/per-task) and reused across every
    # promotion attempted this tick, so a project with no waiting_quota
    # Tasks at all never pays for a quota read it doesn't need, and every
    # promotion decided in the same tick sees one consistent snapshot.
    sweep_quota_document = []  # 0 or 1 element: lazily-cached quota_document, or [False] once a read fails
    promotions_this_poll = 0

    def sweep_quota():
        if not sweep_quota_document:
            try:
                sweep_quota_document.append(read_drive_status(service=service))
            except Exception:
                sweep_quota_document.append(False)
        return sweep_quota_document[0] or None

    raw_project_ids = _enumerate_project_ids(discovery_store, deadline=deadline)
    if not raw_project_ids:
        return results

    from manager.phase1_cursor import (CREATE_ONLY, CursorLockError, CursorMissingError,
                                       CursorRecoveryRequiredError, CursorStateError,
                                       StaleCursorError, bind_phase1_cursor_path,
                                       default_phase1_cursor, load_phase1_cursor,
                                       require_phase1_cursor_first_boot, save_phase1_cursor)

    # Bound ONCE for the whole tick. The load below and the save at the
    # end of the tick both receive this same absolute path object, so a
    # relative home or cursor path plus a cwd/environment change midway
    # through the tick cannot make the tick read one file and advance a
    # decoy. An unresolvable home raises ManagerHomeError here, exactly as
    # the load used to, before any cursor state has been consulted.
    phase1_cursor_path = bind_phase1_cursor_path(cursor_path=cursor_path)

    # `cursor_cas_token` is this invocation's write permit, and the
    # branches below are the states the durable cursor can be in.
    # Keeping them apart is the whole fix: the old loader collapsed all
    # of them into "generation 0", so a cursor that merely failed to read
    # was indistinguishable from one that had never existed, and the next
    # save rebuilt it from zero.
    #
    #   absent, proven first boot -> CREATE_ONLY. No cursor, no custody
    #                  claim, and no record of a committed initialization.
    #   absent, otherwise -> None. The cursor EXISTED and is gone, or a
    #                  custody claim from an interrupted mutation holds
    #                  the last copy, or the initialization record is
    #                  garbage. Recreating from zero is the 2026-09-02
    #                  incident, so the tick refuses and leaves the
    #                  decision to a human.
    #   present     -> its durable generation. Normal CAS amendment.
    #   unreadable  -> None, meaning "do not persist at all". The tick
    #                  still serves commands, but it will not overwrite
    #                  state it could not read -- that is precisely how a
    #                  generation-2458 file becomes a generation-1 one
    #                  holding 5 projects instead of 13. The bad file is
    #                  left exactly as it is, as evidence for a human.
    try:
        cursor = load_phase1_cursor(cursor_path=phase1_cursor_path, missing_ok=False)
        cursor_cas_token = cursor["generation"]
    except CursorMissingError:
        cursor = default_phase1_cursor()
        # Absence alone does not authorise creation; the recovery protocol
        # is what separates a first boot from a lost cursor.
        try:
            require_phase1_cursor_first_boot(cursor_path=phase1_cursor_path)
        except (CursorRecoveryRequiredError, CursorStateError, StaleCursorError) as exc:
            print("PHASE1_CURSOR: durable cursor is MISSING and this is not a provable first boot; "
                  "refusing to reinitialize Phase-1 rotation from zero. Not persisting this tick. "
                  f"This needs a human: {exc}", file=sys.stderr)
            cursor_cas_token = None
        else:
            cursor_cas_token = CREATE_ONLY
    except CursorStateError as exc:
        print(f"PHASE1_CURSOR: durable cursor unusable, not persisting this tick: {exc}", file=sys.stderr)
        cursor = default_phase1_cursor()
        cursor_cas_token = None

    num_projects = len(raw_project_ids)
    proj_idx = cursor.get("project_cursor", 0) % num_projects

    # Project rotation is ordered by actual service progress, starting at target_project_id
    project_ids = raw_project_ids[proj_idx:] + raw_project_ids[:proj_idx]
    target_project_id = project_ids[0]

    just_promoted = set()
    phase1_deadline = min(deadline, time.monotonic() + PHASE_1_TIME_BUDGET_SECONDS)

    # Phase 1: Dedicated bounded slice for M=1 project (target_project_id).
    # Advances actual-invocation cursor deterministically.
    record_cursors = cursor.get("per_project_record_cursor", {})
    record_offset = record_cursors.get(target_project_id, 0)

    try:
        try:
            waiting_tasks = _enumerate_waiting_quota_tasks(
                discovery_store, target_project_id, deadline=phase1_deadline, rotate_offset=record_offset
            )
        except TypeError:
            waiting_tasks = _enumerate_waiting_quota_tasks(
                discovery_store, target_project_id, deadline=phase1_deadline
            )
    except TaskError:
        waiting_tasks = []

    for task in waiting_tasks:
        if promotions_this_poll >= MAX_WAITING_QUOTA_PROMOTIONS_PER_POLL or time.monotonic() >= phase1_deadline:
            break
        quota_document = sweep_quota()
        if quota_document is None:
            break  # quota unavailable this tick -- do not attempt more promotions
        try:
            promoted = _promote_waiting_quota_task(store, service, task, quota_document)
            if promoted is not None:
                promotions_this_poll += 1
                just_promoted.add((target_project_id, promoted["command_id"]))
        except TaskError:
            continue

    # Advance project cursor to next project (0 -> 1 -> ... -> P-1 -> 0) and advance target project's record offset.
    # Persisted ONCE per invocation, together with this tick's attention-visit
    # advances, by _persist_cursor() in the `finally` below -- one save per
    # invocation, so the actual-invocation generation stays exactly that
    # (bounded delta review round 3: two advancing saves per invocation let
    # an overlapping writer's second save be rolled back by the first
    # writer's stale payload; a single save leaves only the pre-existing,
    # out-of-scope equal-generation lost update of the non-atomic CAS).
    cursor["project_cursor"] = (proj_idx + 1) % num_projects
    cursor["per_project_record_cursor"][target_project_id] = record_offset + WAITING_QUOTA_DISCOVERY_WINDOW

    # Phase 2a/2b: inspect a tiny modified-time-ordered batch from every
    # project before historical hydration, then a regular full bounded
    # sweep in the same rotated order using whatever budget remains -- both
    # NONTERMINAL WORK ONLY. Residual-P0-A fix: nonterminal work (claimed/
    # running/queued/attention) is processed GLOBALLY across every rotated
    # project before terminal-recovery classification is considered for ANY
    # project -- an earlier round of this fix still interleaved "project A
    # nonterminal -> project A terminal lookup -> project B nonterminal"
    # per-project, which let a slow/failing terminal lookup in an
    # early-rotated project delay a later-rotated project's genuinely
    # actionable queued/running/claimed work, exactly the starvation this
    # whole fix exists to prevent, just moved up one level. Each project's
    # already-hydrated, already-bounded batch is cached in
    # `hydrated_commands_by_project` as it is fetched (from EITHER sweep),
    # so the later global terminal-recovery phase (2c) never re-enumerates
    # or re-hydrates anything -- it only ever looks at batches this tick
    # already paid to fetch.
    processed = set()
    hydrated_commands_by_project = {}

    def _remember_batch(project_id, commands):
        bucket = hydrated_commands_by_project.setdefault(project_id, {})
        for c in commands:
            bucket[c["command_id"]] = c

    def _run(project_id, command, prevalidation=None):
        try:
            results.append(process_command(store, service, command, allowlist=allowlist,
                                           origin_context=origin_context, async_launch=async_launch,
                                           terminal_recovery_prevalidation=prevalidation, **factories))
        except Exception as exc:
            # One command's processing must never take the rest of this
            # project's queue -- or any later-rotated project's -- down
            # with it. Before this isolation existed, an uncaught exception
            # here propagated straight out of poll_once() and was only ever
            # caught by main()'s top-level `except Exception`, which
            # discards the whole tick's results and retries next minute --
            # with the SAME command still first in priority order
            # (claimed/running always sort ahead of queued), so a command
            # that reliably threw once reliably threw on every subsequent
            # tick too, indefinitely starving every other command in the
            # same project behind it. Recording the failure here and
            # moving on to the next command keeps that guarantee real: a
            # single bad record's blast radius is itself, not its
            # neighbors.
            results.append({"status": "error", "project_id": project_id,
                            "command_id": command["command_id"], "error": repr(exc)})
        processed.add((project_id, command["command_id"]))

    # LIVE P0 fix: hydration (fetching MORE projects'/records' Command
    # batches) must yield once fewer than TERMINAL_RECOVERY_RESERVED_SECONDS
    # remain before the real poll deadline -- see that constant's own
    # docstring for the live production trace this closes. This bounds
    # only STARTING further optional enumeration; a project's own
    # already-hydrated nonterminal commands are still processed against
    # the full, unreserved `deadline` below (`_run()`'s own checks), so
    # already-discovered active/queued work is never held back by this
    # reservation -- only additional, not-yet-fetched historical hydration
    # is what yields.
    hydration_deadline = deadline - TERMINAL_RECOVERY_RESERVED_SECONDS

    recent_deadline = min(deadline, time.monotonic() + RECENT_COMMAND_SWEEP_BUDGET_SECONDS)
    recent_hydration_deadline = min(recent_deadline, hydration_deadline)
    # Attention-group rotation base (see _prioritized_nonterminal_commands):
    # a DURABLE per-project visit counter from the Phase-1 cursor, advanced
    # only when that project's batch is actually swept with more than one
    # attention record in it. Independent adversarial review of the first
    # round rejected a wall-clock tick counter for this: real ticks run
    # ~90-120s apart (60s trigger + IgnoreNew), so int(time()//60) advances
    # by 2 per sweep and a 2-record attention group never rotated at all,
    # and any skipped bucket can permanently exclude a residue class of a
    # larger group. Counting actual sweeps of this exact project makes the
    # round-robin strict regardless of cadence, budget, or which invocations
    # happen to reach the project.
    attention_visits = dict(cursor.get("per_project_attention_visits") or {})
    attention_rotation_by_project = {}
    attention_visited = set()

    def _attention_rotation(project_id, commands):
        rotation = attention_rotation_by_project.setdefault(project_id, attention_visits.get(project_id, 0))
        if project_id not in attention_visited and sum(1 for c in commands if c.get("status") == "attention") > 1:
            attention_visited.add(project_id)
            attention_visits[project_id] = rotation + 1
        return rotation

    def _persist_cursor():
        # The single durable cursor save of this invocation: Phase 1's
        # project/record advance plus this tick's attention-visit advances.
        # Runs on EVERY exit of the nonterminal sweeps below (normal
        # completion, the full sweep's deadline `return`, MAX_COMMANDS
        # break, or an exception) -- bounded round-robin only holds if a
        # visit that already decided this tick's rotation is durably
        # counted; a deadline exit that dropped it would restore the exact
        # same rotation next tick (bounded delta review, round 2). CAS on
        # the generation this invocation loaded; losing that CAS means
        # another invocation owns the cursor and nothing is written.
        if cursor_cas_token is None:
            return
        cursor["per_project_attention_visits"] = attention_visits
        try:
            # The SAME bound path object the load used -- never re-resolved.
            save_phase1_cursor(cursor, cursor_path=phase1_cursor_path,
                               expected_generation=cursor_cas_token)
        except (StaleCursorError, CursorStateError, CursorLockError,
                CursorRecoveryRequiredError, OSError) as exc:
            # Every one of these means "someone else owns the cursor, or
            # it cannot be trusted right now". Skipping the save costs one
            # tick of rotation; writing anyway costs the durable state.
            # CursorContractError is deliberately NOT caught: it can only
            # mean this call site stopped presenting a valid CAS token,
            # and swallowing that is how the original footgun stayed
            # invisible for as long as it did.
            print(f"PHASE1_CURSOR: not persisting this tick: {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    try:
        for project_id in project_ids:
            if len(results) == MAX_COMMANDS_PER_POLL or time.monotonic() >= recent_hydration_deadline:
                break
            try:
                commands = _enumerate_recent_commands(recent_store, project_id, deadline=recent_hydration_deadline)
            except TaskError:
                continue
            _remember_batch(project_id, commands)
            # Zero-remote-lookup nonterminal priority work (claimed/running/
            # queued/attention) only -- terminal-recovery eligibility (which
            # requires a real Execution lookup) is entirely deferred to the
            # GLOBAL Phase 2c below. Uses the full `recent_deadline`, not the
            # tightened `recent_hydration_deadline` -- already-hydrated active
            # work is never held back by the terminal-recovery reservation.
            for command in _prioritized_nonterminal_commands(commands, attention_rotation=_attention_rotation(project_id, commands)):
                if (project_id, command["command_id"]) in just_promoted:
                    continue
                if len(results) == MAX_COMMANDS_PER_POLL or time.monotonic() >= recent_deadline:
                    break
                _run(project_id, command)

        for project_id in project_ids:
            if time.monotonic() >= hydration_deadline:
                break
            try:
                commands = _enumerate_commands(discovery_store, project_id, deadline=hydration_deadline)
            except TaskError:
                # A project that has never had a single Command written to it
                # yet has no COMMANDS Drive folder at all --
                # list_records_bounded()/list_records() raise TaskError("Drive
                # folder not found") for that completely normal case, not a
                # real error; nothing here needs any per-project fallback
                # beyond skipping this project's own command processing (Phase
                # 1's waiting_quota sweep above is already independent of this
                # loop entirely, unlike before this restructure).
                continue
            _remember_batch(project_id, commands)
            for command in _prioritized_nonterminal_commands(commands, attention_rotation=_attention_rotation(project_id, commands)):
                if (project_id, command["command_id"]) in just_promoted:
                    continue  # promoted this same tick -- launches on a later natural tick, not this one
                if (project_id, command["command_id"]) in processed:
                    continue
                if len(results) == MAX_COMMANDS_PER_POLL:
                    # Global command-processing budget reached. `break` (not
                    # `return`) only stops processing MORE commands for THIS
                    # project -- every later-rotated project's own command loop
                    # hits this same check on its first iteration and breaks
                    # immediately too (the shared `results` list is still full),
                    # so the total command-processing budget stays exactly as
                    # bounded as before this fix. Phase 1 above has already run
                    # to completion for every project regardless, so this can
                    # never again starve anyone's waiting_quota sweep the way
                    # the pre-fix combined single-pass loop did.
                    break
                if time.monotonic() >= deadline:
                    return results
                _run(project_id, command)
    finally:
        _persist_cursor()


    # Phase 2c: GLOBAL terminal-recovery classification -- only now, after
    # EVERY rotated project's nonterminal work (both sweeps) has already
    # had its turn, and only from the batches already cached above (never
    # a new enumeration). `lookup_budget` bounds LOOKUP ATTEMPTS (not
    # eligible results -- see _terminal_recovery_candidates()'s own
    # docstring) and is threaded across every project's cached batch in
    # the SAME rotated order used throughout this tick, so which project's
    # terminal backlog gets first look at the budget naturally rotates
    # tick over tick along with everything else -- historical R17-shaped
    # backlog in a later-rotated project is never permanently starved by
    # an earlier one always consuming the whole budget first.
    #
    # Blocker-2 fix (third residual-P0-A review): this iterates
    # `project_ids` itself -- the SAME canonical rotated order the whole
    # rest of this tick already used -- not the order projects happened to
    # be inserted into `hydrated_commands_by_project`. Before this fix,
    # that dict's insertion order followed whichever sweep call HAPPENED
    # to succeed first for each project: if project p1's recent sweep
    # raised TaskError (transient failure) but its full sweep succeeded
    # later, while p2's recent sweep succeeded immediately, p2 would be
    # inserted first regardless of p1 being the tick's true first-rotated
    # project -- letting a project with a merely flaky recent sweep lose
    # terminal-recovery priority to a later-rotated one indefinitely, even
    # though `project_ids` itself correctly still rotates p1 first.
    # `hydrated_commands_by_project.get(project_id)` is None/empty for any
    # rotated project neither sweep reached or hydrated anything for this
    # tick -- skipped, never re-enumerated.
    #
    # `classification_store`, if given, is what actually bounds a single
    # lookup's own worst-case duration to something far shorter than the
    # whole poll budget (TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS, via a
    # genuinely different, shorter-timeout transport -- see main()'s
    # construction of it and _terminal_recovery_candidates()'s docstring);
    # defaults to `store` when omitted, exactly like `discovery_store`/
    # `recent_store` above, reproducing prior behavior for any caller not
    # yet updated to pass it.
    # P1 delta fix (independent adversarial review of 95768752, finding
    # 1): the hydration_deadline reservation above bounds only optional
    # ENUMERATION -- it cannot bound the nonterminal _run() calls both
    # Phase 2 loops deliberately keep running against the full, unreserved
    # `deadline` (that unreserved processing is itself a hard design
    # requirement: already-discovered active work is never held back by
    # terminal recovery). A single reconcile of a cached running command
    # goes through the primary 45s-timeout `store`, so it can legitimately
    # consume 30+ seconds AFTER hydration already yielded properly -- and
    # when the same healthy-but-slow command sorts first every tick, Phase
    # 2c once again got zero classification attempts forever, the exact
    # starvation shape the reservation was supposed to close.
    #
    # Fix: Phase 2c gets its own INDEPENDENT bounded execution window
    # instead of sharing whatever happens to be left of `deadline`:
    #  - a tick with headroom to spare behaves exactly as before
    #    (phase2c_deadline == deadline);
    #  - a tick whose nonterminal processing ate into (or past) the
    #    reserved slice extends Phase 2c's own window to
    #    now + TERMINAL_RECOVERY_RESERVED_SECONDS, restoring the >=
    #    TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS headroom its guard needs;
    #  - the extension is HARD-CAPPED at
    #    deadline + TERMINAL_RECOVERY_RESERVED_SECONDS, so a tick that
    #    overran even further (e.g. a full 45s transport-timeout stall)
    #    skips this in-poll pass for that tick rather than letting the
    #    window drift with the overrun. Like every deadline in this
    #    module, the cap bounds what may START -- an already-started
    #    lookup or recovery _run() is never interrupted, so the cap is a
    #    start bound, not a wall-clock tick-length bound (that has always
    #    been poll_once()'s documented deadline semantics; see the
    #    function docstring). A schedule whose active work overruns past
    #    the cap on EVERY tick would therefore starve this in-poll pass
    #    indefinitely -- which is exactly why it is no longer the only
    #    path: terminal_recovery_once() below runs with its own
    #    independent budget every scheduler cycle, AFTER poll_once(), and
    #    cannot be starved by anything that happens inside this function.
    #    This in-poll pass remains the cheap, same-tick fast path for the
    #    common case (candidate already cached, tick healthy).
    # Every single lookup inside stays bounded by classification_store's
    # own short transport timeout exactly as before; the guard in
    # _terminal_recovery_candidates() is unchanged.
    phase2c_deadline = min(deadline + TERMINAL_RECOVERY_RESERVED_SECONDS,
                           max(deadline, time.monotonic() + TERMINAL_RECOVERY_RESERVED_SECONDS))
    if len(results) < MAX_COMMANDS_PER_POLL and time.monotonic() < phase2c_deadline:
        lookup_budget = MAX_COMMANDS_PER_POLL - len(results)
        for project_id in project_ids:
            if lookup_budget <= 0 or len(results) == MAX_COMMANDS_PER_POLL or time.monotonic() >= phase2c_deadline:
                break
            bucket = hydrated_commands_by_project.get(project_id)
            if not bucket:
                continue
            commands = list(bucket.values())
            candidates, lookup_budget = _terminal_recovery_candidates(
                commands, classification_store, phase2c_deadline, lookup_budget)
            for command, prevalidation in candidates:
                if (project_id, command["command_id"]) in just_promoted:
                    continue
                if (project_id, command["command_id"]) in processed:
                    continue
                if len(results) == MAX_COMMANDS_PER_POLL:
                    break
                if time.monotonic() >= phase2c_deadline:
                    return results
                _run(project_id, command, prevalidation=prevalidation)
    return results


def terminal_recovery_once(store, service, allowlist=None, deadline=None, discovery_store=None,
                           classification_store=None, origin_context=None, async_launch=False, **factories):
    """Independent, bounded terminal-recovery pass -- the unstarveable
    guarantee behind poll_once()'s own in-poll Phase 2c fast path.

    Why this exists (second independent adversarial review round): any
    terminal-recovery pass that runs INSIDE poll_once()'s shared tick can
    be starved by legitimately slow active work, because nonterminal
    _run() calls are deliberately never interrupted (a real provider
    lifecycle must run to natural completion) and go through the primary
    ~45s-timeout transport. A production-valid repeating schedule --
    Phase 1 cost plus one persistent running command whose per-tick
    reconciliation is slow-but-successful -- therefore reaches the
    in-poll pass after even its capped extension window on EVERY tick,
    forever. No amount of within-tick budget arithmetic can fix that
    while also (a) never delaying active work behind terminal recovery
    and (b) keeping tick length bounded: the three constraints are
    mutually unsatisfiable inside one shared sequential budget.

    So terminal recovery gets its own sequential slot instead: main()
    calls this AFTER poll_once() returns, every cycle, with a fresh
    deadline of TERMINAL_RECOVERY_PASS_BUDGET_SECONDS computed from its
    own start time -- nothing that happened inside the poll (however
    overrun) can consume it. Ordering after the poll preserves the
    active-work-first contract: a normal tick's active commands are all
    processed before this pass spends a single second.

    Bounded and fail-closed the same way as everything else here:
    - Hydration (project listing + bounded command enumeration, expected
      on the short-timeout `discovery_store`) stops at
      deadline - TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS, keeping full
      classification headroom by construction.
    - _enumerate_commands()'s per-(tick, project) hashed rotate offset
      means the small per-cycle hydration window lands at effectively
      independent positions each cycle, so a candidate anywhere in a
      large history is reached in a bounded expected number of cycles --
      never permanently excluded.
    - Classification lookups go through `classification_store` (short
      dedicated transport) under _terminal_recovery_candidates()'s
      unchanged headroom guard and lookup budget.
    - Eligible candidates run through the exact same
      process_command(terminal_recovery_prevalidation=...) recovery-only
      path as the in-poll pass: recovery is idempotent and self-limiting
      (a recovered candidate stops being eligible), and a terminal
      command can never relaunch its provider. Per-candidate errors are
      recorded and isolated, never propagated.
    - Deadlines bound what may START; an in-flight call is never
      interrupted (same documented semantics as poll_once()).
    - Project order comes from _rotated_project_ids() (wall-clock
      round-robin), NOT the Phase-1 cursor -- this pass must never
      advance or contend with poll_once()'s own durable cursor.
    """
    if allowlist is None:
        allowlist = load_allowlist()
    if deadline is None:
        deadline = time.monotonic() + TERMINAL_RECOVERY_PASS_BUDGET_SECONDS
    if discovery_store is None:
        discovery_store = store
    if classification_store is None:
        classification_store = store
    results = []
    # Classification keeps its full short-transport headroom; what is
    # left in front of it is the hydration slice, sized (see
    # TERMINAL_RECOVERY_PASS_HYDRATION_SECONDS) so that the production
    # list_records_bounded() guard -- never start a record hydration
    # unless a full WATCHER_DISCOVERY_TIMEOUT_SECONDS worst case still
    # fits -- leaves a genuinely usable start window rather than
    # refusing every record outright.
    hydration_deadline = deadline - TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS
    if time.monotonic() >= hydration_deadline:
        return results
    try:
        raw_project_ids = _enumerate_project_ids(discovery_store, deadline=hydration_deadline)
    except TaskError:
        return results
    if not raw_project_ids:
        return results
    project_ids = _rotated_project_ids(raw_project_ids)
    batches = {}
    for project_id in project_ids:
        if time.monotonic() >= hydration_deadline:
            break
        try:
            commands = _enumerate_commands(discovery_store, project_id, deadline=hydration_deadline)
        except TaskError:
            continue
        if commands:
            batches[project_id] = commands
    lookup_budget = MAX_COMMANDS_PER_POLL
    for project_id in project_ids:
        if lookup_budget <= 0 or len(results) >= MAX_COMMANDS_PER_POLL or time.monotonic() >= deadline:
            break
        commands = batches.get(project_id)
        if not commands:
            continue
        candidates, lookup_budget = _terminal_recovery_candidates(
            commands, classification_store, deadline, lookup_budget)
        for command, prevalidation in candidates:
            if len(results) >= MAX_COMMANDS_PER_POLL or time.monotonic() >= deadline:
                break
            try:
                results.append(process_command(store, service, command, allowlist=allowlist,
                                               origin_context=origin_context, async_launch=async_launch,
                                               terminal_recovery_prevalidation=prevalidation, **factories))
            except Exception as exc:
                # Same per-record blast-radius rule as poll_once()'s _run().
                results.append({"status": "error", "project_id": project_id,
                                "command_id": command["command_id"], "error": repr(exc)})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll Drive commands and run Codex through ADM execution_runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=POLL_SECONDS)
    args = parser.parse_args(argv)
    if not 10 <= args.interval_seconds <= MAX_POLL_SECONDS:
        raise SystemExit("interval-seconds must be from 10 to 900")
    try:
        require_runtime_guard()
    except RuntimeGuardError as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code}, separators=(",", ":")))
        return 1
    # Resolve the home once, up front, and fail closed rather than
    # defaulting to "." (the working directory) as this call site did
    # before the 2026-09-02 checkout-contamination outage.
    try:
        manager_home = resolve_manager_home()
    except ManagerHomeError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc).split(":", 1)[0]},
                          separators=(",", ":")))
        return 1
    from manager.scheduler_provenance import finish, start
    invocation = start(manager_home, "command_watcher")
    while True:
        status = "completed"
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
            recent_service = build_service(timeout=RECENT_COMMAND_DISCOVERY_TIMEOUT_SECONDS)
            recent_store = DriveRecords(recent_service)
            # Residual-P0-A fix: a THIRD separate, short-timeout service+store
            # dedicated to terminal-recovery Execution lookups
            # (poll_once()'s Phase 2c / _terminal_recovery_candidates()) --
            # the same reasoning as discovery_store/recent_store above
            # applies: a real transport timeout is a property of the
            # *service* a DriveRecords was built from, so this is the only
            # way to make a single classification lookup's own worst case
            # genuinely bounded to TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS
            # instead of the shared ~45s default every write/active-
            # lifecycle call on `store` still correctly uses.
            classification_service = build_service(timeout=TERMINAL_CLASSIFICATION_TIMEOUT_SECONDS)
            classification_store = DriveRecords(classification_service)
            ingress = []
            # embedded_ingress_enabled() gates this independent of the
            # folder-id env var below -- see its docstring for the
            # dedicated-Scheduled-Task migration contract this enforces.
            if embedded_ingress_enabled() and os.environ.get("ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID"):
                from manager.drive_dispatch_ingress import poll_drive_dispatch_requests
                ingress = poll_drive_dispatch_requests(store, service, os.environ.get("ADM_LOCK_GCS_BUCKET"))
            result = poll_once(store, service, discovery_store=discovery_store, recent_store=recent_store,
                               classification_store=classification_store,
                               origin_context=invocation,
                               async_launch=True)
            # Independent terminal-recovery slot -- see
            # terminal_recovery_once()'s docstring: runs with its OWN
            # fresh bounded deadline after the poll, so no schedule of
            # slow active work inside poll_once() can ever starve
            # terminal recovery permanently.
            recovery = terminal_recovery_once(store, service, discovery_store=discovery_store,
                                              classification_store=classification_store,
                                              origin_context=invocation, async_launch=True)
            print(json.dumps({"status": "ok", "host": socket.gethostname()[:100], "ingress": ingress,
                              "commands": result, "terminal_recovery": recovery}, separators=(",", ":")))
        except Exception:
            status = "failed"
            print(json.dumps({"status": "unavailable"}, separators=(",", ":")))
        if args.once:
            finish(manager_home, invocation, status)
            try_check_and_recover(manager_home)
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    from manager.win_background_guard import install_hidden_subprocess_guard
    install_hidden_subprocess_guard()
    raise SystemExit(main())
