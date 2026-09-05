"""V5 safety + reachability kernel — executable reference model.

This module is the mechanical contract. KERNEL_V5.md is the prose twin.
Production manager/ runtime must not import this in this slice.

Remediation Round 1 (2026-09-05) closes the independent-review blockers
B1..B7 plus C7 (oracle lineage reuse) and C10 (human-escalation DoS).
The single structural change is that **trusted identity never comes from the
candidate payload**: every event carries an optional typed `EventEnvelope`
injected at the controller/launcher API boundary, and an event without one is
treated as CANDIDATE_EXECUTOR.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple


KERNEL_ID = "KERNEL_V5"
KERNEL_V5_AUTHORITATIVE = True
KERNEL_VERSION = "v5.0.0-draft"

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


class State(str, Enum):
    OPEN = "OPEN"
    VERIFYING = "VERIFYING"
    WAITING_RECOVERABLE = "WAITING_RECOVERABLE"
    REQUIRES_RE_ADJUDICATION = "REQUIRES_RE_ADJUDICATION"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class Event(str, Enum):
    OPEN_TASK = "OPEN_TASK"
    EXECUTOR_DONE = "EXECUTOR_DONE"
    VERIFY_START = "VERIFY_START"
    MECHANICAL_REPLAY = "MECHANICAL_REPLAY"
    REVIEW_CLAIM = "REVIEW_CLAIM"
    VERIFIER_UNAVAILABLE = "VERIFIER_UNAVAILABLE"
    STALE_BINDING = "STALE_BINDING"
    REDERIVE = "REDERIVE"
    ADJUDICATE = "ADJUDICATE"
    RETRY = "RETRY"
    ADVERSE_BLOCKER = "ADVERSE_BLOCKER"
    HUMAN_GATE = "HUMAN_GATE"
    REJECT = "REJECT"
    CLOSE_WINDOW = "CLOSE_WINDOW"


class ObligationState(str, Enum):
    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    ADVERSE = "ADVERSE"
    UNAVAILABLE_RECOVERABLE = "UNAVAILABLE_RECOVERABLE"
    UNAVAILABLE_HUMAN = "UNAVAILABLE_HUMAN"
    INVALIDATED = "INVALIDATED"


class Issuer(str, Enum):
    LAUNCHER = "LAUNCHER"
    PINNED_CONTROLLER = "PINNED_CONTROLLER"
    HUMAN_OPERATOR = "HUMAN_OPERATOR"
    CANDIDATE_EXECUTOR = "CANDIDATE_EXECUTOR"
    REVIEWER = "REVIEWER"


class Risk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    DESTRUCTIVE = "DESTRUCTIVE"


HUMAN_ESCALATION_CLOSED_SET = frozenset(
    {
        "login_authentication_consent",
        "permission_credential_provisioning",
        "destructive_action_approval",
        "intentional_human_gate",
        "unresolved_governance_conflict",
        "automated_recovery_budget_exhausted",
        "semantic_decision_no_objective_verifier",
    }
)

# C10: this reason is CONTROLLER-OWNED. A candidate cannot ask for it; it is
# produced only by recovery state where used >= maximum.
CONTROLLER_OWNED_HUMAN_REASONS = frozenset({"automated_recovery_budget_exhausted"})

RECOVERABLE_FAILURE_KINDS = frozenset(
    {
        "stale_observation",
        "temporary_reviewer_unavailable",
        "transient_read_error",
        "retryable_provider_failure",
        "cache_miss",
        "missing_recomputable_evidence",
    }
)

# B1/B5: only these sources may satisfy a critical obligation. The source is
# read from the EventEnvelope, never from the payload.
TRUSTED_MECHANICAL_SOURCES = frozenset({Issuer.LAUNCHER.value, Issuer.PINNED_CONTROLLER.value})
TRUSTED_ADJUDICATION_ISSUERS = frozenset({Issuer.HUMAN_OPERATOR.value, Issuer.PINNED_CONTROLLER.value})

# B5: an adjudication record must name this scope exactly; anything else is ignored.
ADJUDICATION_SCOPE = "obligation"
# Subjects that may be adjudicated without being obligations in their own right.
ADJUDICABLE_VIRTUAL_SUBJECTS = frozenset({"review.semantic"})

DEFAULT_RETRY_BUDGET = 3

V5_STATE_MODEL = [s.value for s in State]
OBLIGATION_STATE_MODEL = [s.value for s in ObligationState]
REVIEWER_OUTPUT_ROLE = "CLAIM"
REVIEW_CONTEXT_BINDING = "LAUNCHER_CAPTURED_MANIFEST_DIGEST"
INVALIDATION_POLICY = "ASYMMETRIC"
CONTROLLER_TRUST_ROOT = "LAUNCHER_CAPTURED_CONTROLLER_AND_POLICY_DIGEST"

# Section 10 of the remediation charter: say plainly what this model does and
# does not prove. The envelope separates event source from payload *inside the
# reference model*. It is not a runtime attestation of a real launcher process.
REFERENCE_MODEL_TRUST_BOUNDARY = "ENVELOPE_INJECTED_AT_CONTROLLER_LAUNCHER_API_BOUNDARY"
RUNTIME_ATTESTATION_BOUNDARY = "NOT_IMPLEMENTED_IN_THIS_SLICE"
TRUST_ROOT_RUNTIME_PROVEN = False

# Section 13: ACCEPTED is terminal for ONE verification cycle, bound to the
# OPEN/CLOSE evidence window that produced it. Later external change opens a
# NEW cycle; this kernel does no monitoring inside an accepted state.
ACCEPTED_LIFECYCLE = "TERMINAL_FOR_ONE_VERIFICATION_CYCLE_BOUND_TO_ITS_OPEN_CLOSE_WINDOW"

# C7: default lineage arity. 1:N lineage is deliberately NOT implemented here.
ORACLE_LINEAGE_ARITY = "1:1"


@dataclass(frozen=True)
class FailClosedSemantics:
    why_non_accepting: str
    recovery_owner: str
    recovery_action: str
    retry_condition: Optional[str]
    terminal_condition: str
    human_escalation_eligible: bool
    human_escalation_reason: Optional[str] = None


FAIL_CLOSED: Dict[State, FailClosedSemantics] = {
    State.OPEN: FailClosedSemantics(
        why_non_accepting="task opened; obligations pending; ACCEPTED not derivable",
        recovery_owner="CONTROLLER",
        recovery_action="await EXECUTOR_DONE then VERIFY_START",
        retry_condition="executor_done",
        terminal_condition="never terminal in OPEN",
        human_escalation_eligible=False,
    ),
    State.VERIFYING: FailClosedSemantics(
        why_non_accepting="verification in progress; decide() has not derived ACCEPTED",
        recovery_owner="CONTROLLER",
        recovery_action="replay mechanical criteria; bind close window; dispose obligations",
        retry_condition="evidence_or_replay_available",
        terminal_condition="derive ACCEPTED | REJECTED | HUMAN_REQUIRED | WAITING_RECOVERABLE",
        human_escalation_eligible=False,
    ),
    State.WAITING_RECOVERABLE: FailClosedSemantics(
        why_non_accepting="recoverable failure; obligation UNAVAILABLE_RECOVERABLE",
        recovery_owner="LAUNCHER",
        recovery_action="bounded retry / rederive under same retry identity",
        retry_condition="retry_identity_ready AND budget_remaining",
        terminal_condition="budget_exhausted -> HUMAN_REQUIRED or REJECTED (never ACCEPTED)",
        human_escalation_eligible=False,
    ),
    State.REQUIRES_RE_ADJUDICATION: FailClosedSemantics(
        why_non_accepting="OPEN/CLOSE disagreement or invalidated PASS; old PASS not retained",
        recovery_owner="CONTROLLER",
        recovery_action="rederive close window; typed adjudication if semantic",
        retry_condition="close_rederived OR typed_adjudication_record",
        terminal_condition="rederived SATISFIED -> VERIFYING; adverse -> REJECTED",
        human_escalation_eligible=False,
    ),
    State.REJECTED: FailClosedSemantics(
        why_non_accepting="explicit adverse blocker or invalid candidate write",
        recovery_owner="NONE",
        recovery_action="none — terminal reject; new task required for retry of work",
        retry_condition=None,
        terminal_condition="REJECTED is terminal",
        human_escalation_eligible=False,
    ),
    State.HUMAN_REQUIRED: FailClosedSemantics(
        why_non_accepting="closed-set human reason is active",
        recovery_owner="HUMAN",
        recovery_action="perform the named human action; emit allowed-issuer record",
        retry_condition="human_record_with_allowed_issuer",
        terminal_condition="HUMAN_REQUIRED remains until allowed record or task withdrawn",
        human_escalation_eligible=True,
        human_escalation_reason="set per instance from HUMAN_ESCALATION_CLOSED_SET",
    ),
    State.ACCEPTED: FailClosedSemantics(
        why_non_accepting="N/A — accepting",
        recovery_owner="NONE",
        recovery_action="none",
        retry_condition=None,
        terminal_condition="ACCEPTED is derived-terminal for this verification cycle",
        human_escalation_eligible=False,
    ),
}


@dataclass(frozen=True)
class EventEnvelope:
    """B1: the trusted provenance of an event, injected by the launcher /
    controller API boundary.

    The candidate supplies `payload`. It never supplies this. An event that
    arrives without an envelope is treated as CANDIDATE_EXECUTOR, so omitting
    the envelope fails closed instead of defaulting to LAUNCHER.

    `capture_id` must be one of the ids the launcher registered at OPEN, so a
    payload-shaped dict cannot stand in for a capture that never happened.
    """

    event_source: str
    capture_id: str
    captured_at_tick: int = 0
    capture_context: str = ""


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    required: bool
    state: ObligationState
    source: str
    evidence_digest: Optional[str] = None
    attester: Optional[str] = None
    # B5: when an obligation is SATISFIED by adjudication, remember which record
    # backs it so decide() can re-check scope/subject/expiry every time.
    backing_record_id: Optional[str] = None


@dataclass(frozen=True)
class Observation:
    predicate_id: str
    value: str
    window: str
    digest: Optional[str] = None


@dataclass(frozen=True)
class FreezeRecord:
    record_id: str
    issuer: str
    scope: str
    subject: str
    expires_tick: int
    provenance_digest: str
    issued_tick: int = 0
    resolution: str = "SATISFIED"


@dataclass(frozen=True)
class ReviewClaim:
    """REVIEWER_OUTPUT_ROLE = CLAIM.

    B4: a claim must carry a minimum schema. `files_used` and `context_manifest_digest`
    are what the reviewer *says*; they are cross-checked against the launcher
    capture and never promoted to truth on their own.
    """

    invocation_id: str
    context_manifest_digest: str
    files_used: Tuple[str, ...]
    findings: Optional[Tuple[Any, ...]] = None
    reviewer_identity: str = ""
    completion_status: str = ""
    launcher_capture_ref: str = ""
    verdict_text: str = ""
    role: str = "CLAIM"


REVIEW_CLAIM_REQUIRED_FIELDS = (
    "invocation_id",
    "context_manifest_digest",
    "launcher_capture_ref",
    "reviewer_identity",
    "completion_status",
)


@dataclass(frozen=True)
class ReviewCapture:
    """Launcher-captured truth about one review invocation (B4).

    The controller checks the claim against this; the reviewer cannot write it.
    """

    invocation_id: str
    context_digest: str
    files_used: Tuple[str, ...]


@dataclass(frozen=True)
class TrustRoot:
    captured_by: str
    controller_src_sha256: str
    policy_id: str
    policy_sha256: str
    kernel_id: str
    authoritative: bool


@dataclass(frozen=True)
class RetryBudget:
    identity: str
    used: int
    maximum: int

    @property
    def remaining(self) -> int:
        return max(0, self.maximum - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.maximum


@dataclass
class World:
    """Mutable only through apply(). Candidate cannot assign state=ACCEPTED."""

    state: State
    tick: int
    risk: Risk
    policy_id: str
    policy_sha256: str
    required_obligation_ids: Tuple[str, ...]
    obligations: Dict[str, Obligation]
    launcher_trust: TrustRoot
    claimed_trust: Optional[TrustRoot]
    oracle_expected: FrozenSet[str]
    oracle_observed: FrozenSet[str]
    oracle_lineage: Dict[str, str]
    open_obs: Dict[str, Observation]
    close_obs: Dict[str, Observation]
    review_claims: List[ReviewClaim]
    freeze_records: List[FreezeRecord]
    allowed_review_files: FrozenSet[str]
    captured_review_context_digest: str
    actual_review_files: Tuple[str, ...]
    mechanical_replay: Optional[str]
    mechanical_replay_attester: Optional[str]
    budgets: Dict[str, RetryBudget]
    human_reason: Optional[str]
    last_failure_kind: Optional[str]
    new_invariant_candidates: List[Dict[str, Any]]
    executor_asserted_accepted: bool
    candidate_status_field: Optional[str]
    latest_review_pointer: Optional[str]
    # --- Round 1 additions ---
    launcher_capture_ids: FrozenSet[str] = field(default_factory=frozenset)
    review_captures: Dict[str, ReviewCapture] = field(default_factory=dict)
    human_gate_subject: Optional[str] = None
    last_retry_identity: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def obligation(self, oid: str) -> Optional[Obligation]:
        return self.obligations.get(oid)


@dataclass(frozen=True)
class Transition:
    from_state: State
    event: Event
    guard: str
    to_state: State
    next_action: str


@dataclass(frozen=True)
class Decision:
    derived_status: State
    reasons: Tuple[str, ...]
    blockers: Tuple[str, ...]
    controller_digest: str
    trust_ok: bool


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def controller_trust_digest() -> str:
    """Digest of this module's source. Launcher captures; candidate cannot mint."""
    try:
        with open(__file__, "rb") as fh:
            body = fh.read()
    except OSError:
        body = b"KERNEL_V5_UNREADABLE"
    return hashlib.sha256(b"KERNEL_V5|" + KERNEL_VERSION.encode() + b"|" + body).hexdigest()


