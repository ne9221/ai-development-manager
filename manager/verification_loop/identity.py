"""The (provider, account_id, session_id) identity triple and its resolution.

Phase B-1 carried identity as a single opaque string. The independent review
made the consequence concrete: ``"codex-impl"`` can never equal
``"unit_checker"``, so the executor-is-not-the-checker gate could only ever be
tripped artificially by a test, never by real data. Worse, an *unresolved*
identity skipped the check entirely and the run was ACCEPTED -- None was being
read as "no conflict" when it actually means "we do not know who did this".

Phase A v3 admission predicates 3-5 require three separate things, and this
module keeps them separate on purpose:

1. **Who claims to have produced this** -- the triple itself.
2. **Whether that claim was resolved rather than self-asserted** -- Session
   Center's classification status/confidence/method. A checker asserting its
   own identity proves nothing; ADM already has a resolver, and this reuses its
   vocabulary (``classified`` / ``needs_review`` / ``unclassified``,
   ``conflicting_deterministic_signals``) rather than inventing a second one.
3. **Whether it is the same actor as the executor** -- compared as a whole
   triple. Same provider and account but a different session counts as a
   different actor, per Phase A v3 C.3 predicate 4.

Every failure mode here fails closed: missing, blank, unresolved, low
confidence and conflicting-signal identities are all inadmissible. There is no
path on which an absent identity is treated as safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# Session Center vocabulary. Kept as module constants so the admission
# predicate and the tests refer to the same spelling, and so a mutation that
# widens one of them is visible in a diff rather than buried in a comparison.
CLASSIFIED = "classified"
HIGH_CONFIDENCE = "high"
REJECTED_METHODS: Tuple[str, ...] = ("conflicting_deterministic_signals", "unclassified")
RESOLVER = "session_center"


@dataclass(frozen=True)
class Identity:
    """A provider-side actor.

    ``provider_session_id`` is extra correlation evidence, not part of the
    comparison key: Phase A v3 compares the triple. It is still carried so a
    checker's report can be traced back to a provider-side record.
    """

    provider: str
    account_id: str
    session_id: str
    provider_session_id: Optional[str] = None

    @property
    def triple(self) -> Tuple[str, str, str]:
        return (self.provider, self.account_id, self.session_id)

    @property
    def is_complete(self) -> bool:
        """Whether all three comparison fields are actually populated.

        A triple with a blank member cannot be compared meaningfully -- two
        different actors both missing an account_id would compare equal on that
        field -- so a blank is treated as absent, not as a value.
        """
        return all(isinstance(v, str) and v.strip() for v in self.triple)


@dataclass(frozen=True)
class ResolvedIdentity:
    """An Identity plus the Session Center resolution that vouches for it."""

    identity: Identity
    status: str
    confidence: Optional[str]
    method: str
    resolved_by: str = RESOLVER


def resolution_failure(resolved: Optional[ResolvedIdentity]) -> Optional[str]:
    """Why ``resolved`` cannot be trusted as an identity, or None if it can.

    Ordered so the reported reason is deterministic. Returns a reason -- never
    a bool -- so an inadmissible report can say which of the five distinct
    ways identity resolution failed applies to it.
    """
    if resolved is None:
        return "IDENTITY_ABSENT"
    if not isinstance(resolved.identity, Identity) or not resolved.identity.is_complete:
        return "IDENTITY_INCOMPLETE"
    if resolved.resolved_by != RESOLVER:
        # Anything other than the Session Center resolver is a self-assertion.
        return "IDENTITY_NOT_RESOLVED_BY_SESSION_CENTER"
    if resolved.status != CLASSIFIED:
        return "IDENTITY_UNCLASSIFIED"
    if resolved.method in REJECTED_METHODS:
        return "IDENTITY_METHOD_UNRELIABLE"
    if resolved.confidence != HIGH_CONFIDENCE:
        return "IDENTITY_CONFIDENCE_LOW"
    return None


def same_actor(left: Optional[ResolvedIdentity], right: Optional[ResolvedIdentity]) -> bool:
    """Whether both sides resolve to the same (provider, account, session).

    Returns False when either side is missing. That is *not* "they differ, so
    independence holds" -- callers must reject an unresolved identity via
    :func:`resolution_failure` before asking this question. Keeping the two
    apart is what stops a None identity from being read as independence, which
    is exactly the B-1 fail-open the review found.
    """
    if left is None or right is None:
        return False
    return left.identity.triple == right.identity.triple
