import secrets
"""Authoritative reservation-to-running lifecycle gate."""

from datetime import timedelta

from manager import task_root
from manager.executions import MAX_HARD_TIMEOUT_SECONDS, hard_timeout_seconds, parse_time, persist_terminal, quota_snapshot, task_snapshot
from manager.quota_reader import read_drive_status
from manager.task_claims import TaskClaimConflict
from manager.tasks import TaskError, create_handoff, now_iso, validate
from manager.worktree_locks import active, acquire, canonical_baseline, canonical_branch, canonical_repository, canonical_scope, owner_fields, read_registry, release, repository_lock_id, same_owner, validate_local_preflight


_PERSISTENCE_RANK = {"incomplete": 0, "partial": 1, "complete": 2}
_PERSISTED_ORDER = ["execution", "handoff", "task"]
_WRITER_RELEASE_STICKY = ("released", "not_required")


def _sticky(existing_value, new_value, sticky_values):
    """Once either side already shows a value in `sticky_values`, that value
    wins and can never be pulled back to a non-sticky one -- order of the two
    calls this merge is folded across must not matter (commutative)."""
    if existing_value in sticky_values:
        return existing_value
    if new_value in sticky_values:
        return new_value
    return new_value if new_value is not None else existing_value


def _write_once(existing_value, new_value, field_name):
    """`field_name` may be recorded once; a second write is only ever
    valid if it repeats the same value. Two different non-empty values is a
    genuine conflict between two observations of what should be the single
    same fact -- fail closed rather than silently pick one (the caller is
    responsible for surfacing this as a hard error, not for guessing)."""
    if existing_value is None:
        return new_value
    if new_value is None or new_value == existing_value:
        return existing_value
    raise TaskError(f"cleanup evidence conflict on write-once field '{field_name}': "
                    f"{existing_value!r} != {new_value!r}")


def merge_cleanup_evidence(existing, updates):
    """The single shared lattice merge for every terminal-lifecycle write to
    Execution.cleanup_evidence -- distinct from (and orthogonal to) the
    Task Root's own materialization/cleanup facets in manager.task_root,
    which track runtime claim/writer authority rather than this record's
    own persistence/provider-outcome audit trail.

    Never a wholesale overwrite: always fold `updates` onto `existing`
    field-by-field under a fixed per-field law, so the result is idempotent
    (merge(x, x) == x), commutative (merge(a, b) == merge(b, a)), associative
    (merge(merge(a, b), c) == merge(a, merge(b, c))), and monotonic (no field
    ever regresses to a "less converged" value once it has advanced):

      persistence          -- incomplete < partial < complete; keeps the max
      persisted             -- set union, rendered in canonical order
      task_claim_release    -- "released" is sticky
      writer_release        -- "released"/"not_required" are sticky
      provider_outcome      -- write-once; conflicting non-empty values FAIL CLOSED
      errors                -- append-only + dedup (historical audit trail)
      terminal_at           -- write-once; an existing value is never re-stamped
      session_id            -- enrichment-only; conflicting non-empty values FAIL CLOSED
      error_kind            -- enrichment-only; conflicting non-empty values FAIL CLOSED

    Any other key present on either side passes through (`updates` wins when
    both sides set it). Both inputs are read-only; a fresh dict is always
    returned. `existing` should be the just-re-read current value from the
    store (or None), not a snapshot captured before other work happened, so
    a concurrent writer's progress is never clobbered.
    """
    existing = dict(existing or {})
    updates = dict(updates or {})
    merged = {}

    if "persistence" in existing or "persistence" in updates:
        existing_rank = _PERSISTENCE_RANK.get(existing.get("persistence"), -1)
        updates_rank = _PERSISTENCE_RANK.get(updates.get("persistence"), -1)
        merged["persistence"] = existing.get("persistence") if existing_rank >= updates_rank else updates.get("persistence")

    if "persisted" in existing or "persisted" in updates:
        union = set(existing.get("persisted") or []) | set(updates.get("persisted") or [])
        merged["persisted"] = [item for item in _PERSISTED_ORDER if item in union] + sorted(union - set(_PERSISTED_ORDER))

    if "task_claim_release" in existing or "task_claim_release" in updates:
        merged["task_claim_release"] = _sticky(existing.get("task_claim_release"), updates.get("task_claim_release"), {"released"})

    if "writer_release" in existing or "writer_release" in updates:
        merged["writer_release"] = _sticky(existing.get("writer_release"), updates.get("writer_release"), _WRITER_RELEASE_STICKY)

    if "provider_outcome" in existing or "provider_outcome" in updates:
        merged["provider_outcome"] = _write_once(existing.get("provider_outcome"), updates.get("provider_outcome"), "provider_outcome")

    if "errors" in existing or "errors" in updates:
        merged_errors = list(existing.get("errors") or [])
        for error in updates.get("errors") or []:
            if error not in merged_errors:
                merged_errors.append(error)
        merged["errors"] = merged_errors

    if "terminal_at" in existing or "terminal_at" in updates:
        merged["terminal_at"] = existing.get("terminal_at") or updates.get("terminal_at")

    if "session_id" in existing or "session_id" in updates:
        merged["session_id"] = _write_once(existing.get("session_id"), updates.get("session_id"), "session_id")

    if "error_kind" in existing or "error_kind" in updates:
        merged["error_kind"] = _write_once(existing.get("error_kind"), updates.get("error_kind"), "error_kind")

    handled = {"persistence", "persisted", "task_claim_release", "writer_release",
               "provider_outcome", "errors", "terminal_at", "session_id", "error_kind"}
    for key in (set(existing) | set(updates)) - handled:
        merged[key] = updates[key] if key in updates else existing[key]

    return merged


