#!/usr/bin/env python3
"""Remediation Round 1 — PRE-FIX reproduction of the independent-review blockers.

This script asserts the BROKEN behaviour on purpose. It is evidence, not a
regression suite: run it at the repair base `c116854` and every probe must
report BYPASS_REPRODUCED. After the repair every probe must report
BYPASS_CLOSED.

    python3 docs/verification-loop/v5/tests/prefix_repro_r1.py

Exit code is the number of probes still reproducing a bypass (0 after repair).
"""

from __future__ import annotations

import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from fixtures import close_ok, honest_open, launcher_capture, mechanical_pass, policy, review_ok
from v5_kernel.harness import HARNESS_REQUIRED_AGENTS, AgentResult, harness_gate
from v5_kernel.kernel import (Event, Issuer, Obligation, ObligationState, State, apply,
                              controller_trust_digest, decide, open_task)

RESULTS = []


def probe(bid, what, bypassed, detail):
    RESULTS.append((bid, what, bypassed, detail))
    tag = "BYPASS_REPRODUCED" if bypassed else "BYPASS_CLOSED"
    print("[%s] %s: %s" % (bid, tag, what))
    print("        %s" % detail)


def _status(d):
    return d.derived_status.value


# --- B1: trusted-label forgery -------------------------------------------
def b1():
    w = honest_open("LOW")
    apply(w, Event.EXECUTOR_DONE, {})
    # No attester at all: the candidate payload omits it, the kernel defaults to LAUNCHER.
    apply(w, Event.MECHANICAL_REPLAY, {"result": "PASS"})
    apply(w, Event.CLOSE_WINDOW, close_ok())
    d = decide(w)
    ob = w.obligations["mechanical.tests"]
    probe("B1", "MECHANICAL_REPLAY with no trusted envelope reaches ACCEPTED",
          d.derived_status == State.ACCEPTED,
          "attester_recorded=%r obligation=%s status=%s" % (w.mechanical_replay_attester, ob.state.value, _status(d)))

    w2 = honest_open("LOW")
    apply(w2, Event.EXECUTOR_DONE, {})
    apply(w2, Event.MECHANICAL_REPLAY, {"result": "PASS", "attester": "LAUNCHER"})
    apply(w2, Event.CLOSE_WINDOW, close_ok())
    d2 = decide(w2)
    probe("B1b", "payload self-declared attester=LAUNCHER is honoured",
          d2.derived_status == State.ACCEPTED, "status=%s" % _status(d2))


# --- B2: required obligation unavailable still ACCEPTED --------------------
def b2():
    w = open_task(policy(extra_required_obligations=["security.audit"]), launcher_capture())
    apply(w, Event.EXECUTOR_DONE, {})
    apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    w.obligations["security.audit"] = Obligation(
        obligation_id="security.audit", required=True,
        state=ObligationState.UNAVAILABLE_RECOVERABLE, source="TRANSIENT")
    apply(w, Event.CLOSE_WINDOW, close_ok())
    d = decide(w)
    probe("B2", "required obligation UNAVAILABLE_RECOVERABLE still reaches ACCEPTED",
          d.derived_status == State.ACCEPTED,
          "security.audit=%s status=%s" % (w.obligations["security.audit"].state.value, _status(d)))

    w2 = open_task(policy(oracle_expected=[]), launcher_capture(oracle_expected=[]))
    probe("B2b", "explicitly empty oracle domain is silently replaced by an invented item",
          w2.oracle_expected == frozenset({"oracle.unit"}),
          "declared=[] resolved=%s" % sorted(w2.oracle_expected))


# --- B3: partial interval satisfaction ------------------------------------
def b3():
    w = honest_open("LOW")
    apply(w, Event.EXECUTOR_DONE, {})
    apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    apply(w, Event.CLOSE_WINDOW, close_ok())
    apply(w, Event.STALE_BINDING, {})
    apply(w, Event.REDERIVE, {"close_predicates": {"mechanical.tests": "PASS", "oracle.set": "MISSING"}})
    apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    d = decide(w)
    close = ", ".join("%s=%s" % (k, v.value) for k, v in sorted(w.close_obs.items()))
    probe("B3", "partial CLOSE rederive keeps interval SATISFIED and reaches ACCEPTED",
          d.derived_status == State.ACCEPTED,
          "interval=%s close={%s} status=%s" % (w.obligations["interval.binding"].state.value, close, _status(d)))