def policy_digest(policy: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(dict(policy))).hexdigest()


def record_invariant_candidate(world: World, candidate_id: str, statement: str, evidence: str) -> None:
    """NEW_INVARIANT_CANDIDATE: record + prove. Never auto-expands INV-*."""
    world.new_invariant_candidates.append(
        {
            "id": candidate_id,
            "statement": statement,
            "evidence": evidence,
            "status": "PENDING_HUMAN_ADJUDICATION",
        }
    )


# ---------------------------------------------------------------------------
# B1 — trusted event source
# ---------------------------------------------------------------------------


def trusted_envelope(world: World, source: str, capture_id: Optional[str] = None, context: str = "") -> EventEnvelope:
    """The controller/launcher API boundary. Not reachable from a payload."""
    if capture_id is None:
        capture_id = next(iter(sorted(world.launcher_capture_ids)), "")
    return EventEnvelope(
        event_source=source,
        capture_id=capture_id,
        captured_at_tick=world.tick,
        capture_context=context,
    )


def _event_source(world: World, envelope: Optional[EventEnvelope]) -> str:
    """Resolve the trusted source of an event.

    Fail closed: no envelope, wrong type, or an unregistered capture id all
    resolve to CANDIDATE_EXECUTOR. The payload is never consulted.
    """
    if not isinstance(envelope, EventEnvelope):
        return Issuer.CANDIDATE_EXECUTOR.value
    if envelope.event_source == Issuer.CANDIDATE_EXECUTOR.value:
        return Issuer.CANDIDATE_EXECUTOR.value
    if envelope.capture_id not in world.launcher_capture_ids:
        return Issuer.CANDIDATE_EXECUTOR.value
    try:
        Issuer(envelope.event_source)
    except ValueError:
        return Issuer.CANDIDATE_EXECUTOR.value
    return envelope.event_source