def _claimed_task(task, execution_id):
    context = dict(task.get("source_context") or {})
    active = context.get("active_execution_id")
    if active and active != execution_id:
        raise TaskClaimConflict(f"task is already claimed by execution {active}")
    context["active_execution_id"] = execution_id
    claimed = {**task, "source_context": context}
    validate("task", claimed)
    return claimed


def _rollback_task_record(store, project_id, task_id, execution_id, original):
    current = store.get("tasks", project_id, task_id)
    if current == original:
        return True
    if (current.get("source_context") or {}).get("active_execution_id") != execution_id:
        # The GCS claim, not this Drive field, is authoritative. Never overwrite
        # evidence that no longer identifies this execution during recovery.
        return True
    store.put("tasks", project_id, task_id, original)
    return store.get("tasks", project_id, task_id) == original


def _rollback_execution(store, project_id, execution_id, original):
    current = store.get("executions", project_id, execution_id)
    if current == original:
        return True
    if current.get("execution_id") != execution_id or current.get("status") != "running":
        return False
    store.put("executions", project_id, execution_id, original)
    return store.get("executions", project_id, execution_id) == original


def enter_running_gate(store, service, registry, project_id, task_id, execution_id, provider, access, baseline_head=None,
                       started_at=None, task_claim_registry=None, hard_timeout=None, claim_token=None):
    """Validate and authorize one reservation; never launches a provider."""
    if hard_timeout is not None and (isinstance(hard_timeout, bool) or not isinstance(hard_timeout, (int, float))
                                     or not 0 < hard_timeout <= MAX_HARD_TIMEOUT_SECONDS):
        raise TaskError("hard execution timeout is invalid")
    try:
        execution = store.get("executions", project_id, execution_id)
    except KeyError as exc:
        raise TaskError("running gate requires an existing reserved execution") from exc
    except TaskError as exc:
        if "found 0" not in str(exc) and "not found" not in str(exc):
            raise
        raise TaskError("running gate requires an existing reserved execution") from exc
    validate("execution", execution)
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(execution.get(key) != value for key, value in identity.items()):
        raise TaskError("requested running identity does not match the reservation")
    if execution["status"] != "reserved":
        raise TaskError("running gate requires an existing reserved execution")
    task = store.get("tasks", project_id, task_id)
    validate("task", task)
    if task["status"] != "ready":
        raise TaskError("running gate requires the task to remain ready")
    snapshot = execution["task_snapshot"]
    if task_snapshot(task) != snapshot:
        raise TaskError("current task contract does not match the reservation snapshot")
    if task_claim_registry is None:
        raise TaskError("authoritative task claim registry is required")

    lease = None
    expected_lock_id = None
    if access == "read_only":
        if snapshot.get("read_only") is not True or snapshot.get("needs_repo_edit") is True:
            raise TaskError("read-only gate requires an explicitly read-only reservation")
        lease_evidence = None
    elif access == "production_write":
        if snapshot.get("read_only") is True:
            raise TaskError("read-only execution cannot upgrade in place; create a production-write reservation")
        project = store.get("projects", project_id, project_id)
        validate("project", project)
        branch = snapshot.get("branch")
        if not isinstance(branch, str) or not branch.startswith("refs/heads/"):
            raise TaskError("production running gate requires a full branch ref")
        repository_request = project["repo"]
        repository = canonical_repository(repository_request)
        expected_lock_id = repository_lock_id(repository)
        branch = canonical_branch(branch)
        scope = canonical_scope(snapshot.get("allowed_paths"))
        reserved_baseline = canonical_baseline(snapshot.get("baseline_head"))
        if baseline_head is not None and canonical_baseline(baseline_head) != reserved_baseline:
            raise TaskError("requested baseline_head does not match the reservation")
        baseline_head = reserved_baseline
        validate_local_preflight(snapshot.get("working_directory"), repository, branch, baseline_head)
        if registry is None:
            raise TaskError("writer lock registry is required")
        acquired = acquire(registry, project_id, task_id, execution_id, provider, repository=repository_request, branch=branch, scope=scope, baseline_head=baseline_head, working_directory=snapshot.get("working_directory"), preflight_func=validate_local_preflight)
        token = acquired.get("lease_token") if isinstance(acquired, dict) else None
        expected_lease = {**identity, "lock_id": expected_lock_id, "repository": repository, "branch": branch, "scope": scope, "baseline_head": baseline_head}
        try:
            if not isinstance(acquired, dict) or acquired.get("authority") != "acquired" or acquired.get("effective_status") != "active" or not isinstance(token, str) or len(token) < 32 or not isinstance(acquired.get("generation"), int) or any(acquired.get(key) != value for key, value in expected_lease.items()):
                raise TaskError("authoritative writer acquire did not return an active owned lease")
        except Exception as exc:
            if isinstance(token, str) and len(token) >= 32:
                try:
                    release(registry, expected_lock_id, project_id, task_id, execution_id, provider, lease_token=token)
                except Exception as cleanup_exc:
                    raise TaskError(f"acquire validation failed and lease cleanup failed: {cleanup_exc}") from exc
            elif isinstance(acquired, dict) and acquired.get("authority") == "acquired":
                raise TaskError("acquire validation failed; cleanup could not be proven without a valid lease token") from exc
            raise
        lease = acquired
        lease_evidence = {
            "authority": "acquired", "lock_id": lease["lock_id"], "generation": lease["generation"],
            "repository": lease["repository"], "branch": lease["branch"], "scope": lease["scope"],
            "baseline_head": lease["baseline_head"],
        }
    else:
        raise TaskError("access must be production_write or read_only")

    claim = None
    claim_time = started_at or now_iso()
    claim_token = claim_token or secrets.token_urlsafe(32)
    try:
        claim = task_root.acquire_task_root(
            task_claim_registry, project_id, task_id, execution_id, provider, claim_time, claim_token=claim_token,
            legacy_migration_lookup=task_root.legacy_terminal_execution_lookup(store))
        task_root.validate_task_root_running_authority(claim, project_id, task_id, execution_id, provider)
    except TaskClaimConflict as exc:
        if lease:
            try:
                release(registry, lease["lock_id"], project_id, task_id, execution_id, provider, lease_token=lease["lease_token"])
            except Exception as cleanup_exc:
                raise TaskError(f"task claim conflict and writer lease release failed: {cleanup_exc}") from exc
        raise
    except Exception as exc:
        generation = claim.get("generation") if isinstance(claim, dict) else None
        if isinstance(generation, int) and generation >= 1:
            try:
                task_root.release_runtime_claim(task_claim_registry, project_id, task_id, execution_id, generation, claim_token=claim_token)
            except Exception as cleanup_exc:
                raise TaskError(f"task claim validation failed and claim cleanup failed; writer lease retained when present: {cleanup_exc}") from exc
            if lease:
                try:
                    release(registry, lease["lock_id"], project_id, task_id, execution_id, provider, lease_token=lease["lease_token"])
                except Exception as cleanup_exc:
                    raise TaskError(f"task claim validation failed and writer lease release failed: {cleanup_exc}") from exc
        elif lease:
            raise TaskError("task claim acquisition could not be proven; writer lease retained for recovery") from exc
        raise

    running = None
    try:
        current_task = store.get("tasks", project_id, task_id)
        validate("task", current_task)
        if current_task != task:
            raise TaskError("task changed after authoritative claim; running transition blocked")
        before = quota_snapshot(read_drive_status(service=service), provider)
        claimed = _claimed_task(current_task, execution_id)
        in_progress = {
            **claimed, "status": "in_progress", "assigned_provider": provider,
            "blocked_reason": None, "updated_at": now_iso(),
            "current_progress": f"Execution {execution_id} running",
            "next_action": "Launch provider after the running gate",
        }
        validate("task", in_progress)
        store.put("tasks", project_id, task_id, in_progress)
        if store.get("tasks", project_id, task_id) != in_progress:
            raise TaskError("in-progress task persistence verification failed")
        running_started = started_at or now_iso()
        hard_timeout_at = (parse_time(running_started) + timedelta(
            seconds=hard_timeout if hard_timeout is not None else hard_timeout_seconds(snapshot.get("expected_minutes") or 20)
        )).isoformat(timespec="seconds").replace("+00:00", "Z")
        running = {
            **execution, "access": access, "lease_evidence": lease_evidence,
            "started_at": running_started, "status": "running",
            "quota_before": before, "source_confidence": before.get("confidence", "unknown"),
            "heartbeat_at": running_started, "progress_updated_at": running_started,
            "hard_timeout_at": hard_timeout_at,
            "last_provider_event": "running_gate", "recovery_reason": None, "stale_at": None,
        }
        validate("execution", running)
        store.put("executions", project_id, execution_id, running)
        if store.get("executions", project_id, execution_id) != running:
            raise TaskError("running execution persistence verification failed")
    except Exception as exc:
        recovery_errors = []
        try:
            execution_recovered = _rollback_execution(store, project_id, execution_id, execution)
        except Exception as recovery_exc:
            execution_recovered = False
            recovery_errors.append(f"execution rollback failed: {recovery_exc}")
        task_recovered = False
        if execution_recovered:
            try:
                task_recovered = _rollback_task_record(store, project_id, task_id, execution_id, task)
            except Exception as recovery_exc:
                recovery_errors.append(f"task record rollback failed: {recovery_exc}")
        else:
            recovery_errors.append("task record and authoritative claim retained because execution rollback was not confirmed")
        if not execution_recovered or not task_recovered:
            details = "; ".join(recovery_errors) or "persistent state could not be confirmed"
            raise TaskError(f"running gate recovery required; claims and lease retained when present: {details}") from exc
        try:
            task_root.release_runtime_claim(task_claim_registry, project_id, task_id, execution_id, claim["generation"])
        except Exception as cleanup_exc:
            raise TaskError(f"running gate failed and task claim release failed; writer lease retained when present: {cleanup_exc}") from exc
        if lease:
            try:
                release(registry, lease["lock_id"], project_id, task_id, execution_id, provider, lease_token=lease["lease_token"])
            except Exception as cleanup_exc:
                raise TaskError(f"running gate failed and lease release failed: {cleanup_exc}") from exc
        raise
    return {"execution": running, "lease": lease, "task_claim": claim}


