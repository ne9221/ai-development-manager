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


class FileTicketStore:
    """Append-only ticket ledger under ``<root>/verification/tickets``.

    One directory per ``ticket_id``, holding one content-addressed record per
    state transition: the issue record, and -- once a report answers it -- the
    consumption record naming the digest that answered it (Phase A v3 predicate
    11). The directory name is the CAS key for the round; the file names are
    the content addresses of what the ticket said at each step.

    Splitting the two is what closes the review's second vector.
    ``ticket_id`` hashes only (execution, candidate, bundle, gate, round), so
    the identities, the task, the base SHA, the lease and the status all sit
    outside it and were previously free to edit. They are inside the record
    digest, which the reader recomputes.
    """

    def __init__(self, root) -> None:
        self.root = assert_non_production_root(root)
        self.directory = self.root / "verification" / "tickets"

    def _dir(self, ticket_id: str) -> Path:
        if not ticket_id or "/" in ticket_id or "\\" in ticket_id or ticket_id.startswith("."):
            raise VerificationStoreError(f"UNSAFE_TICKET_ID: {ticket_id!r}")
        return self.directory / ticket_id

    def _records(self, ticket_id: str) -> List[VerificationTicket]:
        directory = self._dir(ticket_id)
        if not directory.exists():
            return []
        records = []
        for path in sorted(directory.glob("*.json")):
            records.append(_verify_record(path, VerificationTicket, path.stem, "TICKET"))
        return records

    def _append(self, ticket: VerificationTicket) -> VerificationTicket:
        payload = canonical_json(ticket)
        path = self._dir(ticket.ticket_id) / (record_digest(ticket) + ".json")
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
        authorised must not conjure the authorisation it lacks, so the report
        is simply stored and rejected later as ``NO_MATCHING_TICKET``.

        Answering twice with the same digest is a no-op, which is what makes a
        retried submission idempotent even though the wall-clock time differs.
        Answering with a *different* digest appends a second, conflicting
        claim rather than overwriting the first -- the contest becomes durable
        evidence, and the fold below refuses the ticket to both of them.
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
        """The ticket's current state, folded from its verified records.

        A ledger missing its issue record, or holding two of them, or holding
        two conflicting consumptions, does not resolve to "no constraint" -- the
        first two raise, and the third yields an invalidated ticket that admits
        nothing. Partial state is the shape a deletion attack leaves behind.
        """
        records = self._records(ticket_id)
        if not records:
            return None

        issued = [record for record in records if record.ticket_seq == ISSUE_SEQ]
        consumed = [record for record in records if record.ticket_seq == CONSUME_SEQ]
        unknown = [record for record in records if record.ticket_seq not in (ISSUE_SEQ, CONSUME_SEQ)]

        if len(issued) != 1 or unknown:
            raise EvidenceIntegrityError(
                f"TICKET_LEDGER_INCOHERENT: {ticket_id!r} holds {len(issued)} issue records "
                f"and {len(unknown)} records at an unknown sequence; a ticket has exactly one"
            )
        if len(consumed) > 1:
            # Two reports claimed one ticket. Neither is trusted, and the
            # choice is not left to whichever was written first.
            return replace(issued[0], status="invalidated")
        return consumed[0] if consumed else issued[0]

    def list_ids(self) -> Sequence[str]:
        if not self.directory.exists():
            return ()
        return tuple(sorted(path.name for path in self.directory.iterdir() if path.is_dir()))


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
