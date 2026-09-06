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
    if execution.executor_identity is not None and report.checker_identity == execution.executor_identity:
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
    if observation.category is not None:
        return observation.category
    if observation.signature in bundle.transient_allowlist:
        return "ENVIRONMENT_TRANSIENT"
    clauses = bundle.gate_contract_clauses.get(gate_id, ())
    if observation.signature in clauses:
        return "REGRESSION" if observation.is_regression else "CONTRACT_VIOLATION"
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

    # Latest (highest round) admissible report per gate is authoritative.
    latest_by_gate: Dict[str, VerificationReportFixture] = {}
    latest_round_by_gate: Dict[str, int] = {}
    for _, report in admissible:
        current = latest_round_by_gate.get(report.gate_id)
        if current is None or report.round >= current:
            latest_round_by_gate[report.gate_id] = report.round
            latest_by_gate[report.gate_id] = report

    missing_required_gates: List[str] = []
    satisfied_gates: List[str] = []

    for gate_id in required_gates:
        report = latest_by_gate.get(gate_id)
        if report is None:
            missing_required_gates.append(gate_id)
            continue

        trustworthy = _report_is_trustworthy(report, bundle)

        if report.result == "PASS":
            if trustworthy:
                satisfied_gates.append(gate_id)
            else:
                # A claimed-complete gate whose evidence cannot be trusted is
                # a live false-complete signal, not merely "not yet run" --
                # it must escalate, never silently fall back to
                # CONTINUE_VERIFICATION (Phase B-1 Fixture 2 and 3).
                admitted_classes.add("UNKNOWN")
        elif report.result == "FAIL":
            if not trustworthy and (
                report.evidence_source not in TRUSTED_EVIDENCE_SOURCES
                or report.identity_resolution != "resolved"
            ):
                admitted_classes.add("UNKNOWN")
            elif not report.failure_observations:
                admitted_classes.add("UNKNOWN")
            else:
                for observation in report.failure_observations:
                    admitted_classes.add(_classify_observation(observation, gate_id, bundle))
        else:  # explicit "UNKNOWN" result
            admitted_classes.add("UNKNOWN")

    if missing_required_gates and not admitted_classes:
        next_action = "CONTINUE_VERIFICATION"
    elif "UNKNOWN" in admitted_classes or "GOVERNANCE_CONFLICT" in admitted_classes:
        next_action = "ESCALATE_HUMAN"
    elif "ACCEPTANCE_CONTRACT_DEFECT" in admitted_classes:
        next_action = "OPEN_BUNDLE_REVISION"
    elif admitted_classes & {"CONTRACT_VIOLATION", "REGRESSION", "TEST_DEFECT"}:
        next_action = "REPAIR"
    elif _retry_eligible(admitted_classes):
        next_action = "RETRY_SAME_CANDIDATE"
    elif missing_required_gates:
        next_action = "CONTINUE_VERIFICATION"
    elif required_gates and set(satisfied_gates) == set(required_gates) and not admitted_classes:
        next_action = "ACCEPTED"
    else:
        # No required gates declared for this risk tier and nothing failed:
        # fail closed to human judgement rather than silently accepting.
        next_action = "ESCALATE_HUMAN"
        admitted_classes.add("UNKNOWN")

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
