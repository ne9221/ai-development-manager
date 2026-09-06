"""Phase B-2 data model for the verification controller.

These remain *fixture* representations, not the real ADM schema
(``schema/execution.schema.json``), and must never be conflated with it. Field
names deliberately echo the real schema's ``retry_of_execution_id`` /
``repair_of_execution_id`` / ``provider_evidence`` / ``lease_evidence`` naming
so a later adapter slice maps one onto the other without inventing a
vocabulary in between. Phase A v3's central constraint -- reuse ADM's existing
primitives rather than starting a second truth system -- is why identity
resolution borrows Session Center's classification vocabulary and why the
production boundary is expressed in terms ``manager.production_guard`` already
understands.

What is *not* modelled here is as deliberate as what is. There is no
``acceptance_state`` field on Execution and no writable ACCEPTED record
anywhere: acceptance is derived every time from admissible reports, so there
is no field an executor could write to declare itself done. There is no
LoopRun, no second Task/Execution/Session registry, and no persisted verdict.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from .identity import Identity, ResolvedIdentity  # noqa: F401  (re-exported)

# Risk only ever escalates along this total order. "artifact_sensitive" sits
# between "high" and "production" for deliverables (spreadsheets, rendered UI)
# where a byte- or pixel-level defect is invisible to a purely logical gate.
# This ordering is inherited unchanged from Phase B-1, which the independent
# review accepted; it differs from Phase A v3's R1..R5 spelling, and that
# divergence is recorded rather than silently re-litigated here.
RISK_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "artifact_sensitive", "production")

# Phase A v3 C0..C7. Deliberately exactly eight: a ninth class would be new
# architecture, so situations like "this test also failed at base" are
# expressed through an existing class (TEST_DEFECT via TD-1) rather than by
# growing this tuple.
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
    "REPAIR_TEST_ONLY",
    "OPEN_BUNDLE_REVISION",
    "RETRY_SAME_CANDIDATE",
    "INVALIDATE_ROUND",
    "ESCALATE_HUMAN",
    "ROUTE_TO_PFP",
    "CONTINUE_VERIFICATION",
    "ACCEPTED",
)

# Derived, never stored. Nothing in this package writes an acceptance state to
# any durable field -- see the module docstring.
ACCEPTANCE_STATES: Tuple[str, ...] = (
    "PENDING",
    "IN_VERIFICATION",
    "ACCEPTED",
    "REJECTED_NEEDS_REPAIR",
    "BLOCKED_HUMAN",
    "DEFERRED_TO_PFP",
    "ROUND_INVALIDATED",
    "STALE",
    "REVOKED",
)

# Evidence sources a report's verdict may be trusted from. Executor self-report
# signals -- an HTTP 200 it observed, a port it says is listening, a prompt it
# says it wrote, its own Handoff.tests_status -- are excluded by construction.
# Phase A v3 C.2's three-way split: executor input is never evidence; only
# checker-produced replayable output or externally verified truth is.
TRUSTED_EVIDENCE_SOURCES: Tuple[str, ...] = (
    "checker_produced_replayable",
    "external_truth_verified",
    "independent_render",
    "independent_check",
    "reproducible_test_run",
    "frozen_oracle_replay",
)

# Bumped whenever the derivation algorithm itself changes meaning. It is mixed
# into derivation_key so a previously ACCEPTED execution goes STALE when the
# algorithm changes, without anyone having to remember to invalidate caches.
DERIVE_ALGO_VERSION = "verification-loop/derive/2"


def risk_rank(risk: str) -> int:
    return RISK_LEVELS.index(risk)


# ---------------------------------------------------------------------------
# Frozen Acceptance Bundle members
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckerSpec:
    """Who is allowed to answer one gate, and with what authority.

    ``checker_impl_digest`` is what makes "checker_version 2.3.0" mean
    something: a version string is a claim, a digest of the implementation
    bytes is checkable. ``transient_code_allowlist`` replaces Phase B-1's
    signature-based ``transient_allowlist`` -- Phase A v3 requires the
    *checker* to emit a transient code, so a failure cannot be retried just
    because its message happens to match a listed string.
    """

    checker_id: str
    checker_version: str
    checker_impl_digest: str
    transient_code_allowlist: Tuple[str, ...] = ()
    retryable: bool = False
    # A gate that writes to production can never be answered by this loop.
    # Phase A v3 I5: the controller is structurally unable to emit its PASS.
    production_write: bool = False
    environment: str = "isolated"


@dataclass(frozen=True)
class FrozenOracle:
    """A golden/snapshot/test-selector set whose member digests are frozen.

    Members are (path, sha256) pairs. Because they feed bundle_hash, editing a
    golden file changes the bundle hash, which invalidates every report bound
    to the old hash. "Change the golden until it passes" therefore has no
    expression within a single bundle.
    """

    oracle_id: str
    role: str  # "golden" | "snapshot" | "test_selector"
    members: Tuple[Tuple[str, str], ...] = ()

    @property
    def member_paths(self) -> Tuple[str, ...]:
        return tuple(path for path, _digest in self.members)


@dataclass(frozen=True)
class EnvironmentFingerprint:
    """What must match for two test results to be comparable at all.

    ``checkout_path_length`` is in here because it has actually caused false
    regressions in this repo: the installer tests are sensitive to path length,
    so a base and a candidate measured at different path lengths produce
    "new failures" that are pure measurement artefact.
    """

    os: str
    python: str
    checkout_path_length: int
    ai_manager_home_class: str


@dataclass(frozen=True)
class BaselineAttestation:
    """An attested full run at one exact base_sha.

    This is what lets REGRESSION be derived without re-running base every
    round. It is only usable when ``base_sha`` matches the execution's base and
    the fingerprints agree; otherwise absence of a signature from
    ``known_baseline_failures`` proves nothing and the comparison is UNKNOWN.
    """

    base_sha: str
    environment_fingerprint: EnvironmentFingerprint
    known_baseline_failures: Tuple[str, ...] = ()
    attested_by_report_digest: Optional[str] = None


@dataclass(frozen=True)
class RiskRule:
    """A diff-path trigger that forces a minimum risk tier."""

    path_prefix: str
    forced_risk: str

    def __post_init__(self) -> None:
        if self.forced_risk not in RISK_LEVELS:
            raise ValueError(f"unknown forced_risk {self.forced_risk!r}")


@dataclass(frozen=True)
class ComponentRule:
    """An escalation keyed on a measured environment component, not a diff.

    This is the third term of effective_risk that Phase B-1 lacked entirely:
    "the worktree is a production checkout" is not something a diff path can
    express, and it must still force the production tier.
    """

    component: str
    forced_risk: str

    def __post_init__(self) -> None:
        if self.forced_risk not in RISK_LEVELS:
            raise ValueError(f"unknown forced_risk {self.forced_risk!r}")


@dataclass(frozen=True)
class ImpactRule:
    """Which gates a change under ``path_prefix`` invalidates."""

    path_prefix: str
    gates: Tuple[str, ...]


@dataclass(frozen=True)
class Budget:
    """Caps that stop a verification loop running forever.

    These live in the bundle rather than on the Task on purpose: the bundle is
    frozen and content-addressed, so raising a budget changes bundle_hash and
    needs a human-approved bundle revision. A budget stored on a writable Task
    field would be raisable by whatever is being budgeted.
    """

    max_transient_retries: int = 2
    max_repair_executions: int = 3
    max_review_rounds: int = 2
    max_wallclock_minutes: Optional[int] = None


@dataclass(frozen=True)
class AcceptanceBundleFixture:
    """The frozen acceptance contract for one project at one version.

    ``bundle_hash`` must equal the hash computed over every other field (see
    ``bundle.compute_bundle_hash``); a mismatch is an ACCEPTANCE_CONTRACT_DEFECT
    rather than something the evaluator tries to work around.
    """

    bundle_hash: str
    bundle_id: str
    bundle_version: str
    # Governance binding (Phase A v3 I10). There is no
    # "github_copy_authoritative" field because it is not configurable: the
    # repo's own rules file is a stale copy and is never authoritative.
    governance_source_uri: str
    governance_version: str
    governance_digest: str
    gate_requirements_by_risk: Mapping[str, Tuple[str, ...]]
    checkers: Mapping[str, CheckerSpec]
    frozen_oracles: Tuple[FrozenOracle, ...] = ()
    gate_oracle_refs: Mapping[str, str] = field(default_factory=dict)
    gate_required_dimensions: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    gate_contract_clauses: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    risk_rules: Tuple[RiskRule, ...] = ()
    component_escalation_rules: Tuple[ComponentRule, ...] = ()
    impact_rules: Tuple[ImpactRule, ...] = ()
    impact_always_rerun: Tuple[str, ...] = ()
    baseline: Optional[BaselineAttestation] = None
    budget: Budget = field(default_factory=Budget)
    # V0 blacklists. Membership means the loop must route to the Production Fix
    # Protocol instead of verifying anything itself.
    production_canonical_checkout_paths: Tuple[str, ...] = ()
    production_ai_manager_home: Optional[str] = None

    def oracle(self, oracle_id: str) -> Optional[FrozenOracle]:
        for candidate in self.frozen_oracles:
            if candidate.oracle_id == oracle_id:
                return candidate
        return None

    def frozen_oracle_paths(self) -> Tuple[str, ...]:
        paths = []
        for oracle in self.frozen_oracles:
            paths.extend(oracle.member_paths)
        return tuple(sorted(set(paths)))


# ---------------------------------------------------------------------------
# Task / Execution / preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskFixture:
    task_id: str
    declared_risk: str
    # The single SHA the Task actually delivers. Phase A v3 A.5: a Task's
    # deliverable is one SHA, not a collection of PASSes, which is what makes
    # cross-SHA gate assembly impossible by definition rather than by check.
    deliverable_sha: Optional[str]
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
    acceptance_bundle_ref: str
    executor_identity: Optional[ResolvedIdentity] = None
    # repair changes candidate_sha (new code); retry does not (same candidate,
    # transient failure only). Two fields, two budgets -- conflating them is
    # how a repair loop launders itself into an unbounded retry loop.
    repair_of_execution_id: Optional[str] = None
    retry_of_execution_id: Optional[str] = None
    trigger: str = "human"


@dataclass(frozen=True)
class PreflightFacts:
    """What V0 measured about the environment this round actually ran in.

    Required, not optional. Without it the controller cannot prove the worktree
    is not a production checkout, and "we could not check" must never read as
    "it was fine".
    """

    worktree_path: str
    worktree_head: str
    ai_manager_home: str
    ai_manager_home_class: str
    governance_digest_measured: str
    environment_fingerprint: EnvironmentFingerprint
    # Measured with manager.production_guard.is_marked_production_path, which
    # walks ancestors -- so a subdirectory of a marked checkout cannot slip
    # past by resolving deeper.
    worktree_is_marked_production: bool = False
    # The lease actually held *now*, to compare against the one the ticket was
    # issued under. Two verifications can share a HEAD and still be different
    # worktrees, so head equality is not worktree identity. Optional in the
    # dataclass only because a caller can omit it; omitting it is not the same
    # as matching, and admission treats it as unverifiable, not as fine.
    worktree_lock_id: Optional[str] = None
    worktree_generation: Optional[int] = None


# ---------------------------------------------------------------------------
# Observations / reports / tickets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContradictionProof:
    """TD-2: evidence that a test contradicts a frozen contract clause.

    All four fields must be populated. A proof missing its witness is an
    assertion, and assertions do not admit a TEST_DEFECT.
    """

    test_id: str
    asserted_predicate: str
    clause_id: str
    witness: str

    @property
    def is_complete(self) -> bool:
        return all(
            isinstance(v, str) and v.strip()
            for v in (self.test_id, self.asserted_predicate, self.clause_id, self.witness)
        )


@dataclass(frozen=True)
class FailureObservation:
    """One failure a checker observed.

    Note what is absent: Phase B-1's ``category`` (deleted in B-1R after the
    review showed a report could relabel a regression as transient) and
    ``is_regression`` (deleted here -- a report asserting that its own failure
    is a regression is exactly the self-declared field the architecture
    forbids). Regression is now *derived* from base evidence; see
    classification.py.

    ``proposed_class`` survives only so tests can prove it is read nowhere.
    """

    signature: str
    # Checker-issued. Not a free-text hint: it must be in the gate checker's
    # allowlist for ENVIRONMENT_TRANSIENT to be admitted at all.
    transient_code: Optional[str] = None
    # What this same test did at base_sha in this round, if base was run.
    base_result: Optional[str] = None  # "PASS" | "FAIL" | "UNKNOWN" | None
    base_signature: Optional[str] = None
    contradiction_proof: Optional[ContradictionProof] = None
    # Paths a proposed test-only repair would touch, checked against the frozen
    # oracle paths before TEST_DEFECT can be admitted.
    repair_paths: Tuple[str, ...] = ()
    proposed_class: Optional[str] = None


@dataclass(frozen=True)
class VerificationTicket:
    """Issued by the controller *before* a checker runs.

    A report with no matching ticket does not enter derivation -- it is not a
    FAIL, it does not exist. ``ticket_id`` is derived from the tuple it binds
    (see tickets.derive_ticket_id), so a forged ticket carrying a mismatched id
    is self-evidently invalid.
    """

    ticket_id: str
    task_id: str
    execution_id: str
    candidate_sha: str
    base_sha: str
    bundle_hash: str
    gate_id: str
    round: int
    issued_by: ResolvedIdentity
    expected_checker_identity: ResolvedIdentity
    forbidden_identity: Optional[ResolvedIdentity]
    candidate_head_at_issue: str
    worktree_lock_id: str
    worktree_generation: int
    issued_at: str
    status: str = "issued"  # issued | consumed | invalidated
    # Phase A v3 C.3 predicate 11. A ticket is consumed by exactly one report,
    # and which one is recorded at the moment of consumption rather than
    # inferred later -- "the report that matches" is a question with more than
    # one answer once an attacker can add files.
    consumed_report_digest: Optional[str] = None
    consumed_at: Optional[str] = None
    # Monotone within one ticket: 0 is the issue record, 1 the consumption.
    ticket_seq: int = 0


@dataclass(frozen=True)
class VerificationReportFixture:
    execution_id: str
    task_id: str
    ticket_id: str
    gate_id: str
    round: int
    candidate_sha: str
    base_sha: str
    bundle_hash: str
    deliverable_sha: Optional[str]
    checker_id: str
    checker_version: str
    checker_impl_digest: str
    producer_identity: Optional[ResolvedIdentity]
    evidence_source: str
    result: str  # "PASS" | "FAIL" | "UNKNOWN" | "DEFERRED_TO_PFP"
    environment_fingerprint: EnvironmentFingerprint
    # Phase A v3 I6: both must equal candidate_sha, or the round ran against a
    # tree that moved underneath it and every verdict in it is void.
    worktree_head_before: str
    worktree_head_after: str
    governance_digest: str
    coverage_dimensions: Tuple[str, ...] = ()
    failure_observations: Tuple[FailureObservation, ...] = ()
    oracle_set_digest: Optional[str] = None


@dataclass(frozen=True)
class PriorAcceptance:
    """A previously derived ACCEPTED, kept only so it can be invalidated.

    This is not a stored verdict that anything trusts. It exists so STALE
    (inputs moved, old conclusion no longer reproducible) can be told apart
    from REVOKED (same inputs, contradictory evidence appeared) -- Phase A v3
    I3 insists those are different, because only one of them implies a defect.
    """

    derivation_key: str
    bundle_hash: str
    candidate_sha: str
    governance_digest: str


@dataclass(frozen=True)
class EvaluationResult:
    execution_id: str
    task_id: str
    candidate_sha: str
    base_sha: str
    acceptance_bundle_hash: str
    effective_risk: str
    required_gates: Tuple[str, ...]
    satisfied_gates: Tuple[str, ...]
    admissible_report_keys: Tuple[str, ...]
    admitted_failure_classes: Tuple[str, ...]
    missing_required_gates: Tuple[str, ...]
    invalidated_report_reasons: Tuple[Tuple[str, str], ...]
    # (signature, "REGRESSION" | "NOT_REGRESSION" | "UNKNOWN"). Reported
    # separately from the admitted class so an unprovable comparison is stated
    # as unknown instead of being asserted either way.
    regression_determinations: Tuple[Tuple[str, str], ...]
    rerun_gates: Tuple[str, ...]
    budget_exhausted: Tuple[str, ...]
    prior_acceptance_status: str  # NONE | CURRENT | STALE | REVOKED
    derivation_key: str
    next_action: str
    acceptance_state: str

    def __post_init__(self) -> None:
        if self.next_action not in NEXT_ACTIONS:
            raise ValueError(f"unknown next_action {self.next_action!r}")
        if self.acceptance_state not in ACCEPTANCE_STATES:
            raise ValueError(f"unknown acceptance_state {self.acceptance_state!r}")
        # Belt and braces on the one correspondence that matters: ACCEPTED is
        # reachable through exactly one next_action, so no future branch can
        # produce the state without going through the guard that earns it.
        if (self.acceptance_state == "ACCEPTED") != (self.next_action == "ACCEPTED"):
            raise ValueError("acceptance_state ACCEPTED and next_action ACCEPTED must agree")


@dataclass(frozen=True)
class TaskCloseDecision:
    task_id: str
    deliverable_sha: Optional[str]
    close_eligible: bool
    blocking_reasons: Tuple[str, ...]
    accepted_execution_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Rehydration
# ---------------------------------------------------------------------------
#
# Lives here rather than on the controller because the stores need it too: a
# ledger that verifies a record's content address has to rebuild the record
# first, and importing the controller from the store would invert the
# dependency. It touches no filesystem and no clock, so it does not widen the
# set of modules allowed to.


def rehydrate(cls, data: Any):
    """Rebuild a dataclass from the plain JSON a store holds.

    Only the shapes tickets and reports actually use are handled -- nested
    dataclasses, Optional, and homogeneous tuples. Anything else raises rather
    than guessing, because a silently mis-typed field would change a
    derivation without changing anything visible.
    """
    if data is None:
        return None
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"not a dataclass: {cls!r}")
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for field_ in dataclasses.fields(cls):
        kwargs[field_.name] = _coerce(hints[field_.name], data.get(field_.name))
    return cls(**kwargs)


def _coerce(annotation, value):
    origin = typing.get_origin(annotation)

    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        if len(args) == 1:
            return _coerce(args[0], value)
        return value

    if origin is tuple:
        args = typing.get_args(annotation)
        if not args or value is None:
            return tuple(value or ())
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(args[0], item) for item in value)
        return tuple(_coerce(arg, item) for arg, item in zip(args, value))

    if dataclasses.is_dataclass(annotation):
        return rehydrate(annotation, value)

    return value
