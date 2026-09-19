"""Verified result -> Failure Atlas codes, and the proofs completion needs.

Classification only reads evidence levels; it never re-reads prose and never
guesses. Every signal it emits is a ``detection_signal`` of exactly one atlas
entry (the atlas validator enforces uniqueness), so a signal can only ever
mean one failure. A signal no entry claims classifies as
``planner_no_valid_action`` -- a gap is a human decision, not a silent drop.

``completion_proof`` is the single definition of "done" the planner uses:
every item must be VERIFIED by a probe, except the agent's own PASS claim,
which must at least be explicitly REPORTED (a heuristic "looks done" is not a
claim anyone made).
"""

from __future__ import annotations

from manager.nextplan import contracts
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import default_atlas
from manager.nextplan.verify import equivalent

DEFAULT_REQUIREMENTS = {
    "requires_tests": True,
    "requires_commit": True,
    "requires_push": True,
    "requires_review": True,
    "requires_drive_sync": False,
    "min_tests": 0,
}

EXECUTION_SIGNALS = {
    "timed_out": "execution.timed_out",
    "crashed": "execution.crashed",
    "lost": "execution.lost_no_terminal",
    "resume_rejected": "execution.resume_rejected",
    "quota_exhausted": "execution.quota_exhausted",
    "rate_limited": "execution.rate_limited",
}
EXECUTION_KINDS = frozenset({"completed", "failed", *EXECUTION_SIGNALS})

_CLAIMED = (v.REPORTED, v.VERIFIED)
_KNOWN = (v.REPORTED, v.VERIFIED, v.DERIVED)


def requirements_with_defaults(requirements):
    merged = dict(DEFAULT_REQUIREMENTS)
    merged.update(requirements or {})
    return merged


def _known(result, field):
    return r.level(result, field) in _KNOWN


def _item(requirement, field, result, ok, expected):
    fact = r.get(result, field)
    return {"requirement": requirement, "field": field, "expected": expected, "value": fact["value"],
            "level": fact["level"], "ok": bool(ok)}


def completion_proof(result, requirements):
    """Items a worker result must satisfy before it may leave the worker phase."""
    req = requirements_with_defaults(requirements)
    level = lambda f: r.level(result, f)  # noqa: E731
    value = lambda f: r.value(result, f)  # noqa: E731
    items = [
        _item("agent explicitly reported PASS", "status", result,
              value("status") == "PASS" and level("status") in _CLAIMED, "PASS (REPORTED or VERIFIED)"),
        _item("candidate HEAD observed", "head_sha", result, level("head_sha") == v.VERIFIED, "VERIFIED"),
        _item("worktree clean", "git_status", result,
              level("git_status") == v.VERIFIED and value("git_status") == "clean", "clean (VERIFIED)"),
    ]
    if req["requires_commit"]:
        items.append(_item("final commit is HEAD", "commit_sha", result, level("commit_sha") == v.VERIFIED, "VERIFIED"))
    if req["requires_push"]:
        items.append(_item("push landed", "push_status", result,
                           level("push_status") == v.VERIFIED and value("push_status") == "pushed", "pushed (VERIFIED)"))
        items.append(_item("remote equals HEAD", "remote_sha", result,
                           level("remote_sha") == v.VERIFIED and level("head_sha") == v.VERIFIED
                           and equivalent("remote_sha", value("remote_sha"), value("head_sha")), "== head_sha (VERIFIED)"))
    if req["requires_tests"]:
        items.append(_item("required tests passed", "tests_failed", result,
                           level("tests_failed") == v.VERIFIED and value("tests_failed") == 0, "0 (VERIFIED)"))
        # This item is unconditional on purpose. It used to be appended only
        # when tests_run was already VERIFIED, so when nothing could say how
        # many tests ran the requirement vanished from the proof instead of
        # failing it -- and a validation command that ran zero tests (`true`,
        # exit 0) completed the task. An absent proof item is not a satisfied
        # one.
        items.append(_item("tests actually ran", "tests_run", result,
                           level("tests_run") == v.VERIFIED and (value("tests_run") or 0) > 0, "> 0 (VERIFIED)"))
    if req["requires_drive_sync"]:
        items.append(_item("Drive evidence read back", "ssot_sync_drive", result,
                           level("ssot_sync_drive") == v.VERIFIED and value("ssot_sync_drive") == "synced",
                           "synced (VERIFIED)"))
    return items


