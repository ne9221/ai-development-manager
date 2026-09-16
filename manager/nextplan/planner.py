"""NextPlan: one deterministic decision from (task state, event).

``plan(state, event)`` is a pure function: no clock, no I/O, no randomness, and
it never writes anything. Its output is one action from the closed set in
manager.nextplan.vocabulary plus the reasons, failures, budget and proof that
justify it. ``apply(state, event, decision)`` is the equally pure state
transition, so sequences can be replayed and tested without a store.

Guard order (first match wins):

  1. terminal task            -> NO_OP   (task_already_completed; terminal is immutable)
  2. event already processed  -> NO_OP   (duplicate_result; idempotent)
  3. event from an older gen  -> NO_OP   (stale_task_state; never overwrites newer)
     event from a newer gen   -> WAIT_DEPENDENCY (stale_ssot; our view is behind)
  4. task held for a human    -> NO_OP   (only a human resolution moves it)
  5. tick                     -> dependencies / resume the phase
  6. wrong owner              -> conflicting_owner
  7. failures (Failure Atlas) -> governing failure's policy, within budget
  8. success                  -> SEND_TO_REVIEW / MARK_COMPLETE, only on a
                                 completion proof in which every item holds
  9. anything else            -> HUMAN_GATE (planner_no_valid_action)

Termination: every round-consuming decision is paid from a finite budget
(per failure code, repair, review, continuation) and from a global cap of
MAX_TOTAL_ROUNDS, after which only hold actions remain.
"""

from __future__ import annotations

import copy
import hashlib
import json

from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import default_atlas
from manager.nextplan.classify import (
    classify, completion_proof, requirements_with_defaults, review_proof, signal_index,
)

PLANNER_VERSION = "1.0.0"
MAX_TOTAL_ROUNDS = 20
MAX_CONTINUATIONS = 5
_CLAIMED = (v.REPORTED, v.VERIFIED)


class PlannerInputError(ValueError):
    """The caller handed the planner something that is not a planner input."""


def new_task_state(task_id, requirements=None, dependencies=(), reroute_candidates=(), agent=None):
    return {
        "task_id": task_id, "state": v.NEW, "generation": 0, "processed_event_ids": [],
        "phase_owner": {"role": v.WORKER, "session_id": None},
        "worker_session": None, "worker_sessions": [], "reviewer_sessions": [],
        "candidate": None, "requirements": requirements_with_defaults(requirements),
        "retry_history": [], "dependencies": [dict(d) for d in dependencies],
        "reroute_candidates": list(reroute_candidates), "agent": agent, "last_decision": None,
    }


def _digest(state, event, atlas):
    payload = {"planner": PLANNER_VERSION, "atlas": atlas.digest, "state": state, "event": event}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _budget_key(entry, code):
    budget = entry["max_retry_policy"]["budget"]
    return f"code:{code}" if budget == "per_code" else budget


def _used(state, key):
    return sum(1 for item in state["retry_history"] if item["key"] == key)


class _Decider:
    def __init__(self, state, event, atlas):
        self.state, self.event, self.atlas = state, event, atlas
        self.digest = _digest(state, event, atlas)

    def __call__(self, action, reason, code=None, failures=(), signals=(), budget=None, constraints=None,
                 proof=None, candidate=None):
        next_state = v.ACTION_NEXT_STATE[action]
        if next_state == v.UNCHANGED:
            next_state = self.state["state"]
        return {
            "planner_version": PLANNER_VERSION, "atlas_version": self.atlas.version,
            "input_digest": self.digest, "task_id": self.state["task_id"],
            "event_id": self.event.get("event_id"), "action": action, "next_state": next_state, "reason": reason,
            "failure_code": code, "failures": list(failures), "signals": sorted(set(signals)),
            "budget": budget, "constraints": constraints or {}, "proof": proof, "candidate": candidate,
        }


def _worker_target(state, event):
    """The worker session a repair goes back to: the recorded one, or the one reporting now."""
    if state["worker_session"]:
        return state["worker_session"]
    return event.get("session_id") if event.get("role") == v.WORKER else None


