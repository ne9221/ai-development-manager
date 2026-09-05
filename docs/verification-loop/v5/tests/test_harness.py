"""Harness integrity gate + H-M1..H-M4 non-vacuity mutants + reviewer RH1/RH2.

H-M1..H-M4 are the original round's mutants and must stay killed.
RH1/RH2 are the independent reviewer's harness escapes, promoted to permanent
regressions in Remediation Round 1:

  RH1  the runner aggregated an EMPTY safety collection as a pass (0 attacks,
       0 blocked, HARNESS_USABLE=YES, exit 0).
  RH2  a fabricated kernel digest satisfied the digest gate, which only checked
       that *some* digest string was present.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v5_kernel.harness import (
    HARNESS_REQUIRED_AGENTS,
    AgentResult,
    harness_gate,
    historical_failure_records,
    safety_row,
)
from v5_kernel.kernel import KERNEL_ID, controller_trust_digest

EXPECTED_ATTACKS = ("F01", "F02b")


def _rows(digest, attacks=EXPECTED_ATTACKS):
    return [
        safety_row(
            attack_id=a,
            original_attack_blocked=True,
            new_variant_found=False,
            klass="MC-A",
            evidence="row for %s" % a,
            kernel_digest=digest,
        )
        for a in attacks
    ]


def _good_results(digest=None):
    digest_ok = digest or controller_trust_digest()
    return [
        AgentResult("liveness_runner", True, "PASS", prose="L1-L7 7/7", effective={"L": "PASS"}),
        AgentResult(
            "safety_runner",
            True,
            "PASS",
            prose="original vs variant separated",
            original_blocked="YES",
            new_variant_found="NO",
            effective=_rows(digest_ok),
        ),
        AgentResult("harness_mutator", True, "PASS", prose="H-M killed", effective={"H-M1": "KILLED"}),
        AgentResult("aggregator", True, "PASS", prose="n/a", effective={"ready": True}),
    ], digest_ok


def _gate(results, digest, **kw):
    kw.setdefault("expected_safety_attack_ids", EXPECTED_ATTACKS)
    return harness_gate(HARNESS_REQUIRED_AGENTS, results, observed_kernel_digest=digest, **kw)


class HarnessGateTests(unittest.TestCase):
    def test_clean_roster_is_usable(self):
        results, digest = _good_results()
        report = _gate(results, digest)
        self.assertEqual(report.HARNESS_USABLE, "YES", report.reasons)

    def test_HM1_missing_required_agent_not_usable(self):
        results, digest = _good_results()
        roster = [a for a in HARNESS_REQUIRED_AGENTS if a != "safety_runner"]
        report = harness_gate(
            roster, results, observed_kernel_digest=digest, expected_safety_attack_ids=EXPECTED_ATTACKS
        )
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("roster incomplete" in r for r in report.reasons), report.reasons)

    def test_HM2_object_sentinel_not_usable(self):
        results, digest = _good_results()
        results[1].effective = "ERROR: [object]"
        report = _gate(results, digest)
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("sentinel" in r for r in report.reasons), report.reasons)

    def test_HM3_placeholder_kernel_not_usable(self):
        results, digest = _good_results()
        report = _gate(results, digest, placeholder_kernel=True, kernel_id="PLACEHOLDER", kernel_authoritative=False)
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("placeholder" in r for r in report.reasons), report.reasons)

    def test_HM4_blocked_degenerate_contradiction_not_usable(self):
        results, digest = _good_results()
        results[1].verdict = "BLOCKED"
        results[1].block_is_degenerate = True
        results[1].original_blocked = "YES"
        results[1].new_variant_found = "NO"
        report = _gate(results, digest)
        self.assertEqual(report.HARNESS_USABLE, "NO")
        self.assertTrue(any("degenerate" in r for r in report.reasons), report.reasons)

    def test_usage_limit_and_unresolved_failure_not_swallowed(self):
        results, digest = _good_results()
        results[0].error = "agent usage-limit exceeded"
        results[0].verdict = "ERROR"
        report = _gate(results, digest, unresolved_failures=1)
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
        report = _gate(results, digest)
        self.assertEqual(report.HARNESS_USABLE, "NO")

    # --- reviewer-derived regressions ------------------------------------

    def test_RH1_empty_safety_collection_is_not_usable(self):
        results, digest = _good_results()
        results[1].effective = []
        report = _gate(results, digest)
        self.assertEqual(report.HARNESS_USABLE, "NO", report.reasons)
        self.assertTrue(any("empty" in r for r in report.reasons), report.reasons)

    @unittest.skipIf(
        os.environ.get("V5_FORCE_EMPTY_SAFETY_ROSTER") == "1",
        "already inside the forced-empty-roster child runner",
    )
    def test_RH1b_runner_exits_nonzero_on_empty_safety_roster(self):
        """The gate is not the only place this escaped: the runner itself
        aggregated 0/0 and returned exit 0. Drive the real runner."""
        here = os.path.dirname(os.path.abspath(__file__))
        env = dict(os.environ, V5_FORCE_EMPTY_SAFETY_ROSTER="1")
        proc = subprocess.run(
            [sys.executable, os.path.join(here, "run_v5.py")],
            capture_output=True, text=True, env=env, cwd=here,
        )
        self.assertNotEqual(proc.returncode, 0, "empty safety roster must not exit 0:\n%s" % proc.stdout[-2000:])

    def test_RH2_fabricated_kernel_digest_is_not_usable(self):
        results, digest = _good_results()
        report = _gate(results, "deadbeef")
        self.assertEqual(report.HARNESS_USABLE, "NO", report.reasons)
        self.assertTrue(any("does not match the authoritative" in r for r in report.reasons), report.reasons)

    def test_RH2b_safety_rows_must_carry_the_authoritative_digest(self):
        digest = controller_trust_digest()
        results, _ = _good_results()
        results[1].effective = _rows("deadbeef")
        report = _gate(results, digest)
        self.assertEqual(report.HARNESS_USABLE, "NO", report.reasons)
        self.assertTrue(any("kernel_digest" in r for r in report.reasons), report.reasons)


if __name__ == "__main__":
    unittest.main()
