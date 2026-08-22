"""Minimal reserved-to-terminal execution runner.

launch_task()/run_execution() are provider-parameterized (default "codex" for
backward compatibility); main()'s CLI entry point remains Codex-only.
"""

import argparse
import json
import os
import socket
import sys
import uuid
from pathlib import Path

from collectors.publish_drive import build_service
from manager.claude_account_selector import AccountSelectionError, resolve_claude_account
from manager.claude_config_locks import acquire_claude_config_lock, release_claude_config_lock
from manager.codex_launcher import CodexLaunchError, CodexLauncher, LaunchRequest
from manager.dispatcher import dispatch
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import MAX_HARD_TIMEOUT_SECONDS, heartbeat_execution, hard_timeout_seconds, link_execution_session, reserve_execution
from manager.gcs_lock_registry import GCSLockRegistry
from manager.project_registry import (
    ProjectRegistryError,
    get_global_registry,
    resolve_authoritative_working_directory_with_project,
)
from manager.quota_forecast import DEFAULT_MAX_AGE_MINUTES
from manager.quota_reader import read_drive_status
from manager.repo_write_completion import complete_repo_write_execution
from manager.repo_write_enforcement import enforce_allowed_paths, is_bounded_repo_write_snapshot
from manager.session_identity import manager_session_key
from manager.task_claims import task_claim_registry
from manager.tasks import DriveRecords, TaskError, update_task, validate
from manager.trusted_ingress import repo_write_policy_satisfied
from manager.worktree_locks import link_session as link_writer_session
from manager.worktree_materializer import materialize_worktree, verify_checkout_repo_identity


RPC_TIMEOUT_SECONDS = 30.0
MAX_TURN_TIMEOUT_SECONDS = float(MAX_HARD_TIMEOUT_SECONDS)


def _repo_write_validation_checks(project_id):
    """Global Hands-off Execution Layer, Slice D2 (P0-B fix): the required
    validation checks a bounded repo-write execution must pass before its
    real changes are staged/committed/pushed are resolved exclusively from
    this project's own authoritative registry entry
    (`ProjectMetadata.required_validation_checks()`, backed by
    PROJECT-RULES/governance references) -- never from an environment
    variable, a provider's own claim, or any other caller-suppliable input.
    A project with no registered validation_policy fails closed (raises
    ValidationPolicyMissingError, a ProjectRegistryError, before any tests
    run) rather than guessing a generic default command; this is a
    deliberate removal of the prior ADM_REPO_WRITE_TESTS_COMMAND override,
    which let anything in the process environment weaken the policy."""
    return get_global_registry().get_project(project_id).required_validation_checks()


def task_turn_timeout(expected_minutes, override=None):
    if override is not None:
        if isinstance(override, bool) or not isinstance(override, (int, float)) or not 0 < override <= MAX_TURN_TIMEOUT_SECONDS:
            raise TaskError(f"timeout_seconds must be within (0, {MAX_TURN_TIMEOUT_SECONDS:g}]")
        return float(override)
    return hard_timeout_seconds(expected_minutes)


def _provider_session_id(prepared):
    # CodexLauncher's PreparedLaunch names this field thread_id; ClaudeLauncher's
    # names it provider_session_id (its provider-native session id is known
    # before spawn, not assigned by the provider afterward). Both adapters are
    # accepted here via duck typing rather than forcing one launcher's naming
    # onto the other's PreparedLaunch shape.
    value = getattr(prepared, "provider_session_id", None)
    if value is None:
        value = getattr(prepared, "thread_id", None)
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 500 or any(ord(char) < 32 for char in value):
        raise TaskError("prepared provider session id is invalid")
    return value


