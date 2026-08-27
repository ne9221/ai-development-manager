"""Admit GitHub-committed JSON dispatch requests into ADM's existing trusted
ingress -- the GitHub-write-connector counterpart to
manager.drive_dispatch_ingress (ChatGPT's Drive-write connector is
unreliable; its GitHub-write connector is not, so this module gives it an
equivalent path into the exact same admission/idempotency machinery).

Mechanism: a dedicated branch of THIS repo carries one `{request_id}.json`
file per request under a dedicated directory (see REPO_ENV/BRANCH_ENV/
PATH_ENV below) -- exactly the shape a "create/update file" GitHub-write-
connector action can produce with a normal commit. This module discovers
those files via the GitHub REST Contents API (manager.github_dispatch_client),
validates them with the IDENTICAL schema/admission path Drive ingress uses
(schema/dispatch_request.schema.json via manager.tasks.validate(), and
cloud.dispatch_ingress.handle_dispatch() for admission), and shares the
SAME idempotency registry (manager.dispatch_requests.dispatch_request_
registry()/claim_dispatch_request(), via handle_dispatch() -- never forked).
A request submitted via GitHub and one submitted via Drive with the same
request_id collide in that shared registry by design: both resolve to the
same deterministic (task_id, command_id) = f"dispatch-{request_id}".

This module never launches a provider process and never imports/calls
anything from manager.command_watcher -- exactly the same non-negotiable
boundary manager.drive_dispatch_ingress observes. A Command created here
(indirectly, via handle_dispatch()) sits `queued` for Command Watcher's own
unmodified pipeline to pick up under its own rules.

Deliberately NOT ported from manager.drive_dispatch_ingress: the elaborate
absolute-hour-bucket fairness rotation machinery built around Drive's own
files.list() pagination/orderBy quirks. GitHub's Contents API lists an
entire directory in ONE call (no server-side recency ordering exists to ask
for, and -- unlike a Drive folder that can silently accumulate years of
history -- this ingress directory is only ever populated by ChatGPT's own
GitHub-write connector at a human's request, so unbounded historical growth
is a much smaller real risk here). What IS kept, because it is a safety
property rather than a Drive-specific complexity: provenance verification
(repo identity + branch existence), bounded work per poll (both the
directory listing and the per-file downloads are capped), per-candidate
fault isolation (one malformed file can never abort the poll for others),
size/age/schema validation, and durable rejection recording via the
existing record_dispatch_rejection() -- keyed by the file's own git blob
`sha` (GitHub's analogue of Drive's file `id`).
"""

import json
import os
import time
from datetime import datetime, timezone

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.dispatch_requests import dispatch_rejection_registry, dispatch_request_registry, record_dispatch_rejection
from manager.github_dispatch_client import GitHubApiClient, GitHubApiError, GitHubNotFound
from manager.tasks import TaskError, now_iso, validate


REPO_ENV = "ADM_GITHUB_DISPATCH_INGRESS_REPO"
BRANCH_ENV = "ADM_GITHUB_DISPATCH_INGRESS_BRANCH"
PATH_ENV = "ADM_GITHUB_DISPATCH_INGRESS_PATH"
DEFAULT_BRANCH = "dispatch-requests"
DEFAULT_PATH = "dispatch-requests"

MAX_AGE_SECONDS = 86400
MAX_FILE_BYTES = 16384

# Bounded-poll defaults -- see poll_github_dispatch_requests()'s docstring.
# One --once tick must cost roughly the same regardless of how many files
# have ever accumulated in the ingress directory.
DEFAULT_TIME_BUDGET_SECONDS = 20.0
DEFAULT_MAX_CANDIDATES_PER_POLL = 12
# Assumed poll cadence, used only to rotate which subset of a
# larger-than-max_candidates directory listing gets scanned this poll (see
# poll_github_dispatch_requests()'s rotation comment) -- a tuning knob, not
# a correctness assumption, exactly like manager.drive_dispatch_ingress's
# own ASSUMED_POLL_INTERVAL_SECONDS.
ASSUMED_POLL_INTERVAL_SECONDS = 60


def verify_ingress_repo(client, repo, branch):
    """Provenance check adapted to what the GitHub API can cheaply prove:
    the configured repo string actually resolves to that repo's own
    full_name (catches a typo, or a rename/redirect silently pointing
    elsewhere), and the configured branch actually exists on it. Both fail
    closed (TaskError) -- this is the GitHub-API-shape equivalent of
    manager.drive_dispatch_ingress.verify_ingress_folder()'s owner/identity
    checks, though GitHub's REST API has no folder-ownership concept to
    check an equivalent of Drive's single-private-owner invariant against;
    repo + branch identity is the strongest provenance signal available
    here, and per-file checks in read_request() cover the rest."""
    if not repo or not branch:
        raise TaskError(f"{REPO_ENV} and {BRANCH_ENV} are required")
    try:
        repository = client.get_repo(repo)
    except GitHubApiError as exc:
        raise TaskError("GitHub ingress repo is missing or unverifiable") from exc
    if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str) \
            or repository["full_name"].lower() != repo.lower():
        raise TaskError("GitHub ingress repo provenance is ambiguous or unverifiable")
    try:
        client.get_branch(repo, branch)
    except GitHubApiError as exc:
        raise TaskError("GitHub ingress branch is missing or unverifiable") from exc
    return repository


