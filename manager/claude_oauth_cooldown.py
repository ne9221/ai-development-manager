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

    A corrupt or malformed file is never trusted at face value -- reads
    fail closed (see get()) -- and every write replaces the whole file
    atomically, which self-heals any prior corruption as soon as the next
    429 or successful clear happens for any key."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read_raw(self):
        """Returns (data, corrupt). data is always a dict. corrupt is True
        only when the file exists and its content could not be trusted
        as-is -- a missing or empty file is not corrupt, just "no state
        recorded yet"."""
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
        return data, False

    def get(self, key: str, now: Optional[datetime] = None) -> Optional[datetime]:
        """Returns the retry_until datetime for `key` if it is still in the
        future, else None (no recorded cooldown, an expired one, or an
        unknown key). On a corrupt file, fails closed: returns
        `now + CORRUPT_STATE_COOLDOWN_SECONDS` instead of either silently
        treating every credential as clear (would spam the endpoint on
        every affected key) or blocking forever."""
        now = now or now_utc()
        data, corrupt = self._read_raw()
        if corrupt:
            return now + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS)
        entry = data.get(key)
        if not isinstance(entry, dict):
            return None
        retry_until = _parse_iso(entry.get("retry_until"))
        if retry_until is None or retry_until <= now:
            return None
        return retry_until

    def set_retry_until(self, key: str, retry_until: datetime) -> None:
        data, _ = self._read_raw()
        data[key] = {"retry_until": _iso(retry_until)}
        self._write(data)

    def clear(self, key: str) -> None:
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
