import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manager.claude_oauth_cooldown import (
    CORRUPT_STATE_COOLDOWN_SECONDS,
    CooldownStore,
    FALLBACK_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    credential_key,
    parse_retry_after,
)


NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


class ParseRetryAfterTests(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(NOW + timedelta(seconds=120), parse_retry_after("120", now=NOW))

    def test_http_date(self):
        result = parse_retry_after("Sun, 23 Aug 2026 12:05:00 GMT", now=NOW)
        self.assertEqual(NOW + timedelta(minutes=5), result)

    def test_missing_falls_back_to_default(self):
        self.assertEqual(NOW + timedelta(seconds=FALLBACK_COOLDOWN_SECONDS), parse_retry_after(None, now=NOW))

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(NOW + timedelta(seconds=FALLBACK_COOLDOWN_SECONDS), parse_retry_after("not-a-number-or-date", now=NOW))

    def test_negative_value_bounded_to_minimum(self):
        result = parse_retry_after("-500", now=NOW)
        self.assertGreater(result, NOW)
        self.assertLessEqual((result - NOW).total_seconds(), 1)

    def test_absurdly_large_value_bounded_to_maximum(self):
        result = parse_retry_after("99999999", now=NOW)
        self.assertEqual(NOW + timedelta(seconds=MAX_COOLDOWN_SECONDS), result)


class CredentialKeyTests(unittest.TestCase):
    def test_falsy_config_dir_maps_to_default_key(self):
        self.assertEqual(credential_key(None), credential_key(""))
        self.assertEqual("<default>", credential_key(None))

    def test_distinct_config_dirs_map_to_distinct_keys(self):
        self.assertNotEqual(credential_key("/home/a/.claude"), credential_key("/home/b/.claude"))

    def test_same_config_dir_string_or_path_maps_to_same_key(self):
        p = Path("/home/a/.claude")
        self.assertEqual(credential_key(str(p)), credential_key(p))


class CooldownStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "claude_oauth_cooldown.json"
        self.store = CooldownStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_file_means_no_cooldown(self):
        self.assertIsNone(self.store.get("<default>", now=NOW))

    def test_set_then_get_before_expiry_returns_retry_until(self):
        retry_until = NOW + timedelta(seconds=300)
        self.store.set_retry_until("<default>", retry_until)
        self.assertEqual(retry_until, self.store.get("<default>", now=NOW + timedelta(seconds=100)))

    def test_get_after_expiry_returns_none(self):
        retry_until = NOW + timedelta(seconds=300)
        self.store.set_retry_until("<default>", retry_until)
        self.assertIsNone(self.store.get("<default>", now=NOW + timedelta(seconds=301)))

    def test_new_store_instance_same_path_still_honors_cooldown(self):
        # Simulates a restarted process / a new Scheduled Task invocation.
        retry_until = NOW + timedelta(seconds=300)
        self.store.set_retry_until("<default>", retry_until)
        reopened = CooldownStore(self.path)
        self.assertEqual(retry_until, reopened.get("<default>", now=NOW + timedelta(seconds=100)))

    def test_distinct_keys_are_independent(self):
        self.store.set_retry_until("key-a", NOW + timedelta(seconds=300))
        self.assertIsNone(self.store.get("key-b", now=NOW))

    def test_clear_removes_cooldown(self):
        self.store.set_retry_until("<default>", NOW + timedelta(seconds=300))
        self.store.clear("<default>")
        self.assertIsNone(self.store.get("<default>", now=NOW))

    def test_clear_unknown_key_is_a_noop(self):
        self.store.clear("never-set")  # must not raise

    def test_clear_preserves_other_keys(self):
        self.store.set_retry_until("key-a", NOW + timedelta(seconds=300))
        self.store.set_retry_until("key-b", NOW + timedelta(seconds=300))
        self.store.clear("key-a")
        self.assertIsNone(self.store.get("key-a", now=NOW))
        self.assertIsNotNone(self.store.get("key-b", now=NOW))

    def test_malformed_json_fails_closed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        result = self.store.get("<default>", now=NOW)
        self.assertIsNotNone(result)
        self.assertLessEqual((result - NOW).total_seconds(), CORRUPT_STATE_COOLDOWN_SECONDS)

    def test_non_dict_json_fails_closed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNotNone(self.store.get("<default>", now=NOW))

    def test_malformed_json_does_not_raise_on_write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        # A write must self-heal the file rather than propagating the
        # corruption forward or crashing the refresh cycle.
        self.store.set_retry_until("<default>", NOW + timedelta(seconds=60))
        self.assertEqual(NOW + timedelta(seconds=60), self.store.get("<default>", now=NOW))

    def test_stored_state_never_contains_token_like_content(self):
        self.store.set_retry_until("<default>", NOW + timedelta(seconds=60))
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("token", raw.lower())
        self.assertNotIn("credential", raw.lower())
        self.assertNotIn("Bearer", raw)

    # -- Corrupt-state self-heal (fail closed once, then recover, never a
    #    permanent lock-out from the same corruption being rediscovered by
    #    every future process) --

    def test_corrupt_state_quarantines_itself_on_first_get(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        result = self.store.get("<default>", now=NOW)
        self.assertIsNotNone(result)
        # The file must no longer be the original corrupt bytes -- it was
        # atomically replaced with clean, parseable, bounded state.
        raw = self.path.read_text(encoding="utf-8")
        parsed = json.loads(raw)  # must not raise
        self.assertIn("<default>", parsed)

    def test_new_store_before_quarantine_expiry_still_blocks(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        first_retry_until = self.store.get("<default>", now=NOW)
        # A brand new CooldownStore instance over the same (now quarantined,
        # no-longer-corrupt) file stands in for a fresh process.
        reopened = CooldownStore(self.path)
        second = reopened.get("<default>", now=NOW + timedelta(seconds=1))
        self.assertEqual(first_retry_until, second)

    def test_new_store_after_quarantine_expiry_allows_one_request(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("<default>", now=NOW)
        reopened = CooldownStore(self.path)
        after_expiry = NOW + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS + 1)
        self.assertIsNone(reopened.get("<default>", now=after_expiry))

    def test_repeated_corruption_does_not_permanently_lock_out(self):
        # Even if a fresh process re-corrupts the file (e.g. a torn write
        # from an unrelated crash) the quarantine window is still bounded
        # and still expires -- corruption never compounds into forever.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        first = self.store.get("<default>", now=NOW)
        self.path.write_text("also not valid json {{{", encoding="utf-8")
        second_now = NOW + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS + 1)
        second = CooldownStore(self.path).get("<default>", now=second_now)
        self.assertIsNotNone(second)
        self.assertLessEqual((second - second_now).total_seconds(), CORRUPT_STATE_COOLDOWN_SECONDS)

    def test_successful_clear_after_corrupt_state_recovery(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("<default>", now=NOW)  # quarantines, arms cooldown
        after_expiry = NOW + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS + 1)
        self.assertIsNone(self.store.get("<default>", now=after_expiry))  # expired -> caller may proceed
        self.store.clear("<default>")
        self.assertIsNone(self.store.get("<default>", now=after_expiry))

    def test_quarantine_does_not_fabricate_cooldown_for_unrelated_credential(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("key-a", now=NOW)  # quarantines the file for key-a only
        # key-b was never checked against the corrupt file -- it must not
        # inherit a trusted cooldown (nor a fabricated absence-of-one) from
        # key-a's quarantine; the corrupt file never said anything true
        # about key-b, so it must not appear in the quarantined state at
        # all, and a check for it proceeds on its own merits.
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("key-b", raw)
        self.assertIsNone(self.store.get("key-b", now=NOW))

    def test_quarantine_persists_no_token_or_credential_content(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json, leaked accessToken=Bearer sk-ant-fake", encoding="utf-8")
        self.store.get("<default>", now=NOW)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("accessToken", raw)
        self.assertNotIn("Bearer", raw)
        self.assertNotIn("sk-ant-fake", raw)


if __name__ == "__main__":
    unittest.main()
