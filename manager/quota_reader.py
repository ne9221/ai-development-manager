#!/usr/bin/env python3
"""Read and summarize the Google Drive runtime quota SSOT."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.publish_drive import FOLDER_ID, FILE_NAME, build_service
from jsonschema import Draft202012Validator, FormatChecker
from manager.acceptance_gate import apply_controlled_unavailability
from manager.manager_home import ManagerHomeError, resolve_manager_home


EXPECTED_PROVIDERS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "antigravity": "Antigravity",
    "gemini_app": "Gemini App / Google AI Pro",
}
FUTURE_SKEW_MINUTES = 5
RELIABLE_SOURCES = {
    "codex": {"codex_app_server", "official_app_server"},
    "claude": {"claude_code_statusline_rate_limits", "official_statusline"},
    # collectors/antigravity.py: the IDE language server's own
    # RetrieveUserQuotaSummary RPC (the same source the IDE UI renders).
    "antigravity": {"antigravity_language_server_quota_summary"},
}


class QuotaReaderError(RuntimeError):
    pass


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise QuotaReaderError(f"invalid timestamp: {value}") from exc


def validate_status(document, schema_path):
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise QuotaReaderError(f"status schema validation failed: {exc}") from exc


def read_drive_status(service=None, folder_id=FOLDER_ID, schema_path=None, validate_document=True):
    schema_path = schema_path or Path(__file__).parents[1] / "schema" / "status.schema.json"
    query = f"name='{FILE_NAME}' and '{folder_id}' in parents and trashed=false"
    try:
        service = service or build_service()
        files = service.files()
        matches = files.list(q=query, spaces="drive", fields="files(id,name,mimeType,parents)", pageSize=10).execute().get("files", [])
        if len(matches) != 1:
            raise QuotaReaderError(f"Drive SSOT must contain exactly one {FILE_NAME}; found {len(matches)}")
        raw = files.get_media(fileId=matches[0]["id"]).execute()
        document = json.loads(raw.decode("utf-8"))
        if validate_document:
            validate_status(document, schema_path)
        # Controlled acceptance gate (see manager.acceptance_gate's own
        # docstring): a no-op for every real caller on every real machine,
        # since it only ever does anything when a local file exists under
        # AI_MANAGER_HOME that nothing reachable from Drive/GitHub ingress
        # or the MCP adapter can create. Applied after validate_status()
        # above (the real fetched document must itself validate cleanly) so
        # this can never mask a real schema problem in the actual SSOT.
        return apply_controlled_unavailability(document, _gate_home())
    except QuotaReaderError:
        raise
    except Exception as exc:
        raise QuotaReaderError(f"Drive status read failed: {exc}") from exc


def _gate_home():
    """The manager home for the local acceptance gate, or None.

    The gate is a local test affordance that may append to an audit log, so
    the home it is handed must come from the one canonical resolver rather
    than a second spelling of the fallback. An unresolvable home makes the
    gate a no-op -- exactly what an unset AI_MANAGER_HOME already did --
    because a controlled-acceptance feature must never break a real quota
    read.
    """
    try:
        return resolve_manager_home()
    except ManagerHomeError:
        return None

def read_local_status(path=None, schema_path=None, validate_document=True):
    """Read the refresh worker's local runtime status without inventing data."""
    path = (Path(path) if path is not None
            else resolve_manager_home() / "runtime" / "status.json")
    schema_path = schema_path or Path(__file__).parents[1] / "schema" / "status.schema.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if validate_document:
            validate_status(document, schema_path)
        return apply_controlled_unavailability(document, _gate_home())
    except Exception as exc:
        raise QuotaReaderError(f"local runtime status read failed: {exc}") from exc


def _summarize_item(provider_id, display_name, item, now, max_age_minutes):
    updated = parse_time(item.get("last_updated"))
    age_minutes = None if updated is None else (now - updated).total_seconds() / 60
    future_skewed = age_minutes is not None and age_minutes < -FUTURE_SKEW_MINUTES
    stale = age_minutes is None or age_minutes > max_age_minutes or future_skewed
    windows = item.get("windows", [])
    resets = [(parse_time(window.get("resets_at")), window.get("resets_at")) for window in windows if window.get("resets_at")]
    future_resets = [pair for pair in resets if pair[0] and pair[0] >= now]
    nearest_reset = min(future_resets, default=(None, None), key=lambda pair: pair[0])[1]
    source_reliable = (
        item.get("source_type") == "official"
        and item.get("confidence") == "official"
    )
    source_verified = source_reliable and item.get("source") in RELIABLE_SOURCES.get(provider_id, set())
    reliable = (
        not stale
        and source_reliable
        and bool(windows)
        and any(window.get("remaining_percent") is not None for window in windows)
    )
    exhausted = any(
        window.get("remaining_percent") == 0.0 or window.get("used_percent") == 100.0
        for window in windows
    )
    return {
        "provider": provider_id,
        "display_name": item.get("display_name", display_name),
        "status": item.get("status", "unknown"),
        "collection_mode": item.get("collection_mode", "manual"),
        "source": item.get("source", "not_reported"),
        "source_type": item.get("source_type", "manual"),
        "confidence": item.get("confidence", "unknown"),
        "last_updated": item.get("last_updated"),
        "freshness": "stale" if stale else "fresh",
        "stale": stale,
        "age_minutes": None if age_minutes is None else round(max(0, age_minutes), 1),
        "future_skewed": future_skewed,
        "windows": windows,
        "source_reliable": source_reliable,
        "source_verified": source_verified,
        "has_reliable_quota": reliable,
        # Reliability establishes that this record is trustworthy; usability
        # additionally applies the existing launch gate's exhausted-window
        # rule.  This remains scoped to this one record/account.
        "has_usable_quota": reliable and not exhausted,
        "nearest_reset_at": nearest_reset,
        # Passed through unmodified (e.g. Codex's metadata.credits) so
        # downstream forecasting (manager.quota_forecast.forecast_account)
        # can distinguish "subscription quota exhausted" from "provider
        # unavailable" -- extra credits are a separate pool from the quota
        # windows above and must not be silently dropped here.
        "metadata": item.get("metadata", {}),
    }


