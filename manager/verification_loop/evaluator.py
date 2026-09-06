"""Phase B-1 deterministic reference evaluator.

evaluate() is a pure function: same (task, execution, bundle, reports) in,
same EvaluationResult out. No I/O, no clock, no randomness, no real
provider/Excel/Drive/GitHub/screenshot/Session Center calls -- those are
represented purely as fixture fields (evidence_source, identity_resolution,
coverage_dimensions, ...) so this slice can prove the *admission and
classification logic* is sound before any real checker is wired in.

Architectural constraints this function enforces (Phase B-1 spec section C):
  1. No LoopRun is created here -- this module has no persistence at all.
  2. acceptance_state is always recomputed; there is no field on any input
     fixture that can set it directly (EvaluationResult.__post_init__ pins
     the ACCEPTED/next_action correspondence one more time as a hard check).
  3. Namespaced as "verification_loop", not "acceptance-gate" /
     "CONTROLLED_ACCEPTANCE_GATE" (see manager/acceptance_gate.py) -- an
     unrelated pre-existing mechanism.
  9. effective_risk = max(declared, diff-derived) -- see _effective_risk;
     it only ever escalates.
 10. RETRY_SAME_CANDIDATE is reachable only when the admitted failure-class
     set is exactly {ENVIRONMENT_TRANSIENT}.
 11. UNKNOWN outranks REPAIR in the next_action priority order.
 12. effective_risk == "production" can only ever resolve to ROUTE_TO_PFP;
     ACCEPTED is unreachable on that path.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Set, Tuple

from .models import (
    ADMITTED_FAILURE_CLASSES,
    TRUSTED_EVIDENCE_SOURCES,
    AcceptanceBundleFixture,
    EvaluationResult,
    ExecutionFixture,
    FailureObservation,
    TaskFixture,
    VerificationReportFixture,
    risk_rank,
)


def _effective_risk(task: TaskFixture, execution: ExecutionFixture, bundle: AcceptanceBundleFixture) -> str:
    best_rank = risk_rank(task.declared_risk)
    for rule in bundle.risk_rules:
        if any(p.startswith(rule.path_prefix) for p in execution.diff_paths):
            best_rank = max(best_rank, risk_rank(rule.forced_risk))
    from .models import RISK_LEVELS
    return RISK_LEVELS[best_rank]


def _report_key(report: VerificationReportFixture, index: int) -> str:
    return f"{report.gate_id}:r{report.round}:{index}"


def _admissibility_reason(
    report: VerificationReportFixture,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
) -> str | None:
    """Return None if admissible, else the single (first-tripped) reason it
    is not. Checked in a fixed order so the reason is deterministic."""
    if report.execution_id != execution.execution_id:
        return "EXECUTION_ID_MISMATCH"
    if report.candidate_sha != execution.candidate_sha:
        return "CANDIDATE_SHA_MISMATCH"
    if report.base_sha != execution.base_sha:
        return "BASE_SHA_MISMATCH"
    if report.bundle_hash != bundle.bundle_hash:
        return "BUNDLE_HASH_MISMATCH"
    if report.gate_id not in bundle.checker_identities:
        return "UNKNOWN_GATE"
    expected_identity, expected_version = bundle.checker_identities[report.gate_id]
    if report.checker_identity != expected_identity:
        return "CHECKER_IDENTITY_MISMATCH"
    if report.checker_version != expected_version:
        return "CHECKER_VERSION_MISMATCH"
    # B5: an unresolved executor identity must never be treated as "safe
    # independence". None means "we don't know who executed this", not "no
    # conflict" -- a report can only be admitted once we can actually check
    # it against the executor. The full (provider, account, session) triple
    # is Phase B-2; for B-1R, None alone is enough to fail closed.
    if execution.executor_identity is None:
        return "EXECUTOR_IDENTITY_UNRESOLVED"
    if report.checker_identity == execution.executor_identity:
        return "EXECUTOR_EQUALS_CHECKER"
    expected_oracle_hash = bundle.frozen_oracle_hashes.get(report.gate_id)
    if expected_oracle_hash is not None and report.oracle_hash != expected_oracle_hash:
        return "FROZEN_ORACLE_HASH_MISMATCH"
    return None


def _classify_observation(
    observation: FailureObservation,
    gate_id: str,
    bundle: AcceptanceBundleFixture,
) -> str:
    # B1: category/proposed_class are proposals only -- an observation's own
    # self-declared category is NEVER trusted directly. Only these
    # deterministic, evidence-backed predicates may decide the admitted
    # class: an allowlisted transient signature, or a signature the frozen
    # contract already has a clause for.
    if observation.signature in bundle.transient_allowlist:
        return "ENVIRONMENT_TRANSIENT"
    clauses = bundle.gate_contract_clauses.get(gate_id, ())
    if observation.signature in clauses:
        return "REGRESSION" if observation.is_regression else "CONTRACT_VIOLATION"
    if observation.category == "TEST_DEFECT":
        # No TD-1/TD-2 predicate exists yet (Phase B-2) to independently
        # verify a "this is just a flaky test" claim. Without one, a
        # report-authored TEST_DEFECT claim must not be admitted, and must
        # not be softened into the weaker ACCEPTANCE_CONTRACT_DEFECT default
        # either -- fail closed to UNKNOWN/human judgement instead.
        return "UNKNOWN"
    return "ACCEPTANCE_CONTRACT_DEFECT"


def _coverage_complete(report: VerificationReportFixture, bundle: AcceptanceBundleFixture) -> bool:
    required = set(bundle.gate_required_dimensions.get(report.gate_id, ()))
    if not required:
        return True
    return required.issubset(set(report.coverage_dimensions))


def _retry_eligible(admitted_classes: Set[str]) -> bool:
    """Rule 10: transient retry is reachable only when the admitted
    failure-class set is *exactly* {ENVIRONMENT_TRANSIENT} -- never when it
    is a superset (e.g. REGRESSION alongside a genuinely transient signal
    must never retry the same candidate; the regression needs a real fix).
    Pulled out as its own predicate so this exact-match requirement is
    directly unit-testable, independent of the surrounding next_action
    branch order."""
    return admitted_classes == {"ENVIRONMENT_TRANSIENT"}


def _report_is_trustworthy(report: VerificationReportFixture, bundle: AcceptanceBundleFixture) -> bool:
    if report.evidence_source not in TRUSTED_EVIDENCE_SOURCES:
        return False
    if report.identity_resolution != "resolved":
        return False
    if not _coverage_complete(report, bundle):
        return False
    return True


def _gate_outcome(
    gate_id: str,
    report: VerificationReportFixture,
    bundle: AcceptanceBundleFixture,
) -> Tuple[bool, Set[str]]:
    """(satisfied, contributed_classes) for one admissible latest-round
    report on a gate. Pulled out so the same rules apply whether or not the
    gate is required for the current risk tier (B3)."""
    trustworthy = _report_is_trustworthy(report, bundle)
    if report.result == "PASS":
        if trustworthy:
            return True, set()
        # A claimed-complete gate whose evidence cannot be trusted is a live
        # false-complete signal, not merely "not yet run" -- it must
        # escalate, never silently fall back to CONTINUE_VERIFICATION.
        return False, {"UNKNOWN"}
    if report.result == "FAIL":
        if not trustworthy or not report.failure_observations:
            return False, {"UNKNOWN"}
        return False, {_classify_observation(o, gate_id, bundle) for o in report.failure_observations}
    return False, {"UNKNOWN"}  # explicit "UNKNOWN" result


def _latest_admissible_by_gate(
    admissible: Sequence[Tuple[str, VerificationReportFixture]],
) -> Tuple[Dict[str, VerificationReportFixture], Set[str]]:
    """B2: group admissible reports by gate and pick the highest-round one
    per gate. If a gate's highest admissible round carries more than one
    report, that gate's outcome cannot be decided without depending on
    input list order -- such gates are returned separately as
    "duplicate_gates" instead of silently picking one (last-wins)."""
    by_gate: Dict[str, List[VerificationReportFixture]] = {}
    for _, report in admissible:
        by_gate.setdefault(report.gate_id, []).append(report)

    latest_by_gate: Dict[str, VerificationReportFixture] = {}
    duplicate_gates: Set[str] = set()
    for gate_id, reports_for_gate in by_gate.items():
        max_round = max(r.round for r in reports_for_gate)
        at_max = [r for r in reports_for_gate if r.round == max_round]
        if len(at_max) > 1:
            duplicate_gates.add(gate_id)
        else:
            latest_by_gate[gate_id] = at_max[0]
    return latest_by_gate, duplicate_gates


def _derive_next_action(
    admitted_classes: Set[str],
    missing_required_gates: Sequence[str],
    satisfied_gates: Sequence[str],
    required_gates: Sequence[str],
) -> str:
    """B4 guard order, most severe first. Extracted (like _retry_eligible)
    so the priority order itself is directly unit-testable independent of
    whatever admission path a given admitted class arrived through:
      1. GOVERNANCE_CONFLICT       -> ESCALATE_HUMAN
      2. PRODUCTION_SCOPE_VIOLATION -> ROUTE_TO_PFP (never masked by a
         co-occurring CONTRACT_VIOLATION/REGRESSION, never downgraded to
         ESCALATE_HUMAN)
      3. ACCEPTANCE_CONTRACT_DEFECT -> OPEN_BUNDLE_REVISION
      4. UNKNOWN                    -> ESCALATE_HUMAN
      5. REGRESSION/CONTRACT_VIOLATION/TEST_DEFECT -> REPAIR
      6. exactly {ENVIRONMENT_TRANSIENT} -> RETRY_SAME_CANDIDATE
      7. still missing required coverage -> CONTINUE_VERIFICATION
      8. full required coverage, nothing admitted -> ACCEPTED
    The fail-closed default (9) never invents an extra UNKNOWN on top of an
    admitted class that already explains the outcome -- it only adds one
    when admitted_classes was genuinely empty (no required gates declared
    for this risk tier and nothing failed).
    """
    if "GOVERNANCE_CONFLICT" in admitted_classes:
        return "ESCALATE_HUMAN"
    if "PRODUCTION_SCOPE_VIOLATION" in admitted_classes:
        return "ROUTE_TO_PFP"
    if "ACCEPTANCE_CONTRACT_DEFECT" in admitted_classes:
        return "OPEN_BUNDLE_REVISION"
    if "UNKNOWN" in admitted_classes:
        return "ESCALATE_HUMAN"
    if admitted_classes & {"CONTRACT_VIOLATION", "REGRESSION", "TEST_DEFECT"}:
        return "REPAIR"
    if _retry_eligible(admitted_classes):
        return "RETRY_SAME_CANDIDATE"
    if missing_required_gates:
        return "CONTINUE_VERIFICATION"
    if required_gates and set(satisfied_gates) == set(required_gates) and not admitted_classes:
        return "ACCEPTED"
    if admitted_classes:
        return "ESCALATE_HUMAN"
    admitted_classes.add("UNKNOWN")
    return "ESCALATE_HUMAN"


def evaluate(
    task: TaskFixture,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
    reports: Sequence[VerificationReportFixture],
) -> EvaluationResult:
    effective_risk = _effective_risk(task, execution, bundle)

    invalidated: List[Tuple[str, str]] = []
    admissible: List[Tuple[str, VerificationReportFixture]] = []
    for i, report in enumerate(reports):
        key = _report_key(report, i)
        reason = _admissibility_reason(report, execution, bundle)
        if reason is not None:
            invalidated.append((key, reason))
        else:
            admissible.append((key, report))

    admitted_classes: Set[str] = set()

    if effective_risk == "production":
        admitted_classes.add("PRODUCTION_SCOPE_VIOLATION")
        return EvaluationResult(
            execution_id=execution.execution_id,
            candidate_sha=execution.candidate_sha,
            base_sha=execution.base_sha,
            acceptance_bundle_hash=bundle.bundle_hash,
            effective_risk=effective_risk,
            admissible_report_keys=tuple(sorted(k for k, _ in admissible)),
            admitted_failure_classes=("PRODUCTION_SCOPE_VIOLATION",),
            missing_required_gates=(),
            invalidated_report_reasons=tuple(invalidated),
            next_action="ROUTE_TO_PFP",
            acceptance_state="NOT_ACCEPTED",
        )

    required_gates = tuple(bundle.gate_requirements_by_risk.get(effective_risk, ()))

    # B2: latest (highest round) admissible report per gate is authoritative
    # -- but a gate whose highest round has more than one admissible report
    # is ambiguous and must never be resolved by input list order.
    latest_by_gate, duplicate_gates = _latest_admissible_by_gate(admissible)

    missing_required_gates: List[str] = []
    satisfied_gates: List[str] = []

    for gate_id in required_gates:
        if gate_id in duplicate_gates:
            admitted_classes.add("UNKNOWN")
            continue
        report = latest_by_gate.get(gate_id)
        if report is None:
            missing_required_gates.append(gate_id)
            continue
        satisfied, classes = _gate_outcome(gate_id, report, bundle)
        admitted_classes |= classes
        if satisfied:
            satisfied_gates.append(gate_id)

    # B3: required_gates only decides *coverage* (missing vs satisfied). Any
    # other admissible gate report -- required for this risk tier or not --
    # must still be able to contribute a failure class; it must never be
    # silently discarded just because the risk tier didn't require it.
    for gate_id in set(latest_by_gate) | duplicate_gates:
        if gate_id in required_gates:
            continue  # already processed above
        if gate_id in duplicate_gates:
            admitted_classes.add("UNKNOWN")
            continue
        report = latest_by_gate[gate_id]
        _satisfied, classes = _gate_outcome(gate_id, report, bundle)
        admitted_classes |= classes

    next_action = _derive_next_action(
        admitted_classes, tuple(missing_required_gates), tuple(satisfied_gates), required_gates
    )
    acceptance_state = "ACCEPTED" if next_action == "ACCEPTED" else "NOT_ACCEPTED"

    return EvaluationResult(
        execution_id=execution.execution_id,
        candidate_sha=execution.candidate_sha,
        base_sha=execution.base_sha,
        acceptance_bundle_hash=bundle.bundle_hash,
        effective_risk=effective_risk,
        admissible_report_keys=tuple(sorted(k for k, _ in admissible)),
        admitted_failure_classes=tuple(sorted(admitted_classes)),
        missing_required_gates=tuple(missing_required_gates),
        invalidated_report_reasons=tuple(invalidated),
        next_action=next_action,
        acceptance_state=acceptance_state,
    )
