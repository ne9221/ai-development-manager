"""Fail-closed selection among multiple Claude accounts.

Scope note (P0.1 vertical slice): this module is a standalone, pure
selection primitive with its own tests. It is deliberately NOT wired into
manager/dispatcher.py's live dispatch() path in this round -- dispatch()
today has no multi-account concept at all (provider="claude" implies exactly
one implicit account), and wiring real account-registry lookups through it
is a larger, separate change. What this module guarantees today: given a
set of Claude account quota entries, it either returns exactly one
unambiguous account_id or raises -- it never guesses, never averages
confidence, and never treats a stale/unknown entry as usable evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone


class AccountSelectionError(RuntimeError):
    """Raised instead of guessing when no single Claude account can be
    confidently selected. Callers must fail closed on this, not fall back to
    picking the first/any account."""


_UNRELIABLE_CONFIDENCE = (None, "unknown")


def _is_stale(entry, now, max_age_seconds):
    if max_age_seconds is None:
        return False
    last_updated = entry.get("last_updated")
    if not isinstance(last_updated, str) or not last_updated:
        return True
    try:
        parsed = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() > max_age_seconds


def select_claude_account(accounts, *, explicit_account_id=None, max_age_seconds=None, now=None):
    """Select exactly one Claude account_id from a list of account quota
    entries, or raise AccountSelectionError.

    accounts: iterable of dicts with at least {"account_id", "confidence"};
    optionally "enabled" (default True) and "last_updated".

    - explicit_account_id, if given, is honored as long as that account is
      present and enabled -- this is the escape hatch for a caller that
      already knows which account to use; it is not validated against
      quota confidence, since an explicit human/caller choice overrides
      automatic reliability heuristics by design.
    - Otherwise: accounts with confidence in (None, "unknown"), or older
      than max_age_seconds when that is given, are excluded as unreliable.
      Exactly one remaining candidate is required; zero or more than one
      both fail closed rather than guessing.
    """
    now = now or datetime.now(timezone.utc)
    enabled = [dict(a) for a in accounts if a.get("enabled", True)]

    if explicit_account_id is not None:
        match = next((a for a in enabled if a.get("account_id") == explicit_account_id), None)
        if match is None:
            raise AccountSelectionError(
                f"explicit account_id {explicit_account_id!r} is not an enabled Claude account"
            )
        return match["account_id"]

    if not enabled:
        raise AccountSelectionError("no enabled Claude accounts available to select from")

    reliable = [
        a for a in enabled
        if a.get("confidence") not in _UNRELIABLE_CONFIDENCE and not _is_stale(a, now, max_age_seconds)
    ]
    if not reliable:
        raise AccountSelectionError(
            "every Claude account has unknown, missing, or stale quota confidence; "
            "refusing to guess -- pass an explicit account_id instead"
        )
    if len(reliable) > 1:
        candidates = ", ".join(sorted(a["account_id"] for a in reliable))
        raise AccountSelectionError(
            f"multiple Claude accounts have reliable quota data ({candidates}); "
            "pass an explicit account_id instead of relying on automatic selection"
        )
    return reliable[0]["account_id"]
