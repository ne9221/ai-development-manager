#!/usr/bin/env python3
"""Independent remote-readback verification for a repo-write execution's
pushed feature branch (Global Hands-off Execution Layer, Slice D2).

A provider running inside its own isolated worktree (manager.
worktree_materializer) may self-report having committed and pushed its
work, but nothing has ever independently verified that claim -- the
provider's own text output is never persisted or trusted by manager.
execution_runner (see its own docstring). This module closes that gap by
querying the real remote directly (`git ls-remote`, never a cached/local
view) and requiring the branch to exist there with a SHA that exactly
equals the worktree's own local final commit -- fail-closed on a missing
branch, a mismatched SHA, or any query failure.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Dict

from manager.tasks import TaskError

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _run(cwd, *args, runner=subprocess.run):
    return runner(["git", "-C", str(cwd), *args], text=True, encoding="utf-8", errors="replace", capture_output=True)


def resolve_origin_remote(working_directory, runner=subprocess.run) -> str:
    result = _run(working_directory, "remote", "get-url", "origin", runner=runner)
    if result.returncode != 0:
        raise TaskError(f"could not resolve origin remote: {(result.stderr or '').strip()}")
    url = (result.stdout or "").strip()
    if not url:
        raise TaskError("origin remote is configured but resolved to an empty URL")
    return url


def verify_remote_branch_matches(working_directory, branch_short: str, expected_sha: str,
                                 runner=subprocess.run) -> Dict[str, Any]:
    """Query the real origin remote for `branch_short` and require its SHA to
    exactly equal `expected_sha` (the worktree's own local final commit).

    Always raises TaskError -- never returns a "mismatch"/"missing" status
    -- on a missing branch, a mismatched SHA, an ambiguous readback, or any
    query failure, so a caller can never mistake an unverified push for a
    verified one. No force push and no merge are ever performed here; this
    only reads.
    """
    if not isinstance(expected_sha, str) or not SHA_PATTERN.match(expected_sha):
        raise TaskError(f"expected local commit SHA is not a valid full SHA: {expected_sha!r}")
    origin = resolve_origin_remote(working_directory, runner=runner)
    result = _run(working_directory, "ls-remote", "origin", f"refs/heads/{branch_short}", runner=runner)
    if result.returncode != 0:
        raise TaskError(f"git ls-remote against origin failed: {(result.stderr or '').strip()}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise TaskError(f"remote branch refs/heads/{branch_short} does not exist on {origin}; push was not verified")
    if len(lines) > 1:
        raise TaskError(f"remote returned multiple refs for refs/heads/{branch_short}; ambiguous readback")
    parts = lines[0].split()
    remote_sha = parts[0] if parts else ""
    if not SHA_PATTERN.match(remote_sha):
        raise TaskError(f"remote returned an unexpected ref value: {lines[0]!r}")
    if remote_sha != expected_sha:
        raise TaskError(
            f"remote SHA {remote_sha} for refs/heads/{branch_short} does not match local final commit SHA {expected_sha}"
        )
    return {"origin": origin, "branch": branch_short, "remote_sha": remote_sha}
