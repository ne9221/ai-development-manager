"""Retry / repair / review budgets, folded over the Execution chain.

Phase B-1R left retry unbounded, which the re-review recorded as a residual:
nothing capped a RETRY_SAME_CANDIDATE loop. Bounding it is only half the job
though. The interesting failure is subtler -- if the counters lived on the
current Execution, then "repair" (which by definition creates a *new*
Execution) would reset its own budget every time it was spent. An unbounded
loop would reappear wearing a different shape.

So the counters are a fold over the whole chain, not state on one Execution.
They are recomputable from Execution lineage at any time, which is also why
this does not constitute a second source of truth: there is nothing to write,
only something to count. The limits themselves live in the frozen bundle, so
raising one changes ``bundle_hash`` and requires a human-approved revision --
a budget the budgeted party can raise is not a budget.

Retry and repair are counted through two distinct fields on purpose.
``retry_of_execution_id`` means "same candidate, transient failure";
``repair_of_execution_id`` means "new candidate, real fix". Conflating them is
precisely how a repair loop launders itself into an unbounded retry loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

from .models import Budget, ExecutionFixture, VerificationReportFixture

# Actions that begin a further verification round and therefore draw down
# budget. CONTINUE_VERIFICATION is deliberately absent: filling in gates that
# were never run is completing the current round, not starting another one.
ROUND_CONSUMING_ACTIONS = ("REPAIR", "REPAIR_TEST_ONLY", "RETRY_SAME_CANDIDATE")


@dataclass(frozen=True)
class BudgetConsumption:
    transient_retries: int
    repair_executions: int
    review_rounds: int

    def exhausted_against(self, budget: Budget) -> Tuple[str, ...]:
        exhausted: list[str] = []
        if self.transient_retries >= budget.max_transient_retries:
            exhausted.append("transient_retries")
        if self.repair_executions >= budget.max_repair_executions:
            exhausted.append("repair_executions")
        if self.review_rounds >= budget.max_review_rounds:
            exhausted.append("review_rounds")
        return tuple(exhausted)


def consumption(
    chain: Sequence[ExecutionFixture],
    chain_reports: Mapping[str, Sequence[VerificationReportFixture]],
) -> BudgetConsumption:
    """Count what the whole Execution chain has already spent.

    ``chain`` must contain every Execution for the Task, including the current
    one and every earlier repair. Passing only the current Execution is how the
    reset-by-new-Execution bug comes back, so callers that hold the lineage are
    expected to pass all of it.
    """
    retries = sum(1 for e in chain if e.retry_of_execution_id)
    repairs = sum(1 for e in chain if e.repair_of_execution_id)

    rounds = 0
    for reports in chain_reports.values():
        for report in reports:
            rounds = max(rounds, int(report.round))

    return BudgetConsumption(
        transient_retries=retries,
        repair_executions=repairs,
        review_rounds=rounds,
    )


def blocks(action: str, exhausted: Sequence[str]) -> Tuple[bool, Tuple[str, ...]]:
    """Whether ``action`` is blocked, and by which exhausted budgets.

    A blocked action becomes ESCALATE_HUMAN rather than being retried anyway or
    silently downgraded -- Phase A v3's post-filter keeps the decision
    single-valued while making the reason legible.
    """
    if action not in ROUND_CONSUMING_ACTIONS:
        return False, ()

    relevant: list[str] = []
    if "review_rounds" in exhausted:
        relevant.append("review_rounds")
    if action == "RETRY_SAME_CANDIDATE" and "transient_retries" in exhausted:
        relevant.append("transient_retries")
    if action in ("REPAIR", "REPAIR_TEST_ONLY") and "repair_executions" in exhausted:
        relevant.append("repair_executions")

    return bool(relevant), tuple(relevant)
