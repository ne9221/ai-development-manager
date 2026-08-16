#!/usr/bin/env python3
"""Read and summarize the Google Drive runtime quota SSOT."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.publish_drive import FOLDER_ID, FILE_NAME, build_service
from jsonschema import Draft202012Validator, FormatChecker


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
        return document
    except QuotaReaderError:
        raise
    except Exception as exc:
        raise QuotaReaderError(f"Drive status read failed: {exc}") from exc


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
        "nearest_reset_at": nearest_reset,
    }


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
        for item in candidates:
            account_summary = _summarize_item(provider_id, display_name, item, now, max_age_minutes)
            account_summary["account_id"] = item.get("account_id")
            accounts_output.append(account_summary)

        representative = _legacy_representative(candidates)
        providers_output.append(_summarize_item(provider_id, display_name, representative or {}, now, max_age_minutes))

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
