#!/usr/bin/env python3
"""V5 reference runner: liveness first, then harness gate, then safety aggregation.

Remediation Round 1: the runner is now fail-closed too. An empty safety roster
returns non-zero instead of printing `0/0` and exiting 0.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

from v5_kernel.harness import (  # noqa: E402
    HARNESS_REQUIRED_AGENTS,
    AgentResult,
    harness_gate,
)
from v5_kernel.kernel import (  # noqa: E402
    KERNEL_ID,
    KERNEL_V5_AUTHORITATIVE,
    TRUST_ROOT_RUNTIME_PROVEN,
    controller_trust_digest,
)


def _run(mod: str) -> unittest.TestResult:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(mod)
    return unittest.TextTestRunner(verbosity=2).run(suite)


def main() -> int:
    print("=== V5 LIVENESS (must be 7/7 before safety) ===")
    live = _run("test_liveness")
    live_ok = live.wasSuccessful() and live.testsRun >= 7
    print("LIVENESS_REQUIRED = %s (ran=%d fail=%d err=%d)"
          % ("7/7 PASS" if live_ok else "FAIL", live.testsRun, len(live.failures), len(live.errors)))
    if not live_ok:
        print("Safety fleet forbidden: liveness did not pass.")
        return 2

    print("=== V5 REMEDIATION R1 REGRESSIONS (B1-B7, C7, C10) ===")
    rep = _run("test_repairs_r1")
    rep_ok = rep.wasSuccessful()

    print("=== V5 HARNESS MUTATIONS (H-M1..H-M4, RH1/RH2) ===")
    mut = _run("test_harness")
    mut_ok = mut.wasSuccessful()

    print("=== V5 SAFETY ===")
    safe = _run("test_safety")
    safe_ok = safe.wasSuccessful()

    from test_safety import ATTACK_MATRIX, EXPECTED_ATTACK_IDS  # noqa: WPS433

    expected_attacks = tuple(EXPECTED_ATTACK_IDS)
    collected = list(ATTACK_MATRIX)
    # RH1: the runner must fail closed on an empty roster, not aggregate 0/0.
    if os.environ.get("V5_FORCE_EMPTY_SAFETY_ROSTER") == "1":
        print("!! V5_FORCE_EMPTY_SAFETY_ROSTER: simulating an empty safety collection")
        collected = []
        expected_attacks = ()

    if not expected_attacks:
        print("HARNESS_USABLE = NO")
        print("  - expected safety attack roster is empty — 0/0 is not a PASS")
        print("Aggregation forbidden.")
        return 5
    if not collected:
        print("HARNESS_USABLE = NO")
        print("  - safety collection is empty while %d attacks are expected" % len(expected_attacks))
        print("Aggregation forbidden.")
        return 5

    digest = controller_trust_digest()
    results = [
        AgentResult("liveness_runner", live_ok, "PASS" if live_ok else "FAIL",
                    prose="L1-L7", effective={"pass": live_ok}),
        AgentResult(
            "safety_runner",
            safe_ok,
            "PASS" if safe_ok else "FAIL",
            prose="matrix below",
            original_blocked="YES" if all(r["original_attack_blocked"] == "YES" for r in collected) else "NO",
            new_variant_found="YES" if any(r["new_variant_found"] == "YES" for r in collected) else "NO",
            effective=collected,
        ),
        AgentResult("harness_mutator", mut_ok, "PASS" if mut_ok else "FAIL",
                    prose="H-M1..H-M4 + RH1/RH2", effective={"pass": mut_ok}),
        AgentResult("aggregator", True, "PASS", prose="gated", effective={"rows": len(collected)}),
    ]
    gate = harness_gate(
        HARNESS_REQUIRED_AGENTS,
        results,
        expected_roster=HARNESS_REQUIRED_AGENTS,
        expected_safety_attack_ids=expected_attacks,
        unresolved_failures=(
            len(live.failures) + len(live.errors)
            + len(safe.failures) + len(safe.errors)
            + len(mut.failures) + len(mut.errors)
            + len(rep.failures) + len(rep.errors)
        ),
        kernel_id=KERNEL_ID,
        kernel_authoritative=KERNEL_V5_AUTHORITATIVE,
        observed_kernel_digest=digest,
        placeholder_kernel=False,
    )
    print("HARNESS_USABLE = %s" % gate.HARNESS_USABLE)
    for r in gate.reasons:
        print("  - %s" % r)
    if not gate.usable:
        print("Aggregation forbidden.")
        return 3

    print("=== SAFETY MATRIX (aggregated only because harness usable) ===")
    print("| Attack | Original blocked? | New variant? | Class | Evidence |")
    print("|---|---|---|---|---|")
    for row in collected:
        print("| %s | %s | %s | %s | %s |"
              % (row["attack_id"], row["original_attack_blocked"], row["new_variant_found"],
                 row["klass"], row["evidence"]))
    blocked = sum(1 for r in collected if r["original_attack_blocked"] == "YES")
    variants = sum(1 for r in collected if r["new_variant_found"] == "YES")
    print("ORIGINAL_ATTACK_BLOCKED count = %d/%d" % (blocked, len(collected)))
    print("NEW_VARIANT_FOUND count = %d/%d" % (variants, len(collected)))
    print("R1_REGRESSIONS = %s (ran=%d)" % ("PASS" if rep_ok else "FAIL", rep.testsRun))
    print("TRUST_ROOT_RUNTIME_PROVEN = %s" % ("YES" if TRUST_ROOT_RUNTIME_PROVEN else "NO"))
    if not (live_ok and mut_ok and safe_ok and rep_ok and blocked == len(collected) and variants == 0):
        return 4
    print("V5_IMPLEMENTATION_STATUS candidate = R1_REMEDIATED (independent re-review still REQUIRED)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
