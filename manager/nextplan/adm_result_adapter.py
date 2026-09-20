"""Slice B: deterministic adapter from validated adm-result → NextPlan event.

event_id = result_id. The consumer independently re-verifies schema, digest,
task/execution identity, candidate, review bindings, tests provenance, PFP,
and terminal status. Mismatches fail closed. Prose PASS alone never authorizes
MARK_COMPLETE (see reject_prose_completion).
"""

from __future__ import annotations

import copy

from manager import adm_result as a
from manager.nextplan import atlas as atlas_mod
from manager.nextplan import planner
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v


class AdapterError(ValueError):
    def __init__(self, code, message, signals=None):
        super().__init__(message)
        self.code = code
        self.signals = list(signals or [])


def verify_adm_result(result, *, expected_task_id=None, expected_execution_id=None,
                      expected_project_id=None, expected_digest=None, terminal_statuses=None):
    """Independent consumer verification. Raises AdapterError on mismatch."""
    terminal_statuses = terminal_statuses or a.TERMINAL_STATUSES
    if result is None:
        raise AdapterError("adm_result_missing", "adm-result is missing", ["adapter.adm_result_missing"])
    try:
        validated = a.validate_adm_result(copy.deepcopy(result))
    except a.AdmResultValidationError as exc:
        raise AdapterError("adm_result_malformed", f"adm-result failed validation: {exc}",
                           ["adapter.adm_result_malformed"]) from exc
    if expected_digest is not None and validated["result_digest"] != expected_digest:
        raise AdapterError("adm_result_identity_mismatch", "result_digest mismatch",
                           ["adapter.adm_result_identity_mismatch"])
    check = copy.deepcopy(validated)
    check["result_digest"] = None
    check["result_digest"] = a.result_digest_for(check)
    if check["result_digest"] != validated["result_digest"]:
        raise AdapterError("adm_result_identity_mismatch", "result_digest does not rehash",
                           ["adapter.adm_result_identity_mismatch"])
    identity = validated["identity"]
    if expected_project_id is not None and identity["project_id"] != expected_project_id:
        raise AdapterError("adm_result_identity_mismatch", "project_id mismatch", ["adapter.adm_result_identity_mismatch"])
    if expected_task_id is not None and identity["task_id"] != expected_task_id:
        raise AdapterError("adm_result_identity_mismatch", "task_id mismatch", ["adapter.adm_result_identity_mismatch"])
    if expected_execution_id is not None and identity["execution_id"] != expected_execution_id:
        raise AdapterError("adm_result_identity_mismatch", "execution_id mismatch", ["adapter.adm_result_identity_mismatch"])
    if validated["execution"]["status"] not in terminal_statuses:
        raise AdapterError("adm_result_malformed", "execution.status is not terminal", ["adapter.adm_result_malformed"])
    return validated


def reject_prose_completion(prose_claims_pass: bool, structured_event) -> None:
    """M12: prose PASS without structured adapter event cannot complete."""
    if prose_claims_pass and structured_event is None:
        raise AdapterError("adm_result_missing", "prose PASS without structured adm-result adapter event",
                           ["adapter.prose_pass_rejected"])


def require_pfp_gate(result) -> None:
    """M9: PFP-required without evidence → pfp_deferred (caller maps to HUMAN_GATE)."""
    pfp = (result or {}).get("pfp") or {}
    if pfp.get("required") and not pfp.get("evidence"):
        raise AdapterError("pfp_deferred", "PFP required but evidence missing", ["adapter.pfp_deferred"])


