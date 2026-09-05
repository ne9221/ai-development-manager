"""V5 safety + reachability kernel — executable reference model.

This module is the mechanical contract. KERNEL_V5.md is the prose twin.
Production manager/ runtime must not import this in this slice.
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

DEFAULT_RETRY_BUDGET = 3

V5_STATE_MODEL = [s.value for s in State]
OBLIGATION_STATE_MODEL = [s.value for s in ObligationState]
REVIEWER_OUTPUT_ROLE = "CLAIM"
REVIEW_CONTEXT_BINDING = "LAUNCHER_CAPTURED_MANIFEST_DIGEST"
INVALIDATION_POLICY = "ASYMMETRIC"
CONTROLLER_TRUST_ROOT = "LAUNCHER_CAPTURED_CONTROLLER_AND_POLICY_DIGEST"


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
        terminal_condition="ACCEPTED is derived-terminal",
        human_escalation_eligible=False,
    ),
}


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    required: bool
    state: ObligationState
    source: str
    evidence_digest: Optional[str] = None
    attester: Optional[str] = None


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


@dataclass(frozen=True)
class ReviewClaim:
    invocation_id: str
    context_digest: str
    files_used: Tuple[str, ...]
    verdict_text: str
    findings: Tuple[Any, ...]
    role: str = "CLAIM"


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
    expected = frozenset(launcher.get("oracle_expected") or policy.get("oracle_expected") or ["oracle.unit"])
    allowed = frozenset(launcher.get("allowed_review_files") or ("review/policy.md", "task/spec.md"))
    captured_ctx = str(launcher.get("captured_review_context_digest") or "")
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
        notes=[],
    )


def _set_ob(world: World, oid: str, state: ObligationState, **kwargs: Any) -> None:
    current = world.obligations.get(oid)
    if current is None:
        current = Obligation(obligation_id=oid, required=True, state=ObligationState.PENDING, source="POLICY_FLOOR")
    world.obligations[oid] = replace(current, state=state, **kwargs)


def _budget(world: World, identity: str) -> RetryBudget:
    if identity not in world.budgets:
        world.budgets[identity] = RetryBudget(identity=identity, used=0, maximum=DEFAULT_RETRY_BUDGET)
    return world.budgets[identity]


def apply(world: World, event: Event, payload: Optional[Mapping[str, Any]] = None) -> Transition:
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
        attester = str(payload.get("attester", Issuer.LAUNCHER.value))
        result = str(payload.get("result", "FAIL"))
        world.mechanical_replay = result
        world.mechanical_replay_attester = attester
        if attester == Issuer.CANDIDATE_EXECUTOR.value:
            _set_ob(world, "mechanical.tests", ObligationState.PENDING, source="REJECTED_MINT")
        elif result == "PASS" and attester in (Issuer.LAUNCHER.value, Issuer.PINNED_CONTROLLER.value):
            _set_ob(
                world,
                "mechanical.tests",
                ObligationState.SATISFIED,
                source="DERIVED",
                attester=attester,
                evidence_digest=str(payload.get("digest") or "mechanical.pass"),
            )
        elif result == "FAIL":
            _set_ob(world, "mechanical.tests", ObligationState.ADVERSE, source="DERIVED", attester=attester)
        if world.state in (State.OPEN, State.WAITING_RECOVERABLE, State.REQUIRES_RE_ADJUDICATION):
            world.state = State.VERIFYING
        return Transition(start, event, "attester!=CANDIDATE", world.state, "decide")

    if event == Event.REVIEW_CLAIM:
        claim = payload.get("claim")
        if not isinstance(claim, ReviewClaim):
            claim = ReviewClaim(
                invocation_id=str(payload.get("invocation_id", "")),
                context_digest=str(payload.get("context_digest", "")),
                files_used=tuple(payload.get("files_used") or ()),
                verdict_text=str(payload.get("verdict_text", "")),
                findings=tuple(payload.get("findings") or ()),
            )
        world.review_claims.append(claim)
        world.actual_review_files = claim.files_used
        if world.captured_review_context_digest == "":
            world.captured_review_context_digest = claim.context_digest
        extra = set(claim.files_used) - set(world.allowed_review_files)
        if extra:
            _set_ob(world, "review.claim", ObligationState.ADVERSE, source="CONTEXT_BINDING")
            world.notes.append("F02b: review used files outside allowed manifest")
        else:
            _set_ob(
                world,
                "review.claim",
                ObligationState.SATISFIED,
                source="CLAIM",
                attester=Issuer.REVIEWER.value,
                evidence_digest=claim.invocation_id,
            )
        if world.state != State.HUMAN_REQUIRED:
            world.state = State.VERIFYING
        return Transition(start, event, "claim stored; verdict is not a decision", world.state, "decide")

    if event == Event.VERIFIER_UNAVAILABLE:
        kind = str(payload.get("kind", "transient_read_error"))
        world.last_failure_kind = kind
        identity = f"unavailable:{kind}:{payload.get('obligation', 'mechanical.tests')}"
        budget = _budget(world, identity)
        if kind in RECOVERABLE_FAILURE_KINDS:
            budget = RetryBudget(identity, budget.used + 1, budget.maximum)
            world.budgets[identity] = budget
            oid = str(payload.get("obligation", "mechanical.tests"))
            if budget.used > budget.maximum:
                if kind in RECOVERABLE_FAILURE_KINDS:
                    world.state = State.HUMAN_REQUIRED
                    world.human_reason = "automated_recovery_budget_exhausted"
                    _set_ob(world, oid, ObligationState.UNAVAILABLE_HUMAN, source="BUDGET")
                else:
                    world.state = State.REJECTED
                    _set_ob(world, oid, ObligationState.ADVERSE, source="BUDGET")
                return Transition(start, event, "budget exhausted", world.state, "stop retry")
            world.state = State.WAITING_RECOVERABLE
            _set_ob(world, oid, ObligationState.UNAVAILABLE_RECOVERABLE, source="TRANSIENT")
            return Transition(
                start,
                event,
                "recoverable AND budget remaining",
                world.state,
                f"retry identity={identity} remaining={budget.remaining}",
            )
        if kind in HUMAN_ESCALATION_CLOSED_SET or payload.get("human_reason") in HUMAN_ESCALATION_CLOSED_SET:
            world.state = State.HUMAN_REQUIRED
            world.human_reason = str(payload.get("human_reason") or kind)
            return Transition(start, event, "closed-set human reason", world.state, "wait human")
        world.state = State.WAITING_RECOVERABLE
        return Transition(start, event, "default recoverable", world.state, "retry")

    if event == Event.RETRY:
        if world.state != State.WAITING_RECOVERABLE:
            return Transition(start, event, "not waiting", world.state, "ignore")
        identity = str(payload.get("identity") or world.last_failure_kind or "retry")
        budget = world.budgets.get(identity) or next(iter(world.budgets.values()), None)
        if budget is None or budget.remaining <= 0:
            world.state = State.HUMAN_REQUIRED
            world.human_reason = "automated_recovery_budget_exhausted"
            return Transition(start, event, "no budget", world.state, "escalate closed-set")
        for oid, ob in list(world.obligations.items()):
            if ob.state == ObligationState.UNAVAILABLE_RECOVERABLE:
                _set_ob(world, oid, ObligationState.PENDING, source="RETRY")
        world.state = State.VERIFYING
        return Transition(start, event, "budget remaining", world.state, "re-enter VERIFYING")

    if event == Event.STALE_BINDING:
        world.last_failure_kind = "stale_observation"
        for pid, obs in world.open_obs.items():
            close = world.close_obs.get(pid)
            if close is None or close.value != obs.value:
                _set_ob(world, "interval.binding", ObligationState.INVALIDATED, source="ASYMMETRIC")
        identity = "stale_observation:interval.binding"
        budget = _budget(world, identity)
        world.budgets[identity] = RetryBudget(identity, budget.used + 1, budget.maximum)
        if world.budgets[identity].used > world.budgets[identity].maximum:
            world.state = State.HUMAN_REQUIRED
            world.human_reason = "automated_recovery_budget_exhausted"
            return Transition(start, event, "stale exhausted", world.state, "human closed-set")
        world.state = State.REQUIRES_RE_ADJUDICATION
        return Transition(start, event, "OPEN/CLOSE disagree", world.state, "REDERIVE (not human)")

    if event == Event.REDERIVE:
        if world.state not in (State.REQUIRES_RE_ADJUDICATION, State.WAITING_RECOVERABLE, State.VERIFYING):
            return Transition(start, event, "wrong state", world.state, "ignore")
        close_preds = payload.get("close_predicates") or {}
        for pid, val in close_preds.items():
            world.close_obs[pid] = Observation(predicate_id=pid, value=str(val), window="CLOSE")
        adverse_kept = False
        for pid, o in world.open_obs.items():
            c = world.close_obs.get(pid)
            if o.value == "ADVERSE" and (c is None or c.value == "MISSING"):
                adverse_kept = True
                _set_ob(world, "interval.binding", ObligationState.ADVERSE, source="ASYMMETRIC")
            elif o.value == "PASS" and c is not None and c.value == "FAIL":
                _set_ob(world, "interval.binding", ObligationState.INVALIDATED, source="ASYMMETRIC")
                world.state = State.REQUIRES_RE_ADJUDICATION
                return Transition(start, event, "OPEN PASS / CLOSE FAIL", world.state, "do not keep PASS")
            elif c is not None and c.value == "PASS" and o.value in ("PASS",):
                _set_ob(
                    world,
                    "interval.binding",
                    ObligationState.SATISFIED,
                    source="DERIVED",
                    attester=Issuer.PINNED_CONTROLLER.value,
                )
        if adverse_kept:
            world.state = State.REJECTED
            return Transition(start, event, "OPEN adverse persisted", world.state, "REJECTED")
        world.state = State.VERIFYING
        return Transition(start, event, "close rederived", world.state, "decide")

    if event == Event.CLOSE_WINDOW:
        observed = payload.get("oracle_observed")
        if observed is not None:
            world.oracle_observed = frozenset(observed)
        close_preds = payload.get("close_predicates") or {}
        for pid, val in close_preds.items():
            world.close_obs[pid] = Observation(predicate_id=pid, value=str(val), window="CLOSE")
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
        if reason not in HUMAN_ESCALATION_CLOSED_SET:
            world.last_failure_kind = reason if reason in RECOVERABLE_FAILURE_KINDS else "transient_read_error"
            fake_payload = {"kind": world.last_failure_kind, "obligation": payload.get("obligation", "mechanical.tests")}
            return apply(world, Event.VERIFIER_UNAVAILABLE, fake_payload)
        world.human_reason = reason
        if "human.destructive_approval" in world.required_obligation_ids:
            _set_ob(world, "human.destructive_approval", ObligationState.UNAVAILABLE_HUMAN, source="GATE")
        world.state = State.HUMAN_REQUIRED
        return Transition(start, event, "reason in closed set", world.state, "wait human")

    if event == Event.ADJUDICATE:
        rec = payload.get("freeze") or payload.get("record") or {}
        issuer = str(rec.get("issuer", Issuer.CANDIDATE_EXECUTOR.value))
        freeze = FreezeRecord(
            record_id=str(rec.get("record_id", f"fr-{world.tick}")),
            issuer=issuer,
            scope=str(rec.get("scope", "subject")),
            subject=str(rec.get("subject", "")),
            expires_tick=int(rec.get("expires_tick", world.tick + 10)),
            provenance_digest=str(rec.get("provenance_digest", "")),
        )
        if issuer == Issuer.CANDIDATE_EXECUTOR.value:
            world.notes.append("F13: candidate-minted freeze ignored")
            return Transition(start, event, "issuer=CANDIDATE", world.state, "ignore freeze")
        if issuer not in (Issuer.HUMAN_OPERATOR.value, Issuer.PINNED_CONTROLLER.value):
            return Transition(start, event, "issuer not allowed", world.state, "ignore freeze")
        if freeze.expires_tick < world.tick:
            return Transition(start, event, "expired", world.state, "ignore freeze")
        world.freeze_records.append(freeze)
        subject = freeze.subject
        if subject and subject in world.obligations:
            if payload.get("resolution") == "SATISFIED":
                _set_ob(world, subject, ObligationState.SATISFIED, source="ATTESTATION", attester=issuer)
            elif payload.get("resolution") == "ADVERSE":
                _set_ob(world, subject, ObligationState.ADVERSE, source="ATTESTATION", attester=issuer)
        if world.state == State.REQUIRES_RE_ADJUDICATION:
            world.state = State.VERIFYING
        if world.state == State.HUMAN_REQUIRED and issuer == Issuer.HUMAN_OPERATOR.value:
            if "human.destructive_approval" in world.obligations:
                _set_ob(
                    world,
                    "human.destructive_approval",
                    ObligationState.SATISFIED,
                    source="ATTESTATION",
                    attester=issuer,
                )
            world.state = State.VERIFYING
            world.human_reason = None
        return Transition(start, event, "allowed issuer", world.state, "decide")

    if event == Event.REJECT:
        world.state = State.REJECTED
        return Transition(start, event, "explicit", world.state, "terminal")

    return Transition(start, event, "unhandled", world.state, "no-op")


def _oracle_ok(world: World) -> Tuple[bool, str]:
    observed = set(world.oracle_observed)
    for old, new in world.oracle_lineage.items():
        if new in observed and old in world.oracle_expected:
            observed.add(old)
    missing = set(world.oracle_expected) - observed
    if missing:
        _set_ob(world, "oracle.set", ObligationState.ADVERSE, source="ORACLE_ATTRITION")
        return False, f"missing expected oracle ids: {sorted(missing)}"
    if world.oracle_expected:
        _set_ob(
            world,
            "oracle.set",
            ObligationState.SATISFIED,
            source="DERIVED",
            attester=Issuer.PINNED_CONTROLLER.value,
        )
        return True, "oracle set complete by id"
    _set_ob(world, "oracle.set", ObligationState.PENDING, source="POLICY_FLOOR")
    return False, "empty oracle domain is not SATISFIED"


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
    extra = set(claim.files_used) - set(world.allowed_review_files)
    if extra:
        return False, "review used disallowed context"
    if world.captured_review_context_digest and claim.context_digest != world.captured_review_context_digest:
        return False, "review context digest != launcher capture"
    if claim.role != "CLAIM":
        return False, "reviewer output role must be CLAIM"
    return ob.state == ObligationState.SATISFIED, "review claim bound"


def _interval_ok(world: World) -> Tuple[bool, str]:
    if not world.close_obs:
        return False, "CLOSE window not bound — entrance-only is forbidden"
    for pid, o in world.open_obs.items():
        c = world.close_obs.get(pid)
        if o.value == "ADVERSE" and (c is None or c.value in ("MISSING",)):
            return False, f"OPEN adverse {pid} disappeared — not clean"
        if o.value == "PASS" and c is not None and c.value in ("FAIL", "ADVERSE"):
            return False, f"OPEN PASS / CLOSE FAIL for {pid} — old PASS discarded"
        if c is None:
            return False, f"predicate {pid} missing CLOSE observation"
    ob = world.obligations.get("interval.binding")
    if ob and ob.state in (ObligationState.INVALIDATED, ObligationState.ADVERSE):
        return False, "interval obligation not SATISFIED"
    if ob is None or ob.state != ObligationState.SATISFIED:
        all_pass = all(
            world.close_obs.get(pid) and world.close_obs[pid].value == "PASS" and o.value in ("PASS",)
            for pid, o in world.open_obs.items()
        )
        if all_pass:
            _set_ob(
                world,
                "interval.binding",
                ObligationState.SATISFIED,
                source="DERIVED",
                attester=Issuer.PINNED_CONTROLLER.value,
            )
            return True, "open+close PASS"
        return False, "interval not derived SATISFIED"
    return True, "interval SATISFIED"


def _totality_ok(world: World) -> Tuple[bool, str]:
    for oid in world.required_obligation_ids:
        ob = world.obligations.get(oid)
        if ob is None:
            return False, f"required {oid} absent — floor should have created PENDING"
        if ob.state == ObligationState.PENDING:
            return False, f"required {oid} still PENDING"
        if ob.state == ObligationState.SATISFIED and ob.source == "POLICY_FLOOR":
            return False, f"{oid} floor is existence, not satisfaction"
    return True, "every required obligation has explicit non-pending disposition"


def decide(world: World) -> Decision:
    """Pure derivation of ACCEPTED. Candidate status field is ignored (MC-B)."""
    reasons: List[str] = []
    blockers: List[str] = []
    digest = controller_trust_digest()

    if world.executor_asserted_accepted or world.candidate_status_field == "ACCEPTED":
        reasons.append("MC-B: executor-written ACCEPTED ignored")

    if world.state == State.REJECTED:
        return Decision(State.REJECTED, tuple(reasons), tuple(blockers + ["already REJECTED"]), digest, False)
    if world.state == State.HUMAN_REQUIRED:
        return Decision(State.HUMAN_REQUIRED, tuple(reasons), tuple(blockers + [f"human:{world.human_reason}"]), digest, False)
    if world.state == State.WAITING_RECOVERABLE:
        return Decision(State.WAITING_RECOVERABLE, tuple(reasons), tuple(blockers + ["recoverable wait"]), digest, False)
    if world.state == State.REQUIRES_RE_ADJUDICATION:
        return Decision(State.REQUIRES_RE_ADJUDICATION, tuple(reasons), tuple(blockers + ["must rederive"]), digest, False)
    if world.state == State.OPEN:
        return Decision(State.OPEN, tuple(reasons), tuple(blockers + ["not verifying yet"]), digest, False)

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
    elif mech.attester == Issuer.CANDIDATE_EXECUTOR.value:
        blockers.append("mechanical.tests attester is candidate — MC-B")
    if world.mechanical_replay != "PASS":
        blockers.append("mechanical replay this decide() is not PASS")

    if world.review_claims:
        claim = world.review_claims[-1]
        semantic = [f for f in claim.findings if isinstance(f, dict) and f.get("kind") == "semantic"]
        if semantic:
            resolved = any(
                fr.subject == "review.semantic"
                and fr.issuer in (Issuer.HUMAN_OPERATOR.value, Issuer.PINNED_CONTROLLER.value)
                for fr in world.freeze_records
                if fr.expires_tick >= world.tick
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
            return Decision(State.HUMAN_REQUIRED, tuple(reasons), tuple(blockers), digest, trust_ok)

    for oid in world.required_obligation_ids:
        ob = world.obligations.get(oid)
        if ob and ob.state in (ObligationState.ADVERSE, ObligationState.INVALIDATED, ObligationState.UNAVAILABLE_HUMAN):
            blockers.append(f"{oid}={ob.state.value}")

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