def _review_context(state, event):
    return {
        "reviewer_session": event.get("session_id"),
        "worker_sessions": list(state["worker_sessions"]) + ([state["worker_session"]] if state["worker_session"] else []),
        "prior_reviewer_sessions": list(state["reviewer_sessions"]),
        "candidate_sha": (state["candidate"] or {}).get("head_sha"),
    }


def _fresh_review_constraints(state, event):
    exclude = set(state["reviewer_sessions"]) | set(state["worker_sessions"])
    for session in (state["worker_session"], event.get("session_id")):
        if session:
            exclude.add(session)
    return {"fresh_reviewer": True, "exclude_sessions": sorted(exclude)}


def _failure_decision(decide, state, event, findings, atlas):
    codes = [finding["code"] for finding in findings]
    signals = [signal for finding in findings for signal in finding["signals"]]
    code = codes[0]
    entry = atlas.entry(code)
    role = event.get("role") or state["phase_owner"]["role"]
    if entry["human_gate_required"]:
        return decide(v.HUMAN_GATE, f"{code} requires a human decision", code, codes, signals)
    action = atlas.action_for(code, role)
    if action not in v.ROUND_CONSUMING_ACTIONS:
        return decide(action, f"{code}: {entry['safe_default']} by policy", code, codes, signals)

    key = _budget_key(entry, code)
    used, limit = _used(state, key), entry["max_retry_policy"]["max_attempts"]
    total = len(state["retry_history"])
    if used >= limit or total >= MAX_TOTAL_ROUNDS:
        terminal = entry["terminal_if_unresolved"]
        why = "global round cap reached" if total >= MAX_TOTAL_ROUNDS else f"budget {key} spent ({used}/{limit})"
        return decide(terminal, f"{code}: {why}", code, codes + ["retry_exhausted"], signals + ["planner.budget_exhausted"],
                      budget={"key": key, "used": used, "max": limit, "exhausted": True})

    constraints = {}
    if action == v.REROUTE_AGENT:
        current = event.get("agent") or state["agent"]
        alternates = sorted(c for c in state["reroute_candidates"] if c != current)
        if alternates:
            constraints = {"agent": alternates[0], "exclude_agents": sorted({current} - {None})}
        else:
            fallback = entry["fallback_action"] or entry["terminal_if_unresolved"]
            if fallback not in v.ROUND_CONSUMING_ACTIONS:
                return decide(fallback, f"{code}: no eligible agent to reroute to", code, codes, signals)
            action = fallback
    if action == v.RETURN_TO_WORKER:
        # The worker that just reported is the original worker when the task
        # state has not recorded one yet (its first event is being decided).
        target = _worker_target(state, event)
        if not target:
            fallback = entry["fallback_action"] or entry["terminal_if_unresolved"]
            return decide(fallback, f"{code}: the original worker session is unknown", code, codes, signals)
        constraints = {"target_session": target}
    elif action == v.SEND_TO_REVIEW:
        constraints = _fresh_review_constraints(state, event)
    elif action == v.CONTINUE_WORKER:
        constraints = {"target_session": _worker_target(state, event)}
    if action in (v.RETRY_SAME_AGENT, v.WAIT_DEPENDENCY, v.REROUTE_AGENT) or entry["max_retry_policy"]["backoff_seconds"]:
        constraints["not_before_seconds"] = entry["max_retry_policy"]["backoff_seconds"]
    return decide(action, f"{code}: {action} (attempt {used + 1}/{limit})", code, codes, signals,
                  budget={"key": key, "used": used, "max": limit, "exhausted": False}, constraints=constraints)


def _orchestration_findings(event, atlas):
    index = signal_index(atlas)
    grouped = {}
    for signal in event.get("orchestration_signals") or ():
        grouped.setdefault(index.get(signal, "planner_no_valid_action"), set()).add(signal)
    return grouped


