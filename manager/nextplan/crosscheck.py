"""Failure Atlas x NextPlan cross-check.

A validator, not a document: it drives the real planner for every failure code
in the atlas, in every role, fresh and budget-exhausted, and reports any of the
situations the Execution Plan forbids:

- a failure with no legal next action, or one the planner refuses to decide;
- a silent drop: a signal any stage can emit that no atlas entry claims;
- undefined terminal behaviour once a budget is spent;
- retry forever: a failure that never reaches a hold;
- UNKNOWN or an error turning into MARK_COMPLETE;
- a human-gated failure reachable through an automated action, including when
  it appears together with a more severe automated failure.

``coverage_report`` additionally says, per failure code, whether anything can
actually produce it today: internally (extract/verify/classify/planner emit the
signal), externally (the orchestration layer passes it in), or not at all.
Codes in NOT_PRODUCED are honest gaps -- defined policy, no detector yet.
"""

from __future__ import annotations

import re
from pathlib import Path

from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import default_atlas

STAGE_MODULES = ("extract.py", "verify.py", "classify.py", "planner.py")
EXTERNAL_PREFIX = "orchestration."
# Signals live in named namespaces, so an ordinary dotted string in the source
# (a git config key, a module path) is not mistaken for one.
SIGNAL_NAMESPACES = ("extract", "result", "verify", "execution", "review", "orchestration", "planner")
SIGNAL_LITERAL = re.compile(r"[\"']((?:%s)(?:\.[a-z0-9_]+)+)[\"']" % "|".join(SIGNAL_NAMESPACES))

# Failure classes whose policy is defined but that no stage can raise yet.
# Adding to this set is a decision; the cross-check test pins it so it cannot
# grow by accident. These are the honest gaps of this round:
#   regression_unknown   needs a base-SHA attestation to compare against
#   local_only_artifact  needs an artifact inventory beyond the git worktree
#   provenance_unknown   needs the ADM provenance records wired in
#   unsafe_retry         needs side-effect tracking the orchestration layer owns
NOT_PRODUCED = frozenset({"regression_unknown", "local_only_artifact", "provenance_unknown", "unsafe_retry"})

# Signals an atlas entry declares that no stage emits yet. They are not dead
# weight: they document how the class *will* be detected, and pinning the list
# keeps that promise visible instead of letting it rot.
UNDETECTED_SIGNALS = frozenset({
    "planner.retry_not_idempotent", "result.branch_mismatch", "result.detached_head", "result.merge_conflict",
    "result.repo_mismatch", "result.worktree_mismatch", "verify.artifact.not_on_remote",
    "verify.provenance.unlinked", "verify.ssot.github_drive_disagree", "verify.tests.failed_base_unknown",
    "verify.tests.no_evidence_required", "verify.tests.reported_not_in_evidence", "verify.tests.subset_of_required",
})


def emitted_signals(package_dir=None):
    """Every signal-shaped literal the stage modules mention."""
    package_dir = Path(package_dir or Path(__file__).parent)
    found = set()
    for name in STAGE_MODULES:
        text = (package_dir / name).read_text(encoding="utf-8")
        found |= {match for match in SIGNAL_LITERAL.findall(text)}
    return found


def _known_signals(atlas):
    return {signal for code in atlas.codes for signal in atlas.entry(code)["detection_signal"]}


def _decide(state, findings, atlas, role):
    from manager.nextplan.harness import event as build_event
    from manager.nextplan.harness import worker_result
    from manager.nextplan.planner import _failure_decision, _Decider

    event = build_event(worker_result(), role=role, generation=state["generation"])
    return _failure_decision(_Decider(state, event, atlas), state, event, findings, atlas)


def _state(retry_history=(), **changes):
    from manager.nextplan.planner import new_task_state

    state = new_task_state("t-cross", reroute_candidates=["claude", "codex"], agent="claude")
    state.update({"state": v.WORKING, "worker_session": "s-w", "worker_sessions": ["s-w"],
                  "retry_history": list(retry_history)})
    state.update(changes)
    return state


def crosscheck_problems(atlas=None):
    atlas = atlas or default_atlas()
    problems = []
    known = _known_signals(atlas)
    for signal in sorted(emitted_signals() - known):
        if not signal.startswith(EXTERNAL_PREFIX):
            problems.append(f"signal {signal!r} is emitted by a stage but no failure code claims it (silent drop)")

    for code in atlas.codes:
        entry = atlas.entry(code)
        findings = [{"code": code, "signals": sorted(entry["detection_signal"])}]
        for role in v.ROLES:
            fresh = _decide(_state(), findings, atlas, role)
            spent_key = f"code:{code}" if entry["max_retry_policy"]["budget"] == "per_code" else entry["max_retry_policy"]["budget"]
            spent = _decide(_state([{"key": spent_key, "code": code, "action": v.CONTINUE_WORKER}] * 12),
                            findings, atlas, role)
            for label, decision in (("fresh", fresh), ("budget spent", spent)):
                where = f"{code} ({role}, {label})"
                if decision["action"] not in v.ACTIONS:
                    problems.append(f"{where}: action {decision['action']!r} is not in the closed set")
                if decision["action"] == v.MARK_COMPLETE:
                    problems.append(f"{where}: a failure reached MARK_COMPLETE")
                if decision["next_state"] not in v.STATES:
                    problems.append(f"{where}: next_state {decision['next_state']!r} is undefined")
                if entry["human_gate_required"] and decision["action"] != v.HUMAN_GATE:
                    problems.append(f"{where}: human-gated failure decided {decision['action']}")
            if spent["action"] in v.ROUND_CONSUMING_ACTIONS:
                problems.append(f"{code} ({role}): still starts another round with the budget spent (retry forever)")
            if spent["action"] not in v.HOLD_ACTIONS and spent["action"] != v.NO_OP:
                problems.append(f"{code} ({role}): budget spent but the outcome is neither a hold nor a no-op")

    gated = [code for code in atlas.codes if atlas.entry(code)["human_gate_required"]]
    for gate in gated:
        for other in atlas.codes:
            if other == gate:
                continue
            findings = [{"code": c, "signals": sorted(atlas.entry(c)["detection_signal"])}
                        for c in sorted({gate, other}, key=atlas.rank)]
            decision = _decide(_state(), findings, atlas, v.WORKER)
            if decision["action"] != v.HUMAN_GATE:
                problems.append(f"human gate {gate} is bypassed when it occurs with {other}: {decision['action']}")
    return problems


def coverage_report(atlas=None):
    atlas = atlas or default_atlas()
    emitted = emitted_signals()
    report = {}
    for code in atlas.codes:
        signals = atlas.entry(code)["detection_signal"]
        internal = sorted(s for s in signals if s in emitted)
        external = sorted(s for s in signals if s.startswith(EXTERNAL_PREFIX) and s not in emitted)
        report[code] = {
            "internal_producers": internal,
            "external_producers": external,
            "produced": bool(internal or external),
        }
    return report
