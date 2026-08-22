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
    UNKNOWN_LABEL,
    DISPATCH_STATE_SUBMITTED,
    DISPATCH_STATE_ACCEPTED,
    DISPATCH_STATE_QUEUED,
    DISPATCH_STATE_CLAIMED,
    DISPATCH_STATE_RUNNING,
    DISPATCH_STATE_COMPLETED,
    DISPATCH_STATE_BLOCKED,
    DISPATCH_STATE_UNKNOWN,
    compute_dispatch_state,
    build_provider_truth,
    build_quota_truth,
    build_dispatch_truth_row,
    compute_visible_dispatch_gate,
    parse_task_to_run_path,
    build_provenance_vm,
    compute_provenance_gate,
    compute_overall_visible_dispatch_gate,
    validate_provenance_evidence_document,
    reconcile_watcher_provenance_evidence,
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

        stale_projection = get_global_summary(providers, [{"status": "in_progress"}], [])
        self.assertEqual(stale_projection["running_tasks_count"], 0)

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


class VisibleDispatchTruthGateTests(unittest.TestCase):
    """Task/Provider/Account/Quota truth for the Dashboard's Visible
    Dispatch Gate. Covers the acceptance checklist from the Dashboard
    Visible Dispatch Truth Gate task spec (2026-08-22): dispatch-state
    honesty, per-account quota isolation, and graceful UNKNOWN/STALE
    rather than any guessed or demo value."""

    def setUp(self):
        self.now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
        self.project = {"project_id": "p1", "name": "Project One"}

    def task(self, **overrides):
        base = {"task_id": "t1", "project_id": "p1", "status": "in_progress", "title": "Do the thing"}
        base.update(overrides)
        return base

    def command(self, **overrides):
        base = {
            "command_id": "cmd-1", "project_id": "p1", "task_id": "t1", "provider": "claude",
            "account_id": "claude-b", "model": "claude-opus", "mode": "code", "status": "running",
        }
        base.update(overrides)
        return base

    def execution(self, **overrides):
        base = {
            "execution_id": "exec-1", "project_id": "p1", "task_id": "t1", "provider": "claude",
            "status": "running", "provider_session_id": "sess-abc",
        }
        base.update(overrides)
        return base

    def quota_accounts(self, entries):
        doc = {"providers": entries}
        return build_daily_brief_vm(doc, now=self.now).accounts

    # 1. submitted must not display as running.
    def test_submitted_task_is_not_running(self):
        result = compute_dispatch_state(self.task(status="ready"), None, None, self.now)
        self.assertEqual(result["state"], DISPATCH_STATE_SUBMITTED)
        self.assertNotEqual(result["state"], DISPATCH_STATE_RUNNING)

    # 2. accepted (dispatch-request evidence only, no command yet) must not display as running.
    def test_accepted_task_is_not_running(self):
        result = compute_dispatch_state(self.task(status="ready"), None, None, self.now, has_dispatch_request=True)
        self.assertEqual(result["state"], DISPATCH_STATE_ACCEPTED)
        self.assertNotEqual(result["state"], DISPATCH_STATE_RUNNING)

    # queued/claimed must not display as running either.
    def test_queued_and_claimed_are_not_running(self):
        queued = compute_dispatch_state(self.task(), self.command(status="queued"), None, self.now)
        claimed = compute_dispatch_state(self.task(), self.command(status="claimed"), None, self.now)
        self.assertEqual(queued["state"], DISPATCH_STATE_QUEUED)
        self.assertEqual(claimed["state"], DISPATCH_STATE_CLAIMED)
        self.assertNotIn(DISPATCH_STATE_RUNNING, (queued["state"], claimed["state"]))

    # 3. RUNNING requires execution.status == running AND provider session evidence.
    def test_running_requires_execution_status_and_session_evidence(self):
        result = compute_dispatch_state(self.task(), self.command(), self.execution(), self.now)
        self.assertEqual(result["state"], DISPATCH_STATE_RUNNING)

    def test_command_running_without_execution_record_is_not_running(self):
        result = compute_dispatch_state(self.task(), self.command(status="running"), None, self.now)
        self.assertEqual(result["state"], DISPATCH_STATE_CLAIMED)
        self.assertNotEqual(result["state"], DISPATCH_STATE_RUNNING)

    def test_execution_running_without_provider_session_id_is_not_running(self):
        exe = self.execution(provider_session_id=None)
        result = compute_dispatch_state(self.task(), self.command(), exe, self.now)
        self.assertNotEqual(result["state"], DISPATCH_STATE_RUNNING)
        self.assertEqual(result["state"], DISPATCH_STATE_CLAIMED)

    # 4. Claude A / Claude B must never share a quota card.
    def test_claude_a_and_b_quota_not_shared(self):
        accounts = self.quota_accounts([
            {"provider": "claude", "account_id": "claude-a", "display_name": "Claude A",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": (self.now + timedelta(hours=3)).isoformat()}]},
            {"provider": "claude", "account_id": "claude-b", "display_name": "Claude B",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [{"name": "five_hour", "remaining_percent": 10.0, "resets_at": (self.now + timedelta(hours=1)).isoformat()}]},
        ])
        quota_a = build_quota_truth(accounts, "claude", "claude-a")
        quota_b = build_quota_truth(accounts, "claude", "claude-b")
        self.assertEqual(quota_a["five_hour_remaining_pct"], 90.0)
        self.assertEqual(quota_b["five_hour_remaining_pct"], 10.0)
        self.assertNotEqual(quota_a["five_hour_remaining_pct"], quota_b["five_hour_remaining_pct"])

    # 5. 5h and weekly windows displayed independently.
    def test_five_hour_and_weekly_shown_independently(self):
        accounts = self.quota_accounts([
            {"provider": "claude", "account_id": "claude-b", "display_name": "Claude B",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [
                 {"name": "five_hour", "remaining_percent": 40.0, "resets_at": (self.now + timedelta(hours=2)).isoformat()},
                 {"name": "seven_day", "remaining_percent": 70.0, "resets_at": (self.now + timedelta(days=3)).isoformat()},
             ]},
        ])
        quota = build_quota_truth(accounts, "claude", "claude-b")
        self.assertEqual(quota["five_hour_remaining_pct"], 40.0)
        self.assertEqual(quota["weekly_remaining_pct"], 70.0)
        self.assertNotEqual(quota["five_hour_reset_at"], quota["weekly_reset_at"])

    # 6. Stale telemetry displayed explicitly, not hidden.
    def test_stale_quota_marked_explicitly(self):
        accounts = self.quota_accounts([
            {"provider": "claude", "account_id": "claude-b", "display_name": "Claude B",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": (self.now - timedelta(hours=5)).isoformat(),
             "windows": [{"name": "five_hour", "remaining_percent": 50.0, "resets_at": (self.now + timedelta(hours=1)).isoformat()}]},
        ])
        quota = build_quota_truth(accounts, "claude", "claude-b")
        self.assertEqual(quota["freshness"], "STALE")

    # 7. Missing account quota is explicit UNKNOWN, never borrowed from another account.
    def test_missing_account_quota_is_unknown(self):
        accounts = self.quota_accounts([
            {"provider": "claude", "account_id": "claude-a", "display_name": "Claude A",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": (self.now + timedelta(hours=3)).isoformat()}]},
        ])
        quota = build_quota_truth(accounts, "claude", "claude-b")
        self.assertFalse(quota["found"])
        self.assertEqual(quota["freshness"], UNKNOWN_LABEL)
        self.assertEqual(quota["formatted_five_hour_remaining"], UNKNOWN_LABEL)
        self.assertIsNone(quota["five_hour_remaining_pct"])

    # 8. Null reset_at is UNKNOWN, never a guessed timestamp.
    def test_null_reset_at_is_unknown_not_guessed(self):
        accounts = self.quota_accounts([
            {"provider": "claude", "account_id": "claude-b", "display_name": "Claude B",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [{"name": "five_hour", "remaining_percent": 50.0, "resets_at": None}]},
        ])
        quota = build_quota_truth(accounts, "claude", "claude-b")
        self.assertIsNone(quota["five_hour_reset_at"])
        self.assertEqual(quota["formatted_five_hour_reset_at"], UNKNOWN_LABEL)

    # 9. Task/provider/account correspondence: two tasks must not cross-bind.
    def test_row_binds_task_to_its_own_provider_and_account(self):
        accounts = self.quota_accounts([
            {"provider": "claude", "account_id": "claude-a", "display_name": "Claude A",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": (self.now + timedelta(hours=3)).isoformat()}]},
            {"provider": "codex", "account_id": "codex-1", "display_name": "Codex",
             "source": "codex_app_server", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [{"name": "primary", "remaining_percent": 20.0, "resets_at": (self.now + timedelta(hours=4)).isoformat()}]},
        ])
        row_claude = build_dispatch_truth_row(
            self.project, self.task(task_id="t1"), self.command(task_id="t1", provider="claude", account_id="claude-a"),
            None, accounts, self.now,
        )
        row_codex = build_dispatch_truth_row(
            self.project, self.task(task_id="t2"), self.command(task_id="t2", command_id="cmd-2", provider="codex", account_id="codex-1", model="gpt", status="queued"),
            None, accounts, self.now,
        )
        self.assertEqual(row_claude["provider"], "claude")
        self.assertEqual(row_claude["account_id"], "claude-a")
        self.assertEqual(row_claude["quota"]["five_hour_remaining_pct"], 90.0)
        self.assertEqual(row_codex["provider"], "codex")
        self.assertEqual(row_codex["account_id"], "codex-1")
        self.assertEqual(row_codex["quota"]["five_hour_remaining_pct"], 20.0)

    # 10. Wrong execution linkage must FAIL to prove running.
    def test_mismatched_execution_linkage_does_not_prove_running(self):
        wrong_execution = self.execution(task_id="other-task", project_id="p1")
        result = compute_dispatch_state(self.task(), self.command(), wrong_execution, self.now)
        self.assertNotEqual(result["state"], DISPATCH_STATE_RUNNING)
        self.assertEqual(result["state"], DISPATCH_STATE_CLAIMED)
        self.assertIn("linkage mismatch", result["reason"])

    # 11. Stale session/heartbeat must not be mis-bound as running.
    def test_stale_session_not_treated_as_running(self):
        stale_execution = self.execution(
            provider="codex", heartbeat_at=(self.now - timedelta(minutes=30)).isoformat(),
            started_at=(self.now - timedelta(minutes=45)).isoformat(),
        )
        result = compute_dispatch_state(self.task(), self.command(provider="codex"), stale_execution, self.now)
        self.assertNotEqual(result["state"], DISPATCH_STATE_RUNNING)

    # 12. Missing Drive record degrades gracefully to UNKNOWN.
    def test_missing_task_record_is_unknown(self):
        result = compute_dispatch_state(None, None, None, self.now)
        self.assertEqual(result["state"], DISPATCH_STATE_UNKNOWN)

    # 13. Dashboard truth rows use the real schema fields (not renamed/invented ones).
    def test_visible_dispatch_gate_passes_on_a_complete_real_schema_row(self):
        accounts = self.quota_accounts([
            {"provider": "claude", "account_id": "claude-b", "display_name": "Claude B",
             "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
             "status": "ok", "last_updated": self.now.isoformat(),
             "windows": [{"name": "five_hour", "remaining_percent": 50.0, "resets_at": (self.now + timedelta(hours=1)).isoformat()}]},
        ])
        row = build_dispatch_truth_row(self.project, self.task(), self.command(), self.execution(), accounts, self.now)
        gate = compute_visible_dispatch_gate([row])
        self.assertEqual(gate["result"], "PASS")
        self.assertEqual(gate["reasons"], [])

    def test_visible_dispatch_gate_fails_on_incomplete_row(self):
        row = build_dispatch_truth_row(self.project, self.task(), self.command(), self.execution(), [], self.now)
        del row["provider"]
        gate = compute_visible_dispatch_gate([row])
        self.assertEqual(gate["result"], "FAIL")
        self.assertTrue(any("provider" in reason for reason in gate["reasons"]))

    def test_visible_dispatch_gate_fails_on_no_rows(self):
        gate = compute_visible_dispatch_gate([])
        self.assertEqual(gate["result"], "FAIL")

    # 14. No mock/demo fallback: empty real inputs must yield explicit UNKNOWN, never invented numbers.
    def test_empty_inputs_never_fabricate_demo_data(self):
        row = build_dispatch_truth_row(None, self.task(status="ready"), None, None, [], self.now)
        self.assertEqual(row["provider"], UNKNOWN_LABEL)
        self.assertEqual(row["account_id"], UNKNOWN_LABEL)
        self.assertEqual(row["model"], UNKNOWN_LABEL)
        self.assertEqual(row["mode"], UNKNOWN_LABEL)
        self.assertEqual(row["dispatch_state"], DISPATCH_STATE_SUBMITTED)
        self.assertEqual(row["quota"]["freshness"], UNKNOWN_LABEL)
        self.assertFalse(row["quota"]["found"])


class ProductionProvenanceContractTests(unittest.TestCase):
    """Dashboard-vs-Watcher runtime identity truth (2026-08-22 hard
    constraint): the Gate must require Dashboard reviewed SHA == Watcher
    running_sha == tested_sha == activated_sha, all real -- any UNKNOWN or
    mismatch must FAIL, never be hidden, and never be overridden by other
    tests/gates passing."""

    def setUp(self):
        self.now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)

    def test_parse_task_to_run_path_extracts_quoted_script_path(self):
        raw = 'Task To Run:                          wscript.exe "C:\\repo\\manager\\generated\\command-watcher.vbs"'
        self.assertEqual(parse_task_to_run_path(raw), "C:\\repo\\manager\\generated\\command-watcher.vbs")

    def test_parse_task_to_run_path_handles_unquoted_path(self):
        raw = "Task To Run:                          C:\\repo\\run.exe"
        self.assertEqual(parse_task_to_run_path(raw), "C:\\repo\\run.exe")

    def test_parse_task_to_run_path_missing_line_is_none(self):
        self.assertIsNone(parse_task_to_run_path("TaskName: \\Foo\nStatus: Ready\n"))
        self.assertIsNone(parse_task_to_run_path(None))

    def test_all_four_shas_matching_passes(self):
        vm = build_provenance_vm(
            "C:\\dash", "main", "abc123",
            "C:\\watcher", "main", "abc123", "abc123", "abc123",
            now=self.now,
        )
        self.assertTrue(vm.all_match)
        self.assertEqual(compute_provenance_gate(vm)["result"], "PASS")

    def test_dashboard_and_watcher_on_different_sha_fails(self):
        vm = build_provenance_vm(
            "C:\\dash", "fix/dashboard-x", "aa5216f",
            "C:\\watcher", "integration/runtime-v2", "680b107", "680b107", "680b107",
            now=self.now,
        )
        self.assertFalse(vm.all_match)
        gate = compute_provenance_gate(vm)
        self.assertEqual(gate["result"], "FAIL")
        self.assertIn("mismatch", gate["reasons"][0].lower())

    def test_missing_tested_and_activated_sha_fails_not_silently_passes(self):
        # No real Production Provenance Contract evidence yet: tested_sha/
        # activated_sha are UNKNOWN. This must FAIL, never be treated as a
        # pass just because running_sha matches the dashboard's own SHA.
        vm = build_provenance_vm(
            "C:\\dash", "main", "abc123",
            "C:\\watcher", "main", "abc123", None, None,
            now=self.now,
        )
        self.assertFalse(vm.all_match)
        self.assertEqual(vm.watcher_tested_sha, UNKNOWN_LABEL)
        self.assertEqual(vm.watcher_activated_sha, UNKNOWN_LABEL)
        gate = compute_provenance_gate(vm)
        self.assertEqual(gate["result"], "FAIL")
        self.assertIn("no real evidence", gate["reasons"][0])

    def test_missing_watcher_evidence_entirely_fails(self):
        vm = build_provenance_vm("C:\\dash", "main", "abc123", None, None, None, None, None, now=self.now)
        self.assertEqual(vm.watcher_repository_path, UNKNOWN_LABEL)
        self.assertEqual(compute_provenance_gate(vm)["result"], "FAIL")

    def test_overall_gate_requires_both_dispatch_and_provenance_pass(self):
        passing_dispatch = {"result": "PASS", "reasons": []}
        failing_dispatch = {"result": "FAIL", "reasons": ["task t1: missing required truth field 'provider'"]}
        passing_provenance = {"result": "PASS", "reasons": []}
        failing_provenance = {"result": "FAIL", "reasons": ["SHA mismatch: dashboard_reviewed_sha=aa5216f, watcher_running_sha=680b107"]}

        self.assertEqual(compute_overall_visible_dispatch_gate(passing_dispatch, passing_provenance)["result"], "PASS")
        self.assertEqual(compute_overall_visible_dispatch_gate(failing_dispatch, passing_provenance)["result"], "FAIL")
        self.assertEqual(compute_overall_visible_dispatch_gate(passing_dispatch, failing_provenance)["result"], "FAIL")
        overall = compute_overall_visible_dispatch_gate(failing_dispatch, failing_provenance)
        self.assertEqual(overall["result"], "FAIL")
        self.assertEqual(len(overall["reasons"]), 2)

    def test_28_other_passing_checks_never_override_a_provenance_fail(self):
        # Regression guard for the explicit hard constraint: passing every
        # other row/dispatch check must not paper over a provenance FAIL.
        many_passing_dispatch_reasons = {"result": "PASS", "reasons": []}
        provenance_fail = compute_provenance_gate(build_provenance_vm(
            "C:\\dash", "main", "aa5216f", "C:\\watcher", "main", "680b107", "680b107", "680b107", now=self.now,
        ))
        overall = compute_overall_visible_dispatch_gate(many_passing_dispatch_reasons, provenance_fail)
        self.assertEqual(overall["result"], "FAIL")


class ProvenanceEvidenceFileReconciliationTests(unittest.TestCase):
    """A persisted Production Provenance Contract evidence file (e.g.
    <AI_MANAGER_HOME>/provenance/runtime_evidence.json) is only trusted for
    tested_sha/activated_sha when it agrees with what the Dashboard
    independently observed via real git introspection -- never blindly."""

    def setUp(self):
        self.now = datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc)

    def _doc(self, **overrides):
        base = {
            "running_sha": "6d41645", "tested_sha": "6d41645", "activated_sha": "6d41645",
            "repository_path": "C:\\watcher-repo", "branch": "integration/x", "captured_at": self.now.isoformat(),
        }
        base.update(overrides)
        return base

    @staticmethod
    def _active(session_id="provider-session"):
        return (
            {"project_id": "p1", "task_id": "t1"},
            {"project_id": "p1", "task_id": "t1", "status": "running", "execution_id": "e1"},
            {"project_id": "p1", "task_id": "t1", "status": "running", "execution_id": "e1", "provider_session_id": session_id},
        )

    def _reconcile(self, age=0, watcher_running=False, active=None, **doc_changes):
        doc = self._doc(captured_at=(self.now - timedelta(seconds=age)).isoformat(), **doc_changes)
        task, command, execution = active or (None, None, None)
        return reconcile_watcher_provenance_evidence(
            "C:\\watcher-repo", "6d41645", validate_provenance_evidence_document(doc), now=self.now,
            watcher_running=watcher_running, active_task=task, active_command=command, active_execution=execution,
        )

    def test_valid_document_normalizes_cleanly(self):
        normalized = validate_provenance_evidence_document(self._doc())
        self.assertEqual(normalized["tested_sha"], "6d41645")

    def test_missing_field_is_rejected(self):
        doc = self._doc()
        del doc["activated_sha"]
        self.assertIsNone(validate_provenance_evidence_document(doc))

    def test_blank_field_is_rejected(self):
        self.assertIsNone(validate_provenance_evidence_document(self._doc(tested_sha="  ")))

    def test_non_dict_is_rejected(self):
        self.assertIsNone(validate_provenance_evidence_document(None))
        self.assertIsNone(validate_provenance_evidence_document("not a dict"))

    def test_matching_repository_and_sha_is_trusted(self):
        doc = validate_provenance_evidence_document(self._doc())
        result = reconcile_watcher_provenance_evidence("C:\\watcher-repo", "6d41645", doc, now=self.now)
        self.assertEqual(result["tested_sha"], "6d41645")
        self.assertEqual(result["activated_sha"], "6d41645")
        self.assertIn("matches", result["note"])

    def test_no_evidence_document_is_unknown_not_guessed(self):
        result = reconcile_watcher_provenance_evidence("C:\\watcher-repo", "6d41645", None)
        self.assertIsNone(result["tested_sha"])
        self.assertIsNone(result["activated_sha"])

    def test_mismatched_repository_path_is_ignored_not_trusted(self):
        doc = validate_provenance_evidence_document(self._doc(repository_path="C:\\different-repo"))
        result = reconcile_watcher_provenance_evidence("C:\\watcher-repo", "6d41645", doc)
        self.assertIsNone(result["tested_sha"])
        self.assertIn("does not match", result["note"])

    def test_stale_running_sha_in_evidence_file_is_ignored_not_trusted(self):
        # The Dashboard independently sees a newer live HEAD than the
        # evidence file claims -- the file is stale and must not be used.
        doc = validate_provenance_evidence_document(self._doc(running_sha="old-sha"))
        result = reconcile_watcher_provenance_evidence("C:\\watcher-repo", "new-sha", doc)
        self.assertIsNone(result["tested_sha"])
        self.assertIn("stale", result["note"])

    def test_normal_freshness_and_boundary_are_trusted(self):
        for age in (0, 300):
            with self.subTest(age=age):
                self.assertEqual("6d41645", self._reconcile(age=age)["tested_sha"])

    def test_stale_future_and_invalid_captured_at_are_untrusted(self):
        self.assertIsNone(self._reconcile(age=301)["tested_sha"])
        future = self._doc(captured_at=(self.now + timedelta(seconds=16)).isoformat())
        self.assertIsNone(reconcile_watcher_provenance_evidence("C:\\watcher-repo", "6d41645", validate_provenance_evidence_document(future), now=self.now)["tested_sha"])
        self.assertIsNone(validate_provenance_evidence_document(self._doc(captured_at="not-a-time")))
        self.assertIsNone(validate_provenance_evidence_document(self._doc(captured_at="")))

    def test_extended_running_grace_requires_all_live_linkage_evidence(self):
        active = self._active()
        self.assertEqual("6d41645", self._reconcile(age=1800, watcher_running=True, active=active)["tested_sha"])
        self.assertIsNone(self._reconcile(age=1800, watcher_running=True)["tested_sha"])
        self.assertIsNone(self._reconcile(age=1800, watcher_running=True, active=self._active(session_id=""))["tested_sha"])
        self.assertIsNone(self._reconcile(age=1800, watcher_running=False, active=active)["tested_sha"])
        task, command, execution = active
        execution["task_id"] = "other"
        self.assertIsNone(self._reconcile(age=1800, watcher_running=True, active=(task, command, execution))["tested_sha"])

    def test_extended_running_hard_cap_and_boundaries(self):
        active = self._active()
        self.assertEqual("6d41645", self._reconcile(age=7800, watcher_running=True, active=active)["tested_sha"])
        self.assertIsNone(self._reconcile(age=7801, watcher_running=True, active=active)["tested_sha"])
        future = self._doc(captured_at=(self.now + timedelta(seconds=15)).isoformat())
        self.assertEqual("6d41645", reconcile_watcher_provenance_evidence("C:\\watcher-repo", "6d41645", validate_provenance_evidence_document(future), now=self.now)["tested_sha"])


if __name__ == "__main__":
    unittest.main()
