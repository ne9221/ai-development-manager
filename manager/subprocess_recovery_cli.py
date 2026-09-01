"""CLI driver for Gap 2's true fresh-OS-process recovery proof.

Each invocation (`python -m manager.subprocess_recovery_cli <verb> ...`) is
a genuinely separate OS process: a fresh Python interpreter, fresh module
state, zero inherited objects. The only channel between successive
invocations is the durable state on disk (manager.subprocess_recovery_support's
FileStore/FileClaimRegistry). This is what the crash-matrix tests in
manager/test_subprocess_recovery.py exercise -- see that file for the
CP1-CP5 scenarios and SUBPROCESS_DURABLE_PROOF vs REAL_GCS_DRIVE_PROOF
framing.

Verbs:
  seed <store> <claim> <project_id> <task_id> <execution_id>
      Build the exact R17 legacy durable starting state: a terminal
      Execution, a blocked/stale Task, and a legacy (pre-Design-A) claim
      document -- all via the real production functions
      (reserve_execution/enter_running_gate/terminalize_execution), then
      corrupted to the exact live R17 shape.

  partial <stage> <store> <claim> <project_id> <task_id> <execution_id>
      Perform real production work up through (and including) `stage`,
      then exit immediately -- simulating a crash right after that stage.
      stage in: bind, handoff, task, cleanup, release, attention.

  recover <store> <claim> <project_id> <task_id> <execution_id>
      Call the REAL execution_lifecycle.retry_incomplete_terminal_persistence
      -- the actual production recovery function, not a test-only
      shortcut -- to converge from whatever durable state is currently on
      disk, regardless of which partial stage produced it.

  verify <store> <claim> <project_id> <task_id> <execution_id>
      Print a JSON summary of the current durable state for the test
      process to assert on.
"""

import json
import socket
import sys
from datetime import datetime, timezone
from unittest.mock import patch

from manager import task_root
from manager.execution_lifecycle import (
    _expected_terminal_task, _terminal_handoff, enter_running_gate,
    merge_cleanup_evidence, retry_incomplete_terminal_persistence, terminalize_execution,
)
from manager.executions import reserve_execution
from manager.subprocess_recovery_support import FileClaimRegistry, FileStore
from manager.tasks import create_handoff, create_project, create_task, now_iso, validate
from manager.trusted_ingress import REQUIRED_TASK_POLICIES


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _quota_document():
    return {"claude": {"freshness": "fresh"}, "codex": {"freshness": "fresh"}}


def _project():
    return {
        "project_id": "p1", "name": "Project", "repo": "github:owner/repo", "default_branch": "main",
        "runtime_ssot": "Drive", "project_rules": [], "active_tasks": ["t1"],
        "current_phase": "Phase 3C", "important_constraints": [],
    }


def _task():
    return {
        "task_id": "t1", "project_id": "p1", "title": "Subprocess recovery proof", "task_type": "implementation",
        "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": False,
        "needs_research": False, "needs_browser": False, "parallelizable": False,
        "read_only": True, "scope": ["manager/subprocess_recovery_cli.py"], "constraints": [],
        "acceptance_criteria": ["gate"], "working_directory": "unused",
        "branch": "refs/heads/main", "baseline_head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "allowed_paths": ["manager/subprocess_recovery_cli.py"],
        "execution_policies": ["fail closed"],
    }


