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

import hashlib
from typing import Optional, Sequence

from .models import VerificationTicket

TICKET_ID_PREFIX = "vt-"


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