def _verify_terminal_authority(store, writer_registry, claim_registry, project_id, task_id, execution_id, provider, claim_generation, lease_token, writer_authority_released=False):
    execution = store.get("executions", project_id, execution_id)
    validate("execution", execution)
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(execution.get(key) != value for key, value in identity.items()):
        raise TaskError("terminal callback identity does not match execution")
    claim = task_root.read_task_root_or_legacy_claim(claim_registry, project_id, task_id)
    if not claim or claim.get("execution_id") != execution_id or claim.get("provider") != provider or claim.get("generation") != claim_generation:
        raise TaskError("terminal callback does not hold the exact task claim generation")
    cleanup = execution.get("cleanup_evidence") or {}
    if execution.get("access") == "production_write" and cleanup.get("writer_release") != "released":
        lease = execution.get("lease_evidence") or {}
        document, _, server_time = read_registry(writer_registry)
        current = document["locks"].get(lease.get("lock_id"))
        session_id = current.get("session_id") if current else None
        if session_id is not None and session_id != execution.get("session_id"):
            raise TaskError("terminal callback session does not match the writer lease")
        owner = owner_fields(project_id, task_id, execution_id, provider, session_id)
        if writer_authority_released:
            from manager.worktree_locks import verify_released_terminal_lease
            verify_released_terminal_lease(
                writer_registry, lease.get("lock_id"), project_id, task_id, execution_id,
                provider, lease.get("generation"), execution.get("session_id"),
            )
        elif (not current or current.get("generation") != lease.get("generation")
              or (current.get("status") != "released" and not active(current, server_time))
              or not same_owner(current, owner, lease_token)):
            raise TaskError("terminal callback does not own the writer lease generation")
    return execution