# --- B4: empty review context claim ---------------------------------------
def b4():
    w = honest_open("MEDIUM")
    apply(w, Event.EXECUTOR_DONE, {})
    apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    # Nothing but a context digest: no invocation id, no files, no findings, no verdict.
    apply(w, Event.REVIEW_CLAIM, {"context_digest": "ctx-bound-1"})
    apply(w, Event.CLOSE_WINDOW, close_ok())
    d = decide(w)
    c = w.review_claims[-1]
    probe("B4", "review CLAIM with only a context digest satisfies a MEDIUM review obligation",
          d.derived_status == State.ACCEPTED,
          "invocation_id=%r files_used=%s review.claim=%s status=%s"
          % (c.invocation_id, c.files_used, w.obligations["review.claim"].state.value, _status(d)))

    w2 = open_task(policy(risk="MEDIUM"), launcher_capture(captured_review_context_digest=""))
    apply(w2, Event.EXECUTOR_DONE, {})
    apply(w2, Event.MECHANICAL_REPLAY, mechanical_pass())
    apply(w2, Event.REVIEW_CLAIM, {"invocation_id": "self", "context_digest": "digest-i-made-up",
                                   "files_used": ("review/policy.md",)})
    apply(w2, Event.CLOSE_WINDOW, close_ok())
    d2 = decide(w2)
    probe("B4b", "reviewer claim back-fills the launcher context binding it is checked against",
          w2.captured_review_context_digest == "digest-i-made-up" and d2.derived_status == State.ACCEPTED,
          "captured_digest=%r status=%s" % (w2.captured_review_context_digest, _status(d2)))


# --- B5: adjudication scope / resolution / expiry -------------------------
def _destructive_to_gate():
    w = honest_open("DESTRUCTIVE")
    apply(w, Event.EXECUTOR_DONE, {})
    apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    apply(w, Event.REVIEW_CLAIM, review_ok())
    apply(w, Event.CLOSE_WINDOW, close_ok())
    apply(w, Event.HUMAN_GATE, {"reason": "destructive_action_approval"})
    return w


def _finish(w):
    apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    apply(w, Event.CLOSE_WINDOW, close_ok())
    return decide(w)


def b5():
    w = _destructive_to_gate()
    apply(w, Event.ADJUDICATE, {"record": {"issuer": Issuer.HUMAN_OPERATOR.value,
                                           "subject": "some.unrelated.thing",
                                           "expires_tick": 99, "provenance_digest": "h"},
                                "resolution": "SATISFIED"})
    d = _finish(w)
    probe("B5a", "human record for an UNRELATED subject discharges the destructive approval",
          d.derived_status == State.ACCEPTED,
          "subject=some.unrelated.thing destructive=%s status=%s"
          % (w.obligations["human.destructive_approval"].state.value, _status(d)))

    w = _destructive_to_gate()
    apply(w, Event.ADJUDICATE, {"record": {"issuer": Issuer.HUMAN_OPERATOR.value,
                                           "subject": "human.destructive_approval",
                                           "scope": "totally-wrong-scope",
                                           "expires_tick": 99, "provenance_digest": "h"},
                                "resolution": "SATISFIED"})
    d = _finish(w)
    probe("B5b", "adjudication scope is never checked",
          d.derived_status == State.ACCEPTED, "scope=totally-wrong-scope status=%s" % _status(d))

    w = _destructive_to_gate()
    apply(w, Event.ADJUDICATE, {"record": {"issuer": Issuer.HUMAN_OPERATOR.value,
                                           "subject": "human.destructive_approval",
                                           "expires_tick": 99, "provenance_digest": "h"},
                                "resolution": "ADVERSE"})
    d = _finish(w)
    probe("B5c", "resolution=ADVERSE removes the blocker instead of preserving it",
          d.derived_status == State.ACCEPTED,
          "resolution=ADVERSE destructive=%s status=%s"
          % (w.obligations["human.destructive_approval"].state.value, _status(d)))

    w = _destructive_to_gate()
    at = w.tick
    apply(w, Event.ADJUDICATE, {"record": {"issuer": Issuer.HUMAN_OPERATOR.value,
                                           "subject": "human.destructive_approval",
                                           "expires_tick": at + 1, "provenance_digest": "h"},
                                "resolution": "SATISFIED"})
    for _ in range(8):
        apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    d = _finish(w)
    probe("B5d", "approval that expired before decide() is still honoured",
          d.derived_status == State.ACCEPTED and w.tick > at + 1,
          "expires_tick=%d tick_at_decide=%d status=%s" % (at + 1, w.tick, _status(d)))


