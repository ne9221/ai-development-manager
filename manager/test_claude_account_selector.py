import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from manager.claude_account_selector import (
    AccountRegistryError, AccountSelectionError, load_claude_accounts,
    resolve_claude_account, select_claude_account,
)


def account(account_id, confidence="official", last_updated="2026-08-15T02:00:00Z", enabled=True, windows=None):
    entry = {"account_id": account_id, "confidence": confidence, "last_updated": last_updated, "enabled": enabled}
    if windows is not None:
        # forecast_account() only treats a record as source_reliable (a
        # prerequisite for has_reliable_quota/dispatchable) when source_type
        # is explicitly "official" -- real production entries always carry
        # this (see quota_reader._summarize_item), so windowed test fixtures
        # must too, or every forecast comes back unreliable regardless of
        # the windows' actual remaining_percent values.
        entry["source_type"] = "official"
        entry["windows"] = windows
        entry["provider"] = "claude"
    return entry


def windows(five_hour, seven_day):
    return [
        {"name": "five_hour", "remaining_percent": five_hour},
        {"name": "seven_day", "remaining_percent": seven_day},
    ]


NOW = datetime(2026, 8, 15, 2, 10, 0, tzinfo=timezone.utc)


class SelectClaudeAccountTests(unittest.TestCase):
    def test_single_reliable_account_is_selected(self):
        self.assertEqual(
            "account-a",
            select_claude_account([account("account-a")], now=NOW),
        )

    def test_both_accounts_unknown_confidence_fails_closed(self):
        accounts = [account("account-a", confidence="unknown"), account("account-b", confidence="unknown")]
        with self.assertRaises(AccountSelectionError) as ctx:
            select_claude_account(accounts, now=NOW)
        self.assertIn("unknown", str(ctx.exception).lower())

    def test_both_accounts_reliable_and_ambiguous_fails_closed_not_random(self):
        accounts = [account("account-a"), account("account-b")]
        with self.assertRaises(AccountSelectionError) as ctx:
            select_claude_account(accounts, now=NOW)
        self.assertIn("account-a", str(ctx.exception))
        self.assertIn("account-b", str(ctx.exception))

    def test_no_enabled_accounts_fails_closed(self):
        accounts = [account("account-a", enabled=False)]
        with self.assertRaises(AccountSelectionError):
            select_claude_account(accounts, now=NOW)

    def test_missing_confidence_field_treated_as_unreliable(self):
        accounts = [{"account_id": "account-a", "last_updated": "2026-08-15T02:00:00Z", "enabled": True}]
        with self.assertRaises(AccountSelectionError):
            select_claude_account(accounts, now=NOW)

    def test_stale_entry_beyond_max_age_is_excluded(self):
        stale = account("account-a", last_updated="2026-08-09T04:14:40Z")
        fresh = account("account-b", last_updated="2026-08-15T01:55:00Z")
        self.assertEqual(
            "account-b",
            select_claude_account([stale, fresh], max_age_seconds=3600, now=NOW),
        )

    def test_both_stale_fails_closed_not_treated_as_reliable(self):
        stale_a = account("account-a", last_updated="2026-08-09T04:14:40Z")
        stale_b = account("account-b", last_updated="2026-08-08T04:14:40Z")
        with self.assertRaises(AccountSelectionError):
            select_claude_account([stale_a, stale_b], max_age_seconds=3600, now=NOW)

    def test_explicit_account_id_bypasses_confidence_heuristic(self):
        # An explicit caller choice is honored even if quota is unknown --
        # this is the intended escape hatch, not a silent fallback.
        accounts = [account("account-a", confidence="unknown"), account("account-b", confidence="unknown")]
        self.assertEqual(
            "account-a",
            select_claude_account(accounts, explicit_account_id="account-a", now=NOW),
        )

    def test_explicit_account_id_not_present_fails_closed(self):
        accounts = [account("account-a")]
        with self.assertRaises(AccountSelectionError):
            select_claude_account(accounts, explicit_account_id="account-missing", now=NOW)

    def test_explicit_account_id_disabled_fails_closed(self):
        accounts = [account("account-a", enabled=False)]
        with self.assertRaises(AccountSelectionError):
            select_claude_account(accounts, explicit_account_id="account-a", now=NOW)

    # -- P0 claude-auth-routing-truth: auth_ready cross-check --

    def test_auth_unready_account_excluded_even_with_reliable_quota(self):
        accounts = [account("account-a"), account("account-b")]
        self.assertEqual(
            "account-b",
            select_claude_account(accounts, auth_ready={"account-a": False, "account-b": True}, now=NOW),
        )

    def test_account_missing_from_auth_ready_mapping_treated_as_not_ready(self):
        # A candidate the auth check never got to (or errored on) must not
        # win by omission -- only an explicit True counts as ready.
        accounts = [account("account-a"), account("account-b")]
        self.assertEqual(
            "account-b",
            select_claude_account(accounts, auth_ready={"account-b": True}, now=NOW),
        )

    def test_all_accounts_auth_unready_fails_closed(self):
        accounts = [account("account-a"), account("account-b")]
        with self.assertRaises(AccountSelectionError):
            select_claude_account(accounts, auth_ready={"account-a": False, "account-b": False}, now=NOW)

    def test_explicit_account_id_bypasses_auth_ready_check(self):
        # Explicit choice is honored as-is (no substitution); the launcher's
        # own preflight is what fails closed on it, not this selector.
        accounts = [account("account-a"), account("account-b")]
        self.assertEqual(
            "account-a",
            select_claude_account(
                accounts, explicit_account_id="account-a",
                auth_ready={"account-a": False, "account-b": True}, now=NOW,
            ),
        )

    def test_auth_ready_none_preserves_pre_fix_behavior(self):
        # Omitting auth_ready entirely (the default) must behave exactly as
        # before this parameter existed.
        self.assertEqual(
            "account-a",
            select_claude_account([account("account-a")], now=NOW),
        )

    # -- exhausted-account single-candidate fast-path bug (2026-08-30) --
    #
    # select_claude_account()'s `len(reliable) == 1: return reliable[0]`
    # shortcut historically returned the sole "reliable" (fresh, known-
    # confidence) candidate WITHOUT ever running it through
    # quota_forecast.forecast_account()'s exhaustion check -- that check
    # only ran in the >=2-candidate ranking branch. Live-reproduced in
    # production: whenever a live auth_ready check (or momentary staleness)
    # excluded one account, leaving exactly one "reliable" candidate whose
    # own quota was ALREADY exhausted on one window, that exhausted account
    # was still returned and then genuinely failed at real launch
    # (authentication_check_failed / provider_error), three times in a row
    # (Commands dispatch-cgate2-r4/r6/r7-20260830...).

    def test_two_candidates_one_exhausted_selects_the_healthy_one(self):
        healthy = account("account-a", windows=windows(74.0, 32.0))
        exhausted_secondary = account("account-b", windows=windows(98.0, 0.0))
        self.assertEqual(
            "account-a",
            select_claude_account([healthy, exhausted_secondary], now=NOW),
        )

    def test_two_candidates_primary_exhausted_selects_the_healthy_one(self):
        exhausted_primary = account("account-a", windows=windows(0.0, 50.0))
        healthy = account("account-b", windows=windows(60.0, 40.0))
        self.assertEqual(
            "account-b",
            select_claude_account([exhausted_primary, healthy], now=NOW),
        )

    def test_single_reliable_candidate_with_secondary_window_exhausted_fails_closed(self):
        # THE BUG: with only one "reliable" candidate, the old fast path
        # returned it unconditionally even though its seven_day window is
        # genuinely exhausted (0% remaining) -- this must now fail closed
        # instead, exactly like the >=2-candidate branch already does.
        exhausted = account("account-a", windows=windows(98.0, 0.0))
        with self.assertRaises(AccountSelectionError):
            select_claude_account([exhausted], now=NOW)

    def test_single_reliable_candidate_with_primary_window_exhausted_fails_closed(self):
        exhausted = account("account-a", windows=windows(0.0, 40.0))
        with self.assertRaises(AccountSelectionError):
            select_claude_account([exhausted], now=NOW)

    def test_single_reliable_candidate_healthy_still_selected(self):
        # Must not regress the common single-account case: no exhausted
        # window at all is still selected via the fast path.
        healthy = account("account-a", windows=windows(74.0, 32.0))
        self.assertEqual("account-a", select_claude_account([healthy], now=NOW))

    def test_single_reliable_candidate_no_windows_preserves_legacy_behavior(self):
        # An account entry with no windows at all (legacy/manual collection,
        # never captured any quota data) must keep behaving exactly as
        # before this fix -- forecast_account() cannot meaningfully assess
        # "exhausted" with zero windows, so this must not newly start
        # failing closed on accounts that simply have no quota data yet.
        self.assertEqual(
            "account-a",
            select_claude_account([account("account-a")], now=NOW),
        )

    def test_both_candidates_exhausted_fails_closed(self):
        exhausted_a = account("account-a", windows=windows(98.0, 0.0))
        exhausted_b = account("account-b", windows=windows(0.0, 40.0))
        with self.assertRaises(AccountSelectionError):
            select_claude_account([exhausted_a, exhausted_b], now=NOW)

    def test_auth_ready_narrowing_to_one_exhausted_candidate_still_fails_closed(self):
        # The exact production sequence: two accounts start reliable, a live
        # auth_ready check excludes one, leaving exactly one candidate --
        # which must still be exhaustion-checked, not fast-pathed through.
        healthy_but_unauthed = account("account-a", windows=windows(74.0, 32.0))
        exhausted_but_authed = account("account-b", windows=windows(98.0, 0.0))
        with self.assertRaises(AccountSelectionError):
            select_claude_account(
                [healthy_but_unauthed, exhausted_but_authed],
                auth_ready={"account-a": False, "account-b": True}, now=NOW,
            )