def _list_request_files(client, repo, path, branch):
    """One bounded Contents API call listing the ingress directory --
    metadata only (name/path/sha/size/type), never content. A directory
    that has never received a commit does not exist in git at all, so a 404
    here is treated as "no requests yet" (an empty list), not a
    misconfiguration -- repo/branch identity was already proven by
    verify_ingress_repo() before this is ever called."""
    try:
        entries = client.list_directory(repo, path, branch)
    except GitHubNotFound:
        return []
    except GitHubApiError as exc:
        raise TaskError("GitHub ingress directory listing failed") from exc
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("type") == "file"
            and isinstance(entry.get("name"), str) and entry["name"].endswith(".json")]


def read_request(client, repo, path, branch, entry, now=None):
    """Fetch, verify, and parse one candidate file -- the GitHub-API-shape
    counterpart of manager.drive_dispatch_ingress.read_request(). `entry` is
    one shallow listing entry from _list_request_files()."""
    if not isinstance(entry, dict) or entry.get("type") != "file" or not isinstance(entry.get("name"), str) \
            or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha"), str):
        raise TaskError("GitHub request listing entry is malformed")
    try:
        size = int(entry.get("size"))
    except (TypeError, ValueError) as exc:
        raise TaskError("GitHub request size is unverifiable") from exc
    if size < 2 or size > MAX_FILE_BYTES:
        raise TaskError("GitHub request size is outside the accepted range")
    try:
        document_entry = client.get_file(repo, entry["path"], branch)
    except GitHubApiError as exc:
        raise TaskError("GitHub request content fetch failed") from exc
    if not isinstance(document_entry, dict) or document_entry.get("sha") != entry["sha"] \
            or document_entry.get("path") != entry["path"]:
        # The listing and the per-file fetch must agree on identity -- a
        # mismatch (e.g. a concurrent force-push/rewrite between the two
        # calls) is treated the same as any other unverifiable candidate,
        # never trusted on partial evidence.
        raise TaskError("GitHub request content verification failed")
    raw = GitHubApiClient.decode_file_content(document_entry)
    if len(raw) != size or len(raw) > MAX_FILE_BYTES:
        raise TaskError("GitHub request content verification failed")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskError("GitHub request is not valid UTF-8 JSON") from exc
    validate("dispatch_request", document)
    if entry["name"] != f'{document["request_id"]}.json':
        raise TaskError("GitHub request filename does not match request_id")
    created = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    age = (current - created.astimezone(timezone.utc)).total_seconds()
    if age < -300 or age > MAX_AGE_SECONDS:
        raise TaskError("GitHub request is stale or future-dated")
    return document


def build_dispatch_payload(request):
    """Turn one validated dispatch_request document (already schema-checked
    by manager.tasks.validate("dispatch_request", ...)) into the payload
    shape cloud.dispatch_ingress.handle_dispatch() expects. Shared verbatim
    between every GitHub-based ingress mechanism (file-based here, and
    manager.github_issue_dispatch_ingress's Issue-based counterpart) so this
    security-sensitive mapping -- most notably the explicit-opt-in-by-key-
    presence repo_write/read_only contract -- exists in exactly one place.

    Explicit opt-in only, by KEY PRESENCE -- not `is not None` -- a request
    is write-mode if and only if it names its own repo_write key.
    schema/dispatch_request.schema.json's own repo_write.type is "object"
    (not ["object", "null"]), so "repo_write": null already fails schema
    validation before a document ever reaches here -- this dict membership
    check is never actually asked to distinguish null from absence, but is
    written as membership regardless so the security contract ("only two
    valid states: absent, or a valid object") is legible here too, not just
    enforced one layer up. There is deliberately no inference from
    title/goal text and no server-side default to write mode."""
    payload = {
        "request_id": request["request_id"], "project_id": request["project_id"],
        "title": request["title"], "goal": request["goal"],
        "priority": request.get("priority") or "normal",
    }
    if "repo_write" in request:
        payload["constraints"] = {"read_only": False}
        payload["repo_write"] = request["repo_write"]
    else:
        payload["constraints"] = {"read_only": True}
    if request.get("preferred_provider") is not None:
        payload["provider"] = request["preferred_provider"]
    if request.get("account_id") is not None:
        payload["account_id"] = request["account_id"]
    return payload