def cleanup_execution(writer_registry, claim_registry, execution, claim_generation, lease_token=None, writer_authority_released=False):
    """Release writer authority first; release only the supplied claim generation last."""
    previous = execution.get("cleanup_evidence") or {}
    writer_release = "released" if (previous.get("writer_release") == "released" or writer_authority_released) else "not_required"
    evidence = {"writer_release": writer_release, "task_claim_release": "not_attempted", "errors": []}
    if execution.get("access") == "production_write" and writer_release != "released":
        try:
            release(writer_registry, execution["lease_evidence"]["lock_id"], execution["project_id"], execution["task_id"], execution["execution_id"], execution["provider"], lease_token=lease_token)
            evidence["writer_release"] = "released"
        except Exception as exc:
            evidence["writer_release"] = "failed"
            evidence["errors"].append(f"writer release failed: {exc}")
            return evidence
    try:
        result = task_root.release_runtime_claim(claim_registry, execution["project_id"], execution["task_id"], execution["execution_id"], claim_generation)
        evidence["task_claim_release"] = "released" if result.get("released") else result.get("reason", "not_released")
    except Exception as exc:
        evidence["task_claim_release"] = "failed"
        evidence["errors"].append(f"task claim release failed: {exc}")
    return evidence


def _terminal_handoff(execution, task, status, summary, timestamp):
    # repo_write_evidence is real, independently git/remote-verified success
    # evidence attached to the execution before it terminalized (see
    # manager.executions.record_repo_write_evidence / manager.
    # repo_write_enforcement.capture_repo_write_evidence) -- never
    # recomputed here and never fabricated. Its mere presence on `execution`
    # is itself sufficient proof it is real: record_repo_write_evidence() is
    # the only writer of this field, and it is only ever called with
    # capture_repo_write_evidence()'s return value, which either raises
    # (nothing gets recorded -- a read-only/legacy task, or a genuine
    # commit/push failure, correctly leaves every evidence field empty/null)
    # or is fully git/remote-verified. Since 2026-08-28 that includes a
    # "failed" execution whose commit/push succeeded but whose declared
    # validation_command's real exit code was nonzero -- gating this on
    # status == "completed" would silently drop exactly the test-failure
    # evidence (real command/exit_code/output) a handoff's "Review outcome
    # and decide whether to resume" exists to carry forward.
    evidence = execution.get("repo_write_evidence")
    handoff = {
        # A retry reuses the same execution_id as the attempt it retries (see
        # reserve_execution), so retry_count must be part of this id or two
        # different attempts that both terminalize with the same status
        # collide on one deterministic handoff record -- confirmed live: a
        # genuine retry's own termination raised "deterministic terminal
        # handoff conflicts with persisted content" against the prior
        # attempt's handoff, leaving cleanup partial and the task claim
        # retained, which then made prepare_task_retry correctly refuse the
        # *next* retry too.
        "handoff_id": f"{execution['task_id']}-{status}-{execution['execution_id']}-{execution.get('retry_count', 0)}",
        "task_id": execution["task_id"], "project_id": execution["project_id"],
        "created_at": timestamp, "from_provider": execution["provider"], "to_provider": None,
        "from_session": execution.get("session_id"), "reason": status,
        "completed_work": [summary] if status == "completed" else [], "current_state": status,
        "next_action": "" if status == "completed" else "Review outcome and decide whether to resume",
        "minimal_context": summary,
        "files_changed": list(evidence["files_changed"]) if evidence else [],
        "commits": list(evidence["commits"]) if evidence else [],
        "tests": list(evidence["tests"]) if evidence else [],
        "known_issues": [], "do_not_touch": [],
        "acceptance_criteria": execution["task_snapshot"].get("acceptance_criteria", []),
        "feature_branch": evidence["branch"] if evidence else None,
        "push_status": evidence["push_status"] if evidence else None,
        "worktree_path": evidence["worktree_path"] if evidence else None,
        "remote_sha": evidence["remote_sha"] if evidence else None,
        # Honest test-evidence status (P0-C, hardened 2026-08-28): "passed"/
        # "failed" only from ADM independently running the Task's own
        # declared validation_command in the isolated worktree and reading
        # its real exit code -- never a provider's self-report; "not_required"
        # when the Task declared no validation_command. An empty `tests` list
        # must never be read downstream as "tests were run and verified" by
        # omission alone -- see manager.repo_write_enforcement.capture_repo_
        # write_evidence().
        "tests_status": evidence["tests_status"] if evidence else None,
    }
    if status == "completed":
        from manager.governance import execution_completion_report

        handoff["completion_report"] = execution_completion_report(task, execution, summary)
    return handoff


