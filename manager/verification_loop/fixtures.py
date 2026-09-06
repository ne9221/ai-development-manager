"""The three required false-complete fixtures (Phase B-1 spec section F).

Each builder returns a dict of named scenario ingredients rather than a
single case, so a test can combine them into the "before" and "after"
moments the spec calls out (e.g. V3 missing vs. V3 failing vs. the contract
having no clause for the defect yet).

Nothing here talks to a real provider, Excel, Drive, GitHub, screenshot or
Session Center -- every one of those surfaces is represented purely as
fixture data (evidence_source, identity_resolution, coverage_dimensions).
"""

from __future__ import annotations

from .models import (
    AcceptanceBundleFixture,
    ExecutionFixture,
    FailureObservation,
    RiskRule,
    TaskFixture,
    VerificationReportFixture,
)


def _pass_report(execution_id, gate_id, bundle, round_=1, dimensions=()):
    identity, version = bundle.checker_identities[gate_id]
    return VerificationReportFixture(
        execution_id=execution_id,
        gate_id=gate_id,
        round=round_,
        candidate_sha="sha-candidate-1",
        base_sha="sha-base-1",
        bundle_hash=bundle.bundle_hash,
        checker_identity=identity,
        checker_version=version,
        evidence_source="independent_check",
        identity_resolution="resolved",
        result="PASS",
        coverage_dimensions=dimensions,
    )


# ---------------------------------------------------------------------------
# Fixture 1: FX-LEDGER-FREEZE-PANE
# ---------------------------------------------------------------------------

def fx_ledger_freeze_pane():
    task = TaskFixture(
        task_id="ledger-freeze-pane",
        declared_risk="medium",
        deliverable_sha="sha-candidate-1",
        acceptance_bundle_ref="bundle-freeze-pane",
    )
    execution = ExecutionFixture(
        execution_id="exec-freeze-pane-1",
        task_id=task.task_id,
        base_sha="sha-base-1",
        candidate_sha="sha-candidate-1",
        diff_paths=("tools/xlsx_template_render.py",),
        status="completed",
        executor_identity="codex-impl",
    )
    checker_identities = {
        "V0": ("unit_checker", "1.0"),
        "V1": ("lint_checker", "1.0"),
        "V2": ("integration_checker", "1.0"),
        "V3": ("excel_visual_checker", "1.0"),
    }
    common = dict(
        governance_digest="gov-digest-1",
        gate_requirements_by_risk={
            "low": ("V0",),
            "medium": ("V0", "V1"),
            "high": ("V0", "V1", "V2"),
            "artifact_sensitive": ("V0", "V1", "V2", "V3"),
        },
        checker_identities=checker_identities,
        risk_rules=(RiskRule("tools/xlsx_template_render.py", "artifact_sensitive"),),
    )
    # Distinct hashes: a Frozen Acceptance Bundle is content-addressed, so a
    # different contract-clause set (the whole point of OPEN_BUNDLE_REVISION)
    # is a different bundle, never the same hash with silently different
    # contents.
    bundle_without_clause = AcceptanceBundleFixture(
        bundle_hash="bundle-freeze-pane-hash-rev1-no-clause", gate_contract_clauses={}, **common
    )
    bundle_with_clause = AcceptanceBundleFixture(
        bundle_hash="bundle-freeze-pane-hash-rev2-with-clause",
        gate_contract_clauses={"V3": ("freeze_pane_mismatch",)}, **common
    )

    def reports_v0_v1_v2_pass(bundle):
        return [
            _pass_report(execution.execution_id, "V0", bundle),
            _pass_report(execution.execution_id, "V1", bundle),
            _pass_report(execution.execution_id, "V2", bundle),
        ]

    def v3_fail_report(bundle):
        identity, version = bundle.checker_identities["V3"]
        return VerificationReportFixture(
            execution_id=execution.execution_id,
            gate_id="V3",
            round=1,
            candidate_sha="sha-candidate-1",
            base_sha="sha-base-1",
            bundle_hash=bundle.bundle_hash,
            checker_identity=identity,
            checker_version=version,
            evidence_source="independent_render",
            identity_resolution="resolved",
            result="FAIL",
            failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
        )

    return {
        "task": task,
        "execution": execution,
        "bundle_without_clause": bundle_without_clause,
        "bundle_with_clause": bundle_with_clause,
        "reports_v0_v1_v2_pass": reports_v0_v1_v2_pass,
        "v3_fail_report": v3_fail_report,
    }


# ---------------------------------------------------------------------------
# Fixture 2: FX-OB-MOBILE-OVERFLOW
# ---------------------------------------------------------------------------

