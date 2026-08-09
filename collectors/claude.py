#!/usr/bin/env python3
"""Receive Claude Code's official statusline JSON and update runtime status."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


class CollectorError(RuntimeError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def reset_iso(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise CollectorError("resets_at must be an ISO 8601 string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectorError(f"invalid resets_at: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize(payload, captured_at=None):
    if not isinstance(payload, dict):
        raise CollectorError("statusline input must be a JSON object")
    rate_limits = payload.get("rate_limits")
    if rate_limits is None:
        rate_limits = {}
    if not isinstance(rate_limits, dict):
        raise CollectorError("rate_limits must be an object when present")

    windows = []
    durations = {"five_hour": 300, "seven_day": 10080}
    for name, duration in durations.items():
        window = rate_limits.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise CollectorError(f"rate_limits.{name} must be an object or null")
        used = window.get("used_percentage")
        if not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 100:
            raise CollectorError(f"rate_limits.{name}.used_percentage must be a number from 0 to 100")
        windows.append({
            "name": name,
            "duration_minutes": duration,
            "used_percent": used,
            "remaining_percent": 100 - used,
            "resets_at": reset_iso(window.get("resets_at")),
        })

    captured_at = captured_at or now_iso()
    return {
        "provider": "claude",
        "display_name": "Claude Code",
        "collection_mode": "automatic",
        "source": "claude_code_statusline_rate_limits",
        "source_type": "official",
        "confidence": "official" if windows else "unknown",
        "last_updated": captured_at,
        "status": "ok" if windows else "unknown",
        "windows": windows,
        "metadata": {
            "official_rate_limits_available": bool(windows),
            "missing_windows": [name for name in durations if name not in rate_limits or rate_limits.get(name) is None],
        },
    }


def validate(document, schema_path):
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise CollectorError(f"schema validation failed: {exc}") from exc


def update_status(status_path, schema_path, provider):
    if not status_path.is_file():
        raise CollectorError(f"runtime status JSON not found: {status_path}")
    try:
        document = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError(f"runtime status JSON is invalid: {exc}") from exc
    validate(document, schema_path)

    existing = next((item for item in document["providers"] if item.get("provider") == "claude"), None)
    if not provider["windows"] and existing and existing.get("source_type") == "official" and existing.get("windows"):
        return document, False
    document["providers"] = [item for item in document["providers"] if item.get("provider") != "claude"] + [provider]
    document["generated_at"] = provider["last_updated"]
    validate(document, schema_path)
    temporary = status_path.with_suffix(status_path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(status_path)
    return document, True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", type=Path, default=Path(__file__).with_name("codex.status.json"))
    parser.add_argument("--schema", type=Path, default=Path(__file__).parents[1] / "schema" / "status.schema.json")
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.read().lstrip("\ufeff"))
        provider = normalize(payload)
        _, changed = update_status(args.status, args.schema, provider)
        if provider["windows"]:
            print("Claude quota: official snapshot captured")
        elif changed:
            print("Claude quota: official rate_limits unavailable")
        else:
            print("Claude quota: unavailable; preserved newer official snapshot")
    except (CollectorError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
