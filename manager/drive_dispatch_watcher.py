"""Bounded, single-poll executable trigger for Drive dispatch ingress.

    python -m manager.drive_dispatch_watcher --once

This module is intentionally a thin runner, not a second orchestrator: it
only ever builds the existing Drive service (collectors.publish_drive.
build_service), the existing Drive-backed record store
(manager.tasks.DriveRecords), resolves the existing configured GCS
idempotency bucket (manager.gcs_lock_registry.BUCKET_ENV -- the same
ADM_LOCK_GCS_BUCKET manager.command_watcher and cloud.app already use), and
calls manager.drive_dispatch_ingress.poll_drive_dispatch_requests() exactly
once. That existing call already does all provenance validation and
delegates Task+Command creation solely to cloud.dispatch_ingress.
handle_dispatch() -- this module duplicates none of that logic.

This runner never launches a provider process, never imports or calls any
provider launcher or the execution runner, and creates no Task/Command
itself. Command Watcher remains the only provider launch
authority; a Command created (indirectly, via handle_dispatch()) by a poll
here sits `queued` for Command Watcher's own unmodified pipeline to pick up
under its own rules, exactly like any other trusted-ingress Command.

One invocation performs exactly one bounded poll -- there is no loop here.
A malformed individual request cannot abort the poll (poll_drive_dispatch_
requests() already isolates per-request failures); missing required
configuration (the GCS bucket, or the Drive ingress folder/owner env vars
checked inside poll_drive_dispatch_requests -> verify_ingress_folder) and
Drive authentication failures fail the whole invocation closed instead of
silently no-oping.
"""

import argparse
import json
import os
import sys

from collectors.publish_drive import build_service
from manager.drive_dispatch_ingress import poll_drive_dispatch_requests
from manager.gcs_lock_registry import BUCKET_ENV
from manager.tasks import DriveRecords, TaskError


def run_once(build_service_fn=build_service, store_factory=DriveRecords, poll=poll_drive_dispatch_requests):
    """Build the existing Drive service/store, resolve the existing GCS
    idempotency bucket, and call poll_drive_dispatch_requests() exactly
    once. Raises TaskError (missing config) or whatever build_service_fn
    raises (Drive auth failure) rather than silently no-oping; the caller
    decides how to turn that into a process exit status."""
    bucket = os.environ.get(BUCKET_ENV)
    if not bucket:
        raise TaskError(f"{BUCKET_ENV} is required")
    service = build_service_fn()
    store = store_factory(service)
    # folder_id/expected_owner are intentionally left to poll_drive_dispatch_
    # requests()'s own existing defaulting + verify_ingress_folder() check --
    # that already fails closed (TaskError) on missing/invalid
    # ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID / ADM_DRIVE_DISPATCH_INGRESS_OWNER
    # without this module reimplementing that check.
    results = poll(store, service, bucket)
    return {"status": "ok", "ingress": results}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bounded single poll of Drive dispatch ingress requests; never launches a provider")
    parser.add_argument("--once", action="store_true", required=True,
                         help="required: this runner only ever performs exactly one bounded poll, never a loop")
    parser.parse_args(argv)
    try:
        # Looked up from module globals at call time (not via run_once()'s
        # own default arguments, which bind at function-definition time) so
        # that patching manager.drive_dispatch_watcher.build_service/
        # DriveRecords in tests takes effect.
        result = run_once(build_service_fn=build_service, store_factory=DriveRecords)
    except TaskError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, separators=(",", ":")), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, separators=(",", ":")), file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
