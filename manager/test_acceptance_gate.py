"""Tests for the controlled acceptance gate (PROJECT BLOCKER 2/3, 2026-08-28):
never touches the real quota SSOT, only ever activates from a local file
nothing external can write, fails closed on any malformed/expired/missing
override."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manager.acceptance_gate import (
    KNOWN_PROVIDERS,
    OVERRIDE_TAG,
    apply_controlled_unavailability,
    clear_override,
    read_active_override,
    write_override,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _document():
    return {
        "generated_at": NOW.isoformat(),
        "providers": [
            {
                "provider": "codex", "display_name": "Codex", "collection_mode": "automatic",
                "source": "codex_app_server", "source_type": "official", "confidence": "official",
                "last_updated": NOW.isoformat(), "status": "ok",
                "windows": [{"name": "seven_day", "remaining_percent": 90, "used_percent": 10, "resets_at": None}],
            },
            {
                "provider": "claude", "display_name": "Claude Code", "collection_mode": "automatic",
                "source": "claude_code_statusline_rate_limits", "source_type": "official", "confidence": "official",
                "last_updated": NOW.isoformat(), "status": "ok",
                "windows": [{"name": "five_hour", "remaining_percent": 80, "used_percent": 20, "resets_at": None}],
            },
        ],
    }


class NoOverrideTests(unittest.TestCase):
    """A no-op for every real machine/caller: no manager_home, no override
    file present, or a manager_home directory that just doesn't have one."""

    def test_no_manager_home_is_inert(self):
        self.assertIsNone(read_active_override(None, now=NOW))
        document = _document()
        self.assertEqual(document, apply_controlled_unavailability(document, None, now=NOW))

    def test_missing_override_file_is_inert(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(read_active_override(home, now=NOW))
            document = _document()
            result = apply_controlled_unavailability(document, home, now=NOW)
            # Inert path returns the same object -- no copy overhead on the
            # overwhelmingly common (no override active) case.
            self.assertIs(document, result)


class FailClosedTests(unittest.TestCase):
    """Every malformed/expired/mistagged override is "no override" -- never
    "block real production dispatch." (module docstring point 3)"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        self.override_path = Path(self.home) / "acceptance-gate" / "override.json"

    def _write_raw(self, data):
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        self.override_path.write_text(json.dumps(data), encoding="utf-8")

    def test_malformed_json_is_no_override(self):
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        self.override_path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_active_override(self.home, now=NOW))

    def test_wrong_tag_is_no_override(self):
        self._write_raw({
            "tag": "SOMETHING_ELSE", "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            "unavailable_providers": ["codex"],
        })
        self.assertIsNone(read_active_override(self.home, now=NOW))

    def test_expired_override_is_no_override(self):
        self._write_raw({
            "tag": OVERRIDE_TAG, "created_at": (NOW - timedelta(minutes=20)).isoformat(),
            "expires_at": (NOW - timedelta(minutes=10)).isoformat(),
            "unavailable_providers": ["codex"],
        })
        self.assertIsNone(read_active_override(self.home, now=NOW))

    def test_age_beyond_hard_cap_is_no_override_even_if_expires_at_says_otherwise(self):
        """MAX_OVERRIDE_AGE_SECONDS is a hard cap regardless of what the file
        itself claims (module docstring point 6) -- a stale file with a
        far-future expires_at must not be honored forever."""
        self._write_raw({
            "tag": OVERRIDE_TAG, "created_at": (NOW - timedelta(days=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=365)).isoformat(),
            "unavailable_providers": ["codex"],
        })
        self.assertIsNone(read_active_override(self.home, now=NOW))

    def test_future_dated_created_at_is_no_override(self):
        self._write_raw({
            "tag": OVERRIDE_TAG, "created_at": (NOW + timedelta(minutes=5)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
            "unavailable_providers": ["codex"],
        })
        self.assertIsNone(read_active_override(self.home, now=NOW))

    def test_unknown_provider_name_is_no_override(self):
        self._write_raw({
            "tag": OVERRIDE_TAG, "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            "unavailable_providers": ["not_a_real_provider"],
        })
        self.assertIsNone(read_active_override(self.home, now=NOW))

    def test_empty_providers_list_is_no_override(self):
        self._write_raw({
            "tag": OVERRIDE_TAG, "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            "unavailable_providers": [],
        })
        self.assertIsNone(read_active_override(self.home, now=NOW))

    def test_missing_timestamps_are_no_override(self):
        self._write_raw({"tag": OVERRIDE_TAG, "unavailable_providers": ["codex"]})
        self.assertIsNone(read_active_override(self.home, now=NOW))


class ActivationTests(unittest.TestCase):
    """write_override() -> apply_controlled_unavailability() actually
    simulates real-shaped exhaustion, never touches the input document, and
    never affects a provider it wasn't told to."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name

    def test_write_then_apply_simulates_real_shaped_exhaustion(self):
        write_override(self.home, ["codex"], ttl_seconds=300, request_id="r1", now=NOW)
        document = _document()
        result = apply_controlled_unavailability(document, self.home, now=NOW)

        codex = next(p for p in result["providers"] if p["provider"] == "codex")
        self.assertEqual(0.0, codex["windows"][0]["remaining_percent"])
        self.assertEqual(100.0, codex["windows"][0]["used_percent"])
        # Untouched provider is genuinely untouched, byte-for-byte.
        claude = next(p for p in result["providers"] if p["provider"] == "claude")
        self.assertEqual(80, claude["windows"][0]["remaining_percent"])
        # The real input document is never mutated in place.
        self.assertEqual(90, document["providers"][0]["windows"][0]["remaining_percent"])

    def test_apply_never_mutates_the_input_document(self):
        write_override(self.home, ["codex"], now=NOW)
        document = _document()
        original = json.loads(json.dumps(document))
        apply_controlled_unavailability(document, self.home, now=NOW)
        self.assertEqual(original, document)

    def test_multiple_providers_can_be_named(self):
        write_override(self.home, ["codex", "claude"], now=NOW)
        result = apply_controlled_unavailability(_document(), self.home, now=NOW)
        for provider in result["providers"]:
            self.assertEqual(0.0, provider["windows"][0]["remaining_percent"])

    def test_clear_override_removes_it_immediately(self):
        write_override(self.home, ["codex"], now=NOW)
        self.assertIsNotNone(read_active_override(self.home, now=NOW))
        self.assertTrue(clear_override(self.home))
        self.assertIsNone(read_active_override(self.home, now=NOW))
        # Idempotent: clearing an already-absent override is not an error.
        self.assertFalse(clear_override(self.home))

    def test_write_override_rejects_unknown_provider(self):
        with self.assertRaises(ValueError):
            write_override(self.home, ["not_a_real_provider"], now=NOW)

    def test_write_override_rejects_empty_provider_list(self):
        with self.assertRaises(ValueError):
            write_override(self.home, [], now=NOW)

    def test_ttl_is_clamped_to_the_hard_cap(self):
        from manager.acceptance_gate import MAX_OVERRIDE_AGE_SECONDS
        write_override(self.home, ["codex"], ttl_seconds=MAX_OVERRIDE_AGE_SECONDS * 10, now=NOW)
        # Still active at the hard cap boundary...
        self.assertIsNotNone(read_active_override(self.home, now=NOW + timedelta(seconds=MAX_OVERRIDE_AGE_SECONDS - 1)))
        # ...but not one second beyond it, regardless of the requested ttl.
        self.assertIsNone(read_active_override(self.home, now=NOW + timedelta(seconds=MAX_OVERRIDE_AGE_SECONDS + 1)))

    def test_applied_evidence_is_logged_and_clearly_tagged(self):
        """Module docstring point 4: every application is auditable and
        never indistinguishable from real exhaustion in the log, even
        though the returned document itself deliberately mirrors real
        exhaustion's shape."""
        write_override(self.home, ["codex"], request_id="acceptance-test-r1", now=NOW)
        apply_controlled_unavailability(_document(), self.home, now=NOW)
        log_path = Path(self.home) / "acceptance-gate" / "applied-log.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(lines))
        entry = json.loads(lines[0])
        self.assertEqual(OVERRIDE_TAG, entry["tag"])
        self.assertEqual("acceptance-test-r1", entry["request_id"])
        self.assertEqual(["codex"], entry["unavailable_providers"])

    def test_known_providers_matches_the_real_expected_provider_set(self):
        from manager.quota_reader import EXPECTED_PROVIDERS
        self.assertEqual(set(EXPECTED_PROVIDERS), KNOWN_PROVIDERS)
