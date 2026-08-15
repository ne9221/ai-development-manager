import unittest
from datetime import datetime, timezone

from manager.claude_account_selector import AccountSelectionError, select_claude_account


def account(account_id, confidence="official", last_updated="2026-08-15T02:00:00Z", enabled=True):
    return {"account_id": account_id, "confidence": confidence, "last_updated": last_updated, "enabled": enabled}


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


if __name__ == "__main__":
    unittest.main()