def _session(execution, prepared, request, provider):
    provider_session_id = _provider_session_id(prepared)
    session_id = manager_session_key(provider, provider_session_id)
    # account_id is duck-typed like provider_session_id above: only
    # ClaudeLauncher's PreparedLaunch carries it today (CodexLauncher's does
    # not), and getattr(..., None) makes both single-account Claude and all
    # Codex records identical to before this field existed. It is evidence
    # attribution only -- provider_session_id (a UUID ADM assigns itself)
    # already guarantees no two sessions collide, with or without account_id.
    account_id = getattr(prepared, "account_id", None)
    # session_path is adapter-validated advisory evidence; identity is provider_session_id.
    return {
        "session_id": session_id, "provider": provider, "provider_session_id": provider_session_id,
        "account_id": account_id,
        "project_id": execution["project_id"], "task_id": execution["task_id"],
        "conversation_label": None, "title": None, "summary": None,
        "started_at": prepared.prepared_at, "updated_at": prepared.prepared_at,
        "working_directory": request.working_directory, "repository": None,
        "source_identifier": provider_session_id, "source_path": prepared.session_path,
        "classification_method": "working_directory", "classification_confidence": "high",
        "classification_status": "classified", "status": "active", "message_count": 0,
        "model": request.model, "first_user_prompt": None,
    }


def _persist_session_link(store, writer_registry, execution, prepared, request, lease_token, provider):
    session = _session(execution, prepared, request, provider)
    validate("session", session)
    try:
        existing = store.get("sessions", execution["project_id"], session["session_id"])
    except KeyError:
        existing = None
    except TaskError as exc:
        if "found 0" not in str(exc) and "not found" not in str(exc):
            raise
        existing = None
    if existing is None:
        store.put("sessions", execution["project_id"], session["session_id"], session)
    elif existing != session:
        raise TaskError("canonical provider session conflicts with persisted content")
    if store.get("sessions", execution["project_id"], session["session_id"]) != session:
        raise TaskError("canonical provider session persistence verification failed")

    link_execution_session(store, execution["project_id"], execution["execution_id"], session)
    linked = store.get("executions", execution["project_id"], execution["execution_id"])
    if linked.get("session_id") != session["session_id"] or linked.get("provider_session_id") != _provider_session_id(prepared):
        raise TaskError("execution session link persistence verification failed")
    if execution.get("access") == "production_write":
        lease = link_writer_session(
            writer_registry, execution["lease_evidence"]["lock_id"], execution["project_id"],
            execution["task_id"], execution["execution_id"], execution["provider"],
            session["session_id"], lease_token,
        )
        if lease.get("session_id") != session["session_id"] or lease.get("effective_status") != "active":
            raise TaskError("writer lease session link verification failed")
    return session


def _terminalize_session(store, session, terminal):
    timestamp = terminal["execution"]["completed_at"]
    expected = {**session, "status": "completed" if terminal["execution"]["status"] == "completed" else "unknown",
                "updated_at": timestamp}
    validate("session", expected)
    store.put("sessions", expected["project_id"], expected["session_id"], expected)
    if store.get("sessions", expected["project_id"], expected["session_id"]) != expected:
        raise TaskError("terminal session persistence verification failed")
    return expected


def _stopped(prepared):
    # CodexLauncher's PreparedLaunch keeps the raw subprocess behind
    # _client.process (the app-server client wraps it); ClaudeLauncher's
    # PreparedLaunch holds it directly as _process (no app-server client
    # exists for Claude's single-subprocess stream-json protocol). Duck-type
    # across both shapes the same way _provider_session_id() already does
    # for provider_session_id/thread_id, rather than assuming Codex's shape.
    process = getattr(getattr(prepared, "_client", None), "process", None)
    if process is None:
        process = getattr(prepared, "_process", None)
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            wait(timeout=5)
        except Exception:
            pass
    poll = getattr(process, "poll", None)
    return callable(poll) and poll() is not None


def _failure_summary(error, provider):
    classification = getattr(error, "classification", None) or type(error).__name__
    return f"{provider} runner interrupted: {str(classification)[:200]}"


