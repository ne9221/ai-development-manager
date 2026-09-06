"""The deterministic derivation: admissible reports in, single next action out.

``evaluate()`` is still a pure function -- same inputs, same result, no I/O, no
clock, no randomness. Everything that touches a filesystem lives in
``stores.py``; everything that would touch a provider, Excel, Drive, GitHub or
Session Center is represented as fixture data. That separation is what lets the
admission and classification logic be proven before any real checker exists.

The invariant this module protects is Phase A v3 A.2: **ACCEPTED is not a
stored field.** There is nowhere to write it. It is recomputed from the
admissible report set every single time, which is what makes contract revision
automatically invalidate old conclusions (STALE) and contradictory evidence
automatically overturn them (REVOKED) -- neither needs a cache-invalidation
step somebody could forget.

Guard order (Phase A v3 D.2, extended with the two states B-2 adds):

  1. GOVERNANCE_CONFLICT        -> ESCALATE_HUMAN
  2. PRODUCTION_SCOPE_VIOLATION -> ROUTE_TO_PFP
  3. round invalidated          -> INVALIDATE_ROUND
  4. prior acceptance REVOKED   -> ESCALATE_HUMAN
  5. ACCEPTANCE_CONTRACT_DEFECT -> OPEN_BUNDLE_REVISION
  6. UNKNOWN                    -> ESCALATE_HUMAN
  7. REGRESSION/CONTRACT_VIOLATION -> REPAIR
  8. TEST_DEFECT                -> REPAIR_TEST_ONLY
  9. exactly {ENVIRONMENT_TRANSIENT} -> RETRY_SAME_CANDIDATE
 10. required coverage incomplete -> CONTINUE_VERIFICATION
 11. full coverage, nothing admitted -> ACCEPTED
 12. anything else             -> ESCALATE_HUMAN

Two orderings are load-bearing and were chosen against convenience.
UNKNOWN outranks REPAIR because a repair changes candidate_sha, and any UNKNOWN
bound to the old SHA would evaporate with it -- so a trivial manufactured
regression could be used to make an inconvenient UNKNOWN disappear. And the
transient guard tests set *equality*, not membership, so a real defect
occurring alongside a genuinely transient one can never be retried away.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import budget as budget_module
from .admission import ROUND_INVALIDATING_REASONS, admissibility_reason, lineage_reason
from .bundle import canonical_json, report_digest, validate_bundle
from .classification import classify_observation
from .impact import affected_gates
from .models import (
    DERIVE_ALGO_VERSION,
    AcceptanceBundleFixture,
    EvaluationResult,
    ExecutionFixture,
    PreflightFacts,
    PriorAcceptance,
    TaskFixture,
    VerificationReportFixture,
    VerificationTicket,
)
from .risk import effective_risk, production_write_gates, required_gates
from .tickets import index_tickets

_ACCEPTANCE_STATE_BY_ACTION = {
    "ACCEPTED": "ACCEPTED",
    "ROUTE_TO_PFP": "DEFERRED_TO_PFP",
    "INVALIDATE_ROUND": "ROUND_INVALIDATED",
    "REPAIR": "REJECTED_NEEDS_REPAIR",
    "REPAIR_TEST_ONLY": "REJECTED_NEEDS_REPAIR",
    "OPEN_BUNDLE_REVISION": "BLOCKED_HUMAN",
    "ESCALATE_HUMAN": "BLOCKED_HUMAN",
    "RETRY_SAME_CANDIDATE": "IN_VERIFICATION",
    "CONTINUE_VERIFICATION": "IN_VERIFICATION",
}


def _report_key(report: VerificationReportFixture, index: int) -> str:
    return f"{report.gate_id}:r{report.round}:{index}"


def _coverage_complete(
    report: VerificationReportFixture, bundle: AcceptanceBundleFixture
) -> bool:
    required = set(bundle.gate_required_dimensions.get(report.gate_id, ()))
    if not required:
        return True
    return required.issubset(set(report.coverage_dimensions))


def _gate_outcome(
    gate_id: str,
    report: VerificationReportFixture,
    bundle: AcceptanceBundleFixture,
    execution: ExecutionFixture,
) -> Tuple[bool, Set[str], List[Tuple[str, str]]]:
    """``(satisfied, contributed_classes, regression_determinations)``.

    Applied to every admissible latest-per-gate report, required or not. Phase
    B-1 only classified failures on *required* gates, so an admissible FAIL on
    a gate the current tier did not require was discarded and the run was
    ACCEPTED -- the review reproduced exactly that with a freeze-pane defect.
    """
    determinations: List[Tuple[str, str]] = []

    if report.result == "DEFERRED_TO_PFP":
        # A checker that recognised it was looking at production. It cannot be
        # a PASS and must not be repaired inside this loop.
        return False, {"PRODUCTION_SCOPE_VIOLATION"}, determinations

    trustworthy = _coverage_complete(report, bundle)

    if report.result == "PASS":
        if trustworthy:
            return True, set(), determinations
        # A gate claiming completion on partial coverage is a live
        # false-complete signal, not "not yet run": it must escalate rather
        # than fall back to CONTINUE_VERIFICATION, which would look like
        # ordinary progress.
        return False, {"UNKNOWN"}, determinations

    if report.result == "FAIL":
        if not trustworthy or not report.failure_observations:
            return False, {"UNKNOWN"}, determinations
        classes: Set[str] = set()
        for observation in report.failure_observations:
            admitted, determination = classify_observation(
                observation, gate_id, bundle, execution, report
            )
            classes.add(admitted)
            determinations.append((observation.signature, determination))
        return False, classes, determinations

    return False, {"UNKNOWN"}, determinations


def _latest_admissible_by_gate(
    admissible: Sequence[Tuple[str, VerificationReportFixture]],
) -> Tuple[Dict[str, VerificationReportFixture], Set[str]]:
    """Highest-round admissible report per gate, refusing to break ties.

    A gate whose highest admissible round carries more than one report cannot
    be decided without depending on input list order, so it is returned as
    ambiguous instead. Ticket binding now makes this nearly unreachable -- the
    same (gate, round) implies the same ticket_id, and duplicate tickets are
    already rejected -- but it stays as defence in depth, since the alternative
    failure mode is a verdict that changes when the list is reordered.
    """
    by_gate: Dict[str, List[VerificationReportFixture]] = {}
    for _key, report in admissible:
        by_gate.setdefault(report.gate_id, []).append(report)

    latest: Dict[str, VerificationReportFixture] = {}
    duplicates: Set[str] = set()
    for gate_id, reports_for_gate in by_gate.items():
        max_round = max(r.round for r in reports_for_gate)
        at_max = [r for r in reports_for_gate if r.round == max_round]
        if len(at_max) > 1:
            duplicates.add(gate_id)
        else:
            latest[gate_id] = at_max[0]
    return latest, duplicates


def _partition_reports(
    reports: Sequence[VerificationReportFixture],
    tickets_by_id: Mapping[str, VerificationTicket],
    task: TaskFixture,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
    preflight: PreflightFacts,
) -> Tuple[
    List[Tuple[str, VerificationReportFixture]], List[Tuple[str, str]], bool
]:
    """Split reports into admissible and rejected, and detect round invalidation.

    Two reports claiming the same ticket are *both* rejected. One ticket
    authorises one answer; two answers to it mean at least one is unauthorised
    and there is no principled way to tell which, so neither is trusted.
    """
    seen_ticket_ids: Dict[str, List[int]] = {}
    for index, report in enumerate(reports):
        seen_ticket_ids.setdefault(report.ticket_id, []).append(index)
    duplicated = {tid for tid, idxs in seen_ticket_ids.items() if len(idxs) > 1}

    admissible: List[Tuple[str, VerificationReportFixture]] = []
    rejected: List[Tuple[str, str]] = []
    round_invalidated = False

    for index, report in enumerate(reports):
        key = _report_key(report, index)
        if report.ticket_id in duplicated:
            rejected.append((key, "DUPLICATE_REPORT_FOR_TICKET"))
            continue
        reason = admissibility_reason(
            report, tickets_by_id.get(report.ticket_id), task, execution, bundle, preflight
        )
        if reason is None:
            admissible.append((key, report))
            continue
        rejected.append((key, reason))
        if reason in ROUND_INVALIDATING_REASONS:
            # The tree moved under this round. Every verdict measured against
            # it is void, not just the report that happened to notice.
            round_invalidated = True

    return admissible, rejected, round_invalidated


def _derivation_key(
    bundle: AcceptanceBundleFixture,
    execution: ExecutionFixture,
    admissible: Sequence[Tuple[str, VerificationReportFixture]],
) -> str:
    digests = sorted(report_digest(report) for _key, report in admissible)
    return report_digest(
        canonical_json(
            {
                "algo": DERIVE_ALGO_VERSION,
                "bundle_hash": bundle.bundle_hash,
                "candidate_sha": execution.candidate_sha,
                "reports": digests,
            }
        )
    )


def _prior_status(
    prior: Optional[PriorAcceptance],
    bundle: AcceptanceBundleFixture,
    execution: ExecutionFixture,
    derivation_key: str,
) -> str:
    """CURRENT / STALE / REVOKED / NONE for a previously derived acceptance.

    Phase A v3 I3 insists these are different things. STALE means the inputs
    moved, so the old conclusion is no longer reproducible -- it implies no
    defect and needs no human. REVOKED means the inputs are identical and the
    evidence now contradicts itself, which does imply a defect and does need a
    human. Collapsing them would either cry wolf on every contract revision or
    silence a genuine contradiction.
    """
    if prior is None:
        return "NONE"
    if prior.derivation_key == derivation_key:
        return "CURRENT"
    if (
        prior.bundle_hash != bundle.bundle_hash
        or prior.candidate_sha != execution.candidate_sha
        or prior.governance_digest != bundle.governance_digest
    ):
        return "STALE"
    return "REVOKED"


def _retry_eligible(admitted: Set[str]) -> bool:
    """Transient retry needs the admitted set to be *exactly* {transient}.

    Set equality, not membership. A regression sitting alongside a genuinely
    transient failure must never retry the same candidate -- the regression
    needs a real fix, and retrying would keep re-observing it. Kept as its own
    named predicate so this exact-match requirement is directly unit-testable,
    independent of the surrounding guard order.
    """
    return admitted == {"ENVIRONMENT_TRANSIENT"}


def _derive_next_action(
    admitted: Set[str],
    round_invalidated: bool,
    prior_status: str,
    missing_required: Sequence[str],
    satisfied: Sequence[str],
    required: Sequence[str],
) -> str:
    if "GOVERNANCE_CONFLICT" in admitted:
        return "ESCALATE_HUMAN"
    if "PRODUCTION_SCOPE_VIOLATION" in admitted:
        return "ROUTE_TO_PFP"
    if round_invalidated:
        return "INVALIDATE_ROUND"
    if prior_status == "REVOKED":
        return "ESCALATE_HUMAN"
    if "ACCEPTANCE_CONTRACT_DEFECT" in admitted:
        return "OPEN_BUNDLE_REVISION"
    if "UNKNOWN" in admitted:
        return "ESCALATE_HUMAN"
    if admitted & {"REGRESSION", "CONTRACT_VIOLATION"}:
        return "REPAIR"
    if "TEST_DEFECT" in admitted:
        return "REPAIR_TEST_ONLY"
    if _retry_eligible(admitted):
        return "RETRY_SAME_CANDIDATE"
    if missing_required:
        return "CONTINUE_VERIFICATION"
    if required and set(satisfied) == set(required) and not admitted:
        return "ACCEPTED"
    # No required gates declared for this tier, or some other shape that does
    # not positively earn an acceptance. Fail closed to a human rather than
    # treating "nothing required" as "nothing to prove".
    return "ESCALATE_HUMAN"


def evaluate(
    task: TaskFixture,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
    reports: Sequence[VerificationReportFixture],
    *,
    preflight: PreflightFacts,
    tickets: Sequence[VerificationTicket] = (),
    chain: Sequence[ExecutionFixture] = (),
    chain_reports: Optional[Mapping[str, Sequence[VerificationReportFixture]]] = None,
    prior_acceptance: Optional[PriorAcceptance] = None,
) -> EvaluationResult:
    """Derive the acceptance state and the single next action for one Execution.

    ``preflight`` is required, not optional: without measured facts about the
    worktree and the manager home, the controller cannot prove it is not
    looking at production, and "could not check" must never read as "was fine".
    """
    admitted: Set[str] = set()
    determinations: List[Tuple[str, str]] = []

    # A malformed contract is a defect in the contract, never configuration to
    # obey: a non-monotone gate matrix, a hash that does not match its own
    # contents, or a gate with no checker all route to human bundle revision.
    for _reason in validate_bundle(bundle):
        admitted.add("ACCEPTANCE_CONTRACT_DEFECT")

    if lineage_reason(task, execution, bundle) is not None:
        admitted.add("UNKNOWN")

    if preflight.governance_digest_measured != bundle.governance_digest:
        admitted.add("GOVERNANCE_CONFLICT")

    risk, unreadable_paths = effective_risk(task, execution, bundle, preflight)
    if unreadable_paths:
        # A diff path we cannot canonicalise could be anywhere, so we cannot
        # prove which rules apply to it.
        admitted.add("UNKNOWN")

    required = required_gates(bundle, risk)

    tickets_by_id, _rejected_tickets = index_tickets(tickets)
    admissible, invalidated, round_invalidated = _partition_reports(
        reports, tickets_by_id, task, execution, bundle, preflight
    )

    latest_by_gate, duplicate_gates = _latest_admissible_by_gate(admissible)

    # Phase A v3 I5. Checked across every gate that was *answered*, not only
    # the ones this tier requires: a production-write gate the current tier
    # happens not to require would otherwise be reportable without ever
    # tripping the boundary. Its PASS could not fill a required slot, so it
    # was not a false-complete -- but a report about a production write is
    # itself the signal that this loop must not be the one deciding.
    answered_gates = set(required) | set(latest_by_gate) | duplicate_gates
    if risk == "production" or production_write_gates(bundle, sorted(answered_gates)):
        admitted.add("PRODUCTION_SCOPE_VIOLATION")

    missing_required: List[str] = []
    satisfied: List[str] = []
    for gate_id in required:
        if gate_id in duplicate_gates:
            admitted.add("UNKNOWN")
            continue
        report = latest_by_gate.get(gate_id)
        if report is None:
            missing_required.append(gate_id)
            continue
        gate_satisfied, classes, gate_determinations = _gate_outcome(
            gate_id, report, bundle, execution
        )
        admitted |= classes
        determinations.extend(gate_determinations)
        if gate_satisfied:
            satisfied.append(gate_id)

    # Required gates decide *coverage* only. Any other admissible gate report
    # still contributes its failure classes; it must never be dropped merely
    # because the current risk tier did not ask for it.
    for gate_id in sorted(set(latest_by_gate) | duplicate_gates):
        if gate_id in required:
            continue
        if gate_id in duplicate_gates:
            admitted.add("UNKNOWN")
            continue
        _satisfied, classes, gate_determinations = _gate_outcome(
            gate_id, latest_by_gate[gate_id], bundle, execution
        )
        admitted |= classes
        determinations.extend(gate_determinations)

    derivation_key = _derivation_key(bundle, execution, admissible)
    prior_status = _prior_status(prior_acceptance, bundle, execution, derivation_key)

    next_action = _derive_next_action(
        admitted, round_invalidated, prior_status, missing_required, satisfied, required
    )

    # Budgets are folded over the whole Execution chain, so a repair -- which
    # is by definition a new Execution -- cannot reset the budget it spends.
    effective_chain = tuple(chain) or (execution,)
    reports_by_execution = dict(chain_reports or {})
    reports_by_execution.setdefault(execution.execution_id, tuple(reports))
    exhausted = budget_module.consumption(
        effective_chain, reports_by_execution
    ).exhausted_against(bundle.budget)
    blocked, blocking = budget_module.blocks(next_action, exhausted)
    if blocked:
        next_action = "ESCALATE_HUMAN"

    acceptance_state = _ACCEPTANCE_STATE_BY_ACTION[next_action]
    if acceptance_state == "IN_VERIFICATION" and not admissible:
        acceptance_state = "PENDING"
    if prior_status == "REVOKED":
        acceptance_state = "REVOKED"
    elif prior_status == "STALE" and next_action != "ACCEPTED":
        # The earlier acceptance no longer follows from current inputs. If the
        # current inputs independently earn an acceptance, that is a new and
        # genuine one, not a stale one.
        acceptance_state = "STALE"

    rerun, _policy = affected_gates(execution.diff_paths, bundle, required)

    return EvaluationResult(
        execution_id=execution.execution_id,
        task_id=task.task_id,
        candidate_sha=execution.candidate_sha,
        base_sha=execution.base_sha,
        acceptance_bundle_hash=bundle.bundle_hash,
        effective_risk=risk,
        required_gates=tuple(required),
        satisfied_gates=tuple(satisfied),
        admissible_report_keys=tuple(sorted(key for key, _ in admissible)),
        admitted_failure_classes=tuple(sorted(admitted)),
        missing_required_gates=tuple(missing_required),
        invalidated_report_reasons=tuple(invalidated),
        regression_determinations=tuple(sorted(set(determinations))),
        rerun_gates=tuple(rerun),
        budget_exhausted=tuple(blocking) if blocked else (),
        prior_acceptance_status=prior_status,
        derivation_key=derivation_key,
        next_action=next_action,
        acceptance_state=acceptance_state,
    )
