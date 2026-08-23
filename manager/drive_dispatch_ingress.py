"""Admit private Drive JSON requests into ADM's existing trusted ingress."""

import json
import os
import time
from datetime import datetime, timezone

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.dispatch_requests import dispatch_request_registry
from manager.tasks import MIME_FOLDER, MIME_JSON, TaskError, validate


FOLDER_NAME = "DISPATCH-REQUESTS"
FOLDER_ENV = "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID"
OWNER_ENV = "ADM_DRIVE_DISPATCH_INGRESS_OWNER"
MAX_AGE_SECONDS = 86400
MAX_FILE_BYTES = 16384
METADATA_FIELDS = ("id,name,mimeType,trashed,parents,size,driveId,ownedByMe,"
                   "owners(emailAddress,permissionId,me),permissions(id,type,role,emailAddress),createdTime")

# Bounded-poll defaults (see poll_drive_dispatch_requests docstring). These
# exist so one --once tick can never again degrade into O(lifetime request
# history): a growing DISPATCH-REQUESTS folder must cost roughly the same
# per poll regardless of how much history has accumulated in it.
DEFAULT_TIME_BUDGET_SECONDS = 20.0
DEFAULT_MAX_METADATA_PAGES = 3
DEFAULT_RECENT_CANDIDATES = 8
DEFAULT_MAX_CANDIDATES_PER_POLL = 12
# Conservative clock-skew margin before trusting Drive's own metadata
# createdTime to skip a download outright -- always strictly more
# conservative than read_request()'s own -300s future-dated tolerance, so
# this optimization can never skip something the authoritative body check
# would have accepted.
STALE_METADATA_SKIP_MARGIN_SECONDS = 300


def _identity_matches(identity, expected):
    return expected in {identity.get("emailAddress"), identity.get("permissionId")}


def _one_private_owner(metadata, expected):
    owners = metadata.get("owners")
    permissions = metadata.get("permissions")
    return (isinstance(owners, list) and len(owners) == 1 and _identity_matches(owners[0], expected)
            and isinstance(permissions, list) and len(permissions) == 1
            and permissions[0].get("type") == "user" and permissions[0].get("role") == "owner"
            and _identity_matches(permissions[0], expected))


def verify_ingress_folder(service, folder_id, expected_owner):
    if not folder_id or not expected_owner:
        raise TaskError(f"{FOLDER_ENV} and {OWNER_ENV} are required")
    about = service.about().get(fields="user(emailAddress,permissionId)").execute()
    user = about.get("user") if isinstance(about, dict) else None
    if not isinstance(user, dict) or not _identity_matches(user, expected_owner):
        raise TaskError("Drive ingress OAuth identity is missing or does not match configured owner")
    folder = service.files().get(fileId=folder_id, fields=METADATA_FIELDS).execute()
    if (not isinstance(folder, dict) or folder.get("name") != FOLDER_NAME
            or folder.get("mimeType") != MIME_FOLDER or folder.get("trashed") is not False
            or folder.get("driveId") is not None or folder.get("ownedByMe") is not True
            or not _one_private_owner(folder, expected_owner)):
        raise TaskError("Drive ingress folder provenance is ambiguous or unverifiable")
    return folder


def _request_files(service, folder_id, order_by=None, max_pages=None, deadline=None):
    """List DISPATCH-REQUESTS metadata (never content). `order_by`, if
    given, is passed straight through as Drive's own `orderBy` (e.g.
    "createdTime desc") so recency ordering is resolved server-side rather
    than by fetching everything and sorting locally. `max_pages` and
    `deadline` (a `time.monotonic()` value), if given, bound how much of
    a large historical listing this call will ever walk -- once either is
    hit, whatever pages were already fetched are returned rather than
    continuing. All three default to None/unbounded, reproducing this
    function's exact prior behavior for any existing caller that does not
    pass them."""
    query = f"'{folder_id}' in parents and trashed=false"
    files, page_token, seen_tokens = [], None, set()
    pages_fetched = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return files
        if max_pages is not None and pages_fetched >= max_pages:
            return files
        params = {
            "q": query, "spaces": "drive",
            "fields": f"nextPageToken,files({METADATA_FIELDS})", "pageSize": 100,
        }
        if order_by:
            params["orderBy"] = order_by
        if page_token is not None:
            params["pageToken"] = page_token
        response = service.files().list(**params).execute()
        pages_fetched += 1
        page = response.get("files") if isinstance(response, dict) else None
        next_token = response.get("nextPageToken") if isinstance(response, dict) else None
        if (not isinstance(page, list)
                or (next_token is not None and (not isinstance(next_token, str)
                                                or not next_token or next_token in seen_tokens))):
            raise TaskError("malformed Drive ingress listing response")
        files.extend(page)
        if next_token is None:
            return files
        seen_tokens.add(next_token)
        page_token = next_token