def _dispatch_request(task, provider, account_id=None):
    return {
        "project_id": task["project_id"], "task_id": task["task_id"], "title": task["title"],
        "task_type": task["task_type"], "complexity": task["complexity"],
        "expected_minutes": task["expected_minutes"], "scope": task.get("scope", []),
        "constraints": task.get("constraints", []), "acceptance_criteria": task.get("acceptance_criteria", []),
        "needs_repo_edit": task.get("needs_repo_edit", True),
        "needs_research": task.get("needs_research", False), "needs_browser": task.get("needs_browser", False),
        # The watcher is replaying a queued Command's provider authority, not
        # accepting a fresh caller override. Its resolved Claude account is
        # therefore never reclassified as caller-explicit at this stage.
        "preferred_provider": provider, "provider_is_assigned": True, "account_id": account_id,
    }


def _materialize_repo_write_working_directory(store, task):
    """Slice C of the Global Hands-off Execution Layer: a Task that actually
    carries Slice A's v2-repo-write admission evidence (bounded allowed_paths/
    baseline_head/repo, not merely the common needs_repo_edit=True default
    every ordinary production task also carries) must launch inside its own
    deterministic, isolated git worktree -- never the project's single shared
    canonical checkout, which every other task (read-only or otherwise) also
    reads. Resolution
    goes through the Global Project Registry (manager.project_registry,
    Slice B) rather than any hardcoded machine path; any failure here (wrong
    repo, invalid/mismatched baseline, ownership conflict, ...) propagates
    and must never fall through to the canonical-checkout fallback below.
    """
    project_id = task["project_id"]
    project = get_global_registry().get_project(project_id)
    drive_project = store.get("projects", project_id, project_id)
    canonical_checkout = drive_project.get("working_directory")
    if not isinstance(canonical_checkout, str) or not canonical_checkout.strip():
        raise TaskError(f"project {project_id!r} has no canonical working_directory to materialize a worktree from")
    workspace_root = os.environ.get("ADM_WORKTREE_WORKSPACE_ROOT") or str(Path(canonical_checkout).parent / "adm-worktrees")
    result = materialize_worktree(store, project, task, canonical_checkout, workspace_root)
    return result["working_directory"]


def _resolve_working_directory(store, task):
    """Resolve the launch-time working_directory, backfilling a legacy Task.

    manager.dispatcher.dispatch() normally runs in Cloud Run, which has no
    HOME-local ADM_WORKSPACE_ROOT, so for a project registered in the
    Global Project Registry it deliberately snapshots working_directory as
    None rather than the Drive Project record's raw `working_directory`
    literal (see
    fix/direct-dispatch-working-directory-authority-p0-20260822 R2) --
    machine-local authority belongs on the actual execution host, which is
    here. This is that re-resolution: never any caller-supplied value
    (there is none available here), and never the Drive literal taken at
    face value, since nothing keeps it synchronized with the actual current
    checkout -- except for a Task carrying genuine v2-repo-write admission
    evidence, which is never allowed to fall back to the shared canonical
    checkout and instead materializes (or reuses) its own isolated worktree
    (Slice C). A project NOT registered in the Global Project Registry at
    all still gets the legacy Drive-literal fallback, for backward
    compatibility. The resolved value is validated (string, absolute,
    existing directory, and -- when it came from the registry -- a real
    git checkout of the project's own registered repo, never a stale or
    mis-pointed workspace) before it is trusted, and only *then* backfilled
    onto the Task record itself, so a bad value is never persisted, and
    every later launch/retry of this same Task reuses this Task's own
    snapshot instead of re-resolving it again.
    """
    value = task.get("working_directory")
    from_project = False
    registry_project = None
    if value is None and repo_write_policy_satisfied(task):
        value = _materialize_repo_write_working_directory(store, task)
    if value is None:
        project = store.get("projects", task["project_id"], task["project_id"])
        value, registry_project = resolve_authoritative_working_directory_with_project(
            task["project_id"], project.get("working_directory")
        )
        from_project = True
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"no working_directory is configured for task {task['task_id']!r} or its project")
    if not os.path.isabs(value):
        raise TaskError(f"working_directory must be an absolute path: {value!r}")
    if not os.path.isdir(value):
        raise TaskError(f"working_directory does not exist or is not a directory: {value!r}")
    if registry_project is not None:
        verify_checkout_repo_identity(value, registry_project)
    if from_project:
        update_task(store, task["project_id"], task["task_id"], working_directory=value)
    return value


