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

R3 changes (evidence gaps found after R2 was structurally accepted):

4. Invariant E's terminal-identity check no longer treats Command.provider/
   account_id as authoritative for the *final launched* identity -- current
   runtime permits automatic sibling-account substitution, so Command's own
   provider/account fields are provisional routing evidence only.
   Execution and Session are the terminal actual-identity authority: PASS
   now additionally requires Execution.provider == Session.provider, and
   for provider "claude", Execution.account_id == Session.account_id with
   both non-empty. Command's requested_provider/requested_account_id being
   both None is still required for "automatic" in the first place.

5. Invariant O now independently inspects the real canonical/shared
   checkout read-only (manager.project_registry's Global Project Registry +
   its configured ADM_WORKSPACE_ROOT-style env var -- never the Drive
   Project record's unmaintained `working_directory` literal): resolves
   the canonical path, and reads (never mutates) its origin remote
   identity, `git rev-parse HEAD`, and `git status --porcelain`. PASS now
   additionally requires: the canonical checkout is reachable and its repo
   identity matches the registered project, its working tree is clean, and
   its HEAD still equals Task.baseline_head (proof this execution's
   isolated worktree never touched it). Unreachable/uninspectable is
   UNKNOWN, never PASS.

6. Invariant C now also resolves the Global Project Registry entry (not
   just Task.governance) and requires resolution_status=="verified",
   status=="enabled", a registered repo identity consistent with the
   Task/lease/D2 repo, and non-empty common_governance.reference /
   project_rules.reference -- Task.governance alone no longer proves
   PROJECT-RULES authority. Where a read-only GitHub contents check of
   both referenced files at the admitted baseline was actually performed,
   a confirmed-missing file is a hard FAIL; otherwise this remains a
   best-effort addition to the detail, never blocking PASS on its own
   (matching "where technically practical").

7. Invariant D now additionally requires one fresh, independent resolution
   of the canonical remote baseline (manager.remote_baseline_resolver,
   itself GitHub-API-only, honoring the registry's own
   baseline_resolution_policy for origin_default vs. pinned_ref) and
   requires Task.baseline_head to equal that freshly-resolved SHA -- so
   Task/lease/D2 can no longer all agree on a baseline that is not
   actually the true canonical one. Not performed => UNKNOWN; performed
   and mismatched => FAIL.

R4 corrections (a live-topology false-negative plus two fail-closed gaps
found by independent review of R3):

8. Invariant O no longer requires the canonical/shared checkout's HEAD to
   equal Task.baseline_head -- that assumption is false in live ADM
   topology (a project's registered canonical checkout can legitimately
   sit at a different, e.g. further-ahead, commit than whatever SHA a
   given Task happened to admit as its own baseline_head; they are simply
   different authorities). Instead, this now requires an independent
   PRE-E2E snapshot (`canonical_checkout_before`, which this module cannot
   reconstruct after the fact -- callers running a real acceptance must
   capture it themselves via inspect_canonical_checkout() before
   dispatching) compared against a fresh POST-E2E snapshot
   (`canonical_checkout_after`, captured by collect_evidence() right now):
   PASS requires both available, the same path/repo identity, both clean,
   and identical head_sha before and after. No trustworthy pre-E2E
   snapshot => UNKNOWN, never PASS.

9. Invariant C's reference-file-existence check now fails closed instead
   of silently PASSing on "not independently checked" as a best-effort
   note: True+True is required for PASS, a confirmed-missing reference is
   a hard FAIL, and anything else (not checked, a transport error, or an
   ambiguous private-repo 404) is UNKNOWN. check_repo_file_exists() itself
   was also corrected: a bare Contents-API 404 is never trusted as
   "confirmed missing" on its own, since GitHub deliberately returns 404
   (never 403) for an inaccessible/private repo too -- it is only trusted
   once a follow-up GET on the bare repo resource itself independently
   confirms (200) that this owner/name is actually visible with the
   credentials in use.

10. Invariant D's independent baseline resolution now fails closed on the
    registry's declared baseline_resolution_policy.strategy: manager.
    remote_baseline_resolver.resolve_remote_baseline() only ever
    implements "origin_default" and "pinned_ref" (it dispatches purely on
    whether pinned_ref is set, never on the strategy label itself), so a
    registry entry declaring any other strategy is never silently resolved
    as if it were origin_default just because pinned_ref happens to be
    unset -- the resolution call is skipped entirely and recorded as "not
    performed" with the reason, which evaluate() reports as UNKNOWN.

R5 fixes (two final issues found after R4):

11. SUPPORTED_BASELINE_STRATEGIES was wrong: schema/project_registry.
    schema.json's baseline_resolution_policy.strategy enum is exactly
    ["origin_default", "pinned_commit", "latest_release", "custom"] --
    "pinned_ref" is a sibling FIELD on that policy object, never a
    strategy value, and R4 had mistakenly accepted it as one. Fixed here,
    and manager.remote_baseline_resolver.resolve_remote_baseline() itself
    was hardened to dispatch strictly on the declared strategy (previously
    it ignored "strategy" entirely and just used pinned_ref whenever set):
    origin_default requires pinned_ref to be empty and resolves
    default_branch; pinned_commit requires a non-empty pinned_ref and
    resolves it directly; any other strategy (including a contradictory
    origin_default+pinned_ref combination) fails closed with a dedicated
    error code, never silently reinterpreted as origin_default.

12. Invariant O no longer accepts an arbitrary caller-supplied
    canonical_checkout_before dict on faith. The new preflight evidence
    contract (capture_preflight_snapshot(), write_preflight_snapshot(),
    read_preflight_snapshot() -- a small dedicated read-only JSON file,
    deliberately never written onto any Task/Command/Execution record)
    binds the snapshot to the exact project_id/request_id/observed_at of
    the dispatch it precedes. _evaluate_o() now requires that provenance
    to match the dispatch under verification: missing/unbound provenance
    is UNKNOWN; provenance present but bound to a *different*
    project/request (stale or reused evidence) is a hard FAIL. The CLI
    (`manager.rule44_evidence_verifier capture-preflight` /
    `... verify --canonical-checkout-before <path>`) and
    verify_write_e2e()'s new canonical_checkout_before_path parameter make
    this the ordinary invocation path, not a design where every real
    repo-write verification is forced to return O=UNKNOWN.

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from manager.tasks import TaskError

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REPO_IDENTITY_RE = re.compile(r"github(?::|\.com[:/])(?P<owner>[a-z0-9_.-]+)/(?P<repo>[a-z0-9_.-]+?)(?:\.git)?/?$", re.IGNORECASE)

# schema/project_registry.schema.json's baseline_resolution_policy.strategy
# enum is exactly ["origin_default", "pinned_commit", "latest_release",
# "custom"] -- "pinned_ref" is a sibling FIELD on that policy object, never
# a strategy value, and must never be accepted as one here.
# manager.remote_baseline_resolver.resolve_remote_baseline() only actually
# implements "origin_default" and "pinned_commit" (R5 hardened it to
# dispatch strictly on the declared strategy, fail closed otherwise). Any
# other declared strategy must never be silently treated as origin_default
# -- see collect_evidence()'s baseline-resolution wiring, which mirrors
# this same set so the guard fires before the resolver is ever called.
SUPPORTED_BASELINE_STRATEGIES = frozenset({"origin_default", "pinned_commit"})

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


def _evaluate_c(evidence: Dict[str, Any], expected_repo: Optional[str]) -> InvariantResult:
    task = evidence.get("task")
    if task is None:
        return _result("C", UNKNOWN, "no Task record; governance resolution cannot be checked")
    if not _governance_ok(task.get("governance")):
        return _result("C", FAIL, "Task.governance is missing or incomplete (rules_version/rules_digest/mandatory_rule_ids/mandatory_status_fields) -- this is only the Common Governance stamp, never PROJECT-RULES authority by itself")

    # Task.governance alone never proves PROJECT-RULES authority -- that
    # authority lives in the Global Project Registry entry itself (R3
    # point 6), which the Task's governance stamp is merely a per-task
    # digest of.
    registry_project = evidence.get("registry_project")
    if not isinstance(registry_project, dict):
        return _result("C", UNKNOWN, "Task carries a Common Governance stamp, but no Global Project Registry entry was resolved; project-specific PROJECT-RULES authority cannot be proven")

    if registry_project.get("resolution_status") != "verified":
        return _result("C", FAIL, f"registry resolution_status is {registry_project.get('resolution_status')!r}, not 'verified'")
    if registry_project.get("status") != "enabled":
        return _result("C", FAIL, f"registry status is {registry_project.get('status')!r}, not 'enabled'")

    common_governance = registry_project.get("common_governance") or {}
    if not common_governance.get("reference"):
        return _result("C", FAIL, "registry common_governance.reference is missing")
    project_rules = registry_project.get("project_rules") or {}
    if not project_rules.get("reference"):
        return _result("C", FAIL, "registry project_rules.reference is missing")

    if expected_repo:
        registry_identity = _repo_identity((registry_project.get("repo") or {}).get("canonical_url"))
        expected_identity = _repo_identity(expected_repo)
        if registry_identity and expected_identity and registry_identity != expected_identity:
            return _result("C", FAIL, f"registry repo canonical_url does not match expected repo {expected_repo!r}")

    baseline_policy = registry_project.get("baseline_resolution_policy")
    if not isinstance(baseline_policy, dict) or not baseline_policy.get("strategy"):
        return _result("C", FAIL, "registry baseline_resolution_policy is missing or has no 'strategy'")

    # R4 correction: fail closed on reference-file existence. True+True is
    # required for PASS; a confirmed-missing file (False) is a hard FAIL;
    # anything else (not checked / transport error / ambiguous private-repo
    # 404 -- see check_repo_file_exists) is UNKNOWN, never silently folded
    # into PASS as a "best-effort" note the way R3 did.
    file_check = evidence.get("registry_reference_file_check") or {}
    governance_exists = file_check.get("common_governance_exists")
    rules_exists = file_check.get("project_rules_exists")
    if governance_exists is False:
        return _result("C", FAIL, f"common_governance reference {common_governance.get('reference')!r} is confirmed missing at the admitted baseline")
    if rules_exists is False:
        return _result("C", FAIL, f"project_rules reference {project_rules.get('reference')!r} is confirmed missing at the admitted baseline")
    if governance_exists is not True or rules_exists is not True:
        return _result("C", UNKNOWN, "registry verified+enabled with common_governance/project_rules references present, but at least one reference file's existence at the admitted baseline could not be independently confirmed (not checked / transport error / ambiguous auth) -- never treated as PASS")

    return _result("C", PASS, f"Common Governance stamp present on Task; registry verified+enabled with common_governance.reference={common_governance.get('reference')!r} and project_rules.reference={project_rules.get('reference')!r} as the project-specific rules authority; both reference files independently confirmed to exist at the admitted baseline")


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

    # Task/lease/D2 agreeing with each other still doesn't prove the
    # baseline they all agree on is the *true* canonical one (R3 point 7):
    # require one fresh, independent resolution of the canonical remote
    # baseline (manager.remote_baseline_resolver, itself GitHub-API-only,
    # honoring the registry's own baseline_resolution_policy).
    resolution = evidence.get("remote_baseline_resolution")
    if not isinstance(resolution, dict) or not resolution.get("performed"):
        reason = (resolution or {}).get("error") if isinstance(resolution, dict) else None
        detail = "branch/baseline_head resolved and consistent across Task/lease/D2 evidence, but no independent canonical-baseline resolution was performed"
        detail += f": {reason}" if reason else "; Task.baseline_head cannot be proven to match true canonical authority"
        return _result("D", UNKNOWN, detail)
    if resolution.get("error"):
        return _result("D", FAIL, f"independent canonical baseline resolution failed: {resolution['error']}")
    resolved_sha = resolution.get("baseline_sha")
    if resolved_sha != baseline_head:
        return _result("D", FAIL, f"Task.baseline_head {baseline_head!r} does not match the independently-resolved canonical baseline {resolved_sha!r}")

    return _result("D", PASS, f"branch/baseline_head resolved and consistent across Task/lease/D2 evidence, and independently confirmed to equal the true canonical remote baseline {resolved_sha}")


def _evaluate_e(evidence: Dict[str, Any]) -> InvariantResult:
    command = evidence.get("command")
    if command is None:
        return _result("E", UNKNOWN, "no Command record; provider/account selection cannot be checked")
    if command.get("requested_provider") is not None:
        return _result("E", FAIL, f"Command.requested_provider={command.get('requested_provider')!r} was explicitly supplied; not automatic selection")
    if command.get("requested_account_id") is not None:
        return _result("E", FAIL, f"Command.requested_account_id={command.get('requested_account_id')!r} was explicitly supplied; not automatic selection")

    # Command.provider/account_id are provisional routing evidence only --
    # current runtime permits automatic sibling-account substitution, so
    # the *final launched* identity is never assumed to equal Command's.
    # Execution + Session are the terminal actual-identity authority.
    execution = evidence.get("execution")
    session = evidence.get("session")
    if execution is None or session is None:
        missing = [name for name, record in (("execution", execution), ("session", session)) if record is None]
        return _result("E", UNKNOWN, f"requested_provider/requested_account_id both absent, but terminal identity cannot be verified; missing: {', '.join(missing)}")

    terminal_provider = execution.get("provider")
    if not terminal_provider:
        return _result("E", FAIL, "Execution.provider is empty; no terminal provider attribution")
    if execution.get("provider") != session.get("provider"):
        return _result("E", FAIL, f"Execution.provider {execution.get('provider')!r} != Session.provider {session.get('provider')!r}; terminal identity disagrees")

    if terminal_provider == "claude":
        exec_account_id = execution.get("account_id")
        session_account_id = session.get("account_id")
        if not exec_account_id or not session_account_id:
            return _result("E", FAIL, "provider is claude but Execution.account_id/Session.account_id is missing; terminal account attribution cannot be proven")
        if exec_account_id != session_account_id:
            return _result("E", FAIL, f"Execution.account_id {exec_account_id!r} != Session.account_id {session_account_id!r}; terminal account identity disagrees")

    return _result("E", PASS, f"requested_provider/requested_account_id both absent; terminal provider {terminal_provider!r} (Execution == Session) routed automatically with consistent account attribution")


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


def _valid_observed_at(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _parse_iso_z(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp ending in 'Z' -- tolerant of the
    fractional-seconds precision difference between capture_preflight_
    snapshot()'s observed_at (whole seconds) and manager.tasks.now_iso()'s
    dispatch_request.created_at (microseconds). Returns None (never
    raises) for anything that doesn't parse, so callers can fail closed
    rather than trust a malformed timestamp."""
    if not isinstance(value, str) or not value or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def _evaluate_o(evidence: Dict[str, Any], expected_project_id: str, expected_request_id: str) -> InvariantResult:
    task = evidence.get("task")
    execution = evidence.get("execution")
    if task is None:
        return _result("O", UNKNOWN, "no Task record; canonical-checkout isolation cannot be checked")
    if task.get("read_only") is True:
        return _result("O", PASS, "Task is read_only; no repo write to check against the canonical checkout")

    # R4 correction: Task.baseline_head and the canonical/shared checkout's
    # HEAD are NOT the same authority and are never required to be equal --
    # ADM's registered canonical checkout can legitimately sit at a
    # different (e.g. further-ahead) commit than whatever origin_default
    # SHA a given Task happened to admit as its own baseline_head. Proving
    # the canonical checkout was untouched by this E2E instead requires an
    # independent PRE-E2E snapshot (canonical_checkout_before) compared
    # against a fresh POST-E2E snapshot (canonical_checkout_after) -- both
    # produced by the same read-only inspect_canonical_checkout() helper,
    # never by comparing either one to Task.baseline_head. If no
    # trustworthy pre-E2E snapshot was supplied, this is UNKNOWN, never
    # PASS -- there is no way to reconstruct "before" after the fact.
    #
    # R5 correction: an arbitrary caller-supplied dict is no longer taken
    # on faith. The pre-E2E snapshot must carry its own provenance --
    # project_id, request_id, and a valid observed_at -- produced by
    # capture_preflight_snapshot() and bound to the exact dispatch under
    # verification. Missing/unbound provenance is UNKNOWN (cannot prove
    # anything); provenance that is *present but wrong* (a stale snapshot
    # from a different project or request reused here) is a hard FAIL,
    # never silently accepted.
    before = evidence.get("canonical_checkout_before")
    after = evidence.get("canonical_checkout_after")

    if not isinstance(before, dict) or before.get("project_id") is None or before.get("request_id") is None:
        return _result("O", UNKNOWN, "no trustworthy pre-E2E canonical-checkout snapshot is bound to this dispatch (missing, or missing project_id/request_id provenance); canonical-checkout integrity cannot be proven after the fact")
    if before.get("project_id") != expected_project_id or before.get("request_id") != expected_request_id:
        return _result("O", FAIL, f"pre-E2E snapshot is bound to project_id={before.get('project_id')!r}/request_id={before.get('request_id')!r}, not this verification's project_id={expected_project_id!r}/request_id={expected_request_id!r} -- mismatched or stale evidence")
    if before.get("schema_version") != PREFLIGHT_SNAPSHOT_SCHEMA_VERSION:
        return _result("O", FAIL, f"pre-E2E snapshot schema_version {before.get('schema_version')!r} != expected {PREFLIGHT_SNAPSHOT_SCHEMA_VERSION!r}")
    if not _valid_observed_at(before.get("observed_at")):
        return _result("O", UNKNOWN, "pre-E2E snapshot has no valid observed_at timestamp; its provenance cannot be trusted")

    # R5.1 correction: prove the snapshot was actually captured BEFORE
    # dispatch, not merely that it carries the right project_id/request_id
    # -- a snapshot bound to the right identity but taken too late (e.g.
    # after the E2E already started mutating the isolated worktree, or
    # after a race) would otherwise slip through. dispatch_request.
    # created_at (manager.dispatch_requests, stamped by manager.tasks.
    # now_iso() at claim time) is the authoritative "dispatch happened at"
    # timestamp; the preflight snapshot's own observed_at must be no later
    # than it.
    dispatch_request = evidence.get("dispatch_request")
    created_at = dispatch_request.get("created_at") if isinstance(dispatch_request, dict) else None
    created_dt = _parse_iso_z(created_at)
    if created_dt is None:
        return _result("O", UNKNOWN, f"dispatch_request.created_at ({created_at!r}) is missing or unparseable; pre-E2E snapshot chronology cannot be proven")
    observed_dt = _parse_iso_z(before.get("observed_at"))
    if observed_dt is None or observed_dt > created_dt:
        return _result("O", FAIL, f"pre-E2E snapshot observed_at ({before.get('observed_at')!r}) is not <= dispatch_request.created_at ({created_at!r}); the snapshot was not proven to precede dispatch")

    if not before.get("available"):
        reason = before.get("reason")
        return _result("O", UNKNOWN, f"pre-E2E snapshot recorded the canonical checkout as unavailable{': ' + reason if reason else ''}; canonical-checkout integrity cannot be proven after the fact")

    if not isinstance(after, dict) or not after.get("available"):
        reason = (after or {}).get("reason") if isinstance(after, dict) else None
        return _result("O", UNKNOWN, f"canonical checkout could not be independently inspected after the E2E{': ' + reason if reason else ''}")

    if _norm_path(before.get("path")) != _norm_path(after.get("path")):
        return _result("O", FAIL, f"canonical checkout path changed between pre- and post-E2E snapshots: {before.get('path')!r} -> {after.get('path')!r}")
    if before.get("repo_identity_ok") is not True or after.get("repo_identity_ok") is not True:
        return _result("O", FAIL, "canonical checkout repo identity is not confirmed correct in the pre- and/or post-E2E snapshot")
    if before.get("clean") is not True:
        return _result("O", FAIL, "canonical checkout was already dirty before the E2E started (pre-E2E snapshot)")
    if after.get("clean") is not True:
        return _result("O", FAIL, "canonical checkout is dirty after the E2E (git status --porcelain is not empty)")
    if before.get("head_sha") != after.get("head_sha"):
        return _result("O", FAIL, f"canonical checkout HEAD changed during the E2E: {before.get('head_sha')!r} -> {after.get('head_sha')!r}")

    # Task.working_directory is deliberately None for repo-write ingress
    # (module docstring point 2); the actual runtime checkout location is
    # instead the working_directory _resolve_working_directory() resolved
    # for this exact launch, snapshotted onto Execution.task_snapshot by
    # reserve_execution() -- captured strictly after materialization, so it
    # reflects the real isolated worktree path even when Task.working_directory
    # itself still reads None.
    snapshot = (execution or {}).get("task_snapshot") or {}
    runtime_working_directory = snapshot.get("working_directory") or task.get("working_directory")

    if runtime_working_directory is None:
        d2 = _d2(execution)
        lease = (execution or {}).get("lease_evidence")
        if d2 is not None or isinstance(lease, dict):
            return _result("O", UNKNOWN, "canonical checkout independently confirmed clean/correct/unchanged before and after the E2E, but no runtime working_directory was recorded on Execution.task_snapshot to prove the isolated worktree path itself differs from it")
        return _result("O", UNKNOWN, "no runtime working_directory available on Task or Execution.task_snapshot; canonical-checkout isolation cannot be checked")

    task_dir = _norm_path(runtime_working_directory)
    canonical_dir = _norm_path(after.get("path"))
    if task_dir and canonical_dir and task_dir == canonical_dir:
        return _result("O", FAIL, "the runtime working_directory is the same path as the independently-inspected canonical/shared checkout")
    return _result("O", PASS, "canonical checkout independently confirmed reachable, correct repo identity, clean, and with an unchanged HEAD both before and after the E2E, and distinct from the runtime working_directory")


_EVALUATORS = {
    "A": lambda e, p, r, repo: _evaluate_a(e, r),
    "B": lambda e, p, r, repo: _evaluate_b(e, p),
    "C": lambda e, p, r, repo: _evaluate_c(e, repo),
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
    "O": lambda e, p, r, repo: _evaluate_o(e, p, r),
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


def _run_git_readonly(cwd: str, *args: str, runner=subprocess.run) -> Optional[str]:
    """Run one read-only `git` command against `cwd` and return its trimmed
    stdout, or None if it failed. Only ever invoked (see
    inspect_canonical_checkout below) with read-only subcommands
    (remote get-url / rev-parse / status --porcelain) -- this module never
    mutates, resets, or checks out the canonical repo."""
    try:
        completed = runner(["git", "-C", cwd, *args], text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30)
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip()


def inspect_canonical_checkout(project_metadata, workspace_root: Optional[str] = None,
                                exists_check=None, git_runner=subprocess.run) -> Dict[str, Any]:
    """Independently, read-only, locate and inspect the real canonical/
    shared checkout for `project_metadata` (a manager.project_registry.
    ProjectMetadata) -- resolved via the Global Project Registry's own
    workspace-root convention, never the Drive Project record's
    unmaintained `working_directory` literal. Only ever runs read-only git
    subcommands (remote get-url / rev-parse HEAD / status --porcelain);
    never fetches, resets, or checks out anything. Returns
    {"available": False, "reason": ...} whenever the checkout cannot be
    located or fully inspected -- callers must treat that as UNKNOWN, never
    PASS."""
    import os as _os

    if exists_check is None:
        exists_check = _os.path.isdir

    env_var = "ADM_WORKSPACE_ROOT"
    policy = getattr(project_metadata, "working_directory_policy", None)
    if isinstance(policy, dict):
        env_var = policy.get("env_var", env_var)
    root = workspace_root if workspace_root is not None else _os.environ.get(env_var)
    if not root:
        return {"available": False, "reason": f"{env_var} is not set; canonical checkout cannot be independently located"}

    path = str(project_metadata.resolve_runtime_working_directory(workspace_root=root))
    if not exists_check(path):
        return {"available": False, "path": path, "reason": "canonical checkout path does not exist"}

    remote_url = _run_git_readonly(path, "remote", "get-url", "origin", runner=git_runner)
    if remote_url is None:
        return {"available": False, "path": path, "reason": "could not read canonical checkout's origin remote"}
    from manager.project_registry import normalize_repo_identity
    repo_identity_ok = bool(normalize_repo_identity(remote_url)) and normalize_repo_identity(remote_url) == getattr(project_metadata, "repo_identity", None)

    head_sha = _run_git_readonly(path, "rev-parse", "HEAD", runner=git_runner)
    if head_sha is None:
        return {"available": False, "path": path, "reason": "could not read canonical checkout HEAD"}

    status_output = _run_git_readonly(path, "status", "--porcelain", runner=git_runner)
    if status_output is None:
        return {"available": False, "path": path, "reason": "could not read canonical checkout git status"}

    return {"available": True, "path": path, "repo_identity_ok": repo_identity_ok, "head_sha": head_sha, "clean": status_output == ""}


PREFLIGHT_SNAPSHOT_SCHEMA_VERSION = "1.0.0"


def capture_preflight_snapshot(project_metadata, request_id: str, workspace_root: Optional[str] = None,
                                exists_check=None, git_runner=subprocess.run) -> Dict[str, Any]:
    """The Rule44 write-E2E preflight evidence contract (R5): a read-only
    snapshot of the canonical/shared checkout, captured BEFORE a write-E2E
    is dispatched, and bound to the exact fresh `request_id` that dispatch
    is about to use.

    This is the only trustworthy source for invariant O's
    canonical_checkout_before -- an arbitrary caller-supplied dict is never
    accepted on faith; _evaluate_o() requires this snapshot's own
    project_id/request_id/observed_at provenance to match the dispatch
    under verification (see that function's docstring). Reuses
    inspect_canonical_checkout() verbatim (never duplicates its git
    subcommands) and never mutates/resets/checks out anything.

    Callers running a real Rule44 write-E2E acceptance must call this
    immediately before dispatching, then persist the result (see
    write_preflight_snapshot()/read_preflight_snapshot() below -- a small
    dedicated read-only evidence file, never Task/Command truth) so it
    survives until the post-hoc verifier runs.
    """
    inspection = inspect_canonical_checkout(project_metadata, workspace_root=workspace_root, exists_check=exists_check, git_runner=git_runner)
    return {
        "schema_version": PREFLIGHT_SNAPSHOT_SCHEMA_VERSION,
        "project_id": project_metadata.project_id,
        "request_id": request_id,
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **inspection,
    }


def write_preflight_snapshot(snapshot: Dict[str, Any], path) -> None:
    """Persist a capture_preflight_snapshot() result to a small, dedicated,
    read-only acceptance-evidence file -- deliberately never written onto
    any Task/Command/Execution record, so Rule44's own preflight provenance
    can never be confused with (or overload) ADM's actual dispatch truth."""
    import json as _json
    from pathlib import Path as _Path
    _Path(path).write_text(_json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def read_preflight_snapshot(path) -> Dict[str, Any]:
    """Read back a snapshot written by write_preflight_snapshot(). Read-only;
    raises if the file is missing or unparseable rather than guessing."""
    import json as _json
    from pathlib import Path as _Path
    return _json.loads(_Path(path).read_text(encoding="utf-8"))


def check_repo_file_exists(owner: str, name: str, path: str, ref: str, token: Optional[str] = None, http_get=None) -> Optional[bool]:
    """Best-effort, read-only proof that `path` exists in owner/name at
    commit/ref `ref`, via the GitHub Contents API. Returns True only on an
    unambiguous 200. Returns None (not proven either way -- never a
    positive AND never treated as a confirmed-missing FAIL) for any
    transport error, rate limit, or unexpected status.

    A bare Contents-API 404 is NOT by itself treated as "confirmed
    missing": GitHub deliberately returns 404 (never 403) for a private or
    otherwise inaccessible repository, to avoid confirming its existence
    to an unauthorized caller -- so an unauthenticated/under-privileged
    404 here is genuinely ambiguous between "file absent" and "repo not
    visible to us". A 404 is only trusted as a real absence once repo
    accessibility itself is independently unambiguous: a follow-up GET on
    the bare repo resource must itself return 200 (proving this exact
    owner/name is actually visible with the credentials in use) before its
    sibling Contents 404 is trusted as a genuine "file does not exist".
    """
    if http_get is None:
        import requests as _requests
        http_get = _requests.get
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    contents_url = f"https://api.github.com/repos/{owner}/{name}/contents/{path}"
    try:
        response = http_get(contents_url, headers=headers, params={"ref": ref}, timeout=15)
    except Exception:
        return None
    if response.status_code == 200:
        return True
    if response.status_code != 404:
        return None

    repo_url = f"https://api.github.com/repos/{owner}/{name}"
    try:
        repo_response = http_get(repo_url, headers=headers, timeout=15)
    except Exception:
        return None
    if repo_response.status_code == 200:
        return False
    return None


def collect_evidence(store, project_id: str, request_id: str, task_claim_registry_factory=None,
                      dispatch_registry_factory=None, bucket: Optional[str] = None,
                      final_commit_sha: Optional[str] = None, expected_repo: Optional[str] = None,
                      git_runner=subprocess.run, project_registry=None, workspace_root: Optional[str] = None,
                      github_fetch=None, github_token: Optional[str] = None,
                      repo_file_exists_check=None, canonical_checkout_exists_check=None,
                      canonical_checkout_before: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble one evidence dict for (project_id, request_id) using only
    existing read APIs. Never raises for a merely-missing record -- every
    field is None (or []) if it cannot be found, and `evaluate()` reports
    UNKNOWN/FAIL rather than this function ever guessing.

    `final_commit_sha` is now only a fallback for a non-repo-write (or
    legacy/pre-D2) execution -- when Execution.repo_write_completion_evidence
    exists, its own commit_sha/branch/repository drive the independent
    `git ls-remote` readback directly; a caller-supplied final_commit_sha is
    never required (and never substituted) in that case.

    `project_registry` (a manager.project_registry.ProjectRegistry, default
    manager.project_registry.get_global_registry()), `workspace_root` (a
    forced canonical-checkout root, default the registry entry's own
    configured env var), `github_fetch`/`github_token` (forwarded to
    manager.remote_baseline_resolver.resolve_remote_baseline), and
    `repo_file_exists_check` (forwarded to check_repo_file_exists) are all
    injectable so tests never need live Drive/GCS/git/GitHub access.

    `canonical_checkout_before` is the one piece of evidence this function
    cannot reconstruct after the fact: a snapshot of the canonical/shared
    checkout taken (via inspect_canonical_checkout()) BEFORE the E2E was
    ever dispatched. Callers running a real Rule44 write-E2E acceptance
    must capture it themselves at that point and pass it in here; omitting
    it makes invariant O UNKNOWN, never PASS. The POST-E2E snapshot
    (canonical_checkout_after) is always captured fresh, right now, by
    this function.
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
        "final_commit_sha": final_commit_sha, "registry_project": None,
        "registry_reference_file_check": None, "canonical_checkout_before": canonical_checkout_before,
        "canonical_checkout_after": None, "remote_baseline_resolution": None,
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

    # Global Project Registry entry (R3 points 5-7): resolved read-only,
    # never mutated. A resolution failure (unregistered/ambiguous/disabled
    # project) leaves every dependent evidence field None -- evaluate()
    # reports UNKNOWN, never guesses.
    registry_metadata = None
    try:
        registry = project_registry
        if registry is None:
            from manager.project_registry import get_global_registry
            registry = get_global_registry()
        registry_metadata = registry.get_project(project_id, allow_disabled=True)
    except Exception:
        registry_metadata = None

    if registry_metadata is not None:
        raw_repo = registry_metadata.repo if isinstance(registry_metadata.repo, dict) else None
        evidence["registry_project"] = {
            "resolution_status": registry_metadata.resolution_status,
            "status": registry_metadata.status,
            "common_governance": dict(registry_metadata.common_governance) if isinstance(registry_metadata.common_governance, dict) else registry_metadata.common_governance,
            "project_rules": dict(registry_metadata.project_rules) if isinstance(registry_metadata.project_rules, dict) else registry_metadata.project_rules,
            "baseline_resolution_policy": dict(registry_metadata.baseline_resolution_policy) if isinstance(registry_metadata.baseline_resolution_policy, dict) else registry_metadata.baseline_resolution_policy,
            "repo": dict(raw_repo) if raw_repo else None,
        }

        # Best-effort, read-only proof the referenced governance/rules
        # files actually exist at the admitted baseline (R3 point 6). Never
        # required for this function to succeed; a transport failure just
        # leaves both as None (evaluate() treats that as "not independently
        # checked", not a failure).
        owner = raw_repo.get("owner") if raw_repo else None
        name = raw_repo.get("name") if raw_repo else None
        baseline_head = task.get("baseline_head") if task else None
        if owner and name and baseline_head:
            file_check = repo_file_exists_check or check_repo_file_exists
            gov_ref = (registry_metadata.common_governance or {}).get("reference") if isinstance(registry_metadata.common_governance, dict) else None
            rules_ref = (registry_metadata.project_rules or {}).get("reference") if isinstance(registry_metadata.project_rules, dict) else None
            token = github_token
            evidence["registry_reference_file_check"] = {
                "common_governance_exists": file_check(owner, name, gov_ref, baseline_head, token=token) if gov_ref else None,
                "project_rules_exists": file_check(owner, name, rules_ref, baseline_head, token=token) if rules_ref else None,
            }

        # Independent, read-only POST-E2E canonical-checkout inspection (R3
        # point 5 / R4 correction 1). canonical_checkout_before, if any, was
        # already supplied by the caller above -- it can never be captured
        # here, after the fact.
        evidence["canonical_checkout_after"] = inspect_canonical_checkout(
            registry_metadata, workspace_root=workspace_root, exists_check=canonical_checkout_exists_check, git_runner=git_runner)

        # Independent, fresh canonical-baseline resolution (R3 point 7),
        # fail-closed on the registry's own declared strategy (R4
        # correction 3): manager.remote_baseline_resolver.resolve_remote_baseline
        # only ever implements "origin_default" (project.default_branch) and
        # "pinned_ref" (an explicit pinned_ref) -- it does not dispatch on
        # the strategy label at all, it just uses pinned_ref when present.
        # A registry entry declaring any other strategy (e.g.
        # "latest_release", a bespoke custom scheme) must never be silently
        # resolved as if it were origin_default just because pinned_ref
        # happens to be unset; this call is skipped entirely in that case
        # and recorded as "not performed" with the reason, so evaluate()
        # reports UNKNOWN rather than a false PASS/FAIL built on the wrong
        # baseline semantics.
        if task is not None and task.get("baseline_head"):
            policy = registry_metadata.baseline_resolution_policy if isinstance(registry_metadata.baseline_resolution_policy, dict) else {}
            strategy = policy.get("strategy")
            if strategy not in SUPPORTED_BASELINE_STRATEGIES:
                evidence["remote_baseline_resolution"] = {
                    "performed": False, "baseline_sha": None,
                    "error": f"registry baseline_resolution_policy.strategy {strategy!r} is not implemented by remote_baseline_resolver "
                             f"(supported: {sorted(SUPPORTED_BASELINE_STRATEGIES)}); refusing to silently reinterpret it as origin_default",
                }
            else:
                try:
                    from manager.remote_baseline_resolver import resolve_remote_baseline
                    kwargs = {"registry": registry}
                    if github_fetch is not None:
                        kwargs["github_fetch"] = github_fetch
                    if github_token is not None:
                        kwargs["github_token"] = github_token
                    resolved = resolve_remote_baseline(project_id, **kwargs)
                    evidence["remote_baseline_resolution"] = {"performed": True, "baseline_sha": resolved.get("baseline_sha"), "error": None}
                except Exception as exc:
                    evidence["remote_baseline_resolution"] = {"performed": True, "baseline_sha": None, "error": str(exc)}

    return evidence


def verify_write_e2e(store, project_id: str, request_id: str, expected_repo: Optional[str] = None,
                      final_commit_sha: Optional[str] = None, test_evidence: Optional[Dict[str, Any]] = None,
                      bucket: Optional[str] = None, git_runner=subprocess.run, workspace_root: Optional[str] = None,
                      github_token: Optional[str] = None,
                      canonical_checkout_before: Optional[Dict[str, Any]] = None,
                      canonical_checkout_before_path=None) -> VerifierReport:
    """Top-level convenience: collect real evidence for one finished
    dispatch and evaluate the full Rule44 write-E2E contract against it.

    `canonical_checkout_before` must be a snapshot produced by
    capture_preflight_snapshot() BEFORE the E2E was dispatched -- see
    collect_evidence()'s own docstring. `canonical_checkout_before_path` is
    the usual real-invocation path: the file write_preflight_snapshot()
    wrote at capture time, read back here via read_preflight_snapshot()
    (mutually exclusive with passing the dict directly; the path wins if
    both are given). Omitting both makes invariant O UNKNOWN, never PASS --
    but a real repo-write acceptance run is never forced into that shape:
    it only has to call capture_preflight_snapshot() once before
    dispatching and pass the result (or its persisted path) here.
    """
    if canonical_checkout_before_path is not None:
        canonical_checkout_before = read_preflight_snapshot(canonical_checkout_before_path)
    evidence = collect_evidence(store, project_id, request_id, bucket=bucket,
                                 final_commit_sha=final_commit_sha, expected_repo=expected_repo,
                                 git_runner=git_runner, workspace_root=workspace_root, github_token=github_token,
                                 canonical_checkout_before=canonical_checkout_before)
    if test_evidence is not None:
        evidence["test_evidence"] = test_evidence
    return evaluate(evidence, expected_project_id=project_id, expected_request_id=request_id, expected_repo=expected_repo)


def _capture_preflight_command(args) -> int:
    from manager.project_registry import get_global_registry

    project_metadata = get_global_registry().get_project(args.project_id, allow_disabled=True)
    snapshot = capture_preflight_snapshot(project_metadata, args.request_id, workspace_root=args.workspace_root)
    write_preflight_snapshot(snapshot, args.out)
    print(f"wrote preflight snapshot to {args.out}: {snapshot}")
    return 0


def _verify_command(args) -> int:
    import json as _json

    from collectors.publish_drive import build_service
    from manager.tasks import DriveRecords

    store = DriveRecords(build_service())
    report = verify_write_e2e(
        store, args.project_id, args.request_id, expected_repo=args.expected_repo,
        final_commit_sha=args.final_commit_sha, bucket=args.bucket,
        workspace_root=args.workspace_root, canonical_checkout_before_path=args.canonical_checkout_before,
    )
    print(_json.dumps(report.as_dict(), indent=2))
    return 0 if report.overall == PASS else 1


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Read-only Rule44 write-E2E evidence verifier")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture-preflight",
        help="Capture a read-only canonical-checkout snapshot BEFORE dispatching a write-E2E, bound to its fresh request_id",
    )
    capture.add_argument("--project-id", required=True)
    capture.add_argument("--request-id", required=True)
    capture.add_argument("--workspace-root", default=None)
    capture.add_argument("--out", required=True, help="Path to write the preflight snapshot JSON to")
    capture.set_defaults(func=_capture_preflight_command)

    verify = subparsers.add_parser("verify", help="Verify a finished dispatch against the full Rule44 write-E2E contract")
    verify.add_argument("--project-id", required=True)
    verify.add_argument("--request-id", required=True)
    verify.add_argument("--expected-repo", default=None)
    verify.add_argument("--final-commit-sha", default=None)
    verify.add_argument("--bucket", default=None)
    verify.add_argument("--workspace-root", default=None)
    verify.add_argument("--canonical-checkout-before", default=None,
                         help="Path to the preflight snapshot written by 'capture-preflight' -- without this, invariant O is UNKNOWN")
    verify.set_defaults(func=_verify_command)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
