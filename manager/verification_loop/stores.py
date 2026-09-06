"""Append-only ticket and report ledgers -- the runtime foundation.

This is the only module in the package that touches a filesystem, kept apart so
``evaluate()`` stays a pure function. Three properties matter more than the API:

**Every record is verified on read, not only addressed on write.** Phase B-2
computed a record's content address when it stored it and then trusted whatever
JSON came back. The independent review edited a stored FAIL report into a PASS
and turned REPAIR into ACCEPTED, with the filename still the digest of the FAIL
content, because nothing ever compared the two. Both ledgers now recompute the
digest of the record they just rehydrated and refuse it if it does not hash to
the name it is filed under. A store that verifies only on write is not
content-addressed; it is content-addressed *once*, which is a different and
much weaker claim.

That refusal is an exception, not an inadmissible record. A ledger whose
contents do not match their names is not evidence that happens to fail a check
-- it is a broken ledger, and continuing to derive an acceptance from the parts
that still verify would be answering a question nobody can trust the inputs to.

**Create-only writes are the concurrency control.** Both ledgers open with
``O_CREAT | O_EXCL``, the same primitive ``manager/phase1_cursor.py`` uses for
two-phase publication in this repo. Nothing is ever overwritten: a ticket's
consumption is a *new* record beside its issue record, not an edit of it, so a
second checker answering a ticket someone else already answered appends a
conflicting claim rather than replacing the first one. Two claims on one ticket
leave it invalidated for everyone, which is the only order-independent answer
-- letting the first writer win would mean submitting the PASS before the FAIL
decided the outcome.

**The production runtime home is refused, not detected-and-warned.** Phase B-2
builds runtime foundation but is explicitly not allowed to operate on
production. ``manager.production_guard.is_marked_production_path`` is reused
rather than reimplemented -- it walks ancestors, so passing a subdirectory of a
marked checkout does not evade it -- and the canonical user-level manager home
is refused as well, since the marker only exists on activated checkouts and a
fresh clone would otherwise slip through. A second copy of that logic here
would be a second source of truth about what "production" means, which is
exactly what Phase A v3 forbids.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Sequence

from .. import manager_home, production_guard
from .bundle import canonical_json, record_digest, report_digest
from .models import VerificationReportFixture, VerificationTicket, rehydrate
from .tickets import ticket_self_consistency_reason

# The two record kinds a ticket directory may hold, by ``ticket_seq``.
ISSUE_SEQ = 0
CONSUME_SEQ = 1


class VerificationStoreError(RuntimeError):
    """A store refused an operation. Always fail-closed, never a warning."""


class ProductionStoreRefused(VerificationStoreError):
    """The requested root is (or is inside) a production runtime location."""


class EvidenceIntegrityError(VerificationStoreError):
    """A persisted record does not hash to the name it is filed under.

    Deliberately not an admission reason. An admission reason says "this report
    does not qualify"; this says "the ledger is lying about what it holds", and
    the honest response to that is to stop, not to derive around it.
    """


def assert_non_production_root(root) -> Path:
    """Resolve ``root`` after proving it is not a production runtime location.

    Three independent refusals, ORed because they fail in different ways: the
    activation marker is absent on a checkout that was never activated, the
    canonical home is the default nothing had to opt into, and an explicit
    AI_MANAGER_HOME pointing at either is the case a test could create by
    accident. Any one of them is enough to refuse.
    """
    resolved = Path(root).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError as exc:  # pragma: no cover - platform dependent
        raise VerificationStoreError(f"STORE_ROOT_UNRESOLVABLE: {root!r}") from exc

    if production_guard.is_marked_production_path(resolved):
        raise ProductionStoreRefused(
            "PRODUCTION_STORE_REFUSED: refusing to open a verification store at "
            f"{str(resolved)!r} -- it is inside a marked production runtime checkout; "
            "use a temporary AI_MANAGER_HOME instead"
        )

    canonical = manager_home.canonical_manager_home()
    if canonical is not None:
        try:
            canonical_resolved = Path(canonical).expanduser().resolve()
        except OSError:  # pragma: no cover - platform dependent
            canonical_resolved = Path(canonical)
        if resolved == canonical_resolved or canonical_resolved in resolved.parents:
            raise ProductionStoreRefused(
                "PRODUCTION_STORE_REFUSED: refusing to open a verification store under the "
                f"canonical manager home {str(canonical_resolved)!r}; Phase B-2 may only use a "
                "temporary AI_MANAGER_HOME"
            )

    return resolved


def _write_create_only(path: Path, payload: str) -> bool:
    """Write ``payload`` at ``path`` only if nothing is there. True if written.

    Returns False rather than raising when the file already exists, so the
    caller can decide whether an existing entry is an idempotent re-submission
    or a genuine conflict -- a distinction the filesystem cannot make.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
    except BaseException:
        # A half-written record is worse than no record: it would be read back
        # as authoritative evidence. Remove it and let the caller retry.
        path.unlink(missing_ok=True)
        raise
    return True


