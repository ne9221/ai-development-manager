#!/usr/bin/env python3
"""Read-only Rule44 write-E2E evidence verifier (R2, ported onto the
integration/post-home-runtime-foundations-20260823 baseline).

Proves (or disproves) that one finished dispatch -- identified by
(project_id, request_id) -- satisfies the full write-E2E evidence chain:
fresh request_id -> Task/Command/Execution/Session/Handoff, isolated
worktree/branch, real provider edits, tests, a real commit, an
independently-verified GitHub push, truthful terminal state, released
claim, no duplicate authority, and canonical checkout left untouched.

This module never dispatches, launches a provider, or mutates any ADM
record. It reads existing Drive/GCS records via the same read APIs the
rest of the codebase already uses (manager.tasks.DriveRecords,
manager.task_claims.check_task_execution_claim, the manager.
dispatch_requests GCSLockRegistry) plus one independent, read-only
`git ls-remote` call.

R2 changes vs the R1 (ee5000e) version, driven by Slice D2 landing on
this baseline:

1. Execution.repo_write_completion_evidence (D2's own
   changed_paths/tests/commit_sha/remote_sha/branch/repository/
   baseline_head) is now the authoritative source for "real edits",
   "tests passed", "real commit", and "real push" -- a caller no longer
   needs to manually supply final_commit_sha/test_evidence when a bounded
   repo-write execution actually produced D2 evidence. This module still
   never trusts D2's own stored remote_sha by itself: it always performs
   one fresh, independent `git ls-remote` and requires
   commit_sha == stored remote_sha == the fresh readback, exactly as
   directed.

2. Task.working_directory is deliberately persisted as None by repo-write
   ingress (Cloud Run has no HOME-local checkout; see
   fix/direct-dispatch-working-directory-authority-p0-20260822 R2 and
   manager.execution_runner._resolve_working_directory's own docstring).
   A correct repo-write Task must never fail an invariant merely because
   Task.working_directory is None. Isolation is instead proven from:
   Task.branch/baseline_head (admission-time authority, never the
   None-by-design field), Execution.lease_evidence (only ever populated
   for an execution that actually acquired the isolated-worktree writer
   lease), Execution.repo_write_completion_evidence (D2 evidence only
   ever exists once a real materialized worktree committed and pushed),
   and Execution.task_snapshot.working_directory (task_snapshot() is
   captured by reserve_execution() *after* _resolve_working_directory()
   already ran and backfilled the real path for this launch -- so it is
   the actual runtime checkout location even when the Task's own
   top-level working_directory field reads None).

3. Invariant E (automatic provider/account selection) now checks the
   trusted Command's own requested_provider/requested_account_id fields
   (manager.trusted_ingress / cloud.dispatch_ingress: preserved verbatim
   for audit, never launch authority by themselves) rather than the
   provisional/assigned Command.account_id. Automatic-selection PASS
   requires requested_provider and requested_account_id to BOTH be None
   while the actual terminal provider/account attribution is present;
   either one being explicitly set fails Rule44's automatic-selection
   acceptance outright.

Two layers, deliberately kept separate:

- `evaluate(...)` is a pure function over an already-assembled evidence
  dict. It performs no I/O, so tests exercise the real acceptance logic
  with synthetic fixtures, exactly as Phase 4 requires.
- `collect_evidence(...)` is the thin, real-API wiring layer that
  assembles that dict for one live (project_id, request_id). Nothing in
  it is a new source of truth -- every field is read from a record type
  that already exists in this codebase.

Every invariant reports one of PASS / FAIL / UNKNOWN. UNKNOWN means the
evidence needed to decide is simply absent (fails closed: UNKNOWN is
never treated as PASS by `overall_verdict`). FAIL means evidence was
found and it contradicts the invariant.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from manager.tasks import TaskError

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REPO_IDENTITY_RE = re.compile(r"github(?::|\.com[:/])(?P<owner>[a-z0-9_.-]+)/(?P<repo>[a-z0-9_.-]+?)(?:\.git)?/?$", re.IGNORECASE)

INVARIANT_ORDER = ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O")

INVARIANT_TITLES = {
    "A": "fresh unique request_id",
    "B": "correct project identification",
    "C": "Common Governance + PROJECT-RULES resolved",
    "D": "canonical repo/baseline resolved",
    "E": "provider/account selected automatically",
    "F": "isolated branch/worktree created",
    "G": "real provider edits repo",
    "H": "tests execute and pass",
    "I": "real commit created",
    "J": "real push to GitHub verified by remote readback",
    "K": "Task/Command/Execution/Session/Handoff all linked",
    "L": "Execution reaches truthful terminal state",
    "M": "claim released",
    "N": "no duplicate Task/Command/Execution authority",
    "O": "canonical/shared production checkout not modified",
}


@dataclass
class InvariantResult:
    code: str
    verdict: str
    detail: str


@dataclass
class VerifierReport:
    results: List[InvariantResult]
    overall: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "invariants": {
                r.code: {"title": INVARIANT_TITLES[r.code], "verdict": r.verdict, "detail": r.detail}
                for r in self.results
            },
        }


def _result(code: str, verdict: str, detail: str) -> InvariantResult:
    return InvariantResult(code=code, verdict=verdict, detail=detail)


def _sha_matches(candidate: Optional[str], recorded: Sequence[str]) -> bool:
    if not candidate:
        return False
    for entry in recorded:
        if not isinstance(entry, str) or not entry:
            continue
        if entry == candidate or entry.startswith(candidate) or candidate.startswith(entry):
            return True
    return False


def _norm_path(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value.replace("\\", "/").rstrip("/").lower()


def _repo_identity(value: Optional[str]) -> Optional[str]:
    """Normalize a repo identifier -- either lease_evidence/D2's
    `github:owner/repo` form or a project's `https://github.com/owner/repo(.git)`
    URI -- down to a bare `owner/repo` for cross-comparison, so the same
    repository is recognized regardless of which representation a given
    record happens to store."""
    if not isinstance(value, str) or not value:
        return None
    match = _REPO_IDENTITY_RE.search(value)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}".lower()


def _branch_short(ref: Optional[str]) -> Optional[str]:
    if not isinstance(ref, str) or not ref:
        return None
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def _governance_ok(governance: Any) -> bool:
    if not isinstance(governance, dict):
        return False
    if not isinstance(governance.get("rules_version"), str) or not governance["rules_version"]:
        return False
    digest = governance.get("rules_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False
    if not isinstance(governance.get("mandatory_rule_ids"), list) or not governance["mandatory_rule_ids"]:
        return False
    if not isinstance(governance.get("mandatory_status_fields"), list) or not governance["mandatory_status_fields"]:
        return False
    return True


def _is_repo_write_task(task: Optional[Dict[str, Any]]) -> bool:
    """Whether `task` is a bounded v2-repo-write Task, using the exact same
    admission predicate the rest of the codebase already gates on
    (manager.trusted_ingress.repo_write_policy_satisfied) rather than a
    second, independently-drifting definition. False for a read-only task,
    a legacy/malformed task, or None (unknown)."""
    if not isinstance(task, dict):
        return False
    try:
        from manager.trusted_ingress import repo_write_policy_satisfied
        return bool(repo_write_policy_satisfied(task))
    except Exception:
        return False


def _d2(execution: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(execution, dict):
        return None
    evidence = execution.get("repo_write_completion_evidence")
    return evidence if isinstance(evidence, dict) else None


def _evaluate_a(evidence: Dict[str, Any], expected_request_id: str) -> InvariantResult:
    dispatch_request = evidence.get("dispatch_request")
    command = evidence.get("command")
    if dispatch_request is None:
        return _result("A", FAIL, "no dispatch_request claim record found for this request_id")
    if dispatch_request.get("request_id") != expected_request_id:
        return _result("A", FAIL, "dispatch_request record's request_id does not match the request_id being verified")
    if command is None:
        return _result("A", FAIL, "no Command record found to cross-check request_id against")
    if command.get("request_id") != expected_request_id:
        return _result("A", FAIL, "Command.request_id does not match the dispatch_request's request_id")
    return _result("A", PASS, "dispatch_request and Command agree on one request_id")


def _evaluate_b(evidence: Dict[str, Any], expected_project_id: str) -> InvariantResult:
    task = evidence.get("task")
    if task is None:
        return _result("B", FAIL, "no Task record found; project identification cannot be proven")
    if task.get("project_id") != expected_project_id:
        return _result("B", FAIL, f"Task.project_id {task.get('project_id')!r} != expected {expected_project_id!r}")
    for name in ("command", "execution", "session", "handoff"):
        record = evidence.get(name)
        if record is not None and record.get("project_id") not in (None, expected_project_id):
            return _result("B", FAIL, f"{name}.project_id {record.get('project_id')!r} != expected {expected_project_id!r}")
    return _result("B", PASS, "every present record agrees on project_id")


def _evaluate_c(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    if task is None:
        return _result("C", UNKNOWN, "no Task record; governance resolution cannot be checked")
    if not _governance_ok(task.get("governance")):
        return _result("C", FAIL, "Task.governance is missing or incomplete (rules_version/rules_digest/mandatory_rule_ids/mandatory_status_fields)")
    return _result("C", PASS, "Task carries a complete governance stamp")


def _evaluate_d(evidence: Dict[str, Any], expected_repo: Optional[str]) -> InvariantResult:
    task = evidence.get("task")
    if task is None:
        return _result("D", UNKNOWN, "no Task record; repo/baseline resolution cannot be checked")
    branch = task.get("branch")
    baseline_head = task.get("baseline_head")
    # Task.working_directory is deliberately None for repo-write ingress
    # (Cloud Run has no HOME-local checkout) -- never gated on here. See
    # module docstring point 2.
    if not branch or not baseline_head:
        return _result("D", FAIL, "Task is missing branch/baseline_head admission-time authority")

    execution = evidence.get("execution")
    lease = execution.get("lease_evidence") if execution else None
    if isinstance(lease, dict):
        if lease.get("baseline_head") != baseline_head:
            return _result("D", FAIL, "Execution.lease_evidence.baseline_head does not match Task.baseline_head")
        if lease.get("branch") != f"refs/heads/{branch}":
            return _result("D", FAIL, "Execution.lease_evidence.branch does not match Task.branch")
        if expected_repo:
            lease_identity, expected_identity = _repo_identity(lease.get("repository")), _repo_identity(expected_repo)
            if lease_identity and expected_identity and lease_identity != expected_identity:
                return _result("D", FAIL, f"Execution.lease_evidence.repository {lease.get('repository')!r} does not match expected repo {expected_repo!r}")

    d2 = _d2(execution)
    if d2 is not None:
        if d2.get("baseline_head") != baseline_head:
            return _result("D", FAIL, "D2 repo_write_completion_evidence.baseline_head does not match Task.baseline_head")
        if _branch_short(d2.get("branch")) != branch:
            return _result("D", FAIL, "D2 repo_write_completion_evidence.branch does not match Task.branch")
        if isinstance(lease, dict) and d2.get("branch") != lease.get("branch"):
            return _result("D", FAIL, "D2 repo_write_completion_evidence.branch does not match Execution.lease_evidence.branch")
        if isinstance(lease, dict):
            d2_identity, lease_identity = _repo_identity(d2.get("repository")), _repo_identity(lease.get("repository"))
            if d2_identity and lease_identity and d2_identity != lease_identity:
                return _result("D", FAIL, "D2 repo_write_completion_evidence.repository does not match Execution.lease_evidence.repository")
        if expected_repo:
            d2_identity, expected_identity = _repo_identity(d2.get("repository")), _repo_identity(expected_repo)
            if d2_identity and expected_identity and d2_identity != expected_identity:
                return _result("D", FAIL, f"D2 repo_write_completion_evidence.repository {d2.get('repository')!r} does not match expected repo {expected_repo!r}")

    return _result("D", PASS, "branch/baseline_head resolved and consistent across Task/lease/D2 evidence")


def _evaluate_e(evidence: Dict[str, Any]) -> InvariantResult:
    command = evidence.get("command")
    if command is None:
        return _result("E", UNKNOWN, "no Command record; provider/account selection cannot be checked")
    if command.get("requested_provider") is not None:
        return _result("E", FAIL, f"Command.requested_provider={command.get('requested_provider')!r} was explicitly supplied; not automatic selection")
    if command.get("requested_account_id") is not None:
        return _result("E", FAIL, f"Command.requested_account_id={command.get('requested_account_id')!r} was explicitly supplied; not automatic selection")
    provider = command.get("provider")
    if not provider:
        return _result("E", FAIL, "Command.provider is empty; no terminal provider attribution")
    if provider == "claude":
        execution = evidence.get("execution") or {}
        session = evidence.get("session") or {}
        account_id = command.get("account_id") or execution.get("account_id") or session.get("account_id")
        if not account_id:
            return _result("E", FAIL, "provider is claude but no account_id was recorded on Command/Execution/Session")
    return _result("E", PASS, f"requested_provider/requested_account_id both absent; provider {provider!r} routed automatically with terminal attribution present")


def _evaluate_f(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    if task is None:
        return _result("F", UNKNOWN, "no Task record; worktree/branch isolation cannot be checked")
    branch = task.get("branch")
    if not branch:
        return _result("F", FAIL, "Task is missing branch admission-time authority")

    execution = evidence.get("execution")
    lease = execution.get("lease_evidence") if execution else None
    d2 = _d2(execution)
    # Isolation is proven by the lease (only ever acquired for the isolated
    # worktree path, manager.worktree_materializer) and/or D2 evidence (only
    # ever produced once a real materialized worktree committed+pushed) --
    # never by Task.worktree_id/working_directory alone, since ingress
    # deliberately leaves working_directory None (module docstring point 2).
    if not isinstance(lease, dict) and d2 is None:
        return _result("F", UNKNOWN, "no Execution.lease_evidence and no D2 completion evidence; isolated worktree use cannot be proven")
    if isinstance(lease, dict) and lease.get("branch") != f"refs/heads/{branch}":
        return _result("F", FAIL, "Execution.lease_evidence.branch does not match the isolated Task.branch")
    if task.get("worktree_id") is not None and not task.get("worktree_id"):
        return _result("F", FAIL, "Task.worktree_id is present but empty")
    return _result("F", PASS, "isolated worktree use proven via lease_evidence/D2 completion evidence")


def _evaluate_g(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    execution = evidence.get("execution")
    d2 = _d2(execution)
    if _is_repo_write_task(task):
        if d2 is None:
            return _result("G", UNKNOWN, "Task is a bounded repo-write Task but no D2 repo_write_completion_evidence exists; real edits cannot be proven (a Handoff free-text claim is never accepted by itself)")
        changed_paths = d2.get("changed_paths")
        if not isinstance(changed_paths, list) or not changed_paths:
            return _result("G", FAIL, "D2 repo_write_completion_evidence.changed_paths is empty")
        return _result("G", PASS, f"D2 evidence records {len(changed_paths)} changed path(s)")
    handoff = evidence.get("handoff")
    if handoff is None:
        return _result("G", UNKNOWN, "no Handoff record; real edits cannot be checked")
    files_changed = handoff.get("files_changed")
    if not isinstance(files_changed, list) or not files_changed:
        return _result("G", FAIL, "Handoff.files_changed is empty; no evidence the provider actually edited the repo")
    return _result("G", PASS, f"Handoff records {len(files_changed)} changed file(s)")


def _evaluate_h(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    execution = evidence.get("execution")
    d2 = _d2(execution)
    if _is_repo_write_task(task):
        if d2 is None:
            return _result("H", UNKNOWN, "Task is a bounded repo-write Task but no D2 repo_write_completion_evidence exists; tests cannot be proven (Handoff free text is never accepted by itself)")
        tests = d2.get("tests")
        if not isinstance(tests, list) or not tests:
            return _result("H", FAIL, "D2 repo_write_completion_evidence.tests is empty; no evidence any validation check ran")
        failing = [check for check in tests if not (isinstance(check, dict) and check.get("passed") is True)]
        if failing:
            return _result("H", FAIL, f"D2 evidence records {len(failing)} failing/incomplete validation check(s): {failing!r}")
        return _result("H", PASS, f"D2 evidence records {len(tests)} passing validation check(s)")

    test_evidence = evidence.get("test_evidence")
    if isinstance(test_evidence, dict) and "passed" in test_evidence:
        if test_evidence.get("passed") is True and test_evidence.get("exit_code", 0) == 0:
            return _result("H", PASS, f"independent test_evidence reports a passing run: {test_evidence.get('command')!r}")
        return _result("H", FAIL, f"independent test_evidence reports a failing/incomplete run: {test_evidence!r}")
    handoff = evidence.get("handoff")
    if handoff is None:
        return _result("H", UNKNOWN, "no Handoff and no independent test_evidence; tests cannot be checked")
    tests = handoff.get("tests")
    if not isinstance(tests, list) or not tests:
        return _result("H", FAIL, "Handoff.tests is empty; no evidence any test ran")
    return _result("H", UNKNOWN, "Handoff.tests is free-text and self-reported; no independently-verifiable evidence was supplied, so a real pass cannot be proven")


def _evaluate_i(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    execution = evidence.get("execution")
    d2 = _d2(execution)
    if _is_repo_write_task(task):
        if d2 is None:
            return _result("I", UNKNOWN, "Task is a bounded repo-write Task but no D2 repo_write_completion_evidence exists; a real commit cannot be proven")
        commit_sha = d2.get("commit_sha")
        if not commit_sha or not _SHA_RE.match(commit_sha):
            return _result("I", FAIL, f"D2 repo_write_completion_evidence.commit_sha is missing or malformed: {commit_sha!r}")
        return _result("I", PASS, f"D2 evidence records real commit {commit_sha}")

    final_commit_sha = evidence.get("final_commit_sha")
    if not final_commit_sha or not _SHA_RE.match(final_commit_sha):
        return _result("I", UNKNOWN, "no final_commit_sha supplied; a real commit cannot be checked")
    handoff = evidence.get("handoff")
    if handoff is None:
        return _result("I", UNKNOWN, "final_commit_sha supplied but no Handoff to cross-check it against")
    commits = handoff.get("commits")
    if not isinstance(commits, list) or not commits:
        return _result("I", FAIL, "Handoff.commits is empty despite a final_commit_sha being claimed")
    if not _sha_matches(final_commit_sha, commits):
        return _result("I", FAIL, "final_commit_sha does not appear in Handoff.commits")
    return _result("I", PASS, f"commit {final_commit_sha} recorded in Handoff.commits")


def _evaluate_j(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    execution = evidence.get("execution")
    d2 = _d2(execution)
    check = evidence.get("remote_ref_check")

    if _is_repo_write_task(task):
        if d2 is None:
            return _result("J", UNKNOWN, "Task is a bounded repo-write Task but no D2 repo_write_completion_evidence exists; a push cannot be proven")
        commit_sha, stored_remote_sha = d2.get("commit_sha"), d2.get("remote_sha")
        if not commit_sha or not stored_remote_sha:
            return _result("J", FAIL, "D2 repo_write_completion_evidence is missing commit_sha/remote_sha")
        if commit_sha != stored_remote_sha:
            return _result("J", FAIL, f"D2's own commit_sha {commit_sha!r} != its stored remote_sha {stored_remote_sha!r}; internally inconsistent")
        if not isinstance(check, dict) or not check.get("performed"):
            return _result("J", UNKNOWN, "D2's stored remote_sha agrees with commit_sha, but no independent fresh git ls-remote readback was performed -- the stored remote_sha is never trusted alone")
        if check.get("error"):
            return _result("J", FAIL, f"fresh remote readback failed: {check['error']}")
        fresh_sha = check.get("remote_sha")
        if fresh_sha != commit_sha:
            return _result("J", FAIL, f"fresh git ls-remote readback {fresh_sha!r} disagrees with D2 commit_sha/remote_sha {commit_sha!r}; the remote has moved or D2's evidence is stale")
        return _result("J", PASS, f"commit_sha == stored remote_sha == fresh ls-remote readback ({commit_sha}) on {check.get('ref')}")

    if not isinstance(check, dict) or not check.get("performed"):
        return _result("J", UNKNOWN, "no independent remote readback was performed; a local commit is never accepted as proof of a push")
    if check.get("error"):
        return _result("J", FAIL, f"remote readback failed: {check['error']}")
    if check.get("matches") is True:
        return _result("J", PASS, f"git ls-remote confirms {check.get('remote_sha')} is live on {check.get('ref')}")
    return _result("J", FAIL, f"remote HEAD {check.get('remote_sha')!r} does not match the claimed commit")


def _evaluate_k(evidence: Dict[str, Any], expected_request_id: str) -> InvariantResult:
    task = evidence.get("task")
    if task is None:
        return _result("K", FAIL, "no Task record; nothing else can be linked to it")
    task_id = task.get("task_id")
    command = evidence.get("command")
    execution = evidence.get("execution")
    session = evidence.get("session")
    handoff = evidence.get("handoff")

    for name, record in (("command", command), ("execution", execution), ("handoff", handoff)):
        if record is not None and record.get("task_id") != task_id:
            return _result("K", FAIL, f"{name}.task_id {record.get('task_id')!r} != Task.task_id {task_id!r}")
    if session is not None and session.get("task_id") not in (None, task_id):
        return _result("K", FAIL, f"session.task_id {session.get('task_id')!r} != Task.task_id {task_id!r}")

    if command is not None and command.get("request_id") != expected_request_id:
        return _result("K", FAIL, "Command.request_id does not match the request_id under verification")

    if command is not None and execution is not None:
        if command.get("execution_id") and command.get("execution_id") != execution.get("execution_id"):
            return _result("K", FAIL, "Command.execution_id does not match Execution.execution_id")

    if execution is not None and session is not None:
        if execution.get("session_id") and execution.get("session_id") != session.get("session_id"):
            return _result("K", FAIL, "Execution.session_id does not match Session.session_id")

    if command is None or execution is None or session is None or handoff is None:
        missing = [name for name, record in (("command", command), ("execution", execution), ("session", session), ("handoff", handoff)) if record is None]
        return _result("K", UNKNOWN, f"cannot confirm the full chain; missing: {', '.join(missing)}")
    return _result("K", PASS, "Task/Command/Execution/Session/Handoff all cross-reference consistently")


def _evaluate_l(evidence: Dict[str, Any]) -> InvariantResult:
    execution = evidence.get("execution")
    if execution is None:
        return _result("L", UNKNOWN, "no Execution record; terminal state cannot be checked")
    status = execution.get("status")
    if status != "completed":
        return _result("L", FAIL, f"Execution.status is {status!r}, not a truthful completed terminal state")
    if not execution.get("completed_at") and not execution.get("finished_at"):
        return _result("L", FAIL, "Execution.status is completed but neither completed_at nor finished_at is set")
    return _result("L", PASS, "Execution reached a truthful completed terminal state")


def _evaluate_m(evidence: Dict[str, Any]) -> InvariantResult:
    execution = evidence.get("execution")
    task_claim = evidence.get("task_claim")
    if task_claim is None:
        return _result("M", PASS, "no active task claim outstanding; claim was released")
    if execution is not None and task_claim.get("execution_id") == execution.get("execution_id"):
        return _result("M", FAIL, f"task claim is still held by this execution {execution.get('execution_id')!r}; never released")
    return _result("M", UNKNOWN, "a task claim exists but is held by a different execution; cannot confirm this execution's claim was released")


def _evaluate_n(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    execution = evidence.get("execution")
    sibling_executions = evidence.get("sibling_executions")
    sibling_commands = evidence.get("sibling_commands")
    if task is None or execution is None or sibling_executions is None or sibling_commands is None:
        return _result("N", UNKNOWN, "insufficient sibling Execution/Command data to check for duplicate authority")

    by_id = {ex.get("execution_id"): ex for ex in sibling_executions if isinstance(ex, dict)}
    active = [ex for ex in by_id.values() if ex.get("status") != "cancelled"]
    roots = [ex for ex in active if not ex.get("retry_of_execution_id")]
    if len(roots) > 1:
        return _result("N", FAIL, f"found {len(roots)} independent (non-retry) Executions for this Task; duplicate authority")
    chain_ids = set()
    for ex in active:
        if ex.get("retry_of_execution_id") and ex["retry_of_execution_id"] not in by_id:
            return _result("N", FAIL, f"Execution {ex.get('execution_id')!r} retries an execution not present in this Task's history")
        chain_ids.add(ex.get("execution_id"))
    if execution.get("execution_id") not in chain_ids:
        return _result("N", FAIL, "the Execution under verification is not part of this Task's own (non-cancelled) execution chain")

    matching_commands = [cmd for cmd in sibling_commands if isinstance(cmd, dict) and cmd.get("request_id") == evidence.get("_expected_request_id")]
    distinct_command_ids = {cmd.get("command_id") for cmd in matching_commands}
    if len(distinct_command_ids) > 1:
        return _result("N", FAIL, f"found {len(distinct_command_ids)} distinct Commands sharing one request_id; duplicate authority")

    return _result("N", PASS, "exactly one execution chain and one Command own this Task's request_id")


def _evaluate_o(evidence: Dict[str, Any]) -> InvariantResult:
    task = evidence.get("task")
    project = evidence.get("project")
    execution = evidence.get("execution")
    if task is None or project is None:
        return _result("O", UNKNOWN, "no Task or Project record; canonical-checkout isolation cannot be checked")
    if task.get("read_only") is True:
        return _result("O", PASS, "Task is read_only; no repo write to check against the canonical checkout")

    # Task.working_directory is deliberately None for repo-write ingress
    # (module docstring point 2); the actual runtime checkout location is
    # instead the working_directory _resolve_working_directory() resolved
    # for this exact launch, snapshotted onto Execution.task_snapshot by
    # reserve_execution() -- captured strictly after materialization, so it
    # reflects the real isolated worktree path even when Task.working_directory
    # itself still reads None.
    snapshot = (execution or {}).get("task_snapshot") or {}
    runtime_working_directory = snapshot.get("working_directory") or task.get("working_directory")
    canonical_dir = _norm_path(project.get("working_directory"))

    if runtime_working_directory is None:
        d2 = _d2(execution)
        lease = (execution or {}).get("lease_evidence")
        if d2 is not None or isinstance(lease, dict):
            return _result("O", UNKNOWN, "no runtime working_directory recorded on Execution.task_snapshot, but lease/D2 evidence exists -- isolation is proven elsewhere (see invariant F) though this specific path comparison cannot be made")
        return _result("O", UNKNOWN, "no runtime working_directory available on Task or Execution.task_snapshot; canonical-checkout isolation cannot be checked")

    task_dir = _norm_path(runtime_working_directory)
    if task_dir and canonical_dir and task_dir == canonical_dir:
        return _result("O", FAIL, "the runtime working_directory is the same path as the project's canonical/shared checkout")
    return _result("O", PASS, "the runtime working_directory is isolated from the canonical/shared checkout")


_EVALUATORS = {
    "A": lambda e, p, r, repo: _evaluate_a(e, r),
    "B": lambda e, p, r, repo: _evaluate_b(e, p),
    "C": lambda e, p, r, repo: _evaluate_c(e),
    "D": lambda e, p, r, repo: _evaluate_d(e, repo),
    "E": lambda e, p, r, repo: _evaluate_e(e),
    "F": lambda e, p, r, repo: _evaluate_f(e),
    "G": lambda e, p, r, repo: _evaluate_g(e),
    "H": lambda e, p, r, repo: _evaluate_h(e),
    "I": lambda e, p, r, repo: _evaluate_i(e),
    "J": lambda e, p, r, repo: _evaluate_j(e),
    "K": lambda e, p, r, repo: _evaluate_k(e, r),
    "L": lambda e, p, r, repo: _evaluate_l(e),
    "M": lambda e, p, r, repo: _evaluate_m(e),
    "N": lambda e, p, r, repo: _evaluate_n(e),
    "O": lambda e, p, r, repo: _evaluate_o(e),
}


def overall_verdict(results: Sequence[InvariantResult]) -> str:
    verdicts = {r.verdict for r in results}
    if FAIL in verdicts:
        return FAIL
    if UNKNOWN in verdicts:
        return UNKNOWN
    return PASS


def evaluate(evidence: Dict[str, Any], expected_project_id: str, expected_request_id: str, expected_repo: Optional[str] = None) -> VerifierReport:
    """Pure evaluation over an already-assembled evidence dict. No I/O."""
    evidence = dict(evidence)
    evidence["_expected_request_id"] = expected_request_id
    results = [_EVALUATORS[code](evidence, expected_project_id, expected_request_id, expected_repo) for code in INVARIANT_ORDER]
    return VerifierReport(results=results, overall=overall_verdict(results))


def _safe_get(store, area, project_id, name):
    try:
        return store.get(area, project_id, name)
    except TaskError:
        return None


def _safe_list(store, area, project_id):
    try:
        return store.list_records(area, project_id)
    except TaskError:
        return []


def check_remote_ref(repo_url: str, branch: str, expected_sha: Optional[str], runner=subprocess.run) -> Dict[str, Any]:
    """Independent, read-only proof that `expected_sha` is actually live on
    the remote's `refs/heads/<branch>` -- never inferred from a local
    commit, a stored remote_sha, or anything a provider/Handoff/D2 record
    merely claims."""
    ref = f"refs/heads/{branch}"
    if not expected_sha:
        return {"performed": False, "ref": ref, "remote_sha": None, "matches": False, "error": "no expected commit sha supplied"}
    try:
        completed = runner(["git", "ls-remote", repo_url, ref], text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=60)
    except Exception as exc:  # pragma: no cover - defensive, exercised via fake runners in tests
        return {"performed": True, "ref": ref, "remote_sha": None, "matches": False, "error": str(exc)}
    if completed.returncode != 0:
        return {"performed": True, "ref": ref, "remote_sha": None, "matches": False, "error": (completed.stderr or "git ls-remote failed").strip()}
    lines = (completed.stdout or "").strip().splitlines()
    if not lines:
        return {"performed": True, "ref": ref, "remote_sha": None, "matches": False, "error": f"remote ref {ref} not found"}
    remote_sha = lines[0].split()[0]
    return {"performed": True, "ref": ref, "remote_sha": remote_sha, "matches": remote_sha == expected_sha, "error": None}


def collect_evidence(store, project_id: str, request_id: str, task_claim_registry_factory=None,
                      dispatch_registry_factory=None, bucket: Optional[str] = None,
                      final_commit_sha: Optional[str] = None, expected_repo: Optional[str] = None,
                      git_runner=subprocess.run) -> Dict[str, Any]:
    """Assemble one evidence dict for (project_id, request_id) using only
    existing read APIs. Never raises for a merely-missing record -- every
    field is None (or []) if it cannot be found, and `evaluate()` reports
    UNKNOWN/FAIL rather than this function ever guessing.

    `final_commit_sha` is now only a fallback for a non-repo-write (or
    legacy/pre-D2) execution -- when Execution.repo_write_completion_evidence
    exists, its own commit_sha/branch/repository drive the independent
    `git ls-remote` readback directly; a caller-supplied final_commit_sha is
    never required (and never substituted) in that case.
    """
    if task_claim_registry_factory is None:
        from manager.task_claims import task_claim_registry as task_claim_registry_factory
    if dispatch_registry_factory is None:
        from manager.dispatch_requests import dispatch_request_registry as dispatch_registry_factory
    from manager.task_claims import check_task_execution_claim

    evidence: Dict[str, Any] = {
        "dispatch_request": None, "task": None, "command": None, "execution": None,
        "session": None, "handoff": None, "project": None, "task_claim": None,
        "sibling_executions": None, "sibling_commands": None, "remote_ref_check": None,
        "final_commit_sha": final_commit_sha,
    }

    dispatch_request = None
    try:
        registry = dispatch_registry_factory(bucket, project_id, request_id)
        existing = registry.read_if_exists()
        if existing is not None:
            document, _generation, _ = existing
            dispatch_request = document
    except Exception:
        dispatch_request = None
    evidence["dispatch_request"] = dispatch_request

    task_id = dispatch_request.get("task_id") if dispatch_request else None
    command_id = dispatch_request.get("command_id") if dispatch_request else None

    project = _safe_get(store, "projects", project_id, project_id)
    evidence["project"] = project

    task = _safe_get(store, "tasks", project_id, task_id) if task_id else None
    evidence["task"] = task

    command = _safe_get(store, "commands", project_id, command_id) if command_id else None
    evidence["command"] = command

    execution_id = command.get("execution_id") if command else None
    execution = _safe_get(store, "executions", project_id, execution_id) if execution_id else None
    evidence["execution"] = execution

    session_id = execution.get("session_id") if execution else None
    session = _safe_get(store, "sessions", project_id, session_id) if session_id else None
    evidence["session"] = session

    handoff = None
    if task_id:
        try:
            handoff = store.latest("handoffs", project_id, task_id)
        except TaskError:
            handoff = None
    evidence["handoff"] = handoff

    if task_id:
        registry = task_claim_registry_factory(bucket, project_id, task_id)
        evidence["task_claim"] = check_task_execution_claim(registry, project_id, task_id)

    if task_id:
        all_executions = _safe_list(store, "executions", project_id)
        evidence["sibling_executions"] = [ex for ex in all_executions if isinstance(ex, dict) and ex.get("task_id") == task_id]
        all_commands = _safe_list(store, "commands", project_id)
        evidence["sibling_commands"] = [cmd for cmd in all_commands if isinstance(cmd, dict) and cmd.get("task_id") == task_id]

    repo = expected_repo or (project.get("repo") if project else None)
    d2 = _d2(execution)
    if d2 is not None:
        d2_repo = repo
        d2_identity = _repo_identity(d2.get("repository"))
        if d2_identity and not repo:
            d2_repo = d2.get("repository")
        branch_short = _branch_short(d2.get("branch"))
        if d2_repo and branch_short and d2.get("commit_sha"):
            evidence["remote_ref_check"] = check_remote_ref(d2_repo, branch_short, d2.get("commit_sha"), runner=git_runner)
    else:
        branch = task.get("branch") if task else None
        if repo and branch and final_commit_sha:
            evidence["remote_ref_check"] = check_remote_ref(repo, branch, final_commit_sha, runner=git_runner)

    return evidence


def verify_write_e2e(store, project_id: str, request_id: str, expected_repo: Optional[str] = None,
                      final_commit_sha: Optional[str] = None, test_evidence: Optional[Dict[str, Any]] = None,
                      bucket: Optional[str] = None, git_runner=subprocess.run) -> VerifierReport:
    """Top-level convenience: collect real evidence for one finished
    dispatch and evaluate the full Rule44 write-E2E contract against it."""
    evidence = collect_evidence(store, project_id, request_id, bucket=bucket,
                                 final_commit_sha=final_commit_sha, expected_repo=expected_repo,
                                 git_runner=git_runner)
    if test_evidence is not None:
        evidence["test_evidence"] = test_evidence
    return evaluate(evidence, expected_project_id=project_id, expected_request_id=request_id, expected_repo=expected_repo)


def main():
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Read-only Rule44 write-E2E evidence verifier")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-repo", default=None)
    parser.add_argument("--final-commit-sha", default=None)
    parser.add_argument("--bucket", default=None)
    args = parser.parse_args()

    from collectors.publish_drive import build_service
    from manager.tasks import DriveRecords

    store = DriveRecords(build_service())
    report = verify_write_e2e(store, args.project_id, args.request_id, expected_repo=args.expected_repo,
                               final_commit_sha=args.final_commit_sha, bucket=args.bucket)
    print(_json.dumps(report.as_dict(), indent=2))
    return 0 if report.overall == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
