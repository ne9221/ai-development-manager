#!/usr/bin/env python3
"""Persisted per-credential OAuth rate-limit cooldown state.

Claude's OAuth usage endpoint (collectors.claude_oauth) issues HTTP 429
with an advisory Retry-After when a credential is rate limited.
manager.refresh_status.refresh() already refuses to retry within a single
call, but it runs on a 15-minute Scheduled Task -- without persistence, a
429 near the start of one cycle was forgotten the moment the process
exited, so the very next cycle 15 minutes later hit the endpoint again
well before most Retry-After windows expire.

This module makes that cooldown durable across process restarts by writing
it to a small JSON file under AI_MANAGER_HOME/runtime. It is keyed by
*credential identity* -- the same config_dir string (or "<default>")
manager.refresh_status already uses to dedupe OAuth calls within one
refresh() cycle -- never by account_id or token, so two account_ids that
resolve to the same underlying config_dir correctly share one cooldown.

Security contract: only a credential_key (a config_dir path string, or the
literal "<default>") and a retry_until timestamp are ever written. No
token, credential file content, or response body is stored or logged by
this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DEFAULT_CREDENTIAL_KEY = "<default>"

# Sentinel key for a *global* quarantine, distinct from any real
# credential_key (those are always a config_dir path string or
# DEFAULT_CREDENTIAL_KEY, never this literal). See CooldownStore.get()'s
# corrupt-file handling.
GLOBAL_QUARANTINE_KEY = "__global_corrupt_quarantine__"

# Bounds applied to every Retry-After value, valid or not: never trust a
# provider-supplied delay of zero (would defeat the cooldown) or of more
# than a day (a transient outage shouldn't lock a credential out for good).
MIN_COOLDOWN_SECONDS = 1
MAX_COOLDOWN_SECONDS = 24 * 60 * 60

# Used when Retry-After is missing or unparseable -- still backs off
# instead of retrying immediately, but only briefly.
FALLBACK_COOLDOWN_SECONDS = 60

# Fail-closed window applied when the state file itself can't be trusted
# (corrupt JSON, wrong shape, unreadable). Blocks requests for a bounded
# window rather than either spamming the endpoint (fail open) or locking
# the credential out forever.
CORRUPT_STATE_COOLDOWN_SECONDS = 60


def credential_key(config_dir) -> str:
    """The same identity manager.refresh_status uses to dedupe OAuth calls:
    the config_dir's string form, or DEFAULT_CREDENTIAL_KEY for a falsy
    config_dir (the legacy ~/.claude default)."""
    return str(config_dir) if config_dir else DEFAULT_CREDENTIAL_KEY


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> datetime:
    """Safely parses a Retry-After header value into an absolute
    retry_until datetime, bounded to [MIN_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS] from `now`. Accepts either delta-seconds ("120")
    or an HTTP-date. Missing, negative, non-numeric, or otherwise
    unparseable values fall back to FALLBACK_COOLDOWN_SECONDS."""
    now = now or now_utc()
    seconds = None
    if value is not None:
        text = value.strip()
        try:
            seconds = float(text)
        except (TypeError, ValueError):
            try:
                from email.utils import parsedate_to_datetime
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError, OverflowError):
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = (parsed.astimezone(timezone.utc) - now).total_seconds()
    if seconds is None:
        seconds = FALLBACK_COOLDOWN_SECONDS
    seconds = max(MIN_COOLDOWN_SECONDS, min(MAX_COOLDOWN_SECONDS, seconds))
    return now + timedelta(seconds=seconds)


class CooldownStore:
    """Atomic JSON-file-backed cooldown state:
        {"<default>": {"retry_until": "<iso8601>"}, "/path/to/config": {...}}

    A corrupt or malformed file is never trusted at face value. Its
    per-key contents could have included a still-active Retry-After for
    *any* credential, so a corrupt read can't be narrowed to "only the key
    someone happened to check" -- it fails closed for every credential at
    once, via one bounded GLOBAL_QUARANTINE_KEY entry (see get()), rather
    than by fabricating individual cooldowns for every known credential.
    Corruption cannot cause a permanent lock-out by being silently
    rediscovered by every future process: quarantining is a single write,
    and it self-expires. Every write (a 429, a successful clear, or a
    quarantine) replaces the whole file atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read_raw(self):
        """Returns (data, corrupt). data is always a dict. corrupt is True
        only when the file exists and its content could not be trusted
        as-is -- a missing or empty file is not corrupt, just "no state
        recorded yet".

        Validates the *complete* shape, not just "is it a dict": every
        top-level key must be a string, and every entry (both real
        credential entries and a GLOBAL_QUARANTINE_KEY entry, if present)
        must match the one supported shape -- {"retry_until": <ISO-8601
        string>} -- with a retry_until that actually parses. Valid JSON
        with a malformed entry (a non-object entry, a non-string
        retry_until, or an unparseable retry_until string) is exactly as
        untrustworthy as unparseable JSON: some entry's persisted cooldown
        truth can't be verified, so the whole file is treated as corrupt
        rather than letting the unverifiable entry silently read as "no
        cooldown"."""
        if not self.path.is_file():
            return {}, False
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}, True
        if not text.strip():
            return {}, False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}, True
        if not isinstance(data, dict):
            return {}, True
        for key, entry in data.items():
            if not isinstance(key, str) or not self._valid_entry(entry):
                return {}, True
        return data, False

    @staticmethod
    def _valid_entry(entry) -> bool:
        """The one supported entry shape: a dict with exactly one key,
        retry_until, whose value is a string that parses as ISO-8601. An
        entry that is expired is still valid -- expiry is a value judgment
        made later against `now`, not a shape defect."""
        if not isinstance(entry, dict) or set(entry.keys()) != {"retry_until"}:
            return False
        retry_until = entry.get("retry_until")
        return isinstance(retry_until, str) and _parse_iso(retry_until) is not None

    def get(self, key: str, now: Optional[datetime] = None) -> Optional[datetime]:
        """Returns the retry_until datetime for `key` if it is still in the
        future, else None (no recorded cooldown, an expired one, or an
        unknown key).

        On a corrupt file, fails closed for *every* credential, not just
        `key`: the whole corrupt content is discarded (never silently
        preserved -- none of it can be trusted, and any of it could have
        been a still-active Retry-After for some other credential) and
        replaced with a clean file containing exactly one entry, under
        GLOBAL_QUARANTINE_KEY, recording a fresh bounded
        `now + CORRUPT_STATE_COOLDOWN_SECONDS`. This call returns that same
        timestamp regardless of which `key` triggered it. A subsequent
        call for any key, in this process or a brand new one reading the
        same now-valid file, sees the still-active global entry and blocks
        too -- until it expires, at which point normal per-credential
        state (which the global quarantine never touches or fabricates)
        resumes on its own. This is what makes the fail-closed window
        self-heal instead of compounding into a permanent lock-out: it is
        one bounded write, not one rediscovery of corruption per process."""
        now = now or now_utc()
        data, corrupt = self._read_raw()
        if corrupt:
            retry_until = now + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS)
            try:
                self._write({GLOBAL_QUARANTINE_KEY: {"retry_until": _iso(retry_until)}})
            except OSError:
                pass
            return retry_until
        global_retry_until = self._entry_retry_until(data, GLOBAL_QUARANTINE_KEY, now)
        if global_retry_until is not None:
            return global_retry_until
        return self._entry_retry_until(data, key, now)

    @staticmethod
    def _entry_retry_until(data: dict, key: str, now: datetime) -> Optional[datetime]:
        entry = data.get(key)
        if not isinstance(entry, dict):
            return None
        retry_until = _parse_iso(entry.get("retry_until"))
        if retry_until is None or retry_until <= now:
            return None
        return retry_until

    def set_retry_until(self, key: str, retry_until: datetime) -> None:
        assert key != GLOBAL_QUARANTINE_KEY, "real credential keys must never collide with the quarantine sentinel"
        data, _ = self._read_raw()
        data[key] = {"retry_until": _iso(retry_until)}
        self._write(data)

    def clear(self, key: str) -> None:
        assert key != GLOBAL_QUARANTINE_KEY, "real credential keys must never collide with the quarantine sentinel"
        data, corrupt = self._read_raw()
        if corrupt or key not in data:
            return
        del data[key]
        self._write(data)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