def _created_time_indicates_stale(metadata, now, margin_seconds=STALE_METADATA_SKIP_MARGIN_SECONDS):
    """Optimization only, never authoritative: skip an obviously-ancient
    candidate's get_media() download using Drive's own metadata
    `createdTime`, conservatively margined for clock skew. A missing,
    malformed, or not-old-enough-to-skip createdTime always falls through
    to a real download -- read_request()'s own `created_at` request-body
    check remains the sole authority on staleness/future-dating; this can
    only ever skip a download early, never accept or reject a request."""
    if not isinstance(metadata, dict):
        return False
    created_time = metadata.get("createdTime")
    if not isinstance(created_time, str) or not created_time:
        return False
    try:
        created = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (now - created.astimezone(timezone.utc)).total_seconds()
    return age > (MAX_AGE_SECONDS + margin_seconds)


def read_request(service, folder_id, expected_owner, metadata, now=None):
    if (not isinstance(metadata, dict) or metadata.get("mimeType") != MIME_JSON
            or metadata.get("parents") != [folder_id] or metadata.get("trashed") is not False
            or metadata.get("driveId") is not None or metadata.get("ownedByMe") is not True
            or not _one_private_owner(metadata, expected_owner)):
        raise TaskError("Drive request provenance is ambiguous or unverifiable")
    try:
        size = int(metadata.get("size"))
    except (TypeError, ValueError) as exc:
        raise TaskError("Drive request size is unverifiable") from exc
    if size < 2 or size > MAX_FILE_BYTES:
        raise TaskError("Drive request size is outside the accepted range")
    raw = service.files().get_media(fileId=metadata["id"]).execute()
    if not isinstance(raw, bytes) or len(raw) != size or len(raw) > MAX_FILE_BYTES:
        raise TaskError("Drive request content verification failed")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskError("Drive request is not valid UTF-8 JSON") from exc
    validate("dispatch_request", document)
    if metadata.get("name") != f'{document["request_id"]}.json':
        raise TaskError("Drive request filename does not match request_id")
    created = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    age = (current - created.astimezone(timezone.utc)).total_seconds()
    if age < -300 or age > MAX_AGE_SECONDS:
        raise TaskError("Drive request is stale or future-dated")
    return document


