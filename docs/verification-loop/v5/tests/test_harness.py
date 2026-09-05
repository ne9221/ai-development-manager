"""Harness integrity gate + H-M1..H-M4 non-vacuity mutants."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v5_kernel.harness import (
    HARNESS_REQUIRED_AGENTS,
    AgentResult,
    harness_gate,
    historical_failure_records,
)
from v5_kernel.kernel import KERNEL_ID, controller_trust_digest


def _good_results():
    digest_ok = controller_trust_digest()
    return [
        AgentResult("liveness_runner", True, "PASS", prose="L1-L7 7/7", effective={"L": "PASS"}),
        AgentResult(
            "safety_runner",
            True,
            "PASS",
            prose="original vs variant separated",
            original_blocked="YES",
            new_variant_found="NO",
            effective={"F12": "BLOCKED"},
        ),
        AgentResult("harness_mutator", True, "PASS", prose="H-M killed", effective={"H-M1": "KILLED"}),
        AgentResult("aggregator", True, "PASS", prose="n/a", effective={"ready": True}),
    ], digest_ok


class HarnessGateTests(unittest.TestCase):
    def test_clean_roster_is_usable(self):
        results, digest = _good_results()
        report = harness_gate(HARNESS_REQUIRED_AGENTS, results, kernel_digest=digest)
        self.assertEqual(report.HARNESS_USABLE, "YES", report.reasons)

    def test_HM1_missing_required_agent_not_usable(self):
        results, digest = _good_results()
        roster = [a for a in HARNESS_REQUIRED_AGENTS if a != "safety_runner"]
        report = harness_gate(roster, results, kernel_digest=digest)
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("roster incomplete" in r for r in report.reasons))

    def test_HM2_object_sentinel_not_usable(self):
        results, digest = _good_results()
        results[1].effective = "ERROR: [object]"
        report = harness_gate(HARNESS_REQUIRED_AGENTS, results, kernel_digest=digest)
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("sentinel" in r for r in report.reasons))

    def test_HM3_placeholder_kernel_not_usable(self):
        results, digest = _good_results()
        report = harness_gate(
            HARNESS_REQUIRED_AGENTS,
            results,
            kernel_digest=digest,
            placeholder_kernel=True,
            kernel_id="PLACEHOLDER",
            kernel_authoritative=False,
        )
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("placeholder" in r for r in report.reasons))

    def test_HM4_blocked_degenerate_contradiction_not_usable(self):
        results, digest = _good_results()
        results[1].verdict = "BLOCKED"
        results[1].block_is_degenerate = True
        results[1].original_blocked = "YES"
        results[1].new_variant_found = "NO"
        report = harness_gate(HARNESS_REQUIRED_AGENTS, results, kernel_digest=digest)
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("degenerate" in r for r in report.reasons))

    def test_usage_limit_and_unresolved_failure_not_swallowed(self):
        results, digest = _good_results()
        results[0].error = "agent usage-limit exceeded"
        results[0].verdict = "ERROR"
        report = harness_gate(
            HARNESS_REQUIRED_AGENTS,
            results,
            unresolved_failures=1,
            kernel_digest=digest,
        )
        self.assertEqual(report.HARNESS_USABLE, "NO")

    def test_historical_failures_remain_on_record(self):
        recs = historical_failure_records()
        ids = {r["id"] for r in recs}
        self.assertGreaterEqual(len(recs), 4)
        self.assertIn("HF-USAGE-LIMIT-SWALLOWED", ids)
        self.assertIn("HF-OBJECT-SENTINEL", ids)
        self.assertIn("HF-PROSE-VERDICT-DIVERGENCE", ids)
        self.assertIn("HF-PLACEHOLDER-KERNEL", ids)
        self.assertTrue(all(r["status"] == "MUST_REMAIN_ON_RECORD" for r in recs))

    def test_original_vs_variant_fields_required_on_safety(self):
        results, digest = _good_results()
        results[1].original_blocked = None
        results[1].new_variant_found = None
        report = harness_gate(HARNESS_REQUIRED_AGENTS, results, kernel_digest=digest)
        self.assertEqual(report.HARNESS_USABLE, "NO")


if __name__ == "__main__":
    unittest.main()