def _claude_account_auth_ready(account):
    """P0 auth-routing-truth check: proves one Claude account is actually
    launchable right now (a real `claude auth status --json` preflight
    against its exact config_dir), for use as automatic account-selection
    eligibility rather than as the launcher's own post-selection gate.

    Fails closed on every ambiguous outcome, not just a confirmed
    not-logged-in: any exception from resolving the CLI executable or from
    check_claude_auth_ready (including ClaudeLaunchError's own
    "authentication_check_failed" classification for a timeout/unparseable
    result) is treated as not-ready, same as a confirmed
    "authentication_unavailable" -- an account this uncertain about must
    never win automatic selection over one that checked out clean.
    """
    from manager.claude_launcher import check_claude_auth_ready, resolve_claude_executable
    env = None
    config_dir = account.get("config_dir")
    if config_dir is not None:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        executable = resolve_claude_executable()
        return check_claude_auth_ready(executable, env)
    except Exception:
        return False


def launch_task(store, service, writer_registry, claim_registry, launcher, project_id, task_id,
                execution_id=None, model=None, timeout_seconds=None, quota_document=None, executions=None,
                retry_count=0, retry_of_execution_id=None, on_running=None, provider="codex",
                account_id=None, config_dir=None, claude_accounts=None):
    """Dispatch, reserve, and run one ready task; callers supply real authorities.

    `provider` names which provider this launcher belongs to (the caller
    already chose the launcher/quota gate for it); dispatch() is forced to
    that exact provider via preferred_provider, and every downstream record
    (Execution, task claim, Session) is written with this same provider
    string rather than a hardcoded one, so evidence never disagrees with the
    launcher actually used.

    `account_id`/`config_dir` are additive and default to None (today's
    single-account behavior, unchanged); when supplied directly (and
    `claude_accounts` is not given) they are passed through to
    `run_execution()` -> `launcher.prepare()` only for launchers that accept
    them (ClaudeLauncher), never forced onto CodexLauncher.

    `claude_accounts` is the additive routing path: a loaded account
    registry (`manager.claude_account_selector.load_claude_accounts()`).
    When given for provider="claude", it is authoritative -- it (re)resolves
    account_id/config_dir via `resolve_claude_account()`, validating any
    explicitly-supplied `account_id` against the registry+quota (fail-closed
    on unknown/disabled/ambiguous/all-stale, exactly like
    `select_claude_account()`) rather than trusting it blindly. It is a
    no-op for any other provider and when omitted (default None), so every
    existing single-account/Codex caller is unaffected.

    A raw/direct caller that supplies an explicit `account_id` for
    provider="claude" but omits `claude_accounts` has no registry to
    validate that id against -- there would be no way to prove it names a
    real, enabled account, or to resolve its correct config_dir. Rather than
    silently trusting the bare id through to `run_execution()` (where a
    missing/unresolvable config_dir means the child process falls back to
    whatever Claude config is ambient/default on this machine), this fails
    closed with `AccountSelectionError` before dispatch/reservation/spawn.
    The pre-P0.1.5 single-account default path -- provider="claude" with
    neither `account_id` nor `claude_accounts` supplied -- is unaffected.
    """
    task = store.get("tasks", project_id, task_id)
    validate("task", task)
    working_directory = _resolve_working_directory(store, task)
    turn_timeout = task_turn_timeout(task["expected_minutes"], timeout_seconds)
    if provider == "claude" and claude_accounts is not None:
        quota_document = quota_document or read_drive_status(service=service)
        # max_age_seconds uses the same canonical quota-freshness window every
        # other quota-reliability gate in this codebase already enforces
        # (quota_reader.summarize()'s default, quota_forecast's
        # DEFAULT_MAX_AGE_MINUTES) -- a frozen "last-known-good" quota
        # confidence must not count as reliable forever just because nothing
        # has recomputed it since collection started failing.
        resolved = resolve_claude_account(claude_accounts, quota_document, explicit_account_id=account_id,
                                          max_age_seconds=DEFAULT_MAX_AGE_MINUTES * 60,
                                          check_auth_ready=_claude_account_auth_ready if account_id is None else None)
        account_id, config_dir = resolved["account_id"], resolved["config_dir"]
    elif provider == "claude" and account_id is not None:
        raise AccountSelectionError(
            f"explicit Claude account_id {account_id!r} was supplied without an account registry "
            "(claude_accounts=None); refusing to launch against an unvalidated account instead of "
            "falling back to ambient/default Claude config"
        )
    dispatched = dispatch(store, service, _dispatch_request(task, provider, account_id), quota_document, executions)
    if dispatched["recommended_provider"] != provider:
        raise TaskError(f"dispatch did not select {provider}")
    execution_id = execution_id or f"{task_id}-{uuid.uuid4().hex[:12]}"
    reserve_execution(store, project_id, task_id, execution_id, provider, dispatched["quota_evidence"],
                      dispatched["mode"], dispatched["effort"], retry_count=retry_count,
                      retry_of_execution_id=retry_of_execution_id)
    if provider == "antigravity":
        from manager.ag_runner import LaunchRequest as _AgLaunchRequest
        request = _AgLaunchRequest(working_directory, project_id=project_id, model=model,
                                   reasoning_effort=dispatched["effort"],
                                   sandbox="read-only" if task["read_only"] else None,
                                   approval_policy="never" if task["read_only"] else None,
                                   timeout_seconds=RPC_TIMEOUT_SECONDS, turn_timeout_seconds=turn_timeout)
    else:
        request = LaunchRequest(working_directory, model=model, reasoning_effort=dispatched["effort"],
                                sandbox="read-only" if task["read_only"] else None,
                                approval_policy="never" if task["read_only"] else None,
                                timeout_seconds=RPC_TIMEOUT_SECONDS, turn_timeout_seconds=turn_timeout)
    result = run_execution(store, service, writer_registry, claim_registry, launcher, project_id, task_id,
                           execution_id, dispatched["generated_prompt"], request,
                           access="read_only" if task["read_only"] else "production_write",
                           baseline_head=task.get("baseline_head"), on_running=on_running, provider=provider,
                           account_id=account_id, config_dir=config_dir)
    return {"execution_id": execution_id, "dispatch": dispatched, **result}