def to_nextplan_event(result, *, generation: int, session_id=None, agent=None, pointer=None):
    """Map a verified adm-result to a NextPlan planner event.

    event_id = result_id. Embeds a normalized_result when present; otherwise
    builds a minimal blank result carrying identity facts only (never a prose PASS).
    """
    validated = verify_adm_result(
        result,
        expected_digest=(pointer or {}).get("result_digest"),
        expected_task_id=(pointer or {}).get("task_id"),
        expected_execution_id=(pointer or {}).get("execution_id"),
        expected_project_id=(pointer or {}).get("project_id"),
    )
    require_pfp_gate(validated)
    role = validated["identity"]["role"]
    task_id = validated["identity"]["task_id"]
    event_id = validated["result_id"]
    normalized = validated.get("normalized_result")
    if normalized is None:
        normalized = r.blank_result(event_id, task_id, role, tier="none",
                                    raw_sha256=(validated.get("agent_output") or {}).get("sha256") or ("0" * 64))
    event = {
        "event_id": event_id,
        "task_id": task_id,
        "kind": "result",
        "role": role,
        "session_id": session_id or validated["actor"].get("session_id"),
        "agent": agent or validated["actor"].get("provider"),
        "generation": generation,
        "result": normalized,
        "adm_result": {
            "result_id": validated["result_id"],
            "result_digest": validated["result_digest"],
            "execution_status": validated["execution"]["status"],
            "candidate_sha": (validated.get("candidate") or {}).get("candidate_sha"),
            "tests_status": (validated.get("tests") or {}).get("tests_status"),
            "review_authorized": ((validated.get("review") or {}).get("authority") or {}).get("authorized"),
            "pfp_required": (validated.get("pfp") or {}).get("required"),
            "pfp_evidence": bool((validated.get("pfp") or {}).get("evidence")),
            "role": role,
            "lineage": validated.get("lineage"),
        },
        "orchestration_signals": [],
    }
    return event


def plan_from_adm_result(state, result, *, generation, atlas=None, pointer=None, session_id=None, agent=None,
                         allow_missing=False, prose_pass=False):
    """Verify → adapt → plan. Missing/malformed results fail closed via atlas codes."""
    atlas = atlas or atlas_mod.load_atlas()
    try:
        if prose_pass:
            reject_prose_completion(True, None if result is None else object())
        if result is None:
            raise AdapterError("adm_result_missing", "adm-result missing", ["adapter.adm_result_missing"])
        event = to_nextplan_event(result, generation=generation, session_id=session_id, agent=agent, pointer=pointer)
    except AdapterError as exc:
        if not allow_missing:
            failure_event = {
                "event_id": f"adapter-error-{(pointer or {}).get('result_id') or 'missing'}",
                "task_id": state["task_id"],
                "kind": "signal",
                "role": state.get("phase_owner", {}).get("role") or v.WORKER,
                "generation": generation,
                "result": r.blank_result("adapter-error", state["task_id"], v.WORKER),
                "orchestration_signals": [{"code": exc.code, "signals": exc.signals}],
                "failure_code_hint": exc.code,
            }
            decision = planner.plan(state, {
                "event_id": failure_event["event_id"],
                "task_id": state["task_id"],
                "kind": "result",
                "role": v.WORKER,
                "generation": generation,
                "result": failure_event["result"],
                "orchestration_signals": [{"code": exc.code, "signals": exc.signals}],
            }, atlas=atlas)
            if decision["action"] in (v.MARK_COMPLETE,):
                decision = {
                    **decision,
                    "action": v.HUMAN_GATE,
                    "next_state": v.HUMAN_GATE,
                    "failure_code": exc.code,
                    "reason": str(exc),
                }
            return decision, failure_event
        raise
    decision = planner.plan(state, event, atlas=atlas)
    adm = event["adm_result"]
    if decision["action"] == v.MARK_COMPLETE:
        if adm.get("pfp_required") and not adm.get("pfp_evidence"):
            decision = {**decision, "action": v.HUMAN_GATE, "next_state": v.HUMAN_GATE,
                        "failure_code": "pfp_deferred", "reason": "PFP required"}
        if adm["role"] == v.WORKER and state.get("requirements", {}).get("requires_review"):
            decision = {**decision, "action": v.SEND_TO_REVIEW, "next_state": v.AWAITING_REVIEW,
                        "reason": "worker adm-result alone cannot MARK_COMPLETE when review required"}
    return decision, event
