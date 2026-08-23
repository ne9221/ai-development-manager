import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manager.claude_oauth_cooldown import (
    CORRUPT_STATE_COOLDOWN_SECONDS,
    GLOBAL_QUARANTINE_KEY,
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
        # atomically replaced with clean, parseable, bounded state, under
        # the single global sentinel (not a per-key entry: a corrupt read
        # can't be trusted to have been "only about <default>").
        raw = self.path.read_text(encoding="utf-8")
        parsed = json.loads(raw)  # must not raise
        self.assertEqual([GLOBAL_QUARANTINE_KEY], list(parsed.keys()))

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

    # A corrupt file could have held a still-active Retry-After for *any*
    # credential -- once corruption is detected, a per-key quarantine that
    # only blocks the key someone happened to check first is fail-open for
    # every other credential. The quarantine must be global: every
    # credential blocks until the single bounded window expires.

    def test_global_quarantine_blocks_a_different_key_in_the_same_process(self):
        # A: corrupt file, checking key-a starts the global quarantine.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNotNone(self.store.get("key-a", now=NOW))
        # B: key-b, in the very same process/store, is blocked too -- not
        # because key-b has its own recorded cooldown, but because the
        # global quarantine covers every credential.
        self.assertIsNotNone(self.store.get("key-b", now=NOW))

    def test_global_quarantine_persists_across_new_process_for_both_keys(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("key-a", now=NOW)
        before_expiry = NOW + timedelta(seconds=1)
        # C: new process, key-a, still before expiry -> blocked.
        self.assertIsNotNone(CooldownStore(self.path).get("key-a", now=before_expiry))
        # D: new process, key-b, still before expiry -> blocked.
        self.assertIsNotNone(CooldownStore(self.path).get("key-b", now=before_expiry))

    def test_quarantine_does_not_fabricate_per_key_cooldown_entries(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("key-a", now=NOW)
        parsed = json.loads(self.path.read_text(encoding="utf-8"))
        # Only the global sentinel is written -- no invented per-account
        # 429 history for key-a, key-b, or anything else.
        self.assertEqual({GLOBAL_QUARANTINE_KEY}, set(parsed.keys()))

    def test_after_global_quarantine_expiry_each_key_resumes_independently(self):
        # E: once the global quarantine window has passed, keys go back to
        # behaving on their own per-key state -- an untouched key has none
        # (proceeds), while a key with its own genuine recorded cooldown
        # still honors it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("key-a", now=NOW)  # arms the global quarantine
        after_expiry = NOW + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS + 1)
        self.assertIsNone(self.store.get("key-a", now=after_expiry))
        self.assertIsNone(self.store.get("key-b", now=after_expiry))
        self.store.set_retry_until("key-b", after_expiry + timedelta(seconds=300))
        self.assertIsNone(self.store.get("key-a", now=after_expiry))
        self.assertIsNotNone(self.store.get("key-b", now=after_expiry))

    def test_no_permanent_lockout_after_global_quarantine_expiry(self):
        # I: the global quarantine window itself is bounded and expires --
        # it does not compound into an unbounded lock-out for any key.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("key-a", now=NOW)
        after_expiry = NOW + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS + 1)
        self.assertIsNone(CooldownStore(self.path).get("key-a", now=after_expiry))
        self.assertIsNone(CooldownStore(self.path).get("key-b", now=after_expiry))

    def test_real_429_after_recovery_persists_only_that_credential(self):
        # F: after the global quarantine has expired, a genuine 429 for
        # key-b persists a cooldown for key-b alone -- key-a is unaffected.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.store.get("key-a", now=NOW)
        after_expiry = NOW + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS + 1)
        self.store.set_retry_until("key-b", after_expiry + timedelta(seconds=120))
        self.assertIsNone(self.store.get("key-a", now=after_expiry))
        self.assertIsNotNone(self.store.get("key-b", now=after_expiry))

    def test_successful_key_a_does_not_clear_key_b_cooldown(self):
        # G
        self.store.set_retry_until("key-a", NOW + timedelta(seconds=300))
        self.store.set_retry_until("key-b", NOW + timedelta(seconds=300))
        self.store.clear("key-a")
        self.assertIsNone(self.store.get("key-a", now=NOW))
        self.assertIsNotNone(self.store.get("key-b", now=NOW))

    def test_quarantine_persists_no_token_or_credential_content(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json, leaked accessToken=Bearer sk-ant-fake", encoding="utf-8")
        self.store.get("<default>", now=NOW)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("accessToken", raw)
        self.assertNotIn("Bearer", raw)
        self.assertNotIn("sk-ant-fake", raw)

    # -- Structurally malformed *valid* JSON: the file parses and is a
    #    top-level dict, but an entry's shape or retry_until can't be
    #    trusted. This must fail closed exactly like unparseable JSON, not
    #    silently read as "no cooldown" for the malformed key. --

    def _write_json(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_malformed_retry_until_string_fails_closed_globally(self):
        self._write_json({"<default>": {"retry_until": "not-an-iso-time"}})
        result = self.store.get("<default>", now=NOW)
        self.assertIsNotNone(result)
        self.assertLessEqual((result - NOW).total_seconds(), CORRUPT_STATE_COOLDOWN_SECONDS)
        parsed = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([GLOBAL_QUARANTINE_KEY], list(parsed.keys()))
        # Zero HTTP: even an unrelated key is blocked, not just <default>.
        self.assertIsNotNone(self.store.get("other-key", now=NOW))

    def test_malformed_retry_until_type_fails_closed(self):
        self._write_json({"<default>": {"retry_until": 12345}})
        self.assertIsNotNone(self.store.get("<default>", now=NOW))

    def test_entry_not_an_object_fails_closed(self):
        self._write_json({"<default>": "not-an-object"})
        self.assertIsNotNone(self.store.get("<default>", now=NOW))

    def test_malformed_global_sentinel_retry_until_fails_closed(self):
        self._write_json({GLOBAL_QUARANTINE_KEY: {"retry_until": 12345}})
        result = self.store.get("<default>", now=NOW)
        self.assertIsNotNone(result)
        # A fresh bounded quarantine window is armed, not the untrusted one.
        self.assertLessEqual((result - NOW).total_seconds(), CORRUPT_STATE_COOLDOWN_SECONDS)

    def test_valid_expired_iso_retry_until_is_not_treated_as_corrupt(self):
        expired = NOW - timedelta(seconds=1)
        self._write_json({"<default>": {"retry_until": expired.isoformat().replace("+00:00", "Z")}})
        # Expired but structurally valid -> normal expiry, no quarantine.
        self.assertIsNone(self.store.get("<default>", now=NOW))
        parsed = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn(GLOBAL_QUARANTINE_KEY, parsed)

    def test_valid_multiple_entries_remain_independent(self):
        future = NOW + timedelta(seconds=300)
        self._write_json({
            "key-a": {"retry_until": future.isoformat().replace("+00:00", "Z")},
            "key-b": {"retry_until": (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")},
        })
        self.assertIsNotNone(self.store.get("key-a", now=NOW))
        self.assertIsNone(self.store.get("key-b", now=NOW))

    def test_new_process_honors_quarantine_from_structurally_malformed_state(self):
        self._write_json({"<default>": {"retry_until": "garbage"}})
        first = self.store.get("<default>", now=NOW)
        second = CooldownStore(self.path).get("some-other-key", now=NOW + timedelta(seconds=1))
        self.assertEqual(first, second)

    def test_post_expiry_exactly_one_request_allowed_after_structural_malformation(self):
        self._write_json({"<default>": {"retry_until": "garbage"}})
        self.store.get("<default>", now=NOW)
        after_expiry = NOW + timedelta(seconds=CORRUPT_STATE_COOLDOWN_SECONDS + 1)
        self.assertIsNone(CooldownStore(self.path).get("<default>", now=after_expiry))

    def test_malformed_state_quarantine_persists_no_secret_content(self):
        self._write_json({"<default>": {"retry_until": "garbage", "accessToken": "Bearer sk-ant-fake"}})
        self.store.get("<default>", now=NOW)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("accessToken", raw)
        self.assertNotIn("Bearer", raw)
        self.assertNotIn("sk-ant-fake", raw)


if __name__ == "__main__":
    unittest.main()
