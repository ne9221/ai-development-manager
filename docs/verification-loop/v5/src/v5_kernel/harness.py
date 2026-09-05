"""Harness integrity gate. Aggregation is forbidden unless HARNESS_USABLE=YES.

Historical failures MUST remain in EVIDENCE_PRIOR_ROUNDS.md. This module
re-implements the gate that those failures escaped, so they stay killed.

Remediation Round 1 (B7) makes the gate fail closed:

  H1  the expected kernel digest is COMPUTED from the authoritative kernel
      bytes; the caller may only supply an *observed* digest to compare.
  H2  the roster and the safety attack set are EXACT expected sets. `0/0`
      can never pass.
  H3  every safety effective row must carry the full required schema.
  H4  replicated rows for one logical attack must agree; a PASS/FAIL flip
      forbids aggregation instead of letting the latest row win.
  H5  any required agent that is NOT_RUN / ERROR / unusable / sentinel-bearing
      makes the harness unusable, safety or not.
  H6  a non-empty expected roster with nothing collected is unusable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .kernel import KERNEL_ID, KERNEL_V5_AUTHORITATIVE, State, controller_trust_digest

HARNESS_REQUIRED_AGENTS = ("liveness_runner", "safety_runner", "harness_mutator", "aggregator")
SAFETY_AGENTS = ("safety_runner",)

ERROR_SENTINELS = (
    "ERROR: [object]",
    "[object]",
    "[object Object]",
    "undefined",
    "TypeError",
    "usage limit",
    "usage-limit",
)

VALID_VERDICTS = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "ERROR"}
# H5: a required agent may not sit in one of these and still be aggregated.
NON_EFFECTIVE_VERDICTS = {"NOT_RUN", "ERROR"}
VALID_YN = {"YES", "NO"}

# H3: the canonical schema of one safety effective row.
SAFETY_ROW_REQUIRED_FIELDS = (
    "attack_id",
    "original_attack_blocked",
    "new_variant_found",
    "status",
    "usable",
    "kernel_digest",
    "klass",
    "evidence",
)
# H4: replicated rows for the same attack must agree on these.
SAFETY_ROW_CONSENSUS_FIELDS = ("original_attack_blocked", "new_variant_found", "status")


@dataclass
class AgentResult:
    agent_id: str
    usable: bool
    verdict: str
    prose: Optional[str] = None
    error: Optional[str] = None
    original_blocked: Optional[str] = None
    new_variant_found: Optional[str] = None
    block_is_degenerate: Optional[bool] = None
    effective: Any = None


@dataclass
class HarnessReport:
    usable: bool
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def HARNESS_USABLE(self) -> str:
        return "YES" if self.usable else "NO"


def _has_sentinel(value: Any) -> bool:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    lowered = text.lower()
    for sent in ERROR_SENTINELS:
        if sent.lower() in lowered:
            return True
    if text.strip() in {"[object]", "[object Object]"}:
        return True
    return False


def safety_row(attack_id, original_attack_blocked, new_variant_found, klass, evidence, kernel_digest, usable=True, status="PASS"):
    """Build one schema-complete safety row. Callers must not hand-roll dicts."""
    return {
        "attack_id": attack_id,
        "original_attack_blocked": "YES" if original_attack_blocked else "NO",
        "new_variant_found": "YES" if new_variant_found else "NO",
        "status": status,
        "usable": bool(usable),
        "kernel_digest": kernel_digest,
        "klass": klass,
        "evidence": evidence,
    }


def _check_safety_rows(
    rows: Any,
    expected_attack_ids: Optional[Iterable[str]],
    authoritative_digest: str,
) -> List[str]:
    reasons: List[str] = []
    if not isinstance(rows, (list, tuple)):
        return ["safety effective result is not a list of rows"]

    # H6 / H2: a non-empty expected set with nothing collected is never usable.
    expected = set(expected_attack_ids or ())
    if not expected:
        return ["expected safety attack roster is empty — 0/0 cannot be PASS"]
    if len(rows) == 0:
        return ["safety collection is empty while %d attacks are expected" % len(expected)]

    seen: Dict[str, Dict[str, Any]] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, Mapping):
            reasons.append("safety row %d is not a mapping" % idx)
            continue
        # H3: full schema, every field, every row.
        missing = [f for f in SAFETY_ROW_REQUIRED_FIELDS if f not in row]
        if missing:
            reasons.append(
                "safety row %r missing required fields %s" % (row.get("attack_id", "<no attack_id>"), sorted(missing))
            )
            continue
        aid = str(row["attack_id"])
        if row["original_attack_blocked"] not in VALID_YN:
            reasons.append("safety row %s: original_attack_blocked=%r not YES/NO" % (aid, row["original_attack_blocked"]))
        if row["new_variant_found"] not in VALID_YN:
            reasons.append("safety row %s: new_variant_found=%r not YES/NO" % (aid, row["new_variant_found"]))
        if row["status"] not in VALID_VERDICTS:
            reasons.append("safety row %s: invalid status %r" % (aid, row["status"]))
        if row["status"] in NON_EFFECTIVE_VERDICTS:
            reasons.append("safety row %s: status %s is not an effective result" % (aid, row["status"]))
        if not row["usable"]:
            reasons.append("safety row %s: row marked not usable" % aid)
        if row["kernel_digest"] != authoritative_digest:
            reasons.append("safety row %s: kernel_digest does not match the authoritative kernel" % aid)
        if _has_sentinel(row.get("evidence")):
            reasons.append("safety row %s: evidence contains an error sentinel" % aid)

        # H4: replication divergence forbids aggregation. Latest never wins.
        prior = seen.get(aid)
        if prior is None:
            seen[aid] = dict(row)
        else:
            diverged = [f for f in SAFETY_ROW_CONSENSUS_FIELDS if prior.get(f) != row.get(f)]
            if diverged:
                reasons.append(
                    "safety attack %s replicated with divergent %s (%s vs %s) — aggregation forbidden"
                    % (
                        aid,
                        sorted(diverged),
                        {f: prior.get(f) for f in diverged},
                        {f: row.get(f) for f in diverged},
                    )
                )

    collected = set(seen)
    absent = sorted(expected - collected)
    unexpected = sorted(collected - expected)
    if absent:
        reasons.append("safety roster incomplete: expected attacks not collected %s" % absent)
    if unexpected:
        reasons.append("safety roster has attacks outside the expected set %s" % unexpected)
    return reasons


def harness_gate(
    roster: Sequence[str],
    results: Sequence[AgentResult],
    *,
    expected_roster: Sequence[str] = HARNESS_REQUIRED_AGENTS,
    expected_safety_attack_ids: Optional[Sequence[str]] = None,
    unresolved_failures: int = 0,
    kernel_id: str = KERNEL_ID,
    kernel_authoritative: bool = KERNEL_V5_AUTHORITATIVE,
    observed_kernel_digest: Optional[str] = None,
    authoritative_kernel_digest: Optional[str] = None,
    placeholder_kernel: bool = False,
    placeholder_input: bool = False,
    prose_required_agents: Sequence[str] = ("safety_runner",),
    required_agents: Optional[Sequence[str]] = None,
) -> HarnessReport:
    """Return HARNESS_USABLE. Any fail forbids aggregation."""
    reasons: List[str] = []
    details: Dict[str, Any] = {}

    expected = tuple(expected_roster or ())
    required = tuple(required_agents if required_agents is not None else expected)

    # H1: the expected digest is derived from the authoritative kernel bytes.
    # The caller supplies only what it observed.
    authoritative = authoritative_kernel_digest or controller_trust_digest()
    if not observed_kernel_digest:
        reasons.append("authoritative kernel digest not captured")
    elif observed_kernel_digest != authoritative:
        reasons.append(
            "observed kernel digest %s... does not match the authoritative kernel digest %s..."
            % (str(observed_kernel_digest)[:12], authoritative[:12])
        )

    # H2 / H6: exact roster, never a subset, never empty.
    if not expected:
        reasons.append("expected roster is empty — nothing to certify")
    missing = sorted(set(expected) - set(roster))
    extra = sorted(set(roster) - set(expected))
    if missing:
        reasons.append("roster incomplete: missing %s" % missing)
    if extra:
        reasons.append("roster has agents outside the expected set: %s" % extra)
    if expected and not results:
        reasons.append("expected roster is non-empty but no agent results were collected")
    details["roster"] = list(roster)
    details["expected_roster"] = list(expected)

    if unresolved_failures != 0:
        reasons.append("unresolved_failures=%s (must be 0)" % unresolved_failures)

    if placeholder_kernel or kernel_id != KERNEL_ID or not kernel_authoritative:
        reasons.append("placeholder or non-authoritative kernel")
    if placeholder_input:
        reasons.append("placeholder input present")

    by_id = {r.agent_id: r for r in results}
    for agent in required:
        if agent not in by_id:
            reasons.append("no result for required agent %s" % agent)

    for result in results:
        is_required = result.agent_id in required
        if result.verdict not in VALID_VERDICTS:
            reasons.append("%s: invalid verdict %r" % (result.agent_id, result.verdict))
        if result.error and _has_sentinel(result.error):
            reasons.append("%s: error sentinel %r" % (result.agent_id, result.error))
        if result.effective is not None and _has_sentinel(result.effective):
            reasons.append("%s: effective-result sentinel" % result.agent_id)
        if isinstance(result.effective, str) and result.effective.startswith("ERROR:"):
            reasons.append("%s: effective result is error string" % result.agent_id)
        if result.verdict == "ERROR":
            reasons.append("%s: verdict ERROR — not an effective result" % result.agent_id)
        # H5: this applies to EVERY required agent, not only the prose-required ones.
        if is_required:
            if not result.usable:
                reasons.append("%s: required agent is not usable" % result.agent_id)
            if result.verdict in NON_EFFECTIVE_VERDICTS:
                reasons.append("%s: required agent verdict %s is not an effective result" % (result.agent_id, result.verdict))
            if result.effective is None:
                reasons.append("%s: required agent produced no effective result" % result.agent_id)
        if result.verdict == "BLOCKED" and result.block_is_degenerate is True:
            reasons.append("%s: BLOCKED + block_is_degenerate contradiction" % result.agent_id)
        if result.agent_id in prose_required_agents:
            if not result.prose or not str(result.prose).strip():
                reasons.append("%s: prose-required agent missing prose" % result.agent_id)
        # Safety results must separate original vs new variant (v4 measurement defect).
        if result.agent_id in SAFETY_AGENTS:
            if result.verdict in {"PASS", "FAIL", "BLOCKED"}:
                if result.original_blocked not in VALID_YN or result.new_variant_found not in VALID_YN:
                    reasons.append("%s: original-vs-new-variant fields incomplete" % result.agent_id)
            reasons.extend(_check_safety_rows(result.effective, expected_safety_attack_ids, authoritative))

    usable = len(reasons) == 0
    details["kernel_id"] = kernel_id
    details["observed_kernel_digest"] = observed_kernel_digest
    details["authoritative_kernel_digest"] = authoritative
    details["authoritative"] = kernel_authoritative
    details["expected_safety_attack_ids"] = sorted(expected_safety_attack_ids or ())
    return HarnessReport(usable=usable, reasons=reasons, details=details)


def historical_failure_records() -> List[Dict[str, str]]:
    """Pinned memory of real harness failures. Do not delete or rewrite as success."""
    return [
        {
            "id": "HF-USAGE-LIMIT-SWALLOWED",
            "what": "agent usage-limit failure was incorrectly swallowed",
            "gate": "unresolved_failures must be 0; usage-limit is ERROR not PASS",
            "status": "MUST_REMAIN_ON_RECORD",
        },
        {
            "id": "HF-OBJECT-SENTINEL",
            "what": "[object] sentinel was treated as a result",
            "gate": "effective results must not contain error sentinels",
            "status": "MUST_REMAIN_ON_RECORD",
        },
        {
            "id": "HF-PROSE-VERDICT-DIVERGENCE",
            "what": "prose divergence was incorrectly treated as verdict divergence",
            "gate": "prose-required agents usable; verdict enums validated separately from prose",
            "status": "MUST_REMAIN_ON_RECORD",
        },
        {
            "id": "HF-PLACEHOLDER-KERNEL",
            "what": "placeholder kernel nearly entered the attack fleet",
            "gate": "authoritative kernel digest captured; KERNEL_V5_AUTHORITATIVE required",
            "status": "MUST_REMAIN_ON_RECORD",
        },
        {
            "id": "HF-R1-FABRICATED-DIGEST",
            "what": "a fabricated kernel digest ('deadbeef') satisfied the digest gate, which only "
                    "checked that a digest was non-empty",
            "gate": "observed digest must equal the digest computed from authoritative kernel bytes",
            "status": "MUST_REMAIN_ON_RECORD",
        },
        {
            "id": "HF-R1-EMPTY-SAFETY-ROSTER",
            "what": "an empty safety roster aggregated as 0 attacks, 0 blocked, HARNESS_USABLE=YES, exit 0",
            "gate": "expected attack set must be non-empty and exactly collected",
            "status": "MUST_REMAIN_ON_RECORD",
        },
    ]
