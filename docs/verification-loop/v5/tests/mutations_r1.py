#!/usr/bin/env python3
"""R1 negative controls: revert each repair, prove the matching regression fails.

A regression that passes against its own mutant is vacuous. Each mutation below
undoes exactly one Round 1 repair in an isolated copy of the v5 tree and runs
the tests that are supposed to catch it. The mutation is KILLED only when those
tests actually FAIL.

Oracle guards (a crash is not evidence):
  * a mutation whose source text is not found is an ERROR, never SURVIVED;
  * a run that collects zero tests is an ERROR, never KILLED;
  * a run that fails to import is reported distinctly from a real assertion
    failure, because "the module exploded" is not "the guard fired".

    python3 docs/verification-loop/v5/tests/mutations_r1.py
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
V5 = os.path.abspath(os.path.join(HERE, ".."))

KERNEL = os.path.join("src", "v5_kernel", "kernel.py")
HARNESS = os.path.join("src", "v5_kernel", "harness.py")


MUTATIONS = [
    {
        "id": "M-R1",
        "reverts": "B1 — restore the payload-driven attester defaulting to LAUNCHER",
        "target": "test_repairs_r1.B1TrustedLabelForgery",
        "edits": [
            (
                KERNEL,
                "    if not isinstance(envelope, EventEnvelope):\n"
                "        return Issuer.CANDIDATE_EXECUTOR.value",
                "    if not isinstance(envelope, EventEnvelope):\n"
                "        return Issuer.LAUNCHER.value",
            ),
            (
                KERNEL,
                "        source = _event_source(world, envelope)\n"
                '        result = str(payload.get("result", "FAIL"))',
                "        source = str(payload.get(\"attester\", Issuer.LAUNCHER.value))\n"
                '        result = str(payload.get("result", "FAIL"))',
            ),
        ],
    },
    {
        "id": "M-R2",
        "reverts": "B2 — accept a required obligation merely because it is not ADVERSE",
        "target": "test_repairs_r1.B2RequiredObligations",
        "edits": [
            (
                KERNEL,
                "        if ob.state != ObligationState.SATISFIED:\n"
                '            unsatisfied.append("%s=%s" % (oid, ob.state.value))\n'
                "            continue",
                "        if ob.state == ObligationState.ADVERSE:\n"
                '            unsatisfied.append("%s=%s" % (oid, ob.state.value))\n'
                "            continue",
            ),
            (
                KERNEL,
                "            ObligationState.UNAVAILABLE_HUMAN,\n"
                "            ObligationState.UNAVAILABLE_RECOVERABLE,\n"
                "        ):",
                "            ObligationState.UNAVAILABLE_HUMAN,\n"
                "        ):",
            ),
            (
                KERNEL,
                "    declared_oracle = _declared(launcher, policy, key=\"oracle_expected\")\n"
                "    expected = frozenset(declared_oracle if declared_oracle is not None else ())",
                "    expected = frozenset(\n"
                "        launcher.get(\"oracle_expected\") or policy.get(\"oracle_expected\") or [\"oracle.unit\"]\n"
                "    )",
            ),
        ],
    },
    {
        "id": "M-R3",
        "reverts": "B3 — let a previous interval SATISFIED survive a later partial CLOSE",
        "target": "test_repairs_r1.B3IntervalRecomputation",
        "edits": [
            (
                KERNEL,
                "    else:\n"
                '        _set_ob(world, "interval.binding", state, source="ASYMMETRIC")\n'
                "    return state",
                "    else:\n"
                '        prev = world.obligations.get("interval.binding")\n'
                "        if prev is None or prev.state != ObligationState.SATISFIED:\n"
                '            _set_ob(world, "interval.binding", state, source="ASYMMETRIC")\n'
                "        else:\n"
                "            state = ObligationState.SATISFIED\n"
                "    return state",
            ),
        ],
    },
    {
        "id": "M-R4",
        "reverts": "B4 — a review claim with no invocation and no launcher capture still satisfies",
        "target": "test_repairs_r1.B4ReviewClaimSchema",
        "edits": [
            (
                KERNEL,
                "    if missing:\n"
                '        world.notes.append("B4: review claim missing required fields %s" % sorted(set(missing)))\n'
                '        return "claim below minimum schema", ObligationState.PENDING',
                "    if False:\n"
                '        world.notes.append("B4: review claim missing required fields %s" % sorted(set(missing)))\n'
                '        return "claim below minimum schema", ObligationState.PENDING',
            ),
            (
                KERNEL,
                '        return "no launcher capture for this invocation", ObligationState.PENDING',
                '        return "mutant: trusting the claim itself", ObligationState.SATISFIED',
            ),
        ],
    },
    {
        "id": "M-R5",
        "reverts": "B5 — ignore adjudication subject matching and record expiry",
        "target": "test_repairs_r1.B5Adjudication",
        "edits": [
            (
                KERNEL,
                "    if expires >= 0 and expires <= world.tick:\n"
                '        defects.append("record already expired at tick %d" % world.tick)',
                "    if False:\n"
                '        defects.append("record already expired at tick %d" % world.tick)',
            ),
            (
                KERNEL,
                "        if world.human_gate_subject and subject == world.human_gate_subject:",
                "        if True:",
            ),
            (
                KERNEL,
                '    """B5: expiry/scope/subject are re-checked at every decide(), not only at write."""\n'
                "    blockers: List[str] = []",
                '    """B5: expiry/scope/subject are re-checked at every decide(), not only at write."""\n'
                "    return []\n"
                "    blockers: List[str] = []",
            ),
        ],
    },
    {
        "id": "M-R6",
        "reverts": "B6 — a recoverable failure no longer consumes its retry budget",
        "target": "test_repairs_r1.B6RecoveryBudget",
        "edits": [
            (
                KERNEL,
                "    budget = RetryBudget(identity, budget.used + 1, budget.maximum)\n"
                "    world.budgets[identity] = budget",
                "    budget = RetryBudget(identity, budget.used, budget.maximum)\n"
                "    world.budgets[identity] = budget",
            ),
        ],
    },
    {
        "id": "M-R7",
        "reverts": "B7 — an empty safety roster is usable again",
        "target": "test_repairs_r1.B7HarnessFailClosed",
        "edits": [
            (
                HARNESS,
                "    if not expected:\n"
                '        return ["expected safety attack roster is empty — 0/0 cannot be PASS"]',
                "    if not expected:\n"
                "        return []",
            ),
            (
                HARNESS,
                "    if len(rows) == 0:\n"
                '        return ["safety collection is empty while %d attacks are expected" % len(expected)]',
                "    if len(rows) == 0:\n"
                "        return []",
            ),
        ],
    },
]


def _apply(root, path, old, new, mid):
    full = os.path.join(root, path)
    src = io.open(full, encoding="utf-8").read()
    if old not in src:
        raise AssertionError("%s: mutation source text not found in %s" % (mid, path))
    if src.count(old) != 1:
        raise AssertionError("%s: mutation text is ambiguous in %s (%d matches)" % (mid, path, src.count(old)))
    io.open(full, "w", encoding="utf-8", newline="\n").write(src.replace(old, new, 1))


def run_mutation(mut):
    root = tempfile.mkdtemp(prefix="v5mut-")
    try:
        tree = os.path.join(root, "v5")
        shutil.copytree(V5, tree)
        for path, old, new in mut["edits"]:
            _apply(tree, path, old, new, mut["id"])
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", mut["target"], "-v"],
            cwd=os.path.join(tree, "tests"),
            capture_output=True,
            text=True,
        )
        out = proc.stdout + proc.stderr
        ran = re.search(r"^Ran (\d+) test", out, re.M)
        n = int(ran.group(1)) if ran else 0
        if n == 0:
            return "ERROR", "mutation collected 0 tests — oracle is vacuous", out
        if "ModuleNotFoundError" in out or "ImportError" in out:
            return "ERROR", "mutant did not import; crash is not a guard firing", out
        if proc.returncode == 0:
            return "SURVIVED", "%d tests still passed against the mutant" % n, out
        fails = len(re.findall(r"^(FAIL|ERROR): ", out, re.M))
        return "KILLED", "%d/%d targeted tests failed against the mutant" % (fails, n), out
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    rows = []
    for mut in MUTATIONS:
        try:
            verdict, detail, out = run_mutation(mut)
        except AssertionError as exc:
            verdict, detail, out = "ERROR", str(exc), ""
        rows.append((mut["id"], verdict, mut["reverts"], detail))
        print("[%s] %s — %s" % (mut["id"], verdict, mut["reverts"]))
        print("        %s" % detail)
        if verdict != "KILLED":
            print(out[-3000:])
    killed = sum(1 for _i, v, _r, _d in rows if v == "KILLED")
    print("")
    print("=" * 72)
    print("R1_MUTATIONS = %d/%d KILLED" % (killed, len(rows)))
    for mid, verdict, reverts, _d in rows:
        print("  %-9s %s  (%s)" % (verdict, mid, reverts))
    return 0 if killed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
