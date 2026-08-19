import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cloud.dispatch_ingress import handle_dispatch
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
    DailyBriefViewModel,
    parse_scheduled_task_health,
    build_session_center_health,
)
from manager.quota_forecast import (
    AccountQuotaForecast,
    QuotaWindowForecast,
    WarningLevel,
    RiskStatus,
    ActionRecommendation
)
from manager.tasks import DriveRecords, create_project
from manager.test_dispatcher import quota as quota_fixture
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_tasks import FakeDriveService


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

    def test_daily_brief_codex_primary_exhausted_with_credits_via_real_dashboard_pipeline(self):
        """End-to-end reproduction of the actual production dashboard.py data
        flow: read_drive_status() -> manager.quota_reader.summarize() ->
        build_daily_brief_vm() -- NOT build_daily_brief_vm(raw_doc) directly.
        This is the exact path that was still showing 'No AI Available' live
        after only manager.quota_forecast was fixed, because summarize()
        silently dropped metadata.credits before the forecast ever saw it."""
        from manager.quota_reader import summarize

        now = datetime(2026, 8, 19, 15, 49, 25, tzinfo=timezone.utc)
        raw_doc = {
            "generated_at": now.isoformat(),
            "providers": [
                {
                    "provider": "claude",
                    "display_name": "Claude Code",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "unknown",
                    "status": "unknown",
                    "last_updated": "2026-08-09T04:14:40Z",
                    "windows": [],
                },
                {
                    "provider": "codex",
                    "display_name": "Codex",
                    "source": "codex_app_server",
                    "source_type": "official",
                    "confidence": "official",
                    "status": "ok",
                    "last_updated": now.isoformat(),
                    "windows": [
                        {"name": "primary", "duration_minutes": 10080, "used_percent": 100, "remaining_percent": 0, "resets_at": (now + timedelta(hours=12)).isoformat()}
                    ],
                    "metadata": {
                        "credits": {"hasCredits": True, "unlimited": False, "balance": "768.2067540000"},
                    },
                },
            ],
        }
        quota_summary = summarize(raw_doc, max_age_minutes=60, now=now)
        brief = build_daily_brief_vm(quota_summary, now=now)

        self.assertNotEqual(brief.recommended_display_name, "No AI Available")
        self.assertNotEqual(brief.recommended_action, "hold")
        self.assertEqual(brief.recommended_provider, "codex")
        codex_vm = next(a for a in brief.accounts if a.provider == "codex")
        self.assertEqual(codex_vm.effective_availability, "available_via_credits")

    def test_daily_brief_codex_primary_exhausted_with_credits_is_not_no_ai_available(self):
        """Reproduces the real production status.json shape (2026-08-19): Codex
        primary quota at 0% with a usable credits balance, Claude stale from a
        much older snapshot. The Dashboard must NOT say 'No AI Available' /
        HOLD -- Codex is truthfully available via its extra credits."""
        now = datetime(2026, 8, 19, 15, 34, 25, tzinfo=timezone.utc)
        doc = {
            "providers": [
                {
                    "provider": "claude",
                    "account_id": None,
                    "display_name": "Claude Code",
                    "source": "claude_code_statusline_rate_limits",
                    "source_type": "official",
                    "confidence": "unknown",
                    "status": "unknown",
                    "last_updated": "2026-08-09T04:14:40Z",
                    "windows": [],
                },
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
                        {"name": "primary", "duration_minutes": 10080, "used_percent": 100, "remaining_percent": 0, "resets_at": (now + timedelta(hours=12)).isoformat()}
                    ],
                    "metadata": {
                        "credits": {"hasCredits": True, "unlimited": False, "balance": "813.5882690000"},
                    },
                },
            ]
        }
        brief = build_daily_brief_vm(doc, now=now)

        self.assertNotEqual(brief.recommended_display_name, "No AI Available")
        self.assertNotEqual(brief.recommended_action, "hold")
        self.assertEqual(brief.recommended_provider, "codex")

        codex_vm = next(a for a in brief.accounts if a.provider == "codex")
        claude_vm = next(a for a in brief.accounts if a.provider == "claude")

        # Primary quota and extra credits are separately visible and truthful.
        self.assertEqual(codex_vm.formatted_five_hour_remaining, "0.0%")
        self.assertEqual(codex_vm.extra_credits_available, True)
        self.assertIn("813.5882690000", codex_vm.formatted_extra_credits)
        self.assertEqual(codex_vm.effective_availability, "available_via_credits")
        self.assertEqual(codex_vm.formatted_effective_availability, "Available via credits")

        # Claude stays Unknown/Stale -- never converted to a fabricated 0%.
        self.assertTrue(claude_vm.stale)
        self.assertEqual(claude_vm.effective_availability, "unknown")
        self.assertEqual(claude_vm.formatted_effective_availability, "Unknown / Stale")

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