def _note_payload_identity_claim(world: World, payload: Mapping[str, Any], event: Event) -> None:
    """MC-B: record, and ignore, any identity the payload tried to assert."""
    for key in ("attester", "issuer", "event_source", "captured_by"):
        if key in payload:
            world.notes.append(
                "MC-B: %s payload asserted %s=%r — ignored; source comes from the envelope"
                % (event.value, key, payload[key])
            )


def _required_ids(policy: Mapping[str, Any]) -> Tuple[str, ...]:
    ids = ["mechanical.tests", "oracle.set", "interval.binding", "controller.trust"]
    risk = Risk(policy.get("risk", "LOW"))
    if risk in (Risk.MEDIUM, Risk.HIGH, Risk.DESTRUCTIVE):
        ids.append("review.claim")
    if risk == Risk.DESTRUCTIVE:
        ids.append("human.destructive_approval")
    extra = policy.get("extra_required_obligations") or []
    for item in extra:
        if item not in ids:
            ids.append(item)
    return tuple(ids)


def _floor_obligations(required: Tuple[str, ...]) -> Dict[str, Obligation]:
    return {
        oid: Obligation(
            obligation_id=oid,
            required=True,
            state=ObligationState.PENDING,
            source="POLICY_FLOOR",
        )
        for oid in required
    }


def _declared(*sources: Mapping[str, Any], key: str) -> Optional[Any]:
    """B2: `declared but empty` must survive. Presence of the key decides, not truthiness."""
    for src in sources:
        if key in src and src[key] is not None:
            return src[key]
    return None


def open_task(policy: Mapping[str, Any], launcher: Mapping[str, Any]) -> World:
    pdig = policy_digest(policy)
    required = _required_ids(policy)
    src = controller_trust_digest()
    trust = TrustRoot(
        captured_by="LAUNCHER",
        controller_src_sha256=str(launcher.get("controller_src_sha256") or src),
        policy_id=str(policy.get("policy_id", "vl-v5")),
        policy_sha256=pdig,
        kernel_id=KERNEL_ID,
        authoritative=bool(launcher.get("authoritative", True)),
    )

    # B2: an explicitly declared empty oracle domain STAYS empty. The runtime
    # must not invent `oracle.unit` to make a vacuous domain look populated.
    declared_oracle = _declared(launcher, policy, key="oracle_expected")
    expected = frozenset(declared_oracle if declared_oracle is not None else ())

    allowed = frozenset(launcher.get("allowed_review_files") or ("review/policy.md", "task/spec.md"))
    captured_ctx = str(launcher.get("captured_review_context_digest") or "")

    captures: Dict[str, ReviewCapture] = {}
    for inv, cap in (launcher.get("review_captures") or {}).items():
        if isinstance(cap, ReviewCapture):
            captures[str(inv)] = cap
        else:
            captures[str(inv)] = ReviewCapture(
                invocation_id=str(inv),
                context_digest=str(cap.get("context_digest", "")),
                files_used=tuple(cap.get("files_used") or ()),
            )

    capture_ids = frozenset(str(c) for c in (launcher.get("capture_ids") or ("launcher-capture-1",)))

    open_obs = {
        pid: Observation(predicate_id=pid, value=val, window="OPEN", digest=None)
        for pid, val in (launcher.get("open_predicates") or {"mechanical.tests": "PASS"}).items()
    }
    return World(
        state=State.OPEN,
        tick=0,
        risk=Risk(policy.get("risk", "LOW")),
        policy_id=trust.policy_id,
        policy_sha256=pdig,
        required_obligation_ids=required,
        obligations=_floor_obligations(required),
        launcher_trust=trust,
        claimed_trust=None,
        oracle_expected=expected,
        oracle_observed=frozenset(),
        oracle_lineage=dict(launcher.get("oracle_lineage") or {}),
        open_obs=open_obs,
        close_obs={},
        review_claims=[],
        freeze_records=[],
        allowed_review_files=allowed,
        captured_review_context_digest=captured_ctx,
        actual_review_files=tuple(),
        mechanical_replay=None,
        mechanical_replay_attester=None,
        budgets={},
        human_reason=None,
        last_failure_kind=None,
        new_invariant_candidates=[],
        executor_asserted_accepted=False,
        candidate_status_field=None,
        latest_review_pointer=None,
        launcher_capture_ids=capture_ids,
        review_captures=captures,
        human_gate_subject=None,
        last_retry_identity=None,
        notes=[],
    )


