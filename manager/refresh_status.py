#!/usr/bin/env python3
"""Single-writer refresh worker that updates runtime and Drive status."""

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from collectors.claude import normalize as normalize_claude
from collectors.claude_oauth import (
    CollectorError as ClaudeOauthError,
    RateLimitedError as ClaudeOauthRateLimited,
    collect as collect_claude_oauth,
)
from collectors.codex import collect as collect_codex
from collectors.publish_drive import build_service, sync_drive
from manager.quota_reader import read_drive_status, validate_status


SCHEMA = Path(__file__).parents[1] / "schema" / "status.schema.json"


class RefreshError(RuntimeError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log_line(path, message):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{now_iso()} {message}\n")


@contextmanager
def runtime_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RefreshError("another refresh is already running") from exc
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def replace_provider(document, provider):
    """Replace the entry matching (provider, account_id), not provider alone,
    so publishing one Claude account's snapshot never wipes another
    account's entry. account_id=None (the single/legacy-account case,
    including every non-Claude provider such as codex) behaves exactly as
    before this field existed."""
    provider_id = provider["provider"]
    account_id = provider.get("account_id")
    document["providers"] = [
        item for item in document["providers"]
        if not (item.get("provider") == provider_id and item.get("account_id") == account_id)
    ]
    document["providers"].append(provider)


def claude_snapshot(path, account_id=None):
    if not path.is_file():
        return None
    captured = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    provider = normalize_claude(json.loads(path.read_text(encoding="utf-8-sig")), captured_at=captured)
    provider["account_id"] = account_id
    return provider


def refresh_diagnostic(outcome, error, attempted_at):
    """The per-entry `metadata.refresh` record (schema: metadata is freeform)
    that explains WHY a Claude entry did not refresh this cycle. Live
    20260902: account-a sat STALE for 12+ hours while refresh.log only said
    `claude oauth unavailable: CredentialsUnavailableError` -- the actual
    cause (an empty accessToken in the default ~/.claude credentials file,
    i.e. the account needs a re-login) was invisible both in the log and on
    the Dashboard. Error text here is the collector's own fixed message
    (never a token, never response bodies)."""
    return {"outcome": outcome, "error": error, "attempted_at": attempted_at}


def claude_oauth_snapshot(config_dir, account_id=None, timeout=15, collector=collect_claude_oauth, log_path=None,
                          diagnostics=None):
    """Attempts one real OAuth usage fetch for one account. Returns
    (outcome, provider_or_None):
      - ("success", provider) on a real 200 response with at least one window
      - ("rate_limited", None) on HTTP 429 -- caller must not touch any
        existing last-good entry for this account at all
      - ("unavailable", None) on missing/unreadable credentials, HTTP 401,
        malformed JSON, or any other collector failure -- caller may still
        try the statusline compatibility fallback
    Never logs or returns the access token. The exception's own message IS
    logged (and stored into `diagnostics["error"]` when a dict is given):
    every collectors.claude_oauth error message is a fixed string
    ("access token missing from credentials", "access token rejected
    (401)", ...) -- never a token, never a response body -- and without it
    the log could not distinguish "needs re-login" from "file missing"."""
    try:
        provider = collector(config_dir, account_id, timeout=timeout)
        return "success", provider
    except ClaudeOauthRateLimited as exc:
        retry_note = f" retry_after={exc.retry_after}" if exc.retry_after else ""
        if log_path is not None:
            log_line(log_path, f"claude oauth rate_limited{retry_note}")
        if diagnostics is not None:
            diagnostics["error"] = (f"{type(exc).__name__}: retry_after={exc.retry_after}" if exc.retry_after
                                    else f"{type(exc).__name__}: rate limited")
        return "rate_limited", None
    except ClaudeOauthError as exc:
        reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        if log_path is not None:
            log_line(log_path, f"claude oauth unavailable: {reason}")
        if diagnostics is not None:
            diagnostics["error"] = reason
        return "unavailable", None


def write_atomic(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def refresh(*, service, runtime_path, log_path, lock_path, claude_path, claude_accounts=None,
            claude_config_dirs=None, claude_oauth_collector=collect_claude_oauth, claude_oauth_timeout=15,
            reader=read_drive_status, codex_collector=collect_codex,
            publisher=sync_drive, validator=validate_status, history_store=None):
    """`claude_path` remains the single/legacy-account payload path (account_id=None),
    unchanged from before. `claude_accounts`, if given, is an additional
    {account_id: path} mapping for extra Claude accounts; each is captured,
    logged, and published independently under its own (provider="claude",
    account_id=...) key -- one account's missing/stale payload never
    overwrites or blocks another's, and omitting claude_accounts entirely
    reproduces today's single-account behavior byte-for-byte.

    `claude_config_dirs`, if given, is an additional {account_id: config_dir}
    mapping used to attempt one real OAuth usage fetch per account BEFORE
    falling back to that account's statusline payload path. Omitting it
    entirely (the default) skips OAuth for every account and reproduces the
    pre-OAuth statusline-only behavior byte-for-byte -- this keeps every
    existing caller/test that doesn't pass this parameter unaffected,
    including Codex, which this parameter has no effect on at all."""
    from manager.production_guard import require_runtime_guard
    require_runtime_guard(manager_home=Path(runtime_path).parents[1])
    with runtime_lock(lock_path):
        log_line(log_path, "refresh start")
        try:
            document = reader(service=service)
        except Exception as exc:
            log_line(log_path, f"Drive read failure: {type(exc).__name__}")
            raise RefreshError("could not read Drive runtime SSOT") from exc

        # Initialize history store if not explicitly disabled (history_store=False)
        resolved_history_store = None
        if history_store is not False:
            if history_store is not None:
                resolved_history_store = history_store
            else:
                try:
                    from manager.quota_history import QuotaHistoryStore
                    resolved_history_store = QuotaHistoryStore(runtime_path.parent / "quota_history.json")
                except Exception as exc:
                    log_line(log_path, f"history store init warning: {type(exc).__name__}")

        outcomes = {}
        try:
            _, codex_document = codex_collector(timeout=20)
            codex = next(item for item in codex_document["providers"] if item.get("provider") == "codex")
            replace_provider(document, codex)
            outcomes["codex"] = "success"
            log_line(log_path, "provider codex success")
            if resolved_history_store is not None and codex.get("windows"):
                try:
                    resolved_history_store.append_snapshot(codex)
                    log_line(log_path, "quota history codex recorded")
                except Exception as exc:
                    log_line(log_path, f"quota history codex warning: {type(exc).__name__}")
        except Exception as exc:
            outcomes["codex"] = "unavailable"
            log_line(log_path, f"provider codex unavailable: {type(exc).__name__}")

        accounts = {None: claude_path, **(claude_accounts or {})}
        # Deliberately NOT auto-injecting a {None: None} default here: unlike
        # `accounts`, which must always have a legacy slot, OAuth must stay
        # fully opt-in per account_id so every existing caller/test that
        # doesn't pass claude_config_dirs reproduces pre-OAuth behavior
        # byte-for-byte, including for the legacy account_id=None slot.
        config_dirs = dict(claude_config_dirs or {})
        # Two account_ids can legitimately point at the same real Claude
        # account (e.g. the legacy account_id=None slot and an explicit
        # "account-a" entry both defaulting to ~/.claude) -- this cache
        # ensures each *distinct underlying credential* gets at most one
        # OAuth request per refresh() call, satisfying the "max 1
        # request/account/refresh" contract even when two schema-level
        # account_ids alias the same real account.
        oauth_cache = {}
        for account_id, path in accounts.items():
            outcome_key = "claude" if account_id is None else f"claude:{account_id}"
            try:
                claude = None
                diagnostics = {}
                if account_id in config_dirs:
                    cache_key = str(config_dirs[account_id]) if config_dirs[account_id] else "<default>"
                    if cache_key in oauth_cache:
                        oauth_outcome, oauth_provider, diagnostics = oauth_cache[cache_key]
                    else:
                        oauth_outcome, oauth_provider = claude_oauth_snapshot(
                            config_dirs[account_id], account_id=account_id,
                            timeout=claude_oauth_timeout, collector=claude_oauth_collector,
                            log_path=log_path, diagnostics=diagnostics,
                        )
                        oauth_cache[cache_key] = (oauth_outcome, oauth_provider, diagnostics)
                    if oauth_provider is not None:
                        oauth_provider = dict(oauth_provider, account_id=account_id)
                    if oauth_outcome == "rate_limited":
                        # Explicit contract: a 429 must never overwrite or be
                        # papered over by any other source (including the
                        # statusline fallback) this cycle -- last-good stays
                        # exactly as it was, and freshness is judged purely
                        # from that last-good entry's own captured_at.
                        outcomes[outcome_key] = "rate_limited"
                        # Windows and last_updated stay exactly as they are;
                        # only the WHY is recorded (bounded review finding:
                        # the 429 path used to continue before the
                        # metadata.refresh write, so the card could never
                        # explain a rate-limited account).
                        rate_limited_entry = next(
                            (item for item in document["providers"]
                             if item.get("provider") == "claude" and item.get("account_id") == account_id),
                            None,
                        )
                        if rate_limited_entry is not None:
                            if not isinstance(rate_limited_entry.get("metadata"), dict):
                                rate_limited_entry["metadata"] = {}
                            rate_limited_entry["metadata"]["refresh"] = refresh_diagnostic(
                                "rate_limited", diagnostics.get("error"), now_iso())
                        log_line(log_path, f"provider {outcome_key} rate_limited" + (f" ({diagnostics.get('error')})" if diagnostics.get("error") else ""))
                        continue
                    if oauth_outcome == "success":
                        claude = oauth_provider

                if claude is None:
                    # OAuth unavailable (no config_dirs entry, missing/stale
                    # credentials, 401, network/parse failure) -- fall back to
                    # the statusline payload as compatibility/last-good
                    # evidence only. Its captured_at is the file's real mtime,
                    # never "now", so a stale fallback still reports itself as
                    # stale rather than pretending to be fresh.
                    claude = claude_snapshot(path, account_id=account_id)

                existing = next(
                    (item for item in document["providers"]
                     if item.get("provider") == "claude" and item.get("account_id") == account_id),
                    None,
                )
                if claude and claude["windows"]:
                    if not existing or claude["last_updated"] > existing.get("last_updated", ""):
                        replace_provider(document, claude)
                        outcomes[outcome_key] = "success"
                        if resolved_history_store is not None:
                            try:
                                resolved_history_store.append_snapshot(claude)
                                log_line(log_path, f"quota history {outcome_key} recorded")
                            except Exception as exc:
                                log_line(log_path, f"quota history {outcome_key} warning: {type(exc).__name__}")
                    else:
                        outcomes[outcome_key] = "unchanged"
                else:
                    outcomes[outcome_key] = "unavailable"
                reason = diagnostics.get("error")
                if outcomes[outcome_key] != "success" and existing is not None:
                    # Keep the last-good numbers exactly as they are (they
                    # still carry their own real last_updated, so freshness
                    # is judged truthfully) but record WHY nothing newer
                    # landed, so the Dashboard can say "needs re-login"
                    # instead of a bare STALE.
                    existing.setdefault("metadata", {})
                    if not isinstance(existing["metadata"], dict):
                        existing["metadata"] = {}
                    existing["metadata"]["refresh"] = refresh_diagnostic(outcomes[outcome_key], reason, now_iso())
                log_line(log_path, f"provider {outcome_key} {outcomes[outcome_key]}" + (f" ({reason})" if reason else ""))
            except Exception as exc:
                outcomes[outcome_key] = "unavailable"
                log_line(log_path, f"provider {outcome_key} unavailable: {type(exc).__name__}")

        document["generated_at"] = now_iso()
        try:
            validator(document, SCHEMA)
            log_line(log_path, "schema validation success")
        except Exception as exc:
            log_line(log_path, f"schema validation failure: {type(exc).__name__}")
            raise RefreshError("schema validation failed; Drive was not published") from exc

        write_atomic(runtime_path, document)
        try:
            result = publisher(service, runtime_path)
            log_line(log_path, f"Drive publish success: {result['action']}")
        except Exception as exc:
            log_line(log_path, f"Drive publish failure: {type(exc).__name__}")
            raise RefreshError("Drive publish failed") from exc
        log_line(log_path, "refresh end success")
        return {"providers": outcomes, "publish": result, "document": document}


def _additional_claude_accounts():
    """Optional {account_id: payload_path} mapping for extra Claude accounts,
    read from CLAUDE_STATUSLINE_PAYLOADS as a JSON object of account_id ->
    path strings. Absent/empty/unset reproduces today's single-account
    behavior exactly (empty dict, merged with nothing)."""
    raw = os.environ.get("CLAUDE_STATUSLINE_PAYLOADS")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RefreshError(f"CLAUDE_STATUSLINE_PAYLOADS must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RefreshError("CLAUDE_STATUSLINE_PAYLOADS must be a JSON object of account_id -> path")
    return {account_id: Path(path) for account_id, path in parsed.items()}


def discover_claude_accounts(home_path=None):
    """Discover enabled Claude accounts from claude_accounts.json in config dir,
    and merge with any explicit CLAUDE_STATUSLINE_PAYLOADS environment variable overrides.
    Returns {account_id: payload_path}."""
    home = Path(home_path or os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager"))
    registry_path = home / "config" / "claude_accounts.json"
    accounts_map = {}
    if registry_path.is_file():
        try:
            from manager.claude_account_selector import load_claude_accounts
            for acc in load_claude_accounts(registry_path):
                if acc.get("enabled", True):
                    acc_id = acc["account_id"]
                    config_dir = acc.get("config_dir")
                    if config_dir:
                        accounts_map[acc_id] = Path(config_dir) / "statusline-payload.json"
                    else:
                        accounts_map[acc_id] = Path(os.environ.get("CLAUDE_STATUSLINE_PAYLOAD", Path.home() / ".claude" / "statusline-payload.json"))
        except Exception:
            pass

    # Merge explicit CLAUDE_STATUSLINE_PAYLOADS env overrides
    env_accounts = _additional_claude_accounts()
    accounts_map.update(env_accounts)
    return accounts_map


def discover_claude_config_dirs(home_path=None):
    """Discover enabled Claude accounts' config_dir (for OAuth credential
    lookup) from the same claude_accounts.json registry
    discover_claude_accounts reads. Returns {account_id: config_dir_or_None}
    -- config_dir=None means "use the default ~/.claude location", matching
    collectors.claude_oauth.read_access_token's own default. Never reads or
    returns any credential contents itself, only the directory path the
    OAuth collector should look in."""
    home = Path(home_path or os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager"))
    registry_path = home / "config" / "claude_accounts.json"
    config_dirs = {}
    if registry_path.is_file():
        try:
            from manager.claude_account_selector import load_claude_accounts
            for acc in load_claude_accounts(registry_path):
                if acc.get("enabled", True):
                    config_dirs[acc["account_id"]] = acc.get("config_dir")
        except Exception:
            pass
    return config_dirs


def main():
    home = Path(os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager"))
    log_path = home / "logs" / "refresh.log"
    try:
        from manager.production_guard import RuntimeGuardError, require_runtime_guard
        require_runtime_guard(manager_home=home)
        result = refresh(
            service=build_service(),
            runtime_path=home / "runtime" / "status.json",
            log_path=log_path,
            lock_path=home / "refresh.lock",
            claude_path=Path(os.environ.get("CLAUDE_STATUSLINE_PAYLOAD", Path.home() / ".claude" / "statusline-payload.json")),
            claude_accounts=discover_claude_accounts(home),
            # Real production run: also attempt OAuth for the legacy
            # account_id=None slot against the default ~/.claude location,
            # in addition to whatever discover_claude_config_dirs finds in
            # the registry. Test callers that don't pass claude_config_dirs
            # at all are unaffected -- this injection only happens here.
            claude_config_dirs={None: None, **discover_claude_config_dirs(home)},
        )
        print(f"REFRESHED Drive status.json ({result['publish']['action']})")
        return 0
    except RuntimeGuardError as exc:
        # A blocked runtime must not create a refresh/status artifact.
        print(json.dumps({"status": "blocked", "reason": exc.code}), file=sys.stderr)
        return 1
    except RefreshError as exc:
        # This is the only place a lock-contention failure (or any other
        # RefreshError raised before the "refresh start" line at
        # refresh()'s runtime_lock entry) gets recorded anywhere -- the
        # wscript.exe scheduled-task wrapper runs hidden and discards
        # stdout/stderr, so without this log_line the run leaves zero
        # trace in refresh.log.
        log_line(log_path, f"refresh failed before start: RefreshError: {exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log_line(log_path, f"refresh initialization failed: {type(exc).__name__}")
        print(f"ERROR: refresh initialization failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    from manager.win_background_guard import install_hidden_subprocess_guard
    install_hidden_subprocess_guard()
    raise SystemExit(main())
