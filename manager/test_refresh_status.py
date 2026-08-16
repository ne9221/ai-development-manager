import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

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
            codex_collector=lambda **_: ({}, {"providers": [provider("codex", new_window, "2026-08-09T01:00:00Z")]}),
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


if __name__ == "__main__":
    unittest.main()
