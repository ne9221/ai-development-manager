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


def _check_home_target_identity(evidence: Dict[str, Any]) -> CheckResult:
    """A single authoritative identity check: FORMAL_REMOTE, TESTED,
    ACTIVATED, RUNNING, and the REMOTE/CLOUD runtime SHA must all equal
    one top-level `expected_target_sha` -- not merely equal each other
    pairwise within their own sections. Three independently-PASSing
    sections (formal=AAA, TESTED/ACTIVATED/RUNNING=BBB, remote=CCC) must
    never combine into an overall PASS; cross-checking against one
    externally supplied target is what rules that out. A missing
    expected_target_sha, or any missing component identity, is UNKNOWN
    (truth unavailable) -- any supplied identity that disagrees with the
    expected target, or with any other supplied identity, is FAIL."""
    name = "home_target_identity"
    expected = evidence.get("expected_target_sha")

    formal = evidence.get("formal_identity")
    runtime = evidence.get("runtime_state")
    remote = evidence.get("remote_runtime_identity")
    if not isinstance(formal, dict) or not isinstance(runtime, dict) or not isinstance(remote, dict):
        return _missing(name)

    values = {
        "formal_remote": formal.get("actual"),
        "tested": runtime.get("tested"),
        "activated": runtime.get("activated"),
        "running": runtime.get("running"),
        "remote_cloud_runtime": remote.get("actual_sha"),
    }

    if not expected:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: expected_target_sha not supplied")
    missing = sorted(k for k, v in values.items() if not v)
    if missing:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: missing identity value(s) for {missing}")

    if any(v != expected for v in values.values()):
        detail = ", ".join(f"{k}={v!r}" for k, v in values.items())
        return CheckResult(
            name, STATUS_FAIL,
            f"{name}: identities diverge from expected_target_sha={expected!r} ({detail})",
        )
    return CheckResult(name, STATUS_PASS, f"{name}: FORMAL_REMOTE=TESTED=ACTIVATED=RUNNING=REMOTE/CLOUD_RUNTIME={expected!r}")


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


def _check_automatic_selection(section: Dict[str, Any], name: str) -> Optional[CheckResult]:
    """Returns a FAIL/UNKNOWN CheckResult if the smoke's provider/account
    selection cannot be proven automatic, else None (caller continues to the
    remaining lifecycle checks). Lifecycle completeness alone never proves
    automatic selection -- a manually pinned provider/account can complete
    a perfectly valid lifecycle and still not be the automatic-dispatch
    smoke HOME_VISIBLE requires, so this is checked independently of, and
    before, the lifecycle component checks below."""
    automatic = section.get("automatic_selection")
    if automatic is None:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: automatic_selection not supplied")
    if automatic is not True:
        return CheckResult(name, STATUS_FAIL, f"{name}: automatic_selection is False (caller-pinned dispatch)")
    if section.get("requested_provider"):
        return CheckResult(
            name, STATUS_FAIL,
            f"{name}: requested_provider {section.get('requested_provider')!r} was caller-pinned, not automatic",
        )
    if section.get("requested_account_id"):
        return CheckResult(
            name, STATUS_FAIL,
            f"{name}: requested_account_id {section.get('requested_account_id')!r} was caller-pinned, not automatic",
        )
    if not section.get("selected_provider"):
        return CheckResult(name, STATUS_UNKNOWN,
                            f"{name}: selected_provider (ADM's own automatic choice) not supplied")
    return None


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

    not_automatic = _check_automatic_selection(section, name)
    if not_automatic is not None:
        return not_automatic

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
        _check_home_target_identity(evidence),
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


# Known upstream status vocabulary that doesn't match this module's own
# PASS/NOT_READY/FAIL/UNKNOWN spelling, mapped at this adapter boundary
# only -- this is not a redefinition of what UNKNOWN means globally, it is
# a translation of one specific real upstream value (manager.
# canonical_baseline_guard.STATUS_CONVERGENCE_REQUIRED) into the vocabulary
# this evaluator already uses for "known incomplete, not yet passable".
_UPSTREAM_STATUS_ALIASES = {
    "CONVERGENCE_REQUIRED": STATUS_NOT_READY,
}


# Timestamp field names accepted for time-sensitive upstream evidence, in
# preference order -- the first one present in the section is used.
_UPSTREAM_TIMESTAMP_FIELDS = ("generated_at", "captured_at")


def _check_upstream_freshness(name: str, section: Dict[str, Any], now: datetime) -> Optional[CheckResult]:
    """Returns a FAIL/UNKNOWN CheckResult if the upstream section's own
    freshness evidence (a generated_at/captured_at timestamp bounded by
    max_age_seconds) is missing, malformed, stale, or from the future; else
    None (caller proceeds to read the propagated status). A historical
    PASS from a long-finished run must not be silently reusable forever --
    it must be re-proven fresh, exactly like every other time-sensitive
    check in this module."""
    max_age = section.get("max_age_seconds")
    if not isinstance(max_age, (int, float)) or max_age <= 0:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: no valid max_age_seconds supplied")
    raw_ts = next((section.get(f) for f in _UPSTREAM_TIMESTAMP_FIELDS if section.get(f) is not None), None)
    ts = _parse_ts(raw_ts)
    if ts is None:
        return CheckResult(
            name, STATUS_UNKNOWN,
            f"{name}: none of {_UPSTREAM_TIMESTAMP_FIELDS} is present/parseable",
        )
    age = (now - ts).total_seconds()
    if age < 0:
        return CheckResult(name, STATUS_FAIL, f"{name}: timestamp is in the future relative to `now`")
    if age > max_age:
        return CheckResult(name, STATUS_FAIL, f"{name}: timestamp is stale ({age:.0f}s old, max_age_seconds={max_age})")
    return None


