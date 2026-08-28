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

Stale-access-token recovery: this module never implements the OAuth
refresh protocol itself and never reads or uses the stored refresh
material directly -- the Claude CLI is the sole credential authority.
When the stored access token looks clearly expired, or the usage endpoint
itself rejects it with HTTP 401, this module runs one bounded,
non-interactive real completion (`claude -p "ok"`, the smallest possible
real prompt) against the exact account's own environment, then re-reads
the credentials file to see whether the CLI actually persisted a newer
access token as a side effect. Only when a genuinely different/fresher
token is now on disk does it retry the usage GET, and only once.

This is deliberately NOT `claude auth status --json` (an earlier version
of this module used that instead) -- confirmed live, on both real
production accounts, after their access tokens had been expired for 33
hours and 137 hours respectively: `claude auth status --json` reports
`loggedIn: true` and returns instantly, but never touches the on-disk
`accessToken`/`expiresAt` at all, even once. It appears to be a lightweight
local check (whatever session/login state it inspects is evidently
distinct from the specific OAuth access token this collector needs), never
an operation that exercises the CLI's real token-refresh path. Only an
ACTUAL API-invoking completion was confirmed, live, to make the CLI
perform its internal OAuth refresh (via the stored refresh_token, which
this module still never touches directly) and persist a new access token
to disk -- this is the real, necessary cost of a working refresh: one
minimal real completion, spent only when the token has already been
confirmed stale/rejected, not on every poll.

If this bounded real completion cannot be confirmed to have persisted a
fresh token -- whether the CLI invocation itself exits non-zero (times
out, errors, or reports the account is not actually logged in) or exits
cleanly yet leaves the on-disk token/expiry completely unchanged -- this
fails closed (AuthRefreshNotPersistedError for the two "ran, nothing new"
cases, CollectorError for the invocation itself never completing) rather
than looping, guessing, or reusing a stale token.

Security contract: neither the access token nor the on-disk credential
material this module reads is ever placed in a return value, log line, or
exception message this module raises. The CLI preflight is only ever
invoked with the account's own config_dir (via CLAUDE_CONFIG_DIR when
config_dir is set, or the ambient environment untouched for the default
account) -- one account's preflight can never read another account's
credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
# A real API round trip (unlike the old `claude auth status --json` check)
# needs meaningfully more time than a local-only status read; bounded, but
# generous enough that ordinary network/API latency doesn't spuriously
# fail-close a genuinely working refresh.
AUTH_PREFLIGHT_TIMEOUT_SECONDS = 30.0
# Deliberately the smallest real completion that still exercises the CLI's
# actual API-authenticated path -- this is spent (a small amount of real
# usage quota) only when the access token has already been confirmed
# stale/rejected, never on every ordinary poll.
CLI_REFRESH_PROMPT = "ok"


class CollectorError(RuntimeError):
    """Base error for this module. Messages here are always static/free of
    credential material -- callers may log str(exc) safely."""


class CredentialsUnavailableError(CollectorError):
    """Credentials file missing, unreadable, or missing the access token."""


class AuthStaleError(CollectorError):
    """The provider rejected the access token (HTTP 401). Callers must
    fail closed: never guess quota, never treat this as a 0%/100% value."""


class AuthRefreshNotPersistedError(CollectorError):
    """The bounded CLI refresh completion ran, but re-reading the
    credentials file afterward shows no new/fresher access token was
    actually persisted (either the completion itself exited non-zero, or
    it exited cleanly yet left the on-disk token/expiry unchanged). ADM
    never calls the Anthropic OAuth refresh endpoint itself and never
    treats the stored refresh material as usable credential authority on
    its own -- when the CLI (the sole refresh authority) doesn't produce a
    fresh token, this fails closed instead of retrying in a loop or
    reusing the stale token."""

    def __init__(self):
        super().__init__("AUTH_REFRESH_NOT_PERSISTED")


class RateLimitedError(CollectorError):
    """HTTP 429 from the usage endpoint. Callers must not overwrite any
    existing last-good quota entry on this outcome, and must not retry in
    a loop -- one refresh cycle gets one request per account, full stop.
    A 429 must never trigger the CLI auth preflight either: rate limiting
    is not an authentication problem."""

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


def _credentials_path(config_dir: Optional[str]) -> Path:
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / ".credentials.json"


def _read_oauth_section(config_dir: Optional[str]) -> dict:
    """Reads and returns the raw `claudeAiOauth` object for one account's
    config_dir (or the default ~/.claude when config_dir is falsy).
    Internal helper only -- callers outside this module must never log or
    return this dict verbatim; extract only the specific safe field(s)
    actually needed (see read_access_token)."""
    cred_path = _credentials_path(config_dir)
    if not cred_path.is_file():
        raise CredentialsUnavailableError("credentials file not found")
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsUnavailableError("credentials file unreadable") from exc
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise CredentialsUnavailableError("credentials file missing oauth section")
    return oauth


def read_access_token(config_dir: Optional[str]) -> str:
    """Reads the OAuth access token for one account's config_dir (or the
    default ~/.claude when config_dir is falsy). Returns the bare token
    string only -- never the parsed credentials dict, so a caller cannot
    accidentally log the whole structure."""
    oauth = _read_oauth_section(config_dir)
    token = oauth.get("accessToken")
    if not token or not isinstance(token, str):
        raise CredentialsUnavailableError("access token missing from credentials")
    return token


