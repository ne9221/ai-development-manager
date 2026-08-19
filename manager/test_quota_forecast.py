"""Unit tests for manager.quota_forecast module."""

import unittest
from datetime import datetime, timedelta, timezone

from manager.quota_forecast import (
    ActionRecommendation,
    DailyBriefForecast,
    QuotaWindowForecast,
    RiskStatus,
    WarningLevel,
    calculate_window_burn_rate,
    forecast_account,
    forecast_daily_brief,
    forecast_to_dict,
    forecast_window,
    parse_iso_time,
    score_account_forecast,
)


NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def make_window(name="five_hour", remaining=80.0, used=20.0, resets_at=None, duration=300):
    return {
        "name": name,
        "duration_minutes": duration,
        "remaining_percent": remaining,
        "used_percent": used,
        "resets_at": resets_at.isoformat() if isinstance(resets_at, datetime) else resets_at,
    }


def make_account_item(
    provider="claude",
    account_id="A",
    display_name="Claude Code",
    windows=None,
    last_updated=NOW,
    source="claude_code_statusline_rate_limits",
    source_type="official",
    confidence="official",
    status="ok",
    stale=False,
    metadata=None,
):
    if windows is None:
        windows = [make_window("five_hour", 80.0, 20.0, resets_at=NOW + timedelta(hours=3))]
    item = {
        "provider": provider,
        "account_id": account_id,
        "display_name": display_name,
        "collection_mode": "automatic",
        "source": source,
        "source_type": source_type,
        "confidence": confidence,
        "status": status,
        "stale": stale,
        "last_updated": last_updated.isoformat() if isinstance(last_updated, datetime) else last_updated,
        "windows": windows,
    }
    if metadata is not None:
        item["metadata"] = metadata
    return item