def _set_ob(world: World, oid: str, state: ObligationState, **kwargs: Any) -> None:
    current = world.obligations.get(oid)
    if current is None:
        current = Obligation(obligation_id=oid, required=True, state=ObligationState.PENDING, source="POLICY_FLOOR")
    # A disposition change always clears the previous adjudication backing unless
    # the caller sets a new one, so an expired record cannot linger on the state.
    kwargs.setdefault("backing_record_id", None)
    world.obligations[oid] = replace(current, state=state, **kwargs)


def _budget(world: World, identity: str) -> RetryBudget:
    if identity not in world.budgets:
        world.budgets[identity] = RetryBudget(identity=identity, used=0, maximum=DEFAULT_RETRY_BUDGET)
    return world.budgets[identity]


def retry_identity(kind: str, obligation: str) -> str:
    """B6: one canonical identity shape for every recoverable route."""
    return "recovery:%s:%s" % (kind, obligation)


# ---------------------------------------------------------------------------
# B6 — one recovery function for every recoverable route
# ---------------------------------------------------------------------------


def _enter_recoverable_failure(
    world: World,
    kind: str,
    obligation: str,
    *,
    waiting_state: State = State.WAITING_RECOVERABLE,
    affected_state: ObligationState = ObligationState.UNAVAILABLE_RECOVERABLE,
) -> Tuple[str, bool]:
    """Every recoverable failure — named kind or unknown default — lands here.

    1. invalidates the affected evidence, 2. consumes budget under a bound retry
    identity, 3. is idempotent per identity, 4. exhausts at a finite limit, and
    5. leaves an obligation state that decide() cannot accept over.

    Returns (retry_identity, exhausted).
    """
    identity = retry_identity(kind, obligation)
    budget = _budget(world, identity)
    budget = RetryBudget(identity, budget.used + 1, budget.maximum)
    world.budgets[identity] = budget
    world.last_failure_kind = kind
    world.last_retry_identity = identity

    if budget.used > budget.maximum:
        _set_ob(world, obligation, ObligationState.UNAVAILABLE_HUMAN, source="BUDGET")
        world.state = State.HUMAN_REQUIRED
        world.human_reason = "automated_recovery_budget_exhausted"
        world.human_gate_subject = obligation
        return identity, True

    _set_ob(world, obligation, affected_state, source="TRANSIENT")
    world.state = waiting_state
    return identity, False


def _budget_actually_exhausted(world: World) -> bool:
    """C10: only controller-owned recovery state may claim exhaustion."""
    return any(b.exhausted for b in world.budgets.values())


# ---------------------------------------------------------------------------
# B3 — interval disposition is recomputed over the WHOLE required domain
# ---------------------------------------------------------------------------


_CLOSE_ABSENT_VALUES = frozenset({"MISSING", "ABSENT", ""})


def _recompute_interval(world: World) -> ObligationState:
    """Recompute `interval.binding` from scratch over the full OPEN domain.

    Never incremental, never OR-ed with a previous PASS: the new disposition
    atomically replaces the old one, so a stale SATISFIED from an earlier
    CLOSE/REDERIVE cannot survive a later partial one.
    """
    domain = world.open_obs
    if not domain:
        state = ObligationState.PENDING  # MC-A: no predicates is not satisfaction
    elif not world.close_obs:
        state = ObligationState.PENDING  # entrance-only binding is forbidden
    else:
        adverse = False
        invalidated = False
        for pid, o in domain.items():
            c = world.close_obs.get(pid)
            c_value = c.value if c is not None else None
            if o.value == "ADVERSE" and (c_value is None or c_value in _CLOSE_ABSENT_VALUES):
                # asymmetric: an OPEN adverse that disappears at CLOSE is not clean
                adverse = True
            elif c_value is None:
                invalidated = True  # incomplete close over a required predicate
            elif c_value != "PASS":
                invalidated = True  # FAIL / ADVERSE / MISSING / anything not PASS
            elif o.value != "PASS":
                invalidated = True  # OPEN was not clean; CLOSE alone cannot mint a PASS
        if adverse:
            state = ObligationState.ADVERSE
        elif invalidated:
            state = ObligationState.INVALIDATED
        else:
            state = ObligationState.SATISFIED

    if state == ObligationState.SATISFIED:
        _set_ob(
            world,
            "interval.binding",
            state,
            source="DERIVED",
            attester=Issuer.PINNED_CONTROLLER.value,
        )
    else:
        _set_ob(world, "interval.binding", state, source="ASYMMETRIC")
    return state


