"""The three required false-complete fixtures, plus single-factor builders.

Nothing here talks to a real provider, Excel, Drive, GitHub, screenshot or
Session Center. Every one of those surfaces is fixture data, which is the point:
the admission and classification logic has to be provable before any real
checker exists to be trusted or distrusted.

``Scenario.report()`` and ``Scenario.ticket()`` produce a *fully valid* record
by default and apply overrides on top. That is a test-integrity property, not a
convenience. The Phase B-1 review found attack test 05 (mixed SHA) was vacuous
because its report differed from the execution in two ways at once -- so the
test still passed when the SHA check was mutated away, since the identity check
was catching it instead. Builders that are valid by construction make a
one-field override genuinely single-factor, so a test that claims to prove the
SHA binding actually depends on it.

The B-1R re-review left one such wart open as a residual: FX-ADM-FALSE-DISPATCH's
production execution used a different ``execution_id`` from its reports, so
every report was rejected on lineage before the production routing was reached.
That is fixed here -- the production variant now shares the execution's id, so
the routing is proven by production risk alone.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Tuple

from .bundle import finalize_bundle, oracle_digest
from .identity import Identity, ResolvedIdentity
from .models import (
    AcceptanceBundleFixture,
    BaselineAttestation,
    Budget,
    CheckerSpec,
    ComponentRule,
    EnvironmentFingerprint,
    ExecutionFixture,
    FailureObservation,
    FrozenOracle,
    ImpactRule,
    PreflightFacts,
    RiskRule,
    TaskFixture,
    VerificationReportFixture,
    VerificationTicket,
)
from .risk import COMPONENT_PRODUCTION_CHECKOUT, COMPONENT_PRODUCTION_HOME
from .tickets import derive_ticket_id

GOVERNANCE_URI = "gdrive://1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU"
GOVERNANCE_VERSION = "0.5.0"
GOVERNANCE_DIGEST = "gov-digest-v0-5-0"

ENV = EnvironmentFingerprint(
    os="win32", python="3.14.7", checkout_path_length=128, ai_manager_home_class="ephemeral"
)

# Three distinct actors. The executor and the checker differ in all three
# fields, so "executor is not the checker" is a property of the data rather
# than something a test has to force.
EXECUTOR = ResolvedIdentity(
    Identity("codex", "acct-executor", "sess-exec-1"), "classified", "high", "deterministic_signal"
)
CHECKER = ResolvedIdentity(
    Identity("claude", "acct-checker", "sess-check-1"), "classified", "high", "deterministic_signal"
)
CONTROLLER = ResolvedIdentity(
    Identity("adm", "acct-controller", "sess-ctl-1"), "classified", "high", "deterministic_signal"
)

CANDIDATE_SHA = "a" * 40
BASE_SHA = "b" * 40

# One worktree lease, shared by the tickets and the preflight facts, so a
# lease mismatch in a test is something the test asked for rather than an
# artefact of two fixtures having been written independently.
WORKTREE_LOCK_ID = "repo-lease-1"
WORKTREE_GENERATION = 1


def checker_spec(gate_id: str, **overrides: Any) -> CheckerSpec:
    spec = CheckerSpec(
        checker_id=gate_id.lower() + "_checker",
        checker_version="1.0.0",
        checker_impl_digest="impl-" + gate_id.lower() + "-1",
    )
    return replace(spec, **overrides) if overrides else spec


@dataclass(frozen=True)
class Scenario:
    """One coherent (task, execution, bundle, preflight) world plus builders."""

    task: TaskFixture
    execution: ExecutionFixture
    bundle: AcceptanceBundleFixture
    preflight: PreflightFacts
    executor: ResolvedIdentity = EXECUTOR
    checker: ResolvedIdentity = CHECKER
    controller: ResolvedIdentity = CONTROLLER

    def ticket(self, gate_id: str, round_: int = 1, **overrides: Any) -> VerificationTicket:
        ticket = VerificationTicket(
            ticket_id="",
            task_id=self.task.task_id,
            execution_id=self.execution.execution_id,
            candidate_sha=self.execution.candidate_sha,
            base_sha=self.execution.base_sha,
            bundle_hash=self.bundle.bundle_hash,
            gate_id=gate_id,
            round=round_,
            issued_by=self.controller,
            expected_checker_identity=self.checker,
            forbidden_identity=self.executor,
            candidate_head_at_issue=self.execution.candidate_sha,
            worktree_lock_id=WORKTREE_LOCK_ID,
            worktree_generation=WORKTREE_GENERATION,
            issued_at="2026-09-06T00:00:00Z",
        )
        explicit_id = overrides.pop("ticket_id", None)
        if overrides:
            ticket = replace(ticket, **overrides)
        # Derived last, from the final field values, so an override of (say)
        # the round produces a coherent ticket rather than a self-inconsistent
        # one -- unless the test is deliberately forging the id.
        ticket = replace(
            ticket,
            ticket_id=explicit_id
            if explicit_id is not None
            else derive_ticket_id(
                ticket.execution_id,
                ticket.candidate_sha,
                ticket.bundle_hash,
                ticket.gate_id,
                ticket.round,
            ),
        )
        return ticket

    def report(
        self,
        gate_id: str,
        round_: int = 1,
        result: str = "PASS",
        **overrides: Any,
    ) -> VerificationReportFixture:
        spec = self.bundle.checkers.get(gate_id) or checker_spec(gate_id)
        report = VerificationReportFixture(
            execution_id=self.execution.execution_id,
            task_id=self.task.task_id,
            ticket_id="",
            gate_id=gate_id,
            round=round_,
            candidate_sha=self.execution.candidate_sha,
            base_sha=self.execution.base_sha,
            bundle_hash=self.bundle.bundle_hash,
            deliverable_sha=self.task.deliverable_sha,
            checker_id=spec.checker_id,
            checker_version=spec.checker_version,
            checker_impl_digest=spec.checker_impl_digest,
            producer_identity=self.checker,
            evidence_source="checker_produced_replayable",
            result=result,
            environment_fingerprint=self.preflight.environment_fingerprint,
            worktree_head_before=self.execution.candidate_sha,
            worktree_head_after=self.execution.candidate_sha,
            governance_digest=self.bundle.governance_digest,
            # Declared in full by default, so a coverage-gap test has to
            # narrow it deliberately rather than benefit from an omission.
            coverage_dimensions=tuple(self.bundle.gate_required_dimensions.get(gate_id, ())),
            oracle_set_digest=oracle_digest(self.bundle, gate_id),
        )
        explicit_id = overrides.pop("ticket_id", None)
        if overrides:
            report = replace(report, **overrides)
        report = replace(
            report,
            ticket_id=explicit_id
            if explicit_id is not None
            else derive_ticket_id(
                report.execution_id,
                report.candidate_sha,
                report.bundle_hash,
                report.gate_id,
                report.round,
            ),
        )
        return report

    def tickets_for(self, *pairs: Tuple[str, int]) -> Tuple[VerificationTicket, ...]:
        return tuple(self.ticket(gate_id, round_) for gate_id, round_ in pairs)


def _preflight(**overrides: Any) -> PreflightFacts:
    facts = PreflightFacts(
        worktree_path="/scratch/isolated-worktree",
        worktree_head=CANDIDATE_SHA,
        ai_manager_home="/scratch/ephemeral-home",
        ai_manager_home_class="ephemeral",
        governance_digest_measured=GOVERNANCE_DIGEST,
        environment_fingerprint=ENV,
        worktree_lock_id=WORKTREE_LOCK_ID,
        worktree_generation=WORKTREE_GENERATION,
    )
    return replace(facts, **overrides) if overrides else facts


def _baseline(**overrides: Any) -> BaselineAttestation:
    baseline = BaselineAttestation(
        base_sha=BASE_SHA,
        environment_fingerprint=ENV,
        known_baseline_failures=(),
        attested_by_report_digest="baseline-report-digest-1",
    )
    return replace(baseline, **overrides) if overrides else baseline


# ---------------------------------------------------------------------------
# Fixture 1: FX-LEDGER-FREEZE-PANE
# The suite is green because it never asserted the frozen panes at all.
# ---------------------------------------------------------------------------


def fx_ledger_freeze_pane():
    golden = FrozenOracle(
        oracle_id="ORA-ART-STRUCT",
        role="golden",
        members=(("tests/golden/workbook-descriptor.json", "sha-golden-1"),),
    )
    common = dict(
        bundle_id="ab-ledger",
        bundle_version="1.4.0",
        governance_source_uri=GOVERNANCE_URI,
        governance_version=GOVERNANCE_VERSION,
        governance_digest=GOVERNANCE_DIGEST,
        gate_requirements_by_risk={
            "low": ("V0",),
            "medium": ("V0", "V1"),
            "high": ("V0", "V1", "V2"),
            "artifact_sensitive": ("V0", "V1", "V2", "V3"),
        },
        checkers={
            "V0": checker_spec("V0"),
            "V1": checker_spec("V1"),
            "V2": checker_spec("V2"),
            "V3": checker_spec("V3"),
        },
        frozen_oracles=(golden,),
        gate_oracle_refs={"V3": "ORA-ART-STRUCT"},
        risk_rules=(RiskRule("manager/artifact", "artifact_sensitive"),),
        component_escalation_rules=(
            ComponentRule(COMPONENT_PRODUCTION_CHECKOUT, "production"),
            ComponentRule(COMPONENT_PRODUCTION_HOME, "production"),
        ),
        impact_rules=(ImpactRule("manager/artifact", ("V1", "V2", "V3")),),
        impact_always_rerun=("V0",),
        baseline=_baseline(),
        budget=Budget(),
    )
    # Two distinct bundles, not one bundle mutated: a different clause set is a
    # different contract, and a content-addressed bundle makes that structural
    # rather than a matter of discipline.
    bundle_without_clause = finalize_bundle(gate_contract_clauses={}, **common)
    bundle_with_clause = finalize_bundle(
        gate_contract_clauses={"V3": ("freeze_pane_mismatch",)}, **common
    )

    def scenario(bundle: AcceptanceBundleFixture) -> Scenario:
        task = TaskFixture(
            task_id="T-LED-01",
            declared_risk="medium",
            deliverable_sha=CANDIDATE_SHA,
            acceptance_bundle_ref=bundle.bundle_id,
        )
        execution = ExecutionFixture(
            execution_id="E-LED-01",
            task_id=task.task_id,
            base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            diff_paths=("manager/artifact/workbook_writer.py",),
            status="completed",
            acceptance_bundle_ref=bundle.bundle_id,
            executor_identity=EXECUTOR,
        )
        return Scenario(task=task, execution=execution, bundle=bundle, preflight=_preflight())

    return {
        "bundle_without_clause": bundle_without_clause,
        "bundle_with_clause": bundle_with_clause,
        "scenario": scenario,
        "freeze_pane_failure": FailureObservation(
            signature="freeze_pane_mismatch",
            base_result="PASS",
        ),
    }


# ---------------------------------------------------------------------------
# Fixture 2: FX-OB-MOBILE-OVERFLOW
# Desktop DOM is green; the mobile viewport was never measured.
# ---------------------------------------------------------------------------


def fx_ob_mobile_overflow():
    bundle = finalize_bundle(
        bundle_id="ab-obsidian",
        bundle_version="1.0.0",
        governance_source_uri=GOVERNANCE_URI,
        governance_version=GOVERNANCE_VERSION,
        governance_digest=GOVERNANCE_DIGEST,
        gate_requirements_by_risk={
            "low": ("V0",),
            "medium": ("V0", "V1"),
            "high": ("V0", "V1", "V2"),
        },
        checkers={"V0": checker_spec("V0"), "V1": checker_spec("V1"), "V2": checker_spec("V2")},
        gate_required_dimensions={
            "V2": ("viewport:desktop-1440", "viewport:tablet-768", "viewport:mobile-375")
        },
        gate_contract_clauses={"V2": ("mobile_overflow",)},
        component_escalation_rules=(
            ComponentRule(COMPONENT_PRODUCTION_CHECKOUT, "production"),
            ComponentRule(COMPONENT_PRODUCTION_HOME, "production"),
        ),
        baseline=_baseline(),
        budget=Budget(),
    )
    task = TaskFixture(
        task_id="T-OB-01",
        declared_risk="high",
        deliverable_sha=CANDIDATE_SHA,
        acceptance_bundle_ref=bundle.bundle_id,
    )
    execution = ExecutionFixture(
        execution_id="E-OB-01",
        task_id=task.task_id,
        base_sha=BASE_SHA,
        candidate_sha=CANDIDATE_SHA,
        diff_paths=("dashboard/home.css",),
        status="completed",
        acceptance_bundle_ref=bundle.bundle_id,
        executor_identity=EXECUTOR,
    )
    scenario = Scenario(task=task, execution=execution, bundle=bundle, preflight=_preflight())
    return {
        "scenario": scenario,
        "overflow_failure": FailureObservation(
            signature="mobile_overflow",
            base_result="PASS",
        ),
    }


# ---------------------------------------------------------------------------
# Fixture 3: FX-ADM-FALSE-DISPATCH
# HTTP 200, a listening port, a generated prompt file and the executor's own
# tests_status -- none of which is evidence that a provider actually started.
# ---------------------------------------------------------------------------


def fx_adm_false_dispatch():
    common = dict(
        bundle_id="ab-adm",
        bundle_version="1.0.0",
        governance_source_uri=GOVERNANCE_URI,
        governance_version=GOVERNANCE_VERSION,
        governance_digest=GOVERNANCE_DIGEST,
        gate_requirements_by_risk={"low": ("V0",), "medium": ("V0", "V1")},
        checkers={
            "V0": checker_spec("V0"),
            "V1": checker_spec("V1", retryable=True, transient_code_allowlist=("IO_TIMEOUT",)),
        },
        risk_rules=(RiskRule("manager/execution_runner.py", "production"),),
        component_escalation_rules=(
            ComponentRule(COMPONENT_PRODUCTION_CHECKOUT, "production"),
            ComponentRule(COMPONENT_PRODUCTION_HOME, "production"),
        ),
        baseline=_baseline(),
        budget=Budget(),
    )
    bundle = finalize_bundle(**common)

    def _scenario(execution_id: str, diff_paths, preflight: Optional[PreflightFacts] = None):
        task = TaskFixture(
            task_id="T-ADM-01",
            declared_risk="medium",
            deliverable_sha=CANDIDATE_SHA,
            acceptance_bundle_ref=bundle.bundle_id,
        )
        execution = ExecutionFixture(
            execution_id=execution_id,
            task_id=task.task_id,
            base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            diff_paths=diff_paths,
            status="completed",
            acceptance_bundle_ref=bundle.bundle_id,
            executor_identity=EXECUTOR,
        )
        return Scenario(
            task=task,
            execution=execution,
            bundle=bundle,
            preflight=preflight or _preflight(),
        )

    ordinary = _scenario("E-ADM-01", ("manager/dispatch_helpers.py",))
    # The production variant keeps the SAME execution_id as its reports (the
    # B-1R residual): every report is admissible, so ROUTE_TO_PFP is proven by
    # production risk alone and not by an incidental lineage rejection.
    production = _scenario("E-ADM-01", ("manager/execution_runner.py",))

    return {
        "bundle": bundle,
        "ordinary": ordinary,
        "production": production,
        # Everything the executor submitted about itself, folded into one
        # untrusted evidence source.
        "self_reported_evidence_source": "executor_claim",
    }