# --- B6: unbounded default recovery ---------------------------------------
def b6():
    w = honest_open("LOW")
    apply(w, Event.EXECUTOR_DONE, {})
    for _ in range(20):
        apply(w, Event.VERIFIER_UNAVAILABLE, {"kind": "network_timeout", "obligation": "mechanical.tests"})
    budgets = dict((k, (v.used, v.maximum)) for k, v in w.budgets.items())
    probe("B6", "unknown recoverable failure kind loops 20x without consuming any budget",
          (not budgets) or all(u == 0 for u, _m in budgets.values()),
          "state=%s budgets=%s" % (w.state.value, budgets if budgets else "{} (none created)"))

    w2 = honest_open("LOW")
    apply(w2, Event.EXECUTOR_DONE, {})
    apply(w2, Event.MECHANICAL_REPLAY, mechanical_pass())
    apply(w2, Event.VERIFIER_UNAVAILABLE, {"kind": "network_timeout", "obligation": "mechanical.tests"})
    apply(w2, Event.MECHANICAL_REPLAY, mechanical_pass())
    apply(w2, Event.CLOSE_WINDOW, close_ok())
    d2 = decide(w2)
    probe("B6b", "unresolved default-branch recoverable failure still reaches ACCEPTED",
          d2.derived_status == State.ACCEPTED,
          "last_failure_kind=%r status=%s" % (w2.last_failure_kind, _status(d2)))

    w3 = honest_open("LOW")
    apply(w3, Event.EXECUTOR_DONE, {})
    apply(w3, Event.VERIFIER_UNAVAILABLE, {"kind": "cache_miss", "obligation": "mechanical.tests"})
    before = sorted(w3.budgets)
    apply(w3, Event.RETRY, {"identity": "an-identity-that-does-not-exist"})
    probe("B6c", "RETRY with a wrong identity borrows an unrelated budget",
          w3.state == State.VERIFYING,
          "requested=an-identity-that-does-not-exist available=%s state=%s" % (before, w3.state.value))


# --- C10: candidate-induced human escalation ------------------------------
def c10():
    w = honest_open("LOW")
    apply(w, Event.EXECUTOR_DONE, {})
    apply(w, Event.HUMAN_GATE, {"reason": "automated_recovery_budget_exhausted"})
    probe("C10", "candidate can self-declare budget exhaustion and force HUMAN_REQUIRED",
          w.state == State.HUMAN_REQUIRED and w.human_reason == "automated_recovery_budget_exhausted",
          "budgets_used=%s state=%s reason=%r"
          % (dict((k, v.used) for k, v in w.budgets.items()), w.state.value, w.human_reason))


# --- C7: oracle lineage reuse ---------------------------------------------
def c7():
    w = honest_open("LOW")
    w.oracle_lineage["oracle.lint"] = "oracle.unit"
    apply(w, Event.EXECUTOR_DONE, {})
    apply(w, Event.MECHANICAL_REPLAY, mechanical_pass())
    apply(w, Event.CLOSE_WINDOW, {"oracle_observed": ["oracle.unit"],
                                  "close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"}})
    d = decide(w)
    probe("C7", "one observed oracle id satisfies two distinct expected ids via lineage",
          d.derived_status == State.ACCEPTED,
          "expected=%s observed=%s status=%s"
          % (sorted(w.oracle_expected), sorted(w.oracle_observed), _status(d)))