def poll_github_dispatch_requests(store, service, bucket, client, repo=None, branch=None, path=None, now=None,
                                  registry_factory=dispatch_request_registry,
                                  rejection_registry_factory=dispatch_rejection_registry,
                                  max_candidates=DEFAULT_MAX_CANDIDATES_PER_POLL,
                                  deadline=None, time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS):
    """Bounded, fault-isolated poll of the GitHub dispatch-requests ingress
    directory -- the GitHub-write-connector counterpart of
    manager.drive_dispatch_ingress.poll_drive_dispatch_requests().

    `store`/`service` are the EXISTING Drive-backed record store / Drive
    service (manager.tasks.DriveRecords / collectors.publish_drive.
    build_service) -- unrelated to GitHub, but still required: handle_dispatch()
    persists the created Task+Command through `store` exactly as it always
    has, and forwards `service` to manager.dispatcher.dispatch() for its own
    quota read. `client` is a manager.github_dispatch_client.GitHubApiClient
    (or test double with the same list_directory/get_file/get_repo/
    get_branch shape) used ONLY to discover/read candidate request files.

    Contract:
    - Exactly one Contents API directory listing is performed
      (_list_request_files(), one bounded HTTP call, metadata only).
    - Provenance is verified once per poll (verify_ingress_repo(): repo
      identity + branch existence) before any listing/reading happens.
    - Work is bounded to `max_candidates` file reads per poll, regardless
      of how many files exist in the directory. When the listing is larger
      than `max_candidates`, which subset gets read rotates deterministically
      by wall-clock minute (mirroring manager.drive_dispatch_ingress's own
      tail-fairness rotation, minus the absolute-hour-bucket machinery that
      exists there specifically to work around Drive's own list-API
      pagination/ordering quirks -- GitHub's one-call directory listing has
      no equivalent need) so a poll never starves the same subset forever.
    - Every individual candidate is fault-isolated: a malformed/rejected/
      stale file can never abort any other candidate, and always resolves
      to `{"file_id": ..., "accepted": False}` rather than raising.
    - `deadline` (a `time.monotonic()` value; defaults to now +
      `time_budget_seconds`) stops STARTING new candidate work once passed
      -- an already-started HTTP call or dispatch in progress is never
      interrupted. A true hard cutoff is expected to come from the calling
      Scheduled Task's own execution time limit, exactly like the Drive
      poller.
    - No GitHub file is ever deleted/reverted/force-pushed away here -- this
      module only ever reads. Idempotency remains solely
      manager.dispatch_requests.dispatch_request_registry() via
      handle_dispatch(). A candidate rejected before ever reaching that
      registry (malformed JSON, schema-invalid payload, unverifiable
      provenance, invalid project_id/provider/account, oversized, or any
      other TaskError/DispatchIngressError this function's per-candidate
      handler catches) durably records that rejection instead -- keyed by
      the file's own git blob `sha`, via
      manager.dispatch_requests.record_dispatch_rejection() -- so it is
      never silently lost with only this poll's own in-memory return value
      as evidence, matching the Drive poller's own contract.
    """
    repo = repo or os.environ.get(REPO_ENV)
    branch = branch or os.environ.get(BRANCH_ENV, DEFAULT_BRANCH)
    path = path or os.environ.get(PATH_ENV, DEFAULT_PATH)
    verify_ingress_repo(client, repo, branch)
    current = now or datetime.now(timezone.utc)
    if deadline is None:
        deadline = time.monotonic() + time_budget_seconds

    results = []

    def _handle_one(entry):
        try:
            request = read_request(client, repo, path, branch, entry, now=current)
            payload = build_dispatch_payload(request)
            result = handle_dispatch(store, service, lambda project_id, request_id:
                                     registry_factory(bucket, project_id, request_id), payload,
                                     request_created_at=request.get("created_at"))
            results.append({"file_id": entry["sha"], **result})
        except (TaskError, DispatchIngressError) as exc:
            file_id = entry.get("sha") if isinstance(entry, dict) else None
            reason_code = exc.code if isinstance(exc, DispatchIngressError) else "ingress_rejected"
            if file_id:
                try:
                    record_dispatch_rejection(rejection_registry_factory(bucket, file_id), file_id,
                                              reason_code, str(exc), now_iso())
                except Exception:
                    # Best-effort only, matching manager.dispatch_requests'
                    # established contract: never let a failure to durably
                    # record the rejection mask the real rejection outcome
                    # this poll already produced below.
                    pass
            results.append({"file_id": file_id, "accepted": False})

    if time.monotonic() >= deadline:
        return results

    entries = _list_request_files(client, repo, path, branch)
    if entries:
        # Deterministic wall-clock rotation over the (bounded) listing --
        # see module docstring for why the elaborate absolute-hour-bucket
        # machinery in manager.drive_dispatch_ingress is not ported here.
        rotation = int(current.timestamp() // ASSUMED_POLL_INTERVAL_SECONDS) % len(entries)
        entries = entries[rotation:] + entries[:rotation]

    downloads_used = 0
    for entry in entries:
        if downloads_used >= max_candidates or time.monotonic() >= deadline:
            break
        downloads_used += 1
        _handle_one(entry)

    return results