def _optional_record(store, area, project_id, record_id):
    try:
        return store.get(area, project_id, record_id)
    except KeyError:
        return None
    except TaskError as exc:
        if "found 0" in str(exc) or "not found" in str(exc):
            return None
        raise


def _expected_terminal_task(task, execution_id, status, summary, timestamp):
    if (task.get("source_context") or {}).get("active_execution_id") != execution_id:
        raise TaskError("task no longer identifies the terminal execution")
    expected = {**task, "status": "completed" if status == "completed" else "blocked", "updated_at": timestamp,
                "blocked_reason": None if status == "completed" else f"Execution {status}: {summary}",
                "current_progress": summary,
                "next_action": "" if status == "completed" else "Review outcome and decide whether to resume"}
    if status == "completed":
        expected["completed_at"] = timestamp
    validate("task", expected)
    return expected


def _terminal_state(execution, task, handoff, status, summary, timestamp):
    expected_handoff = _terminal_handoff(execution, task, status, summary, timestamp)
    expected_task = _expected_terminal_task(task, execution["execution_id"], status, summary, timestamp)
    return expected_handoff, expected_task, handoff == expected_handoff and task == expected_task


def _retain_terminal_authority(store, execution, status, persisted, error):
    try:
        current = store.get("executions", execution["project_id"], execution["execution_id"])
        existing_evidence = current.get("cleanup_evidence")
    except Exception:
        current = None
        existing_evidence = execution.get("cleanup_evidence")
    writer_release = "released" if (existing_evidence or {}).get("writer_release") == "released" else ("retained" if execution.get("access") == "production_write" else "not_required")
    updates = {
        "provider_outcome": status, "persistence": "incomplete" if not persisted else "partial",
        "persisted": persisted,
        "writer_release": writer_release,
        "task_claim_release": "retained", "errors": [f"persistence failed: {error}"],
    }
    audit = merge_cleanup_evidence(existing_evidence, updates)
    try:
        if current is None:
            current = store.get("executions", execution["project_id"], execution["execution_id"])
        current["cleanup_evidence"] = audit
        validate("execution", current)
        store.put("executions", execution["project_id"], execution["execution_id"], current)
    except Exception as audit_exc:
        error.add_note(f"terminal recovery audit persistence failed: {audit_exc}")
    return audit


