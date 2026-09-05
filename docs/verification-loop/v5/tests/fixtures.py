"""Honest-path and attack world constructors for the v5 reference kernel."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v5_kernel.kernel import (  # noqa: E402
    Event,
    Issuer,
    ReviewClaim,
    TrustRoot,
    World,
    controller_trust_digest,
    open_task,
)


def launcher_capture(**overrides):
    digest = controller_trust_digest()
    base = {
        "controller_src_sha256": digest,
        "authoritative": True,
        "oracle_expected": ["oracle.unit", "oracle.lint"],
        "allowed_review_files": ("review/policy.md", "task/spec.md"),
        "captured_review_context_digest": "ctx-bound-1",
        "open_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"},
        "oracle_lineage": {},
    }
    base.update(overrides)
    return base


def policy(risk="LOW", **overrides):
    base = {
        "policy_id": "vl-v5",
        "risk": risk,
        "oracle_expected": ["oracle.unit", "oracle.lint"],
    }
    base.update(overrides)
    return base


def honest_open(risk="LOW") -> World:
    return open_task(policy(risk=risk), launcher_capture())


def close_ok():
    return {
        "oracle_observed": ["oracle.unit", "oracle.lint"],
        "close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"},
    }


def review_ok(**overrides):
    data = dict(
        invocation_id="rev-1",
        context_digest="ctx-bound-1",
        files_used=("review/policy.md", "task/spec.md"),
        verdict_text="APPROVE",
        findings=(),
    )
    data.update(overrides)
    return {"claim": ReviewClaim(**data)}


def mechanical_pass(attester=Issuer.LAUNCHER.value):
    return {"attester": attester, "result": "PASS", "digest": "mech-1"}


def candidate_trust():
    return TrustRoot(
        captured_by=Issuer.CANDIDATE_EXECUTOR.value,
        controller_src_sha256="deadbeef",
        policy_id="vl-v5",
        policy_sha256="00",
        kernel_id="KERNEL_V5",
        authoritative=True,
    )