def plan(state, event, atlas=None):
    """Decide the next action. Pure; see the module docstring for the guard order."""
    atlas = atlas or default_atlas()
    if not isinstance(event, dict) or event.get("task_id") != state["task_id"]:
        raise PlannerInputError("event does not belong to this task")
    decide = _Decider(state, event, atlas)
    no_valid = lambda reason: decide(v.HUMAN_GATE, reason, "planner_no_valid_action",  # noqa: E731
                                     ["planner_no_valid_action"], ["planner.no_rule_matched"])

    if state["state"] in v.TERMINAL_STATES:
        return decide(v.NO_OP, "task is terminal; transitions are rejected", "task_already_completed",
                      ["task_already_completed"], ["orchestration.task_terminal"])
    if event.get("event_id") in state["processed_event_ids"]:
        return decide(v.NO_OP, "event already processed", "duplicate_result", ["duplicate_result"],
                      ["orchestration.event_already_processed"])
    generation = event.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or not event.get("event_id"):
        return no_valid("event lacks an integer generation or an event_id")
    if generation < state["generation"]:
        return decide(v.NO_OP, f"event generation {generation} is older than task generation {state['generation']}",
                      "stale_task_state", ["stale_task_state"], ["orchestration.event_generation_behind"])
    if generation > state["generation"]:
        findings = [{"code": "stale_ssot", "signals": ["verify.ssot.revision_behind"]}]
        return _failure_decision(decide, state, event, findings, atlas)
    if state["state"] in v.HELD_STATES:
        return decide(v.NO_OP, "task is held; only a human resolution moves it")

    unsatisfied = sorted(d["id"] for d in state["dependencies"] if not d.get("satisfied"))
    kind = event.get("kind", "result")
    if kind == "tick":
        if unsatisfied:
            findings = [{"code": "blocked_dependency", "signals": ["orchestration.dependency_not_satisfied"]}]
            return _failure_decision(decide, state, event, findings, atlas)
        if state["state"] in (v.NEW, v.WAITING):
            if state["phase_owner"] and state["phase_owner"]["role"] == v.REVIEWER:
                return decide(v.SEND_TO_REVIEW, "resume the review phase",
                              constraints=_fresh_review_constraints(state, event))
            return decide(v.CONTINUE_WORKER, "dispatch or resume the worker",
                          constraints={"target_session": state["worker_session"]})
        return decide(v.NO_OP, "tick: nothing is waiting")
    if kind != "result" or not isinstance(event.get("result"), dict):
        return no_valid(f"unknown event kind {kind!r} or missing result")

    orchestration = _orchestration_findings(event, atlas)
    owner = state["phase_owner"]
    role = event.get("role")
    if owner is None or role != owner["role"] or (owner["session_id"] and event.get("session_id") != owner["session_id"]):
        orchestration.setdefault("conflicting_owner", set()).add("orchestration.owner_mismatch")
    if unsatisfied:
        orchestration.setdefault("blocked_dependency", set()).add("orchestration.dependency_not_satisfied")

    result = event["result"]
    if result.get("task_id") != state["task_id"] or result.get("role") != role:
        return no_valid("result envelope does not match the event")
    try:
        r.validate_result(result)
    except r.ResultError:
        return no_valid("result does not satisfy the normalized result contract")

    review_context = _review_context(state, event)
    findings = classify(result, event.get("verification"), state["requirements"], event.get("execution"),
                        review_context, atlas)
    merged = {f["code"]: set(f["signals"]) for f in findings}
    for code, found in orchestration.items():
        merged.setdefault(code, set()).update(found)
    if merged:
        ordered = [{"code": c, "signals": sorted(merged[c])} for c in sorted(merged, key=atlas.rank)]
        return _failure_decision(decide, state, event, ordered, atlas)

    if role == v.WORKER:
        status, level = r.value(result, "status"), r.level(result, "status")
        if status in ("PARTIAL", "IN_PROGRESS") and level in _CLAIMED:
            used, total = _used(state, "continuation"), len(state["retry_history"])
            if used >= MAX_CONTINUATIONS or total >= MAX_TOTAL_ROUNDS:
                return decide(v.HUMAN_GATE, "partial progress keeps repeating", "retry_exhausted",
                              ["retry_exhausted"], ["planner.budget_exhausted"],
                              budget={"key": "continuation", "used": used, "max": MAX_CONTINUATIONS, "exhausted": True})
            return decide(v.CONTINUE_WORKER, "partial progress: continue the same worker",
                          budget={"key": "continuation", "used": used, "max": MAX_CONTINUATIONS, "exhausted": False},
                          constraints={"target_session": state["worker_session"] or event.get("session_id")})
        proof = completion_proof(result, state["requirements"])
        if status == "PASS" and level in _CLAIMED and all(item["ok"] for item in proof):
            candidate = {"head_sha": r.value(result, "head_sha"), "proof": proof,
                         "worker_session": event.get("session_id")}
            if state["requirements"]["requires_review"]:
                return decide(v.SEND_TO_REVIEW, "worker result verified; independent review required",
                              constraints=_fresh_review_constraints(state, event), proof=proof, candidate=candidate)
            return decide(v.MARK_COMPLETE, "worker result verified; review not required", proof=proof,
                          candidate=candidate)
        return no_valid("worker result neither failed nor proved completion")

    candidate = state["candidate"]
    rproof = review_proof(result, review_context)
    if candidate and all(item["ok"] for item in candidate["proof"]) and all(item["ok"] for item in rproof):
        return decide(v.MARK_COMPLETE, "verified candidate approved by a fresh reviewer",
                      proof={"candidate": candidate["proof"], "review": rproof})
    return no_valid("review result neither failed nor approved a verified candidate")