def review_expectation(review_context):
    """What ADM itself knows a valid reviewer decision must match.

    Every field comes from ADM's own records -- the candidate it is holding and
    the reviewer run it dispatched -- and none of it from the agent's output.
    That is what a forged block cannot satisfy and a sentence cannot even
    address.
    """
    context = review_context or {}
    dispatch = context.get("review_dispatch") or {}
    return {"target_sha": context.get("candidate_sha"),
            "reviewer_run_id": dispatch.get("reviewer_run_id"),
            "provider": dispatch.get("provider"),
            "job_id": dispatch.get("job_id")}


def review_authority_for(result, review_context):
    """The one place this module asks whether a reviewer output authorizes.

    Round 6 added the reviewer's own decision statements as a second argument to
    ``contracts.review_authority`` and passed them from ``review_proof`` only.
    ``signals_for`` went on calling the same contract with the same decisions and
    a *different* question, so the proof could refuse a contradicted PASS while
    the signals still read it as authorized. Nothing downstream reconciled the
    two: they happened to agree only where some other rule already routed the
    task away from completion.

    A contract that is consulted twice must be consulted the same way both
    times, so it is consulted here and nowhere else in this module.
    """
    context = review_context or {}
    return contracts.review_authority(result.get("decisions") or (), review_expectation(context),
                                      result.get("decision_statements") or ())


def review_proof(result, review_context):
    """Items a reviewer result must satisfy before it may approve the candidate.

    The first item replaced two prose-derived ones in Round 5: a ``review_verdict``
    fact read out of the message, and a ``reviewed_sha`` compared against the
    candidate. Both were extracted from natural language, and both were shown to
    be forgeable -- a heading, a quoted history line or a paragraph break was
    enough to make a decision line look authoritative, while eight of nine
    genuine phrasings were refused. A structured decision settles target and
    authority together, so neither question is answered by reading prose any
    more.

    The remaining items are unchanged: they are facts about ADM's own dispatch
    (who this session is, what it has already done), never claims in the output.
    """
    context = review_context or {}
    session = context.get("reviewer_session")
    authority = review_authority_for(result, context)
    return [
        {"requirement": "a bound reviewer decision authorizes this candidate",
         "ok": authority["authorized"], "reason": authority["reason"], "problems": authority["problems"]},
        {"requirement": "reviewer is not the implementer",
         "ok": bool(session) and session not in set(context.get("worker_sessions") or ())},
        {"requirement": "reviewer session is fresh",
         "ok": bool(session) and session not in set(context.get("prior_reviewer_sessions") or ())},
        {"requirement": "review cites evidence", "ok": bool(result["evidence"])},
    ]


