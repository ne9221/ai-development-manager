"""Production Watcher provenance contract: TESTED == ACTIVATED == RUNNING.

Every SHA in this contract is captured by independently running `git
rev-parse HEAD` against a real checkout, or read back from an evidence file
that was itself written that way. Nothing here ever accepts a SHA as a bare
CLI argument or trusts an environment variable as ground truth -- that is
the whole point of the contract (see PROJECT task: production provenance
must be provable, not asserted).

Three phases, each gated on the previous one:

- capture-tested   Run at test time against the checkout under test. Writes
                    TESTED evidence (real `git rev-parse HEAD`).
- activate         Run by the installer against the checkout being
                    installed. Refuses (PROVENANCE_MISMATCH) unless the
                    checkout's current HEAD equals the TESTED evidence.
                    Writes ACTIVATED evidence.
- verify-running   Run by the Watcher runner on every tick, before it is
                    allowed to launch a real provider. Independently
                    re-resolves HEAD (the RUNNING sha) and refuses unless
                    TESTED == ACTIVATED == RUNNING, the checkout path
                    matches, and the ACTIVATED evidence is not stale.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manager.production_guard import PRODUCTION_MARKER_FILENAME, mark_production_path

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days


class ProvenanceError(Exception):
    """Raised whenever the TESTED/ACTIVATED/RUNNING contract cannot be proven."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _run_git(repository_path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_path), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise ProvenanceError(
            f"cannot run git {' '.join(args)} against {repository_path}: {exc} {stderr}".strip()
        ) from exc
    return result.stdout.strip()


def get_git_head_sha(repository_path: Path) -> str:
    """Independently resolve the real `git rev-parse HEAD` for repository_path."""
    sha = _run_git(repository_path, "rev-parse", "HEAD")
    if not _SHA_RE.match(sha):
        raise ProvenanceError(f"git HEAD for {repository_path} is not a valid SHA: {sha!r}")
    return sha


def get_git_branch(repository_path: Path) -> str:
    return _run_git(repository_path, "rev-parse", "--abbrev-ref", "HEAD")


def is_checkout_clean(repository_path: Path) -> bool:
    """Whether repository_path has zero uncommitted changes (tracked or
    untracked), ignoring this module's own ADM-managed production marker
    file (manager.production_guard.PRODUCTION_MARKER_FILENAME) -- an
    untracked marker that activate() itself just wrote into repository_path
    is not developer drift and must never make an otherwise-clean, freshly
    activated checkout look dirty. A real dirty production checkout means
    the on-disk state no longer matches the SHA verify_running() is about
    to vouch for -- e.g. a developer edited files in place without
    committing -- so this must gate verify_running() rather than being
    purely informational."""
    status = _run_git(repository_path, "status", "--porcelain", "--", ".",
                       f":(exclude){PRODUCTION_MARKER_FILENAME}")
    return status == ""


def _tested_evidence_path(manager_home: Path) -> Path:
    return manager_home / "provenance" / "tested_sha.json"


def _activated_evidence_path(manager_home: Path) -> Path:
    return manager_home / "provenance" / "activated_sha.json"


def runtime_evidence_path(manager_home: Path) -> Path:
    """Where real, per-tick runtime evidence is written on a PASS.

    This is the file a Dashboard or acceptance harness should read to learn
    the production Watcher's proven running/tested/activated identity
    (invariant: Watcher HEAD vs displayed runtime/version identity). It is
    only ever written after verify_running() has independently confirmed
    tested_sha == activated_sha == running_sha -- a FAIL never produces a
    fresh PASS-looking evidence file.
    """
    return manager_home / "provenance" / "runtime_evidence.json"


def read_runtime_evidence(manager_home: Path) -> dict | None:
    """Read back the last real runtime evidence, or None if none exists yet."""
    path = runtime_evidence_path(manager_home)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def identity_gate_from_evidence(manager_home: Path) -> str:
    """PASS/FAIL identity gate backed by real on-disk runtime evidence.

    Same PASS/FAIL semantics as the invariant "Watcher HEAD vs Dashboard
    runtime/version identity must agree" -- but driven by the actual
    evidence file verify_running() writes, not by literal test-fixture
    strings. Fails closed (FAIL) on missing or internally inconsistent
    evidence.
    """
    evidence = read_runtime_evidence(manager_home)
    if not evidence:
        return "FAIL"
    running_sha = evidence.get("running_sha")
    tested_sha = evidence.get("tested_sha")
    activated_sha = evidence.get("activated_sha")
    if not (running_sha and tested_sha and activated_sha):
        return "FAIL"
    return "PASS" if tested_sha == activated_sha == running_sha else "FAIL"


