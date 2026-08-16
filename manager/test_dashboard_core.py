import unittest
from datetime import datetime, timedelta, timezone

from manager.dashboard_core import (
    parse_time,
    is_cleanup_confirmed,
    determine_execution_state,
    is_execution_stale,
    get_global_summary,
    map_task_board,
    format_countdown,
    format_percent,
    format_burn_rate,
    build_account_quota_card_vm,
    build_daily_brief_vm,
    AccountQuotaCardViewModel,
    DailyBriefViewModel
)
from manager.quota_forecast import (
    AccountQuotaForecast,
    QuotaWindowForecast,
    WarningLevel,
    RiskStatus,
    ActionRecommendation
)


class TestDashboardCore(unittest.TestCase):
    def test_parse_time(self):
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time("invalid-date"))

        t = parse_time("2026-08-15T02:34:26Z")
        self.assertIsNotNone(t)
        self.assertEqual(t.tzinfo, timezone.utc)
        self.assertEqual(t.year, 2026)

        # Test with offset representation
        t2 = parse_time("2026-08-15T02:34:26+00:00")
        self.assertEqual(t, t2)

    def test_is_cleanup_confirmed(self):
        # Read-only execution with non-required writer
        ro_exec = {
            "access": "read_only",
            "cleanup_evidence": {
                "task_claim_release": "released",
                "writer_release": "not_required"
            }
        }
        self.assertTrue(is_cleanup_confirmed(ro_exec))

        # Read-only execution with missing cleanup evidence
        self.assertFalse(is_cleanup_confirmed({"access": "read_only"}))

        # Production write execution with released writer lease
        write_exec = {
            "access": "production_write",
            "cleanup_evidence": {
                "task_claim_release": "released",
                "writer_release": "released"
            }
        }
        self.assertTrue(is_cleanup_confirmed(write_exec))

    def test_determine_execution_state(self):
        now = datetime(2026, 8, 15, 3, 0, 0, tzinfo=timezone.utc)

        # Terminal state with confirmed cleanup
        term_exec = {
            "status": "completed",
            "access": "read_only",
            "cleanup_evidence": {
                "task_claim_release": "released",
                "writer_release": "not_required"
            }
        }
        self.assertEqual(determine_execution_state(term_exec, now), "completed")

        # Terminal state but cleanup not confirmed
        term_exec_dirty = {
            "status": "completed",
            "access": "read_only"
        }
        self.assertEqual(determine_execution_state(term_exec_dirty, now), "finishing")

        # Running but no session linked (correlating)
        running_no_session = {
            "status": "running"
        }
        self.assertEqual(determine_execution_state(running_no_session, now), "correlating")

        # Running and linked, recent heartbeat
        running_linked = {
            "status": "running",
            "provider_session_id": "sess-1",
            "heartbeat_at": "2026-08-15T02:50:00Z"  # 10 mins ago
        }
        self.assertEqual(determine_execution_state(running_linked, now), "running")

        # Running and linked, old heartbeat (waiting)
        running_waiting = {
            "status": "running",
            "provider_session_id": "sess-1",
            "heartbeat_at": "2026-08-15T02:30:00Z"  # 30 mins ago
        }
        self.assertEqual(determine_execution_state(running_waiting, now), "waiting")

        # Claude: Running and linked, old heartbeat (remains running)
        claude_running = {
            "status": "running",
            "provider": "claude",
            "provider_session_id": "sess-2",
            "heartbeat_at": "2026-08-15T02:30:00Z"  # 30 mins ago
        }
        self.assertEqual(determine_execution_state(claude_running, now), "running")

    def test_is_execution_stale(self):
        now = datetime(2026, 8, 15, 3, 0, 0, tzinfo=timezone.utc)

        # Terminal execution is never stale
        term = {"status": "completed"}
        self.assertFalse(is_execution_stale(term, now))

        # Exceeded hard timeout
        exec_timeout = {
            "status": "running",
            "hard_timeout_at": "2026-08-15T02:59:00Z"
        }
        self.assertTrue(is_execution_stale(exec_timeout, now))

        # Heartbeat >= 15 mins (900 seconds)
        exec_old_hb = {
            "status": "running",
            "heartbeat_at": "2026-08-15T02:44:00Z"  # 16 mins ago
        }
        self.assertTrue(is_execution_stale(exec_old_hb, now))

        # Claude: Heartbeat >= 15 mins (not stale)
        claude_exec_old = {
            "status": "running",
            "provider": "Claude",
            "heartbeat_at": "2026-08-15T02:30:00Z"  # 30 mins ago
        }
        self.assertFalse(is_execution_stale(claude_exec_old, now))

        # Heartbeat < 15 mins
        exec_fresh = {
            "status": "running",
            "heartbeat_at": "2026-08-15T02:55:00Z"  # 5 mins ago
        }
        self.assertFalse(is_execution_stale(exec_fresh, now))

    def test_get_global_summary(self):
        providers = [
            {"provider": "codex", "has_reliable_quota": True},
            {"provider": "claude", "has_reliable_quota": False}
        ]
        tasks = [
            {"status": "in_progress"},
            {"status": "blocked"},
            {"status": "ready"}
        ]
        executions = [
            {"status": "running"}
        ]

        summary = get_global_summary(providers, tasks, executions)
        self.assertEqual(summary["running_tasks_count"], 1)
        self.assertEqual(summary["blocked_tasks_count"], 1)
        self.assertEqual(summary["active_sessions_count"], 1)
        self.assertEqual(summary["reliable_providers_count"], 1)

    def test_map_task_board(self):
        now = datetime(2026, 8, 15, 3, 0, 0, tzinfo=timezone.utc)
        tasks = [
            {"task_id": "t1", "project_id": "p1", "status": "ready"},
            {"task_id": "t2", "project_id": "p1", "status": "in_progress"},
            {"task_id": "t3", "project_id": "p1", "status": "blocked"},
            {"task_id": "t4", "project_id": "p1", "status": "completed"}
        ]

        board = map_task_board(tasks, {}, now)
        self.assertEqual(len(board["Ready"]), 1)
        self.assertEqual(len(board["In progress"]), 1)
        self.assertEqual(len(board["Blocked / Attention"]), 1)
        self.assertEqual(len(board["Completed"]), 1)
        self.assertEqual(board["Ready"][0]["task_id"], "t1")

    # =================================================================
    # Slice 4A ViewModel Unit Tests
    # =================================================================

    def test_formatting_helpers(self):
        # Format countdown
        self.assertEqual(format_countdown(None), "—")
        self.assertEqual(format_countdown(-1.0), "Past due")
        self.assertEqual(format_countdown(0.5), "in 30m")
        self.assertEqual(format_countdown(2.34), "in 2.3h")
        self.assertEqual(format_countdown(25.0), "in 1d 1h")
        self.assertEqual(format_countdown(48.0), "in 2d")

        # Format percent (None MUST NOT be 0%)
        self.assertEqual(format_percent(None), "Unknown")
        self.assertEqual(format_percent(0.0), "0.0%")
        self.assertEqual(format_percent(74.5), "74.5%")

        # Format burn rate
        self.assertEqual(format_burn_rate(None), "—")
        self.assertEqual(format_burn_rate(12.34), "12.3%/hr")

    def test_build_account_quota_card_vm_none_percent(self):
        fc = AccountQuotaForecast(
            provider="codex",
            account_id=None,
            display_name="Codex",
            status="ok",
            freshness="fresh",
            stale=False,
            confidence="official",
            source="codex_app_server",
            source_type="official",
            has_reliable_quota=True,
            windows=[
                QuotaWindowForecast(
                    window_name="primary",
                    remaining_percent=None,
                    used_percent=None,
                    hours_to_reset=None
                )
            ]
        )
        vm = build_account_quota_card_vm(fc)
        self.assertIsNone(vm.five_hour_remaining_pct)
        self.assertEqual(vm.formatted_five_hour_remaining, "Unknown")
        self.assertNotEqual(vm.formatted_five_hour_remaining, "0.0%")
        self.assertNotEqual(vm.formatted_five_hour_remaining, "0%")

    def test_build_account_quota_card_vm_zero_percent(self):
        fc = AccountQuotaForecast(
            provider="claude",
            account_id="A",
            display_name="Claude Code",
            status="ok",
            freshness="fresh",
            stale=False,
            confidence="official",
            source="claude_code_statusline_rate_limits",
            source_type="official",
            has_reliable_quota=True,
            windows=[
                QuotaWindowForecast(
                    window_name="five_hour",
                    remaining_percent=0.0,
                    used_percent=100.0,
                    hours_to_reset=1.5,
                    risk_status=RiskStatus.EXHAUSTED,
                    action_recommendation=ActionRecommendation.HOLD
                )
            ],
            overall_action_recommendation=ActionRecommendation.HOLD,
            overall_risk_status=RiskStatus.EXHAUSTED
        )
        vm = build_account_quota_card_vm(fc)
        self.assertEqual(vm.five_hour_remaining_pct, 0.0)
        self.assertEqual(vm.formatted_five_hour_remaining, "0.0%")
        self.assertEqual(vm.action_recommendation, "hold")
        self.assertEqual(vm.overall_risk, "exhausted")

    def test_build_daily_brief_vm_claude_ab_routing(self):
        now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
        doc = {
            "providers": [
                {
                    "provider": "claude",
                    "account_id": "account-a",
                    "display_name": "Claude Code (A)",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "official",
                    "status": "ok",
                    "last_updated": now.isoformat(),
                    "windows": [
                        {"name": "five_hour", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                        {"name": "seven_day", "remaining_percent": 10.0, "used_percent": 90.0, "resets_at": (now + timedelta(days=5)).isoformat()}
                    ]
                },
                {
                    "provider": "claude",
                    "account_id": "account-b",
                    "display_name": "Claude Code (B)",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "official",
                    "status": "ok",
                    "last_updated": now.isoformat(),
                    "windows": [
                        {"name": "five_hour", "remaining_percent": 74.0, "used_percent": 26.0, "resets_at": (now + timedelta(hours=2)).isoformat()},
                        {"name": "seven_day", "remaining_percent": 85.0, "used_percent": 15.0, "resets_at": (now + timedelta(days=6)).isoformat()}
                    ]
                }
            ]
        }
        history = [
            # Account A history: 7-day is burning fast and will exhaust in 2 hours (< 5 days)
            {
                "provider": "claude",
                "account_id": "account-a",
                "observed_at": (now - timedelta(hours=1)).isoformat(),
                "last_updated": (now - timedelta(hours=1)).isoformat(),
                "source": "claude_code_statusline_rate_limits",
                "source_type": "official",
                "confidence": "official",
                "status": "ok",
                "windows": [
                    {"name": "five_hour", "remaining_percent": 85.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                    {"name": "seven_day", "remaining_percent": 15.0, "resets_at": (now + timedelta(days=5)).isoformat()}
                ]
            },
            # Account B history: 5-hour has moderate usage with plenty of surplus projected at reset
            {
                "provider": "claude",
                "account_id": "account-b",
                "observed_at": (now - timedelta(hours=1)).isoformat(),
                "last_updated": (now - timedelta(hours=1)).isoformat(),
                "source": "claude_code_statusline_rate_limits",
                "source_type": "official",
                "confidence": "official",
                "status": "ok",
                "windows": [
                    {"name": "five_hour", "remaining_percent": 75.0, "resets_at": (now + timedelta(hours=2)).isoformat()},
                    {"name": "seven_day", "remaining_percent": 85.0, "resets_at": (now + timedelta(days=6)).isoformat()}
                ]
            }
        ]

        brief = build_daily_brief_vm(doc, history=history, now=now)
        # Account A has 7-day quota at 10% (conserve / multi-window protection)
        # Account B has fresh 5-hour quota (74%) and fresh weekly quota (85%)
        # So Account B should be the recommended account!
        self.assertEqual(brief.recommended_provider, "claude")
        self.assertEqual(brief.recommended_account, "account-b")
        self.assertIn("account-b", brief.recommended_display_name.lower())
        self.assertIn("conserve", brief.reason.lower())
        self.assertEqual(len(brief.accounts), 2)

        # Verify Account A and B are strictly isolated in view models
        acc_a_vm = next(a for a in brief.accounts if a.account_id == "account-a")
        acc_b_vm = next(a for a in brief.accounts if a.account_id == "account-b")
        self.assertEqual(acc_a_vm.five_hour_remaining_pct, 80.0)
        self.assertEqual(acc_a_vm.weekly_remaining_pct, 10.0)
        self.assertEqual(acc_a_vm.action_recommendation, "conserve")

        self.assertEqual(acc_b_vm.five_hour_remaining_pct, 74.0)
        self.assertEqual(acc_b_vm.weekly_remaining_pct, 85.0)
        self.assertEqual(acc_b_vm.action_recommendation, "urgent_consume")

    def test_build_daily_brief_vm_stale_telemetry(self):
        now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
        stale_time = now - timedelta(hours=5)
        doc = {
            "providers": [
                {
                    "provider": "claude",
                    "account_id": "account-a",
                    "display_name": "Claude Code (A)",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "official",
                    "status": "ok",
                    "last_updated": stale_time.isoformat(),
                    "windows": [
                        {"name": "five_hour", "remaining_percent": 90.0, "resets_at": (now + timedelta(hours=1)).isoformat()}
                    ]
                }
            ]
        }
        brief = build_daily_brief_vm(doc, now=now)
        self.assertEqual(brief.recommended_action, "hold")
        self.assertTrue(any("stale" in w.lower() for w in brief.telemetry_warnings))
        self.assertTrue(brief.accounts[0].stale)

    def test_insufficient_history_does_not_fabricate_burn_rate(self):
        now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
        doc = {
            "providers": [
                {
                    "provider": "codex",
                    "account_id": None,
                    "display_name": "Codex",
                    "source": "codex_app_server",
                    "source_type": "official",
                    "confidence": "official",
                    "status": "ok",
                    "last_updated": now.isoformat(),
                    "windows": [
                        {"name": "primary", "remaining_percent": 85.0, "resets_at": (now + timedelta(hours=4)).isoformat()}
                    ]
                }
            ]
        }
        # Only 1 snapshot (insufficient samples)
        history = [
            {
                "provider": "codex",
                "account_id": None,
                "observed_at": now.isoformat(),
                "source": "codex_app_server",
                "source_type": "official",
                "confidence": "official",
                "status": "ok",
                "windows": [{"name": "primary", "remaining_percent": 85.0}]
            }
        ]
        brief = build_daily_brief_vm(doc, history=history, now=now)
        codex_vm = brief.accounts[0]
        self.assertIsNone(codex_vm.five_hour_burn_rate)
        self.assertEqual(codex_vm.formatted_five_hour_burn_rate, "—")


if __name__ == "__main__":
    unittest.main()
