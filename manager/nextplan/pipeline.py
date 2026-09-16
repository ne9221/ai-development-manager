"""The read-only pipeline: agent output in, one decision out.

    extract -> verify -> classify -> plan

``process`` composes the stages and returns every intermediate artifact, so a
decision can be audited back to the exact claim, probe observation and failure
class that produced it. ``advance`` additionally returns the next task state.

Nothing here dispatches, writes a repository, writes Drive or touches an ADM
record. Verification happens only through the probes the caller passes; with
no probes, claims stay claims (and therefore cannot complete a task).
"""

from __future__ import annotations

from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import default_atlas
from manager.nextplan.extract import extract
from manager.nextplan.planner import apply, plan
from manager.nextplan.verify import verify

ENVELOPE_KEYS = ("event_id", "task_id", "role", "session_id", "agent", "generation", "format", "content")


def process(state, envelope, context=None, probes=(), execution=None, orchestration_signals=(), atlas=None):
    """Run one agent output through the pipeline. Pure apart from the probes."""
    missing = [key for key in ("event_id", "task_id", "role", "generation") if envelope.get(key) is None]
    if missing:
        raise ValueError(f"envelope is missing {', '.join(missing)}")
    atlas = atlas or default_atlas()
    reported = extract(envelope)
    verified, report = verify(reported, context or {}, probes) if probes else (reported, None)
    event = {
        "event_id": envelope["event_id"], "task_id": envelope["task_id"], "kind": "result",
        "role": envelope["role"], "session_id": envelope.get("session_id"), "agent": envelope.get("agent"),
        "generation": envelope["generation"], "result": verified, "verification": report,
        "execution": execution, "orchestration_signals": list(orchestration_signals),
    }
    return {"reported": reported, "verified": verified, "verification": report, "event": event,
            "decision": plan(state, event, atlas)}


def advance(state, envelope, **kwargs):
    """``process`` plus the resulting task state."""
    record = process(state, envelope, **kwargs)
    record["state_after"] = apply(state, record["event"], record["decision"])
    return record


def replay(state, envelopes, **kwargs):
    """Fold a sequence of agent outputs into (final state, records)."""
    records = []
    for envelope in envelopes:
        record = advance(state, envelope, **kwargs)
        state = record["state_after"]
        records.append(record)
    return state, records


def audit_trail(record):
    """A compact, human-readable account of why this decision was made."""
    decision, extraction = record["decision"], record["verified"]["extraction"]
    return {
        "action": decision["action"],
        "next_state": decision["next_state"],
        "reason": decision["reason"],
        "failure_code": decision["failure_code"],
        "failures": decision["failures"],
        "signals": decision["signals"],
        "extraction_tier": extraction["tier"],
        "probes_run": (record["verification"] or {}).get("probes_run", []),
        "budget": decision["budget"],
        "input_digest": decision["input_digest"],
    }
