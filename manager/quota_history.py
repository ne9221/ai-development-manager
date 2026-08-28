#!/usr/bin/env python3
"""Bounded, per-account, per-window quota telemetry history store and retention."""

import argparse
import json
import logging
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from jsonschema import Draft202012Validator, FormatChecker

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "0.1.0"
DEFAULT_MAX_SNAPSHOTS_PER_SERIES = 100
DEFAULT_MAX_RETENTION_HOURS = 336.0  # 14 days
SCHEMA_PATH = Path(__file__).parents[1] / "schema" / "quota_history.schema.json"


class QuotaHistoryError(RuntimeError):
    """Base exception for quota history operations."""
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_time(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def validate_quota_history(document: Dict[str, Any], schema_path: Optional[Path] = None) -> None:
    path = schema_path or SCHEMA_PATH
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise QuotaHistoryError(f"quota history schema validation failed: {exc}") from exc


def sanitize_snapshot(item: Dict[str, Any], observed_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Extract and validate ONLY telemetry metadata for a quota snapshot.

    Strictly discards prompts, conversations, task content, credentials, tokens,
    or arbitrary metadata to ensure bounded privacy and deterministic schema.
    """
    if not isinstance(item, dict):
        return None

    provider = item.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return None
    provider = provider.strip()

    account_id = item.get("account_id")
    if account_id is not None:
        if not isinstance(account_id, str) or not account_id.strip():
            account_id = None
        else:
            account_id = account_id.strip()

    # Determine observation timestamp
    obs_time = (
        observed_at
        or item.get("observed_at")
        or item.get("last_updated")
        or item.get("captured_at")
    )
    dt = parse_iso_time(obs_time)
    if dt is None:
        obs_time = now_iso()
    else:
        obs_time = dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    last_updated = item.get("last_updated")
    lu_dt = parse_iso_time(last_updated)
    lu_str = lu_dt.isoformat(timespec="seconds").replace("+00:00", "Z") if lu_dt else obs_time

    source = item.get("source", "not_reported")
    if not isinstance(source, str):
        source = "not_reported"

    source_type = item.get("source_type", "manual")
    if source_type not in ("official", "local_estimate", "manual"):
        source_type = "manual"

    confidence = item.get("confidence", "unknown")
    if not isinstance(confidence, str):
        confidence = "unknown"

    status = item.get("status", "unknown")
    if not isinstance(status, str):
        status = "unknown"

    raw_windows = item.get("windows", [])
    sanitized_windows = []
    if isinstance(raw_windows, list):
        for w in raw_windows:
            if not isinstance(w, dict):
                continue
            name = w.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            duration = w.get("duration_minutes")
            if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 0):
                duration = None
            used = w.get("used_percent")
            if used is not None and (not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 100):
                used = None
            else:
                used = float(used) if used is not None else None
            rem = w.get("remaining_percent")
            if rem is not None and (not isinstance(rem, (int, float)) or isinstance(rem, bool) or not 0 <= rem <= 100):
                rem = None
            else:
                rem = float(rem) if rem is not None else None

            resets_at = w.get("resets_at")
            r_dt = parse_iso_time(resets_at)
            resets_str = r_dt.isoformat(timespec="seconds").replace("+00:00", "Z") if r_dt else None

            sanitized_windows.append({
                "name": name.strip(),
                "duration_minutes": duration,
                "used_percent": used,
                "remaining_percent": rem,
                "resets_at": resets_str,
            })

    return {
        "provider": provider,
        "account_id": account_id,
        "observed_at": obs_time,
        "last_updated": lu_str,
        "source": source,
        "source_type": source_type,
        "confidence": confidence,
        "status": status,
        "windows": sanitized_windows,
    }


class QuotaHistoryStore:
    """File-backed or in-memory bounded quota telemetry history store."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        max_snapshots_per_series: int = DEFAULT_MAX_SNAPSHOTS_PER_SERIES,
        max_retention_hours: float = DEFAULT_MAX_RETENTION_HOURS,
        schema_path: Optional[Path] = None,
        fail_safe: bool = True,
    ):
        self.path = Path(path) if path is not None else None
        self.max_snapshots_per_series = max(2, max_snapshots_per_series)
        self.max_retention_hours = max(1.0, max_retention_hours)
        self.schema_path = schema_path or SCHEMA_PATH
        self.fail_safe = fail_safe
        self._memory_doc: Optional[Dict[str, Any]] = None if self.path is not None else self._empty_document()

    def _empty_document(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "snapshots": [],
        }

    def load(self) -> Dict[str, Any]:
        """Load and validate the quota history document."""
        if self.path is None:
            return deepcopy(self._memory_doc or self._empty_document())

        if not self.path.is_file():
            return self._empty_document()

        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return self._empty_document()
            doc = json.loads(raw)
            validate_quota_history(doc, self.schema_path)
            return doc
        except Exception as exc:
            logger.warning("Failed to load quota history from %s: %s", self.path, exc)
            if self.fail_safe:
                return self._empty_document()
            raise QuotaHistoryError(f"could not load quota history from {self.path}: {exc}") from exc

    def save(self, document: Dict[str, Any]) -> None:
        """Atomically persist and validate the quota history document."""
        document["updated_at"] = now_iso()
        try:
            validate_quota_history(document, self.schema_path)
        except Exception as exc:
            logger.warning("Validation error saving quota history: %s", exc)
            if not self.fail_safe:
                raise
            return  # Fail-safe: never write schema-invalid document to disk!

        if self.path is None:
            self._memory_doc = deepcopy(document)
            return

        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(document, indent=2) + "\n")
            Path(temporary).replace(self.path)
        except Exception as exc:
            logger.warning("Failed to atomically save quota history to %s: %s", self.path, exc)
            if not self.fail_safe:
                raise QuotaHistoryError(f"could not save quota history to {self.path}: {exc}") from exc
        finally:
            try:
                if temporary is not None and Path(temporary).exists():
                    Path(temporary).unlink()
            except Exception:
                pass

    def _prune_snapshots(
        self,
        snapshots: List[Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Apply deterministic pruning: time window filter + max N entries per (provider, account_id)."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.max_retention_hours)

        # 1. Group valid snapshots by (provider, account_id)
        grouped: Dict[Tuple[str, Optional[str]], List[Dict[str, Any]]] = {}
        for s in snapshots:
            dt = parse_iso_time(s.get("observed_at") or s.get("last_updated"))
            if dt is None or dt < cutoff:
                continue
            key = (s["provider"], s.get("account_id"))
            grouped.setdefault(key, []).append(s)

        # 2. For each group: deduplicate by timestamp (last-wins), sort chronologically, keep last N
        pruned_all: List[Dict[str, Any]] = []
        for key, items in grouped.items():
            dedup_by_ts: Dict[str, Dict[str, Any]] = {}
            for item in items:
                ts_str = item.get("observed_at") or item.get("last_updated") or ""
                dedup_by_ts[ts_str] = item

            # Sort chronologically
            sorted_items = sorted(
                dedup_by_ts.values(),
                key=lambda x: parse_iso_time(x.get("observed_at") or x.get("last_updated")) or datetime.min.replace(tzinfo=timezone.utc),
            )

            # Bounded count per series
            if len(sorted_items) > self.max_snapshots_per_series:
                sorted_items = sorted_items[-self.max_snapshots_per_series:]

            pruned_all.extend(sorted_items)

        # Sort overall list chronologically
        pruned_all.sort(
            key=lambda x: parse_iso_time(x.get("observed_at") or x.get("last_updated")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        return pruned_all

    def append_snapshot(
        self,
        snapshot_or_provider: Dict[str, Any],
        now: Optional[datetime] = None,
    ) -> bool:
        """Sanitize, deduplicate, append, prune, and save a quota telemetry snapshot."""
        sanitized = sanitize_snapshot(snapshot_or_provider)
        if sanitized is None:
            return False

        now = now or datetime.now(timezone.utc)
        try:
            doc = self.load()
            snapshots = doc.get("snapshots", [])
            snapshots.append(sanitized)
            doc["snapshots"] = self._prune_snapshots(snapshots, now=now)
            self.save(doc)
            return True
        except Exception as exc:
            logger.warning("append_snapshot failed: %s", exc)
            if not self.fail_safe:
                raise
            return False

    def append_snapshots(
        self,
        snapshots: Sequence[Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> int:
        """Batch append multiple snapshots with a single atomic write."""
        sanitized_list = [sanitize_snapshot(s) for s in snapshots]
        valid_items = [s for s in sanitized_list if s is not None]
        if not valid_items:
            return 0

        now = now or datetime.now(timezone.utc)
        try:
            doc = self.load()
            all_snapshots = doc.get("snapshots", []) + valid_items
            doc["snapshots"] = self._prune_snapshots(all_snapshots, now=now)
            self.save(doc)
            return len(valid_items)
        except Exception as exc:
            logger.warning("append_snapshots failed: %s", exc)
            if not self.fail_safe:
                raise
            return 0

    def get_history(
        self,
        provider: Optional[str] = None,
        account_id: Optional[str] = None,
        window_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve history snapshots filtered by provider, account_id, and/or window_name."""
        doc = self.load()
        snapshots = doc.get("snapshots", [])
        filtered = []
        for s in snapshots:
            if provider is not None and s.get("provider") != provider:
                continue
            if account_id is not None and s.get("account_id") != account_id:
                continue
            if window_name is not None:
                if not any(w.get("name") == window_name for w in s.get("windows", [])):
                    continue
            filtered.append(deepcopy(s))
        return filtered

    def get_account_history(
        self,
        provider: str,
        account_id: Optional[str] = None,
        window_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Strictly retrieve history for a specific (provider, account_id)."""
        doc = self.load()
        snapshots = doc.get("snapshots", [])
        filtered = []
        for s in snapshots:
            if s.get("provider") != provider:
                continue
            if s.get("account_id") != account_id:
                continue
            if window_name is not None:
                if not any(w.get("name") == window_name for w in s.get("windows", [])):
                    continue
            filtered.append(deepcopy(s))
        return filtered

    def prune(self, now: Optional[datetime] = None) -> int:
        """Run pruning pass and persist pruned document."""
        now = now or datetime.now(timezone.utc)
        try:
            doc = self.load()
            orig_count = len(doc.get("snapshots", []))
            doc["snapshots"] = self._prune_snapshots(doc.get("snapshots", []), now=now)
            pruned_count = orig_count - len(doc["snapshots"])
            self.save(doc)
            return max(0, pruned_count)
        except Exception as exc:
            logger.warning("prune failed: %s", exc)
            if not self.fail_safe:
                raise
            return 0

    def clear(self) -> None:
        """Clear all stored snapshots."""
        self.save(self._empty_document())


def get_default_quota_history_store(path: Optional[Union[str, Path]] = None) -> QuotaHistoryStore:
    """Get standard file-backed QuotaHistoryStore in AI manager runtime folder."""
    if path is not None:
        return QuotaHistoryStore(path)
    home = Path(os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager"))
    return QuotaHistoryStore(home / "runtime" / "quota_history.json")


def main():
    parser = argparse.ArgumentParser(description="Query or manage quota telemetry history.")
    parser.add_argument("--provider", help="Filter by provider (e.g. claude, codex)")
    parser.add_argument("--account-id", help="Filter by account id")
    parser.add_argument("--window", help="Filter by window name (e.g. five_hour)")
    parser.add_argument("--prune", action="store_true", help="Run retention pruning")
    parser.add_argument("--clear", action="store_true", help="Clear all stored snapshots")
    parser.add_argument("--path", type=Path, help="Explicit quota history file path")
    args = parser.parse_args()

    store = get_default_quota_history_store(args.path)
    if args.clear:
        store.clear()
        print("Cleared quota history.")
        return 0
    if args.prune:
        count = store.prune()
        print(f"Pruned {count} snapshots.")
        return 0

    history = store.get_history(
        provider=args.provider,
        account_id=args.account_id,
        window_name=args.window,
    )
    print(json.dumps(history, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
