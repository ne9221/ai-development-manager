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

Per-slice lineage: each automatic continuation is a "slice" -- one
task/execution/session/handoff attempt (and its bounded retries) at a given
depth. Overwriting top-level task_id/execution_id/session_id/handoff_id in
place would lose that history the moment a new slice starts, so this module
also appends to `slices`, an ordered, append-only list of per-slice lineage
records; the top-level fields remain a convenience mirror of the *current*
slice only, never the source of truth for history.
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

# Events/target-states that begin a genuinely NEW slice: the chain's very
# first task_planned (idle -> task_planned) and every later
# next_dispatch_check that actually proceeds (next_task_ready ->
# task_planned). The dispatch_requested self-loop hold (task_planned ->
# task_planned) and the next_dispatch_check self-loop hold (next_task_ready
# -> next_task_ready) both target "task_planned"/"next_task_ready" too but
# via a different event, so they are correctly excluded below.
_NEW_SLICE_EVENTS = {
    ("task_planned", "task_planned"),
    ("next_dispatch_check", "task_planned"),
}


def validate_continuation(document):
    try:
        _VALIDATOR.validate(document)
    except Exception as exc:
        raise ContinuationError(f"invalid continuation record: {exc}") from exc


def create_chain(store, project_id, chain_id, root_request_id, max_depth, max_retries, now=None):
    """Persist a fresh chain at IDLE with no slices yet. Raises if one
    already exists for chain_id, so a caller cannot silently reuse another
    chain's lineage."""
    timestamp = now or now_iso()
    try:
        store.get(AREA, project_id, chain_id)
    except (KeyError, TaskError):
        pass
    else:
        raise ContinuationError(f"continuation chain already exists: {chain_id}")
    record = {
        "schema_version": 1, "chain_id": chain_id, "root_request_id": root_request_id, "project_id": project_id,
        "state": "idle", "task_id": None, "execution_id": None, "session_id": None, "handoff_id": None,
        "depth": 0, "max_depth": max_depth, "retry_count": 0, "max_retries": max_retries,
        "created_at": timestamp, "updated_at": timestamp, "last_reason": "chain created", "history": [], "slices": [],
    }
    validate_continuation(record)
    store.put(AREA, project_id, chain_id, record)
    return record


def load_chain(store, project_id, chain_id):
    record = store.get(AREA, project_id, chain_id)
    validate_continuation(record)
    return record


def _new_slice(record, request_id, timestamp):
    if request_id is None:
        raise ContinuationError("a new slice requires its own dispatch request_id")
    if request_id == record["root_request_id"]:
        raise ContinuationError("a new slice must not reuse the root/origin request_id as its idempotency key")
    if any(existing["request_id"] == request_id for existing in record["slices"]):
        raise ContinuationError(f"dispatch request_id already used by an earlier slice in this chain: {request_id}")
    return {
        "slice_index": len(record["slices"]), "request_id": request_id, "task_id": None,
        "execution_ids": [], "session_id": None, "handoff_id": None, "outcome": None,
        "retry_count": 0, "started_at": timestamp, "terminal_at": None,
    }


def apply(store, project_id, chain_id, decision, request_id=None, task_id=None, execution_id=None, session_id=None, handoff_id=None, now=None):
    """Apply one Decision (from manager.continuation_decision) to a
    persisted chain: validates the decision matches the chain's current
    state, manages per-slice lineage (new slice on a genuine continuation,
    in-place update on a retry of the same slice, fresh retry_count per
    slice), and persists.

    `request_id` is required exactly when this Decision starts a new slice
    (the chain's first task_planned, or a next_dispatch_check that actually
    proceeds) and must be distinct from the root request_id and from every
    prior slice's request_id -- the same mutual-exclusion/duplicate-authority
    contract that governs dispatch also governs slice identity, so a caller
    cannot accidentally collapse two slices' evidence into one idempotency
    key.
    """
    record = load_chain(store, project_id, chain_id)
    if decision.from_state != record["state"]:
        raise ContinuationError(
            f"stale decision: chain {chain_id} is at {record['state']!r}, decision expects {decision.from_state!r}"
        )
    timestamp = now or now_iso()
    updated = dict(record)
    updated["state"] = decision.to_state
    updated["slices"] = [dict(item) for item in record["slices"]]

    starts_new_slice = (decision.event, decision.to_state) in _NEW_SLICE_EVENTS
    if starts_new_slice:
        updated["slices"].append(_new_slice(updated, request_id, timestamp))
        updated["depth"] = len(updated["slices"]) - 1
        updated["retry_count"] = 0
    elif request_id is not None:
        raise ContinuationError("request_id may only be supplied when a new slice is starting")

    for field, value in decision.patch.items():
        if field in ("depth", "max_depth", "retry_count", "max_retries", "is_production_mutating", "action_kind"):
            updated[field] = value

    current_slice = updated["slices"][-1] if updated["slices"] else None

    if decision.event == "retry_check" and decision.to_state == "dispatching":
        if current_slice is None:
            raise ContinuationError("retry requires an existing slice")
        current_slice["retry_count"] = updated["retry_count"]

    if decision.event == "validation_result":
        if current_slice is None:
            raise ContinuationError("validation result requires an existing slice")
        current_slice["outcome"] = decision.to_state
        current_slice["terminal_at"] = timestamp

    for field, value in (("task_id", task_id), ("session_id", session_id), ("handoff_id", handoff_id)):
        if value is not None:
            updated[field] = value
            if current_slice is not None:
                current_slice[field] = value
    if execution_id is not None:
        updated["execution_id"] = execution_id
        if current_slice is not None and execution_id not in current_slice["execution_ids"]:
            current_slice["execution_ids"] = current_slice["execution_ids"] + [execution_id]

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
