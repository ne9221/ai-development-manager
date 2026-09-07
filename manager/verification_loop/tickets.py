"""Verification tickets: issued before a checker runs, consumed by its report.

Phase B-1 accepted any report whose fields happened to line up. That is
sufficient to catch an honest mistake and useless against a checker that simply
writes the fields it needs. Phase A v3's answer is ordering: the controller
issues a ticket *first*, naming the exact (execution, candidate, bundle, gate,
round) tuple, who is expected to answer it, and who is forbidden to. A report
with no matching ticket does not become a FAIL -- it does not enter derivation
at all.

``ticket_id`` is derived from the tuple it binds rather than assigned, which
gives two properties for free:

* It is the natural compare-and-swap key for the round (Phase A v3 I6). Two
  attempts to verify the same tuple collide on one id instead of quietly
  producing two independent answers.
* A forged ticket is self-refuting. Because the id is recomputable from the
  fields, a ticket whose id does not match its own contents is rejected without
  reference to any store.

The length-prefixed encoding below matters more than it looks. With a plain
separator, ``execution_id="a|b"`` with gate ``"c"`` and ``execution_id="a"``
with gate ``"b|c"`` would hash identically -- an attacker could move a boundary
and land on someone else's ticket id. Prefixing each field with its length
makes the encoding unambiguous.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Optional, Sequence

from .models import VerificationTicket

TICKET_ID_PREFIX = "vt-"

# The only fields a consumption record may differ from its issue record on.
# Everything else a ticket carries was frozen when the controller issued it,
# and a consumption record is a *state transition* of that ticket, not a
# restatement of it. Enumerated by exclusion on purpose: a field added to
# VerificationTicket later is bound the moment it exists, so nobody can widen
# the ticket with something a consumption record could quietly rewrite.
CONSUMPTION_MUTABLE_FIELDS = frozenset(
    {"status", "consumed_report_digest", "consumed_at", "ticket_seq"}
)


def issue_frozen_fields() -> tuple:
    """Every VerificationTicket field a consumption record may not change."""
    return tuple(
        field_.name
        for field_ in dataclasses.fields(VerificationTicket)
        if field_.name not in CONSUMPTION_MUTABLE_FIELDS
    )


def consumption_divergence_field(
    issued: VerificationTicket, consumption: VerificationTicket
) -> Optional[str]:
    """The first issue-frozen field ``consumption`` rewrites, or None.

    Phase B-2 final review residual NB-A: ``ticket_id`` hashes only
    (execution, candidate, bundle, gate, round), so ``expected_checker_identity``
    and every other field the issue record froze sat outside it, and a new,
    correctly named consumption record could restate them. The store's own
    ``consume()`` builds a consumption record by ``replace()`` on the issue
    record, so an honest one never differs here; any difference is a record
    the store did not write. Kept as a pure function so the binding can be
    tested and mutated directly, independent of the ledger around it.
    """
    for name in issue_frozen_fields():
        if getattr(issued, name) != getattr(consumption, name):
            return name
    return None


def _encode(*parts: object) -> str:
    encoded = []
    for part in parts:
        text = "" if part is None else str(part)
        encoded.append(str(len(text)) + ":" + text)
    return "|".join(encoded)


def derive_ticket_id(
    execution_id: str,
    candidate_sha: str,
    bundle_hash: str,
    gate_id: str,
    round_: int,
) -> str:
    """The one ticket id for this verification round tuple."""
    payload = _encode(execution_id, candidate_sha, bundle_hash, gate_id, round_)
    return TICKET_ID_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ticket_self_consistency_reason(ticket: VerificationTicket) -> Optional[str]:
    """Why this ticket is invalid on its own terms, or None.

    Checked before any store lookup: a ticket carrying an id that does not
    match its own fields was not issued by a controller following this
    protocol, whatever a store says about it.
    """
    expected = derive_ticket_id(
        ticket.execution_id,
        ticket.candidate_sha,
        ticket.bundle_hash,
        ticket.gate_id,
        ticket.round,
    )
    if ticket.ticket_id != expected:
        return "TICKET_ID_NOT_DERIVED_FROM_CONTENT"
    if ticket.status not in ("issued", "consumed", "invalidated"):
        return "TICKET_STATUS_UNKNOWN"
    # Consumption state has to be internally coherent for the same reason the
    # id does: a ticket claiming it was answered while naming no answer, or
    # naming an answer it never took, is refuted by its own contents before any
    # store is consulted.
    if ticket.status == "consumed":
        digest = ticket.consumed_report_digest
        if not isinstance(digest, str) or not digest.strip():
            return "TICKET_CONSUMED_WITHOUT_DIGEST"
        if ticket.ticket_seq < 1:
            return "TICKET_CONSUMED_AT_ISSUE_SEQ"
    elif ticket.consumed_report_digest is not None:
        return "TICKET_UNCONSUMED_WITH_DIGEST"
    if ticket.ticket_seq < 0:
        return "TICKET_SEQ_NEGATIVE"
    return None


def index_tickets(
    tickets: Sequence[VerificationTicket],
) -> tuple[dict, tuple[tuple[str, str], ...]]:
    """Index tickets by id, rejecting anything ambiguous or self-inconsistent.

    Returns ``(usable_by_id, rejected)``. Two tickets sharing an id are *both*
    dropped rather than resolved by order -- if the same round tuple has two
    conflicting tickets, no answer to it can be trusted, and picking one would
    make the outcome depend on list order (the exact defect the B-1 review
    found in duplicate report handling).
    """
    by_id: dict = {}
    duplicates: set = set()
    rejected: list = []

    for ticket in tickets:
        reason = ticket_self_consistency_reason(ticket)
        if reason is not None:
            rejected.append((ticket.ticket_id, reason))
            continue
        if ticket.ticket_id in by_id:
            duplicates.add(ticket.ticket_id)
            continue
        by_id[ticket.ticket_id] = ticket

    for ticket_id in sorted(duplicates):
        by_id.pop(ticket_id, None)
        rejected.append((ticket_id, "DUPLICATE_TICKET_ID"))

    return by_id, tuple(rejected)