def apply(
    world: World,
    event: Event,
    payload: Optional[Mapping[str, Any]] = None,
    envelope: Optional[EventEnvelope] = None,
) -> Transition:
    payload = payload or {}
    world.tick += 1
    start = world.state

    if world.state in (State.ACCEPTED, State.REJECTED) and event not in (Event.OPEN_TASK,):
        return Transition(start, event, "terminal", world.state, "ignore — terminal")
    if world.state == State.HUMAN_REQUIRED and event not in (
        Event.ADJUDICATE,
        Event.REJECT,
        Event.OPEN_TASK,
    ):
        return Transition(start, event, "human sticky", world.state, "wait allowed-issuer human record")

    if event == Event.EXECUTOR_DONE:
        world.executor_asserted_accepted = bool(payload.get("assert_accepted", False))
        world.candidate_status_field = payload.get("candidate_status")
        world.claimed_trust = payload.get("claimed_trust")
        if world.state == State.OPEN:
            world.state = State.VERIFYING
        return Transition(start, event, "always", world.state, "VERIFY_START / decide")

    if event == Event.VERIFY_START:
        if world.state == State.OPEN:
            world.state = State.VERIFYING
        return Transition(start, event, "from OPEN or VERIFYING", world.state, "replay and bind")

    if event == Event.MECHANICAL_REPLAY:
        # B1: the source is the envelope's, never the payload's self-declaration.
        _note_payload_identity_claim(world, payload, event)
        source = _event_source(world, envelope)
        result = str(payload.get("result", "FAIL"))
        world.mechanical_replay = result
        world.mechanical_replay_attester = source
        if source not in TRUSTED_MECHANICAL_SOURCES:
            _set_ob(world, "mechanical.tests", ObligationState.PENDING, source="REJECTED_MINT")
            world.notes.append("MC-B: MECHANICAL_REPLAY from %s cannot satisfy mechanical.tests" % source)
            guard = "untrusted event source — leave PENDING"
        elif result == "PASS":
            _set_ob(
                world,
                "mechanical.tests",
                ObligationState.SATISFIED,
                source="DERIVED",
                attester=source,
                evidence_digest=str(payload.get("digest") or "mechanical.pass"),
            )
            guard = "trusted envelope AND result=PASS"
        elif result == "FAIL":
            _set_ob(world, "mechanical.tests", ObligationState.ADVERSE, source="DERIVED", attester=source)
            guard = "trusted envelope AND result=FAIL"
        else:
            _set_ob(world, "mechanical.tests", ObligationState.PENDING, source="DERIVED", attester=source)
            guard = "trusted envelope AND result unknown"
        if world.state in (State.OPEN, State.WAITING_RECOVERABLE, State.REQUIRES_RE_ADJUDICATION):
            world.state = State.VERIFYING
        return Transition(start, event, guard, world.state, "decide")

    if event == Event.REVIEW_CLAIM:
        _note_payload_identity_claim(world, payload, event)
        claim = payload.get("claim")
        if not isinstance(claim, ReviewClaim):
            findings = payload.get("findings", None)
            claim = ReviewClaim(
                invocation_id=str(payload.get("invocation_id", "")),
                context_manifest_digest=str(
                    payload.get("context_manifest_digest", payload.get("context_digest", ""))
                ),
                files_used=tuple(payload.get("files_used") or ()),
                findings=None if findings is None else tuple(findings),
                reviewer_identity=str(payload.get("reviewer_identity", "")),
                completion_status=str(payload.get("completion_status", "")),
                launcher_capture_ref=str(payload.get("launcher_capture_ref", "")),
                verdict_text=str(payload.get("verdict_text", "")),
            )
        world.review_claims.append(claim)

        guard, ob_state = _dispose_review_claim(world, claim)
        _set_ob(
            world,
            "review.claim",
            ob_state,
            source="CLAIM" if ob_state == ObligationState.SATISFIED else "CONTEXT_BINDING",
            attester=Issuer.REVIEWER.value if ob_state == ObligationState.SATISFIED else None,
            evidence_digest=claim.invocation_id or None,
        )
        if world.state != State.HUMAN_REQUIRED:
            world.state = State.VERIFYING
        return Transition(start, event, guard, world.state, "decide")

    if event == Event.VERIFIER_UNAVAILABLE:
        kind = str(payload.get("kind", "transient_read_error"))
        oid = str(payload.get("obligation", "mechanical.tests"))
        asked_reason = payload.get("human_reason")
        # C10: a candidate cannot mint a controller-owned escalation reason.
        for candidate_reason in (kind, asked_reason):
            if candidate_reason in CONTROLLER_OWNED_HUMAN_REASONS:
                world.notes.append(
                    "C10: payload asked for controller-owned reason %r — ignored" % candidate_reason
                )
        human_reason = None
        if kind in HUMAN_ESCALATION_CLOSED_SET and kind not in CONTROLLER_OWNED_HUMAN_REASONS:
            human_reason = kind
        elif asked_reason in HUMAN_ESCALATION_CLOSED_SET and asked_reason not in CONTROLLER_OWNED_HUMAN_REASONS:
            human_reason = str(asked_reason)
        if human_reason is not None:
            world.human_reason = human_reason
            world.human_gate_subject = oid
            world.state = State.HUMAN_REQUIRED
            return Transition(start, event, "closed-set human reason", world.state, "wait human")

        # B6: every remaining route — named recoverable kind OR unknown default —
        # consumes the same bounded budget under a bound retry identity.
        identity, exhausted = _enter_recoverable_failure(world, kind, oid)
        if exhausted:
            return Transition(start, event, "budget exhausted", world.state, "stop retry")
        return Transition(
            start,
            event,
            "recoverable (named or default) AND budget remaining",
            world.state,
            "retry identity=%s remaining=%d" % (identity, world.budgets[identity].remaining),
        )

    if event == Event.RETRY:
        if world.state != State.WAITING_RECOVERABLE:
            return Transition(start, event, "not waiting", world.state, "ignore")
        identity = str(payload.get("identity") or "")
        # B6: a wrong identity must not borrow another obligation's budget.
        if identity not in world.budgets:
            world.notes.append("B6: RETRY identity %r is not a bound recovery identity — ignored" % identity)
            return Transition(
                start,
                event,
                "unknown retry identity",
                world.state,
                "ignore — cannot borrow another budget",
            )
        budget = world.budgets[identity]
        if budget.remaining <= 0:
            world.state = State.HUMAN_REQUIRED
            world.human_reason = "automated_recovery_budget_exhausted"
            return Transition(start, event, "no budget", world.state, "escalate closed-set")
        for oid, ob in list(world.obligations.items()):
            if ob.state == ObligationState.UNAVAILABLE_RECOVERABLE:
                _set_ob(world, oid, ObligationState.PENDING, source="RETRY")
        world.state = State.VERIFYING
        return Transition(start, event, "budget remaining", world.state, "re-enter VERIFYING")

    if event == Event.STALE_BINDING:
        # A stale binding is a recoverable failure of `interval.binding`; it uses
        # the same bounded budget, but its waiting state is re-adjudication.
        _recompute_interval(world)
        identity, exhausted = _enter_recoverable_failure(
            world,
            "stale_observation",
            "interval.binding",
            waiting_state=State.REQUIRES_RE_ADJUDICATION,
            affected_state=ObligationState.INVALIDATED,
        )
        if exhausted:
            return Transition(start, event, "stale exhausted", world.state, "human closed-set")
        return Transition(start, event, "OPEN/CLOSE disagree", world.state, "REDERIVE (not human)")

    if event == Event.REDERIVE:
        if world.state not in (State.REQUIRES_RE_ADJUDICATION, State.WAITING_RECOVERABLE, State.VERIFYING):
            return Transition(start, event, "wrong state", world.state, "ignore")
        for pid, val in (payload.get("close_predicates") or {}).items():
            world.close_obs[pid] = Observation(predicate_id=pid, value=str(val), window="CLOSE")
        # B3: recompute the whole domain; a partial rederive cannot keep an old PASS.
        state = _recompute_interval(world)
        if state == ObligationState.ADVERSE:
            world.state = State.REJECTED
            return Transition(start, event, "OPEN adverse persisted", world.state, "REJECTED")
        if state != ObligationState.SATISFIED:
            world.state = State.REQUIRES_RE_ADJUDICATION
            return Transition(
                start,
                event,
                "close incomplete or not PASS over the full domain",
                world.state,
                "do not keep old PASS",
            )
        world.state = State.VERIFYING
        return Transition(start, event, "close rederived over full domain", world.state, "decide")

    if event == Event.CLOSE_WINDOW:
        observed = payload.get("oracle_observed")
        if observed is not None:
            world.oracle_observed = frozenset(observed)
        for pid, val in (payload.get("close_predicates") or {}).items():
            world.close_obs[pid] = Observation(predicate_id=pid, value=str(val), window="CLOSE")
        _recompute_interval(world)
        if world.state not in (State.REJECTED, State.HUMAN_REQUIRED, State.ACCEPTED):
            world.state = State.VERIFYING
        return Transition(start, event, "close bound", world.state, "decide")

    if event == Event.ADVERSE_BLOCKER:
        oid = str(payload.get("obligation", "mechanical.tests"))
        _set_ob(world, oid, ObligationState.ADVERSE, source="DERIVED")
        world.state = State.REJECTED
        return Transition(start, event, "real blocker", world.state, "must not ACCEPTED")

    if event == Event.HUMAN_GATE:
        reason = str(payload.get("reason", "destructive_action_approval"))
        oid = str(payload.get("obligation", "mechanical.tests"))
        # C10: budget exhaustion is controller-owned; a payload cannot ask for it.
        if reason in CONTROLLER_OWNED_HUMAN_REASONS and not _budget_actually_exhausted(world):
            world.notes.append(
                "C10: HUMAN_GATE asked for %r without exhausted controller recovery state — refused" % reason
            )
            identity, exhausted = _enter_recoverable_failure(world, "missing_recomputable_evidence", oid)
            guard = "controller-owned reason refused; treated as recoverable"
            return Transition(start, event, guard, world.state, "retry identity=%s" % identity)
        if reason not in HUMAN_ESCALATION_CLOSED_SET:
            identity, exhausted = _enter_recoverable_failure(
                world, reason if reason in RECOVERABLE_FAILURE_KINDS else "transient_read_error", oid
            )
            return Transition(
                start, event, "reason not in closed set", world.state, "retry identity=%s" % identity
            )
        world.human_reason = reason
        if reason == "destructive_action_approval" and "human.destructive_approval" in world.required_obligation_ids:
            _set_ob(world, "human.destructive_approval", ObligationState.UNAVAILABLE_HUMAN, source="GATE")
            world.human_gate_subject = "human.destructive_approval"
        else:
            world.human_gate_subject = oid
        world.state = State.HUMAN_REQUIRED
        return Transition(start, event, "reason in closed set", world.state, "wait human")

    if event == Event.ADJUDICATE:
        _note_payload_identity_claim(world, payload, event)
        return _apply_adjudication(world, payload, envelope, start, event)

    if event == Event.REJECT:
        world.state = State.REJECTED
        return Transition(start, event, "explicit", world.state, "terminal")

    return Transition(start, event, "unhandled", world.state, "no-op")