# --- B7: harness false usable ---------------------------------------------
# The gate signature changed in the repair, so this block adapts: the SAME
# probes must run against the base kernel and the repaired one.
_GATE_PARAMS = set(inspect.signature(harness_gate).parameters)
_NEW_GATE = "observed_kernel_digest" in _GATE_PARAMS
AUTHORITATIVE = controller_trust_digest()


def _row(attack="F01", **over):
    """A row carrying both the old display keys and the new canonical keys, so
    it is otherwise-valid under either gate version."""
    row = {
        # old display schema
        "Attack": attack, "Original blocked?": "YES", "New variant?": "NO",
        "Class": "MC-A", "Evidence": "e",
        # canonical schema
        "attack_id": attack, "original_attack_blocked": "YES", "new_variant_found": "NO",
        "status": "PASS", "usable": True, "kernel_digest": AUTHORITATIVE,
        "klass": "MC-A", "evidence": "e",
    }
    row.update(over)
    return row


def _res(effective=None, **over):
    base = [
        AgentResult("liveness_runner", True, "PASS", prose="L", effective={"L": "PASS"}),
        AgentResult("safety_runner", True, "PASS", prose="m", original_blocked="YES",
                    new_variant_found="NO", effective=[_row()] if effective is None else effective),
        AgentResult("harness_mutator", True, "PASS", prose="H", effective={"H": "K"}),
        AgentResult("aggregator", True, "PASS", prose="a", effective={"r": 1}),
    ]
    idx = {"liveness": 0, "safety": 1, "mutator": 2, "agg": 3}
    for key, changes in over.items():
        for attr, val in changes.items():
            setattr(base[idx[key]], attr, val)
    return base


def _gate(results, digest, expected=("F01",)):
    if _NEW_GATE:
        return harness_gate(HARNESS_REQUIRED_AGENTS, results,
                            observed_kernel_digest=digest,
                            expected_safety_attack_ids=expected)
    return harness_gate(HARNESS_REQUIRED_AGENTS, results, kernel_digest=digest)


def b7():
    g = _gate(_res(), "deadbeef")
    probe("B7a", "fabricated kernel digest 'deadbeef' passes the harness gate",
          g.usable, "HARNESS_USABLE=%s reasons=%s" % (g.HARNESS_USABLE, g.reasons))

    no_variant = _row()
    no_variant.pop("New variant?")
    no_variant.pop("new_variant_found")
    g = _gate(_res(effective=[no_variant]), AUTHORITATIVE)
    probe("B7b", "safety effective row missing 'New variant?' passes the gate",
          g.usable, "row has no new-variant field; HARNESS_USABLE=%s" % g.HARNESS_USABLE)

    flip = _row(**{"Original blocked?": "NO", "New variant?": "YES",
                   "original_attack_blocked": "NO", "new_variant_found": "YES"})
    g = _gate(_res(effective=[_row(), flip]), AUTHORITATIVE)
    probe("B7c", "replicated F01 rows that disagree (PASS/FAIL flip) pass the gate",
          g.usable, "two F01 rows disagree on both fields; HARNESS_USABLE=%s" % g.HARNESS_USABLE)

    g = _gate(_res(liveness={"usable": False, "verdict": "NOT_RUN"}), AUTHORITATIVE)
    probe("B7d", "required non-safety agent NOT_RUN / unusable passes the gate",
          g.usable, "liveness_runner usable=False verdict=NOT_RUN HARNESS_USABLE=%s" % g.HARNESS_USABLE)

    g = _gate(_res(effective=[]), AUTHORITATIVE, expected=())
    probe("B7e", "empty safety collection (0 attacks) passes the gate",
          g.usable, "0 safety rows, 0 expected; HARNESS_USABLE=%s" % g.HARNESS_USABLE)


def main():
    for fn in (b1, b2, b3, b4, b5, b6, c10, c7, b7):
        fn()
    n = sum(1 for _i, _w, byp, _d in RESULTS if byp)
    print("")
    print("=" * 72)
    print("PRE_FIX_BYPASSES_REPRODUCED = %d/%d" % (n, len(RESULTS)))
    for bid, what, byp, _d in RESULTS:
        print("  %s  %s: %s" % ("REPRODUCED" if byp else "CLOSED    ", bid, what))
    return n


if __name__ == "__main__":
    sys.exit(main())
