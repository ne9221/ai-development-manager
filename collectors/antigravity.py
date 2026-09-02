#!/usr/bin/env python3
"""Collect Antigravity quota from the IDE's language server (official RPC, no model turn).

Mirrors ``collectors/codex.py``: ``collect(timeout)`` returns ``(raw, document)``
where ``document`` is a ``status.schema.json`` v0.1 document carrying one
``provider="antigravity"`` entry. The source is the language server's own
``RetrieveUserQuotaSummary`` + ``GetUserStatus`` Connect-RPCs, which the IDE
UI itself uses -- ``source_type="official"``, ``confidence="official"``.

Window names are the server's bucket ids (``gemini-5h``, ``gemini-weekly``,
``3p-5h``, ``3p-weekly``): two model groups, each with a 5-hour and a weekly
limit. Nothing is fabricated: when the server cannot be reached or the payload
shape changed, ``collect()`` raises ``CollectorError`` (with a
``classification``) and the refresh loop keeps the previous last-good entry.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from manager.ag_language_server import (
    AgLanguageServerClient,
    AgLsError,
    _account_records,
    _bucket_records,
    discover_language_server,
    redact,
)

SOURCE = "antigravity_language_server_quota_summary"
WINDOW_MINUTES = {"5h": 300, "weekly": 10080}


class CollectorError(RuntimeError):
    def __init__(self, classification, detail=""):
        self.classification = classification
        self.detail = str(detail)[:500]
        super().__init__(f"{classification}: {self.detail}" if self.detail else classification)


def iso_time(value=None):
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_reset(value):
    if not value:
        return None
    try:
        return iso_time(value)
    except (TypeError, ValueError):
        return None


def normalize(user_status, quota_summary, language_server=None, captured_at=None):
    """Build the status document from the two raw RPC responses (pure, testable)."""
    try:
        buckets = _bucket_records(quota_summary)
    except AgLsError as exc:
        raise CollectorError(exc.classification, exc.detail) from exc
    account, models = _account_records(user_status)
    if account is None or not account.get("email"):
        raise CollectorError("account_identity_unavailable", "GetUserStatus carried no account identity")
    captured = captured_at or iso_time()
    windows = []
    for bucket in buckets:
        remaining = round(bucket["remaining_fraction"] * 100, 1)
        windows.append({
            "name": bucket["bucket_id"] or f"{bucket['group']}:{bucket['window']}",
            "duration_minutes": WINDOW_MINUTES.get(bucket["window"]),
            "used_percent": round(100 - remaining, 1),
            "remaining_percent": remaining,
            "resets_at": _safe_reset(bucket.get("reset_time")),
        })
    exhausted = [window for window in windows if window["remaining_percent"] <= 0.0]
    status = "exhausted" if exhausted and len(exhausted) == len(windows) else "low" if exhausted else "ok"
    metadata = {
        "method": "RetrieveUserQuotaSummary+GetUserStatus",
        "quota_scope": "account",
        "account_email": account.get("email"),
        "account_name": account.get("name"),
        "plan_name": account.get("plan_name"),
        "teams_tier": account.get("teams_tier"),
        "available_prompt_credits": account.get("available_prompt_credits"),
        "available_flow_credits": account.get("available_flow_credits"),
        "groups": [{"bucket_id": bucket["bucket_id"], "group": bucket["group"], "window": bucket["window"],
                    "display_name": bucket["display_name"]} for bucket in buckets],
        "models": [{"model_id": model["model_id"], "label": model["label"], "remaining_fraction": model["remaining_fraction"],
                    "reset_time": _safe_reset(model.get("reset_time"))} for model in models],
        "language_server": dict(language_server) if isinstance(language_server, dict) else None,
    }
    return {
        "schema_version": "0.1.0",
        "generated_at": captured,
        "providers": [{
            "provider": "antigravity", "display_name": "Antigravity",
            "collection_mode": "automatic", "source": SOURCE,
            "source_type": "official", "confidence": "official",
            "last_updated": captured, "status": status, "windows": windows,
            "metadata": {key: value for key, value in metadata.items() if value is not None},
        }],
    }


def validate(document, schema_path):
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def collect(timeout=20, *, discover=None, client_factory=None):
    """Return ``(raw_redacted, document)``; raises CollectorError with a classification on any failure."""
    discover = discover or discover_language_server
    client_factory = client_factory or AgLanguageServerClient
    try:
        endpoint = discover(timeout=timeout)
        client = client_factory(endpoint, timeout=timeout)
        user_status = client.get_user_status()
        quota_summary = client.retrieve_user_quota_summary()
    except AgLsError as exc:
        raise CollectorError(exc.classification, exc.detail) from exc
    document = normalize(user_status, quota_summary, language_server=endpoint.evidence())
    raw = redact({"user_status": {"userStatus": {key: value for key, value in (user_status.get("userStatus") or {}).items()
                                                  if key in ("email", "name", "planStatus")}},
                  "quota_summary": quota_summary, "language_server": endpoint.evidence()})
    return raw, document


def main():
    parser = argparse.ArgumentParser(description="Collect Antigravity quota from the running IDE language server")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--schema", default=str(Path(__file__).resolve().parents[1] / "schema" / "status.schema.json"))
    parser.add_argument("--raw", action="store_true", help="print the redacted raw RPC payloads instead of the document")
    args = parser.parse_args()
    try:
        raw, document = collect(args.timeout)
        validate(document, args.schema)
    except CollectorError as exc:
        print(json.dumps({"error": exc.classification, "detail": exc.detail}), file=sys.stderr)
        return 1
    print(json.dumps(raw if args.raw else document, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
