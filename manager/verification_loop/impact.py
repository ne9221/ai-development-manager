"""Which gates a diff invalidates, and therefore which must be re-run.

Impact rules are a cost optimisation and nothing more. Phase A v3 A.5 is
explicit that the *final* accepted SHA must still carry a valid PASS for every
required gate at that same SHA -- targeted re-runs during intermediate repair
rounds do not substitute for that. This module therefore only ever answers
"which gates must be re-run now"; it has no power to shrink the required-gate
set the evaluator demands before ACCEPTED.

That separation is what keeps attack 8 closed. If impact rules could narrow the
final requirement, a repair could assemble an acceptance out of V3 measured at
one SHA and V4 measured at another. They cannot, so it cannot.

``unmatched_path_policy`` is fixed at RERUN_ALL rather than configurable. A
path no rule mentions is a path whose blast radius is unknown, and the fail-
closed reading of unknown is "everything", not "nothing".
"""

from __future__ import annotations

from typing import Sequence, Tuple

from .models import AcceptanceBundleFixture
from .paths import canonicalise_all, path_matches_prefix

RERUN_ALL = "RERUN_ALL"


def affected_gates(
    diff_paths: Sequence[object],
    bundle: AcceptanceBundleFixture,
    required: Sequence[str],
) -> Tuple[Tuple[str, ...], str]:
    """``(gates_to_rerun, policy)`` where policy is "BY_PATH" or "RERUN_ALL".

    Returns every required gate under RERUN_ALL when any diff path is unmatched
    by the rules, or unparseable, or when there are no rules at all. Only a diff
    whose every path is explicitly accounted for earns a narrowed re-run set.
    """
    canonical, rejected = canonicalise_all(diff_paths)

    if rejected:
        # A path we could not canonicalise could be anywhere; narrowing on the
        # strength of the paths we *could* read would be reasoning from an
        # incomplete diff.
        return tuple(required), RERUN_ALL

    if not bundle.impact_rules:
        return tuple(required), RERUN_ALL

    gates: list[str] = [g for g in bundle.impact_always_rerun]
    for path in canonical:
        matched = False
        for rule in bundle.impact_rules:
            if path_matches_prefix(path, rule.path_prefix):
                matched = True
                for gate_id in rule.gates:
                    if gate_id not in gates:
                        gates.append(gate_id)
        if not matched:
            return tuple(required), RERUN_ALL

    # Never widen beyond what the tier actually requires, and never claim a
    # gate the bundle does not define.
    ordered = tuple(g for g in required if g in gates)
    return ordered, "BY_PATH"