def _read_json(path: Path, kind: str) -> dict:
    if not path.exists():
        raise ProvenanceError(f"PROVENANCE_MISMATCH: no {kind} evidence at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"PROVENANCE_MISMATCH: {kind} evidence at {path} is not valid JSON: {exc}") from exc


def capture_tested(repository_path: Path, manager_home: Path) -> dict:
    """Independently capture the real HEAD SHA of repository_path as TESTED evidence."""
    sha = get_git_head_sha(repository_path)
    evidence = {
        "tested_sha": sha,
        "repository_path": str(repository_path),
        "branch": get_git_branch(repository_path),
        "captured_at": _now_iso(),
    }
    path = _tested_evidence_path(manager_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return evidence


def activate(repository_path: Path, manager_home: Path) -> dict:
    """Verify current HEAD matches TESTED evidence, then write ACTIVATED evidence.

    Fails closed if TESTED evidence is missing/invalid, or if the checkout
    actually being activated is not at the exact SHA that was tested.
    """
    tested = _read_json(_tested_evidence_path(manager_home), "TESTED")
    tested_sha = tested.get("tested_sha", "")
    if not _SHA_RE.match(tested_sha):
        raise ProvenanceError(f"PROVENANCE_MISMATCH: TESTED evidence has no valid tested_sha: {tested_sha!r}")

    activating_sha = get_git_head_sha(repository_path)
    if activating_sha != tested_sha:
        raise ProvenanceError(
            f"PROVENANCE_MISMATCH: tested_sha={tested_sha} != activating HEAD={activating_sha} "
            f"for {repository_path}; run tests against this exact checkout before activating"
        )

    evidence = {
        "tested_sha": tested_sha,
        "activated_sha": activating_sha,
        "repository_path": str(repository_path),
        "branch": get_git_branch(repository_path),
        "captured_at": _now_iso(),
    }
    path = _activated_evidence_path(manager_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    # Mechanical, on-disk consequence of activation: mark repository_path
    # itself as a protected production runtime checkout so
    # manager.execution_runner / manager.worktree_materializer can refuse
    # to ever hand it out as a developer/write task's working_directory
    # (see manager.production_guard).
    mark_production_path(repository_path, activating_sha, manager_home)

    return evidence


@dataclass(frozen=True)
class ProvenanceContract:
    running_sha: str
    tested_sha: str
    activated_sha: str
    repository_path: str
    branch: str
    captured_at: str

    def to_dict(self) -> dict:
        return {
            "running_sha": self.running_sha,
            "tested_sha": self.tested_sha,
            "activated_sha": self.activated_sha,
            "repository_path": self.repository_path,
            "branch": self.branch,
            "captured_at": self.captured_at,
        }


def verify_running(
    repository_path: Path,
    manager_home: Path,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> ProvenanceContract:
    """Independently resolve the RUNNING sha and cross-check it against ACTIVATED evidence.

    Fail-closed: raises ProvenanceError -- the caller must not launch a real
    provider -- if evidence is missing, invalid, for a different checkout,
    stale, or if TESTED/ACTIVATED/RUNNING are not all identical. Never
    trusts an externally supplied SHA (env var, argument); RUNNING is always
    a fresh `git rev-parse HEAD` against repository_path.
    """
    running_sha = get_git_head_sha(repository_path)

    if not is_checkout_clean(repository_path):
        raise ProvenanceError(
            f"PROVENANCE_MISMATCH: {repository_path} has uncommitted changes (dirty working tree); "
            "refusing to trust RUNNING sha until the checkout is clean"
        )

    activated = _read_json(_activated_evidence_path(manager_home), "ACTIVATED")
    tested_sha = activated.get("tested_sha", "")
    activated_sha = activated.get("activated_sha", "")
    evidence_repo = activated.get("repository_path")
    captured_at_raw = activated.get("captured_at")

    for name, value in (("tested_sha", tested_sha), ("activated_sha", activated_sha)):
        if not _SHA_RE.match(value):
            raise ProvenanceError(f"PROVENANCE_MISMATCH: ACTIVATED evidence missing valid {name}: {value!r}")

    if evidence_repo is not None and Path(evidence_repo) != Path(repository_path):
        raise ProvenanceError(
            f"PROVENANCE_MISMATCH: ACTIVATED evidence repository_path={evidence_repo!r} "
            f"!= running repository_path={repository_path!r} (wrong checkout)"
        )

    if captured_at_raw:
        try:
            captured_at = datetime.fromisoformat(captured_at_raw)
        except ValueError:
            raise ProvenanceError(
                f"PROVENANCE_MISMATCH: ACTIVATED evidence has unparseable captured_at={captured_at_raw!r}"
            )
        age_seconds = (_now() - captured_at).total_seconds()
        if age_seconds > max_age_seconds:
            raise ProvenanceError(
                f"PROVENANCE_MISMATCH: stale ACTIVATED evidence, captured_at={captured_at_raw} "
                f"is {age_seconds:.0f}s old (max {max_age_seconds}s); re-run activation"
            )
    else:
        raise ProvenanceError("PROVENANCE_MISMATCH: ACTIVATED evidence missing captured_at")

    if not (tested_sha == activated_sha == running_sha):
        raise ProvenanceError(
            "PROVENANCE_MISMATCH: tested_sha=%s activated_sha=%s running_sha=%s"
            % (tested_sha, activated_sha, running_sha)
        )

    contract = ProvenanceContract(
        running_sha=running_sha,
        tested_sha=tested_sha,
        activated_sha=activated_sha,
        repository_path=str(repository_path),
        branch=get_git_branch(repository_path),
        captured_at=_now_iso(),
    )

    evidence_path = runtime_evidence_path(manager_home)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(contract.to_dict(), indent=2), encoding="utf-8")

    return contract


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("capture-tested", "activate", "verify-running"):
        p = sub.add_parser(name)
        p.add_argument("--repository-path", required=True, type=Path)
        p.add_argument("--manager-home", required=True, type=Path)
        if name == "verify-running":
            p.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)

    args = parser.parse_args(argv)

    try:
        if args.command == "capture-tested":
            evidence = capture_tested(args.repository_path, args.manager_home)
        elif args.command == "activate":
            evidence = activate(args.repository_path, args.manager_home)
        else:
            evidence = verify_running(
                args.repository_path, args.manager_home, args.max_age_seconds
            ).to_dict()
    except ProvenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