def apply(state, event, decision):
    """The pure state transition for ``decision``. Never mutates its inputs."""
    new = copy.deepcopy(state)
    action = decision["action"]
    event_id = event.get("event_id")
    if action == v.NO_OP:
        if state["state"] not in v.TERMINAL_STATES and event_id and event_id not in state["processed_event_ids"]:
            new["processed_event_ids"].append(event_id)
        return new

    new["generation"] += 1
    if event_id:
        new["processed_event_ids"].append(event_id)
    new["state"] = decision["next_state"]
    new["last_decision"] = {"action": action, "failure_code": decision["failure_code"],
                            "input_digest": decision["input_digest"]}
    if decision["budget"] and not decision["budget"].get("exhausted"):
        new["retry_history"].append({"key": decision["budget"]["key"], "code": decision["failure_code"],
                                     "action": action})

    role, session = event.get("role"), event.get("session_id")
    if event.get("kind", "result") == "result" and session:
        if role == v.WORKER:
            if new["worker_session"] is None:
                new["worker_session"] = session
            if session not in new["worker_sessions"]:
                new["worker_sessions"].append(session)
        elif role == v.REVIEWER and session not in new["reviewer_sessions"]:
            new["reviewer_sessions"].append(session)

    current_role = (state["phase_owner"] or {}).get("role", v.WORKER)
    if action in (v.CONTINUE_WORKER, v.RETURN_TO_WORKER):
        new["phase_owner"] = {"role": v.WORKER, "session_id": new["worker_session"]}
    elif action == v.SEND_TO_REVIEW:
        new["phase_owner"] = {"role": v.REVIEWER, "session_id": None}
    elif action in (v.RETRY_SAME_AGENT, v.REROUTE_AGENT):
        new["phase_owner"] = {"role": current_role, "session_id": None}
        if current_role == v.WORKER:
            new["worker_session"] = None
        if action == v.REROUTE_AGENT and decision["constraints"].get("agent"):
            new["agent"] = decision["constraints"]["agent"]
    elif action in (v.HUMAN_GATE, v.MARK_BLOCKED, v.MARK_FAILED, v.MARK_COMPLETE):
        new["phase_owner"] = None
    if decision.get("candidate"):
        new["candidate"] = copy.deepcopy(decision["candidate"])
    return new
