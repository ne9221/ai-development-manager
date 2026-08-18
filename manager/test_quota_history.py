#!/usr/bin/env python3
"""Targeted unit and regression tests for QuotaHistoryStore and snapshot retention."""

import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from manager.quota_history import (
    QuotaHistoryStore,
    QuotaHistoryError,
    sanitize_snapshot,
    validate_quota_history,
    parse_iso_time,
)
from manager.quota_forecast import (
    calculate_window_burn_rate,
    forecast_account,
    forecast_daily_brief,
    WarningLevel,
    RiskStatus,
    ActionRecommendation,
)

NOW = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)


def make_snapshot(
    provider="claude",
    account_id="A",
    remaining=80.0,
    used=20.0,
    resets_at=None,
    last_updated=None,
    window_name="five_hour",
    extra_windows=None,
    **extras,
):
    ts = (last_updated or NOW).isoformat() if isinstance(last_updated, datetime) else (last_updated or NOW.isoformat())
    r_str = resets_at.isoformat() if isinstance(resets_at, datetime) else resets_at
    windows = [{"name": window_name, "remaining_percent": remaining, "used_percent": used, "resets_at": r_str, "duration_minutes": 300}]
    if extra_windows:
        windows.extend(extra_windows)
    item = {
        "provider": provider,
        "account_id": account_id,
        "display_name": f"{provider.title()} {account_id or ''}".strip(),
        "source": "claude_code_statusline_rate_limits",
        "source_type": "official",
        "confidence": "official",
        "status": "ok",
        "last_updated": ts,
        "windows": windows,
    }
    item.update(extras)
    return item


class QuotaHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp_dir.name) / "quota_history.json"
        self.store = QuotaHistoryStore(self.history_path, max_snapshots_per_series=5, max_retention_hours=24.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    # 1. Claude A/B history isolation
    def test_claude_account_a_and_b_isolation(self):
        t1 = NOW - timedelta(hours=2)
        t2 = NOW - timedelta(hours=1)
        reset_time = NOW + timedelta(hours=4)

        # Account A snapshots: 90% -> 80% (10%/h)
        s_a1 = make_snapshot("claude", "A", remaining=90.0, last_updated=t1, resets_at=reset_time)
        s_a2 = make_snapshot("claude", "A", remaining=80.0, last_updated=t2, resets_at=reset_time)

        # Account B snapshots: 30% -> 20% (10%/h)
        s_b1 = make_snapshot("claude", "B", remaining=30.0, last_updated=t1, resets_at=reset_time)
        s_b2 = make_snapshot("claude", "B", remaining=20.0, last_updated=t2, resets_at=reset_time)

        self.store.append_snapshots([s_a1, s_b1, s_a2, s_b2], now=NOW)

        hist_a = self.store.get_account_history("claude", "A")
        hist_b = self.store.get_account_history("claude", "B")

        self.assertEqual(len(hist_a), 2)
        self.assertEqual(len(hist_b), 2)
        self.assertTrue(all(s["account_id"] == "A" for s in hist_a))
        self.assertTrue(all(s["account_id"] == "B" for s in hist_b))

        cur_a = make_snapshot("claude", "A", remaining=70.0, last_updated=NOW, resets_at=reset_time)
        cur_b = make_snapshot("claude", "B", remaining=10.0, last_updated=NOW, resets_at=reset_time)

        fc_a = forecast_account(cur_a, history=self.store.get_history(), now=NOW)
        fc_b = forecast_account(cur_b, history=self.store.get_history(), now=NOW)

        self.assertEqual(fc_a.windows[0].burn_rate_samples, 3)
        self.assertEqual(fc_b.windows[0].burn_rate_samples, 3)
        self.assertAlmostEqual(fc_a.windows[0].burn_rate_pct_per_hour, 10.0, places=1)
        self.assertAlmostEqual(fc_b.windows[0].burn_rate_pct_per_hour, 10.0, places=1)

    # 2. five_hour vs seven_day history isolation
    def test_five_hour_and_seven_day_window_isolation(self):
        t1 = NOW - timedelta(hours=2)
        t2 = NOW - timedelta(hours=1)
        reset_5h = NOW + timedelta(hours=3)
        reset_7d = NOW + timedelta(days=5)

        w_7d_1 = {"name": "seven_day", "remaining_percent": 50.0, "used_percent": 50.0, "resets_at": reset_7d.isoformat()}
        w_7d_2 = {"name": "seven_day", "remaining_percent": 48.0, "used_percent": 52.0, "resets_at": reset_7d.isoformat()}
        w_7d_cur = {"name": "seven_day", "remaining_percent": 46.0, "used_percent": 54.0, "resets_at": reset_7d.isoformat()}

        s1 = make_snapshot("claude", "A", remaining=90.0, last_updated=t1, resets_at=reset_5h, extra_windows=[w_7d_1])
        s2 = make_snapshot("claude", "A", remaining=70.0, last_updated=t2, resets_at=reset_5h, extra_windows=[w_7d_2])
        cur = make_snapshot("claude", "A", remaining=50.0, last_updated=NOW, resets_at=reset_5h, extra_windows=[w_7d_cur])

        self.store.append_snapshots([s1, s2], now=NOW)
        fc = forecast_account(cur, history=self.store.get_account_history("claude", "A"), now=NOW)

        fc_5h = next(w for w in fc.windows if w.window_name == "five_hour")
        fc_7d = next(w for w in fc.windows if w.window_name == "seven_day")

        # 5h window: 90% -> 70% -> 50% over 2h => 20.0%/h
        self.assertAlmostEqual(fc_5h.burn_rate_pct_per_hour, 20.0, places=1)
        # 7d window: 50% -> 48% -> 46% over 2h => 2.0%/h
        self.assertAlmostEqual(fc_7d.burn_rate_pct_per_hour, 2.0, places=1)

    # 3. Duplicate snapshot deduplication
    def test_duplicate_snapshot_deduplication(self):
        t1 = NOW - timedelta(hours=1)
        s1 = make_snapshot("claude", "A", remaining=80.0, last_updated=t1)
        s1_dup = make_snapshot("claude", "A", remaining=80.0, last_updated=t1)

        self.store.append_snapshot(s1, now=NOW)
        self.store.append_snapshot(s1_dup, now=NOW)

        hist = self.store.get_account_history("claude", "A")
        self.assertEqual(len(hist), 1)

    # 4. Out-of-order snapshot handling
    def test_out_of_order_snapshot_sorting(self):
        t1 = NOW - timedelta(hours=3)
        t2 = NOW - timedelta(hours=2)
        t3 = NOW - timedelta(hours=1)

        s1 = make_snapshot("claude", "A", remaining=90.0, last_updated=t1)
        s2 = make_snapshot("claude", "A", remaining=80.0, last_updated=t2)
        s3 = make_snapshot("claude", "A", remaining=70.0, last_updated=t3)

        # Append in reversed order: t3, t1, t2
        self.store.append_snapshot(s3, now=NOW)
        self.store.append_snapshot(s1, now=NOW)
        self.store.append_snapshot(s2, now=NOW)

        hist = self.store.get_account_history("claude", "A")
        timestamps = [s["observed_at"] for s in hist]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual([s["windows"][0]["remaining_percent"] for s in hist], [90.0, 80.0, 70.0])

    # 5. Retention pruning (bounded count and time window)
    def test_bounded_retention_pruning_max_count(self):
        # max_snapshots_per_series is 5 in setUp
        for i in range(10):
            t = NOW - timedelta(minutes=10 * (10 - i))
            s = make_snapshot("claude", "A", remaining=100.0 - i * 5, last_updated=t)
            self.store.append_snapshot(s, now=NOW)

        hist = self.store.get_account_history("claude", "A")
        self.assertEqual(len(hist), 5)
        # Should keep the newest 5 snapshots (i=5, 6, 7, 8, 9)
        self.assertEqual(hist[-1]["windows"][0]["remaining_percent"], 55.0)
        self.assertEqual(hist[0]["windows"][0]["remaining_percent"], 75.0)

    def test_bounded_retention_pruning_time_window(self):
        old_time = NOW - timedelta(hours=30)  # max_retention_hours is 24.0
        fresh_time = NOW - timedelta(hours=2)

        s_old = make_snapshot("claude", "A", remaining=100.0, last_updated=old_time)
        s_fresh = make_snapshot("claude", "A", remaining=80.0, last_updated=fresh_time)

        self.store.append_snapshots([s_old, s_fresh], now=NOW)
        hist = self.store.get_account_history("claude", "A")
        self.assertEqual(len(hist), 1)
        self.assertEqual(parse_iso_time(hist[0]["observed_at"]), fresh_time)

    # Multi-window retention co-existence
    def test_multi_window_snapshots_coexist_without_crowding(self):
        reset_5h = NOW + timedelta(hours=3)
        reset_7d = NOW + timedelta(days=5)

        for i in range(5):
            t = NOW - timedelta(minutes=15 * (5 - i))
            w_7d = {"name": "seven_day", "remaining_percent": 90.0 - i * 1.0, "used_percent": 10.0 + i * 1.0, "resets_at": reset_7d.isoformat()}
            s = make_snapshot("claude", "A", remaining=80.0 - i * 5.0, last_updated=t, resets_at=reset_5h, extra_windows=[w_7d])
            self.store.append_snapshot(s, now=NOW)

        # Retrieve both windows from history
        h_5h = self.store.get_history(provider="claude", account_id="A", window_name="five_hour")
        h_7d = self.store.get_history(provider="claude", account_id="A", window_name="seven_day")

        self.assertEqual(len(h_5h), 5)
        self.assertEqual(len(h_7d), 5)

    # 6. Reset boundary: old cycle does not pollute active cycle burn rate
    def test_reset_boundary_cycle_isolation(self):
        reset_old = NOW - timedelta(minutes=30)
        reset_new = NOW + timedelta(hours=4)

        # Pre-reset cycle (exhausted down to 5%)
        s1 = make_snapshot("claude", "A", remaining=50.0, last_updated=NOW - timedelta(hours=2), resets_at=reset_old)
        s2 = make_snapshot("claude", "A", remaining=5.0, last_updated=NOW - timedelta(hours=1), resets_at=reset_old)

        # Post-reset cycle (replenished to 100% -> 90%)
        s3 = make_snapshot("claude", "A", remaining=100.0, last_updated=NOW - timedelta(minutes=20), resets_at=reset_new)
        cur = make_snapshot("claude", "A", remaining=90.0, last_updated=NOW, resets_at=reset_new)

        self.store.append_snapshots([s1, s2, s3], now=NOW)
        fc = forecast_account(cur, history=self.store.get_account_history("claude", "A"), now=NOW)

        # Burn rate must be calculated ONLY between s3 (100%) and cur (90%) over 20 min = 30%/h
        # Must NOT be polluted by s1/s2
        self.assertEqual(fc.windows[0].burn_rate_samples, 2)
        self.assertAlmostEqual(fc.windows[0].burn_rate_pct_per_hour, 30.0, places=1)

    # 7. Corrupted / missing history file fail-safe
    def test_missing_history_file_returns_empty_safely(self):
        missing_path = Path(self.temp_dir.name) / "nonexistent.json"
        store = QuotaHistoryStore(missing_path, fail_safe=True)
        hist = store.get_history()
        self.assertEqual(hist, [])

    def test_corrupted_history_file_fail_safe(self):
        corrupt_path = Path(self.temp_dir.name) / "corrupt.json"
        corrupt_path.write_text("{ this is invalid json !!!", encoding="utf-8")
        store = QuotaHistoryStore(corrupt_path, fail_safe=True)
        hist = store.get_history()
        self.assertEqual(hist, [])
        # Append should still work and repair the file
        s = make_snapshot("claude", "A", remaining=80.0)
        self.assertTrue(store.append_snapshot(s, now=NOW))
        self.assertEqual(len(store.get_history()), 1)

    def test_invalid_document_save_fails_safe_without_overwriting(self):
        s = make_snapshot("claude", "A", remaining=80.0)
        self.store.append_snapshot(s, now=NOW)
        self.assertEqual(len(self.store.get_history()), 1)

        # Attempt to save schema-invalid document with fail_safe=True
        invalid_doc = {"invalid": True}
        self.store.save(invalid_doc)

        # Verify old valid content was NOT overwritten
        loaded = self.store.load()
        self.assertEqual(len(loaded.get("snapshots", [])), 1)

    # 8. First snapshot insufficient history
    def test_first_snapshot_insufficient_history(self):
        reset_time = NOW + timedelta(hours=4)
        cur = make_snapshot("claude", "A", remaining=80.0, last_updated=NOW, resets_at=reset_time)
        fc = forecast_account(cur, history=[], now=NOW)
        self.assertIsNone(fc.windows[0].burn_rate_pct_per_hour)
        self.assertEqual(fc.windows[0].burn_rate_samples, 1)
        self.assertIn("Insufficient history", fc.windows[0].warning_reason)

    # 9. Second valid snapshot starts producing burn rate
    def test_second_snapshot_produces_burn_rate(self):
        reset_time = NOW + timedelta(hours=4)
        t1 = NOW - timedelta(hours=1)
        s1 = make_snapshot("claude", "A", remaining=90.0, last_updated=t1, resets_at=reset_time)
        cur = make_snapshot("claude", "A", remaining=80.0, last_updated=NOW, resets_at=reset_time)

        self.store.append_snapshot(s1, now=NOW)
        fc = forecast_account(cur, history=self.store.get_history(), now=NOW)

        self.assertEqual(fc.windows[0].burn_rate_samples, 2)
        self.assertAlmostEqual(fc.windows[0].burn_rate_pct_per_hour, 10.0, places=1)

    # 10. Sanitization privacy guarantee: only quota telemetry is stored
    def test_sanitization_privacy_protection(self):
        raw = {
            "provider": "claude",
            "account_id": "A",
            "last_updated": NOW.isoformat(),
            "windows": [{"name": "five_hour", "remaining_percent": 75.0, "used_percent": 25.0, "resets_at": None}],
            # Forbidden private fields:
            "prompt": "Write a secure password manager in Rust",
            "conversation": [{"role": "user", "content": "secret data"}],
            "tokens": {"input": 12345, "output": 678},
            "token": "sk-ant-api03-SECRET",
            "task_content": "Internal proprietary business code",
        }
        sanitized = sanitize_snapshot(raw)
        self.assertIsNotNone(sanitized)
        self.assertNotIn("prompt", sanitized)
        self.assertNotIn("conversation", sanitized)
        self.assertNotIn("tokens", sanitized)
        self.assertNotIn("token", sanitized)
        self.assertNotIn("task_content", sanitized)
        self.assertEqual(set(sanitized.keys()), {
            "provider", "account_id", "observed_at", "last_updated",
            "source", "source_type", "confidence", "status", "windows",
        })


if __name__ == "__main__":
    unittest.main()
