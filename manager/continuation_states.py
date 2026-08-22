"""State/data contract for the autonomous continuation foundation.

Foundation-only: this module defines the state machine's vocabulary (states,
legal transitions, events) that manager/continuation_decision.py and
manager/continuation_orchestrator.py build on. It does not dispatch, launch,
or mutate any provider or production system.
"""

STATES = (
    "idle", "task_planned", "dispatching", "queued", "running",
    "awaiting_evidence", "validating", "pass", "fail", "blocked",
    "next_task_ready", "stop_requires_user",
)

# States where automatic continuation halts and a human must act (either to
# resolve ambiguity or to authorize a stopped irreversible/duplicate/depth
# decision). Neither state has an automatic outgoing edge.
TERMINAL_FOR_AUTOMATION = frozenset({"blocked", "stop_requires_user"})

EVENTS = (
    "task_planned", "dispatch_requested", "dispatch_accepted", "dispatch_failed",
    "provider_started", "provider_terminal_evidence", "provider_crashed",
    "evidence_ready", "validation_result", "continuation_check",
    "next_dispatch_check", "retry_check",
)

# Directed edges the decision engine is allowed to produce, keyed by the
# event that drives the transition. A self-loop (from == to) is a "hold":
# the record stays put pending a condition, not an error and not progress.
TRANSITIONS = {
    "task_planned": {"idle": {"task_planned"}},
    "dispatch_requested": {"task_planned": {"dispatching", "stop_requires_user"}},
    "dispatch_accepted": {"dispatching": {"queued"}},
    "dispatch_failed": {"dispatching": {"fail", "stop_requires_user"}},
    "provider_started": {"queued": {"running"}},
    "provider_terminal_evidence": {"running": {"awaiting_evidence"}},
    "provider_crashed": {"running": {"fail"}},
    "evidence_ready": {"awaiting_evidence": {"validating"}},
    "validation_result": {"validating": {"pass", "fail", "blocked"}},
    "continuation_check": {"pass": {"next_task_ready", "stop_requires_user"}},
    "next_dispatch_check": {"next_task_ready": {"task_planned", "next_task_ready", "stop_requires_user"}},
    "retry_check": {"fail": {"dispatching", "stop_requires_user"}},
}


class ContinuationError(RuntimeError):
    pass


def validate_transition(event, current_state, next_state):
    if event not in TRANSITIONS:
        raise ContinuationError(f"unknown continuation event: {event!r}")
    if current_state not in STATES or next_state not in STATES:
        raise ContinuationError(f"unknown continuation state in transition: {current_state!r} -> {next_state!r}")
    legal_targets = TRANSITIONS[event].get(current_state)
    if legal_targets is None:
        raise ContinuationError(f"event {event!r} is not legal from state {current_state!r}")
    if next_state not in legal_targets:
        raise ContinuationError(f"illegal continuation transition for event {event!r}: {current_state} -> {next_state}")
    return next_state
