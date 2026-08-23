#!/usr/bin/env python3
"""Global Hands-Off Acceptance Evidence Verifier.

A deterministic, READ-ONLY, pure evaluator that decides whether previously
*collected* evidence satisfies the HOME_VISIBLE and GLOBAL_HANDS_OFF_COMPLETE
acceptance contracts.

This module performs ZERO execution of any kind: no subprocess, no shell,
no provider launch, no network call, no git ref mutation, no Drive/GitHub
write, no credential access. It only inspects a caller-supplied evidence
dict (already gathered by other, already-existing subsystems -- e.g.
manager.canonical_baseline_guard.PromotionGateResult.to_dict() for the
Rule44 promotion gate, manager.continuation_states/continuation_decision
for the autonomous continuation chain, etc.) and answers one bounded
question per contract: PASS, NOT_READY, FAIL, or UNKNOWN.

Status semantics (never collapsed into each other):
  PASS       -- every required item is independently evidenced and correct.
  FAIL       -- supplied evidence proves a contract violation (a mismatch,
                a stale timestamp, a broken link, a duplicate, an explicit
                "false"/"diverged"/"read_only" fact).
  UNKNOWN    -- a required truth is simply absent from the supplied
                evidence; this verifier never infers a value that was not
                supplied, and never treats "missing" the same as "false".
  NOT_READY  -- the evidence itself reports a gate as not yet completed
                (e.g. an upstream contract's own status is NOT_READY /
                CONVERGENCE_REQUIRED, or an accepted/frozen flag is False).

This is intentionally a pure function of its input: `now` is always
injectable so tests never depend on wall-clock time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

STATUS_PASS = "PASS"
STATUS_NOT_READY = "NOT_READY"
STATUS_FAIL = "FAIL"
STATUS_UNKNOWN = "UNKNOWN"

_VALID_STATUSES = (STATUS_PASS, STATUS_NOT_READY, STATUS_FAIL, STATUS_UNKNOWN)

# Worst-wins combination order: a single FAIL anywhere makes the overall
# result FAIL, even if everything else is PASS; UNKNOWN outranks NOT_READY
# because "truth unavailable" is a stronger block than "known incomplete".
_STATUS_RANK = {STATUS_PASS: 0, STATUS_NOT_READY: 1, STATUS_UNKNOWN: 2, STATUS_FAIL: 3}

HOME_VISIBLE_MILESTONE_PASS = "HOME_VISIBLE_MILESTONE_PASS"
GLOBAL_HANDS_OFF_COMPLETE = "GLOBAL_HANDS_OFF_COMPLETE"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "reason": self.reason}


@dataclass(frozen=True)
class AcceptanceResult:
    label: str
    status: str
    checks: Sequence[CheckResult] = field(default_factory=tuple)

    @property
    def failing(self) -> List[CheckResult]:
        return [c for c in self.checks if c.status != STATUS_PASS]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "status": self.status,
            "checks": [c.to_dict() for c in self.checks],
            "failing": [c.to_dict() for c in self.failing],
        }


def _combine(statuses: Sequence[str]) -> str:
    worst = STATUS_PASS
    for status in statuses:
        if status not in _VALID_STATUSES:
            raise ValueError(f"unrecognized status: {status!r}")
        if _STATUS_RANK[status] > _STATUS_RANK[worst]:
            worst = status
    return worst


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _missing(name: str) -> CheckResult:
    return CheckResult(name, STATUS_UNKNOWN, f"no evidence supplied for {name!r}")


def _check_freshness(name: str, evidence: Dict[str, Any], ts_field: str, now: datetime) -> Optional[CheckResult]:
    """Returns a FAIL/UNKNOWN CheckResult if `ts_field` is stale/missing/
    malformed, or None if it is fresh (caller continues other checks)."""
    max_age = evidence.get("max_age_seconds")
    if not isinstance(max_age, (int, float)) or max_age <= 0:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: no valid max_age_seconds supplied")
    raw_ts = evidence.get(ts_field)
    ts = _parse_ts(raw_ts)
    if ts is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: {ts_field} is missing or unparseable")
    age = (now - ts).total_seconds()
    if age > max_age:
        return CheckResult(name, STATUS_FAIL,
                            f"{name}: {ts_field} is stale ({age:.0f}s old, max_age_seconds={max_age})")
    if age < 0:
        return CheckResult(name, STATUS_FAIL, f"{name}: {ts_field} is in the future relative to `now`")
    return None


# ---------------------------------------------------------------------------
# HOME_VISIBLE individual gate checks
# ---------------------------------------------------------------------------


def _check_formal_identity(evidence: Dict[str, Any]) -> CheckResult:
    name = "formal_target_identity"
    section = evidence.get("formal_identity")
    if not isinstance(section, dict):
        return _missing(name)
    expected, actual = section.get("expected"), section.get("actual")
    if not expected or not actual:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: expected/actual identity not both supplied")
    if expected != actual:
        return CheckResult(name, STATUS_FAIL, f"{name}: expected {expected!r} but actual is {actual!r}")
    return CheckResult(name, STATUS_PASS, f"{name}: {actual!r} confirmed")


def _check_runtime_state(evidence: Dict[str, Any]) -> CheckResult:
    name = "tested_activated_running_equal"
    section = evidence.get("runtime_state")
    if not isinstance(section, dict):
        return _missing(name)
    tested, activated, running = section.get("tested"), section.get("activated"), section.get("running")
    if not tested or not activated or not running:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: TESTED/ACTIVATED/RUNNING not all supplied")
    if not (tested == activated == running):
        return CheckResult(
            name, STATUS_FAIL,
            f"{name}: mismatch TESTED={tested!r} ACTIVATED={activated!r} RUNNING={running!r}",
        )
    return CheckResult(name, STATUS_PASS, f"{name}: all equal to {tested!r}")


def _check_remote_runtime_identity(evidence: Dict[str, Any]) -> CheckResult:
    name = "remote_runtime_identity"
    section = evidence.get("remote_runtime_identity")
    if not isinstance(section, dict):
        return _missing(name)
    expected, actual = section.get("expected_sha"), section.get("actual_sha")
    if not expected or not actual:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: expected_sha/actual_sha not both supplied")
    if expected != actual:
        return CheckResult(name, STATUS_FAIL,
                            f"{name}: remote/cloud runtime is on {actual!r}, expected {expected!r}")
    return CheckResult(name, STATUS_PASS, f"{name}: remote runtime matches expected {expected!r}")


def _check_bool_section(evidence: Dict[str, Any], key: str, flag: str, name: str) -> CheckResult:
    section = evidence.get(key)
    if not isinstance(section, dict):
        return _missing(name)
    value = section.get(flag)
    if value is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: {flag!r} not supplied")
    if value is not True:
        reason = section.get("reason") or f"{flag} is False"
        return CheckResult(name, STATUS_FAIL, f"{name}: {reason}")
    return CheckResult(name, STATUS_PASS, f"{name}: confirmed valid")


def _check_provider(evidence: Dict[str, Any], now: datetime) -> CheckResult:
    name = "usable_provider"
    section = evidence.get("provider")
    if not isinstance(section, dict):
        return _missing(name)
    for field_name in ("reliable", "usable"):
        if section.get(field_name) is None:
            return CheckResult(name, STATUS_UNKNOWN, f"{name}: {field_name!r} not supplied")
        if section.get(field_name) is not True:
            return CheckResult(name, STATUS_FAIL, f"{name}: provider is not {field_name}")
    remaining = section.get("remaining")
    if remaining is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: remaining quota not supplied")
    if not isinstance(remaining, (int, float)) or remaining <= 0:
        return CheckResult(name, STATUS_FAIL, f"{name}: remaining quota is {remaining!r} (must be > 0)")
    for ts_field in ("generated_at", "last_updated"):
        stale = _check_freshness(name, section, ts_field, now)
        if stale is not None:
            return stale
    return CheckResult(name, STATUS_PASS, f"{name}: {section.get('name', 'provider')} is fresh, reliable, usable")


_SMOKE_COMPONENTS = (
    "ingress", "task", "command", "execution", "provider_process_evidence",
    "session", "terminal_completion", "handoff",
)


def _check_smoke(evidence: Dict[str, Any], now: datetime) -> CheckResult:
    name = "automatic_provider_smoke"
    section = evidence.get("smoke")
    if not isinstance(section, dict):
        return _missing(name)

    stale = _check_freshness(name, section, "generated_at", now)
    if stale is not None:
        return stale

    if section.get("is_duplicate") is True:
        return CheckResult(name, STATUS_FAIL, f"{name}: smoke record is a duplicate")
    if section.get("reused_historical_request_id") is True:
        return CheckResult(name, STATUS_FAIL, f"{name}: request_id was reused from a historical record")
    if not section.get("request_id"):
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: request_id not supplied")

    components = section.get("components")
    if not isinstance(components, dict):
        return _missing(f"{name}.components")
    for component in _SMOKE_COMPONENTS:
        value = components.get(component)
        if value is None:
            return CheckResult(name, STATUS_UNKNOWN, f"{name}: component {component!r} not supplied")
        if value is not True:
            return CheckResult(name, STATUS_FAIL, f"{name}: component {component!r} is missing/false")

    links = section.get("links")
    if not isinstance(links, dict):
        return _missing(f"{name}.links")
    for link in ("execution_linked_to_session", "session_linked_to_handoff"):
        value = links.get(link)
        if value is None:
            return CheckResult(name, STATUS_UNKNOWN, f"{name}: link {link!r} not supplied")
        if value is not True:
            return CheckResult(name, STATUS_FAIL, f"{name}: {link.replace('_', ' ')} is broken")

    return CheckResult(name, STATUS_PASS, f"{name}: complete unique smoke {section.get('request_id')!r}")


def _check_no_duplicates(evidence: Dict[str, Any]) -> CheckResult:
    name = "no_duplicate_or_stale_leaked_execution"
    section = evidence.get("integrity")
    if not isinstance(section, dict):
        return _missing(name)
    if section.get("duplicate_found") is True:
        return CheckResult(name, STATUS_FAIL, f"{name}: a duplicate execution record was found")
    if section.get("stale_leaked_execution_found") is True:
        return CheckResult(name, STATUS_FAIL, f"{name}: a stale/leaked execution record was found")
    if section.get("duplicate_found") is None or section.get("stale_leaked_execution_found") is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: integrity flags not fully supplied")
    return CheckResult(name, STATUS_PASS, f"{name}: no duplicates or leaked executions")


def evaluate_home_visible(evidence: Dict[str, Any], *, now: Optional[datetime] = None) -> AcceptanceResult:
    """Evaluate the HOME_VISIBLE_MILESTONE_PASS contract against supplied
    evidence. Never executes anything; never infers a value not present in
    `evidence`."""
    now = _now(now)
    if not isinstance(evidence, dict):
        return AcceptanceResult(HOME_VISIBLE_MILESTONE_PASS, STATUS_UNKNOWN,
                                 (CheckResult("evidence", STATUS_UNKNOWN, "evidence must be a dict"),))

    checks = [
        _check_formal_identity(evidence),
        _check_runtime_state(evidence),
        _check_remote_runtime_identity(evidence),
        _check_bool_section(evidence, "workspace_root_authority", "valid", "workspace_root_authority"),
        _check_bool_section(evidence, "watcher_identity", "valid", "watcher_identity"),
        _check_bool_section(evidence, "dashboard_health", "healthy", "dashboard_health"),
        _check_bool_section(evidence, "dashboard_health", "session_center_healthy", "session_center_health"),
        _check_provider(evidence, now),
        _check_smoke(evidence, now),
        _check_no_duplicates(evidence),
        _check_bool_section(evidence, "status_agreement", "dashboard_agrees_with_record_chain", "status_agreement"),
    ]
    overall = _combine(c.status for c in checks)
    return AcceptanceResult(HOME_VISIBLE_MILESTONE_PASS, overall, tuple(checks))


# ---------------------------------------------------------------------------
# GLOBAL_HANDS_OFF_COMPLETE individual gate checks
# ---------------------------------------------------------------------------


def _check_upstream_status(evidence: Dict[str, Any], key: str, name: str) -> CheckResult:
    """For sub-gates that already produce their own bounded status (Rule44's
    promotion gate, etc.) -- propagate that status verbatim rather than
    re-deriving it, per 'consume existing evidence contracts'."""
    section = evidence.get(key)
    if not isinstance(section, dict):
        return _missing(name)
    status = section.get("status")
    if status not in _VALID_STATUSES:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: no recognized status supplied ({status!r})")
    reason = section.get("reason") or f"{name} reported {status}"
    return CheckResult(name, status, f"{name}: {reason}")


def _check_accepted_frozen(evidence: Dict[str, Any], key: str, name: str) -> CheckResult:
    section = evidence.get(key)
    if not isinstance(section, dict):
        return _missing(name)
    accepted, frozen = section.get("accepted"), section.get("frozen")
    if accepted is None or frozen is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: accepted/frozen not both supplied")
    if accepted is not True or frozen is not True:
        return CheckResult(name, STATUS_NOT_READY, f"{name}: not yet accepted+frozen (accepted={accepted}, frozen={frozen})")
    return CheckResult(name, STATUS_PASS, f"{name}: accepted and frozen")


_E2E_FLAG_FIELDS = (
    "project_correct", "governance_valid", "baseline_valid", "isolation_valid",
    "provider_execution_valid", "tests_passed", "commit_present", "pushed", "chatgpt_validation",
)
_E2E_LIFECYCLE_COMPONENTS = ("task", "command", "execution", "session", "handoff")


def _check_write_e2e(evidence: Dict[str, Any], key: str, name: str, now: datetime) -> CheckResult:
    section = evidence.get(key)
    if not isinstance(section, dict):
        return _missing(name)

    stale = _check_freshness(name, section, "generated_at", now)
    if stale is not None:
        return stale

    kind = section.get("kind")
    if kind is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: 'kind' (write/read_only) not supplied")
    if kind != "write":
        return CheckResult(name, STATUS_FAIL, f"{name}: is a {kind!r} E2E, not a write E2E")

    for flag in _E2E_FLAG_FIELDS:
        value = section.get(flag)
        if value is None:
            return CheckResult(name, STATUS_UNKNOWN, f"{name}: {flag!r} not supplied")
        if value is not True:
            return CheckResult(name, STATUS_FAIL, f"{name}: {flag!r} is false")

    lifecycle = section.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return _missing(f"{name}.lifecycle")
    for component in _E2E_LIFECYCLE_COMPONENTS:
        value = lifecycle.get(component)
        if value is None:
            return CheckResult(name, STATUS_UNKNOWN, f"{name}: lifecycle component {component!r} not supplied")
        if value is not True:
            return CheckResult(name, STATUS_FAIL, f"{name}: lifecycle component {component!r} is missing/false")

    return CheckResult(name, STATUS_PASS, f"{name}: complete write E2E")


def _check_autonomous_continuation(evidence: Dict[str, Any]) -> CheckResult:
    name = "autonomous_next_slice_continuation"
    section = evidence.get("autonomous_continuation")
    if not isinstance(section, dict):
        return _missing(name)
    proven = section.get("proven")
    manual_continue_required = section.get("manual_continue_required")
    boundary_stops = section.get("human_authorization_boundary_stops_correctly")
    if proven is None or manual_continue_required is None or boundary_stops is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: required fields not all supplied")
    if manual_continue_required is True:
        return CheckResult(name, STATUS_FAIL,
                            f"{name}: chain requires human '继续' between eligible slices")
    if boundary_stops is not True:
        return CheckResult(name, STATUS_FAIL,
                            f"{name}: human authorization boundary does not stop correctly")
    if proven is not True:
        return CheckResult(name, STATUS_NOT_READY, f"{name}: not yet proven")
    return CheckResult(name, STATUS_PASS, f"{name}: proven autonomous, boundary intact")


def evaluate_global_hands_off_complete(
    evidence: Dict[str, Any],
    *,
    home_visible_result: Optional[AcceptanceResult] = None,
    now: Optional[datetime] = None,
) -> AcceptanceResult:
    """Evaluate the GLOBAL_HANDS_OFF_COMPLETE contract.

    `home_visible_result` may be supplied directly (e.g. the result of a
    prior `evaluate_home_visible` call in the same process); otherwise this
    function looks for `evidence["home_visible"]` as a nested evidence dict
    and evaluates it itself. Either way, HOME_VISIBLE must independently
    PASS before anything else here can.
    """
    now = _now(now)
    if not isinstance(evidence, dict):
        return AcceptanceResult(GLOBAL_HANDS_OFF_COMPLETE, STATUS_UNKNOWN,
                                 (CheckResult("evidence", STATUS_UNKNOWN, "evidence must be a dict"),))

    if home_visible_result is None:
        nested = evidence.get("home_visible")
        if isinstance(nested, dict):
            home_visible_result = evaluate_home_visible(nested, now=now)
        else:
            home_visible_result = AcceptanceResult(
                HOME_VISIBLE_MILESTONE_PASS, STATUS_UNKNOWN,
                (CheckResult("home_visible", STATUS_UNKNOWN, "no home_visible evidence supplied"),),
            )

    checks = [
        CheckResult("home_visible_prerequisite", home_visible_result.status,
                    f"HOME_VISIBLE status is {home_visible_result.status}"),
        _check_upstream_status(evidence, "canonical_baseline", "canonical_baseline_authority"),
        _check_accepted_frozen(evidence, "direct_invoke", "direct_invoke"),
        _check_accepted_frozen(evidence, "continuation", "autonomous_continuation_foundation"),
        _check_upstream_status(evidence, "rule44", "rule44_evidence_verifier"),
        _check_write_e2e(evidence, "adm_write_e2e", "adm_repo_write_e2e", now),
        _check_write_e2e(evidence, "non_adm_write_e2e", "non_adm_repo_write_e2e", now),
        _check_autonomous_continuation(evidence),
    ]
    overall = _combine(c.status for c in checks)
    return AcceptanceResult(GLOBAL_HANDS_OFF_COMPLETE, overall, tuple(checks))
