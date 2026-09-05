"""Harness integrity gate. Aggregation is forbidden unless HARNESS_USABLE=YES.

Historical failures MUST remain in EVIDENCE_PRIOR_ROUNDS.md. This module
re-implements the gate that those failures escaped, so they stay killed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .kernel import KERNEL_ID, KERNEL_V5_AUTHORITATIVE, State

HARNESS_REQUIRED_AGENTS = ("liveness_runner", "safety_runner", "harness_mutator", "aggregator")

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
VALID_YN = {"YES", "NO"}


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


def harness_gate(
    roster: Sequence[str],
    results: Sequence[AgentResult],
    *,
    unresolved_failures: int = 0,
    kernel_id: str = KERNEL_ID,
    kernel_authoritative: bool = KERNEL_V5_AUTHORITATIVE,
    kernel_digest: Optional[str] = None,
    placeholder_kernel: bool = False,
    placeholder_input: bool = False,
    prose_required_agents: Sequence[str] = ("safety_runner",),
) -> HarnessReport:
    """Return HARNESS_USABLE. Any fail forbids aggregation."""
    reasons: List[str] = []
    details: Dict[str, Any] = {}

    missing = [a for a in HARNESS_REQUIRED_AGENTS if a not in roster]
    if missing:
        reasons.append(f"roster incomplete: missing {missing}")
    details["roster"] = list(roster)

    if unresolved_failures != 0:
        reasons.append(f"unresolved_failures={unresolved_failures} (must be 0)")

    if placeholder_kernel or kernel_id != KERNEL_ID or not kernel_authoritative:
        reasons.append("placeholder or non-authoritative kernel")
    if not kernel_digest:
        reasons.append("authoritative kernel digest not captured")
    if placeholder_input:
        reasons.append("placeholder input present")

    by_id = {r.agent_id: r for r in results}
    for agent in HARNESS_REQUIRED_AGENTS:
        if agent not in by_id and agent in roster:
            reasons.append(f"no result for rostered agent {agent}")

    for result in results:
        if result.verdict not in VALID_VERDICTS:
            reasons.append(f"{result.agent_id}: invalid verdict {result.verdict!r}")
        if result.error and _has_sentinel(result.error):
            reasons.append(f"{result.agent_id}: error sentinel {result.error!r}")
        if result.effective is not None and _has_sentinel(result.effective):
            reasons.append(f"{result.agent_id}: effective-result sentinel")
        if isinstance(result.effective, str) and result.effective.startswith("ERROR:"):
            reasons.append(f"{result.agent_id}: effective result is error string")
        if result.verdict == "ERROR":
            reasons.append(f"{result.agent_id}: verdict ERROR — not an effective result")
        if result.verdict == "BLOCKED" and result.block_is_degenerate is True:
            reasons.append(f"{result.agent_id}: BLOCKED + block_is_degenerate contradiction")
        if result.agent_id in prose_required_agents:
            if not result.usable:
                reasons.append(f"{result.agent_id}: prose-required agent not usable")
            if not result.prose or not str(result.prose).strip():
                reasons.append(f"{result.agent_id}: prose-required agent missing prose")
        # Safety results must separate original vs new variant (v4 measurement defect).
        if result.agent_id == "safety_runner" and result.verdict in {"PASS", "FAIL", "BLOCKED"}:
            if result.original_blocked not in VALID_YN or result.new_variant_found not in VALID_YN:
                reasons.append("safety_runner: original-vs-new-variant fields incomplete")

    usable = len(reasons) == 0
    details["kernel_id"] = kernel_id
    details["kernel_digest"] = kernel_digest
    details["authoritative"] = kernel_authoritative
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
    ]
