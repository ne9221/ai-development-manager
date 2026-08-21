#!/usr/bin/env python3
"""Comprehensive Truth Contract and Fail-Closed Tests for Claude Multi-Account Quota."""

import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from collectors.claude import normalize as normalize_claude
from manager.claude_account_selector import select_claude_account, resolve_claude_account
from manager.command_watcher import process_command, provider_quota_reliable, claude_quota_reliable
from manager.quota_forecast import forecast_account, RiskStatus, ActionRecommendation, WarningLevel
from manager.quota_reader import summarize, unknown_account_summary
from manager.refresh_status import refresh, discover_claude_accounts, claude_snapshot


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_offset(minutes=0, base=None):
    ref = base or datetime.now(timezone.utc)
    return (ref + timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")


class ClaudeQuotaTruthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    # 1. A/B payload isolation
    def test_1_account_a_b_payload_isolation(self):
        payload_a = self.base / "payload_a.json"
        payload_b = self.base / "payload_b.json"
        payload_a.write_text(json.dumps({
            "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": iso_offset(60)}}
        }), encoding="utf-8")
        payload_b.write_text(json.dumps({
            "rate_limits": {"five_hour": {"used_percentage": 85, "resets_at": iso_offset(180)}}
        }), encoding="utf-8")

        snap_a = claude_snapshot(payload_a, account_id="account-a")
        snap_b = claude_snapshot(payload_b, account_id="account-b")

        self.assertEqual("account-a", snap_a["account_id"])
        self.assertEqual(80, snap_a["windows"][0]["remaining_percent"])
        self.assertEqual("account-b", snap_b["account_id"])
        self.assertEqual(15, snap_b["windows"][0]["remaining_percent"])
        self.assertNotEqual(snap_a["windows"][0]["resets_at"], snap_b["windows"][0]["resets_at"])

    # 2. 5h exhausted blocks account
    def test_2_five_hour_exhausted_blocks_account(self):
        item = {
            "provider": "claude",
            "account_id": "account-b",
            "source": "claude_code_statusline_rate_limits",
            "source_type": "official",
            "confidence": "official",
            "last_updated": now_iso(),
            "windows": [
                {"name": "five_hour", "used_percent": 100.0, "remaining_percent": 0.0, "resets_at": iso_offset(120)}
            ]
        }
        fc = forecast_account(item)
        self.assertFalse(fc.dispatchable, "0% remaining on 5h must be non-dispatchable")
        self.assertEqual(RiskStatus.EXHAUSTED, fc.overall_risk_status)
        self.assertEqual(ActionRecommendation.HOLD, fc.overall_action_recommendation)

    # 3. weekly available does not override 5h exhausted
    def test_3_weekly_available_does_not_override_five_hour_exhausted(self):
        item = {
            "provider": "claude",
            "account_id": "account-b",
            "source": "claude_code_statusline_rate_limits",
            "source_type": "official",
            "confidence": "official",
            "last_updated": now_iso(),
            "windows": [
                {"name": "five_hour", "used_percent": 100.0, "remaining_percent": 0.0, "resets_at": iso_offset(30)},
                {"name": "seven_day", "used_percent": 22.0, "remaining_percent": 78.0, "resets_at": iso_offset(4000)}
            ]
        }
        fc = forecast_account(item)
        self.assertFalse(fc.dispatchable, "weekly available must NOT override 5h exhausted")
        self.assertEqual(RiskStatus.EXHAUSTED, fc.overall_risk_status)
        self.assertEqual(ActionRecommendation.HOLD, fc.overall_action_recommendation)

    # 4. stale -> UNKNOWN/HOLD
    def test_4_stale_record_becomes_unknown_and_hold(self):
        old_time = iso_offset(-120)  # 2 hours ago (> 60m max age)
        item = {
            "provider": "claude",
            "account_id": "account-b",
            "source": "claude_code_statusline_rate_limits",
            "source_type": "official",
            "confidence": "official",
            "last_updated": old_time,
            "windows": [
                {"name": "five_hour", "used_percent": 10.0, "remaining_percent": 90.0, "resets_at": iso_offset(100)}
            ]
        }
        fc = forecast_account(item, max_age_minutes=60)
        self.assertTrue(fc.stale)
        self.assertEqual("stale", fc.freshness)
        self.assertFalse(fc.dispatchable)
        self.assertEqual(RiskStatus.UNKNOWN, fc.overall_risk_status)
        self.assertEqual(ActionRecommendation.HOLD, fc.overall_action_recommendation)

    # 5. missing reset_at does not guess
    def test_5_missing_reset_at_not_guessed(self):
        item = {
            "provider": "claude",
            "account_id": "account-b",
            "source": "claude_code_statusline_rate_limits",
            "source_type": "official",
            "confidence": "official",
            "last_updated": now_iso(),
            "windows": [
                {"name": "five_hour", "used_percent": 30.0, "remaining_percent": 70.0, "resets_at": None}
            ]
        }
        fc = forecast_account(item)
        w = fc.windows[0]
        self.assertIsNone(w.resets_at)
        self.assertIsNone(w.hours_to_reset)
        self.assertEqual(ActionRecommendation.HOLD, w.action_recommendation)
        self.assertIn("Reset timestamp is unknown", w.warning_reason)

    # 6. old payload does not refresh captured_at
    def test_6_old_payload_does_not_refresh_captured_at(self):
        payload_file = self.base / "payload.json"
        payload_file.write_text(json.dumps({
            "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": iso_offset(60)}}
        }), encoding="utf-8")
        # Set mtime back 30 minutes
        past_epoch = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
        os.utime(payload_file, (past_epoch, past_epoch))

        snap = claude_snapshot(payload_file, "account-b")
        snap_time = datetime.fromisoformat(snap["last_updated"].replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        self.assertGreater((now_dt - snap_time).total_seconds(), 1500, "captured_at must reflect file mtime, not now")

    # 7. past reset waits fresh telemetry
    def test_7_past_reset_waits_fresh_telemetry(self):
        item = {
            "provider": "claude",
            "account_id": "account-b",
            "source": "claude_code_statusline_rate_limits",
            "source_type": "official",
            "confidence": "official",
            "last_updated": now_iso(),
            "windows": [
                {"name": "five_hour", "used_percent": 100.0, "remaining_percent": 0.0, "resets_at": iso_offset(-5)}
            ]
        }
        fc = forecast_account(item)
        self.assertFalse(fc.dispatchable)
        self.assertEqual(ActionRecommendation.HOLD, fc.overall_action_recommendation)
        self.assertIn("past; awaiting fresh telemetry", fc.windows[0].warning_reason)

    # 8. fresh post-reset telemetry resumes account
    def test_8_fresh_post_reset_telemetry_resumes_account(self):
        # Fresh post-reset snapshot with 100% remaining (0% used)
        fresh_item = {
            "provider": "claude",
            "account_id": "account-b",
            "source": "claude_code_statusline_rate_limits",
            "source_type": "official",
            "confidence": "official",
            "last_updated": now_iso(),
            "windows": [
                {"name": "five_hour", "used_percent": 0.0, "remaining_percent": 100.0, "resets_at": iso_offset(300)}
            ]
        }
        fc = forecast_account(fresh_item)
        self.assertTrue(fc.dispatchable, "Fresh post-reset telemetry must resume dispatchability")
        self.assertTrue(fc.has_reliable_quota)

    # 9. explicit account-b exhausted rejected by Watcher
    def test_9_explicit_account_b_exhausted_rejected_by_watcher(self):
        store = Mock()
        store.get.return_value = {
            "project_id": "p1", "task_id": "t1", "execution_id": "e1",
            "provider": "claude", "account_id": "account-b", "status": "queued",
            "enforcement": {"rules_version": "0.1.4", "rules_digest": "sha256:digest", "rules_applied": []}
        }
        registry = [{"account_id": "account-b", "enabled": True, "config_dir": str(self.base)}]
        
        # Quota check returns False for exhausted account-b
        def quota_gate(service, account_id=None):
            return False

        with patch("manager.command_watcher._claude_account_registry", return_value=registry),              patch("manager.command_watcher.validate_task_enforcement", return_value=True),              patch("manager.command_watcher.validate", return_value=True),              patch("manager.command_watcher._policy_satisfied", return_value=True):
            cmd = {"command_id": "c1", "project_id": "p1", "task_id": "t1", "provider": "claude", "account_id": "account-b", "status": "queued"}
            res = process_command(
                store, object(), cmd,
                claim_factory=lambda *_: Mock(),
                allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True,
                quota_check=quota_gate,
            )
            self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, res)

    # 10. Windows refresh multi-account contract
    def test_10_windows_refresh_multi_account_contract(self):
        home = self.base / "home"
        cfg_dir = home / "config"
        cfg_dir.mkdir(parents=True)
        registry_file = cfg_dir / "claude_accounts.json"
        registry_file.write_text(json.dumps({
            "accounts": [
                {"account_id": "account-a", "enabled": True, "config_dir": None},
                {"account_id": "account-b", "enabled": True, "config_dir": str(self.base / ".claude-b")},
                {"account_id": "account-c", "enabled": False, "config_dir": str(self.base / ".claude-c")},
            ]
        }), encoding="utf-8")

        discovered = discover_claude_accounts(home)
        self.assertIn("account-a", discovered)
        self.assertIn("account-b", discovered)
        self.assertNotIn("account-c", discovered, "Disabled account must not be discovered")
        self.assertEqual(Path(self.base / ".claude-b" / "statusline-payload.json"), discovered["account-b"])

    # 11. no guessed reset_at in normalize
    def test_11_no_guessed_reset_at_in_normalize(self):
        raw = {
            "rate_limits": {
                "five_hour": {"used_percentage": 45}  # no resets_at
            }
        }
        normalized = normalize_claude(raw)
        self.assertEqual(1, len(normalized["windows"]))
        self.assertIsNone(normalized["windows"][0]["resets_at"])

    # 12. Drive publication preserves account identities
    def test_12_drive_publication_preserves_account_identities(self):
        doc = {
            "schema_version": "0.1.0",
            "generated_at": now_iso(),
            "providers": [
                {
                    "provider": "codex", "display_name": "Codex", "collection_mode": "automatic",
                    "source": "codex_app_server", "source_type": "official", "confidence": "unknown",
                    "last_updated": now_iso(), "status": "unknown", "windows": []
                },
                {
                    "provider": "claude", "account_id": None, "display_name": "Claude Code", "collection_mode": "automatic",
                    "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "unknown",
                    "last_updated": now_iso(), "status": "unknown", "windows": []
                },
            ]
        }
        payload_a = self.base / "claude_a.json"
        payload_b = self.base / "claude_b.json"
        payload_a.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": None}}}), encoding="utf-8")
        payload_b.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": None}}}), encoding="utf-8")

        published = []
        result = refresh(
            service=object(),
            runtime_path=self.base / "status.json",
            log_path=self.base / "refresh.log",
            lock_path=self.base / "refresh.lock",
            claude_path=payload_a,
            claude_accounts={"account-a": payload_a, "account-b": payload_b},
            reader=lambda **_: deepcopy(doc),
            codex_collector=lambda **_: ({}, {"providers": [doc["providers"][0]]}),
            publisher=lambda s, p: published.append(json.loads(p.read_text())) or {"action": "updated"},
            history_store=False,
        )
        self.assertEqual(1, len(published))
        claude_entries = [p for p in published[0]["providers"] if p.get("provider") == "claude"]
        account_ids = {p.get("account_id") for p in claude_entries}
        self.assertIn("account-a", account_ids)
        self.assertIn("account-b", account_ids)
        self.assertIn(None, account_ids)

    # 13. Negative test: explicit account-b quota missing entirely -> MUST NOT launch provider
    def test_13_negative_explicit_account_b_missing_quota_must_not_launch(self):
        store = Mock()
        store.get.return_value = {
            "project_id": "p1", "task_id": "t1", "execution_id": "e1",
            "provider": "claude", "account_id": "account-b", "status": "queued",
            "enforcement": {"rules_version": "0.1.4", "rules_digest": "sha256:digest", "rules_applied": []}
        }
        registry = [{"account_id": "account-b", "enabled": True, "config_dir": str(self.base)}]
        
        # Simulating Drive SSOT having NO quota entry for account-b
        doc_without_account_b = {
            "schema_version": "0.1.0",
            "generated_at": now_iso(),
            "providers": [
                {"provider": "codex", "display_name": "Codex", "collection_mode": "automatic", "source": "codex_app_server", "source_type": "official", "confidence": "official", "last_updated": now_iso(), "status": "ok", "has_reliable_quota": True, "windows": [{"name": "primary", "duration_minutes": 10080, "used_percent": 10, "remaining_percent": 90, "resets_at": None}]},
                {"provider": "claude", "account_id": "account-a", "display_name": "Claude Code", "collection_mode": "automatic", "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official", "last_updated": now_iso(), "status": "ok", "has_reliable_quota": True, "windows": [{"name": "five_hour", "duration_minutes": 300, "used_percent": 10, "remaining_percent": 90, "resets_at": None}]},
            ]
        }

        with patch("manager.command_watcher.read_drive_status", return_value=doc_without_account_b),              patch("manager.command_watcher._claude_account_registry", return_value=registry),              patch("manager.command_watcher.validate_task_enforcement", return_value=True),              patch("manager.command_watcher.validate", return_value=True),              patch("manager.command_watcher._policy_satisfied", return_value=True),              patch("manager.command_watcher.launch_task") as mock_launch:
            cmd = {"command_id": "c1", "project_id": "p1", "task_id": "t1", "provider": "claude", "account_id": "account-b", "status": "queued"}
            res = process_command(
                store, object(), cmd,
                claim_factory=lambda *_: Mock(),
                allowlist=frozenset({("p1", "t1")}),
                health_check=lambda: True,
            )
            # Must reject and must NOT call launch_task
            self.assertEqual({"status": "rejected", "reason": "quota_unreliable"}, res)
            mock_launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
