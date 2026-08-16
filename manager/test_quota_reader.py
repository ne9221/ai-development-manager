import unittest
from datetime import datetime, timezone

from manager.quota_reader import summarize


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def claude_item(account_id, remaining=80, confidence="official", source_type="official",
                 source="claude_code_statusline_rate_limits", updated=NOW, status="ok"):
    windows = [] if remaining is None else [{
        "name": "five_hour", "remaining_percent": remaining,
        "used_percent": 100 - remaining, "resets_at": None,
    }]
    return {
        "provider": "claude", "account_id": account_id, "display_name": "Claude Code",
        "collection_mode": "automatic", "source": source, "source_type": source_type,
        "confidence": confidence, "last_updated": updated.isoformat(), "status": status,
        "windows": windows,
    }


def codex_item(remaining=90, updated=NOW):
    return {
        "provider": "codex", "display_name": "Codex", "collection_mode": "automatic",
        "source": "codex_app_server", "source_type": "official", "confidence": "official",
        "last_updated": updated.isoformat(), "status": "ok",
        "windows": [{"name": "seven_day", "remaining_percent": remaining,
                     "used_percent": 100 - remaining, "resets_at": None}],
    }


def doc(*providers):
    return {"generated_at": NOW.isoformat(), "providers": list(providers)}


def find_account(result, provider_id, account_id):
    return next(
        (a for a in result["accounts"] if a["provider"] == provider_id and a["account_id"] == account_id),
        None,
    )


class TwoClaudeAccountsNotOverwritten(unittest.TestCase):
    """Root-cause regression: two (provider='claude', account_id=X) entries
    must both survive summarize(), not collide into one provider-keyed dict."""

    def test_two_claude_accounts_both_present_with_distinct_quota(self):
        result = summarize(doc(claude_item("A", remaining=70), claude_item("B", remaining=20)), now=NOW)
        account_a = find_account(result, "claude", "A")
        account_b = find_account(result, "claude", "B")
        self.assertIsNotNone(account_a, "account A missing from summarize() output -- data loss")
        self.assertIsNotNone(account_b, "account B missing from summarize() output -- data loss")
        self.assertEqual(account_a["windows"][0]["remaining_percent"], 70)
        self.assertEqual(account_b["windows"][0]["remaining_percent"], 20)

    def test_order_independence(self):
        forward = summarize(doc(claude_item("A", remaining=70), claude_item("B", remaining=20)), now=NOW)
        backward = summarize(doc(claude_item("B", remaining=20), claude_item("A", remaining=70)), now=NOW)
        key = lambda a: (a["provider"], a["account_id"])
        self.assertEqual(sorted(forward["accounts"], key=key), sorted(backward["accounts"], key=key))
        self.assertEqual(forward["providers"], backward["providers"])


class LegacySingleClaudeBehaviorPreserved(unittest.TestCase):
    """A single account_id=None Claude entry (the pre-P0.1 shape) must
    produce the exact same provider-level summary as before this change."""

    def test_legacy_single_claude_provider_summary_unchanged(self):
        result = summarize(doc(claude_item(None, remaining=55)), now=NOW)
        claude = next(p for p in result["providers"] if p["provider"] == "claude")
        self.assertEqual(claude["windows"][0]["remaining_percent"], 55)
        self.assertTrue(claude["has_reliable_quota"])
        self.assertFalse(claude["stale"])


class LegacyNoneAccountCompatPriority(unittest.TestCase):
    """When account_id=None coexists with a named account, the None entry
    -- not the named one -- must be the provider-level compatibility
    source, per spec."""

    def test_none_account_wins_over_named_account_for_provider_summary(self):
        result = summarize(
            doc(claude_item(None, remaining=99), claude_item("B", remaining=1)), now=NOW,
        )
        claude = next(p for p in result["providers"] if p["provider"] == "claude")
        self.assertEqual(claude["windows"][0]["remaining_percent"], 99)
        # Both accounts still individually visible.
        self.assertEqual(find_account(result, "claude", None)["windows"][0]["remaining_percent"], 99)
        self.assertEqual(find_account(result, "claude", "B")["windows"][0]["remaining_percent"], 1)

    def test_no_none_account_falls_back_to_deterministic_named_representative(self):
        forward = summarize(doc(claude_item("B", remaining=1), claude_item("A", remaining=99)), now=NOW)
        backward = summarize(doc(claude_item("A", remaining=99), claude_item("B", remaining=1)), now=NOW)
        forward_claude = next(p for p in forward["providers"] if p["provider"] == "claude")
        backward_claude = next(p for p in backward["providers"] if p["provider"] == "claude")
        self.assertEqual(forward_claude, backward_claude)


class ReliabilityNotConflatedAcrossAccounts(unittest.TestCase):
    """One account being official/reliable must not launder another
    account's unknown/unavailable quota into looking reliable."""

    def test_official_and_unknown_accounts_stay_independent(self):
        result = summarize(
            doc(
                claude_item("A", remaining=60, confidence="official", source_type="official"),
                claude_item("B", remaining=None, confidence="unknown", source_type="manual", status="unknown"),
            ),
            now=NOW,
        )
        account_a = find_account(result, "claude", "A")
        account_b = find_account(result, "claude", "B")
        self.assertTrue(account_a["has_reliable_quota"])
        self.assertFalse(account_b["has_reliable_quota"])
        self.assertFalse(account_b["source_reliable"])


class PerAccountStaleIndependence(unittest.TestCase):
    """Staleness must be evaluated per account, not globally per provider."""

    def test_one_stale_one_fresh_account(self):
        stale_time = NOW.replace(hour=0)  # 12 hours before NOW, older than default 60min max_age
        result = summarize(
            doc(claude_item("A", remaining=60, updated=NOW), claude_item("B", remaining=60, updated=stale_time)),
            now=NOW,
        )
        account_a = find_account(result, "claude", "A")
        account_b = find_account(result, "claude", "B")
        self.assertFalse(account_a["stale"])
        self.assertTrue(account_b["stale"])


class CodexSingleAccountBehaviorPreserved(unittest.TestCase):
    """Codex entries never carry account_id; behavior must be untouched."""

    def test_codex_provider_and_account_summary_match(self):
        result = summarize(doc(codex_item(remaining=42)), now=NOW)
        codex_provider = next(p for p in result["providers"] if p["provider"] == "codex")
        codex_account = find_account(result, "codex", None)
        self.assertEqual(codex_provider["windows"][0]["remaining_percent"], 42)
        self.assertIsNotNone(codex_account)
        self.assertEqual(codex_account["windows"][0]["remaining_percent"], 42)
        self.assertTrue(codex_provider["has_reliable_quota"])


class MalformedAccountIdDoesNotCrash(unittest.TestCase):
    """A pre-P0.1 document with account_id entirely absent, or a stray
    non-string account_id, must not raise."""

    def test_missing_account_id_key_treated_as_legacy(self):
        item = claude_item(None, remaining=33)
        del item["account_id"]
        result = summarize(doc(item), now=NOW)
        claude = next(p for p in result["providers"] if p["provider"] == "claude")
        self.assertEqual(claude["windows"][0]["remaining_percent"], 33)

    def test_non_string_account_id_does_not_crash_representative_selection(self):
        result = summarize(
            doc(claude_item(1, remaining=10), claude_item("2", remaining=20)), now=NOW,
        )
        claude = next(p for p in result["providers"] if p["provider"] == "claude")
        self.assertIn(claude["windows"][0]["remaining_percent"], (10, 20))
        self.assertEqual(len([a for a in result["accounts"] if a["provider"] == "claude"]), 2)


if __name__ == "__main__":
    unittest.main()
