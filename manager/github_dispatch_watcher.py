"""Bounded, single-poll executable trigger for GitHub dispatch ingress.

    python -m manager.github_dispatch_watcher --once

This is the GitHub-write-connector counterpart of
manager.drive_dispatch_watcher -- same thin-runner contract, same
non-negotiable boundaries, adapted only for the fact that this poller needs
TWO clients instead of one: the EXISTING Drive service/store (for Task/
Command persistence and quota reads, exactly as the Drive watcher uses it)
plus a new GitHub API client (manager.github_dispatch_client.GitHubApiClient,
for discovering/reading request files from the GitHub ingress branch). It
only ever builds those, resolves the existing configured GCS idempotency
bucket (manager.gcs_lock_registry.BUCKET_ENV -- the SAME
ADM_LOCK_GCS_BUCKET the Drive watcher, manager.command_watcher, and
cloud.app already use, so a request submitted via either ingress collides
in the same registry), and calls manager.github_dispatch_ingress.
poll_github_dispatch_requests() exactly once. That existing call already
does all provenance validation and delegates Task+Command creation solely
to cloud.dispatch_ingress.handle_dispatch() -- this module duplicates none
of that logic.

This runner never launches a provider process, never imports or calls any
provider launcher or the execution runner, and creates no Task/Command
itself. Command Watcher remains the only provider launch authority; a
Command created (indirectly, via handle_dispatch()) by a poll here sits
`queued` for Command Watcher's own unmodified pipeline to pick up under its
own rules, exactly like any other trusted-ingress Command -- identical to
the Drive ingress path.

One invocation performs exactly one bounded poll -- there is no loop here.
A malformed individual request cannot abort the poll (poll_github_dispatch_
requests() already isolates per-request failures); missing required
configuration (the GCS bucket, the GitHub repo/branch/token env vars
checked inside poll_github_dispatch_requests -> verify_ingress_repo /
GitHubApiClient.default(), or Drive authentication failures) fails the
whole invocation closed instead of silently no-oping.
"""

import argparse
import json
import os
import sys

from collectors.publish_drive import build_service
from manager.github_dispatch_client import GitHubApiClient
from manager.github_dispatch_ingress import poll_github_dispatch_requests
from manager.gcs_lock_registry import BUCKET_ENV
from manager.tasks import DriveRecords, TaskError
from manager.production_guard import RuntimeGuardError, require_runtime_guard


def run_once(build_service_fn=build_service, store_factory=DriveRecords, client_factory=GitHubApiClient.default,
            poll=poll_github_dispatch_requests):
    """Build the existing Drive service/store, resolve the existing GCS
    idempotency bucket, build the GitHub API client, and call
    poll_github_dispatch_requests() exactly once. Raises TaskError (missing
    config) or whatever build_service_fn/client_factory raise (auth
    failure) rather than silently no-oping; the caller decides how to turn
    that into a process exit status."""
    require_runtime_guard()
    bucket = os.environ.get(BUCKET_ENV)
    if not bucket:
        raise TaskError(f"{BUCKET_ENV} is required")
    service = build_service_fn()
    store = store_factory(service)
    client = client_factory()
    # repo/branch/path are intentionally left to poll_github_dispatch_
    # requests()'s own existing defaulting + verify_ingress_repo() check --
    # that already fails closed (TaskError) on missing/invalid
    # ADM_GITHUB_DISPATCH_INGRESS_REPO / ADM_GITHUB_DISPATCH_INGRESS_BRANCH
    # without this module reimplementing that check.
    results = poll(store, service, bucket, client)
    return {"status": "ok", "ingress": results}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bounded single poll of GitHub dispatch ingress requests; never launches a provider")
    parser.add_argument("--once", action="store_true", required=True,
                         help="required: this runner only ever performs exactly one bounded poll, never a loop")
    parser.parse_args(argv)
    from manager.scheduler_provenance import finish, start
    invocation = start(os.environ.get("AI_MANAGER_HOME", "."), "github_dispatch_ingress")
    try:
        require_runtime_guard()
    except RuntimeGuardError as exc:
        _print_error(exc.code)
        return 1
    try:
        # Looked up from module globals at call time (not via run_once()'s
        # own default arguments, which bind at function-definition time) so
        # that patching manager.github_dispatch_watcher.build_service/
        # DriveRecords/GitHubApiClient.default in tests takes effect.
        result = run_once(build_service_fn=build_service, store_factory=DriveRecords,
                          client_factory=GitHubApiClient.default)
    except TaskError as exc:
        _print_safe_failure(exc, "GitHub dispatch ingress configuration or validation error")
        finish(os.environ.get("AI_MANAGER_HOME", "."), invocation, "failed")
        return 1
    except Exception as exc:
        _print_safe_failure(exc, "GitHub dispatch ingress poll failed")
        finish(os.environ.get("AI_MANAGER_HOME", "."), invocation, "failed")
        return 1
    print(json.dumps(result, separators=(",", ":")))
    finish(os.environ.get("AI_MANAGER_HOME", "."), invocation, "completed")
    return 0


def _print_safe_failure(exc, message):
    """Write a deterministic, secret-safe failure report to stderr.

    Never includes the raw exception message (str(exc)) -- an arbitrary
    exception can carry a file path, an Authorization header, a token, or a
    raw GitHub API response body baked into its message. Only a bounded
    exception *class name* (error_kind) and a fixed, generic `message`
    string are ever emitted. The caller still returns a nonzero exit code
    on top of this -- this function changes stderr content only, never the
    failure/exit-code contract."""
    print(json.dumps({"status": "error", "error_kind": type(exc).__name__, "message": message},
                      separators=(",", ":")), file=sys.stderr)


def _print_error(code):
    print(json.dumps({"status": "error", "error_kind": "RuntimeGuardError", "message": code},
                      separators=(",", ":")), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
