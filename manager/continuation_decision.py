"""Pure decision engine for the autonomous continuation foundation.

No I/O, no provider calls, no dispatch. Every function here takes plain data
in and returns a Decision out; manager/continuation_orchestrator.py is the
only caller allowed to persist a Decision's outcome or act on it.
"""

from manager.continuation_states import ContinuationError, validate_transition

# Action kinds that must always stop for a human, regardless of validated
# evidence or remaining depth/retry budget -- these are the actions the task
# spec calls out as never eligible for automatic continuation.
IRREVERSIBLE_ACTION_KINDS = frozenset({"production_deploy", "auth", "payment", "irreversible"})


class Decision:
    __slots__ = ("event", "from_state", "to_state", "reason", "patch")

    def __init__(self, event, from_state, to_state, reason, patch=None):
        self.event = event
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        self.patch = dict(patch or {})

    def __repr__(self):
        return f"Decision({self.event!r}, {self.from_state!r} -> {self.to_state!r}, {self.reason!r})"

    def __eq__(self, other):
        if not isinstance(other, Decision):
            return NotImplemented
        return (self.event, self.from_state, self.to_state, self.reason, self.patch) == (
            other.event, other.from_state, other.to_state, other.reason, other.patch)


def _decision(event, from_state, to_state, reason, patch=None):
    validate_transition(event, from_state, to_state)
    return Decision(event, from_state, to_state, reason, patch)


def plan_task(from_state="idle"):
    """A new slice enters the chain."""
    return _decision("task_planned", from_state, "task_planned", "task planned for dispatch")


def _dispatch_gate(action_kind, duplicate_authority, is_production_mutating, active_production_mutating_count):
    """Shared authorization gate for any TASK_PLANNED -> DISPATCHING style
    transition. Both the initial dispatch (decide_dispatch) and every later
    continuation dispatch (decide_next_dispatch) call this exact function so
    the two paths cannot drift apart. Returns (outcome, reason) where
    outcome is one of "proceed", "hold", "stop". Read-only/prework
    candidates (is_production_mutating=False) are exempt from the
    single-production-writer mutual-exclusion check and may proceed in
    parallel with any number of active actors."""
    if duplicate_authority:
        return "stop", "duplicate request/task authority detected; requires user"
    if action_kind in IRREVERSIBLE_ACTION_KINDS:
        return "stop", f"action_kind={action_kind} is production/irreversible; requires user"
    if is_production_mutating and active_production_mutating_count > 0:
        return "hold", f"holding: {active_production_mutating_count} production-mutating actor(s) already active"
    return "proceed", "dispatch authorized"


def decide_dispatch(from_state, action_kind, duplicate_authority, is_production_mutating, active_production_mutating_count):
    """TASK_PLANNED -> DISPATCHING, gated by the same shared _dispatch_gate
    as decide_next_dispatch: irreversible-action, duplicate-request/task
    authority, and the single-production-writer mutual-exclusion rule all
    apply identically to the very first dispatch of a chain, not only to
    later continuations."""
    outcome, reason = _dispatch_gate(action_kind, duplicate_authority, is_production_mutating, active_production_mutating_count)
    if outcome == "stop":
        return _decision("dispatch_requested", from_state, "stop_requires_user", reason)
    if outcome == "hold":
        return _decision("dispatch_requested", from_state, "task_planned", reason)
    return _decision("dispatch_requested", from_state, "dispatching", reason)


def dispatch_accepted(from_state="dispatching"):
    return _decision("dispatch_accepted", from_state, "queued", "dispatch accepted; queued for a provider")


def dispatch_failed(from_state, retryable, reason):
    """A dispatch-time technical error (e.g. transient quota/API error). Not
    the same as a validated task failure -- always routes through the
    bounded-retry FAIL state rather than going straight to STOP, unless the
    caller marks it non-retryable."""
    if retryable:
        return _decision("dispatch_failed", from_state, "fail", reason or "dispatch failed; retryable")
    return _decision("dispatch_failed", from_state, "stop_requires_user", reason or "dispatch failed; not retryable")


