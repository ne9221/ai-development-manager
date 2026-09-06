"""Append-only ticket and report stores -- the runtime foundation.

This is the only module in the package that touches a filesystem, kept apart so
``evaluate()`` stays a pure function. Two properties matter more than the API:

**Create-only writes are the concurrency control.** Both stores open with
``O_CREAT | O_EXCL``, which is the same primitive ``manager/phase1_cursor.py``
already uses for two-phase publication in this repo. Re-issuing an identical
ticket is idempotent; issuing a *different* ticket under an id that already
exists raises. Because ``ticket_id`` is derived from the round tuple it binds,
that turns "two verifications of the same round" into a collision on one name
rather than two independent answers nobody can reconcile.

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
from pathlib import Path
from typing import Optional, Sequence

from .. import manager_home, production_guard
from .bundle import canonical_json, report_digest
from .models import VerificationReportFixture, VerificationTicket
from .tickets import ticket_self_consistency_reason


class VerificationStoreError(RuntimeError):
    """A store refused an operation. Always fail-closed, never a warning."""


class ProductionStoreRefused(VerificationStoreError):
    """The requested root is (or is inside) a production runtime location."""


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


class FileTicketStore:
    """Append-only ticket ledger under ``<root>/verification/tickets``."""

    def __init__(self, root) -> None:
        self.root = assert_non_production_root(root)
        self.directory = self.root / "verification" / "tickets"

    def _path(self, ticket_id: str) -> Path:
        if not ticket_id or "/" in ticket_id or "\\" in ticket_id or ticket_id.startswith("."):
            raise VerificationStoreError(f"UNSAFE_TICKET_ID: {ticket_id!r}")
        return self.directory / (ticket_id + ".json")

    def issue(self, ticket: VerificationTicket) -> VerificationTicket:
        """Record ``ticket``, or raise if a *different* one holds its id.

        Re-issuing the identical ticket succeeds: the controller retrying after
        a crash must not be punished for it. Issuing different content under
        the same id is the collision that matters, and it raises.
        """
        reason = ticket_self_consistency_reason(ticket)
        if reason is not None:
            raise VerificationStoreError(f"TICKET_REJECTED: {reason}")

        payload = canonical_json(ticket)
        path = self._path(ticket.ticket_id)
        if _write_create_only(path, payload):
            return ticket

        existing = path.read_text(encoding="utf-8")
        if existing != payload:
            raise VerificationStoreError(
                "TICKET_ID_CONFLICT: a different ticket already occupies "
                f"{ticket.ticket_id!r}; the same verification round cannot be issued twice "
                "with different content"
            )
        return ticket

    def get(self, ticket_id: str) -> Optional[dict]:
        path = self._path(ticket_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_ids(self) -> Sequence[str]:
        if not self.directory.exists():
            return ()
        return tuple(sorted(p.stem for p in self.directory.glob("*.json")))


class FileReportStore:
    """Content-addressed report store under ``<root>/verification/reports``.

    Keying by content digest means an unchanged resubmission is a no-op and two
    byte-different reports can never occupy one slot -- so "overwrite the
    report that failed with one that passes" is not an operation this store
    offers.
    """

    def __init__(self, root) -> None:
        self.root = assert_non_production_root(root)
        self.directory = self.root / "verification" / "reports"

    def put(self, report: VerificationReportFixture) -> str:
        digest = report_digest(report)
        payload = canonical_json(report)
        path = self.directory / (digest + ".json")
        if not _write_create_only(path, payload) and path.read_text(encoding="utf-8") != payload:
            # Only reachable on a sha256 collision, but silently trusting the
            # stored copy would make the digest a claim rather than a proof.
            raise VerificationStoreError(f"REPORT_DIGEST_COLLISION: {digest}")
        return digest

    def get(self, digest: str) -> Optional[dict]:
        path = self.directory / (digest + ".json")
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_digests(self) -> Sequence[str]:
        if not self.directory.exists():
            return ()
        return tuple(sorted(p.stem for p in self.directory.glob("*.json")))
