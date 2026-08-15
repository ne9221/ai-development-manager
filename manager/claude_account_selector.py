"""Fail-closed selection among multiple Claude accounts.

P0.1 shipped select_claude_account() as a standalone, pure primitive not yet
wired into anything real. P0.1.5 adds the two pieces needed to make it
usable from a live launch path without touching manager/dispatcher.py's
provider-level (codex vs claude vs ...) decision at all:

- load_claude_accounts(): reads a minimal, non-secret account registry
  (account_id/enabled/config_dir only -- see _FORBIDDEN_ACCOUNT_KEYS) from a
  JSON file. Credentials never live here; each account's only isolation
  mechanism is still its own CLAUDE_CONFIG_DIR, populated by a real `claude
  login` the caller runs out of band.
- resolve_claude_account(): combines that registry with a runtime status
  document's per-account claude quota entries (schema/status.schema.json's
  account_id field, added in P0.1) and calls select_claude_account() to
  return exactly one ready-to-launch {account_id, config_dir}, or raises.

manager/execution_runner.py::launch_task() is the actual live wiring point
(see its `claude_accounts` parameter) -- this module has no knowledge of
executions/sessions/launchers and stays a pure function of (registry, quota
document, explicit choice).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AccountSelectionError(RuntimeError):
    """Raised instead of guessing when no single Claude account can be
    confidently selected. Callers must fail closed on this, not fall back to
    picking the first/any account."""


class AccountRegistryError(RuntimeError):
    """Raised for a malformed/unsafe account registry -- e.g. a missing
    required field, or an entry that looks like it's trying to carry a
    credential. Fails closed rather than loading a partially-valid registry."""


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


# Keys that would indicate a credential leaked into the registry. Checked
# case-insensitively against every key in every account entry; the registry
# is identity/config-dir metadata only, never a place OAuth tokens live --
# each account's actual secret stays exactly where `claude login` put it,
# under that account's own CLAUDE_CONFIG_DIR.
_FORBIDDEN_ACCOUNT_KEYS = frozenset({
    "token", "access_token", "refresh_token", "oauth", "oauth_token",
    "credential", "credentials", "password", "secret", "api_key", "apikey",
})


def _validate_account_entry(entry):
    if not isinstance(entry, dict):
        raise AccountRegistryError("each Claude account registry entry must be a JSON object")
    leaked = {str(key).lower() for key in entry} & _FORBIDDEN_ACCOUNT_KEYS
    if leaked:
        raise AccountRegistryError(
            f"Claude account registry entry contains credential-shaped key(s): {sorted(leaked)}; "
            "the registry may only hold account_id/enabled/config_dir"
        )
    account_id = entry.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        raise AccountRegistryError("Claude account registry entry is missing a non-empty account_id")
    if "config_dir" not in entry:
        raise AccountRegistryError(f"Claude account {account_id!r} entry is missing the config_dir key")
    config_dir = entry["config_dir"]
    if config_dir is not None and (not isinstance(config_dir, str) or not config_dir.strip()):
        raise AccountRegistryError(f"Claude account {account_id!r} config_dir must be null or a non-empty string")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AccountRegistryError(f"Claude account {account_id!r} enabled must be a boolean")
    return {"account_id": account_id, "enabled": enabled, "config_dir": config_dir}


def load_claude_accounts(path):
    """Load and validate the Claude account registry from a JSON file shaped
    {"accounts": [{"account_id", "enabled"?, "config_dir"}, ...]}. A missing
    file returns [] (no registry configured is not an error -- callers that
    never pass claude_accounts to launch_task() don't need one to exist);
    a present-but-malformed file raises AccountRegistryError rather than
    silently dropping/guessing at bad entries."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountRegistryError(f"could not read Claude account registry {path}: {exc}") from exc
    entries = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise AccountRegistryError("Claude account registry must be a JSON object with an 'accounts' array")
    accounts = [_validate_account_entry(entry) for entry in entries]
    ids = [account["account_id"] for account in accounts]
    if len(ids) != len(set(ids)):
        raise AccountRegistryError("Claude account registry has duplicate account_id entries")
    return accounts


def resolve_claude_account(accounts, quota_document=None, *, explicit_account_id=None,
                            max_age_seconds=None, now=None):
    """Combine a loaded account registry with a runtime status document's
    per-account claude quota entries to pick exactly one launch-ready
    account, or raise. Returns {"account_id": ..., "config_dir": ...} --
    config_dir may legitimately be None (the single/legacy-account entry,
    meaning: do not override CLAUDE_CONFIG_DIR, use whatever is already
    logged in, unchanged from pre-P0.1 behavior).

    quota_document: the schema/status.schema.json-shaped document (e.g. from
    manager.quota_reader.read_drive_status()); its provider=="claude"
    entries are matched to registry accounts by account_id. An account with
    no matching entry at all is treated as confidence=unknown -- present in
    the registry but never captured is not evidence of anything.
    """
    quota_by_account = {}
    for item in (quota_document or {}).get("providers", []):
        if item.get("provider") == "claude":
            quota_by_account[item.get("account_id")] = item

    selection_input = [
        {
            "account_id": account["account_id"],
            "enabled": account["enabled"],
            "confidence": quota_by_account.get(account["account_id"], {}).get("confidence"),
            "last_updated": quota_by_account.get(account["account_id"], {}).get("last_updated"),
        }
        for account in accounts
    ]
    selected_id = select_claude_account(
        selection_input, explicit_account_id=explicit_account_id,
        max_age_seconds=max_age_seconds, now=now,
    )
    selected = next(account for account in accounts if account["account_id"] == selected_id)
    return {"account_id": selected["account_id"], "config_dir": selected["config_dir"]}