# ---------------------------------------------------------------------------
# B4 — reviewer CLAIM disposition against the launcher capture
# ---------------------------------------------------------------------------


def _dispose_review_claim(world: World, claim: ReviewClaim) -> Tuple[str, ObligationState]:
    """A CLAIM only satisfies when a launcher capture corroborates it.

    The reviewer's own `files_used` / digest never become truth: the allowlist
    check runs against the launcher-captured file set.
    """
    missing = [f for f in REVIEW_CLAIM_REQUIRED_FIELDS if not str(getattr(claim, f, "")).strip()]
    if claim.findings is None:
        missing.append("findings")
    if not claim.files_used:
        missing.append("files_used")
    if missing:
        world.notes.append("B4: review claim missing required fields %s" % sorted(set(missing)))
        return "claim below minimum schema", ObligationState.PENDING

    if claim.role != "CLAIM":
        return "reviewer output role must be CLAIM", ObligationState.ADVERSE

    capture = world.review_captures.get(claim.invocation_id)
    if capture is None:
        world.notes.append(
            "B4: no launcher capture for review invocation %r — self-reported context is not truth"
            % claim.invocation_id
        )
        return "no launcher capture for this invocation", ObligationState.PENDING

    if claim.launcher_capture_ref != capture.invocation_id:
        return "claim does not reference its own launcher capture", ObligationState.ADVERSE

    # The launcher capture is the authority on what was actually read.
    world.actual_review_files = tuple(capture.files_used)

    if world.captured_review_context_digest and capture.context_digest != world.captured_review_context_digest:
        return "captured review context digest != OPEN manifest digest", ObligationState.ADVERSE
    if claim.context_manifest_digest != capture.context_digest:
        return "claimed context digest != launcher capture", ObligationState.ADVERSE
    if tuple(claim.files_used) != tuple(capture.files_used):
        return "claimed files_used != launcher-captured files_used", ObligationState.ADVERSE

    extra = set(capture.files_used) - set(world.allowed_review_files)
    if extra:
        world.notes.append("F02b: review used files outside allowed manifest: %s" % sorted(extra))
        return "captured context outside allowed manifest", ObligationState.ADVERSE

    return "claim stored and corroborated by launcher capture", ObligationState.SATISFIED


# ---------------------------------------------------------------------------
# B5 — adjudication: exact subject / scope / resolution / issuer / expiry
# ---------------------------------------------------------------------------


def _adjudication_defects(world: World, rec: Mapping[str, Any], issuer: str, resolution: str) -> List[str]:
    defects: List[str] = []
    subject = str(rec.get("subject", ""))
    scope = str(rec.get("scope", ""))
    provenance = str(rec.get("provenance_digest", ""))
    try:
        expires = int(rec.get("expires_tick"))
    except (TypeError, ValueError):
        expires = -1
        defects.append("expires_tick missing or not an integer")
    try:
        issued = int(rec.get("issued_tick", 0))
    except (TypeError, ValueError):
        issued = -1
        defects.append("issued_tick not an integer")

    if issuer not in TRUSTED_ADJUDICATION_ISSUERS:
        defects.append("issuer %s not an allowed adjudication issuer" % issuer)
    if not subject:
        defects.append("subject missing")
    elif subject not in world.obligations and subject not in ADJUDICABLE_VIRTUAL_SUBJECTS:
        defects.append("subject %r is not an obligation of this task" % subject)
    if scope != ADJUDICATION_SCOPE:
        defects.append("scope %r != %r" % (scope, ADJUDICATION_SCOPE))
    if resolution not in ("SATISFIED", "ADVERSE"):
        defects.append("resolution %r is not a typed resolution" % resolution)
    if not provenance:
        defects.append("provenance_digest missing")
    if expires >= 0 and expires <= world.tick:
        defects.append("record already expired at tick %d" % world.tick)
    if issued >= 0 and issued > world.tick:
        defects.append("issued_tick is in the future")
    if expires >= 0 and issued >= 0 and expires <= issued:
        defects.append("lifetime is empty (expires <= issued)")
    # A human obligation needs a human. A pinned controller cannot self-serve one.
    if subject.startswith("human.") and issuer != Issuer.HUMAN_OPERATOR.value:
        defects.append("human obligation %r requires issuer HUMAN_OPERATOR" % subject)
    return defects


