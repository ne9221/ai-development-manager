#!/usr/bin/env python3
"""Collect Claude quota from the official OAuth usage endpoint.

Unlike collectors.claude (which normalizes the statusline hook's JSON, and
only ever runs when Claude Code renders an interactive status bar),
this module actively fetches quota from
https://api.anthropic.com/api/oauth/usage using the account's own stored
OAuth access token -- it works regardless of whether Claude Code is running
interactively, which is what ADM's provider launches (`claude -p`,
non-interactive) actually need: real-world testing confirmed the
statusline hook never fires in `-p` mode, so `statusline-payload.json`
never refreshes under ADM's own launch pattern (see
manager.refresh_status for how this collector and the statusline path are
combined).

Security contract: the access token is read once per call, used only in
the Authorization header of a single outbound request, and never appears
in any return value, log line, or exception message this module raises.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"


class CollectorError(RuntimeError):
    """Base error for this module. Messages here are always static/free of
    credential material -- callers may log str(exc) safely."""


class CredentialsUnavailableError(CollectorError):
    """Credentials file missing, unreadable, or missing the access token."""


class AuthStaleError(CollectorError):
    """The provider rejected the access token (HTTP 401). Callers must
    fail closed: never guess quota, never treat this as a 0%/100% value."""


class RateLimitedError(CollectorError):
    """HTTP 429 from the usage endpoint. Callers must not overwrite any
    existing last-good quota entry on this outcome, and must not retry in
    a loop -- one refresh cycle gets one request per account, full stop."""

    def __init__(self, retry_after: Optional[str] = None):
        super().__init__("rate limited by oauth usage endpoint")
        self.retry_after = retry_after


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reset_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CollectorError("resets_at must be an ISO 8601 string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectorError("invalid resets_at timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_access_token(config_dir: Optional[str]) -> str:
    """Reads the OAuth access token for one account's config_dir (or the
    default ~/.claude when config_dir is falsy). Returns the bare token
    string only -- never the parsed credentials dict, so a caller cannot
    accidentally log the whole structure."""
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    cred_path = base / ".credentials.json"
    if not cred_path.is_file():
        raise CredentialsUnavailableError("credentials file not found")
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsUnavailableError("credentials file unreadable") from exc
    token = (data.get("claudeAiOauth") or {}).get("accessToken")
    if not token or not isinstance(token, str):
        raise CredentialsUnavailableError("access token missing from credentials")
    return token


def fetch_usage(token: str, timeout: float = 15, opener: Optional[Callable[..., Any]] = None) -> dict:
    """Single GET request to the official OAuth usage endpoint. Raises
    AuthStaleError on 401, RateLimitedError on 429, CollectorError on any
    other non-2xx status, network failure, or malformed JSON body."""
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": OAUTH_BETA_HEADER,
            "Accept": "application/json",
        },
        method="GET",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status == 401:
            raise AuthStaleError("access token rejected (401)") from None
        if status == 429:
            retry_after = None
            try:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            except Exception:
                retry_after = None
            raise RateLimitedError(retry_after) from None
        raise CollectorError(f"unexpected HTTP status {status}") from None
    except urllib.error.URLError as exc:
        raise CollectorError("network error contacting oauth usage endpoint") from None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise CollectorError("malformed JSON from oauth usage endpoint") from None
    if not isinstance(parsed, dict):
        raise CollectorError("oauth usage response must be a JSON object")
    return parsed


def _window(payload: dict, name: str, duration_minutes: int) -> Optional[dict]:
    window = payload.get(name)
    if not isinstance(window, dict):
        return None
    utilization = window.get("utilization")
    if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
        return None
    used = max(0.0, min(100.0, float(utilization)))
    return {
        "name": name,
        "duration_minutes": duration_minutes,
        "used_percent": used,
        "remaining_percent": round(100.0 - used, 6),
        "resets_at": _reset_iso(window.get("resets_at")),
    }


def normalize(payload: dict, account_id: Optional[str], captured_at: Optional[str] = None) -> dict:
    if not isinstance(payload, dict):
        raise CollectorError("oauth usage response must be a JSON object")

    windows = []
    missing = []
    for name, duration in (("five_hour", 300), ("seven_day", 10080)):
        window = _window(payload, name, duration)
        if window is None:
            missing.append(name)
        else:
            windows.append(window)

    captured_at = captured_at or now_iso()
    return {
        "provider": "claude",
        "account_id": account_id,
        "display_name": "Claude Code",
        "collection_mode": "automatic",
        "source": "claude_oauth_usage",
        "source_type": "official",
        "confidence": "official" if windows else "unknown",
        "last_updated": captured_at,
        "status": "ok" if windows else "unknown",
        "windows": windows,
        "metadata": {
            "official_rate_limits_available": bool(windows),
            "missing_windows": missing,
        },
    }


def collect(config_dir: Optional[str], account_id: Optional[str], timeout: float = 15,
            opener: Optional[Callable[..., Any]] = None) -> dict:
    """Orchestrates one credentials read + one HTTP request for one
    account, returning a normalized provider entry. Raises
    CredentialsUnavailableError / AuthStaleError / RateLimitedError /
    CollectorError on failure -- callers (manager.refresh_status) decide
    what to do about each, including whether to fall back to the
    statusline-based compatibility path."""
    token = read_access_token(config_dir)
    payload = fetch_usage(token, timeout=timeout, opener=opener)
    return normalize(payload, account_id)