def _check_upstream_status(evidence: Dict[str, Any], key: str, name: str, now: datetime) -> CheckResult:
    """For sub-gates that already produce their own bounded status (Rule44's
    promotion gate, etc.) -- propagate that status verbatim rather than
    re-deriving it, per 'consume existing evidence contracts'. Known upstream
    aliases (see _UPSTREAM_STATUS_ALIASES) are normalized to this module's
    vocabulary first; anything else unrecognized is still UNKNOWN, never
    silently coerced.

    These upstream gates report live repo/baseline truth, which can change
    after the evidence was captured, so a PASS is only trusted if it also
    carries its own bounded freshness evidence (see
    _check_upstream_freshness) -- a stale PASS is FAIL, not a free pass to
    reuse an old snapshot."""
    section = evidence.get(key)
    if not isinstance(section, dict):
        return _missing(name)
    stale = _check_upstream_freshness(name, section, now)
    if stale is not None:
        return stale
    raw_status = section.get("status")
    status = _UPSTREAM_STATUS_ALIASES.get(raw_status, raw_status)
    if status not in _VALID_STATUSES:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: no recognized status supplied ({raw_status!r})")
    reason = section.get("reason") or f"{name} reported {raw_status}"
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


# The canonical ADM project id, used only as a default -- a caller may
# override it via evidence["expected_adm_project_id"] with another
# explicitly trusted canonical id. Project identity is always read from
# the E2E evidence's own `project_id` field; the "adm_write_e2e" /
# "non_adm_write_e2e" *dict key names* are never treated as proof of which
# project an E2E actually ran against.
#
# TRUST BOUNDARY (for the future live evidence assembler, not this pure
# slice): this verifier accepts expected_adm_project_id from the generic
# `evidence` dict because it has no live config of its own to consult --
# it only evaluates whatever it is handed. That is acceptable ONLY because
# nothing in this repo yet assembles `evidence` from untrusted input. Once
# a live caller wires real dispatch/user-supplied data into `evidence`,
# that caller MUST source expected_adm_project_id from trusted internal
# project configuration (e.g. the ADM project registry), never from
# dispatch payloads or any other caller/user-controlled evidence -- an
# attacker who could set expected_adm_project_id could rename their own
# project to "be" the canonical ADM project and defeat this check. Do not
# redesign this verifier around live config in this slice; this is a
# constraint on the assembler that will eventually populate `evidence`.
DEFAULT_ADM_PROJECT_ID = "ai-development-manager"


def _check_distinct_write_e2e_projects(evidence: Dict[str, Any]) -> CheckResult:
    """Two write E2Es that are structurally identical (same project, same
    request) prove nothing about cross-project isolation -- both slots
    passing `_check_write_e2e` independently is not sufficient. This check
    additionally proves the two E2Es are genuinely distinct: the ADM slot's
    own `project_id` field (never the dict key name) must equal the
    canonical ADM project, the non-ADM slot's `project_id` must be present
    and different from it, and both slots' request/E2E identities must be
    present and different from each other."""
    name = "distinct_adm_nonadm_write_e2e"
    adm = evidence.get("adm_write_e2e")
    non_adm = evidence.get("non_adm_write_e2e")
    if not isinstance(adm, dict) or not isinstance(non_adm, dict):
        return _missing(name)

    expected_adm_project = evidence.get("expected_adm_project_id") or DEFAULT_ADM_PROJECT_ID
    adm_project, non_adm_project = adm.get("project_id"), non_adm.get("project_id")
    adm_request, non_adm_request = adm.get("request_id"), non_adm.get("request_id")

    if not adm_project or not non_adm_project:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: project_id not supplied for both write E2Es")
    if not adm_request or not non_adm_request:
        return CheckResult(name, STATUS_UNKNOWN, f"{name}: request_id not supplied for both write E2Es")

    if adm_project != expected_adm_project:
        return CheckResult(
            name, STATUS_FAIL,
            f"{name}: adm_write_e2e project_id {adm_project!r} is not the canonical ADM project {expected_adm_project!r}",
        )
    if non_adm_project == adm_project:
        return CheckResult(
            name, STATUS_FAIL,
            f"{name}: non_adm_write_e2e project_id {non_adm_project!r} is the same as the ADM project (not a distinct project)",
        )
    if adm_request == non_adm_request:
        return CheckResult(
            name, STATUS_FAIL,
            f"{name}: adm_write_e2e and non_adm_write_e2e share request_id {adm_request!r} (not distinct E2Es)",
        )
    return CheckResult(
        name, STATUS_PASS,
        f"{name}: ADM={adm_project!r}/{adm_request!r} distinct from non-ADM={non_adm_project!r}/{non_adm_request!r}",
    )


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
        _check_upstream_status(evidence, "canonical_baseline", "canonical_baseline_authority", now),
        _check_accepted_frozen(evidence, "direct_invoke", "direct_invoke"),
        _check_accepted_frozen(evidence, "continuation", "autonomous_continuation_foundation"),
        _check_upstream_status(evidence, "rule44", "rule44_evidence_verifier", now),
        _check_write_e2e(evidence, "adm_write_e2e", "adm_repo_write_e2e", now),
        _check_write_e2e(evidence, "non_adm_write_e2e", "non_adm_repo_write_e2e", now),
        _check_distinct_write_e2e_projects(evidence),
        _check_autonomous_continuation(evidence),
    ]
    overall = _combine(c.status for c in checks)
    return AcceptanceResult(GLOBAL_HANDS_OFF_COMPLETE, overall, tuple(checks))
