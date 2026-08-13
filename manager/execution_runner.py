"""Minimal reserved-to-terminal Codex execution runner."""

from manager.codex_launcher import CodexLauncher, LaunchRequest
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import link_execution_session
from manager.session_identity import manager_session_key
from manager.tasks import TaskError, validate
from manager.worktree_locks import link_session as link_writer_session


def _thread_id(prepared):
    value = prepared.thread_id
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 500 or any(ord(char) < 32 for char in value):
        raise TaskError("prepared Codex thread id is invalid")
    return value


def _session(execution, prepared, request):
    thread_id = _thread_id(prepared)
    session_id = manager_session_key("codex", thread_id)
    # session_path is adapter-validated advisory evidence; identity is thread_id.
    return {
        "session_id": session_id, "provider": "codex", "provider_session_id": thread_id,
        "project_id": execution["project_id"], "task_id": execution["task_id"],
        "conversation_label": None, "title": None, "summary": None,
        "started_at": prepared.prepared_at, "updated_at": prepared.prepared_at,
        "working_directory": request.working_directory, "repository": None,
        "source_identifier": thread_id, "source_path": prepared.session_path,
        "classification_method": "working_directory", "classification_confidence": "high",
        "classification_status": "classified", "status": "active", "message_count": 0,
        "model": request.model, "first_user_prompt": None,
    }


def _persist_session_link(store, writer_registry, execution, prepared, request, lease_token):
    session = _session(execution, prepared, request)
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
        raise TaskError("canonical Codex session conflicts with persisted content")
    if store.get("sessions", execution["project_id"], session["session_id"]) != session:
        raise TaskError("canonical Codex session persistence verification failed")

    link_execution_session(store, execution["project_id"], execution["execution_id"], session)
    linked = store.get("executions", execution["project_id"], execution["execution_id"])
    if linked.get("session_id") != session["session_id"] or linked.get("provider_session_id") != prepared.thread_id:
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


def _stopped(prepared):
    process = getattr(getattr(prepared, "_client", None), "process", None)
    poll = getattr(process, "poll", None)
    return callable(poll) and poll() is not None


def _failure_summary(error):
    classification = getattr(error, "classification", None) or type(error).__name__
    return f"Codex runner interrupted: {str(classification)[:200]}"


def run_execution(store, service, writer_registry, claim_registry, launcher: CodexLauncher,
                  project_id, task_id, execution_id, prompt, launch_request: LaunchRequest,
                  access="production_write", baseline_head=None):
    """Run one reserved Codex execution through the reviewed lifecycle gates.

    ``provider_stopped`` is derived only after prepare-owned cleanup or after
    ``close()`` plus process-exit evidence; it is never assumed by the caller.
    Full prompt, transcript, stderr, and raw provider failure text are not
    persisted by this runner.
    """
    gate = enter_running_gate(
        store, service, writer_registry, project_id, task_id, execution_id, "codex", access,
        baseline_head=baseline_head, task_claim_registry=claim_registry,
    )
    execution = gate["execution"]
    lease_token = gate["lease"]["lease_token"] if gate["lease"] else None
    prepared = running = outcome = session = None
    operation_error = close_error = None
    status, summary = "interrupted", "Codex runner interrupted"
    try:
        if launch_request.working_directory != execution["task_snapshot"].get("working_directory"):
            raise TaskError("launch working directory does not match the reserved execution")
        prepared = launcher.prepare(launch_request)
        session = _persist_session_link(store, writer_registry, execution, prepared, launch_request, lease_token)
        running = launcher.start(prepared, prompt)
        outcome = launcher.wait(running)
        status = outcome.status if outcome.status in ("completed", "failed", "interrupted") else "failed"
        summary = f"Codex turn {status}"
        if outcome.failure_classification:
            summary += f": {outcome.failure_classification[:200]}"
    except (Exception, KeyboardInterrupt) as exc:
        operation_error = exc
        summary = _failure_summary(exc)
    finally:
        if prepared is not None:
            try:
                launcher.close(running or prepared)
            except (Exception, KeyboardInterrupt) as exc:
                close_error = exc

    # prepare() owns and closes a spawned process until it returns a handle.
    provider_stopped = prepared is None and operation_error is not None
    if prepared is not None:
        provider_stopped = _stopped(prepared)
    if not provider_stopped:
        error = TaskError("Codex provider stop could not be proven; terminal authority retained")
        if operation_error:
            error.add_note(f"runner: {operation_error}")
        if close_error:
            error.add_note(f"close: {close_error}")
        raise error

    terminal = terminalize_execution(
        store, service, writer_registry, claim_registry, project_id, task_id, execution_id, "codex",
        status, gate["task_claim"]["generation"], provider_stopped, lease_token=lease_token,
        completed_at=outcome.completed_at if outcome else None, summary=summary,
    )
    if operation_error:
        operation_error.add_note("execution terminalized as interrupted after provider stop was proven")
        raise operation_error
    if close_error:
        close_error.add_note(f"provider outcome remained {status}")
        raise close_error
    return {"terminal": terminal, "provider_outcome": outcome, "session": session}
