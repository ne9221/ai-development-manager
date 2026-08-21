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


def write_atomic(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def refresh(*, service, runtime_path, log_path, lock_path, claude_path, claude_accounts=None,
            reader=read_drive_status, codex_collector=collect_codex,
            publisher=sync_drive, validator=validate_status, history_store=None):
    """`claude_path` remains the single/legacy-account payload path (account_id=None),
    unchanged from before. `claude_accounts`, if given, is an additional
    {account_id: path} mapping for extra Claude accounts; each is captured,
    logged, and published independently under its own (provider="claude",
    account_id=...) key -- one account's missing/stale payload never
    overwrites or blocks another's, and omitting claude_accounts entirely
    reproduces today's single-account behavior byte-for-byte."""
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
        for account_id, path in accounts.items():
            outcome_key = "claude" if account_id is None else f"claude:{account_id}"
            try:
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
                log_line(log_path, f"provider {outcome_key} {outcomes[outcome_key]}")
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


def main():
    home = Path(os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager"))
    try:
        result = refresh(
            service=build_service(),
            runtime_path=home / "runtime" / "status.json",
            log_path=home / "logs" / "refresh.log",
            lock_path=home / "refresh.lock",
            claude_path=Path(os.environ.get("CLAUDE_STATUSLINE_PAYLOAD", Path.home() / ".claude" / "statusline-payload.json")),
            claude_accounts=discover_claude_accounts(home),
        )
        print(f"REFRESHED Drive status.json ({result['publish']['action']})")
        return 0
    except RefreshError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: refresh initialization failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    from manager.win_background_guard import install_hidden_subprocess_guard
    install_hidden_subprocess_guard()
    raise SystemExit(main())