def signals_for(result, verification_report, requirements, execution=None, review_context=None):
    req = requirements_with_defaults(requirements)
    role = result["role"]
    signals = set(result["extraction"]["signals"])
    if verification_report:
        signals |= set(verification_report["signals"])

    kind = (execution or {}).get("kind", "completed")
    if kind not in EXECUTION_KINDS:
        signals.add("planner.no_rule_matched")
    elif kind == "failed" or (kind == "crashed" and role == v.REVIEWER):
        if role == v.REVIEWER:
            signals.add("execution.reviewer_crashed")
        elif result["extraction"]["tier"] == "none":
            signals.add("execution.nonzero_exit_without_result")
        else:
            signals.add("execution.crashed")
    elif kind != "completed":
        signals.add(EXECUTION_SIGNALS[kind])

    status, status_level = r.value(result, "status"), r.level(result, "status")
    if status == "BLOCKED" and status_level in _KNOWN:
        signals.add("result.status_blocked")
    if status == "ERROR" and status_level in _CLAIMED:
        signals.add("result.status_error")
    if r.value(result, "push_status") == "failed" and r.level(result, "push_status") in _CLAIMED:
        signals.add("result.push_failed")
    if (req["requires_drive_sync"] and r.value(result, "ssot_sync_drive") == "failed"
            and r.level(result, "ssot_sync_drive") in _CLAIMED):
        signals.add("result.sync_failed")
    if r.value(result, "git_status") == "dirty" and r.level(result, "git_status") == v.REPORTED:
        signals.add("result.git_status_dirty")

    failed = r.value(result, "tests_failed") if _known(result, "tests_failed") else None
    if failed:
        signals.add("result.tests_failed_nonzero")
        if status == "PASS" and status_level in _CLAIMED:
            signals.add("result.status_contradicts_counts")

    if role == v.WORKER:
        ran = r.value(result, "tests_run") if _known(result, "tests_run") else None
        if req["requires_tests"] and ran == 0:
            signals.add("result.tests_run_zero")
        if req["requires_tests"] and req["min_tests"] and ran and ran < req["min_tests"]:
            signals.add("result.tests_partial")
        if status == "FAIL" and status_level in _CLAIMED:
            signals.add("result.status_fail")
        if status_level == v.UNKNOWN and result["extraction"]["tier"] != "none":
            signals.add("result.required_field_missing")
        if status == "PASS" and status_level in _KNOWN:
            if not all(item["ok"] for item in completion_proof(result, req)):
                signals.add("verify.claim_unverifiable")
    else:
        context = review_context or {}
        session = context.get("reviewer_session")
        if session and session in set(context.get("worker_sessions") or ()):
            signals.add("review.reviewer_is_implementer")
        if session and session in set(context.get("prior_reviewer_sessions") or ()):
            signals.add("review.session_reused")
        # Authority is read from the structured decision, never from the prose.
        # The prose verdict survives only as annotation. Withdrawal widens,
        # authorization narrows -- unchanged from Round 3, but now the narrow
        # side is a contract rather than a vocabulary.
        #
        # Round 7: through review_authority_for, so this path and review_proof
        # put the reviewer's own decision statements to the contract identically.
        # Asking the same question two ways was R6-IR-4.
        authority = review_authority_for(result, context)
        reason = authority["reason"]
        if reason == contracts.REJECTED:
            signals.add("result.review_verdict_fail")
        elif reason in (contracts.CONFLICT, contracts.INVALID):
            # An unreadable or self-contradictory decision is not a rejection
            # and emphatically not silence; it is an unresolved statement, and
            # the task waits on a person rather than on a wording.
            signals.add("extract.conflicting_statements")
        elif reason == contracts.ABSENT and (result["extraction"]["tier"] != "none" or result.get("decisions")):
            signals.add("result.required_field_missing")
        if authority["authorized"] and not result["evidence"]:
            signals.add("review.pass_without_evidence")
        candidate = context.get("candidate_sha")
        if _known(result, "reviewed_sha") and candidate and not equivalent(
                "reviewed_sha", r.value(result, "reviewed_sha"), candidate):
            signals.add("review.reviewed_sha_not_candidate")
    return signals


def signal_index(atlas):
    return {signal: code for code in atlas.codes for signal in atlas.entry(code)["detection_signal"]}


def classify(result, verification_report, requirements, execution=None, review_context=None, atlas=None):
    """``[{code, signals}]`` ordered most-severe first (deterministic)."""
    atlas = atlas or default_atlas()
    index = signal_index(atlas)
    grouped = {}
    for signal in signals_for(result, verification_report, requirements, execution, review_context):
        code = index.get(signal, "planner_no_valid_action")
        grouped.setdefault(code, set()).add(signal)
    return [{"code": code, "signals": sorted(grouped[code])} for code in sorted(grouped, key=atlas.rank)]
