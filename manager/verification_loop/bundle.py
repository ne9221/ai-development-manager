"""Frozen Acceptance Bundle hashing and integrity validation.

The bundle hash is the mechanism behind three separate Phase A v3 attack
rejections, which is why it covers so much: golden files, snapshot digests, the
identities and implementation digests of every checker, the baseline
attestation, the risk and impact rules, the transient allowlists and the
budgets all feed it. Consequences worth stating plainly:

* Editing a golden file changes ``bundle_hash``. Every existing report is bound
  to the old hash, so all of them stop being admissible at once. "Adjust the
  golden until the candidate passes" has no expression inside one bundle -- it
  necessarily produces a new bundle, which needs human approval.
* Raising a budget changes ``bundle_hash`` too. A budget stored on a writable
  Task field could be raised by the loop it is supposed to bound; a budget
  inside a content-addressed bundle cannot.
* Bumping a checker's version without changing its implementation digest, or
  vice versa, is visible.

``validate_bundle`` additionally rejects a *non-monotone* gate matrix. Phase
B-1's independent review found that escalating medium -> artifact_sensitive
with a bundle whose upper tier listed fewer gates dropped two gates and
ACCEPTed -- escalating risk made verification weaker. A gate matrix where a
higher tier does not require everything a lower tier requires is treated as a
defective contract, not as configuration to obey.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional, Sequence, Tuple

from .models import (
    RISK_LEVELS,
    AcceptanceBundleFixture,
    risk_rank,
)


def _plain(value: Any) -> Any:
    """Recursively reduce a value to JSON-serialisable primitives.

    Dataclasses are walked by field so that adding a field to any bundle member
    automatically widens hash coverage; nothing has to be re-listed here. Sets
    are sorted so an unordered collection cannot produce two different hashes
    for the same content.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _plain(getattr(value, name))
            for name in sorted(value.__dataclass_fields__)
        }
    if isinstance(value, Mapping):
        return {str(k): _plain(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def canonical_json(value: Any) -> str:
    """Stable JSON text for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_bundle_hash(bundle: AcceptanceBundleFixture) -> str:
    """The hash ``bundle.bundle_hash`` is required to equal.

    Every field except ``bundle_hash`` itself is covered. Enumerating by
    exclusion rather than by inclusion is the point: a future field is covered
    the moment it is added, so nobody can widen the bundle with something
    unhashed and therefore silently mutable.
    """
    covered = {
        name: getattr(bundle, name)
        for name in sorted(bundle.__dataclass_fields__)
        if name != "bundle_hash"
    }
    return "ab-" + _sha256(canonical_json(covered))


def finalize_bundle(**fields: Any) -> AcceptanceBundleFixture:
    """Build a bundle whose ``bundle_hash`` is its own content hash.

    Used by fixtures and by any caller assembling a bundle, so a bundle is
    content-addressed by construction and a hand-written hash never has to be
    kept in sync by hand.
    """
    draft = AcceptanceBundleFixture(bundle_hash="", **fields)
    return AcceptanceBundleFixture(bundle_hash=compute_bundle_hash(draft), **fields)


def oracle_digest(bundle: AcceptanceBundleFixture, gate_id: str) -> Optional[str]:
    """Expected ``oracle_set_digest`` for a gate, or None if it references none.

    Derived from the referenced oracle's member digests rather than stored as
    an opaque per-gate constant, so editing a frozen golden changes what the
    gate's reports must declare, automatically.
    """
    oracle_id = bundle.gate_oracle_refs.get(gate_id)
    if oracle_id is None:
        return None
    oracle = bundle.oracle(oracle_id)
    if oracle is None:
        # A gate referencing an oracle the bundle does not define is a broken
        # contract; return a digest nothing can match rather than None, so the
        # gate fails closed instead of skipping the check.
        return "MISSING_ORACLE:" + oracle_id
    return _sha256(canonical_json({"oracle_id": oracle.oracle_id, "members": oracle.members}))


def validate_bundle(bundle: AcceptanceBundleFixture) -> Tuple[str, ...]:
    """Reasons this bundle is not a usable contract; empty tuple if it is.

    Any non-empty result is admitted as ACCEPTANCE_CONTRACT_DEFECT by the
    evaluator -- a malformed contract routes to human bundle revision, it is
    never worked around and never silently obeyed.
    """
    reasons: list[str] = []

    if bundle.bundle_hash != compute_bundle_hash(bundle):
        reasons.append("BUNDLE_HASH_MISMATCH")

    for level, gates in bundle.gate_requirements_by_risk.items():
        if level not in RISK_LEVELS:
            reasons.append("UNKNOWN_RISK_TIER:" + str(level))
            continue
        for gate_id in gates:
            if gate_id not in bundle.checkers:
                reasons.append("REQUIRED_GATE_WITHOUT_CHECKER:" + gate_id)

    reasons.extend(_monotonicity_reasons(bundle))

    for gate_id in bundle.gate_oracle_refs:
        if bundle.oracle(bundle.gate_oracle_refs[gate_id]) is None:
            reasons.append("GATE_ORACLE_UNDEFINED:" + gate_id)

    budget = bundle.budget
    for name in ("max_transient_retries", "max_repair_executions", "max_review_rounds"):
        if getattr(budget, name) < 0:
            reasons.append("NEGATIVE_BUDGET:" + name)

    # Deduplicated but order-stable, so the reason list is deterministic.
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return tuple(seen)


def _monotonicity_reasons(bundle: AcceptanceBundleFixture) -> Sequence[str]:
    """Every tier must require a superset of every lower tier's gates.

    "production" is excluded because it deliberately declares no gates: a
    production-tier execution routes to the Production Fix Protocol instead of
    being verified here, so it is not a weaker tier, it is a different path.
    """
    reasons: list[str] = []
    declared = [
        level
        for level in RISK_LEVELS
        if level != "production" and level in bundle.gate_requirements_by_risk
    ]
    for higher in declared:
        for lower in declared:
            if risk_rank(lower) >= risk_rank(higher):
                continue
            missing = set(bundle.gate_requirements_by_risk[lower]) - set(
                bundle.gate_requirements_by_risk[higher]
            )
            if missing:
                reasons.append(
                    "NON_MONOTONE_GATE_MATRIX:"
                    + higher
                    + "<"
                    + lower
                    + ":"
                    + ",".join(sorted(missing))
                )
    return reasons


def record_digest(record: Any) -> str:
    """Content digest of any append-only ledger record.

    One function for reports and tickets alike, because both ledgers make the
    same promise: the name a record is filed under is the hash of what it
    says. A second hashing scheme would be a second promise to keep in sync.
    """
    return _sha256(canonical_json(record))


def report_digest(report: Any) -> str:
    """Content digest of a verification report.

    Used as the report's identity in ``derivation_key``, as the create-only key
    in the report store, and as the value a ticket records when it is consumed
    (Phase A v3 predicate 11) -- so two byte-different reports can never occupy
    one slot and an unchanged report re-submitted is genuinely idempotent.
    """
    return record_digest(report)