def _attention_note(claim_registry, project_id, task_id, execution_id, view, note):
    try:
        task_root.advance_materialization_view(claim_registry, project_id, task_id, execution_id, view, "attention", note=note)
    except TaskError:
        pass


def retry_incomplete_terminal_persistence(store, project_id, task_id, execution_id, claim_registry=None, drive_file_id_factory=None):
    """Retry a terminal execution's incompletely-persisted handoff/task write.

    terminalize_execution() can raise after persisting 'execution' but before
    'handoff' and/or 'task' complete their own write-then-readback
    verification -- observed live twice in production (C Stability Gate
    rounds 13 and 42) as a transient Drive/GCS eventual-consistency glitch:
    the write itself lands durably, but the immediate readback used to
    confirm it doesn't yet see it. _retain_terminal_authority() durably
    records this as cleanup_evidence={persistence:'partial',
    task_claim_release:'retained', ...} -- correct, since
    cleanup_execution() (which would actually release the claim) is never
    reached when persistence fails. recover_task_claim() then correctly
    REFUSES to release that claim while persistence stays incomplete (a
    real safety fence, not a bug) -- but nothing else ever retries the
    specific missing write, so without this helper the Task/Command stay
    stuck in attention forever, not just for one transient tick.

    Re-deriving and re-writing the exact same idempotent handoff/task
    records from the still-terminal, still-claim-holding execution is safe
    to retry on every watcher tick: nothing else can be racing to write
    them (the GCS claim is still held), and the target records are pure
    functions of the unchanged execution/task state, so a retry either
    completes the missing piece or safely no-ops.

    Strengthened Design A (Checkpoints B-E): when `claim_registry` (the
    task's single GCS Task Root Object) is given, this is also the R17
    legacy-recovery integration point. Before touching Drive at all, it
    CAS-binds the execution's terminal proposal via
    task_root.commit_terminal_bind() -- a losing execution
    (TerminalProposalLost) never materializes anything and returns False,
    with the conflict recorded as durable evidence on its own
    cleanup_evidence.errors (its own terminal status/outcome is never
    rewritten). A winning bind carries the fixed Handoff Drive ID (frozen
    once, never regenerated) and the expected Task/Handoff projection
    digests, and materialization progress is tracked per-view
    (task/handoff) via task_root.advance_materialization_view -- a
    permanently-failing view lands in "attention" (recoverable, not a
    dead end) rather than blocking the other view or wiping out the
    execution's own terminal truth. `claim_registry` is optional so any
    caller not yet migrated keeps the pre-Design-A, Drive-only behavior
    exactly as before.

    Returns True once persistence is (now) complete, False otherwise --
    on any failure, leaves the execution's cleanup_evidence exactly as it
    was, so the caller's existing attention/refusal path still applies
    unchanged."""
    execution = store.get("executions", project_id, execution_id)
    if execution.get("status") not in ("completed", "failed", "interrupted"):
        return False
    evidence = execution.get("cleanup_evidence") or {}
    if evidence.get("persistence") == "complete" and evidence.get("persisted") == ["execution", "handoff", "task"]:
        return True
    status = execution["status"]
    timestamp = execution.get("completed_at") or now_iso()
    summary = execution["notes"][-1] if execution.get("notes") else f"Execution {execution_id} {status}"

    handoff_drive_file_id = None
    bound_projection = None
    if claim_registry is not None:
        try:
            task = store.get("tasks", project_id, task_id)
            expected_handoff_preview = _terminal_handoff(execution, task, status, summary, timestamp)
            expected_task_preview = _expected_terminal_task(task, execution_id, status, summary, timestamp)
        except Exception:
            return False
        try:
            bound_document, _generation = task_root.commit_terminal_bind(
                claim_registry, project_id, task_id, execution,
                task_drive_id_factory=drive_file_id_factory, handoff_drive_id_factory=drive_file_id_factory,
                expected_task_projection=expected_task_preview, expected_handoff_projection=expected_handoff_preview)
        except task_root.TerminalProposalLost as exc:
            current = store.get("executions", project_id, execution_id)
            note = f"terminal commit lost to execution {exc.winner.get('execution_id')}: fail closed, no materialization"
            current["cleanup_evidence"] = merge_cleanup_evidence(current.get("cleanup_evidence"), {"errors": [note]})
            try:
                validate("execution", current)
                store.put("executions", project_id, execution_id, current)
            except Exception:
                pass
            return False
        except (task_root.TerminalProposalConflict, TaskError):
            return False
        bind = bound_document["terminal"]
        handoff_drive_file_id = bind.get("handoff_drive_file_id")
        bound_projection = task_root.projection_of(bind)

    try:
        task = store.get("tasks", project_id, task_id)
        expected_handoff = _terminal_handoff(execution, task, status, summary, timestamp)
        handoff = _optional_record(store, "handoffs", project_id, expected_handoff["handoff_id"])
        if handoff is None:
            create_handoff(store, expected_handoff, drive_file_id=handoff_drive_file_id)
            if store.get("handoffs", project_id, expected_handoff["handoff_id"]) != expected_handoff:
                if claim_registry is not None:
                    _attention_note(claim_registry, project_id, task_id, execution_id, "handoff", "handoff persistence verification failed")
                return False
        elif handoff != expected_handoff:
            if claim_registry is not None:
                _attention_note(claim_registry, project_id, task_id, execution_id, "handoff", "deterministic handoff conflicts with persisted content")
            return False
        if claim_registry is not None:
            task_root.advance_materialization_view(claim_registry, project_id, task_id, execution_id, "handoff", "pending")
            task_root.advance_materialization_view(claim_registry, project_id, task_id, execution_id, "handoff", "verified")

        expected_task = _expected_terminal_task(task, execution_id, status, summary, timestamp)
        if bound_projection is not None:
            expected_task = {**expected_task,
                             "source_context": {**expected_task["source_context"], "terminal_commit_projection": bound_projection}}
            validate("task", expected_task)
        if task != expected_task:
            store.put("tasks", project_id, task_id, expected_task)
            if store.get("tasks", project_id, task_id) != expected_task:
                if claim_registry is not None:
                    _attention_note(claim_registry, project_id, task_id, execution_id, "task", "task persistence verification failed")
                return False
        if claim_registry is not None:
            task_root.advance_materialization_view(claim_registry, project_id, task_id, execution_id, "task", "pending")
            task_root.advance_materialization_view(claim_registry, project_id, task_id, execution_id, "task", "verified")
    except Exception as exc:
        if claim_registry is not None:
            _attention_note(claim_registry, project_id, task_id, execution_id, "task", str(exc)[:300])
        return False

    updates = {"provider_outcome": status, "persistence": "complete",
               "persisted": ["execution", "handoff", "task"], "errors": []}
    terminal = store.get("executions", project_id, execution_id)
    terminal["cleanup_evidence"] = merge_cleanup_evidence(terminal.get("cleanup_evidence"), updates)
    try:
        validate("execution", terminal)
        store.put("executions", project_id, execution_id, terminal)
        return store.get("executions", project_id, execution_id) == terminal
    except Exception:
        return False