def provider_started(from_state="queued"):
    return _decision("provider_started", from_state, "running", "provider launch confirmed")


def provider_terminal_evidence(from_state="running"):
    """The execution reached a terminal provider status and produced a
    completion report to validate."""
    return _decision("provider_terminal_evidence", from_state, "awaiting_evidence", "provider terminalized; evidence available")


def provider_crashed(from_state, reason):
    """The execution ended with no completion evidence at all (process
    crash, hard timeout, watcher-detected death). Always retryable-eligible
    via the FAIL state's own retry_check -- never guessed as a validated
    PASS or FAIL of the task itself."""
    return _decision("provider_crashed", from_state, "fail", reason or "provider crashed with no evidence")


def evidence_ready(from_state="awaiting_evidence"):
    return _decision("evidence_ready", from_state, "validating", "evidence handed to validation")


def classify_evidence(evidence):
    """Pure evidence classifier: pass requires an explicit result plus
    non-empty signals; anything else is ambiguous and must never be guessed
    as a pass."""
    if not isinstance(evidence, dict):
        return "blocked", "evidence missing or malformed; treated as ambiguous"
    result = evidence.get("result")
    reason = evidence.get("reason")
    if result not in ("pass", "fail", "ambiguous"):
        return "blocked", reason or "evidence result missing/unrecognized; treated as ambiguous"
    if result == "ambiguous":
        return "blocked", reason or "evidence explicitly marked ambiguous"
    signals = evidence.get("signals")
    if not signals:
        return "blocked", "evidence claimed a result with no supporting signals; treated as ambiguous"
    if result == "pass":
        return "pass", reason or "evidence confirms pass"
    return "fail", reason or "evidence confirms fail"


def decide_validation(from_state, evidence):
    outcome, reason = classify_evidence(evidence)
    return _decision("validation_result", from_state, outcome, reason,
                      patch={"retryable": bool(evidence.get("retryable"))} if outcome == "fail" and isinstance(evidence, dict) else None)


def decide_continuation(from_state, depth, max_depth):
    """PASS -> NEXT_TASK_READY, gated only by the configurable automatic
    continuation depth ceiling (never infinite)."""
    if not isinstance(max_depth, int) or max_depth < 1:
        raise ContinuationError("max_depth must be a positive integer")
    if depth + 1 > max_depth:
        return _decision("continuation_check", from_state, "stop_requires_user",
                          f"automatic continuation depth limit reached ({depth}/{max_depth})")
    return _decision("continuation_check", from_state, "next_task_ready",
                      f"advancing to next slice (depth {depth + 1}/{max_depth})", patch={"depth": depth + 1})


def decide_next_dispatch(from_state, action_kind, duplicate_authority, is_production_mutating, active_production_mutating_count):
    """NEXT_TASK_READY -> TASK_PLANNED, gated by the same shared
    _dispatch_gate as decide_dispatch: irreversible-action,
    duplicate-authority, and single-production-writer mutual exclusion.
    Read-only/prework candidates are exempt from mutual exclusion and may
    proceed in parallel."""
    outcome, reason = _dispatch_gate(action_kind, duplicate_authority, is_production_mutating, active_production_mutating_count)
    if outcome == "stop":
        return _decision("next_dispatch_check", from_state, "stop_requires_user", reason)
    if outcome == "hold":
        return _decision("next_dispatch_check", from_state, "next_task_ready", reason)
    return _decision("next_dispatch_check", from_state, "task_planned", "next slice authorized")


def decide_retry(from_state, retryable, retry_count, max_retries):
    """FAIL -> DISPATCHING (bounded retry) or STOP_REQUIRES_USER."""
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ContinuationError("max_retries must be a non-negative integer")
    if retryable and retry_count < max_retries:
        return _decision("retry_check", from_state, "dispatching",
                          f"retrying ({retry_count + 1}/{max_retries})", patch={"retry_count": retry_count + 1})
    if retryable:
        return _decision("retry_check", from_state, "stop_requires_user",
                          f"bounded retry limit reached ({retry_count}/{max_retries}); requires user")
    return _decision("retry_check", from_state, "stop_requires_user", "failure is not retryable; requires user")