def _is_clearly_expired(oauth: dict, now_ms: Optional[int] = None) -> bool:
    """True only when the credentials file carries an explicit, well-formed
    `expiresAt` (epoch milliseconds) that is already in the past. Anything
    ambiguous (missing/non-numeric) returns False -- staleness in that case
    is still caught by the usage endpoint's own 401, never guessed here."""
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return False
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return expires_at <= now_ms


def _cli_executable(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin:
        return env_bin
    return "claude.exe" if os.name == "nt" else "claude"


def _cli_env(config_dir: Optional[str]) -> Optional[dict]:
    """None (inherit the ambient environment untouched) for the default
    account; an explicit CLAUDE_CONFIG_DIR override for any other account.
    This is the only mechanism that keeps one account's CLI preflight from
    ever reading another account's credentials."""
    if not config_dir:
        return None
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def run_auth_refresh(config_dir: Optional[str], timeout: float = AUTH_PREFLIGHT_TIMEOUT_SECONDS,
                     run: Callable[..., Any] = subprocess.run,
                     executable: Optional[str] = None) -> bool:
    """Runs one bounded, non-interactive real completion (`claude -p
    "ok"`) against the exact account environment, and returns whether that
    invocation itself completed successfully (exit code 0). See this
    module's own docstring for why this real completion -- not `claude
    auth status --json` -- is what actually exercises the CLI's
    token-refresh path; confirmed live on real production accounts.

    This never performs the OAuth refresh itself -- it only asks the CLI
    to complete one minimal real prompt; whatever internal refresh logic
    the CLI runs as a side effect of that real, authenticated API call is
    entirely up to the CLI. Raises CollectorError only when the invocation
    itself could not be judged at all (timeout, OS error) so an uncertain
    outcome is never mistaken for a clear answer -- a clean non-zero exit
    (the CLI ran and reported it could not complete the prompt, e.g.
    genuinely logged out) is a normal, unambiguous `False`, not an
    exception. Never logs or returns the real completion's own output
    text -- only this one boolean."""
    exe = _cli_executable(executable)
    env = _cli_env(config_dir)
    try:
        completed = run([exe, "-p", CLI_REFRESH_PROMPT], capture_output=True,
                         text=True, timeout=timeout, env=env, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise CollectorError("claude cli refresh timed out") from None
    except OSError as exc:
        raise CollectorError("claude cli refresh failed to start") from None
    return completed.returncode == 0


def _refresh_access_token_via_cli(config_dir: Optional[str], pre_oauth: dict, timeout: float,
                                   run: Callable[..., Any], executable: Optional[str]) -> str:
    """Runs exactly one CLI refresh completion, then re-reads the
    credentials file and returns the new access token only if it genuinely
    changed (token value or expiry) from what was on disk before the
    completion. Otherwise raises AuthRefreshNotPersistedError -- never
    falls back to the stale token, never loops."""
    succeeded = run_auth_refresh(config_dir, timeout=timeout, run=run, executable=executable)
    if not succeeded:
        raise AuthRefreshNotPersistedError()

    post_oauth = _read_oauth_section(config_dir)
    pre_token = pre_oauth.get("accessToken")
    post_token = post_oauth.get("accessToken")
    if not post_token or not isinstance(post_token, str):
        raise AuthRefreshNotPersistedError()
    if post_token == pre_token and post_oauth.get("expiresAt") == pre_oauth.get("expiresAt"):
        raise AuthRefreshNotPersistedError()
    return post_token


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
            opener: Optional[Callable[..., Any]] = None,
            cli_run: Optional[Callable[..., Any]] = None,
            cli_executable: Optional[str] = None) -> dict:
    """Orchestrates one credentials read + at most one HTTP request (plus,
    only when needed, exactly one CLI auth preflight and exactly one retry
    request) for one account, returning a normalized provider entry.

    Recovery path for a stale access token:
      - If the on-disk token is clearly expired (expiresAt already past),
        this runs the CLI preflight *before* ever calling the usage
        endpoint, then makes exactly one usage request with whatever token
        is on disk afterward.
      - Otherwise, this calls the usage endpoint first; only if that
        specific call returns 401 does it run the CLI preflight and retry
        the usage request exactly once.
    In both cases the CLI preflight runs at most once per call, and the
    usage endpoint is ever called at most twice per call (initial attempt +
    at most one retry) -- never a loop. A 429 from the usage endpoint is
    never treated as an auth problem and never triggers the preflight.

    Raises CredentialsUnavailableError / AuthStaleError / RateLimitedError /
    AuthRefreshNotPersistedError / CollectorError on failure -- callers
    (manager.refresh_status) decide what to do about each, including
    whether to fall back to the statusline-based compatibility path."""
    run = cli_run or subprocess.run
    oauth = _read_oauth_section(config_dir)
    token = oauth.get("accessToken")
    if not token or not isinstance(token, str):
        raise CredentialsUnavailableError("access token missing from credentials")

    if _is_clearly_expired(oauth):
        token = _refresh_access_token_via_cli(config_dir, oauth, timeout, run, cli_executable)
        payload = fetch_usage(token, timeout=timeout, opener=opener)
        return normalize(payload, account_id)

    try:
        payload = fetch_usage(token, timeout=timeout, opener=opener)
    except AuthStaleError:
        token = _refresh_access_token_via_cli(config_dir, oauth, timeout, run, cli_executable)
        payload = fetch_usage(token, timeout=timeout, opener=opener)
    return normalize(payload, account_id)
