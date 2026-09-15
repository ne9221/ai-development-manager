"""The closed vocabularies every NextPlan module shares.

Kept in one place so the Failure Atlas validator, the classifier and the
planner cannot drift apart on what an action, a state or an evidence level is.
"""

# -- Planner actions (the only things NextPlan can ever decide) --------------

CONTINUE_WORKER = "CONTINUE_WORKER"
SEND_TO_REVIEW = "SEND_TO_REVIEW"
RETURN_TO_WORKER = "RETURN_TO_WORKER"
RETRY_SAME_AGENT = "RETRY_SAME_AGENT"
REROUTE_AGENT = "REROUTE_AGENT"
WAIT_DEPENDENCY = "WAIT_DEPENDENCY"
HUMAN_GATE = "HUMAN_GATE"
MARK_BLOCKED = "MARK_BLOCKED"
MARK_FAILED = "MARK_FAILED"
MARK_COMPLETE = "MARK_COMPLETE"
NO_OP = "NO_OP"

ACTIONS = (
    CONTINUE_WORKER, SEND_TO_REVIEW, RETURN_TO_WORKER, RETRY_SAME_AGENT,
    REROUTE_AGENT, WAIT_DEPENDENCY, HUMAN_GATE, MARK_BLOCKED, MARK_FAILED,
    MARK_COMPLETE, NO_OP,
)

# Actions that let automation carry on without a person. MARK_COMPLETE is in
# here because it is the one automated *terminal* action; no failure may ever
# route to it.
AUTOMATED_ACTIONS = frozenset({
    CONTINUE_WORKER, SEND_TO_REVIEW, RETURN_TO_WORKER, RETRY_SAME_AGENT,
    REROUTE_AGENT, WAIT_DEPENDENCY, MARK_COMPLETE,
})

# Actions that start another round of work or waiting. When a failure routes
# to one of these, that round must be paid for from a finite budget, or the
# loop could run forever.
ROUND_CONSUMING_ACTIONS = frozenset({
    CONTINUE_WORKER, SEND_TO_REVIEW, RETURN_TO_WORKER, RETRY_SAME_AGENT,
    REROUTE_AGENT, WAIT_DEPENDENCY,
})

# Actions that stop automation for this task until something outside the
# planner changes.
HOLD_ACTIONS = frozenset({HUMAN_GATE, MARK_BLOCKED, MARK_FAILED})

# What a failure may do once its budget is spent.
UNRESOLVED_ACTIONS = frozenset({HUMAN_GATE, MARK_BLOCKED, MARK_FAILED})

# -- Normalized task states --------------------------------------------------

NEW = "NEW"
WORKING = "WORKING"
AWAITING_REVIEW = "AWAITING_REVIEW"
REPAIRING = "REPAIRING"
WAITING = "WAITING"
BLOCKED = "BLOCKED"
HUMAN_GATE_STATE = "HUMAN_GATE"
FAILED = "FAILED"
COMPLETE = "COMPLETE"
UNCHANGED = "UNCHANGED"

STATES = (NEW, WORKING, AWAITING_REVIEW, REPAIRING, WAITING, BLOCKED, HUMAN_GATE_STATE, FAILED, COMPLETE)
TERMINAL_STATES = frozenset({COMPLETE, FAILED})
HELD_STATES = frozenset({BLOCKED, HUMAN_GATE_STATE})
NEXT_STATES = tuple(s for s in STATES if s != NEW) + (UNCHANGED,)

# The state an action leaves the task in. UNCHANGED means "same phase": a
# retry or reroute of a reviewer stays AWAITING_REVIEW, of a worker WORKING.
ACTION_NEXT_STATE = {
    CONTINUE_WORKER: WORKING,
    SEND_TO_REVIEW: AWAITING_REVIEW,
    RETURN_TO_WORKER: REPAIRING,
    RETRY_SAME_AGENT: UNCHANGED,
    REROUTE_AGENT: UNCHANGED,
    WAIT_DEPENDENCY: WAITING,
    HUMAN_GATE: HUMAN_GATE_STATE,
    MARK_BLOCKED: BLOCKED,
    MARK_FAILED: FAILED,
    MARK_COMPLETE: COMPLETE,
    NO_OP: UNCHANGED,
}

# -- Roles --------------------------------------------------------------------

WORKER = "worker"
REVIEWER = "reviewer"
ROLES = (WORKER, REVIEWER)

# -- Evidence levels ------------------------------------------------------------

VERIFIED = "VERIFIED"
REPORTED = "REPORTED"
DERIVED = "DERIVED"
CONTRADICTED = "CONTRADICTED"
UNKNOWN = "UNKNOWN"
LEVELS = (VERIFIED, REPORTED, DERIVED, CONTRADICTED, UNKNOWN)

# -- Failure Atlas axes -----------------------------------------------------------

CATEGORIES = ("repo_git", "agent_execution", "test_review", "ssot_sync", "orchestration")
SEVERITIES = ("critical", "high", "medium", "low")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
BUDGETS = ("per_code", "repair", "review")
