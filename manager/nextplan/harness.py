"""Pure builders for NextPlan fixtures, scenarios and property tests.

No I/O, no clock, no randomness: every builder returns plain data, so a test
can state exactly which facts are VERIFIED, which are only REPORTED and which
are missing. Kept in the package (not in a test module) because the scenario
matrix, the property tests and the read-only pipeline demo all need the same
shapes.
"""

from __future__ import annotations

from manager.nextplan import contracts
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v

BASE = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
HEAD = "3f2a9c1d0b8e7f6a5c4d3e2f1a0b9c8d7e6f5a4b"
REPAIRED = "7c6b5a49382716059e8d4f3e2d1c0b9a88776655"
WORKER_SESSION = "s-worker-1"
REVIEWER_SESSION = "s-reviewer-1"

# The reviewer run ADM itself dispatched. A decision authorizes only when it
# carries these exact values back, so a fixture that forgets them is refused --
# which is the behaviour under test, not an inconvenience.
REVIEWER_RUN = "run-review-1"
PROVIDER = "codex"
JOB_ID = "job-review-1"


def review_dispatch(reviewer_run_id=REVIEWER_RUN, provider=PROVIDER, job_id=JOB_ID):
    """ADM's own record of the review it dispatched."""
    return {"reviewer_run_id": reviewer_run_id, "provider": provider, "job_id": job_id}


def review_decision(verdict="PASS", target=HEAD, reviewer_run_id=REVIEWER_RUN, findings=(),
                    provider=PROVIDER, job_id=JOB_ID, mode="read_only"):
    """One ``adm-review-result/v1`` object."""
    return {"schema": contracts.REVIEW_SCHEMA, "target_sha": target, "reviewer_run_id": reviewer_run_id,
            "verdict": verdict, "findings": [dict(f) for f in findings],
            "provenance": {"provider": provider, "job_id": job_id, "mode": mode}}


def validation_result(passed=12, failed=0, skipped=0, exit_code=0, execution_id="exec-1",
                      argv=("python", "-m", "pytest", "-q"), runner="pytest", counts=True, timed_out=False):
    """One ``adm-validation-result/v1`` as a runner adapter would return it.

    ``counts=False`` is the honest shape for a run whose structured report never
    arrived: the process is recorded, the numbers are not invented.
    """
    return {"schema": contracts.VALIDATION_SCHEMA, "execution_id": execution_id, "argv": list(argv),
            "runner": runner, "started": True, "exit_code": exit_code, "timed_out": timed_out,
            "tests": {"passed": passed, "failed": failed, "skipped": skipped} if counts else None}


def _git_facts(head, verified):
    level = v.VERIFIED if verified else v.REPORTED
    git = "probe:git" if verified else "agent:fenced"
    tests = "probe:test_evidence" if verified else "agent:fenced"
    return {
        "head_sha": (head, level, git),
        "commit_sha": (head, level, git),
        "remote_sha": (head, level, git),
        "push_status": ("pushed", level, git),
        "git_status": ("clean", level, git),
        "branch": ("feat/x", level, git),
        "tests_failed": (0, level, tests),
        "tests_passed": (12, level, tests),
        "tests_run": (12, level, tests),
    }


def worker_result(status="PASS", verified=True, head=HEAD, event_id="evt-w1", task_id="t-1", drop=(), facts=None,
                  evidence=(("test_log", "pytest-output.txt"),), tier="fenced", signals=()):
    """A worker result whose git/test facts are VERIFIED (or merely REPORTED)."""
    result = r.blank_result(event_id, task_id, v.WORKER, tier=tier)
    result["extraction"]["signals"] = sorted(signals)
    if status is not None:
        result = r.with_fact(result, "status", r.fact(status, v.REPORTED, "agent:fenced"))
    for field, (value, level, source) in _git_facts(head, verified).items():
        if field in drop:
            continue
        result = r.with_fact(result, field, r.fact(value, level, source))
    for field, item in (facts or {}).items():
        result = r.with_fact(result, field, item)
    result["evidence"] = [{"kind": kind, "ref": ref} for kind, ref in evidence]
    return result


def reviewer_result(verdict="PASS", reviewed=HEAD, event_id="evt-r1", task_id="t-1",
                    evidence=(("review_notes", "review.md"),), findings=(), facts=None, tier="fenced", signals=(),
                    decisions=None):
    """A reviewer result.

    ``decisions`` defaults to one bound ``adm-review-result/v1`` matching
    ``verdict`` and ``reviewed``, because that is now the only thing that can
    authorize anything. Pass ``decisions=[]`` for a reviewer that wrote only
    prose, or an explicit list to test a malformed, unbound or conflicting one.
    The ``review_verdict`` fact below is kept as the annotation it now is.
    """
    result = r.blank_result(event_id, task_id, v.REVIEWER, tier=tier)
    if decisions is None:
        decisions = ([review_decision(verdict="PASS" if verdict == "PASS" else "REJECT", target=reviewed or HEAD)]
                     if verdict is not None else [])
    result["decisions"] = [dict(d) for d in decisions]
    result["extraction"]["signals"] = sorted(signals)
    result = r.with_fact(result, "status", r.fact("PASS", v.REPORTED, "agent:fenced"))
    if verdict is not None:
        result = r.with_fact(result, "review_verdict", r.fact(verdict, v.REPORTED, "agent:fenced"))
    if reviewed is not None:
        result = r.with_fact(result, "reviewed_sha", r.fact(reviewed, v.REPORTED, "agent:fenced"))
    for field, item in (facts or {}).items():
        result = r.with_fact(result, field, item)
    result["evidence"] = [{"kind": kind, "ref": ref} for kind, ref in evidence]
    result["findings"] = list(findings)
    return result


def event(result=None, kind="result", role=v.WORKER, session_id=None, generation=0, event_id=None, task_id="t-1",
          agent="claude", verification=None, execution=None, orchestration_signals=()):
    if session_id is None:
        session_id = WORKER_SESSION if role == v.WORKER else REVIEWER_SESSION
    if event_id is None:
        event_id = (result or {}).get("event_id", f"evt-{kind}-{generation}")
    return {
        "event_id": event_id, "task_id": task_id, "kind": kind, "role": role, "session_id": session_id,
        "agent": agent, "generation": generation, "result": result,
        "verification": verification, "execution": execution,
        "orchestration_signals": list(orchestration_signals),
    }


def verification(signals=(), observations=(), probes=("git",)):
    return {"observations": list(observations), "signals": sorted(signals), "probes_run": list(probes),
            "probes_unavailable": []}