def unknown_account_summary(provider_id, display_name, account_id, now=None, max_age_minutes=60):
    """Synthesize an unknown/unavailable quota summary for an explicit
    account_id that has no captured per-account entry in the SSOT yet.

    This is deliberately built from an empty source item (via
    _summarize_item), never from another account's or the provider-level
    legacy representative's real data -- callers that ask about one specific,
    uncaptured account must see unknown/stale evidence scoped to exactly that
    account_id, not borrowed numbers that would misrepresent it as reliable
    or as sharing another account's standing."""
    now = now or datetime.now(timezone.utc)
    summary = _summarize_item(provider_id, display_name, {}, now, max_age_minutes)
    summary["account_id"] = account_id
    return summary


def _dedupe_last_wins(candidates):
    """Collapse duplicate entries that share the same account_id within a
    single provider's candidate list. Keeps the last (highest document-order)
    entry per account_id whole -- never blends fields from two records --
    matching the pre-P0.1 `{item["provider"]: item for item in ...}` dict
    comprehension's last-wins semantics, extended from one implicit
    account_id=None key per provider to one key per account_id."""
    deduped = {}
    for item in candidates:
        deduped[item.get("account_id")] = item
    return list(deduped.values())


def _legacy_representative(candidates):
    """Pick the (provider, account_id) entry that stands in for the
    provider-level legacy summary. account_id=None (the pre-P0.1
    single-account entry) always wins when present. Otherwise, among
    named-account-only entries, pick deterministically by account_id string
    so provider-level output never depends on input order -- callers that
    still index by provider alone (dispatcher.py, command_watcher.py,
    scheduler.py, assignment.py, runtime_bridge.py) get a stable, defined
    result instead of an arbitrary one."""
    legacy = next((item for item in candidates if item.get("account_id") is None), None)
    if legacy is not None:
        return legacy
    if not candidates:
        return None
    return min(candidates, key=lambda item: str(item.get("account_id")))


def _provider_summary(provider_id, display_name, account_summaries, now, max_age_minutes):
    """Return one provider-level view without combining account quota pools."""
    named = [item for item in account_summaries if item.get("account_id") is not None]
    if named:
        eligible = [item for item in named if item["has_usable_quota"]]
        # A deterministic eligible account is evidence/scoring only.  R2
        # remains the authority for the launch account.
        representative = min(eligible or named, key=lambda item: str(item["account_id"]))
        result = {key: value for key, value in representative.items() if key != "account_id"}
        result["has_reliable_quota"] = bool(eligible)
        result["has_usable_quota"] = bool(eligible)
        result["availability"] = {
            "scope": "eligible_named_account",
            "eligible_account_ids": sorted((item["account_id"] for item in eligible), key=str),
        }
        return result
    legacy = next((item for item in account_summaries if item.get("account_id") is None), None)
    result = {key: value for key, value in (legacy or unknown_account_summary(provider_id, display_name, None, now, max_age_minutes)).items() if key != "account_id"}
    result["availability"] = {"scope": "provider_record", "eligible_account_ids": []}
    return result


def summarize(document, max_age_minutes=60, now=None):
    now = now or datetime.now(timezone.utc)
    by_provider = {}
    for item in document.get("providers", []):
        provider_id = item.get("provider")
        if provider_id not in EXPECTED_PROVIDERS:
            continue
        by_provider.setdefault(provider_id, []).append(item)

    providers_output = []
    accounts_output = []
    for provider_id, display_name in EXPECTED_PROVIDERS.items():
        candidates = _dedupe_last_wins(by_provider.get(provider_id, []))
        provider_accounts = []
        for item in candidates:
            account_summary = _summarize_item(provider_id, display_name, item, now, max_age_minutes)
            account_summary["account_id"] = item.get("account_id")
            accounts_output.append(account_summary)
            provider_accounts.append(account_summary)

        providers_output.append(_provider_summary(provider_id, display_name, provider_accounts, now, max_age_minutes))

    return {
        "generated_at": document.get("generated_at"),
        "freshness_minutes": max_age_minutes,
        "providers": providers_output,
        "accounts": accounts_output,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-minutes", type=float, default=60)
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(read_drive_status(), args.max_age_minutes), indent=2))
    except QuotaReaderError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
