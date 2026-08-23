import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collectors.claude_oauth import AuthStaleError, RateLimitedError
from manager.claude_oauth_cooldown import CORRUPT_STATE_COOLDOWN_SECONDS, GLOBAL_QUARANTINE_KEY, CooldownStore
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


def oauth_provider(account_id=None, used_percent=5, updated="2026-08-23T12:00:00Z"):
    return {
        "provider": "claude",
        "account_id": account_id,
        "display_name": "Claude Code",
        "collection_mode": "automatic",
        "source": "claude_oauth_usage",
        "source_type": "official",
        "confidence": "official",
        "last_updated": updated,
        "status": "ok",
        "windows": [{
            "name": "five_hour", "duration_minutes": 300,
            "used_percent": used_percent, "remaining_percent": 100 - used_percent, "resets_at": None,
        }],
        "metadata": {"official_rate_limits_available": True, "missing_windows": []},
    }


class ScriptedOauthCollector:
    """Test double for collect_claude_oauth. `behaviors_by_key` maps a
    credential key (config_dir string, or "<default>" for a falsy
    config_dir) to a list of behaviors consumed in call order: either an
    Exception instance to raise, or a provider dict to return. Any call
    beyond what was scripted for its key raises AssertionError, so tests
    can assert "zero HTTP calls" simply by scripting nothing."""

    def __init__(self, behaviors_by_key):
        self.behaviors_by_key = {k: list(v) for k, v in behaviors_by_key.items()}
        self.calls = []

    def __call__(self, config_dir, account_id, timeout=15):
        key = str(config_dir) if config_dir else "<default>"
        self.calls.append(key)
        queue = self.behaviors_by_key.get(key, [])
        if not queue:
            raise AssertionError(f"unexpected OAuth HTTP call for credential key={key!r}")
        behavior = queue.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return dict(behavior, account_id=account_id)


