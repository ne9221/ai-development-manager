"""Report admission: the full lineage cross-binding.

Phase B-1 bound a report to its execution on four fields. The independent
review showed what that leaves open: a Task and Execution whose task_ids
disagree, a deliverable_sha that matches nothing, an execution in a non-terminal
state, and a bundle with no id to compare against the Task's reference all
sailed through to ACCEPTED. Reports could be assembled from different lineages
and the evaluator had no way to notice.

Every check here is an AND, evaluated in a fixed order so the single reported
reason is deterministic. The ordering runs outward-in -- ticket, then lineage,
then contract, then identity, then environment -- so the first tripped reason
is the most structural one available, which is the most useful thing to show a
human.

A report that fails any check is **not a FAIL**. It does not enter derivation
at all; it does not exist. That distinction matters because a rejected report
that counted as a failure would let an attacker suppress an acceptance by
submitting garbage, and a rejected report that counted as a pass would be the
false-complete this whole layer exists to prevent.
"""

from __future__ import annotations

from typing import Optional

from .bundle import oracle_digest
from .identity import resolution_failure, same_actor
from .models import (
    TRUSTED_EVIDENCE_SOURCES,
    AcceptanceBundleFixture,
    ExecutionFixture,
    PreflightFacts,
    TaskFixture,
    VerificationReportFixture,
    VerificationTicket,
)

# Reasons that mean "the tree moved underneath this round" rather than "this
# report is malformed". The evaluator escalates these to ROUND_INVALIDATED for
# the whole round instead of just dropping one report, because a moved head
# invalidates every verdict measured against it, not only the one that noticed.
ROUND_INVALIDATING_REASONS = (
    "WORKTREE_HEAD_MOVED_DURING_ROUND",
    "WORKTREE_HEAD_NOT_CANDIDATE",
    "TICKET_ISSUED_AGAINST_DIFFERENT_HEAD",
)


def lineage_reason(
    task: TaskFixture,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
) -> Optional[str]:
    """Whether the Task/Execution/Bundle triangle is self-consistent.

    Checked once per evaluation rather than per report: if the Task and the
    Execution disagree about which Task they belong to, or point at different
    acceptance bundles, then no report about that Execution can mean anything,
    however well-formed it is.
    """
    if execution.task_id != task.task_id:
        return "TASK_EXECUTION_TASK_ID_MISMATCH"
    if execution.acceptance_bundle_ref != task.acceptance_bundle_ref:
        return "TASK_EXECUTION_BUNDLE_REF_MISMATCH"
    if execution.acceptance_bundle_ref != bundle.bundle_id:
        return "EXECUTION_BUNDLE_REF_NOT_THIS_BUNDLE"
    return None


def admissibility_reason(
    report: VerificationReportFixture,
    ticket: Optional[VerificationTicket],
    task: TaskFixture,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
    preflight: PreflightFacts,
) -> Optional[str]:
    """None if the report may enter derivation, else the first tripped reason."""

    # --- Ticket: was this round authorised before it was answered? ----------
    if ticket is None:
        return "NO_MATCHING_TICKET"
    if ticket.status != "issued":
        # "consumed" means some other report already answered this ticket;
        # answering it twice is the duplicate case, handled by the caller.
        return "TICKET_NOT_OPEN"
    if ticket.execution_id != report.execution_id:
        return "TICKET_EXECUTION_MISMATCH"
    if ticket.candidate_sha != report.candidate_sha:
        return "TICKET_CANDIDATE_SHA_MISMATCH"
    if ticket.base_sha != report.base_sha:
        return "TICKET_BASE_SHA_MISMATCH"
    if ticket.bundle_hash != report.bundle_hash:
        return "TICKET_BUNDLE_HASH_MISMATCH"
    if ticket.gate_id != report.gate_id:
        return "TICKET_GATE_MISMATCH"
    if ticket.round != report.round:
        return "TICKET_ROUND_MISMATCH"
    if ticket.task_id != report.task_id:
        return "TICKET_TASK_MISMATCH"

    # --- Lineage: does this report describe *this* work? -------------------
    if report.execution_id != execution.execution_id:
        return "EXECUTION_ID_MISMATCH"
    if report.task_id != execution.task_id:
        return "REPORT_TASK_ID_MISMATCH"
    if report.candidate_sha != execution.candidate_sha:
        return "CANDIDATE_SHA_MISMATCH"
    if report.base_sha != execution.base_sha:
        return "BASE_SHA_MISMATCH"
    if report.bundle_hash != bundle.bundle_hash:
        return "BUNDLE_HASH_MISMATCH"
    if report.deliverable_sha != task.deliverable_sha:
        # Binds the report to the Task's single declared deliverable, so gate
        # evidence cannot be gathered under one deliverable and spent under
        # another.
        return "DELIVERABLE_SHA_MISMATCH"

    # --- Contract: is this gate, checker and oracle the frozen one? --------
    checker = bundle.checkers.get(report.gate_id)
    if checker is None:
        return "UNKNOWN_GATE"
    if report.checker_id != checker.checker_id:
        return "CHECKER_IDENTITY_MISMATCH"
    if report.checker_version != checker.checker_version:
        return "CHECKER_VERSION_MISMATCH"
    if report.checker_impl_digest != checker.checker_impl_digest:
        # A matching version with a different implementation is the more
        # dangerous of the two mismatches: it looks correct in every log.
        return "CHECKER_IMPL_DIGEST_MISMATCH"

    expected_oracle = oracle_digest(bundle, report.gate_id)
    if expected_oracle is not None and report.oracle_set_digest != expected_oracle:
        return "FROZEN_ORACLE_DIGEST_MISMATCH"

    if report.governance_digest != bundle.governance_digest:
        return "REPORT_GOVERNANCE_DIGEST_MISMATCH"

    # --- Identity: who executed, who checked, were they resolved? ----------
    executor_failure = resolution_failure(execution.executor_identity)
    if executor_failure is not None:
        # An unresolved executor is not "no conflict" -- it is "we cannot check
        # for one". Phase B-1 read None as safe and ACCEPTED the run.
        return "EXECUTOR_" + executor_failure
    producer_failure = resolution_failure(report.producer_identity)
    if producer_failure is not None:
        return "CHECKER_" + producer_failure
    if not same_actor(report.producer_identity, ticket.expected_checker_identity):
        return "CHECKER_NOT_TICKET_EXPECTED_IDENTITY"
    if ticket.forbidden_identity is not None and same_actor(
        report.producer_identity, ticket.forbidden_identity
    ):
        return "CHECKER_IS_FORBIDDEN_IDENTITY"
    if same_actor(report.producer_identity, execution.executor_identity):
        return "EXECUTOR_EQUALS_CHECKER"

    # --- Evidence and environment ------------------------------------------
    if report.evidence_source not in TRUSTED_EVIDENCE_SOURCES:
        return "UNTRUSTED_EVIDENCE_SOURCE"
    if report.environment_fingerprint != preflight.environment_fingerprint:
        return "ENVIRONMENT_FINGERPRINT_MISMATCH"

    # --- Immutable candidate round (Phase A v3 I6) -------------------------
    if ticket.candidate_head_at_issue != execution.candidate_sha:
        return "TICKET_ISSUED_AGAINST_DIFFERENT_HEAD"
    if report.worktree_head_before != report.worktree_head_after:
        return "WORKTREE_HEAD_MOVED_DURING_ROUND"
    if report.worktree_head_before != execution.candidate_sha:
        return "WORKTREE_HEAD_NOT_CANDIDATE"

    return None
