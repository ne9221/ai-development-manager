import unittest
from datetime import datetime, timedelta, timezone

from manager.dashboard_core import (
    build_account_quota_card_vm,
    build_daily_brief_vm,
    build_execution_truth,
    build_progress_truth,
    build_quota_truth,
    classify_dashboard_lifecycle,
)
from manager.quota_forecast import AccountQuotaForecast, QuotaWindowForecast, RiskStatus
from manager.quota_reader import summarize_history


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def quota_vm(account_id="A", remaining=50.0, stale=False):
    window = QuotaWindowForecast(
        window_name="five_hour",
        remaining_percent=remaining,
        used_percent=None if remaining is None else 100.0 - remaining,
        resets_at=iso(NOW + timedelta(hours=2)),
        stale=stale,
        freshness="stale" if stale else "fresh",
        risk_status=RiskStatus.UNKNOWN if remaining is None or stale else RiskStatus.HEALTHY,
    )
    forecast = AccountQuotaForecast(
        provider="claude",
        account_id=account_id,
        display_name=f"Claude {account_id}",
        status="ok",
        last_updated=iso(NOW - timedelta(minutes=1)),
        stale=stale,
        freshness="stale" if stale else "fresh",
        source="claude_code_statusline_rate_limits",
        source_type="official",
        confidence="official",
        has_reliable_quota=not stale and remaining is not None,
        source_reliable=True,
        source_verified=True,
        windows=[window],
        primary_window=window,
        overall_risk_status=window.risk_status,
        dispatchable=not stale and remaining is not None and remaining > 0,
    )
    return build_account_quota_card_vm(forecast)


class DashboardDataTruthTests(unittest.TestCase):
    def test_unknown_quota_never_becomes_numeric_zero(self):
        quota = build_quota_truth([quota_vm(remaining=None)], "claude", "A")
        self.assertIsNone(quota["remaining"]["five_hour"])
        self.assertIsNone(quota["five_hour_remaining_pct"])
        self.assertEqual("unknown", quota["status"])

    def test_stale_quota_remains_distinguishable(self):
        quota = build_quota_truth([quota_vm(stale=True)], "claude", "A")
        self.assertEqual("stale", quota["status"])
        self.assertEqual("stale", quota["freshness_state"])
        self.assertEqual("STALE", quota["freshness"])

    def test_claude_accounts_remain_separate(self):
        accounts = [quota_vm("A", 90.0), quota_vm("B", 10.0)]
        self.assertEqual(90.0, build_quota_truth(accounts, "claude", "A")["remaining"]["five_hour"])
        self.assertEqual(10.0, build_quota_truth(accounts, "claude", "B")["remaining"]["five_hour"])

    def test_known_provider_can_match_the_canonical_default_account_key(self):
        account = quota_vm(None, 80.0)
        quota = build_quota_truth([account], "claude", None)
        self.assertTrue(quota["found"])
        self.assertEqual(80.0, quota["remaining"]["five_hour"])

    def test_execution_truth_preserves_identity_and_derives_elapsed_from_timestamps(self):
        started = NOW - timedelta(minutes=7, seconds=30)
        truth = build_execution_truth(
            {"project_id": "p1", "task_id": "t1", "assigned_provider": "claude", "account_id": "A", "mode": "code"},
            {"provider": "claude", "account_id": "A", "model": "sonnet", "mode": "code"},
            {"execution_id": "e1", "project_id": "p1", "task_id": "t1", "status": "running",
             "provider_session_id": "provider-s1", "started_at": iso(started),
             "heartbeat_at": iso(NOW), "last_provider_event": "turn_started"},
            NOW,
        )
        self.assertEqual({"provider": "claude", "account_id": "A", "model": "sonnet", "mode": "code"},
                         {key: truth[key] for key in ("provider", "account_id", "model", "mode")})
        self.assertEqual("running", truth["current_lifecycle_state"])
        self.assertEqual(450.0, truth["elapsed_runtime_seconds"])
        self.assertEqual("turn_started", truth["last_real_event"])
        self.assertEqual("e1", truth["execution_id"])
        self.assertEqual("provider-s1", truth["session_id"])

    def test_waiting_reason_and_human_action_are_preserved(self):
        execution = {"status": "running", "current_lifecycle_state": "waiting_quota",
                     "waiting_since": "2026-08-27T11:50:00Z", "waiting_reason": "provider quota reset",
                     "human_action_required": False}
        self.assertEqual("waiting_quota", classify_dashboard_lifecycle({}, {}, execution))
        truth = build_execution_truth({}, {}, execution, NOW)
        self.assertEqual("provider quota reset", truth["waiting_reason"])
        self.assertFalse(truth["human_action_required"])

    def test_polling_marker_does_not_replace_the_last_real_event(self):
        truth = build_execution_truth(
            {}, {}, {"status": "running", "last_real_event": "turn_started", "last_provider_event": "heartbeat"}, NOW,
        )
        self.assertEqual("turn_started", truth["last_real_event"])

    def test_progress_exposes_explicit_values_without_inventing_percentages(self):
        progress = build_progress_truth(
            {"current_phase": "M2", "overall_project_progress": "Two tasks complete"},
            {"current_progress": "Implementing read model", "status": "in_progress", "milestone_progress": {"completed": 2, "total": 4}},
        )
        self.assertEqual("Implementing read model", progress["task"]["value"])
        self.assertIsNone(progress["task"]["percent"])
        self.assertEqual((2, 4), (progress["milestone"]["completed"], progress["milestone"]["total"]))
        self.assertIsNone(progress["milestone"]["percent"])
        self.assertEqual("Two tasks complete", progress["project"]["value"])

    def test_history_summary_keeps_newest_account_snapshot(self):
        summary = summarize_history([
            {"provider": "claude", "account_id": "A", "last_updated": "2026-08-27T10:00:00Z", "status": "ok",
             "source_type": "official", "confidence": "official", "windows": [{"name": "five_hour", "remaining_percent": 80.0}]},
            {"provider": "claude", "account_id": "A", "last_updated": "2026-08-27T10:59:00Z", "status": "ok",
             "source_type": "official", "confidence": "official", "windows": [{"name": "five_hour", "remaining_percent": 70.0}]},
        ], now=NOW, max_age_minutes=60)
        self.assertEqual(70.0, summary["accounts"][0]["windows"][0]["remaining_percent"])
        self.assertTrue(summary["accounts"][0]["stale"])

    def test_daily_brief_uses_last_history_snapshot_when_live_summary_is_empty(self):
        brief = build_daily_brief_vm(
            {"accounts": []},
            history=[{"provider": "claude", "account_id": "A", "last_updated": "2026-08-27T10:59:00Z",
                      "status": "ok", "source_type": "official", "confidence": "official",
                      "windows": [{"name": "five_hour", "remaining_percent": 70.0}]}],
            now=NOW,
            max_age_minutes=60,
        )
        self.assertEqual(["A"], [account.account_id for account in brief.accounts])
        self.assertTrue(brief.accounts[0].stale)


if __name__ == "__main__":
    unittest.main()
