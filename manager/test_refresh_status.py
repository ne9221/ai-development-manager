import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from manager import refresh_status
from manager.refresh_status import RefreshError, refresh, runtime_lock


def provider(name, windows=None, updated="2026-08-09T00:00:00Z"):
    return {
        "provider": name,
        "display_name": name.title(),
        "collection_mode": "automatic" if name in ("codex", "claude") else "manual",
        "source": "test",
        "source_type": "official" if name in ("codex", "claude") else "manual",
        "confidence": "official" if windows else "unknown",
        "last_updated": updated,
        "status": "ok" if windows else "unknown",
        "windows": windows or [],
    }


def status():
    window = [{"name": "primary", "used_percent": 20, "remaining_percent": 80, "resets_at": None}]
    return {"schema_version": "0.1.0", "generated_at": "2026-08-09T00:00:00Z", "providers": [
        provider("codex", window), provider("claude"), provider("antigravity"), provider("gemini_app")
    ]}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_refresh(self, **overrides):
        old = status()
        published = []
        new_window = [{"name": "primary", "used_percent": 5, "remaining_percent": 95, "resets_at": None}]
        defaults = dict(
            service=object(), runtime_path=self.base / "status.json", log_path=self.base / "refresh.log",
            lock_path=self.base / "refresh.lock", claude_path=self.base / "missing.json",
            reader=lambda **_: deepcopy(old),
            codex_collector=lambda **_: ({}, {"providers": [provider("codex", new_window, "2026-08-30T01:00:00Z")]}),
            publisher=lambda service, path: published.append(json.loads(path.read_text())) or {"action": "updated", "id": "1"},
        )
        defaults.update(overrides)
        return refresh(**defaults), old, published

    def test_codex_success_preserves_other_providers(self):
        result, _, published = self.run_refresh()
        current = {item["provider"]: item for item in result["document"]["providers"]}
        self.assertEqual(95, current["codex"]["windows"][0]["remaining_percent"])
        self.assertEqual([], current["claude"]["windows"])
        self.assertEqual(1, len(published))

    def test_unavailable_provider_preserves_old_value(self):
        def unavailable(**_):
            raise RuntimeError("offline")
        result, old, _ = self.run_refresh(codex_collector=unavailable)
        self.assertEqual("unavailable", result["providers"]["codex"])
        self.assertEqual(old["providers"][0], next(x for x in result["document"]["providers"] if x["provider"] == "codex"))

    def test_schema_failure_does_not_publish(self):
        called = []
        def invalid(*_):
            raise ValueError("bad schema")
        with self.assertRaises(RefreshError):
            self.run_refresh(validator=invalid, publisher=lambda *_: called.append(True))
        self.assertEqual([], called)

    def test_publisher_failure_does_not_change_remote_fixture(self):
        remote = status()
        def fail(*_):
            raise RuntimeError("Drive unavailable")
        with self.assertRaises(RefreshError):
            self.run_refresh(publisher=fail)
        self.assertEqual(status(), remote)

    def test_empty_claude_capture_preserves_official_snapshot(self):
        old = status()
        old["providers"][1] = provider("claude", [{"name": "seven_day", "used_percent": 50, "remaining_percent": 50, "resets_at": None}])
        self.base.joinpath("claude.json").write_text("{}", encoding="utf-8")
        result, _, _ = self.run_refresh(reader=lambda **_: deepcopy(old), claude_path=self.base / "claude.json")
        claude = next(x for x in result["document"]["providers"] if x["provider"] == "claude")
        self.assertEqual(50, claude["windows"][0]["remaining_percent"])

    def test_claude_never_official_stays_unavailable_without_fabricating(self):
        # Reproduces the real P0.0 finding: statusLine is unreachable from
        # both headless ClaudeLauncher dispatch and Desktop-app sessions, so
        # the payload file never gets rate_limits. Refresh must report
        # "unavailable" and must not fabricate a percentage or bump
        # last_updated on the untouched claude entry.
        result, old, _ = self.run_refresh()
        self.assertEqual("unavailable", result["providers"]["claude"])
        claude = next(x for x in result["document"]["providers"] if x["provider"] == "claude")
        self.assertEqual([], claude["windows"])
        self.assertEqual("unknown", claude["confidence"])
        self.assertEqual(old["providers"][1]["last_updated"], claude["last_updated"])

    def test_second_claude_account_captured_independently_no_cross_contamination(self):
        payload_b = self.base / "claude-b.json"
        payload_b.write_text(json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": 12, "resets_at": None},
        }}), encoding="utf-8")
        result, _, _ = self.run_refresh(claude_accounts={"account-b": payload_b})
        self.assertEqual("unavailable", result["providers"]["claude"])
        self.assertEqual("success", result["providers"]["claude:account-b"])
        claude_entries = [x for x in result["document"]["providers"] if x["provider"] == "claude"]
        self.assertEqual(2, len(claude_entries))
        default_entry = next(x for x in claude_entries if x.get("account_id") is None)
        account_b_entry = next(x for x in claude_entries if x.get("account_id") == "account-b")
        self.assertEqual([], default_entry["windows"])
        self.assertEqual(88, account_b_entry["windows"][0]["remaining_percent"])

    def test_two_claude_accounts_do_not_overwrite_each_others_entry_on_republish(self):
        old = status()
        old["providers"][1] = provider("claude", [{"name": "seven_day", "used_percent": 50, "remaining_percent": 50, "resets_at": None}])
        old["providers"].append({**provider("claude", [{"name": "five_hour", "used_percent": 10, "remaining_percent": 90, "resets_at": None}]), "account_id": "account-b"})
        result, _, _ = self.run_refresh(reader=lambda **_: deepcopy(old))
        claude_entries = [x for x in result["document"]["providers"] if x["provider"] == "claude"]
        self.assertEqual(2, len(claude_entries))
        account_b_entry = next(x for x in claude_entries if x.get("account_id") == "account-b")
        self.assertEqual(90, account_b_entry["windows"][0]["remaining_percent"])

    def test_overlapping_refresh_is_blocked(self):
        with runtime_lock(self.base / "refresh.lock"):
            with self.assertRaises(RefreshError):
                with runtime_lock(self.base / "refresh.lock"):
                    pass


    def test_refresh_appends_to_history_store(self):
        from manager.quota_history import QuotaHistoryStore
        history_path = self.base / "quota_history.json"
        store = QuotaHistoryStore(history_path)
        payload_b = self.base / "claude-b.json"
        payload_b.write_text(json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": "2026-08-17T05:00:00Z"},
        }}), encoding="utf-8")
        result, _, _ = self.run_refresh(
            claude_accounts={"account-b": payload_b},
            history_store=store,
        )
        history = store.get_history()
        self.assertTrue(len(history) >= 2) # Codex and Claude:account-b
        codex_h = store.get_history(provider="codex")
        claude_b_h = store.get_history(provider="claude", account_id="account-b")
        self.assertEqual(1, len(codex_h))
        self.assertEqual(1, len(claude_b_h))
        self.assertEqual(90.0, claude_b_h[0]["windows"][0]["remaining_percent"])

    def test_refresh_history_store_failure_is_fail_safe(self):
        class BrokenStore:
            def append_snapshot(self, *_, **__):
                raise IOError("disk failure")
        # Should not raise RefreshError when history store fails
        result, _, published = self.run_refresh(history_store=BrokenStore())
        self.assertEqual(1, len(published))
        codex_entry = next(x for x in result["document"]["providers"] if x["provider"] == "codex")
        self.assertEqual(95, codex_entry["windows"][0]["remaining_percent"])