def run_execution(store, service, writer_registry, claim_registry, launcher,
                  project_id, task_id, execution_id, prompt, launch_request: LaunchRequest,
                  access="production_write", baseline_head=None, on_running=None, provider="codex",
                  account_id=None, config_dir=None):
    """Run one reserved execution through the reviewed lifecycle gates.

    ``provider_stopped`` is derived only after prepare-owned cleanup or after
    ``close()`` plus process-exit evidence; it is never assumed by the caller.
    Full prompt, transcript, stderr, and raw provider failure text are not
    persisted by this runner.

    ``account_id``/``config_dir`` are only forwarded to ``launcher.prepare()``
    when at least one is supplied, and only as extra keyword arguments -- this
    keeps every existing CodexLauncher call (whose ``prepare(request)`` takes
    no such arguments) byte-for-byte identical to before these parameters
    existed.
    """
    gate = enter_running_gate(
        store, service, writer_registry, project_id, task_id, execution_id, provider, access,
        baseline_head=baseline_head, task_claim_registry=claim_registry,
        hard_timeout=launch_request.turn_timeout_seconds,
    )
    execution = gate["execution"]
    lease_token = gate["lease"]["lease_token"] if gate["lease"] else None
    prepared = running = outcome = session = config_lock = None
    operation_error = close_error = None
    status, summary = "interrupted", f"{provider} runner interrupted"
    try:
        if on_running:
            on_running(execution)
        if launch_request.working_directory != execution["task_snapshot"].get("working_directory"):
            raise TaskError("launch working directory does not match the reserved execution")
        prepare_kwargs = {}
        if account_id is not None:
            prepare_kwargs["account_id"] = account_id
        if config_dir is not None:
            prepare_kwargs["config_dir"] = config_dir
        if provider == "claude":
            # Acquired here (after account/config_dir are resolved, immediately
            # before the child Claude process is spawned) so ADM never runs two
            # of its own Claude launches against the same on-disk config
            # directory at once -- see manager.claude_config_locks for why this
            # is a local, not cross-machine, resource, and why a busy directory
            # fails closed (CLAUDE_CONFIG_BUSY) instead of queueing or silently
            # falling back to a different account.
            config_lock = acquire_claude_config_lock(
                config_dir, account_id=account_id, project_id=project_id,
                task_id=task_id, execution_id=execution_id,
            )
        prepared = launcher.prepare(launch_request, **prepare_kwargs)
        provider_evidence = {
            "host": socket.gethostname()[:100], "pid": prepared.pid,
            "creation_identity": prepared.process_creation_identity,
            "started_at": prepared.prepared_at,
        }
        heartbeat_execution(store, project_id, execution_id, "provider_prepared",
                            at=prepared.prepared_at, provider_evidence=provider_evidence)
        session = _persist_session_link(store, writer_registry, execution, prepared, launch_request, lease_token, provider)
        running = launcher.start(prepared, prompt)
        heartbeat_execution(store, project_id, execution_id, "turn_started", at=running.started_at)
        set_heartbeat = getattr(launcher, "set_heartbeat", None)
        if callable(set_heartbeat):
            set_heartbeat(running, lambda event: heartbeat_execution(
                store, project_id, execution_id, event, provider_evidence=provider_evidence,
                progress=event == "provider_event",
            ))
        outcome = launcher.wait(running)
        heartbeat_execution(store, project_id, execution_id, "turn_terminal", at=outcome.completed_at)
        status = outcome.status if outcome.status in ("completed", "failed", "interrupted") else "failed"
        summary = f"{provider} turn {status}"
        if outcome.failure_classification:
            summary += f": {outcome.failure_classification[:200]}"
    except (Exception, KeyboardInterrupt) as exc:
        operation_error = exc
        summary = _failure_summary(exc, provider)
    finally:
        if prepared is not None:
            try:
                launcher.close(running or prepared)
            except (Exception, KeyboardInterrupt) as exc:
                close_error = exc
        if config_lock is not None:
            # Best-effort, ABA-safe release; never raises, so it can never
            # mask operation_error/close_error above (see
            # release_claude_config_lock's own docstring).
            release_claude_config_lock(config_lock)

    # prepare() owns and closes a spawned process until it returns a handle.
    provider_stopped = prepared is None and operation_error is not None
    if prepared is not None:
        provider_stopped = _stopped(prepared)
    if not provider_stopped:
        error = TaskError(f"{provider} provider stop could not be proven; terminal authority retained")
        if operation_error:
            error.add_note(f"runner: {operation_error}")
        if close_error:
            error.add_note(f"close: {close_error}")
        raise error

    # Global Hands-off Execution Layer, Slice D: a v2-repo-write Task's
    # provider only ever ran inside its own isolated worktree (Slice C), but
    # nothing yet checked that its *actual* git changes stayed within the
    # Task's admitted allowed_paths -- so a provider-reported "completed"
    # outcome could otherwise persist even if it edited files outside its
    # bounded scope. This runs only once the provider has fully, provably
    # stopped (never while it could still be writing), only when it claimed
    # success, and only for a genuine repo-write Task (never legacy/
    # read-only, whose working_directory may still be the shared canonical
    # checkout this check must never be pointed at). Any failure here --
    # a real scope violation or an unexpected git error -- downgrades status
    # before terminalize_execution() ever persists "completed", so a
    # violation can never reach a successful completion or any future
    # commit/push step gated on that status.
    # Global Hands-off Execution Layer, Slice D2: once Slice D has proven the
    # provider's real changes stayed within its admitted allowed_paths, a
    # bounded repo-write execution still isn't durably "done" -- nothing yet
    # ran the project's tests gate, committed, or pushed those changes. This
    # runs the full terminal-completion sequence (tests -> exact stage ->
    # deterministic commit -> push the execution's own isolated task branch
    # -> remote SHA readback) against real git/subprocess state; any failure
    # downgrades status before terminalize_execution() ever persists
    # "completed", exactly like Slice D's own scope check above it.
    snapshot = execution["task_snapshot"]
    repo_write_completion_evidence = None
    if status == "completed" and is_bounded_repo_write_snapshot(snapshot):
        try:
            changed_paths = enforce_allowed_paths(snapshot["working_directory"], snapshot["baseline_head"], snapshot["allowed_paths"])
            repo_write_completion_evidence = complete_repo_write_execution(
                working_directory=snapshot["working_directory"], changed_paths=changed_paths,
                baseline_head=snapshot["baseline_head"], branch=execution["lease_evidence"]["branch"],
                repository=execution["lease_evidence"]["repository"],
                validation_checks=_repo_write_validation_checks(project_id),
                task_id=task_id, execution_id=execution_id,
            )
        except (TaskError, ProjectRegistryError) as exc:
            status = "failed"
            summary = f"{provider} turn rejected: {exc}"

    terminal = terminalize_execution(
        store, service, writer_registry, claim_registry, project_id, task_id, execution_id, provider,
        status, gate["task_claim"]["generation"], provider_stopped, lease_token=lease_token,
        completed_at=outcome.completed_at if outcome else None, summary=summary,
        repo_write_completion_evidence=repo_write_completion_evidence,
    )
    if session is not None:
        session = _terminalize_session(store, session, terminal)
    if operation_error:
        operation_error.add_note("execution terminalized as interrupted after provider stop was proven")
        raise operation_error
    if close_error:
        close_error.add_note(f"provider outcome remained {status}")
        raise close_error
    return {"terminal": terminal, "provider_outcome": outcome, "session": session}