def poll_drive_dispatch_requests(store, service, bucket, folder_id=None, expected_owner=None, now=None,
                                 registry_factory=dispatch_request_registry,
                                 max_candidates=DEFAULT_MAX_CANDIDATES_PER_POLL,
                                 recent_candidates=DEFAULT_RECENT_CANDIDATES,
                                 deadline=None, time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
                                 max_metadata_pages=DEFAULT_MAX_METADATA_PAGES):
    """Bounded, fault-isolated poll of DISPATCH-REQUESTS.

    Every existing positional caller (`store, service, bucket[, folder_id,
    expected_owner, now[, registry_factory]]`) keeps working unchanged --
    every bounding knob below is a new keyword-only-in-practice argument
    with a conservative default appropriate for a ~1-minute trigger
    interval, so this function's runtime cost per call no longer grows
    with the lifetime size of DISPATCH-REQUESTS.

    Contract:
    - Exactly one Drive `files.list()` walk is performed, ordered newest
      first (`orderBy="createdTime desc"`) and capped to `max_metadata_pages`
      pages -- metadata only, no content is downloaded here. This single
      listing is bounded regardless of total historical file count.
    - The front of that listing (`recent_candidates` entries) is
      considered first, so a brand-new request is never starved behind
      history.
    - An obviously-ancient candidate (Drive's own `createdTime` older than
      MAX_AGE_SECONDS plus a clock-skew margin) is skipped before ever
      calling get_media() -- cheap, metadata-only, and never counted
      against the download budget. This is purely an optimization:
      read_request()'s own request-body `created_at` check remains the
      sole authority on staleness and is never weakened or bypassed.
    - After the recent window, remaining budget (`max_candidates` total
      downloads per poll) is spent on the *rest* of the same bounded
      listing -- the candidates recency pushed just past the front. This
      is the fairness pass: a still-valid (<24h) request that a burst of
      newer arrivals pushed out of the recent window is not starved
      forever. Which slice of that remainder gets serviced rotates
      deterministically from the wall clock (current UTC minute), so
      across enough ticks every position in the bounded remainder
      eventually gets a turn -- without persisting any cursor/database
      state between polls.
    - Every individual candidate is still fault-isolated exactly as
      before: a malformed/rejected/stale file can never abort any other
      candidate, and always resolves to `{"file_id": ..., "accepted": False}`
      rather than raising.
    - `deadline` (a `time.monotonic()` value; defaults to now +
      `time_budget_seconds`) is checked before starting the metadata
      listing and before starting each new candidate's read/dispatch --
      once exhausted, no new work is started. Exactly like poll_once()'s
      own deadline contract, this never interrupts a read/dispatch already
      in progress.
    - No Drive request file is ever archived, trashed, moved, or deleted
      here (or anywhere in this module) -- every file remains in
      DISPATCH-REQUESTS for audit regardless of outcome. Idempotency
      remains solely manager.dispatch_requests.dispatch_request_registry()
      via handle_dispatch(); this function creates no other durable state.
    """
    folder_id = folder_id or os.environ.get(FOLDER_ENV)
    expected_owner = expected_owner or os.environ.get(OWNER_ENV)
    verify_ingress_folder(service, folder_id, expected_owner)
    current = now or datetime.now(timezone.utc)
    if deadline is None:
        deadline = time.monotonic() + time_budget_seconds

    results = []
    seen_ids = set()
    downloads_used = 0

    def _handle_one(metadata):
        nonlocal downloads_used
        downloads_used += 1
        try:
            request = read_request(service, folder_id, expected_owner, metadata, now=current)
            payload = {
                "request_id": request["request_id"], "project_id": request["project_id"],
                "title": request["title"], "goal": request["goal"],
                "priority": request.get("priority") or "normal",
                "constraints": {"read_only": True},
            }
            if request.get("preferred_provider") is not None:
                payload["provider"] = request["preferred_provider"]
            if request.get("account_id") is not None:
                payload["account_id"] = request["account_id"]
            result = handle_dispatch(store, service, lambda project_id, request_id:
                                     registry_factory(bucket, project_id, request_id), payload)
            results.append({"file_id": metadata["id"], **result})
        except (TaskError, DispatchIngressError):
            results.append({"file_id": metadata.get("id"), "accepted": False})

    def _scan(metadata_list, budget):
        used = 0
        for metadata in metadata_list:
            if used >= budget or downloads_used >= max_candidates:
                break
            if time.monotonic() >= deadline:
                break
            file_id = metadata.get("id") if isinstance(metadata, dict) else None
            if not file_id or file_id in seen_ids:
                continue
            seen_ids.add(file_id)
            if _created_time_indicates_stale(metadata, current):
                continue
            _handle_one(metadata)
            used += 1

    if time.monotonic() >= deadline:
        return results

    metadata_list = _request_files(service, folder_id, order_by="createdTime desc",
                                    max_pages=max_metadata_pages, deadline=deadline)
    _scan(metadata_list[:recent_candidates], recent_candidates)

    tail = metadata_list[recent_candidates:]
    fairness_budget = max_candidates - downloads_used
    if tail and fairness_budget > 0 and time.monotonic() < deadline:
        rotation = int(current.timestamp() // 60) % len(tail)
        _scan(tail[rotation:] + tail[:rotation], fairness_budget)

    return results