def quota_doc(*claude_entries):
    return {"schema_version": "0.1.0", "generated_at": "2026-08-15T02:00:00Z", "providers": list(claude_entries)}


def claude_entry(account_id, confidence="official", last_updated="2026-08-15T02:00:00Z"):
    return {"provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic",
            "source": "test", "source_type": "official", "confidence": confidence,
            "last_updated": last_updated, "status": "ok" if confidence != "unknown" else "unknown",
            "windows": [], "account_id": account_id}


class LoadClaudeAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "claude_accounts.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, obj):
        self.path.write_text(json.dumps(obj), encoding="utf-8")

    def test_missing_file_returns_empty_list_not_an_error(self):
        self.assertEqual([], load_claude_accounts(self.path / "does-not-exist.json"))

    def test_valid_registry_loads_both_accounts(self):
        self.write({"accounts": [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]})
        accounts = load_claude_accounts(self.path)
        self.assertEqual(["account-a", "account-b"], [a["account_id"] for a in accounts])
        self.assertIsNone(accounts[0]["config_dir"])
        self.assertEqual(r"C:\accounts\b\.claude", accounts[1]["config_dir"])

    def test_missing_config_dir_key_rejected(self):
        self.write({"accounts": [{"account_id": "account-b", "enabled": True}]})
        with self.assertRaises(AccountRegistryError) as ctx:
            load_claude_accounts(self.path)
        self.assertIn("config_dir", str(ctx.exception))

    def test_credential_shaped_key_rejected(self):
        self.write({"accounts": [
            {"account_id": "account-b", "enabled": True, "config_dir": "x", "access_token": "should-not-be-here"},
        ]})
        with self.assertRaises(AccountRegistryError) as ctx:
            load_claude_accounts(self.path)
        self.assertIn("credential-shaped", str(ctx.exception))

    def test_duplicate_account_id_rejected(self):
        self.write({"accounts": [
            {"account_id": "account-a", "config_dir": None},
            {"account_id": "account-a", "config_dir": "x"},
        ]})
        with self.assertRaises(AccountRegistryError):
            load_claude_accounts(self.path)

    def test_malformed_json_rejected(self):
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(AccountRegistryError):
            load_claude_accounts(self.path)


