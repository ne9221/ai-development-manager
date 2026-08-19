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


class MetadataCreditsSurviveSummarize(unittest.TestCase):
    """summarize() must pass metadata (e.g. Codex's extra-credits balance)
    through unmodified. Both the Dashboard and the real dispatcher
    (manager.dispatcher) build their forecast_account() input from this
    summarized shape -- silently dropping metadata here means the account-
    level credits signal can never reach the shared truth source no matter
    what manager.quota_forecast does with it."""

    def test_codex_credits_metadata_present_in_provider_and_account_output(self):
        item = codex_item(remaining=0)
        item["metadata"] = {"credits": {"hasCredits": True, "unlimited": False, "balance": "813.5882690000"}}
        result = summarize(doc(item), now=NOW)

        codex_provider = next(p for p in result["providers"] if p["provider"] == "codex")
        codex_account = find_account(result, "codex", None)
        self.assertEqual(codex_provider["metadata"]["credits"]["hasCredits"], True)
        self.assertEqual(codex_provider["metadata"]["credits"]["balance"], "813.5882690000")
        self.assertEqual(codex_account["metadata"]["credits"]["hasCredits"], True)

    def test_missing_metadata_summarizes_to_empty_dict_not_missing_key(self):
        result = summarize(doc(codex_item(remaining=90)), now=NOW)
        codex_account = find_account(result, "codex", None)
        self.assertIn("metadata", codex_account)
        self.assertEqual(codex_account["metadata"], {})


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


class DuplicateLegacyNoneAccountIsLastWins(unittest.TestCase):
    """Two account_id=None entries for the same provider (a duplicated
    legacy record) must resolve exactly like the pre-P0.1
    `{item["provider"]: item for item in ...}` dict comprehension: the
    entry that appears LAST in document order wins. This is a deliberate,
    documented rule -- not a claim of input-order independence."""

    def test_last_document_entry_wins_forward_order(self):
        result = summarize(doc(claude_item(None, remaining=11), claude_item(None, remaining=99)), now=NOW)
        claude = next(p for p in result["providers"] if p["provider"] == "claude")
        self.assertEqual(claude["windows"][0]["remaining_percent"], 99)
        accounts = [a for a in result["accounts"] if a["provider"] == "claude"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["windows"][0]["remaining_percent"], 99)

    def test_last_document_entry_wins_reversed_order(self):
        result = summarize(doc(claude_item(None, remaining=99), claude_item(None, remaining=11)), now=NOW)
        claude = next(p for p in result["providers"] if p["provider"] == "claude")
        self.assertEqual(claude["windows"][0]["remaining_percent"], 11)
        accounts = [a for a in result["accounts"] if a["provider"] == "claude"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["windows"][0]["remaining_percent"], 11)


class DuplicateNamedAccountCollapsesToOne(unittest.TestCase):
    """Two entries sharing the same named account_id must collapse to a
    single accounts[] entry -- no duplicate key -- and the surviving
    entry must be the last one in document order, explicitly (not an
    input-order-independent merge)."""

    def test_duplicate_named_account_produces_single_entry(self):
        result = summarize(
            doc(claude_item("claude-a", remaining=5), claude_item("claude-a", remaining=95)), now=NOW,
        )
        matches = [a for a in result["accounts"] if a["provider"] == "claude" and a["account_id"] == "claude-a"]
        self.assertEqual(len(matches), 1, "duplicate (provider, account_id) key must not appear twice")
        self.assertEqual(matches[0]["windows"][0]["remaining_percent"], 95)

    def test_duplicate_named_account_last_wins_is_order_sensitive_by_design(self):
        forward = summarize(doc(claude_item("claude-a", remaining=5), claude_item("claude-a", remaining=95)), now=NOW)
        backward = summarize(doc(claude_item("claude-a", remaining=95), claude_item("claude-a", remaining=5)), now=NOW)
        forward_match = find_account(forward, "claude", "claude-a")
        backward_match = find_account(backward, "claude", "claude-a")
        # This is intentionally NOT equal: last-wins means the last document
        # entry is authoritative, so swapping order swaps the winner.
        self.assertEqual(forward_match["windows"][0]["remaining_percent"], 95)
        self.assertEqual(backward_match["windows"][0]["remaining_percent"], 5)


class DistinctAccountsUnaffectedByDedup(unittest.TestCase):
    """Two different named accounts (no shared key) must both survive and
    the resulting account map must be order-independent, since there is
    no duplicate key to resolve."""

    def test_two_distinct_accounts_both_retained_regardless_of_order(self):
        forward = summarize(doc(claude_item("A", remaining=70), claude_item("B", remaining=20)), now=NOW)
        backward = summarize(doc(claude_item("B", remaining=20), claude_item("A", remaining=70)), now=NOW)
        key = lambda a: (a["provider"], a["account_id"])
        self.assertEqual(sorted(forward["accounts"], key=key), sorted(backward["accounts"], key=key))


class DuplicateDoesNotBlendFields(unittest.TestCase):
    """A duplicate pair must resolve to exactly one whole source record --
    never a Frankenstein record with fields mixed from both entries."""

    def test_official_and_unknown_duplicate_does_not_fabricate_quota(self):
        result = summarize(
            doc(
                claude_item("claude-a", remaining=60, confidence="official", source_type="official"),
                claude_item("claude-a", remaining=None, confidence="unknown", source_type="manual", status="unknown"),
            ),
            now=NOW,
        )
        matches = [a for a in result["accounts"] if a["provider"] == "claude" and a["account_id"] == "claude-a"]
        self.assertEqual(len(matches), 1)
        winner = matches[0]
        # The last entry (unknown/manual) must win wholesale -- not a mix
        # of the first entry's "official" confidence with the second's
        # missing quota, and not the reverse.
        self.assertEqual(winner["confidence"], "unknown")
        self.assertEqual(winner["source_type"], "manual")
        self.assertFalse(winner["source_reliable"])
        self.assertFalse(winner["has_reliable_quota"])

    def test_stale_and_fresh_duplicate_does_not_concatenate_fields(self):
        stale_time = NOW.replace(hour=0)
        result = summarize(
            doc(
                claude_item("claude-a", remaining=60, updated=stale_time),
                claude_item("claude-a", remaining=80, updated=NOW),
            ),
            now=NOW,
        )
        matches = [a for a in result["accounts"] if a["provider"] == "claude" and a["account_id"] == "claude-a"]
        self.assertEqual(len(matches), 1)
        winner = matches[0]
        self.assertFalse(winner["stale"])
        self.assertEqual(winner["windows"][0]["remaining_percent"], 80)
        self.assertEqual(winner["last_updated"], NOW.isoformat())


class MissingAccountIdKeyDedupesWithExplicitNone(unittest.TestCase):
    """An entry with the account_id key entirely absent must be treated as
    the same logical key as an explicit account_id=None entry, including
    for duplicate resolution."""

    def test_missing_key_and_explicit_none_collapse_to_one_last_wins(self):
        with_key = claude_item(None, remaining=11)
        missing_key = claude_item(None, remaining=99)
        del missing_key["account_id"]
        result = summarize(doc(with_key, missing_key), now=NOW)
        accounts = [a for a in result["accounts"] if a["provider"] == "claude"]
        self.assertEqual(len(accounts), 1, "missing account_id must dedupe with explicit None, not add a second entry")
        self.assertEqual(accounts[0]["windows"][0]["remaining_percent"], 99)


if __name__ == "__main__":
    unittest.main()