def terminalize_execution(store, service, writer_registry, claim_registry, project_id, task_id, execution_id, provider, status, claim_generation, provider_stopped, lease_token=None, completed_at=None, summary=None, writer_authority_released=False):
    if status not in ("completed", "failed", "interrupted"):
        raise TaskError(f"invalid terminal execution status: {status}")
    if provider_stopped is not True:
        raise TaskError("provider process must be confirmed stopped before terminalization")
    existing = store.get("executions", project_id, execution_id)
    identity = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id, "provider": provider}
    if any(existing.get(key) != value for key, value in identity.items()):
        raise TaskError("terminal callback identity does not match execution")
    terminal_statuses = ("completed", "failed", "interrupted")
    if existing.get("status") in terminal_statuses and existing["status"] != status:
        raise TaskError("conflicting duplicate terminal outcome")
    if existing.get("status") not in ("running", status):
        raise TaskError("terminalization requires a running execution")

    if existing.get("status") == status:
        timestamp = existing["completed_at"]
        summary = existing["notes"][-1] if existing.get("notes") else f"Execution {execution_id} {status}"
        task = store.get("tasks", project_id, task_id)
        handoff_id = f"{task_id}-{status}-{execution_id}-{existing.get('retry_count', 0)}"
        handoff = _optional_record(store, "handoffs", project_id, handoff_id)
        _, _, state_complete = _terminal_state(existing, task, handoff, status, summary, timestamp)
        audit = existing.get("cleanup_evidence") or {}
        cleanup_complete = audit.get("writer_release") in ("released", "not_required") and audit.get("task_claim_release") == "released"
        if state_complete and audit.get("persistence") == "complete" and cleanup_complete:
            return {"execution": existing, "cleanup": audit, "idempotent": True}
    execution = _verify_terminal_authority(store, writer_registry, claim_registry, project_id, task_id, execution_id, provider, claim_generation, lease_token, writer_authority_released=writer_authority_released)

    timestamp = execution.get("completed_at") or completed_at or now_iso()
    summary = (execution["notes"][-1] if execution.get("status") == status and execution.get("notes")
               else summary or f"Execution {execution_id} {status}")
    terminal = execution
    persisted = []
    try:
        if execution.get("status") == "running":
            terminal = persist_terminal(store, service, project_id, execution_id, status, timestamp, summary)
        persisted.append("execution")
        task = store.get("tasks", project_id, task_id)
        expected_handoff = _terminal_handoff(terminal, task, status, summary, timestamp)
        handoff = _optional_record(store, "handoffs", project_id, expected_handoff["handoff_id"])
        if handoff is None:
            create_handoff(store, expected_handoff)
            if store.get("handoffs", project_id, expected_handoff["handoff_id"]) != expected_handoff:
                raise TaskError("terminal handoff persistence verification failed")
        elif handoff != expected_handoff:
            raise TaskError("deterministic terminal handoff conflicts with persisted content")
        persisted.append("handoff")
        expected_task = _expected_terminal_task(task, execution_id, status, summary, timestamp)
        if task != expected_task:
            store.put("tasks", project_id, task_id, expected_task)
            if store.get("tasks", project_id, task_id) != expected_task:
                raise TaskError("terminal task persistence verification failed")
        persisted.append("task")
    except Exception as exc:
        _retain_terminal_authority(store, execution, status, persisted, exc)
        raise

    terminal = store.get("executions", project_id, execution_id)
    existing_cleanup = terminal.get("cleanup_evidence")
    pre_updates = {
        "provider_outcome": status, "persistence": "complete", "persisted": persisted,
        "writer_release": "released" if (writer_authority_released or (existing_cleanup or {}).get("writer_release") == "released") else ("retained" if execution.get("access") == "production_write" else "not_required"),
        "task_claim_release": "retained", "errors": [],
    }
    pre_cleanup = merge_cleanup_evidence(existing_cleanup, pre_updates)
    terminal["cleanup_evidence"] = pre_cleanup
    try:
        validate("execution", terminal)
        store.put("executions", project_id, execution_id, terminal)
        if store.get("executions", project_id, execution_id) != terminal:
            raise TaskError("complete persistence audit verification failed")
    except Exception as exc:
        pre_cleanup = merge_cleanup_evidence(pre_cleanup, {"errors": [f"complete persistence audit failed; authority retained: {exc}"]})
        return {"execution": terminal, "cleanup": pre_cleanup, "idempotent": False}

    cleanup = cleanup_execution(writer_registry, claim_registry, terminal, claim_generation, lease_token, writer_authority_released=writer_authority_released)
    fresh = store.get("executions", project_id, execution_id)
    audit = merge_cleanup_evidence(fresh.get("cleanup_evidence"),
                                   {**cleanup, "provider_outcome": status, "persistence": "complete", "persisted": persisted})
    try:
        fresh["cleanup_evidence"] = audit
        validate("execution", fresh)
        store.put("executions", project_id, execution_id, fresh)
        if store.get("executions", project_id, execution_id) != fresh:
            raise TaskError("cleanup audit persistence verification failed")
        terminal = fresh
    except Exception as audit_exc:
        cleanup["errors"].append(f"cleanup audit persistence failed: {audit_exc}")
    return {"execution": terminal, "cleanup": cleanup, "idempotent": False}
