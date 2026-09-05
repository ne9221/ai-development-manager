"""Verification Loop v5 executable reference kernel.

Not production runtime. Not an activation path.
ACCEPTED is derived by decide(); it is never a candidate-writable field.
"""

from .kernel import (
    KERNEL_ID,
    KERNEL_V5_AUTHORITATIVE,
    V5_STATE_MODEL,
    OBLIGATION_STATE_MODEL,
    CONTROLLER_TRUST_ROOT,
    REVIEWER_OUTPUT_ROLE,
    REVIEW_CONTEXT_BINDING,
    INVALIDATION_POLICY,
    HUMAN_ESCALATION_CLOSED_SET,
    State,
    Event,
    ObligationState,
    World,
    Decision,
    open_task,
    apply,
    decide,
    controller_trust_digest,
)

from .harness import (
    harness_gate,
    HarnessReport,
    HARNESS_REQUIRED_AGENTS,
)

__all__ = [
    "KERNEL_ID",
    "KERNEL_V5_AUTHORITATIVE",
    "V5_STATE_MODEL",
    "OBLIGATION_STATE_MODEL",
    "CONTROLLER_TRUST_ROOT",
    "REVIEWER_OUTPUT_ROLE",
    "REVIEW_CONTEXT_BINDING",
    "INVALIDATION_POLICY",
    "HUMAN_ESCALATION_CLOSED_SET",
    "State",
    "Event",
    "ObligationState",
    "World",
    "Decision",
    "open_task",
    "apply",
    "decide",
    "controller_trust_digest",
    "harness_gate",
    "HarnessReport",
    "HARNESS_REQUIRED_AGENTS",
]