class ClaudeOauthCooldownIntegrationTests(unittest.TestCase):
    """Exercises manager.refresh_status.refresh()'s use of
    manager.claude_oauth_cooldown.CooldownStore end-to-end: a real
    CooldownStore backed by a real file, and a ScriptedOauthCollector that
    fails the test the moment an un-scripted (i.e. cooldown-violating) HTTP
    call happens."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_refresh(self, **overrides):
        old = status()
        published = []
        defaults = dict(
            service=object(), runtime_path=self.base / "status.json", log_path=self.base / "refresh.log",
            lock_path=self.base / "refresh.lock", claude_path=self.base / "missing.json",
            reader=lambda **_: deepcopy(old),
            codex_collector=lambda **_: ({}, {"providers": [provider("codex", [], "2026-08-09T01:00:00Z")]}),
            publisher=lambda service, path: published.append(json.loads(path.read_text())) or {"action": "updated", "id": "1"},
            history_store=False,
        )
        defaults.update(overrides)
        return refresh(**defaults), old, published

    def cooldown_path(self):
        return self.base / "claude_oauth_cooldown.json"

    def write_cooldown_state(self, data):
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text(json.dumps(data), encoding="utf-8")

    def test_429_persists_cooldown(self):
        collector = ScriptedOauthCollector({"<default>": [RateLimitedError("120")]})
        store = CooldownStore(self.cooldown_path())
        result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("rate_limited", result["providers"]["claude"])
        retry_until = store.get("<default>")
        self.assertIsNotNone(retry_until)
        self.assertGreater(retry_until, datetime.now(timezone.utc) + timedelta(seconds=100))

    def test_next_refresh_before_expiry_performs_zero_http_calls(self):
        store = CooldownStore(self.cooldown_path())
        store.set_retry_until("<default>", datetime.now(timezone.utc) + timedelta(seconds=300))
        collector = ScriptedOauthCollector({})  # any call fails the test
        result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("rate_limited", result["providers"]["claude"])
        self.assertEqual([], collector.calls)

    def test_restart_new_refresh_instance_still_honors_persisted_cooldown(self):
        original_store = CooldownStore(self.cooldown_path())
        original_store.set_retry_until("<default>", datetime.now(timezone.utc) + timedelta(seconds=300))
        # A brand new CooldownStore object pointed at the same path stands
        # in for a freshly-started process (e.g. the next Scheduled Task
        # invocation) that never saw the original in-memory store.
        restarted_store = CooldownStore(self.cooldown_path())
        collector = ScriptedOauthCollector({})
        result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=restarted_store,
        )
        self.assertEqual("rate_limited", result["providers"]["claude"])
        self.assertEqual([], collector.calls)

    def test_after_expiry_exactly_one_request_allowed(self):
        store = CooldownStore(self.cooldown_path())
        store.set_retry_until("<default>", datetime.now(timezone.utc) - timedelta(seconds=1))
        collector = ScriptedOauthCollector({"<default>": [oauth_provider()]})
        result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("success", result["providers"]["claude"])
        self.assertEqual(["<default>"], collector.calls)

    def test_default_legacy_and_account_a_alias_share_one_request(self):
        payload_a = self.base / "account-a.json"
        payload_a.write_text("{}", encoding="utf-8")
        collector = ScriptedOauthCollector({"<default>": [oauth_provider()]})
        store = CooldownStore(self.cooldown_path())
        result, _, _ = self.run_refresh(
            claude_accounts={"account-a": payload_a},
            claude_config_dirs={None: None, "account-a": None},
            claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("success", result["providers"]["claude"])
        self.assertEqual("success", result["providers"]["claude:account-a"])
        self.assertEqual(1, len(collector.calls))

    def test_account_b_has_independent_cooldown(self):
        payload_b = self.base / "account-b.json"
        payload_b.write_text("{}", encoding="utf-8")
        collector = ScriptedOauthCollector({
            "<default>": [RateLimitedError("300")],
            "/config/account-b": [oauth_provider(account_id="account-b")],
        })
        store = CooldownStore(self.cooldown_path())
        result, _, _ = self.run_refresh(
            claude_accounts={"account-b": payload_b},
            claude_config_dirs={None: None, "account-b": "/config/account-b"},
            claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("rate_limited", result["providers"]["claude"])
        self.assertEqual("success", result["providers"]["claude:account-b"])
        self.assertIsNotNone(store.get("<default>"))
        self.assertIsNone(store.get("/config/account-b"))

    def test_malformed_retry_after_is_bounded_not_unbounded_or_zero(self):
        collector = ScriptedOauthCollector({"<default>": [RateLimitedError("not-a-valid-value")]})
        store = CooldownStore(self.cooldown_path())
        before = datetime.now(timezone.utc)
        self.run_refresh(claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store)
        retry_until = store.get("<default>")
        self.assertIsNotNone(retry_until)
        self.assertGreater(retry_until, before)
        self.assertLess(retry_until, before + timedelta(seconds=120))

    def test_malformed_cooldown_state_fails_closed_without_spamming(self):
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text("{not valid json at all", encoding="utf-8")
        store = CooldownStore(self.cooldown_path())
        collector = ScriptedOauthCollector({})  # any call fails the test
        result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("rate_limited", result["providers"]["claude"])
        self.assertEqual([], collector.calls)
        # A: the corrupt file must have been quarantined into a clean,
        # parseable file under the single global sentinel, not left
        # untouched and not turned into a per-key entry.
        parsed = json.loads(self.cooldown_path().read_text(encoding="utf-8"))
        self.assertEqual([GLOBAL_QUARANTINE_KEY], list(parsed.keys()))

    def test_corrupt_state_self_heals_full_lifecycle(self):
        # A: first refresh against a corrupt file makes zero calls and
        # persists a clean, bounded quarantine cooldown.
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text("{definitely not json", encoding="utf-8")
        first_collector = ScriptedOauthCollector({})
        first_result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=first_collector,
            cooldown_store=CooldownStore(self.cooldown_path()),
        )
        self.assertEqual("rate_limited", first_result["providers"]["claude"])
        self.assertEqual([], first_collector.calls)

        # B: a brand new process (new CooldownStore instance, same file)
        # checking again before the quarantine cooldown expires still makes
        # zero calls -- no permanent lock-out re-arming, but also no spam.
        second_collector = ScriptedOauthCollector({})
        second_result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=second_collector,
            cooldown_store=CooldownStore(self.cooldown_path()),
        )
        self.assertEqual("rate_limited", second_result["providers"]["claude"])
        self.assertEqual([], second_collector.calls)

        # C: once the quarantine cooldown has actually expired, a new
        # process is allowed exactly one real request. (Simulated directly
        # via the state file, since real wall-clock time won't have
        # advanced CORRUPT_STATE_COOLDOWN_SECONDS within a fast test run.)
        self.write_cooldown_state({GLOBAL_QUARANTINE_KEY: {
            "retry_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }})
        third_collector = ScriptedOauthCollector({"<default>": [oauth_provider()]})
        third_result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=third_collector,
            cooldown_store=CooldownStore(self.cooldown_path()),
        )
        self.assertEqual("success", third_result["providers"]["claude"])
        self.assertEqual(["<default>"], third_collector.calls)

        # D: the successful request (post-recovery) leaves no cooldown
        # behind for this credential.
        self.assertIsNone(CooldownStore(self.cooldown_path()).get("<default>"))

    def test_corrupt_state_blocks_every_credential_in_the_same_cycle(self):
        # A corrupt file could have held a still-active Retry-After for
        # *any* credential -- a per-key quarantine that only blocks the
        # credential checked first is fail-open for every other one. Both
        # the default account and account-b must make zero HTTP calls in
        # the same refresh() cycle that discovers the corruption.
        payload_b = self.base / "account-b.json"
        payload_b.write_text("{}", encoding="utf-8")
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text("{corrupt for everyone", encoding="utf-8")
        collector = ScriptedOauthCollector({})  # any call fails the test
        store = CooldownStore(self.cooldown_path())
        result, _, _ = self.run_refresh(
            claude_accounts={"account-b": payload_b},
            claude_config_dirs={None: None, "account-b": "/config/account-b"},
            claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("rate_limited", result["providers"]["claude"])
        self.assertEqual("rate_limited", result["providers"]["claude:account-b"])
        self.assertEqual([], collector.calls)
        parsed = json.loads(self.cooldown_path().read_text(encoding="utf-8"))
        self.assertEqual([GLOBAL_QUARANTINE_KEY], list(parsed.keys()))

    def test_corrupt_state_blocks_second_credential_in_a_new_process(self):
        # B/C/D: the default account's refresh discovers the corruption and
        # arms the global quarantine; a *separate*, later refresh() call
        # (standing in for a fresh Scheduled Task process) checking only
        # account-b -- which was never checked the first time -- is still
        # blocked, because the quarantine is global, not per-key.
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text("{corrupt for everyone", encoding="utf-8")
        first_collector = ScriptedOauthCollector({})
        first_result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=first_collector,
            cooldown_store=CooldownStore(self.cooldown_path()),
        )
        self.assertEqual("rate_limited", first_result["providers"]["claude"])
        self.assertEqual([], first_collector.calls)

        payload_b = self.base / "account-b.json"
        payload_b.write_text("{}", encoding="utf-8")
        second_collector = ScriptedOauthCollector({})
        second_result, _, _ = self.run_refresh(
            claude_accounts={"account-b": payload_b},
            claude_config_dirs={"account-b": "/config/account-b"},
            claude_oauth_collector=second_collector,
            cooldown_store=CooldownStore(self.cooldown_path()),
        )
        self.assertEqual("rate_limited", second_result["providers"]["claude:account-b"])
        self.assertEqual([], second_collector.calls)

    def test_after_global_quarantine_expiry_credentials_resume_independently(self):
        # E: once the global quarantine has expired, each credential goes
        # back to behaving on its own per-key state.
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text("{corrupt for everyone", encoding="utf-8")
        CooldownStore(self.cooldown_path()).get("<default>")  # arms quarantine
        self.write_cooldown_state({GLOBAL_QUARANTINE_KEY: {
            "retry_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }})

        payload_b = self.base / "account-b.json"
        payload_b.write_text("{}", encoding="utf-8")
        collector = ScriptedOauthCollector({
            "<default>": [oauth_provider()],
            "/config/account-b": [oauth_provider(account_id="account-b")],
        })
        result, _, _ = self.run_refresh(
            claude_accounts={"account-b": payload_b},
            claude_config_dirs={None: None, "account-b": "/config/account-b"},
            claude_oauth_collector=collector, cooldown_store=CooldownStore(self.cooldown_path()),
        )
        self.assertEqual("success", result["providers"]["claude"])
        self.assertEqual("success", result["providers"]["claude:account-b"])
        self.assertEqual(2, len(collector.calls))

    def test_real_429_after_quarantine_recovery_persists_only_that_credential(self):
        # F: a genuine 429 that happens after the global quarantine has
        # expired still only affects the credential it was for.
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text("{corrupt for everyone", encoding="utf-8")
        CooldownStore(self.cooldown_path()).get("<default>")
        self.write_cooldown_state({GLOBAL_QUARANTINE_KEY: {
            "retry_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }})

        payload_b = self.base / "account-b.json"
        payload_b.write_text("{}", encoding="utf-8")
        collector = ScriptedOauthCollector({
            "<default>": [oauth_provider()],
            "/config/account-b": [RateLimitedError("300")],
        })
        store = CooldownStore(self.cooldown_path())
        result, _, _ = self.run_refresh(
            claude_accounts={"account-b": payload_b},
            claude_config_dirs={None: None, "account-b": "/config/account-b"},
            claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("success", result["providers"]["claude"])
        self.assertEqual("rate_limited", result["providers"]["claude:account-b"])
        self.assertIsNone(store.get("<default>"))
        self.assertIsNotNone(store.get("/config/account-b"))

    def test_successful_account_a_does_not_clear_account_b_genuine_cooldown(self):
        # G
        payload_a = self.base / "account-a.json"
        payload_a.write_text("{}", encoding="utf-8")
        store = CooldownStore(self.cooldown_path())
        store.set_retry_until("/config/account-b", datetime.now(timezone.utc) + timedelta(seconds=300))
        collector = ScriptedOauthCollector({"/config/account-a": [oauth_provider(account_id="account-a")]})
        result, _, _ = self.run_refresh(
            claude_accounts={"account-a": payload_a},
            claude_config_dirs={"account-a": "/config/account-a"},
            claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("success", result["providers"]["claude:account-a"])
        self.assertIsNotNone(store.get("/config/account-b"))

    def test_global_quarantine_state_contains_no_credential_material(self):
        # H
        self.cooldown_path().parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_path().write_text(
            "{corrupt, accessToken=Bearer sk-ant-fake-leaked-secret", encoding="utf-8",
        )
        store = CooldownStore(self.cooldown_path())
        collector = ScriptedOauthCollector({})
        self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
        )
        raw = self.cooldown_path().read_text(encoding="utf-8")
        self.assertNotIn("accessToken", raw)
        self.assertNotIn("Bearer", raw)
        self.assertNotIn("sk-ant-fake-leaked-secret", raw)

    def test_last_good_last_updated_unchanged_during_cooldown(self):
        old = status()
        old["providers"][1] = provider("claude", [{"name": "seven_day", "used_percent": 40, "remaining_percent": 60, "resets_at": None}], updated="2026-08-20T00:00:00Z")
        store = CooldownStore(self.cooldown_path())
        store.set_retry_until("<default>", datetime.now(timezone.utc) + timedelta(seconds=300))
        collector = ScriptedOauthCollector({})
        result, _, _ = self.run_refresh(
            reader=lambda **_: deepcopy(old),
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
        )
        claude = next(x for x in result["document"]["providers"] if x["provider"] == "claude")
        self.assertEqual("2026-08-20T00:00:00Z", claude["last_updated"])
        self.assertEqual(60, claude["windows"][0]["remaining_percent"])

    def test_401_stays_auth_stale_not_treated_as_quota_data(self):
        collector = ScriptedOauthCollector({"<default>": [AuthStaleError("access token rejected (401)")]})
        store = CooldownStore(self.cooldown_path())
        result, _, _ = self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
        )
        self.assertEqual("unavailable", result["providers"]["claude"])
        self.assertIsNone(store.get("<default>"))

    def test_token_or_credential_material_never_persisted_or_logged(self):
        secret = "sk-ant-oat01-super-secret-token-value"
        collector = ScriptedOauthCollector({"<default>": [RateLimitedError("30")]})
        store = CooldownStore(self.cooldown_path())
        self.run_refresh(
            claude_config_dirs={None: None}, claude_oauth_collector=collector, cooldown_store=store,
            log_path=self.base / "refresh.log",
        )
        cooldown_text = self.cooldown_path().read_text(encoding="utf-8")
        log_text = (self.base / "refresh.log").read_text(encoding="utf-8")
        for blob in (cooldown_text, log_text):
            self.assertNotIn(secret, blob)
            self.assertNotIn("Bearer", blob)
            self.assertNotIn("accessToken", blob)


if __name__ == "__main__":
    unittest.main()