def fx_ob_mobile_overflow():
    task = TaskFixture(
        task_id="ob-mobile-overflow",
        declared_risk="high",
        deliverable_sha="sha-candidate-1",
        acceptance_bundle_ref="bundle-mobile-overflow",
    )
    execution = ExecutionFixture(
        execution_id="exec-mobile-overflow-1",
        task_id=task.task_id,
        base_sha="sha-base-1",
        candidate_sha="sha-candidate-1",
        diff_paths=("Dashboard/dashboard.css",),
        status="completed",
        executor_identity="codex-impl",
    )
    checker_identities = {
        "V0": ("unit_checker", "1.0"),
        "V1": ("lint_checker", "1.0"),
        "V2": ("responsive_visual_checker", "1.0"),
    }
    bundle = AcceptanceBundleFixture(
        bundle_hash="bundle-mobile-overflow-hash-1",
        governance_digest="gov-digest-1",
        gate_requirements_by_risk={
            "low": ("V0",),
            "medium": ("V0", "V1"),
            "high": ("V0", "V1", "V2"),
        },
        checker_identities=checker_identities,
        gate_required_dimensions={"V2": ("desktop", "mobile")},
        gate_contract_clauses={"V2": ("mobile_overflow",)},
    )

    reports_v0_v1_pass = [
        _pass_report(execution.execution_id, "V0", bundle),
        _pass_report(execution.execution_id, "V1", bundle),
    ]

    v2_pass_desktop_only = VerificationReportFixture(
        execution_id=execution.execution_id,
        gate_id="V2",
        round=1,
        candidate_sha="sha-candidate-1",
        base_sha="sha-base-1",
        bundle_hash=bundle.bundle_hash,
        checker_identity="responsive_visual_checker",
        checker_version="1.0",
        evidence_source="independent_render",
        identity_resolution="resolved",
        result="PASS",
        coverage_dimensions=("desktop",),
    )

    def v2_report_full_coverage(result, failure_observations=()):
        return VerificationReportFixture(
            execution_id=execution.execution_id,
            gate_id="V2",
            round=2,
            candidate_sha="sha-candidate-1",
            base_sha="sha-base-1",
            bundle_hash=bundle.bundle_hash,
            checker_identity="responsive_visual_checker",
            checker_version="1.0",
            evidence_source="independent_render",
            identity_resolution="resolved",
            result=result,
            coverage_dimensions=("desktop", "mobile"),
            failure_observations=failure_observations,
        )

    return {
        "task": task,
        "execution": execution,
        "bundle": bundle,
        "reports_v0_v1_pass": reports_v0_v1_pass,
        "v2_pass_desktop_only": v2_pass_desktop_only,
        "v2_report_full_coverage": v2_report_full_coverage,
    }


# ---------------------------------------------------------------------------
# Fixture 3: FX-ADM-FALSE-DISPATCH
# ---------------------------------------------------------------------------

def fx_adm_false_dispatch():
    task = TaskFixture(
        task_id="adm-false-dispatch",
        declared_risk="medium",
        deliverable_sha="sha-candidate-1",
        acceptance_bundle_ref="bundle-false-dispatch",
    )
    execution_non_production = ExecutionFixture(
        execution_id="exec-false-dispatch-1",
        task_id=task.task_id,
        base_sha="sha-base-1",
        candidate_sha="sha-candidate-1",
        diff_paths=("manager/dispatch_helpers.py",),
        status="completed",
        executor_identity="codex-impl",
    )
    execution_production = ExecutionFixture(
        execution_id="exec-false-dispatch-2",
        task_id=task.task_id,
        base_sha="sha-base-1",
        candidate_sha="sha-candidate-1",
        diff_paths=("manager/execution_runner.py",),
        status="completed",
        executor_identity="codex-impl",
    )
    checker_identities = {
        "V0": ("unit_checker", "1.0"),
        "V1": ("session_center_checker", "1.0"),
    }
    bundle = AcceptanceBundleFixture(
        bundle_hash="bundle-false-dispatch-hash-1",
        governance_digest="gov-digest-1",
        gate_requirements_by_risk={
            "low": ("V0",),
            "medium": ("V0", "V1"),
        },
        checker_identities=checker_identities,
        risk_rules=(RiskRule("manager/execution_runner.py", "production"),),
    )

    report_v0_pass = _pass_report(execution_non_production.execution_id, "V0", bundle)

    # The executor's own signals (HTTP 200, "port is listening", "prompt was
    # generated", its own Handoff.tests_status=passed) are folded into a
    # single self-reported claim: evidence_source is NOT one of
    # TRUSTED_EVIDENCE_SOURCES, and Session Center correlation could not
    # resolve identity (needs_review), so none of it counts as evidence.
    report_v1_false_pass = VerificationReportFixture(
        execution_id=execution_non_production.execution_id,
        gate_id="V1",
        round=1,
        candidate_sha="sha-candidate-1",
        base_sha="sha-base-1",
        bundle_hash=bundle.bundle_hash,
        checker_identity="session_center_checker",
        checker_version="1.0",
        evidence_source="executor_claim",
        identity_resolution="needs_review",
        result="PASS",
    )

    return {
        "task": task,
        "execution_non_production": execution_non_production,
        "execution_production": execution_production,
        "bundle": bundle,
        "report_v0_pass": report_v0_pass,
        "report_v1_false_pass": report_v1_false_pass,
    }
