#!/usr/bin/env python3
"""Canonical Baseline Promotion Guard.

Read-only verification gate for the project's accepted canonical model
(single-trunk, fast-forward-only canonical baseline):

  1. `origin/main` (the branch registered in the Global Project Registry as
     this project's canonical/default branch, or the strategy-resolved
     canonical authority for `pinned_commit` projects) is the canonical
     source baseline.
  2. Before/during a formal production promotion, `origin/main` must be
     fast-forwarded to the exact tested promotion SHA -- never force-updated,
     never merge-committed just to promote.
  3. Formal/production must never silently run ahead of canonical main.
  4. Activation may proceed only once canonical main, the formal remote, the
     TESTED evidence, and the promotion TARGET are all the same commit.

This module never performs a git ref mutation of any kind (no push, no
force-push, no merge commit, no branch move) -- it only reads three already
zero-caller-trust-required facts (canonical main HEAD, formal HEAD, and the
ancestry relationship between two commits) and combines them with the
already-captured TESTED evidence (manager.provenance) to answer one
question: is a promotion to TARGET currently a safe, contract-compliant
fast-forward, and is activation against TARGET currently allowed?

Reused, not duplicated:
  - manager.project_registry.ProjectRegistry / get_global_registry -- the one
    Global Project Registry; this module never maintains a second one.
  - manager.remote_baseline_resolver's GitHub token/env-var convention and
    BASELINE_HEAD_PATTERN commit-id shape -- reused directly rather than
    redefined.
  - manager.provenance.read_tested_sha -- the one place TESTED evidence is
    written/read; this module never re-implements that file format.

Every remote read (branch/ref HEAD, ancestry comparison) is an injectable
callable, exactly like remote_baseline_resolver's `github_fetch` -- so this
gate is fully testable with in-memory fakes and never requires a live
network call or a local git checkout to test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests

from manager.project_registry import (
    ProjectMetadata,
    ProjectRegistry,
    ProjectRegistryError,
    get_global_registry,
)
from manager.remote_baseline_resolver import (
    BASELINE_HEAD_PATTERN,
    GITHUB_TOKEN_ENV_VAR,
)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_TIMEOUT_SECONDS = 15

SUPPORTED_STRATEGIES = ("origin_default", "pinned_commit")

# Overall gate status values.
#
# UNKNOWN means "the truth could not be established" -- a remote read
# failure, an unresolvable/disabled project, missing or malformed TESTED
# evidence, or any other input the gate cannot independently verify. It is
# deliberately distinct from both PASS ("checked and confirmed safe") and
# FAIL ("checked and confirmed unsafe/non-fast-forward/mismatched"): an
# unverifiable promotion is neither of those, and must never be treated as
# either by a caller that only checks `activation_allowed`.
STATUS_PASS = "PASS"
STATUS_CONVERGENCE_REQUIRED = "CONVERGENCE_REQUIRED"
STATUS_FAIL = "FAIL"
STATUS_UNKNOWN = "UNKNOWN"


class CanonicalBaselineGuardError(Exception):
    """Raised only for malformed *inputs* to the gate itself (e.g. a
    caller-supplied target_sha that isn't a valid commit id). Every
    *remote-truth* failure (unreadable branch, diverged history, strategy
    mismatch, etc.) is instead reported as a FAIL/UNKNOWN result -- it is
    not an exception -- because a promotion workflow needs a bounded,
    inspectable result to log and act on, not a stack trace."""


def default_ref_sha_reader(owner: str, name: str, ref: str, *, token: Optional[str] = None) -> str:
    """Read-only: resolve `ref` (a branch name or a commit id) to its
    current commit SHA via `GET /repos/{owner}/{repo}/commits/{ref}`.

    Fails closed (raises CanonicalBaselineGuardError) on any transport
    error, non-200 status, or malformed body -- never returns a guessed or
    stale SHA. This performs no mutation of any kind.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/commits/{ref}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise CanonicalBaselineGuardError(
            f"GitHub API request failed for {owner}/{name}@{ref}: {exc}"
        ) from exc

    if response.status_code != 200:
        raise CanonicalBaselineGuardError(
            f"GitHub API returned HTTP {response.status_code} for {owner}/{name}@{ref}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise CanonicalBaselineGuardError(
            f"GitHub API returned an unparseable response body for {owner}/{name}@{ref}"
        ) from exc

    sha = body.get("sha") if isinstance(body, dict) else None
    if not isinstance(sha, str) or not BASELINE_HEAD_PATTERN.match(sha):
        raise CanonicalBaselineGuardError(
            f"GitHub API returned a malformed or missing commit sha for {owner}/{name}@{ref}: {sha!r}"
        )
    return sha


def default_compare_reader(owner: str, name: str, base: str, head: str, *, token: Optional[str] = None) -> str:
    """Read-only ancestry comparison via
    `GET /repos/{owner}/{repo}/compare/{base}...{head}`.

    Returns GitHub's own `status` field verbatim: one of "identical",
    "ahead" (head is a strict descendant of base -- base -> head is a pure
    fast-forward), "behind" (head is a strict ancestor of base), or
    "diverged" (neither is an ancestor of the other). Fails closed on any
    transport error, non-200 status, malformed body, or unrecognized status
    value. Performs no mutation of any kind.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/compare/{base}...{head}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise CanonicalBaselineGuardError(
            f"GitHub compare API request failed for {owner}/{name} {base}...{head}: {exc}"
        ) from exc

    if response.status_code != 200:
        raise CanonicalBaselineGuardError(
            f"GitHub compare API returned HTTP {response.status_code} for {owner}/{name} {base}...{head}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise CanonicalBaselineGuardError(
            f"GitHub compare API returned an unparseable response body for {owner}/{name} {base}...{head}"
        ) from exc

    status = body.get("status") if isinstance(body, dict) else None
    if status not in ("identical", "ahead", "behind", "diverged"):
        raise CanonicalBaselineGuardError(
            f"GitHub compare API returned an unrecognized status for {owner}/{name} {base}...{head}: {status!r}"
        )
    return status


@dataclass(frozen=True)
class PromotionGateResult:
    target_sha: Optional[str]
    canonical_main_sha: Optional[str]
    formal_sha: Optional[str]
    tested_sha: Optional[str]
    strategy: str
    fast_forward_possible: bool
    canonical_convergence_required: bool
    formal_convergence_required: bool
    activation_allowed: bool
    status: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_sha": self.target_sha,
            "canonical_main_sha": self.canonical_main_sha,
            "formal_sha": self.formal_sha,
            "tested_sha": self.tested_sha,
            "strategy": self.strategy,
            "fast_forward_possible": self.fast_forward_possible,
            "canonical_convergence_required": self.canonical_convergence_required,
            "formal_convergence_required": self.formal_convergence_required,
            "activation_allowed": self.activation_allowed,
            "status": self.status,
            "reason": self.reason,
        }


def _unknown(strategy: str, reason: str, *, target_sha: Optional[str] = None,
             canonical_main_sha: Optional[str] = None, formal_sha: Optional[str] = None,
             tested_sha: Optional[str] = None) -> PromotionGateResult:
    return PromotionGateResult(
        target_sha=target_sha,
        canonical_main_sha=canonical_main_sha,
        formal_sha=formal_sha,
        tested_sha=tested_sha,
        strategy=strategy,
        fast_forward_possible=False,
        canonical_convergence_required=False,
        formal_convergence_required=False,
        activation_allowed=False,
        status=STATUS_UNKNOWN,
        reason=reason,
    )


def _fail(strategy: str, reason: str, *, target_sha: Optional[str] = None,
          canonical_main_sha: Optional[str] = None, formal_sha: Optional[str] = None,
          tested_sha: Optional[str] = None) -> PromotionGateResult:
    return PromotionGateResult(
        target_sha=target_sha,
        canonical_main_sha=canonical_main_sha,
        formal_sha=formal_sha,
        tested_sha=tested_sha,
        strategy=strategy,
        fast_forward_possible=False,
        canonical_convergence_required=False,
        formal_convergence_required=False,
        activation_allowed=False,
        status=STATUS_FAIL,
        reason=reason,
    )


def _relationship(base: str, head: str, compare_reader: Callable[..., str],
                   owner: str, name: str, token: Optional[str]) -> str:
    """Return "identical", "ahead", "behind", or "diverged" describing
    `head` relative to `base`, short-circuiting the remote comparison call
    entirely when the two SHAs are textually identical (no ambiguity, and
    one fewer remote read)."""
    if base == head:
        return "identical"
    return compare_reader(owner, name, base, head, token=token)


def evaluate_promotion_gate(
    project_reference: str,
    target_sha: str,
    *,
    formal_branch: str,
    registry: Optional[ProjectRegistry] = None,
    ref_sha_reader: Callable[..., str] = default_ref_sha_reader,
    compare_reader: Callable[..., str] = default_compare_reader,
    tested_sha: Optional[str] = None,
    tested_sha_reader: Optional[Callable[[], Optional[str]]] = None,
    canonical_convergence_phase: bool = False,
    github_token: Optional[str] = None,
) -> PromotionGateResult:
    """Independently establish TARGET_SHA, CANONICAL_MAIN_SHA, FORMAL_SHA,
    and TESTED_SHA for `project_reference`, and evaluate the single-trunk
    fast-forward canonical model's allowed transitions against them.

    This performs no ref mutation whatsoever -- `ref_sha_reader` and
    `compare_reader` are both read-only remote lookups (real implementations
    default to a plain `GET`; tests inject in-memory fakes so nothing here
    ever needs live network access or a local git checkout).

    `formal_branch` is REQUIRED and has no default -- there is no single
    branch name this project's formal/production authority can safely be
    assumed to be (it has already changed once: `formal/production` is
    stale, the current one is `integration/adm-runtime-repowrite-v2-20260822`,
    and it will change again). The caller -- a trusted internal promotion
    workflow -- must supply this from its own trusted promotion
    configuration; it must never be sourced from external/dispatch-caller
    input, and this gate refuses to guess or silently default it.

    `canonical_convergence_phase=True` only relaxes "activation NOT yet
    allowed" reporting nuance for a workflow explicitly announcing it is
    mid fast-forward-convergence; it never causes this gate to *report* an
    unsafe transition as safe, and it never performs the convergence -- a
    caller still has to do that fast-forward itself via a separate,
    explicitly-mutating code path this module does not contain.
    """
    if not isinstance(target_sha, str) or not BASELINE_HEAD_PATTERN.match(target_sha):
        raise CanonicalBaselineGuardError(f"target_sha must be a valid commit id, got {target_sha!r}")

    if not isinstance(formal_branch, str) or not formal_branch.strip():
        raise CanonicalBaselineGuardError(
            "formal_branch must be a non-empty branch identity supplied by trusted internal promotion "
            "configuration; this gate never guesses or defaults a formal/production branch name"
        )
    formal_branch = formal_branch.strip()

    registry = registry or get_global_registry()
    try:
        project: ProjectMetadata = registry.get_project(project_reference)
    except ProjectRegistryError as exc:
        return _unknown("unresolved", f"project could not be resolved from the Global Project Registry: {exc}",
                         target_sha=target_sha)

    repo = project.repo if isinstance(project.repo, dict) else None
    owner = repo.get("owner") if repo else None
    name = repo.get("name") if repo else None
    if not isinstance(owner, str) or not owner.strip() or not isinstance(name, str) or not name.strip():
        return _unknown("unresolved", f"project {project.project_id!r} has no registered owner/name repository identity",
                         target_sha=target_sha)
    owner, name = owner.strip(), name.strip()

    if github_token is None:
        github_token = os.environ.get(GITHUB_TOKEN_ENV_VAR) or None

    policy = project.baseline_resolution_policy if isinstance(project.baseline_resolution_policy, dict) else {}
    strategy = policy.get("strategy")
    pinned_ref = policy.get("pinned_ref")

    if strategy not in SUPPORTED_STRATEGIES:
        return _fail(f"unsupported:{strategy!r}",
                     f"project {project.project_id!r} declares baseline_resolution_policy.strategy={strategy!r}, "
                     f"which this guard does not recognize; refusing to guess a canonical authority",
                     target_sha=target_sha)

    if strategy == "origin_default":
        if pinned_ref:
            return _fail(
                "origin_default",
                f"project {project.project_id!r} declares strategy='origin_default' but also sets a "
                f"pinned_ref={pinned_ref!r}; this is a contradictory registry configuration and must not be "
                "silently resolved either way",
                target_sha=target_sha,
            )
        canonical_branch = project.default_branch
        if not isinstance(canonical_branch, str) or not canonical_branch.strip():
            return _unknown("origin_default",
                             f"project {project.project_id!r} has no resolvable canonical branch",
                             target_sha=target_sha)
        canonical_branch = canonical_branch.strip()
    else:  # pinned_commit
        if not isinstance(pinned_ref, str) or not BASELINE_HEAD_PATTERN.match(pinned_ref):
            return _fail(
                "pinned_commit",
                f"project {project.project_id!r} declares strategy='pinned_commit' but pinned_ref={pinned_ref!r} "
                "is not a valid commit id",
                target_sha=target_sha,
            )
        # Rule44 R5.1 contract: `pinned_commit` names one exact, immutable
        # commit -- it is NOT a moving ref that TARGET can fast-forward
        # past. There is no such thing as "converging" a pinned commit:
        # either TARGET is that exact commit, or the promotion is invalid,
        # full stop -- regardless of whether TARGET happens to be a
        # descendant, an ancestor, or diverged from the pin. This check is
        # a pure string comparison against an already-known-good value, so
        # it is decided before any remote call of any kind (no ref read,
        # no ancestry compare) -- an immutable pin needs no network access
        # to be judged as mismatched.
        if target_sha != pinned_ref:
            return _fail(
                "pinned_commit",
                f"project {project.project_id!r} strategy='pinned_commit' pins {pinned_ref}, which is an exact "
                f"immutable commit, not a movable branch; TARGET ({target_sha}) does not equal the pinned commit "
                "-- there is no fast-forward/convergence operation for a pinned commit, this is a hard mismatch",
                target_sha=target_sha,
            )
        canonical_branch = pinned_ref

    # --- Independently read the four facts. Any transport failure here is
    # UNKNOWN, never FAIL and never a guessed value. ---
    try:
        canonical_main_sha = ref_sha_reader(owner, name, canonical_branch, token=github_token)
    except CanonicalBaselineGuardError as exc:
        return _unknown(strategy, f"could not read canonical branch {canonical_branch!r}: {exc}",
                         target_sha=target_sha)

    try:
        formal_sha = ref_sha_reader(owner, name, formal_branch, token=github_token)
    except CanonicalBaselineGuardError as exc:
        return _unknown(strategy, f"could not read formal branch {formal_branch!r}: {exc}",
                         target_sha=target_sha, canonical_main_sha=canonical_main_sha)

    if tested_sha is None and tested_sha_reader is not None:
        tested_sha = tested_sha_reader()

    if tested_sha is not None and (not isinstance(tested_sha, str) or not BASELINE_HEAD_PATTERN.match(tested_sha)):
        return _unknown(strategy, f"TESTED evidence is present but malformed: {tested_sha!r}",
                         target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha)

    if tested_sha is None:
        return _unknown(strategy, "no TESTED evidence is available; activation cannot be authorized",
                         target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha)

    if tested_sha != target_sha:
        return _fail(strategy, f"TESTED evidence ({tested_sha}) does not match TARGET ({target_sha})",
                     target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha,
                     tested_sha=tested_sha)

    # --- Canonical main vs target ---
    try:
        canonical_relation = _relationship(canonical_main_sha, target_sha, compare_reader, owner, name, github_token)
    except CanonicalBaselineGuardError as exc:
        return _unknown(strategy, f"could not compare canonical main to target: {exc}",
                         target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha,
                         tested_sha=tested_sha)

    if canonical_relation == "diverged":
        return _fail(strategy, f"canonical branch {canonical_branch!r} has diverged from TARGET; not a fast-forward",
                     target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha,
                     tested_sha=tested_sha)
    if canonical_relation == "behind":
        return _fail(strategy, f"canonical branch {canonical_branch!r} is ahead of TARGET; main must never run "
                                "ahead of the tested promotion SHA",
                     target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha,
                     tested_sha=tested_sha)
    canonical_convergence_required = canonical_relation == "ahead"  # target is a strict descendant of main

    # --- Formal vs target ---
    try:
        formal_relation = _relationship(formal_sha, target_sha, compare_reader, owner, name, github_token)
    except CanonicalBaselineGuardError as exc:
        return _unknown(strategy, f"could not compare formal branch to target: {exc}",
                         target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha,
                         tested_sha=tested_sha)

    if formal_relation == "diverged":
        return _fail(strategy, f"formal branch {formal_branch!r} has diverged from TARGET; not a fast-forward",
                     target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha,
                     tested_sha=tested_sha)
    if formal_relation == "behind":
        return _fail(strategy, f"formal branch {formal_branch!r} is ahead of TARGET; formal must never run ahead "
                                "of canonical main",
                     target_sha=target_sha, canonical_main_sha=canonical_main_sha, formal_sha=formal_sha,
                     tested_sha=tested_sha)
    formal_convergence_required = formal_relation == "ahead"  # target is a strict descendant of formal

    # Both relations were already filtered above to only ever be
    # "identical" or "ahead" at this point (both "diverged" and "behind"
    # returned a FAIL result earlier) -- so reaching here always means a
    # pure fast-forward (or no move at all) is possible on both fronts.
    fast_forward_possible = True

    if canonical_convergence_required or formal_convergence_required:
        reason = "TARGET is a valid strict descendant of "
        parts = []
        if canonical_convergence_required:
            parts.append(f"canonical branch {canonical_branch!r}")
        if formal_convergence_required:
            parts.append(f"formal branch {formal_branch!r}")
        reason += " and ".join(parts) + "; fast-forward convergence required before activation"
        if canonical_convergence_phase:
            reason += " (workflow reports it is currently executing the convergence step)"
        return PromotionGateResult(
            target_sha=target_sha,
            canonical_main_sha=canonical_main_sha,
            formal_sha=formal_sha,
            tested_sha=tested_sha,
            strategy=strategy,
            fast_forward_possible=True,
            canonical_convergence_required=canonical_convergence_required,
            formal_convergence_required=formal_convergence_required,
            activation_allowed=False,
            status=STATUS_CONVERGENCE_REQUIRED,
            reason=reason,
        )

    # canonical_main_sha == target_sha == formal_sha == tested_sha
    return PromotionGateResult(
        target_sha=target_sha,
        canonical_main_sha=canonical_main_sha,
        formal_sha=formal_sha,
        tested_sha=tested_sha,
        strategy=strategy,
        fast_forward_possible=True,
        canonical_convergence_required=False,
        formal_convergence_required=False,
        activation_allowed=True,
        status=STATUS_PASS,
        reason="canonical main, formal, TESTED, and TARGET are all identical; activation is allowed",
    )