class QuotaForecastCoreTests(unittest.TestCase):
    """Test pure functions in manager.quota_forecast."""

    # 1. Exact account A
    def test_exact_account_a(self):
        item = make_account_item("claude", "account-a")
        res = forecast_account(item, now=NOW)
        self.assertEqual(res.provider, "claude")
        self.assertEqual(res.account_id, "account-a")

    # 2. Exact account B
    def test_exact_account_b(self):
        item = make_account_item("claude", "account-b")
        res = forecast_account(item, now=NOW)
        self.assertEqual(res.provider, "claude")
        self.assertEqual(res.account_id, "account-b")

    # 3. account_id=None legacy
    def test_legacy_none_account(self):
        item = make_account_item("claude", None)
        res = forecast_account(item, now=NOW)
        self.assertIsNone(res.account_id)
        self.assertEqual(res.provider, "claude")

    # 4. remaining_percent=None
    def test_remaining_percent_none_is_unknown(self):
        w = make_window("five_hour", remaining=None, resets_at=NOW + timedelta(hours=2))
        item = make_account_item("claude", "A", windows=[w])
        res = forecast_account(item, now=NOW)
        self.assertEqual(res.windows[0].warning_level, WarningLevel.UNKNOWN)
        self.assertEqual(res.windows[0].risk_status, RiskStatus.UNKNOWN)
        self.assertIsNone(res.windows[0].remaining_percent)
        # MUST NOT treat None as 0%
        self.assertNotEqual(res.windows[0].remaining_percent, 0)

    # 5. windows=[]
    def test_empty_windows(self):
        item = make_account_item("claude", "A", windows=[])
        res = forecast_account(item, now=NOW)
        self.assertEqual(len(res.windows), 0)
        self.assertEqual(res.overall_warning_level, WarningLevel.UNKNOWN)

    # 6. resets_at=None
    def test_resets_at_none_is_unknown(self):
        w = make_window("five_hour", remaining=50.0, resets_at=None)
        item = make_account_item("claude", "A", windows=[w])
        history = [make_account_item("claude", "A", windows=[make_window("five_hour", 70.0, resets_at=None)], last_updated=NOW - timedelta(hours=1))]
        res = forecast_account(item, history=history, now=NOW)
        self.assertEqual(res.windows[0].warning_level, WarningLevel.UNKNOWN)
        self.assertIsNone(res.windows[0].resets_at)

    # 7. invalid timestamp
    def test_invalid_timestamp_handled_gracefully(self):
        self.assertIsNone(parse_iso_time("invalid-timestamp"))
        self.assertIsNone(parse_iso_time(None))
        self.assertIsNone(parse_iso_time(12345))
        item = make_account_item("claude", "A", last_updated="invalid-time")
        res = forecast_account(item, now=NOW)
        self.assertTrue(res.stale)
        self.assertEqual(res.overall_warning_level, WarningLevel.UNKNOWN)

    # 8. stale snapshot
    def test_stale_snapshot_warning_is_unknown(self):
        stale_time = NOW - timedelta(hours=2)  # older than 60m
        item = make_account_item("claude", "A", last_updated=stale_time, stale=True)
        res = forecast_account(item, now=NOW, max_age_minutes=60)
        self.assertTrue(res.stale)
        self.assertEqual(res.overall_warning_level, WarningLevel.UNKNOWN)
        self.assertEqual(res.overall_action_recommendation, ActionRecommendation.HOLD)

    # 9. only 1 sample
    def test_single_sample_burn_rate_unknown(self):
        item = make_account_item("claude", "A")
        rate, samples, _ = calculate_window_burn_rate("five_hour", item, history=[], now=NOW)
        self.assertIsNone(rate)
        self.assertEqual(samples, 1)

    # 10. normal 2-point burn rate
    def test_two_point_burn_rate(self):
        reset_time = NOW + timedelta(hours=4)
        t0 = NOW - timedelta(hours=2)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 80.0, resets_at=reset_time)], last_updated=t0)
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 40.0, resets_at=reset_time)], last_updated=NOW)
        rate, samples, _ = calculate_window_burn_rate("five_hour", current, history=[h0], now=NOW)
        # 40% consumed over 2 hours -> 20%/hr
        self.assertAlmostEqual(rate, 20.0, places=2)
        self.assertEqual(samples, 2)

    # 11. multi-point history
    def test_multi_point_history(self):
        reset_time = NOW + timedelta(hours=5)
        h1 = make_account_item("claude", "A", windows=[make_window("five_hour", 90.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=3))
        h2 = make_account_item("claude", "A", windows=[make_window("five_hour", 70.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=2))
        h3 = make_account_item("claude", "A", windows=[make_window("five_hour", 50.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 30.0, resets_at=reset_time)], last_updated=NOW)
        rate, samples, _ = calculate_window_burn_rate("five_hour", current, history=[h1, h2, h3], now=NOW)
        # (90 - 30) / 3h = 20%/hr
        self.assertAlmostEqual(rate, 20.0, places=2)
        self.assertEqual(samples, 4)

    # 12. midway reset
    def test_midway_reset_boundary_isolated(self):
        old_reset = NOW - timedelta(hours=1)
        new_reset = NOW + timedelta(hours=4)
        # Sample before reset
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 10.0, resets_at=old_reset)], last_updated=NOW - timedelta(hours=2))
        # Samples after reset
        h1 = make_account_item("claude", "A", windows=[make_window("five_hour", 100.0, resets_at=new_reset)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 80.0, resets_at=new_reset)], last_updated=NOW)
        rate, samples, _ = calculate_window_burn_rate("five_hour", current, history=[h0, h1], now=NOW)
        # Should only use [h1, current]: (100 - 80) / 1h = 20%/hr, NOT (10 - 80) / 2h
        self.assertAlmostEqual(rate, 20.0, places=2)
        self.assertEqual(samples, 2)

    # 13. remaining increases (replenishment without timestamp change)
    def test_remaining_increase_not_negative_burn(self):
        reset_time = NOW + timedelta(hours=4)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 20.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 50.0, resets_at=reset_time)], last_updated=NOW)
        rate, samples, _ = calculate_window_burn_rate("five_hour", current, history=[h0], now=NOW)
        # Quota jumped up from 20% to 50% -> replenishment boundary -> only current sample left
        self.assertIsNone(rate)
        self.assertEqual(samples, 1)

    # 14. remaining = 0
    def test_remaining_zero_exhausted(self):
        reset_time = NOW + timedelta(hours=2)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 20.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 0.0, resets_at=reset_time)], last_updated=NOW)
        res = forecast_account(current, history=[h0], now=NOW)
        w = res.windows[0]
        self.assertEqual(w.remaining_percent, 0.0)
        self.assertEqual(w.risk_status, RiskStatus.EXHAUSTED)
        self.assertEqual(w.warning_level, WarningLevel.NORMAL)

    # 15. remaining = 100
    def test_remaining_hundred_full(self):
        reset_time = NOW + timedelta(hours=4)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 100.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 100.0, resets_at=reset_time)], last_updated=NOW)
        res = forecast_account(current, history=[h0], now=NOW)
        w = res.windows[0]
        self.assertEqual(w.remaining_percent, 100.0)
        self.assertEqual(w.burn_rate_pct_per_hour, 0.0)
        # Burn rate is 0%, reset in 4h -> remaining at reset will be 100% (>20%) -> WARNING
        self.assertEqual(w.warning_level, WarningLevel.WARNING)
        self.assertEqual(w.risk_status, RiskStatus.CONSUME_FASTER)

    # 16. burn_rate = 0 (idle)
    def test_burn_rate_zero(self):
        reset_time = NOW + timedelta(hours=1)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 50.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 50.0, resets_at=reset_time)], last_updated=NOW)
        res = forecast_account(current, history=[h0], now=NOW)
        w = res.windows[0]
        self.assertEqual(w.burn_rate_pct_per_hour, 0.0)
        # Reset in 1h (<=2h) with 50% remaining (>10%) -> URGENT
        self.assertEqual(w.warning_level, WarningLevel.URGENT)
        self.assertEqual(w.action_recommendation, ActionRecommendation.URGENT_CONSUME)

    # 17. reset already passed
    def test_past_reset_is_unknown(self):
        past_reset = NOW - timedelta(minutes=10)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 50.0, resets_at=past_reset)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 30.0, resets_at=past_reset)], last_updated=NOW)
        res = forecast_account(current, history=[h0], now=NOW)
        w = res.windows[0]
        self.assertEqual(w.warning_level, WarningLevel.UNKNOWN)
        self.assertIn("past", w.warning_reason.lower())

    # 18. 5h and weekly windows strictly isolated
    def test_five_hour_and_weekly_strict_isolation(self):
        reset_5h = NOW + timedelta(hours=3)
        reset_week = NOW + timedelta(days=5)

        w_5h_0 = make_window("five_hour", remaining=80.0, resets_at=reset_5h, duration=300)
        w_week_0 = make_window("seven_day", remaining=90.0, resets_at=reset_week, duration=10080)
        h0 = make_account_item("claude", "A", windows=[w_5h_0, w_week_0], last_updated=NOW - timedelta(hours=2))

        w_5h_1 = make_window("five_hour", remaining=20.0, resets_at=reset_5h, duration=300)  # 60% used in 2h -> 30%/hr
        w_week_1 = make_window("seven_day", remaining=88.0, resets_at=reset_week, duration=10080)  # 2% used in 2h -> 1%/hr
        current = make_account_item("claude", "A", windows=[w_5h_1, w_week_1], last_updated=NOW)

        res = forecast_account(current, history=[h0], now=NOW)
        fw_5h = next(w for w in res.windows if w.window_name == "five_hour")
        fw_week = next(w for w in res.windows if w.window_name == "seven_day")

        self.assertAlmostEqual(fw_5h.burn_rate_pct_per_hour, 30.0, places=1)
        self.assertAlmostEqual(fw_week.burn_rate_pct_per_hour, 1.0, places=1)

        # 5h window: remaining 20%, burn 30%/h -> exhausts in ~0.67h (<3h reset) -> likely exhaust before reset
        self.assertEqual(fw_5h.risk_status, RiskStatus.LIKELY_EXHAUST_BEFORE_RESET)

        # weekly window: remaining 88%, burn 1%/h -> in 120h (5 days) uses 120%, exhausts in 88h (<120h)
        self.assertAlmostEqual(fw_week.estimated_hours_to_exhaustion, 88.0, places=1)

    # 19. duplicate snapshots
    def test_duplicate_snapshots_handled(self):
        reset_time = NOW + timedelta(hours=3)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 60.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        h0_dup = make_account_item("claude", "A", windows=[make_window("five_hour", 60.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 40.0, resets_at=reset_time)], last_updated=NOW)
        rate, samples, _ = calculate_window_burn_rate("five_hour", current, history=[h0, h0_dup], now=NOW)
        self.assertAlmostEqual(rate, 20.0, places=2)
        self.assertEqual(samples, 2)

    # 20. out-of-order history
    def test_out_of_order_history_sorted(self):
        reset_time = NOW + timedelta(hours=4)
        h1 = make_account_item("claude", "A", windows=[make_window("five_hour", 80.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=2))
        h2 = make_account_item("claude", "A", windows=[make_window("five_hour", 60.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 40.0, resets_at=reset_time)], last_updated=NOW)
        # Pass h2 before h1
        rate, samples, _ = calculate_window_burn_rate("five_hour", current, history=[h2, h1], now=NOW)
        self.assertAlmostEqual(rate, 20.0, places=2)
        self.assertEqual(samples, 3)

    # Cross-Account Isolation Test: Account A vs Account B
    def test_cross_account_isolation(self):
        reset_time = NOW + timedelta(hours=4)
        h_a = make_account_item("claude", "A", windows=[make_window("five_hour", 90.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=2))
        h_b = make_account_item("claude", "B", windows=[make_window("five_hour", 30.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=2))

        cur_a = make_account_item("claude", "A", windows=[make_window("five_hour", 70.0, resets_at=reset_time)], last_updated=NOW)
        cur_b = make_account_item("claude", "B", windows=[make_window("five_hour", 10.0, resets_at=reset_time)], last_updated=NOW)

        # Pass mixed history to daily brief
        brief = forecast_daily_brief([cur_a, cur_b], history=[h_a, h_b], now=NOW)
        res_a = next(a for a in brief.accounts if a.account_id == "A")
        res_b = next(a for a in brief.accounts if a.account_id == "B")

        self.assertAlmostEqual(res_a.windows[0].burn_rate_pct_per_hour, 10.0, places=1)
        self.assertAlmostEqual(res_b.windows[0].burn_rate_pct_per_hour, 10.0, places=1)
        self.assertEqual(res_a.windows[0].remaining_percent, 70.0)
        self.assertEqual(res_b.windows[0].remaining_percent, 10.0)

    # Daily Brief Core Rankings & Summaries
    def test_daily_brief_rankings_and_recommendations(self):
        reset_soon = NOW + timedelta(hours=1)
        reset_later = NOW + timedelta(hours=5)

        # Account 1: High remaining, resetting soon -> Urgent consume
        a1_h = make_account_item("claude", "A1", windows=[make_window("five_hour", 90.0, resets_at=reset_soon)], last_updated=NOW - timedelta(hours=1))
        a1_c = make_account_item("claude", "A1", windows=[make_window("five_hour", 80.0, resets_at=reset_soon)], last_updated=NOW)

        # Account 2: Low remaining, resetting later -> Conserve
        a2_h = make_account_item("claude", "A2", windows=[make_window("five_hour", 30.0, resets_at=reset_later)], last_updated=NOW - timedelta(hours=1))
        a2_c = make_account_item("claude", "A2", windows=[make_window("five_hour", 10.0, resets_at=reset_later)], last_updated=NOW)

        # Account 3: Stale / Unknown -> Hold
        a3_c = make_account_item("claude", "A3", windows=[make_window("five_hour", None, resets_at=reset_later)], stale=True, last_updated=NOW - timedelta(hours=3))

        brief = forecast_daily_brief([a1_c, a2_c, a3_c], history=[a1_h, a2_h], now=NOW)

        # Highest remaining accounts (dispatchable)
        self.assertEqual(len(brief.highest_remaining_accounts), 2)
        self.assertEqual(brief.highest_remaining_accounts[0].account_id, "A1")

        # Recommended consume accounts
        consume_ids = [a.account_id for a in brief.recommended_consume_accounts]
        self.assertIn("A1", consume_ids)

        # Conserve accounts
        conserve_ids = [a.account_id for a in brief.conserve_accounts]
        self.assertIn("A2", conserve_ids)

        # Hold accounts
        hold_ids = [a.account_id for a in brief.hold_or_unreliable_accounts]
        self.assertIn("A3", hold_ids)

    # JSON Adapter serialization test
    def test_forecast_to_dict_adapter(self):
        item = make_account_item("claude", "A")
        forecast = forecast_account(item, now=NOW)
        d = forecast_to_dict(forecast)
        self.assertIsInstance(d, dict)
        self.assertEqual(d["provider"], "claude")
        self.assertEqual(d["account_id"], "A")
        self.assertIsInstance(d["windows"], list)
        self.assertEqual(d["overall_warning_level"], "UNKNOWN")

    # P2 Regression Test: ATTENTION + suggest consume must not output RiskStatus.CONSERVE
    def test_p2_note1_moderate_leftover_risk_is_consume_faster_not_conserve(self):
        reset_time = NOW + timedelta(hours=4)
        h0 = make_account_item("claude", "A", windows=[make_window("five_hour", 65.0, resets_at=reset_time)], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[make_window("five_hour", 55.0, resets_at=reset_time)], last_updated=NOW)
        # burn rate: 10%/h. Reset in 4h -> burns 40%. est_remaining_at_reset = 55 - 40 = 15.0% (> 10.0 and <= 20.0).
        fc = forecast_account(current, history=[h0], now=NOW)
        w = fc.windows[0]
        self.assertEqual(w.warning_level, WarningLevel.ATTENTION)
        self.assertEqual(w.action_recommendation, ActionRecommendation.SUGGEST_CONSUME)
        self.assertEqual(w.risk_status, RiskStatus.CONSUME_FASTER)
        self.assertNotEqual(w.risk_status, RiskStatus.CONSERVE)

    # Multi-window protection test: weekly CONSERVE vetoes five_hour SUGGEST_CONSUME
    def test_multi_window_conserve_veto_overrides_five_hour_suggest_consume(self):
        reset_5h = NOW + timedelta(hours=1)
        reset_week = NOW + timedelta(days=2)
        # 5h window has 80% remaining, resets in 1h -> SUGGEST_CONSUME
        w_5h_0 = make_window("five_hour", remaining=90.0, resets_at=reset_5h, duration=300)
        w_5h_1 = make_window("five_hour", remaining=80.0, resets_at=reset_5h, duration=300)
        # 7d window has 20% remaining, burning at 2%/h, resets in 48h -> exhausts in 10h (<48h) -> CONSERVE
        w_7d_0 = make_window("seven_day", remaining=22.0, resets_at=reset_week, duration=10080)
        w_7d_1 = make_window("seven_day", remaining=20.0, resets_at=reset_week, duration=10080)

        h0 = make_account_item("claude", "A", windows=[w_5h_0, w_7d_0], last_updated=NOW - timedelta(hours=1))
        current = make_account_item("claude", "A", windows=[w_5h_1, w_7d_1], last_updated=NOW)

        fc = forecast_account(current, history=[h0], now=NOW)
        # Primary window (5h) by itself is SUGGEST_CONSUME
        fw_5h = next(w for w in fc.windows if w.window_name == "five_hour")
        self.assertEqual(fw_5h.action_recommendation, ActionRecommendation.URGENT_CONSUME)
        # But overall account action is CONSERVE due to 7d window protection
        self.assertEqual(fc.overall_action_recommendation, ActionRecommendation.CONSERVE)
        self.assertEqual(fc.overall_risk_status, RiskStatus.LIKELY_EXHAUST_BEFORE_RESET)
        self.assertIn("Multi-window protection", fc.overall_warning_reason)

    # Multi-window protection test: exhausted window (0%) sets overall EXHAUSTED / HOLD / dispatchable=False
    def test_multi_window_exhausted_window_overrides_to_hold(self):
        reset_5h = NOW + timedelta(hours=2)
        reset_week = NOW + timedelta(days=2)
        w_5h = make_window("five_hour", remaining=80.0, resets_at=reset_5h, duration=300)
        w_7d = make_window("seven_day", remaining=0.0, resets_at=reset_week, duration=10080)
        current = make_account_item("claude", "A", windows=[w_5h, w_7d], last_updated=NOW)
        fc = forecast_account(current, now=NOW)
        self.assertEqual(fc.overall_risk_status, RiskStatus.EXHAUSTED)
        self.assertEqual(fc.overall_action_recommendation, ActionRecommendation.HOLD)
        self.assertFalse(fc.dispatchable)

    # =====================================================================
    # Codex effective-availability truth: primary subscription quota exhausted
    # (0% remaining) is NOT the same fact as "provider unavailable" when the
    # provider itself reports a usable extra-credits balance.
    # =====================================================================

    def test_primary_zero_with_credits_available_is_dispatchable_via_credits(self):
        reset_time = NOW + timedelta(hours=6)
        current = make_account_item(
            "codex", "codex-1",
            windows=[make_window("primary", remaining=0.0, resets_at=reset_time, duration=10080)],
            last_updated=NOW,
            source="codex_app_server",
            metadata={"credits": {"hasCredits": True, "unlimited": False, "balance": "813.5882690000"}},
        )
        fc = forecast_account(current, now=NOW)

        self.assertEqual(fc.primary_window.remaining_percent, 0.0)
        self.assertEqual(fc.extra_credits_available, True)
        self.assertEqual(fc.extra_credits_balance, "813.5882690000")
        self.assertEqual(fc.overall_risk_status, RiskStatus.AVAILABLE_VIA_CREDITS)
        self.assertEqual(fc.overall_action_recommendation, ActionRecommendation.NORMAL_USE)
        self.assertTrue(fc.dispatchable, "account must be dispatchable via credits, not held")

        # And the shared dispatch-scoring function must treat it as eligible.
        is_eligible, action_tier, *_ = score_account_forecast(fc)
        self.assertTrue(is_eligible)
        self.assertGreater(action_tier, 0.0)

    def test_primary_zero_with_no_credits_is_unavailable(self):
        reset_time = NOW + timedelta(hours=6)
        current = make_account_item(
            "codex", "codex-1",
            windows=[make_window("primary", remaining=0.0, resets_at=reset_time, duration=10080)],
            last_updated=NOW,
            source="codex_app_server",
            metadata={"credits": {"hasCredits": False, "unlimited": False, "balance": "0"}},
        )
        fc = forecast_account(current, now=NOW)

        self.assertEqual(fc.extra_credits_available, False)
        self.assertEqual(fc.overall_risk_status, RiskStatus.EXHAUSTED)
        self.assertEqual(fc.overall_action_recommendation, ActionRecommendation.HOLD)
        self.assertFalse(fc.dispatchable)

        is_eligible, *_ = score_account_forecast(fc)
        self.assertFalse(is_eligible)

    def test_primary_zero_with_credits_metadata_absent_does_not_invent_availability(self):
        reset_time = NOW + timedelta(hours=6)
        current = make_account_item(
            "codex", "codex-1",
            windows=[make_window("primary", remaining=0.0, resets_at=reset_time, duration=10080)],
            last_updated=NOW,
            source="codex_app_server",
            # No metadata.credits at all -- must not fabricate availability.
        )
        fc = forecast_account(current, now=NOW)

        self.assertIsNone(fc.extra_credits_available)
        self.assertEqual(fc.overall_risk_status, RiskStatus.EXHAUSTED)
        self.assertEqual(fc.overall_action_recommendation, ActionRecommendation.HOLD)
        self.assertFalse(fc.dispatchable)

    def test_claude_stale_remains_unknown_not_zero(self):
        """A provider with no quota telemetry (e.g. Claude with no statusline
        data) must surface as stale/unknown, never silently treated as 0%."""
        current = make_account_item(
            "claude", "account-a",
            windows=[],
            last_updated=NOW - timedelta(hours=10),
        )
        fc = forecast_account(current, now=NOW)

        self.assertTrue(fc.stale)
        self.assertEqual(fc.overall_warning_level, WarningLevel.UNKNOWN)
        self.assertFalse(fc.dispatchable)
        self.assertIsNone(fc.extra_credits_available)

        is_eligible, *_ = score_account_forecast(fc)
        self.assertFalse(is_eligible)


if __name__ == "__main__":
    unittest.main()
