"""The thin seam between the pure derivation and the durable stores.

Deliberately small. This is Phase B-2 foundation, not a running loop: there is
no scheduling here, no provider dispatch, no activation and no Drive or GitHub
call. It does exactly two things -- issue the tickets for a verification round
before any checker runs, and derive an acceptance from what came back -- which
is the minimum needed to show the pure evaluator and the append-only stores fit
together without a translation layer between them.

The round-trip through the stores is real rather than assumed: reports are read
back from disk and rehydrated into the same dataclasses ``evaluate()`` takes, so
a serialisation that quietly dropped a field would change the derivation and be
caught, instead of being invisible until a real checker existed to trip over it.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any, Mapping, Optional, Sequence, Tuple

from .evaluator import evaluate
from .models import (
    AcceptanceBundleFixture,
    EvaluationResult,
    ExecutionFixture,
    PreflightFacts,
    PriorAcceptance,
    TaskFixture,
    VerificationReportFixture,
    VerificationTicket,
)
from .stores import FileReportStore, FileTicketStore
from .tickets import derive_ticket_id


def rehydrate(cls, data: Any):
    """Rebuild a dataclass from the plain JSON the stores hold.

    Only the shapes tickets and reports actually use are handled -- nested
    dataclasses, Optional, and homogeneous tuples. Anything else raises rather
    than guessing, because a silently mis-typed field would change a
    derivation without changing anything visible.
    """
    if data is None:
        return None
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"not a dataclass: {cls!r}")
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for field in dataclasses.fields(cls):
        kwargs[field.name] = _coerce(hints[field.name], data.get(field.name))
    return cls(**kwargs)


def _coerce(annotation, value):
    origin = typing.get_origin(annotation)

    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        if len(args) == 1:
            return _coerce(args[0], value)
        return value

    if origin is tuple:
        args = typing.get_args(annotation)
        if not args or value is None:
            return tuple(value or ())
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(args[0], item) for item in value)
        return tuple(_coerce(arg, item) for arg, item in zip(args, value))

    if dataclasses.is_dataclass(annotation):
        return rehydrate(annotation, value)

    return value


class VerificationController:
    """Issues tickets, collects reports, derives acceptance. Nothing else."""

    def __init__(self, root) -> None:
        # Both stores refuse a production root; constructing them here means a
        # controller cannot be pointed at production even by accident.
        self.tickets = FileTicketStore(root)
        self.reports = FileReportStore(root)

    def issue_round(
        self,
        *,
        task: TaskFixture,
        execution: ExecutionFixture,
        bundle: AcceptanceBundleFixture,
        gate_ids: Sequence[str],
        round_: int,
        issued_by,
        expected_checker_identity,
        candidate_head_at_issue: str,
        worktree_lock_id: str,
        worktree_generation: int,
        issued_at: str,
    ) -> Tuple[VerificationTicket, ...]:
        """Authorise one round, before any checker has run.

        ``forbidden_identity`` is taken from the Execution rather than passed
        in: who executed is a fact about the work, not a parameter the caller
        gets to choose.
        """
        issued = []
        for gate_id in gate_ids:
            ticket = VerificationTicket(
                ticket_id=derive_ticket_id(
                    execution.execution_id, execution.candidate_sha, bundle.bundle_hash,
                    gate_id, round_,
                ),
                task_id=task.task_id,
                execution_id=execution.execution_id,
                candidate_sha=execution.candidate_sha,
                base_sha=execution.base_sha,
                bundle_hash=bundle.bundle_hash,
                gate_id=gate_id,
                round=round_,
                issued_by=issued_by,
                expected_checker_identity=expected_checker_identity,
                forbidden_identity=execution.executor_identity,
                candidate_head_at_issue=candidate_head_at_issue,
                worktree_lock_id=worktree_lock_id,
                worktree_generation=worktree_generation,
                issued_at=issued_at,
            )
            issued.append(self.tickets.issue(ticket))
        return tuple(issued)

    def submit(self, report: VerificationReportFixture) -> str:
        return self.reports.put(report)

    def stored_tickets(self) -> Tuple[VerificationTicket, ...]:
        return tuple(
            rehydrate(VerificationTicket, self.tickets.get(ticket_id))
            for ticket_id in self.tickets.list_ids()
        )

    def stored_reports(self, execution_id: str) -> Tuple[VerificationReportFixture, ...]:
        loaded = []
        for digest in self.reports.list_digests():
            report = rehydrate(VerificationReportFixture, self.reports.get(digest))
            if report.execution_id == execution_id:
                loaded.append(report)
        return tuple(loaded)

    def derive(
        self,
        *,
        task: TaskFixture,
        execution: ExecutionFixture,
        bundle: AcceptanceBundleFixture,
        preflight: PreflightFacts,
        chain: Sequence[ExecutionFixture] = (),
        chain_reports: Optional[Mapping[str, Sequence[VerificationReportFixture]]] = None,
        prior_acceptance: Optional[PriorAcceptance] = None,
    ) -> EvaluationResult:
        """Derive from what is actually on disk, not from what a caller passes.

        Reading the stores rather than accepting reports as an argument is the
        point: it is the same path a real controller would take, so the
        round-trip is exercised on every derivation instead of only in a test
        that remembers to check it.
        """
        return evaluate(
            task,
            execution,
            bundle,
            self.stored_reports(execution.execution_id),
            preflight=preflight,
            tickets=self.stored_tickets(),
            chain=chain,
            chain_reports=chain_reports,
            prior_acceptance=prior_acceptance,
        )