def _read_exact(path: Path) -> str:
    """The file's characters with no newline translation.

    ``read_text`` would rewrite CRLF into LF before the byte comparison below
    ever ran, which is the difference between checking the bytes on disk and
    checking a normalised copy of them.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _verify_record(path: Path, cls, expected_digest: str, kind: str):
    """Rehydrate the record at ``path``, proving it hashes to its own name.

    Four refusals in a fixed order, so the reported reason is the most
    structural one available: unparseable, unrehydratable, wrong digest, and
    finally not stored in canonical form. The last one exists because a digest
    alone would let two byte sequences share one name, and a store cannot then
    say which of them it hashed.
    """
    raw = _read_exact(path)
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise EvidenceIntegrityError(
            f"{kind}_MALFORMED_JSON: {path.name} is not readable as a record"
        ) from exc
    try:
        record = rehydrate(cls, document)
    except (TypeError, ValueError, KeyError) as exc:
        raise EvidenceIntegrityError(
            f"{kind}_UNREHYDRATABLE: {path.name} does not describe a {cls.__name__}"
        ) from exc

    actual = record_digest(record)
    if actual != expected_digest:
        raise EvidenceIntegrityError(
            f"{kind}_CONTENT_DIGEST_MISMATCH: {path.name} holds content that hashes to "
            f"{actual!r}, not to the {expected_digest!r} it is filed under -- the record "
            "was edited after it was written"
        )

    canonical = canonical_json(record)
    if canonical != raw:
        raise EvidenceIntegrityError(
            f"{kind}_NOT_CANONICAL_BYTES: {path.name} hashes correctly but is not stored "
            "in canonical form, so the bytes on disk are not the bytes that were hashed"
        )
    return record


def validate_ticket_id(ticket_id: str) -> str:
    """Refuse a path-shaped ticket id.

    Ticket ids are not path components in this layout -- records are named by
    their own content digest -- so this is defence in depth rather than the one
    thing standing between an id and the filesystem. It is kept because a
    future layout change could make it load-bearing again, and because an id
    shaped like a traversal is evidence of something regardless.
    """
    if (
        not ticket_id
        or "/" in ticket_id
        or "\\" in ticket_id
        or ticket_id.startswith(".")
    ):
        raise VerificationStoreError(f"UNSAFE_TICKET_ID: {ticket_id!r}")
    return ticket_id


class FileTicketStore:
    """Append-only ticket ledger under ``<root>/verification/tickets``.

    Every record -- the issue record, and the consumption record naming the
    report digest that answered it (Phase A v3 predicate 11) -- is one file
    named by its own content digest, exactly like a report. A ticket is the set
    of records carrying its ``ticket_id``, which lives inside the records
    rather than in a path component.

    That is what closes the review's second vector. ``ticket_id`` hashes only
    (execution, candidate, bundle, gate, round), so the identities, the task,
    the base SHA, the lease and the status all sit outside it and were
    previously free to edit. They are inside the record digest, which is the
    file name, which the reader recomputes.

    Filing records flat rather than in a directory per ticket is not merely
    tidiness. A nested layout adds the full 67-character ticket id to every
    record path, and this repository is checked out and tested at path lengths
    where that crosses the Windows limit -- the same path-length sensitivity
    the installer tests already have. A ledger that fails to write on a deep
    checkout is a ledger with a silent availability cliff.
    """

    def __init__(self, root) -> None:
        self.root = assert_non_production_root(root)
        self.directory = self.root / "verification" / "tickets"

    def _all_records(self) -> List[VerificationTicket]:
        if not self.directory.exists():
            return []
        return [
            _verify_record(path, VerificationTicket, path.stem, "TICKET")
            for path in sorted(self.directory.glob("*.json"))
        ]

    def _records(self, ticket_id: str) -> List[VerificationTicket]:
        validate_ticket_id(ticket_id)
        return [record for record in self._all_records() if record.ticket_id == ticket_id]

    def _append(self, ticket: VerificationTicket) -> VerificationTicket:
        payload = canonical_json(ticket)
        path = self.directory / (record_digest(ticket) + ".json")
        if not _write_create_only(path, payload) and _read_exact(path) != payload:
            # Only reachable on a sha256 collision, but silently trusting the
            # stored copy would make the digest a claim rather than a proof.
            raise EvidenceIntegrityError(f"TICKET_DIGEST_COLLISION: {path.name}")
        return ticket

    def issue(self, ticket: VerificationTicket) -> VerificationTicket:
        """Record ``ticket``, or raise if a *different* one holds its id.

        Re-issuing the identical ticket succeeds: the controller retrying after
        a crash must not be punished for it. Issuing different content under
        the same id is the collision that matters, and it raises.
        """
        reason = ticket_self_consistency_reason(ticket)
        if reason is not None:
            raise VerificationStoreError(f"TICKET_REJECTED: {reason}")
        validate_ticket_id(ticket.ticket_id)
        if ticket.status != "issued" or ticket.ticket_seq != ISSUE_SEQ:
            raise VerificationStoreError(
                "TICKET_NOT_ISSUABLE: a ticket enters the ledger as issued; a consumed "
                "ticket is a record appended later, never one handed in"
            )

        for existing in self._records(ticket.ticket_id):
            if existing.ticket_seq == ISSUE_SEQ and existing != ticket:
                raise VerificationStoreError(
                    "TICKET_ID_CONFLICT: a different ticket already occupies "
                    f"{ticket.ticket_id!r}; the same verification round cannot be issued "
                    "twice with different content"
                )
        return self._append(ticket)

    def consume(
        self, ticket_id: str, consumed_report_digest: str, consumed_at: str
    ) -> Optional[VerificationTicket]:
        """Append the record that this ticket was answered by that report.

        Returns None when no ticket holds ``ticket_id``: a report nobody
        authorised must not conjure the authorisation it lacks, so it is simply
        stored and rejected later as ``NO_MATCHING_TICKET``.

        Answering twice with the same digest is a no-op, which is what makes a
        retried submission idempotent even though the wall-clock time differs.
        Answering with a *different* digest appends a second, conflicting claim
        rather than overwriting the first -- the contest becomes durable
        evidence, and the fold below then refuses the ticket to both claimants.
        """
        issued = None
        consumptions = []
        for record in self._records(ticket_id):
            if record.ticket_seq == ISSUE_SEQ:
                issued = record
            else:
                consumptions.append(record)
        if issued is None:
            return None
        for record in consumptions:
            if record.consumed_report_digest == consumed_report_digest:
                return record

        return self._append(
            replace(
                issued,
                status="consumed",
                consumed_report_digest=consumed_report_digest,
                consumed_at=consumed_at,
                ticket_seq=CONSUME_SEQ,
            )
        )

    def get(self, ticket_id: str) -> Optional[VerificationTicket]:
        """The ticket's current state, folded from its verified records."""
        records = self._records(ticket_id)
        if not records:
            return None
        return _fold_ticket(ticket_id, records)

    def list_ids(self) -> Sequence[str]:
        return tuple(sorted({record.ticket_id for record in self._all_records()}))


