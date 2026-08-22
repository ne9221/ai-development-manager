"""Orchestration service for the autonomous continuation foundation.

This is the only module in the foundation allowed to do I/O. It persists
ContinuationChain records (schema/continuation.schema.json) and drives them
through manager.continuation_decision's pure functions. It never launches a
provider and never calls manager.dispatcher.dispatch() itself -- wiring a
Decision that reaches "task_planned"/"dispatching" to a real dispatch call is
explicitly out of scope for this foundation (see AI-DEVELOPMENT-RULES.md
rule 11 / task spec: "Do NOT implement autonomous production dispatch yet").

Storage uses the same duck-typed interface as manager.tasks.DriveRecords
(`store.get(area, project_id, record_id)` / `store.put(area, project_id,
record_id, document)`) under area "continuations", so a real Drive-backed
store can be wired in later without changing this module.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from manager.continuation_states import ContinuationError
from manager.tasks import TaskError, now_iso

AREA = "continuations"
_SCHEMA_PATH = Path(__file__).parents[1] / "schema" / "continuation.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())


def validate_continuation(document):
    try:
        _VALIDATOR.validate(document)
    except Exception as exc:
        raise ContinuationError(f"invalid continuation record: {exc}") from exc


def create_chain(store, project_id, chain_id, request_id, max_depth, max_retries, now=None):
    """Persist a fresh chain at IDLE. Raises if one already exists for
    chain_id, so a caller cannot silently reuse another chain's lineage."""
    timestamp = now or now_iso()
    try:
        store.get(AREA, project_id, chain_id)
    except (KeyError, TaskError):
        pass
    else:
        raise ContinuationError(f"continuation chain already exists: {chain_id}")
    record = {
        "schema_version": 1, "chain_id": chain_id, "request_id": request_id, "project_id": project_id,
        "state": "idle", "task_id": None, "execution_id": None, "session_id": None, "handoff_id": None,
        "depth": 0, "max_depth": max_depth, "retry_count": 0, "max_retries": max_retries,
        "created_at": timestamp, "updated_at": timestamp, "last_reason": "chain created", "history": [],
    }
    validate_continuation(record)
    store.put(AREA, project_id, chain_id, record)
    return record


def load_chain(store, project_id, chain_id):
    record = store.get(AREA, project_id, chain_id)
    validate_continuation(record)
    return record


def apply(store, project_id, chain_id, decision, task_id=None, execution_id=None, session_id=None, handoff_id=None, now=None):
    """Apply one Decision (from manager.continuation_decision) to a
    persisted chain: validates the decision matches the chain's current
    state, updates lineage fields only when explicitly supplied (never
    drops prior lineage), records history, and persists."""
    record = load_chain(store, project_id, chain_id)
    if decision.from_state != record["state"]:
        raise ContinuationError(
            f"stale decision: chain {chain_id} is at {record['state']!r}, decision expects {decision.from_state!r}"
        )
    timestamp = now or now_iso()
    updated = dict(record)
    updated["state"] = decision.to_state
    for field, value in decision.patch.items():
        if field in ("depth", "max_depth", "retry_count", "max_retries", "is_production_mutating", "action_kind"):
            updated[field] = value
    for field, value in (("task_id", task_id), ("execution_id", execution_id), ("session_id", session_id), ("handoff_id", handoff_id)):
        if value is not None:
            updated[field] = value
    updated["updated_at"] = timestamp
    updated["last_reason"] = decision.reason
    updated["history"] = record["history"] + [{
        "at": timestamp, "event": decision.event, "from_state": decision.from_state,
        "to_state": decision.to_state, "reason": decision.reason,
    }]
    validate_continuation(updated)
    store.put(AREA, project_id, chain_id, updated)
    if store.get(AREA, project_id, chain_id) != updated:
        raise ContinuationError("continuation persistence verification failed")
    return updated


def is_halted(record):
    from manager.continuation_states import TERMINAL_FOR_AUTOMATION

    return record["state"] in TERMINAL_FOR_AUTOMATION