class ResolveClaudeAccountTests(unittest.TestCase):
    def setUp(self):
        self.registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]

    def test_explicit_account_a_resolved_with_its_registry_config_dir(self):
        result = resolve_claude_account(self.registry, quota_doc(), explicit_account_id="account-a", now=NOW)
        self.assertEqual({"account_id": "account-a", "config_dir": None}, result)

    def test_explicit_account_b_resolved_with_its_registry_config_dir(self):
        result = resolve_claude_account(self.registry, quota_doc(), explicit_account_id="account-b", now=NOW)
        self.assertEqual({"account_id": "account-b", "config_dir": r"C:\accounts\b\.claude"}, result)

    def test_unknown_explicit_account_rejected(self):
        with self.assertRaises(AccountSelectionError):
            resolve_claude_account(self.registry, quota_doc(), explicit_account_id="account-ghost", now=NOW)

    def test_disabled_account_rejected(self):
        registry = [{"account_id": "account-a", "enabled": False, "config_dir": None}]
        with self.assertRaises(AccountSelectionError):
            resolve_claude_account(registry, quota_doc(), explicit_account_id="account-a", now=NOW)

    def test_both_accounts_quota_unknown_rejected(self):
        document = quota_doc(claude_entry("account-a", confidence="unknown"), claude_entry("account-b", confidence="unknown"))
        with self.assertRaises(AccountSelectionError):
            resolve_claude_account(self.registry, document, now=NOW)

    def test_no_quota_document_at_all_is_treated_as_unknown_and_rejected(self):
        with self.assertRaises(AccountSelectionError):
            resolve_claude_account(self.registry, None, now=NOW)

    def test_single_reliable_account_auto_selected(self):
        document = quota_doc(claude_entry("account-a", confidence="official"))
        result = resolve_claude_account(self.registry, document, now=NOW)
        self.assertEqual("account-a", result["account_id"])
        self.assertIsNone(result["config_dir"])

    def test_two_reliable_accounts_ambiguous_rejected(self):
        document = quota_doc(claude_entry("account-a"), claude_entry("account-b"))
        with self.assertRaises(AccountSelectionError):
            resolve_claude_account(self.registry, document, now=NOW)

    def test_two_reliable_accounts_higher_remaining_selected(self):
        w_a = [{"name": "five_hour", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": "2026-08-15T05:00:00Z"}]
        w_b = [{"name": "five_hour", "remaining_percent": 20.0, "used_percent": 80.0, "resets_at": "2026-08-15T05:00:00Z"}]
        entry_a = {**claude_entry("account-a"), "windows": w_a}
        entry_b = {**claude_entry("account-b"), "windows": w_b}
        document = quota_doc(entry_a, entry_b)
        result = resolve_claude_account(self.registry, document, now=NOW)
        self.assertEqual("account-a", result["account_id"])

    # -- P0 claude-auth-routing-truth: check_auth_ready cross-check --

    def test_check_auth_ready_excludes_stale_account_despite_equal_quota_confidence(self):
        document = quota_doc(claude_entry("account-a"), claude_entry("account-b"))
        calls = []

        def check_auth_ready(candidate):
            calls.append(candidate["account_id"])
            return candidate["account_id"] == "account-b"

        result = resolve_claude_account(self.registry, document, now=NOW, check_auth_ready=check_auth_ready)
        self.assertEqual("account-b", result["account_id"])
        self.assertEqual(r"C:\accounts\b\.claude", result["config_dir"])
        self.assertEqual({"account-a", "account-b"}, set(calls))

    def test_check_auth_ready_both_unready_fails_closed(self):
        document = quota_doc(claude_entry("account-a"), claude_entry("account-b"))
        with self.assertRaises(AccountSelectionError):
            resolve_claude_account(self.registry, document, now=NOW, check_auth_ready=lambda candidate: False)

    def test_check_auth_ready_not_consulted_for_explicit_account_id(self):
        document = quota_doc(claude_entry("account-a"))

        def check_auth_ready(candidate):
            raise AssertionError("must not be called for an explicit account_id request")

        result = resolve_claude_account(self.registry, document, explicit_account_id="account-a", now=NOW,
                                        check_auth_ready=check_auth_ready)
        self.assertEqual("account-a", result["account_id"])

    def test_two_reliable_accounts_reset_waste_risk_priority(self):
        # A: 80% remaining, resets in 1h (waste risk -> URGENT/SUGGEST_CONSUME)
        w_a = [{"name": "five_hour", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": "2026-08-15T03:00:00Z"}]
        # B: 80% remaining, resets in 10h (NORMAL_USE)
        w_b = [{"name": "five_hour", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": "2026-08-15T12:00:00Z"}]
        entry_a = {**claude_entry("account-a"), "windows": w_a}
        entry_b = {**claude_entry("account-b"), "windows": w_b}
        h_a = {**claude_entry("account-a", last_updated="2026-08-15T01:00:00Z"), "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": "2026-08-15T03:00:00Z"}]}
        h_b = {**claude_entry("account-b", last_updated="2026-08-15T01:00:00Z"), "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": "2026-08-15T12:00:00Z"}]}
        document = quota_doc(entry_a, entry_b)
        result = resolve_claude_account(self.registry, document, history=[h_a, h_b], now=NOW)
        self.assertEqual("account-a", result["account_id"])

    def test_two_reliable_accounts_likely_exhaust_demoted(self):
        # A: 80% remaining, but burns at 50%/h with reset in 3h -> exhausts in 1.6h -> CONSERVE
        w_a = [{"name": "five_hour", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": "2026-08-15T05:00:00Z"}]
        h_a = {**claude_entry("account-a", last_updated="2026-08-15T01:00:00Z"), "windows": [{"name": "five_hour", "remaining_percent": 130.0, "resets_at": "2026-08-15T05:00:00Z"}]}  # delta 50%
        # B: 40% remaining, burns at 5%/h with reset in 3h -> healthy -> NORMAL_USE
        w_b = [{"name": "five_hour", "remaining_percent": 40.0, "used_percent": 60.0, "resets_at": "2026-08-15T05:00:00Z"}]
        h_b = {**claude_entry("account-b", last_updated="2026-08-15T01:00:00Z"), "windows": [{"name": "five_hour", "remaining_percent": 45.0, "resets_at": "2026-08-15T05:00:00Z"}]}
        document = quota_doc({**claude_entry("account-a"), "windows": w_a}, {**claude_entry("account-b"), "windows": w_b})
        result = resolve_claude_account(self.registry, document, history=[h_a, h_b], now=NOW)
        self.assertEqual("account-b", result["account_id"])

    def test_multi_window_conserve_veto_in_account_selection(self):
        # Account A: 5h suggests consume (80%, reset 1h), but 7d requires CONSERVE (20%, reset in 48h, burn 2%/h)
        w_a_5h = {"name": "five_hour", "remaining_percent": 80.0, "used_percent": 20.0, "resets_at": "2026-08-15T03:00:00Z"}
        w_a_7d = {"name": "seven_day", "remaining_percent": 20.0, "used_percent": 80.0, "resets_at": "2026-08-17T02:00:00Z"}
        h_a = {**claude_entry("account-a", last_updated="2026-08-15T01:00:00Z"), "windows": [
            {"name": "five_hour", "remaining_percent": 90.0, "resets_at": "2026-08-15T03:00:00Z"},
            {"name": "seven_day", "remaining_percent": 22.0, "resets_at": "2026-08-17T02:00:00Z"},
        ]}
        # Account B: 5h normal (50%, reset in 4h), 7d healthy (60%, reset in 48h)
        w_b_5h = {"name": "five_hour", "remaining_percent": 50.0, "used_percent": 50.0, "resets_at": "2026-08-15T06:00:00Z"}
        w_b_7d = {"name": "seven_day", "remaining_percent": 60.0, "used_percent": 40.0, "resets_at": "2026-08-17T02:00:00Z"}
        h_b = {**claude_entry("account-b", last_updated="2026-08-15T01:00:00Z"), "windows": [
            {"name": "five_hour", "remaining_percent": 55.0, "resets_at": "2026-08-15T06:00:00Z"},
            {"name": "seven_day", "remaining_percent": 61.0, "resets_at": "2026-08-17T02:00:00Z"},
        ]}
        entry_a = {**claude_entry("account-a"), "windows": [w_a_5h, w_a_7d]}
        entry_b = {**claude_entry("account-b"), "windows": [w_b_5h, w_b_7d]}
        document = quota_doc(entry_a, entry_b)
        result = resolve_claude_account(self.registry, document, history=[h_a, h_b], now=NOW)
        self.assertEqual("account-b", result["account_id"])



class ResolvedAccountReachesRealClaudeLauncherTests(unittest.TestCase):
    """Closes the loop end-to-end with the real (non-double) ClaudeLauncher:
    registry + quota document -> resolve_claude_account() -> its output
    fed directly as ClaudeLauncher.prepare()'s account_id/config_dir kwargs
    -> the real subprocess.Popen call the child would actually receive."""

    def setUp(self):
        from manager.claude_launcher import ClaudeLauncher
        from manager.codex_launcher import LaunchRequest

        self.ClaudeLauncher = ClaudeLauncher
        self.LaunchRequest = LaunchRequest
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []

        class FakeProcess:
            def __init__(self):
                import os as _os
                self.pid = _os.getpid()
                self.returncode = None
                self.stdin = None

            def poll(self):
                return self.returncode

        self.FakeProcess = FakeProcess

    def tearDown(self):
        self.temp.cleanup()

    def _popen(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self.FakeProcess()

    def test_selected_second_account_config_dir_reaches_child_popen_env(self):
        registry = [
            {"account_id": "account-a", "enabled": True, "config_dir": None},
            {"account_id": "account-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        document = quota_doc(claude_entry("account-b", confidence="official"))
        resolved = resolve_claude_account(registry, document, now=NOW)
        self.assertEqual({"account_id": "account-b", "config_dir": r"C:\accounts\b\.claude"}, resolved)

        launcher = self.ClaudeLauncher(executable=__file__, popen=self._popen, log_dir=self.temp.name,
                                       auth_check=lambda *a, **k: True)
        request = self.LaunchRequest(self.temp.name, sandbox="read-only", approval_policy="never")
        prepared = launcher.prepare(request, **resolved)

        env = self.calls[-1]["env"]
        self.assertEqual(r"C:\accounts\b\.claude", env["CLAUDE_CONFIG_DIR"])
        self.assertEqual("account-b", prepared.account_id)

    def test_selected_default_account_leaves_child_env_untouched(self):
        registry = [{"account_id": "account-a", "enabled": True, "config_dir": None}]
        document = quota_doc(claude_entry("account-a", confidence="official"))
        resolved = resolve_claude_account(registry, document, now=NOW)

        launcher = self.ClaudeLauncher(executable=__file__, popen=self._popen, log_dir=self.temp.name,
                                       auth_check=lambda *a, **k: True)
        request = self.LaunchRequest(self.temp.name, sandbox="read-only", approval_policy="never")
        launcher.prepare(request, **resolved)

        self.assertIsNone(self.calls[-1]["env"])


if __name__ == "__main__":
    unittest.main()