def _apply_adjudication(
    world: World,
    payload: Mapping[str, Any],
    envelope: Optional[EventEnvelope],
    start: State,
    event: Event,
) -> Transition:
    rec = payload.get("freeze") or payload.get("record") or {}
    # B1/B5: the issuer is the envelope's source, not a payload field.
    issuer = _event_source(world, envelope)
    resolution = str(payload.get("resolution", ""))

    if issuer == Issuer.CANDIDATE_EXECUTOR.value:
        world.notes.append("F13: candidate-minted freeze ignored")
        return Transition(start, event, "issuer=CANDIDATE", world.state, "ignore freeze")

    defects = _adjudication_defects(world, rec, issuer, resolution)
    if defects:
        world.notes.append("B5: adjudication record rejected: %s" % "; ".join(defects))
        return Transition(start, event, "record does not match exactly", world.state, "ignore freeze")

    subject = str(rec["subject"])
    freeze = FreezeRecord(
        record_id=str(rec.get("record_id", "fr-%d" % world.tick)),
        issuer=issuer,
        scope=str(rec["scope"]),
        subject=subject,
        expires_tick=int(rec["expires_tick"]),
        provenance_digest=str(rec["provenance_digest"]),
        issued_tick=int(rec.get("issued_tick", 0)),
        resolution=resolution,
    )
    world.freeze_records.append(freeze)

    if subject in world.obligations:
        if resolution == "SATISFIED":
            _set_ob(
                world,
                subject,
                ObligationState.SATISFIED,
                source="ATTESTATION",
                attester=issuer,
                backing_record_id=freeze.record_id,
            )
        else:
            # ADVERSE never discharges anything. It preserves the adverse finding.
            _set_ob(
                world,
                subject,
                ObligationState.ADVERSE,
                source="ATTESTATION",
                attester=issuer,
                backing_record_id=freeze.record_id,
            )

    if resolution == "ADVERSE":
        world.notes.append("B5: resolution=ADVERSE preserves the blocker on %r" % subject)
        return Transition(start, event, "allowed issuer, ADVERSE preserved", world.state, "decide")

    if world.state == State.REQUIRES_RE_ADJUDICATION:
        world.state = State.VERIFYING
    elif world.state == State.HUMAN_REQUIRED:
        # Only the record for THIS gate's subject releases THIS gate.
        if world.human_gate_subject and subject == world.human_gate_subject:
            world.state = State.VERIFYING
            world.human_reason = None
            world.human_gate_subject = None
        else:
            world.notes.append(
                "B5: record for %r does not address the active human gate on %r"
                % (subject, world.human_gate_subject)
            )
            return Transition(start, event, "record does not address this gate", world.state, "wait human")
    return Transition(start, event, "allowed issuer, exact match", world.state, "decide")


def _revalidate_adjudications(world: World) -> List[str]:
    """B5: expiry/scope/subject are re-checked at every decide(), not only at write."""
    blockers: List[str] = []
    by_id = {r.record_id: r for r in world.freeze_records}
    for oid, ob in list(world.obligations.items()):
        if ob.source != "ATTESTATION" or not ob.backing_record_id:
            continue
        rec = by_id.get(ob.backing_record_id)
        if rec is None or rec.subject != oid or rec.scope != ADJUDICATION_SCOPE:
            reason = "backing adjudication record for %s is missing or mismatched" % oid
        elif rec.expires_tick <= world.tick:
            reason = "adjudication for %s expired at tick %d (now %d)" % (oid, rec.expires_tick, world.tick)
        else:
            continue
        blockers.append(reason)
        world.notes.append("B5: %s" % reason)
        if oid.startswith("human."):
            _set_ob(world, oid, ObligationState.UNAVAILABLE_HUMAN, source="ATTESTATION_EXPIRED")
            world.state = State.HUMAN_REQUIRED
            world.human_reason = "destructive_action_approval" if "destructive" in oid else "intentional_human_gate"
            world.human_gate_subject = oid
        else:
            _set_ob(world, oid, ObligationState.PENDING, source="ATTESTATION_EXPIRED")
    return blockers


# ---------------------------------------------------------------------------
# C7 — oracle lineage is 1:1 and authenticated
# ---------------------------------------------------------------------------


def _oracle_ok(world: World) -> Tuple[bool, str]:
    expected = set(world.oracle_expected)
    if not expected:
        # MC-A: an empty domain is never satisfaction, and is never back-filled.
        _set_ob(world, "oracle.set", ObligationState.PENDING, source="POLICY_FLOOR")
        return False, "empty oracle domain is not SATISFIED"

    lineage = dict(world.oracle_lineage)
    targets = list(lineage.values())
    lineage_defects: List[str] = []
    if len(set(targets)) != len(targets):
        lineage_defects.append("lineage is not injective (one new id claimed by several old ids)")
    for old, new in lineage.items():
        if new in expected:
            lineage_defects.append(
                "lineage target %r is itself an expected id — one observation cannot serve two ids" % new
            )
        if old == new:
            lineage_defects.append("lineage %r -> %r is not a rename" % (old, new))
    if lineage_defects:
        _set_ob(world, "oracle.set", ObligationState.ADVERSE, source="ORACLE_ATTRITION")
        return False, "oracle lineage rejected (%s arity): %s" % (ORACLE_LINEAGE_ARITY, "; ".join(lineage_defects))

    observed = set(world.oracle_observed)
    consumed: set = set()
    missing: List[str] = []
    for eid in sorted(expected):
        if eid in observed and eid not in consumed:
            consumed.add(eid)
            continue
        new = lineage.get(eid)
        if new and new in observed and new not in consumed:
            consumed.add(new)
            continue
        missing.append(eid)

    if missing:
        _set_ob(world, "oracle.set", ObligationState.ADVERSE, source="ORACLE_ATTRITION")
        return False, "missing expected oracle ids: %s" % sorted(missing)

    _set_ob(
        world,
        "oracle.set",
        ObligationState.SATISFIED,
        source="DERIVED",
        attester=Issuer.PINNED_CONTROLLER.value,
    )
    return True, "oracle set complete by id (1:1 lineage)"


def _trust_ok(world: World) -> Tuple[bool, str]:
    live = controller_trust_digest()
    root = world.launcher_trust
    if not root.authoritative or root.kernel_id != KERNEL_ID:
        return False, "placeholder or non-authoritative kernel"
    if root.captured_by != "LAUNCHER":
        return False, "trust root not launcher-captured"
    if root.controller_src_sha256 != live:
        return False, "controller src digest mismatch vs launcher capture"
    if world.claimed_trust is not None:
        claimed = world.claimed_trust
        if claimed.captured_by == Issuer.CANDIDATE_EXECUTOR.value:
            return False, "candidate-minted trust root rejected"
        if claimed.controller_src_sha256 != root.controller_src_sha256:
            return False, "claimed digest != launcher digest"
    if world.policy_sha256 != root.policy_sha256:
        return False, "policy digest moved after OPEN"
    _set_ob(
        world,
        "controller.trust",
        ObligationState.SATISFIED,
        source="DERIVED",
        attester=Issuer.LAUNCHER.value,
        evidence_digest=root.controller_src_sha256,
    )
    return True, "launcher-captured controller+policy digest matches live"