def cmd_seed(store_path, claim_path, project_id, task_id, execution_id):
    store = FileStore(store_path)
    claim_registry = FileClaimRegistry(claim_path)
    create_project(store, _project())
    create_task(store, _task(), assign=False)
    task = store.get("tasks", project_id, task_id)
    task["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
    store.put("tasks", project_id, task_id, task)

    reserve_execution(store, project_id, task_id, execution_id, "codex", {"decision": "fresh"})
    with patch("manager.execution_lifecycle.read_drive_status", return_value=_quota_document()):
        enter_running_gate(store, object(), None, project_id, task_id, execution_id, "codex",
                          "read_only", task_claim_registry=claim_registry)
    execution = store.get("executions", project_id, execution_id)
    execution["session_id"] = "codex:subprocess-proof-session"
    execution["provider_evidence"] = {"host": socket.gethostname()[:100], "pid": 999996,
                                      "creation_identity": "subprocess-proof", "started_at": _now()}
    store.put("executions", project_id, execution_id, execution)
    with patch("manager.executions.read_drive_status", return_value=_quota_document()):
        terminalize_execution(store, object(), None, claim_registry, project_id, task_id,
                              execution_id, "codex", "completed", 1, True,
                              summary="Execution terminal completed")

    # Corrupt to the exact live R17 shape: persistence partial, claim
    # retained, Task stale/blocked -- as if cleanup_execution() was never
    # reached because persistence itself failed.
    execution = store.get("executions", project_id, execution_id)
    execution["cleanup_evidence"]["persistence"] = "partial"
    execution["cleanup_evidence"]["persisted"] = ["execution"]
    execution["cleanup_evidence"]["task_claim_release"] = "retained"
    validate("execution", execution)
    store.put("executions", project_id, execution_id, execution)
    task = store.get("tasks", project_id, task_id)
    task["source_context"] = {"active_execution_id": execution_id}
    task["status"] = "blocked"
    task["blocked_reason"] = "stuck mid-persistence"
    validate("task", task)
    store.put("tasks", project_id, task_id, task)

    from manager.task_claims import _new_claim_record
    legacy_record = _new_claim_record(project_id, task_id, execution_id, "codex", "2026-08-13T00:00:00Z")
    state = claim_registry._load()
    state["document"] = legacy_record
    state["generation"] = max(state["generation"], 1)
    claim_registry._save(state)
    print(json.dumps({"status": "seeded"}))


def cmd_partial(stage, store_path, claim_path, project_id, task_id, execution_id):
    store = FileStore(store_path)
    claim_registry = FileClaimRegistry(claim_path)
    execution = store.get("executions", project_id, execution_id)
    status = execution["status"]
    timestamp = execution.get("completed_at") or now_iso()
    summary = execution["notes"][-1] if execution.get("notes") else f"Execution {execution_id} {status}"
    task = store.get("tasks", project_id, task_id)

    bound_document, _generation = task_root.commit_terminal_bind(claim_registry, project_id, task_id, execution)
    if stage == "bind":
        print(json.dumps({"status": "crashed_after_bind"}))
        return
    if stage == "attention":
        # Permanent Drive materialization failure: the Handoff view is
        # stuck in attention (bounded retries exhausted), so runtime
        # claim authority is released to avoid holding it hostage --
        # but Execution.cleanup_evidence.persistence must NEVER be
        # claimed "complete", since the actual Drive write never
        # succeeded. This is the exact state a real bounded-retry
        # watchdog (not yet wired into the live path -- see the final
        # report's honest gap list) would leave behind.
        task_root.advance_materialization_view(claim_registry, project_id, task_id, execution_id, "handoff", "pending")
        task_root.advance_materialization_view(claim_registry, project_id, task_id, execution_id, "handoff", "attention", note="simulated permanent Drive 403")
        task_root.advance_cleanup_facet(claim_registry, project_id, task_id, execution_id, "release_pending")
        task_root.advance_cleanup_facet(claim_registry, project_id, task_id, execution_id, "released")
        task_root.release_runtime_claim(claim_registry, project_id, task_id, execution_id, claim_registry.generation)
        print(json.dumps({"status": "crashed_with_materialization_attention"}))
        return
    bind = bound_document["terminal"]
    handoff_drive_file_id = bind.get("handoff_drive_file_id")
    expected_handoff = _terminal_handoff(execution, task, status, summary, timestamp)
    create_handoff(store, expected_handoff, drive_file_id=handoff_drive_file_id)
    if stage == "handoff":
        print(json.dumps({"status": "crashed_after_handoff"}))
        return
    bound_projection = task_root.projection_of(bind)
    expected_task = _expected_terminal_task(task, execution_id, status, summary, timestamp)
    expected_task = {**expected_task, "source_context": {**expected_task["source_context"], "terminal_commit_projection": bound_projection}}
    validate("task", expected_task)
    store.put("tasks", project_id, task_id, expected_task)
    if stage == "task":
        print(json.dumps({"status": "crashed_after_task"}))
        return
    updates = {"provider_outcome": status, "persistence": "complete",
              "persisted": ["execution", "handoff", "task"], "errors": []}
    terminal = store.get("executions", project_id, execution_id)
    terminal["cleanup_evidence"] = merge_cleanup_evidence(terminal.get("cleanup_evidence"), updates)
    validate("execution", terminal)
    store.put("executions", project_id, execution_id, terminal)
    if stage == "cleanup":
        print(json.dumps({"status": "crashed_after_cleanup"}))
        return
    task_root.release_runtime_claim(claim_registry, project_id, task_id, execution_id, claim_registry.generation)
    if stage == "release":
        print(json.dumps({"status": "crashed_after_release"}))
        return
    raise SystemExit(f"unknown stage: {stage}")


def cmd_recover(store_path, claim_path, project_id, task_id, execution_id):
    store = FileStore(store_path)
    claim_registry = FileClaimRegistry(claim_path)
    result = retry_incomplete_terminal_persistence(store, project_id, task_id, execution_id, claim_registry=claim_registry)
    print(json.dumps({"status": "recovered" if result else "not_yet_converged", "result": result}))


def cmd_verify(store_path, claim_path, project_id, task_id, execution_id):
    store = FileStore(store_path)
    claim_registry = FileClaimRegistry(claim_path)
    try:
        execution = store.get("executions", project_id, execution_id)
    except Exception:
        execution = None
    try:
        task = store.get("tasks", project_id, task_id)
    except Exception:
        task = None
    try:
        handoff_ids = [k.split("::")[2] for k in store._load() if k.startswith(f"handoffs::{project_id}::")]
    except Exception:
        handoff_ids = []
    print(json.dumps({
        "execution_status": execution.get("status") if execution else None,
        "execution_cleanup_evidence": execution.get("cleanup_evidence") if execution else None,
        "task_status": task.get("status") if task else None,
        "task_projection": (task.get("source_context") or {}).get("terminal_commit_projection") if task else None,
        "handoff_ids": handoff_ids,
        "task_root_document": claim_registry.document,
    }))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    verb = argv[0]
    if verb == "seed":
        cmd_seed(*argv[1:])
    elif verb == "partial":
        cmd_partial(*argv[1:])
    elif verb == "recover":
        cmd_recover(*argv[1:])
    elif verb == "verify":
        cmd_verify(*argv[1:])
    else:
        raise SystemExit(f"unknown verb: {verb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
