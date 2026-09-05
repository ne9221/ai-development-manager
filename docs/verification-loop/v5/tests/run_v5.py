#!/usr/bin/env python3
"""V5 reference runner: liveness first, then harness gate, then safety aggregation."""

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
from v5_kernel.kernel import KERNEL_ID, KERNEL_V5_AUTHORITATIVE, controller_trust_digest  # noqa: E402


def _run(mod: str) -> unittest.TestResult:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(mod)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result


def main() -> int:
    print("=== V5 LIVENESS (must be 7/7 before safety) ===")
    live = _run("test_liveness")
    live_ok = live.wasSuccessful() and live.testsRun >= 7
    print(f"LIVENESS_REQUIRED = {'7/7 PASS' if live_ok else 'FAIL'} (ran={live.testsRun} fail={len(live.failures)} err={len(live.errors)})")
    if not live_ok:
        print("Safety fleet forbidden: liveness did not pass.")
        return 2

    print("=== V5 HARNESS MUTATIONS ===")
    mut = _run("test_harness")
    mut_ok = mut.wasSuccessful()

    print("=== V5 SAFETY ===")
    # Import after liveness so ATTACK_MATRIX fills during load/run.
    import test_safety  # noqa: WPS433

    safe = _run("test_safety")
    safe_ok = safe.wasSuccessful()

    from test_safety import ATTACK_MATRIX  # noqa: WPS433

    digest = controller_trust_digest()
    original_fields_complete = all(
        row.get("Original blocked?") in {"YES", "NO"} and row.get("New variant?") in {"YES", "NO"}
        for row in ATTACK_MATRIX
    )
    any_unblocked = any(row.get("Original blocked?") != "YES" for row in ATTACK_MATRIX)
    any_variant = any(row.get("New variant?") == "YES" for row in ATTACK_MATRIX)

    results = [
        AgentResult("liveness_runner", live_ok, "PASS" if live_ok else "FAIL", prose="L1-L7", effective={"pass": live_ok}),
        AgentResult(
            "safety_runner",
            safe_ok,
            "PASS" if safe_ok and not any_unblocked else "FAIL",
            prose="matrix below",
            original_blocked="YES" if not any_unblocked else "NO",
            new_variant_found="YES" if any_variant else "NO",
            effective=ATTACK_MATRIX,
        ),
        AgentResult("harness_mutator", mut_ok, "PASS" if mut_ok else "FAIL", prose="H-M1..H-M4", effective={"pass": mut_ok}),
        AgentResult("aggregator", True, "PASS", prose="gated", effective={"rows": len(ATTACK_MATRIX)}),
    ]
    gate = harness_gate(
        HARNESS_REQUIRED_AGENTS,
        results,
        unresolved_failures=len(live.failures) + len(live.errors) + len(safe.failures) + len(safe.errors) + len(mut.failures) + len(mut.errors),
        kernel_id=KERNEL_ID,
        kernel_authoritative=KERNEL_V5_AUTHORITATIVE,
        kernel_digest=digest,
        placeholder_kernel=False,
    )
    print(f"HARNESS_USABLE = {gate.HARNESS_USABLE}")
    if gate.reasons:
        for r in gate.reasons:
            print(f"  - {r}")
    if not gate.usable:
        print("Aggregation forbidden.")
        return 3

    print("=== SAFETY MATRIX (aggregated only because harness usable) ===")
    print("| Attack | Original blocked? | New variant? | Class | Evidence |")
    print("|---|---|---|---|---|")
    for row in ATTACK_MATRIX:
        print(
            f"| {row['Attack']} | {row['Original blocked?']} | {row['New variant?']} | {row['Class']} | {row['Evidence']} |"
        )
    blocked = sum(1 for r in ATTACK_MATRIX if r["Original blocked?"] == "YES")
    variants = sum(1 for r in ATTACK_MATRIX if r["New variant?"] == "YES")
    print(f"ORIGINAL_ATTACK_BLOCKED count = {blocked}/{len(ATTACK_MATRIX)}")
    print(f"NEW_VARIANT_FOUND count = {variants}/{len(ATTACK_MATRIX)}")
    print(f"original_vs_variant_fields_complete = {original_fields_complete}")
    if not (live_ok and mut_ok and safe_ok and blocked == len(ATTACK_MATRIX) and variants == 0):
        return 4
    print("V5_IMPLEMENTATION_STATUS candidate = DRAFT_COMPLETE (independent review still REQUIRED)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
