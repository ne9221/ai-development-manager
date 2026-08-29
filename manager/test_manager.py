import unittest
from datetime import datetime, timedelta, timezone

from manager.assignment import decide, quota_score
from manager.quota_reader import QuotaReaderError, parse_time, summarize


NOW = datetime(2026, 8, 9, 4, 0, tzinfo=timezone.utc)


def provider(name, remaining=None, updated=NOW, resets=None, windows=1):
    values = [] if remaining is None else [{
        "name": f"window_{index}", "remaining_percent": remaining,
        "used_percent": 100 - remaining, "resets_at": resets,
    } for index in range(windows)]
    return {
        "provider": name, "display_name": name, "collection_mode": "automatic",
        "source": "official", "source_type": "official", "confidence": "official" if values else "unknown",
        "last_updated": updated.isoformat(), "status": "ok" if values else "unknown", "windows": values,
    }


def quota(*providers, max_age=60):
    return summarize({"generated_at": NOW.isoformat(), "providers": list(providers)}, max_age, NOW)


class ManagerTest(unittest.TestCase):
    def task(self, **changes):
        value = {"task_type": "implementation", "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True}
        value.update(changes)
        return value

    def test_codex_fresh_claude_unknown(self):
        result = decide(self.task(), quota(provider("codex", 90), provider("claude")), NOW)
        self.assertEqual(result["recommended_provider"], "codex")

    def test_codex_low_claude_available(self):
        result = decide(self.task(task_type="architecture", needs_repo_edit=False), quota(provider("codex", 5), provider("claude", 80)), NOW)
        self.assertEqual(result["recommended_provider"], "claude")

    def test_both_unknown_fails_closed_for_automatic_routing(self):
        result = decide(self.task(), quota(provider("codex"), provider("claude")), NOW)
        self.assertIsNone(result["recommended_provider"])
        self.assertIsNone(result["recommended_mode"])
        self.assertIn("unknown", result["warning"])

    def test_automatic_routing_ignores_stale_provider_even_when_capability_scores_higher(self):
        result = decide(
            self.task(task_type="architecture", needs_repo_edit=False),
            quota(provider("codex", 80), provider("claude", 90, NOW - timedelta(hours=2))),
            NOW,
        )
        self.assertEqual("codex", result["recommended_provider"])

    def test_automatic_routing_keeps_fresh_claude_when_codex_is_stale(self):
        # needs_repo_edit=False, matching this test class's other
        # Claude-eligible scenarios above: ClaudeLauncher v1 only supports
        # the read-only profile, so a repo-write task would correctly
        # capability-filter Claude out regardless of quota freshness --
        # this test is specifically about quota-freshness routing, not
        # repo-write capability.
        result = decide(
            self.task(needs_repo_edit=False),
            quota(provider("codex", 90, NOW - timedelta(hours=2)), provider("claude", 20)),
            NOW,
        )
        self.assertEqual("claude", result["recommended_provider"])

    def test_stale_quota_is_not_reliable(self):
        summary = quota(provider("codex", 90, NOW - timedelta(hours=2)))
        codex = next(item for item in summary["providers"] if item["provider"] == "codex")
        self.assertTrue(codex["stale"])
        self.assertFalse(codex["has_reliable_quota"])

    def test_future_clock_skew_tolerance(self):
        near = quota(provider("codex", 90, NOW + timedelta(minutes=1)))
        self.assertFalse(near["providers"][0]["future_skewed"])
        far = quota(provider("codex", 90, NOW + timedelta(minutes=6)))
        self.assertTrue(far["providers"][0]["future_skewed"])
        self.assertFalse(far["providers"][0]["has_reliable_quota"])

    def test_future_clock_skew_exact_five_minute_boundary_is_pinned(self):
        exactly_five = quota(provider("codex", 90, NOW + timedelta(minutes=5)))
        self.assertFalse(exactly_five["providers"][0]["future_skewed"])
        one_second_over = quota(provider("codex", 90, NOW + timedelta(minutes=5, seconds=1)))
        self.assertTrue(one_second_over["providers"][0]["future_skewed"])

    def test_stale_exact_max_age_boundary_is_pinned(self):
        exactly_max_age = quota(provider("codex", 90, NOW - timedelta(minutes=60)), max_age=60)
        self.assertFalse(exactly_max_age["providers"][0]["stale"])
        slightly_over = quota(provider("codex", 90, NOW - timedelta(minutes=60, seconds=1)), max_age=60)
        self.assertTrue(slightly_over["providers"][0]["stale"])

    def test_naive_and_malformed_timestamps_are_rejected(self):
        for value in ("2026-08-09T04:00:00", "not-a-time"):
            with self.assertRaises(QuotaReaderError):
                parse_time(value)

    def test_one_and_multiple_windows(self):
        summary = quota(provider("codex", 50, windows=1), provider("claude", 50, windows=3))
        self.assertEqual(len(summary["providers"][0]["windows"]), 1)
        self.assertEqual(len(summary["providers"][1]["windows"]), 3)

    def test_empty_windows(self):
        summary = quota(provider("codex"))
        self.assertFalse(summary["providers"][0]["has_reliable_quota"])

    def test_near_reset_is_evidence(self):
        reset = (NOW + timedelta(minutes=2)).isoformat()
        near, _ = quota_score(quota(provider("codex", 10, resets=reset))["providers"][0], 20, NOW)
        far, _ = quota_score(quota(provider("codex", 10, resets=(NOW + timedelta(hours=2)).isoformat()))["providers"][0], 20, NOW)
        self.assertGreater(near, far)

    def test_twenty_minutes_assigns(self):
        self.assertIsNotNone(decide(self.task(expected_minutes=20), quota(provider("codex", 50)), NOW)["recommended_provider"])

    def test_over_twenty_minutes_splits(self):
        result = decide(self.task(expected_minutes=45), quota(provider("codex", 90)), NOW)
        self.assertIsNone(result["recommended_provider"])
        self.assertEqual(result["recommended_mode"], "split_task")

    def test_historical_estimate_is_explainable_evidence(self):
        estimate = {"estimated_minutes": 18, "sample_count": 3, "confidence": "medium"}
        result = decide(self.task(), quota(provider("codex", 90)), NOW, {"codex": estimate})
        self.assertEqual(estimate, result["quota_evidence"]["codex"]["historical_estimate"])
        self.assertIn("3 matching executions", result["reasons"][-1])


if __name__ == "__main__":
    unittest.main()
