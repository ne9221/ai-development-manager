"""Failure admission: which class a failure actually earns, on evidence.

Phase B-1 read a report-authored ``category`` field and returned it verbatim,
so a checker could relabel a regression as transient and collect a retry. B-1R
deleted that field. This module removes the last self-declared input --
``is_regression`` -- and derives every class from evidence instead. A report can
still *say* whatever it likes; ``proposed_class`` is carried through the model
and read nowhere, so tests can prove its irrelevance.

Two ordering decisions carry most of the weight:

**Concrete defects outrank transient retries.** Phase B-1R checked the
transient allowlist first, and the re-review recorded the consequence: a
signature listed both in the allowlist and in a contract clause yielded
RETRY_SAME_CANDIDATE. Clause membership and TEST_DEFECT admission are now
evaluated before any transient code is considered, so a real defect can never
be laundered into a retry by also carrying a transient code.

**The regression question is answered separately from the defect question.**
When base evidence is missing or the environment differs, we genuinely do not
know whether a failure is new. Saying "not a regression" would be a lie and
saying "regression" would be a fabrication, so the class records the defect
(CONTRACT_VIOLATION -- the oracle failed regardless) while the regression
determination records UNKNOWN. This is why REGRESSION is only ever admitted
with a proven base PASS at the same base_sha in an equivalent environment.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .models import (
    AcceptanceBundleFixture,
    ExecutionFixture,
    FailureObservation,
    VerificationReportFixture,
)
from .paths import canonical_repo_path

REGRESSION = "REGRESSION"
NOT_REGRESSION = "NOT_REGRESSION"
UNKNOWN = "UNKNOWN"


def base_evidence(
    observation: FailureObservation,
    execution: ExecutionFixture,
    bundle: AcceptanceBundleFixture,
    report: VerificationReportFixture,
) -> Tuple[str, str]:
    """What this failure did at base_sha: ``(result, reason)``.

    Result is "PASS", "FAIL" or "UNKNOWN". Every path to a non-UNKNOWN answer
    requires the comparison to be *sound*: the attestation must be for this
    exact base_sha, and the environment fingerprints must match. The path
    length lives in that fingerprint because differing path lengths have
    genuinely manufactured phantom failures in this repo's installer tests.
    """
    baseline = bundle.baseline
    if baseline is None:
        return UNKNOWN, "NO_BASELINE_ATTESTATION"
    if baseline.base_sha != execution.base_sha:
        return UNKNOWN, "BASELINE_SHA_MISMATCH"
    if report.environment_fingerprint != baseline.environment_fingerprint:
        return UNKNOWN, "ENVIRONMENT_NOT_EQUIVALENT"

    # A base run performed in this very round is the strongest evidence.
    if observation.base_result in ("PASS", "FAIL"):
        return observation.base_result, "MEASURED_THIS_ROUND"

    if observation.signature in baseline.known_baseline_failures:
        return "FAIL", "ATTESTED_KNOWN_BASELINE_FAILURE"

    if baseline.attested_by_report_digest:
        # The attestation covers a whole run at this base_sha, so absence from
        # its failure list means the test passed there. Without an attesting
        # report, absence proves nothing -- it could simply never have run.
        return "PASS", "ATTESTED_ABSENT_FROM_BASELINE_FAILURES"

    return UNKNOWN, "BASELINE_NOT_ATTESTED"


def _touches_frozen_oracle(
    observation: FailureObservation, bundle: AcceptanceBundleFixture
) -> bool:
    """Whether the proposed test-only repair would edit a frozen oracle.

    Phase A v3 B3: a TEST_DEFECT repair that touches a golden, snapshot or
    referenced test file is forcibly reclassified. Otherwise "this test is
    wrong" becomes a licence to edit the very artefact the contract is
    anchored to -- the oracle would follow the code instead of judging it.
    """
    frozen = {
        canonical_repo_path(path)
        for path in bundle.frozen_oracle_paths()
    }
    frozen.discard(None)
    for raw in observation.repair_paths:
        candidate = canonical_repo_path(raw)
        if candidate is None:
            # An unreadable repair path cannot be proven safe, so it is treated
            # as touching the oracle rather than as touching nothing.
            return True
        if candidate in frozen:
            return True
    return False


def _test_defect_admitted(
    observation: FailureObservation,
    bundle: AcceptanceBundleFixture,
    base_result: str,
) -> bool:
    """TD-1 or TD-2, per Phase A v3 D.1 C5.

    TD-1: the same test also fails at base with the same normalised signature,
    in an equivalent environment -- so the candidate did not break it.
    TD-2: a complete, reproducible proof that the test's asserted predicate
    contradicts a clause the frozen contract actually contains.

    A bare claim satisfies neither.
    """
    if base_result == "FAIL":
        baseline = bundle.baseline
        same_signature = observation.base_signature == observation.signature
        attested = bool(
            baseline is not None
            and observation.signature in baseline.known_baseline_failures
        )
        if same_signature or attested:
            return True

    proof = observation.contradiction_proof
    if proof is not None and proof.is_complete:
        # The clause must exist in this bundle; a proof against an invented
        # clause id proves nothing about this contract.
        for clauses in bundle.gate_contract_clauses.values():
            if proof.clause_id in clauses:
                return True
    return False


def _transient_admitted(
    observation: FailureObservation, gate_id: str, bundle: AcceptanceBundleFixture
) -> bool:
    """Phase A v3 C6: checker-issued code, on this gate's allowlist, gate retryable.

    All three are required. Membership of a *signature* in a bundle list -- the
    Phase B-1R approximation -- is not enough, because a signature is text the
    failure happens to contain, whereas a transient code is something the
    checker had to decide to emit.
    """
    code = observation.transient_code
    if not isinstance(code, str) or not code.strip():
        return False
    checker = bundle.checkers.get(gate_id)
    if checker is None or not checker.retryable:
        return False
    return code in checker.transient_code_allowlist


def classify_observation(
    observation: FailureObservation,
    gate_id: str,
    bundle: AcceptanceBundleFixture,
    execution: ExecutionFixture,
    report: VerificationReportFixture,
) -> Tuple[str, str]:
    """``(admitted_failure_class, regression_determination)``.

    Evaluated in a fixed order so the outcome is deterministic and so the
    precedence is legible in the source rather than emergent:

    1. clause-matched failure -> a real contract defect (REGRESSION when base
       is proven to have passed, else CONTRACT_VIOLATION)
    2. TD-1/TD-2 proven -> TEST_DEFECT, unless the repair would touch a frozen
       oracle, in which case ACCEPTANCE_CONTRACT_DEFECT
    3. checker-issued allowlisted transient code -> ENVIRONMENT_TRANSIENT
    4. anything else -> ACCEPTANCE_CONTRACT_DEFECT (the contract has no clause
       covering a reproducible failure, which is a gap in the contract, not a
       reason to proceed)
    """
    base_result, _reason = base_evidence(observation, execution, bundle, report)

    clauses = bundle.gate_contract_clauses.get(gate_id, ())
    if observation.signature in clauses:
        if base_result == "PASS":
            return "REGRESSION", REGRESSION
        if base_result == "FAIL":
            return "CONTRACT_VIOLATION", NOT_REGRESSION
        return "CONTRACT_VIOLATION", UNKNOWN

    if _test_defect_admitted(observation, bundle, base_result):
        if _touches_frozen_oracle(observation, bundle):
            return "ACCEPTANCE_CONTRACT_DEFECT", (
                NOT_REGRESSION if base_result == "FAIL" else UNKNOWN
            )
        return "TEST_DEFECT", NOT_REGRESSION if base_result == "FAIL" else UNKNOWN

    if _transient_admitted(observation, gate_id, bundle):
        return "ENVIRONMENT_TRANSIENT", UNKNOWN

    if base_result == "PASS":
        # Proven new failure with no clause covering it: the contract is
        # incomplete *and* something regressed. The class names the contract
        # gap (that is what needs a human), the determination records the
        # regression so it is not lost.
        return "ACCEPTANCE_CONTRACT_DEFECT", REGRESSION
    return "ACCEPTANCE_CONTRACT_DEFECT", UNKNOWN if base_result == UNKNOWN else NOT_REGRESSION