def _safe_error(exc):
    classification = getattr(exc, "classification", None) or type(exc).__name__
    return {"kind": str(classification)[:100], "message": "execution failed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dispatch, reserve, and run one Codex task")
    parser.add_argument("project_id"); parser.add_argument("task_id")
    parser.add_argument("--execution-id"); parser.add_argument("--model")
    parser.add_argument("--timeout-seconds", type=float, help="Override bounded turn-completion timeout")
    args = parser.parse_args(argv)
    execution_id = args.execution_id or f"{args.task_id}-{uuid.uuid4().hex[:12]}"
    store = None
    try:
        service = build_service(); store = DriveRecords(service)
        task = store.get("tasks", args.project_id, args.task_id); validate("task", task)
        claim_registry = task_claim_registry(os.environ.get("ADM_LOCK_GCS_BUCKET"), args.project_id, args.task_id)
        writer_registry = None if task["read_only"] else GCSLockRegistry.from_environment()
        result = launch_task(store, service, writer_registry, claim_registry, CodexLauncher(), args.project_id,
                             args.task_id, execution_id, args.model, args.timeout_seconds,
                             quota_document=read_drive_status(service=service))
        execution_id = result["execution_id"]
        output = {"status": result["terminal"]["execution"]["status"], "execution_id": execution_id,
                  "session_id": result["session"]["session_id"], "cleanup": result["terminal"]["cleanup"]}
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0 if output["status"] == "completed" else 1
    except (KeyboardInterrupt, CodexLaunchError, TaskError, OSError, ValueError) as exc:
        terminal = None
        if store is not None:
            try:
                candidate = store.get("executions", args.project_id, execution_id)
                if candidate.get("status") in ("completed", "failed", "interrupted"):
                    terminal = candidate
            except (TaskError, KeyError):
                pass
        print(json.dumps({"status": terminal["status"] if terminal else "error", "execution_id": execution_id,
                          "session_id": terminal.get("session_id") if terminal else None,
                          "error": _safe_error(exc)}, ensure_ascii=False, separators=(",", ":")), file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