def _review_ok(world: World) -> Tuple[bool, str]:
    if "review.claim" not in world.required_obligation_ids:
        return True, "review not required"
    ob = world.obligations.get("review.claim")
    if ob is None or ob.state == ObligationState.PENDING:
        return False, "required review.claim missing — absence != satisfied"
    if ob.state == ObligationState.ADVERSE:
        return False, "review context binding failed"
    if not world.review_claims:
        return False, "reviewer output is CLAIM and no claim present"
    claim = world.review_claims[-1]
    if world.latest_review_pointer and world.latest_review_pointer != claim.invocation_id:
        return False, "movable latest-review pointer rejected"
    # Re-derive the disposition in THIS decide() rather than trusting the stored one.
    guard, state = _dispose_review_claim(world, claim)
    if state != ObligationState.SATISFIED:
        return False, "review claim not corroborated: %s" % guard
    return ob.state == ObligationState.SATISFIED, "review claim bound (%s)" % guard


def _interval_ok(world: World) -> Tuple[bool, str]:
    if not world.close_obs:
        return False, "CLOSE window not bound — entrance-only is forbidden"
    # B3: derived in THIS decide() over the full domain; never a stored PASS.
    state = _recompute_interval(world)
    if state == ObligationState.SATISFIED:
        return True, "interval SATISFIED: OPEN+CLOSE PASS over the whole required domain"
    detail = []
    for pid, o in sorted(world.open_obs.items()):
        c = world.close_obs.get(pid)
        detail.append("%s open=%s close=%s" % (pid, o.value, c.value if c else "ABSENT"))
    return False, "interval %s over the full domain (%s)" % (state.value, "; ".join(detail))


def _totality_ok(world: World) -> Tuple[bool, str]:
    """INV-1: EVERY required obligation must be SATISFIED. Not merely present,
    not merely non-adverse, not merely non-pending."""
    unsatisfied: List[str] = []
    for oid in world.required_obligation_ids:
        ob = world.obligations.get(oid)
        if ob is None:
            unsatisfied.append("%s=ABSENT" % oid)
            continue
        if ob.state != ObligationState.SATISFIED:
            unsatisfied.append("%s=%s" % (oid, ob.state.value))
            continue
        if ob.source == "POLICY_FLOOR":
            unsatisfied.append("%s=floor-existence-not-satisfaction" % oid)
    if unsatisfied:
        return False, "required obligations not SATISFIED: %s" % sorted(unsatisfied)
    return True, "every required obligation is SATISFIED"


def decide(world: World) -> Decision:
    """Pure derivation of ACCEPTED. Candidate status field is ignored (MC-B)."""
    reasons: List[str] = []
    blockers: List[str] = []
    digest = controller_trust_digest()

    if world.executor_asserted_accepted or world.candidate_status_field == "ACCEPTED":
        reasons.append("MC-B: executor-written ACCEPTED ignored")

    # B5: re-check every adjudication-backed disposition before anything else,
    # so an approval that expired since it was written cannot still be spent.
    expired = _revalidate_adjudications(world)

    if world.state == State.REJECTED:
        return Decision(State.REJECTED, tuple(reasons), tuple(blockers + ["already REJECTED"]), digest, False)
    if world.state == State.HUMAN_REQUIRED:
        return Decision(
            State.HUMAN_REQUIRED,
            tuple(reasons),
            tuple(blockers + expired + ["human:%s" % world.human_reason]),
            digest,
            False,
        )
    if world.state == State.WAITING_RECOVERABLE:
        return Decision(State.WAITING_RECOVERABLE, tuple(reasons), tuple(blockers + ["recoverable wait"]), digest, False)
    if world.state == State.REQUIRES_RE_ADJUDICATION:
        return Decision(State.REQUIRES_RE_ADJUDICATION, tuple(reasons), tuple(blockers + ["must rederive"]), digest, False)
    if world.state == State.OPEN:
        return Decision(State.OPEN, tuple(reasons), tuple(blockers + ["not verifying yet"]), digest, False)

    blockers.extend(expired)

    checks = (
        _trust_ok(world),
        _oracle_ok(world),
        _interval_ok(world),
        _review_ok(world),
        _totality_ok(world),
    )
    trust_ok = checks[0][0]
    for ok, msg in checks:
        if ok:
            reasons.append(msg)
        else:
            blockers.append(msg)

    mech = world.obligations.get("mechanical.tests")
    if mech is None or mech.state != ObligationState.SATISFIED:
        blockers.append("mechanical.tests not SATISFIED by derivation/attestation")
    elif mech.attester not in TRUSTED_MECHANICAL_SOURCES:
        blockers.append("mechanical.tests attester %r is not a trusted source — MC-B" % mech.attester)
    if world.mechanical_replay != "PASS":
        blockers.append("mechanical replay this decide() is not PASS")
    elif world.mechanical_replay_attester not in TRUSTED_MECHANICAL_SOURCES:
        blockers.append(
            "mechanical replay came from %r, not a trusted event source — MC-B"
            % world.mechanical_replay_attester
        )

    if world.review_claims:
        claim = world.review_claims[-1]
        semantic = [f for f in (claim.findings or ()) if isinstance(f, dict) and f.get("kind") == "semantic"]
        if semantic:
            resolved = any(
                fr.subject == "review.semantic"
                and fr.resolution == "SATISFIED"
                and fr.issuer in TRUSTED_ADJUDICATION_ISSUERS
                and fr.scope == ADJUDICATION_SCOPE
                for fr in world.freeze_records
                if fr.expires_tick > world.tick
            )
            if not resolved:
                blockers.append("semantic findings need typed adjudication (reviewer CLAIM is not DECISION)")

    if "human.destructive_approval" in world.required_obligation_ids:
        hob = world.obligations.get("human.destructive_approval")
        if hob is None or hob.state != ObligationState.SATISFIED:
            blockers.append("destructive approval not SATISFIED")
            if world.state != State.HUMAN_REQUIRED:
                world.state = State.HUMAN_REQUIRED
                world.human_reason = "destructive_action_approval"
                world.human_gate_subject = "human.destructive_approval"
            return Decision(State.HUMAN_REQUIRED, tuple(reasons), tuple(blockers), digest, trust_ok)

    for oid in world.required_obligation_ids:
        ob = world.obligations.get(oid)
        if ob and ob.state in (
            ObligationState.ADVERSE,
            ObligationState.INVALIDATED,
            ObligationState.UNAVAILABLE_HUMAN,
            ObligationState.UNAVAILABLE_RECOVERABLE,
        ):
            blockers.append("%s=%s" % (oid, ob.state.value))

    if blockers:
        return Decision(
            world.state if world.state != State.ACCEPTED else State.VERIFYING,
            tuple(reasons),
            tuple(blockers),
            digest,
            trust_ok,
        )

    world.state = State.ACCEPTED
    reasons.append("INV-1..4 hold; ACCEPTED derived")
    return Decision(State.ACCEPTED, tuple(reasons), tuple(), digest, trust_ok)


def run_until(world: World, events: List[Tuple[Event, Dict[str, Any]]]) -> Decision:
    last = Decision(world.state, tuple(), tuple(["no events"]), controller_trust_digest(), False)
    for event, payload in events:
        apply(world, event, payload)
        last = decide(world)
        if world.state in (State.ACCEPTED, State.REJECTED):
            return last
    return last