class DirectDispatchDashboardVisibilityTests(unittest.TestCase):
    """Targeted proof that the PC Dashboard's existing data mapping
    (map_task_board/get_global_summary) needs no changes to show a Task
    created through the Direct Dispatch ingress: it loads every Task per
    project unconditionally, with no filter on origin/source_context, so
    an ingress-created Task is visible the same way any other Task is --
    through its lifecycle from Ready to In progress/active session to
    Completed, exactly like the existing PROJECT/TASK/EXECUTION contract
    already relied on by dashboard.py."""

    def setUp(self):
        self.service = FakeDriveService()
        self.store = DriveRecords(self.service)
        create_project(self.store, {
            "project_id": "p1", "name": "Project One", "repo": "https://github.com/example/project",
            "default_branch": "main", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [],
            "current_phase": "Phase 1", "important_constraints": [],
        })
        self.registry = MemoryClaimRegistry()
        self.quota_patch = patch("manager.dispatcher.read_drive_status", return_value=quota_fixture())
        self.quota_patch.start()
        self.addCleanup(self.quota_patch.stop)

    def create_ingress_task(self, request_id="req-1"):
        result = handle_dispatch(
            self.store, self.service, lambda project_id, request_id: self.registry,
            {"request_id": request_id, "project_id": "p1", "title": "Investigate flaky test", "goal": "Read logs only"},
        )
        return self.store.get("tasks", "p1", result["task_id"]), result

    def test_freshly_created_ingress_task_appears_on_the_ready_board(self):
        task, _ = self.create_ingress_task()
        now = datetime.now(timezone.utc)
        board = map_task_board([task], {}, now)
        self.assertEqual(1, len(board["Ready"]))
        self.assertEqual("dispatch-req-1", board["Ready"][0]["task_id"])
        for bucket in ("In progress", "Blocked / Attention", "Completed"):
            self.assertEqual(0, len(board[bucket]))
        summary = get_global_summary([], [task], [])
        self.assertEqual(0, summary["running_tasks_count"])
        self.assertEqual(0, summary["blocked_tasks_count"])

    def test_running_execution_moves_ingress_task_to_in_progress_with_active_session(self):
        task, result = self.create_ingress_task(request_id="req-2")
        task["status"] = "in_progress"  # what the Command Watcher's on_running callback drives
        execution = {
            "project_id": "p1", "task_id": task["task_id"], "execution_id": f"command-{result['command_id']}",
            "status": "running", "provider": "codex",
        }
        now = datetime.now(timezone.utc)
        active_executions = [e for e in [execution] if e.get("status") not in {"completed", "failed", "interrupted", "cancelled"}]
        active_executions_dict = {(e.get("project_id"), e.get("task_id")): e for e in active_executions}
        board = map_task_board([task], active_executions_dict, now)
        self.assertEqual(1, len(board["In progress"]))
        summary = get_global_summary([], [task], active_executions)
        self.assertEqual(1, summary["running_tasks_count"])
        self.assertEqual(1, summary["active_sessions_count"])

    def test_completed_execution_moves_ingress_task_to_completed_and_drops_active_session(self):
        task, result = self.create_ingress_task(request_id="req-3")
        task["status"] = "completed"
        execution = {
            "project_id": "p1", "task_id": task["task_id"], "execution_id": f"command-{result['command_id']}",
            "status": "completed", "provider": "codex",
        }
        now = datetime.now(timezone.utc)
        active_executions = [e for e in [execution] if e.get("status") not in {"completed", "failed", "interrupted", "cancelled"}]
        board = map_task_board([task], {}, now)
        self.assertEqual(1, len(board["Completed"]))
        summary = get_global_summary([], [task], active_executions)
        self.assertEqual(0, summary["active_sessions_count"])


class WatcherSessionCenterHealthTests(unittest.TestCase):
    def test_enabled_running_task_is_online(self):
        raw = "TaskName:  \\Foo\nStatus:  Running\nScheduled Task State:  Enabled\n"
        vm = parse_scheduled_task_health("Foo", raw)
        self.assertTrue(vm.found)
        self.assertEqual("Online", vm.status_label)

    def test_enabled_ready_idle_task_is_online(self):
        raw = "TaskName:  \\Foo\nStatus:  Ready\nScheduled Task State:  Enabled\n"
        vm = parse_scheduled_task_health("Foo", raw)
        self.assertEqual("Online", vm.status_label)

    def test_disabled_task_is_offline(self):
        raw = "TaskName:  \\Foo\nStatus:  Ready\nScheduled Task State:  Disabled\n"
        vm = parse_scheduled_task_health("Foo", raw)
        self.assertEqual("Offline", vm.status_label)

    def test_missing_query_output_is_unknown_not_offline(self):
        vm = parse_scheduled_task_health("Foo", None)
        self.assertFalse(vm.found)
        self.assertEqual("Unknown", vm.status_label)

    def test_unparseable_output_is_unknown(self):
        vm = parse_scheduled_task_health("Foo", "ERROR: The system cannot find the file specified.\n")
        self.assertEqual("Unknown", vm.status_label)

    def test_session_center_not_listening_is_offline(self):
        vm = build_session_center_health(listening=False, session=None)
        self.assertEqual("Offline", vm.status_label)

    def test_session_center_listening_with_session_is_online(self):
        vm = build_session_center_health(listening=True, session={"provider": "codex", "current_state": "running"})
        self.assertEqual("Online", vm.status_label)
        self.assertIn("codex", vm.detail)

    def test_session_center_listening_without_session_payload_is_online(self):
        vm = build_session_center_health(listening=True, session=None)
        self.assertEqual("Online", vm.status_label)


if __name__ == "__main__":
    unittest.main()
