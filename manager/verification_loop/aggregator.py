"""Task close eligibility: one accepted Execution is not a closed Task.

Phase A v3 A.5 states the rule this module implements in one line: **a Task's
deliverable is one SHA, not a set of PASSes.** Early Executions that passed are
history, not credit. An acceptance assembled from V3 measured at one SHA and V4
measured at another cannot satisfy the predicate below, not because a check
rejects it but because the predicate is written against a single
``deliverable_sha`` and there is nothing for the mismatched evidence to attach
to.

The cost is real and worth naming: the final SHA must carry a complete
required-gate run of its own. Targeted re-runs during intermediate repair
rounds (see impact.py) save money in the middle and buy nothing at the end.
That is the price of closing the failure shape where a late one-line change
invalidates a suite of goldens that had already gone green.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from .models import EvaluationResult, ExecutionFixture, TaskCloseDecision, TaskFixture


def task_close_eligibility(
    task: TaskFixture,
    executions: Sequence[ExecutionFixture],
    evaluations: Mapping[str, EvaluationResult],
    *,
    current_governance_digest: str,
    unresolved_blockers: Sequence[str] = (),
    pending_pfp: bool = False,
) -> TaskCloseDecision:
    """Whether ``task`` may be closed, and every reason it may not.

    All blocking reasons are collected rather than short-circuiting on the
    first. A caller that fixes one blocker should be able to see the rest
    without another round trip, and a reviewer should see the full picture at
    once rather than a sequence of single objections.
    """
    reasons: list[str] = []

    if not task.deliverable_sha:
        # Without a declared deliverable there is no SHA for evidence to be
        # bound to, so "which PASSes count" would be an open question.
        reasons.append("NO_DELIVERABLE_SHA")

    accepted_execution_id: Optional[str] = None
    stale_or_revoked: list[str] = []

    for execution in executions:
        evaluation = evaluations.get(execution.execution_id)
        if evaluation is None:
            continue
        if evaluation.prior_acceptance_status in ("STALE", "REVOKED"):
            stale_or_revoked.append(
                execution.execution_id + ":" + evaluation.prior_acceptance_status
            )
        if (
            task.deliverable_sha
            and execution.candidate_sha == task.deliverable_sha
            and evaluation.acceptance_state == "ACCEPTED"
            and not evaluation.missing_required_gates
            and evaluation.required_gates
            and set(evaluation.required_gates) <= set(evaluation.satisfied_gates)
        ):
            accepted_execution_id = execution.execution_id

    if task.deliverable_sha and accepted_execution_id is None:
        reasons.append("NO_ACCEPTED_EXECUTION_AT_DELIVERABLE_SHA")

    for entry in sorted(stale_or_revoked):
        reasons.append("EVIDENCE_" + entry.split(":", 1)[1] + ":" + entry.split(":", 1)[0])

    if accepted_execution_id is not None:
        evaluation = evaluations[accepted_execution_id]
        if evaluation.budget_exhausted:
            reasons.append("BUDGET_EXHAUSTED")
        if evaluation.effective_risk == "production":
            reasons.append("PRODUCTION_RISK_REQUIRES_PFP")

    for blocker in unresolved_blockers:
        reasons.append("UNRESOLVED_BLOCKER:" + blocker)

    if pending_pfp:
        reasons.append("PENDING_PRODUCTION_FIX_PROTOCOL")

    if current_governance_digest and accepted_execution_id is not None:
        # Governance moving after an acceptance does not retroactively make the
        # work wrong, but it does mean the acceptance was derived against rules
        # that are no longer current, so the Task cannot close on it.
        evaluation = evaluations[accepted_execution_id]
        if evaluation.prior_acceptance_status == "STALE":
            reasons.append("GOVERNANCE_OR_BUNDLE_NOT_CURRENT")

    return TaskCloseDecision(
        task_id=task.task_id,
        deliverable_sha=task.deliverable_sha,
        close_eligible=not reasons,
        blocking_reasons=tuple(reasons),
        accepted_execution_id=accepted_execution_id,
    )
