"""Phase B-1 minimal data model: Task / Execution / AcceptanceBundle /
VerificationReport fixtures, plus the fixed vocabularies the evaluator
reasons over (risk tiers, failure classes, next actions).

These are fixture representations for the reference evaluator only -- they
are not the real ADM Task/Execution schema (schema/execution.schema.json)
and must never be conflated with it. Field names echo that schema's
retry_of_execution_id / provider_evidence / lease_evidence naming only so a
future real-checker integration slice can map one to the other without a
vocabulary translation layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Sequence, Tuple

# Risk only ever escalates along this total order (Phase B-1 architecture
# constraint: effective risk = max(declared, diff-derived, component
# escalation); "artifact_sensitive" sits between "high" and "production" for
# deliverables (e.g. spreadsheets) where a byte/pixel-level rendering
# defect is not visible to a purely logical/unit-test gate.
RISK_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "artifact_sensitive", "production")

ADMITTED_FAILURE_CLASSES: Tuple[str, ...] = (
    "GOVERNANCE_CONFLICT",
    "PRODUCTION_SCOPE_VIOLATION",
    "ACCEPTANCE_CONTRACT_DEFECT",
    "REGRESSION",
    "CONTRACT_VIOLATION",
    "TEST_DEFECT",
    "ENVIRONMENT_TRANSIENT",
    "UNKNOWN",
)

NEXT_ACTIONS: Tuple[str, ...] = (
    "REPAIR",
    "OPEN_BUNDLE_REVISION",
    "RETRY_SAME_CANDIDATE",
    "ESCALATE_HUMAN",
    "ROUTE_TO_PFP",
    "CONTINUE_VERIFICATION",
    "ACCEPTED",
)

# Evidence sources a report's PASS/FAIL verdict may actually be trusted from.
# Executor self-report signals (an HTTP 200 the executor observed, a port it
# says is listening, a prompt it says it generated, its own tests_status)
# are deliberately excluded -- Phase B-1 Fixture 3 exists to prove they can
# never, by themselves, satisfy a gate.
TRUSTED_EVIDENCE_SOURCES: Tuple[str, ...] = (
    "independent_render",
    "independent_check",
    "reproducible_test_run",
    "frozen_oracle_replay",
)


def risk_rank(risk: str) -> int:
    return RISK_LEVELS.index(risk)


@dataclass(frozen=True)
class TaskFixture:
    task_id: str
    declared_risk: str
    deliverable_sha: str
    acceptance_bundle_ref: str

    def __post_init__(self) -> None:
        if self.declared_risk not in RISK_LEVELS:
            raise ValueError(f"unknown declared_risk {self.declared_risk!r}")


@dataclass(frozen=True)
class ExecutionFixture:
    execution_id: str
    task_id: str
    base_sha: str
    candidate_sha: str
    diff_paths: Tuple[str, ...]
    status: str
    repair_of_execution_id: Optional[str] = None
    retry_of_execution_id: Optional[str] = None
    executor_identity: Optional[str] = None


@dataclass(frozen=True)
class RiskRule:
    """A diff-path trigger that forces a minimum risk tier."""
    path_prefix: str
    forced_risk: str

    def __post_init__(self) -> None:
        if self.forced_risk not in RISK_LEVELS:
            raise ValueError(f"unknown forced_risk {self.forced_risk!r}")


@dataclass(frozen=True)
class AcceptanceBundleFixture:
    bundle_hash: str
    governance_digest: str
    # risk tier -> required gate_ids for that tier (fixture-authored, so the
    # relationship between tiers -- e.g. artifact_sensitive requiring
    # everything high requires plus more -- is data, not evaluator logic).
    gate_requirements_by_risk: Mapping[str, Tuple[str, ...]]
    # gate_id -> (checker_identity, checker_version)
    checker_identities: Mapping[str, Tuple[str, str]]
    # gate_id -> set of frozen oracle hashes a report for that gate must match
    frozen_oracle_hashes: Mapping[str, str] = field(default_factory=dict)
    # gate_id -> required coverage dimensions (e.g. {"desktop", "mobile"})
    gate_required_dimensions: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    # gate_id -> defect signatures the contract already has a clause for
    gate_contract_clauses: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    risk_rules: Tuple[RiskRule, ...] = ()
    # failure signatures that are allowed to be classified ENVIRONMENT_TRANSIENT
    transient_allowlist: Tuple[str, ...] = ()
    baseline_data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FailureObservation:
    signature: str
    is_regression: bool = False
    # Optional explicit override into one of ADMITTED_FAILURE_CLASSES; still
    # subject to the evaluator's own predicates (rule: a proposed class never
    # directly decides the result) -- see evaluator._classify_observation.
    category: Optional[str] = None

    def __post_init__(self) -> None:
        if self.category is not None and self.category not in ADMITTED_FAILURE_CLASSES:
            raise ValueError(f"unknown category {self.category!r}")


@dataclass(frozen=True)
class VerificationReportFixture:
    execution_id: str
    gate_id: str
    round: int
    candidate_sha: str
    base_sha: str
    bundle_hash: str
    checker_identity: str
    checker_version: str
    evidence_source: str
    identity_resolution: str  # "resolved" | "needs_review" | "conflicting"
    result: str  # "PASS" | "FAIL" | "UNKNOWN"
    coverage_dimensions: Tuple[str, ...] = ()
    failure_observations: Tuple[FailureObservation, ...] = ()
    oracle_hash: Optional[str] = None
    # What the AI/model checker itself proposed, if anything. Deliberately
    # NEVER read by the classification predicate -- see rule in Phase B-1
    # spec section E ("AI/model proposed_class must not directly decide the
    # result"). Kept only so tests can prove it is ignored.
    proposed_class: Optional[str] = None


@dataclass(frozen=True)
class EvaluationResult:
    execution_id: str
    candidate_sha: str
    base_sha: str
    acceptance_bundle_hash: str
    effective_risk: str
    admissible_report_keys: Tuple[Tuple[str, int], ...]  # (gate_id, round)
    admitted_failure_classes: Tuple[str, ...]
    missing_required_gates: Tuple[str, ...]
    invalidated_report_reasons: Tuple[Tuple[str, str], ...]  # (report_key, reason)
    next_action: str
    acceptance_state: str  # "ACCEPTED" | "NOT_ACCEPTED"

    def __post_init__(self) -> None:
        if self.next_action not in NEXT_ACTIONS:
            raise ValueError(f"unknown next_action {self.next_action!r}")
        if self.acceptance_state == "ACCEPTED" and self.next_action != "ACCEPTED":
            raise ValueError("acceptance_state=ACCEPTED requires next_action=ACCEPTED")