def _fold_ticket(ticket_id: str, records: Sequence[VerificationTicket]) -> VerificationTicket:
    """One ticket's current state, from every record that claims its id.

    A ledger missing its issue record, or holding two of them, does not resolve
    to "no constraint" -- it raises, because partial state is the shape a
    deletion attack leaves behind. Two conflicting consumptions leave the
    ticket invalidated for both claimants, which is the only order-independent
    answer available.
    """
    issued = [record for record in records if record.ticket_seq == ISSUE_SEQ]
    consumed = [record for record in records if record.ticket_seq == CONSUME_SEQ]
    unknown = [
        record for record in records if record.ticket_seq not in (ISSUE_SEQ, CONSUME_SEQ)
    ]

    if len(issued) != 1 or unknown:
        raise EvidenceIntegrityError(
            f"TICKET_LEDGER_INCOHERENT: {ticket_id!r} holds {len(issued)} issue records "
            f"and {len(unknown)} records at an unknown sequence; a ticket has exactly one"
        )
    if len(consumed) > 1:
        return replace(issued[0], status="invalidated")
    return consumed[0] if consumed else issued[0]


class FileReportStore:
    """Content-addressed report store under ``<root>/verification/reports``.

    Keying by content digest means an unchanged resubmission is a no-op and two
    byte-different reports can never occupy one slot. The key is verified again
    on every read, so "overwrite the report that failed with one that passes"
    is not an operation this store offers *and* not one the filesystem can
    offer behind its back.
    """

    def __init__(self, root) -> None:
        self.root = assert_non_production_root(root)
        self.directory = self.root / "verification" / "reports"

    def _path(self, digest: str) -> Path:
        if not digest or "/" in digest or "\\" in digest or digest.startswith("."):
            raise VerificationStoreError(f"UNSAFE_REPORT_DIGEST: {digest!r}")
        return self.directory / (digest + ".json")

    def put(self, report: VerificationReportFixture) -> str:
        digest = report_digest(report)
        payload = canonical_json(report)
        path = self._path(digest)
        if not _write_create_only(path, payload) and _read_exact(path) != payload:
            raise EvidenceIntegrityError(f"REPORT_DIGEST_COLLISION: {digest}")
        return digest

    def get(self, digest: str) -> Optional[VerificationReportFixture]:
        path = self._path(digest)
        if not path.exists():
            return None
        return _verify_record(path, VerificationReportFixture, digest, "REPORT")

    def list_digests(self) -> Sequence[str]:
        if not self.directory.exists():
            return ()
        return tuple(sorted(p.stem for p in self.directory.glob("*.json")))
