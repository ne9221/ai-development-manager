"""Effective risk derivation and the required-gate set that follows from it.

Phase A v3 E.1: ``effective_risk = max(declared, diff-derived, component)``.
Phase B-1 implemented only the first two terms, and matched diff paths with a
raw ``startswith`` -- the review evaded the production rule three ways by
respelling a path. Both halves are fixed here: matching goes through
``paths.path_matches_prefix``, and the third term exists.

The declared risk is a *floor*, never a ceiling. That is the whole point of
calling it declared: whoever declares it is the party whose work is being
gated, so it can only ever raise the tier, never lower what the diff or the
environment already demand.

A diff path that cannot be canonicalised (absolute, drive-lettered, or
containing ``..``) is not silently skipped. It is reported, and the evaluator
turns it into UNKNOWN -- a path we cannot reason about must not quietly match
no rules and therefore escalate nothing.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from .models import (
    RISK_LEVELS,
    AcceptanceBundleFixture,
    ExecutionFixture,
    PreflightFacts,
    TaskFixture,
    risk_rank,
)
from .paths import canonicalise_all, path_matches_prefix

# Components the preflight can observe. Named here so a bundle's
# component_escalation_rules and the measurement below cannot drift apart
# silently.
COMPONENT_PRODUCTION_CHECKOUT = "production_canonical_checkout"
COMPONENT_PRODUCTION_HOME = "production_ai_manager_home"


def _same_location(left: Optional[str], right: Optional[str]) -> bool:
    """Compare two filesystem locations for the production blacklist.

    Casefolded and separator-normalised because the blacklist must not be
    evadable by spelling: on Windows the same directory has many spellings and
    all of them are the production checkout.
    """
    if not left or not right:
        return False
    return (
        str(left).replace("\\", "/").rstrip("/").casefold()
        == str(right).replace("\\", "/").rstrip("/").casefold()
    )


def _is_within(child: Optional[str], parent: Optional[str]) -> bool:
    if not child or not parent:
        return False
    child_key = str(child).replace("\\", "/").rstrip("/").casefold()
    parent_key = str(parent).replace("\\", "/").rstrip("/").casefold()
    return child_key == parent_key or child_key.startswith(parent_key + "/")


def observed_components(
    bundle: AcceptanceBundleFixture, preflight: PreflightFacts
) -> Tuple[str, ...]:
    """Which escalation-triggering components this round actually ran against.

    Three independent signals can name the worktree as production, and any one
    of them is enough. They are ORed rather than ranked because they fail in
    different ways: the bundle blacklist can be out of date, the marker file
    can be missing on a fresh clone, and a caller can pass a subdirectory.
    """
    components: list[str] = []

    if preflight.worktree_is_marked_production:
        components.append(COMPONENT_PRODUCTION_CHECKOUT)
    else:
        for blacklisted in bundle.production_canonical_checkout_paths:
            if _is_within(preflight.worktree_path, blacklisted):
                components.append(COMPONENT_PRODUCTION_CHECKOUT)
                break

    if _same_location(preflight.ai_manager_home, bundle.production_ai_manager_home):
        components.append(COMPONENT_PRODUCTION_HOME)
    elif preflight.ai_manager_home_class == "production":
        components.append(COMPONENT_PRODUCTION_HOME)

    return tuple(dict.fromkeys(components))


def effective_risk(
    task: TaskFixture,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
    preflight: PreflightFacts,
) -> Tuple[str, Tuple[str, ...]]:
    """``(effective_risk, uncanonicalisable_diff_paths)``.

    Recomputed against the *current* candidate's diff every round: a repair
    changes the diff, and the new diff may be riskier than the one that
    prompted it.
    """
    rank = risk_rank(task.declared_risk)

    canonical_paths, rejected = canonicalise_all(execution.diff_paths)

    for rule in bundle.risk_rules:
        if any(path_matches_prefix(path, rule.path_prefix) for path in canonical_paths):
            rank = max(rank, risk_rank(rule.forced_risk))

    components = observed_components(bundle, preflight)
    for rule in bundle.component_escalation_rules:
        if rule.component in components:
            rank = max(rank, risk_rank(rule.forced_risk))

    return RISK_LEVELS[rank], rejected


def required_gates(bundle: AcceptanceBundleFixture, risk: str) -> Tuple[str, ...]:
    """Required gates at ``risk``, unioned with every lower tier's.

    The union is defence in depth rather than a substitute for validation: a
    non-monotone matrix is already reported as a contract defect by
    ``bundle.validate_bundle``, and this makes sure that even while that defect
    is being surfaced, escalating risk cannot *reduce* what is required.
    """
    if risk == "production":
        # Production routes to the Production Fix Protocol; this loop declares
        # no gates for it, because it is structurally unable to answer them.
        return ()
    gates: list[str] = []
    ceiling = risk_rank(risk)
    for level in RISK_LEVELS:
        if level == "production" or risk_rank(level) > ceiling:
            continue
        for gate_id in bundle.gate_requirements_by_risk.get(level, ()):
            if gate_id not in gates:
                gates.append(gate_id)
    return tuple(gates)


def production_write_gates(
    bundle: AcceptanceBundleFixture, gate_ids: Sequence[str]
) -> Tuple[str, ...]:
    """Gates among ``gate_ids`` that write to production.

    Phase A v3 I5: this loop cannot express a PASS for any of them, so their
    presence forces DEFERRED_TO_PFP regardless of what any report claims.
    """
    return tuple(
        gate_id
        for gate_id in gate_ids
        if gate_id in bundle.checkers and bundle.checkers[gate_id].production_write
    )