class MainObservabilityTests(unittest.TestCase):
    """Covers the 2026-08-24 scheduler-gap investigation finding: a
    RefreshError raised by runtime_lock() contention (or any other
    exception from main()'s own setup, e.g. build_service()) happens
    before refresh() ever writes "refresh start", and previously was
    only printed to stderr -- which the hidden wscript.exe scheduled-task
    wrapper discards entirely, leaving zero trace in refresh.log."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.log_path = self.base / "logs" / "refresh.log"
        self._env_patch = mock.patch.dict(os.environ, {"AI_MANAGER_HOME": str(self.base)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.temp.cleanup()

    def test_lock_contention_failure_is_logged(self):
        lock_path = self.base / "refresh.lock"
        with runtime_lock(lock_path), \
             mock.patch("manager.refresh_status.build_service", side_effect=RefreshError("another refresh is already running")):
            self.assertEqual(1, refresh_status.main())
        self.assertIn("refresh failed before start: RefreshError", self.log_path.read_text(encoding="utf-8"))

    def test_setup_failure_is_logged(self):
        with mock.patch("manager.refresh_status.build_service", side_effect=RuntimeError("Drive auth unreachable")):
            self.assertEqual(1, refresh_status.main())
        self.assertIn("refresh initialization failed: RuntimeError", self.log_path.read_text(encoding="utf-8"))




class ClaudeRefreshDiagnosticTests(RefreshTests):
    """Live 20260902: account-a was STALE for 12+ hours while refresh.log said
    only `claude oauth unavailable: CredentialsUnavailableError`. The real
    cause -- an EMPTY accessToken in the default ~/.claude credentials file
    (the account needs a re-login) -- must be visible in the log and on the
    entry itself, without ever touching the last-good numbers."""

    def _old_with_account_a(self):
        old = status()
        account_a = deepcopy(next(p for p in old["providers"] if p["provider"] == "claude"))
        account_a.update({
            "account_id": "account-a", "confidence": "official", "status": "ok",
            "last_updated": "2026-09-01T14:41:18Z",
            "windows": [{"name": "five_hour", "used_percent": 0, "remaining_percent": 100, "resets_at": None}],
        })
        old["providers"].append(account_a)
        return old

    def test_oauth_unavailable_reason_is_logged_and_recorded_on_the_entry(self):
        from collectors.claude_oauth import CredentialsUnavailableError

        old = self._old_with_account_a()

        def failing_collector(config_dir, account_id, timeout=15):
            raise CredentialsUnavailableError("access token missing from credentials")

        result, _, _ = self.run_refresh(
            reader=lambda **_: deepcopy(old),
            claude_accounts={"account-a": self.base / "missing-a.json"},
            claude_config_dirs={None: None, "account-a": None},
            claude_oauth_collector=failing_collector,
        )
        log = (self.base / "refresh.log").read_text(encoding="utf-8")
        self.assertIn("claude oauth unavailable: CredentialsUnavailableError: access token missing from credentials", log)
        # No statusline fallback payload exists in this harness, so the
        # outcome is "unavailable" (production, where the old payload file
        # exists, logs "unchanged"); either way the reason rides on the line.
        self.assertRegex(log, r"provider claude:account-a (unchanged|unavailable) \(CredentialsUnavailableError: access token missing from credentials\)")
        entry = next(p for p in result["document"]["providers"]
                     if p["provider"] == "claude" and p.get("account_id") == "account-a")
        # Last-good numbers and their real timestamp are untouched ...
        self.assertEqual("2026-09-01T14:41:18Z", entry["last_updated"])
        self.assertEqual(100, entry["windows"][0]["remaining_percent"])
        # ... but the entry now says WHY it did not refresh.
        self.assertIn(entry["metadata"]["refresh"]["outcome"], ("unchanged", "unavailable"))
        self.assertEqual("CredentialsUnavailableError: access token missing from credentials",
                         entry["metadata"]["refresh"]["error"])
        self.assertTrue(entry["metadata"]["refresh"]["attempted_at"].endswith("Z"))
        self.assertNotIn("token", entry["metadata"]["refresh"]["error"].split(":")[0].lower())

    def test_successful_refresh_replaces_the_diagnostic(self):
        old = self._old_with_account_a()
        old["providers"][-1]["metadata"] = {"refresh": {"outcome": "unchanged", "error": "old", "attempted_at": "x"}}
        fresh = deepcopy(old["providers"][-1])
        fresh.update({"last_updated": "2026-09-02T02:00:00Z", "metadata": {}})
        fresh["windows"] = [{"name": "five_hour", "used_percent": 10, "remaining_percent": 90, "resets_at": None}]

        result, _, _ = self.run_refresh(
            reader=lambda **_: deepcopy(old),
            claude_accounts={"account-a": self.base / "missing-a.json"},
            claude_config_dirs={"account-a": None},
            claude_oauth_collector=lambda config_dir, account_id, timeout=15: deepcopy(fresh),
        )
        entry = next(p for p in result["document"]["providers"]
                     if p["provider"] == "claude" and p.get("account_id") == "account-a")
        self.assertEqual(90, entry["windows"][0]["remaining_percent"])
        self.assertNotIn("refresh", entry.get("metadata") or {})




class ClaudeRateLimitDiagnosticTests(ClaudeRefreshDiagnosticTests):
    def test_rate_limited_keeps_last_good_and_records_the_diagnostic(self):
        from collectors.claude_oauth import RateLimitedError

        old = self._old_with_account_a()

        def limited_collector(config_dir, account_id, timeout=15):
            raise RateLimitedError(30)

        result, _, _ = self.run_refresh(
            reader=lambda **_: deepcopy(old),
            claude_accounts={"account-a": self.base / "missing-a.json"},
            claude_config_dirs={"account-a": None},
            claude_oauth_collector=limited_collector,
        )
        entry = next(p for p in result["document"]["providers"]
                     if p["provider"] == "claude" and p.get("account_id") == "account-a")
        self.assertEqual("2026-09-01T14:41:18Z", entry["last_updated"])
        self.assertEqual(100, entry["windows"][0]["remaining_percent"])
        self.assertEqual("rate_limited", entry["metadata"]["refresh"]["outcome"])
        self.assertIn("RateLimitedError", entry["metadata"]["refresh"]["error"])
        self.assertIn("retry_after=30", entry["metadata"]["refresh"]["error"])
        log = (self.base / "refresh.log").read_text(encoding="utf-8")
        self.assertIn("provider claude:account-a rate_limited (RateLimitedError: retry_after=30)", log)


if __name__ == "__main__":
    unittest.main()
