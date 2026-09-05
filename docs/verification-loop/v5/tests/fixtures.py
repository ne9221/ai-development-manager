"""Honest-path and attack world constructors for the v5 reference kernel.

These fixtures stand in for the controller/launcher API boundary. The envelope
helpers below are what a real launcher would inject; a candidate payload can
never produce one, which is what makes B1 closed rather than merely renamed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v5_kernel.kernel import (  # noqa: E402
    Event,
    EventEnvelope,
    Issuer,
    ReviewCapture,
    ReviewClaim,
    TrustRoot,
    World,
    controller_trust_digest,
    open_task,
    trusted_envelope,
)

DEFAULT_REVIEW_FILES = ("review/policy.md", "task/spec.md")
DEFAULT_CTX_DIGEST = "ctx-bound-1"
DEFAULT_INVOCATION = "rev-1"


def launcher_capture(**overrides):
    digest = controller_trust_digest()
    base = {
        "controller_src_sha256": digest,
        "authoritative": True,
        "oracle_expected": ["oracle.unit", "oracle.lint"],
        "allowed_review_files": DEFAULT_REVIEW_FILES,
        "captured_review_context_digest": DEFAULT_CTX_DIGEST,
        "open_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"},
        "oracle_lineage": {},
        "capture_ids": ("launcher-capture-1",),
        # B4: what the launcher actually observed the reviewer read.
        "review_captures": {
            DEFAULT_INVOCATION: {
                "context_digest": DEFAULT_CTX_DIGEST,
                "files_used": list(DEFAULT_REVIEW_FILES),
            }
        },
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


# --- controller / launcher API boundary (B1) ------------------------------


def launcher_env(world: World) -> EventEnvelope:
    return trusted_envelope(world, Issuer.LAUNCHER.value)


def controller_env(world: World) -> EventEnvelope:
    return trusted_envelope(world, Issuer.PINNED_CONTROLLER.value)


def human_env(world: World) -> EventEnvelope:
    return trusted_envelope(world, Issuer.HUMAN_OPERATOR.value)


def candidate_env(world: World) -> EventEnvelope:
    """A candidate can name itself, but naming LAUNCHER would not help it:
    `_event_source` rejects an unregistered capture id as well."""
    return EventEnvelope(
        event_source=Issuer.CANDIDATE_EXECUTOR.value,
        capture_id="candidate-made-this-up",
    )


def forged_env(source=Issuer.LAUNCHER.value) -> EventEnvelope:
    """An envelope whose capture id was never registered by the launcher."""
    return EventEnvelope(event_source=source, capture_id="never-registered")


def close_ok():
    return {
        "oracle_observed": ["oracle.unit", "oracle.lint"],
        "close_predicates": {"mechanical.tests": "PASS", "oracle.set": "PASS"},
    }


def review_claim(**overrides) -> ReviewClaim:
    data = dict(
        invocation_id=DEFAULT_INVOCATION,
        context_manifest_digest=DEFAULT_CTX_DIGEST,
        files_used=DEFAULT_REVIEW_FILES,
        findings=(),
        reviewer_identity="reviewer-a",
        completion_status="COMPLETE",
        launcher_capture_ref=DEFAULT_INVOCATION,
        verdict_text="APPROVE",
    )
    data.update(overrides)
    return ReviewClaim(**data)


def review_ok(**overrides):
    return {"claim": review_claim(**overrides)}


def review_capture(files_used=DEFAULT_REVIEW_FILES, context_digest=DEFAULT_CTX_DIGEST, invocation=DEFAULT_INVOCATION):
    """Launcher-side capture of one review invocation."""
    return {invocation: ReviewCapture(invocation_id=invocation, context_digest=context_digest, files_used=tuple(files_used))}


def mechanical_pass(**overrides):
    data = {"result": "PASS", "digest": "mech-1"}
    data.update(overrides)
    return data


def candidate_trust():
    return TrustRoot(
        captured_by=Issuer.CANDIDATE_EXECUTOR.value,
        controller_src_sha256="deadbeef",
        policy_id="vl-v5",
        policy_sha256="00",
        kernel_id="KERNEL_V5",
        authoritative=True,
    )
