"""Admit private Drive JSON requests into ADM's existing trusted ingress."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.dispatch_requests import dispatch_request_registry
from manager.tasks import MIME_FOLDER, MIME_JSON, TaskError, validate


FOLDER_NAME = "DISPATCH-REQUESTS"
FOLDER_ENV = "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID"
OWNER_ENV = "ADM_DRIVE_DISPATCH_INGRESS_OWNER"
MAX_AGE_SECONDS = 86400
MAX_FILE_BYTES = 16384
METADATA_FIELDS = ("id,name,mimeType,trashed,parents,size,driveId,ownedByMe,"
                   "owners(emailAddress,permissionId,me),permissions(id,type,role,emailAddress),"
                   "createdTime,modifiedTime")

# Bounded-poll defaults (see poll_drive_dispatch_requests docstring). These
# exist so one --once tick can never again degrade into O(lifetime request
# history): a growing DISPATCH-REQUESTS folder must cost roughly the same
# per poll regardless of how much history has accumulated in it.
DEFAULT_TIME_BUDGET_SECONDS = 20.0
DEFAULT_MAX_METADATA_PAGES = 3
DEFAULT_RECENT_CANDIDATES = 8
DEFAULT_MAX_CANDIDATES_PER_POLL = 12
# Conservative clock-skew margin before trusting Drive's own metadata
# modifiedTime to skip a download outright -- always strictly more
# conservative than read_request()'s own -300s future-dated tolerance, so
# this optimization can never skip something the authoritative body check
# would have accepted.
STALE_METADATA_SKIP_MARGIN_SECONDS = 300
# Bounded, stateless fairness slice (see poll_drive_dispatch_requests
# docstring): the ~24h acceptance window (MAX_AGE_SECONDS) is divided into
# this many equal wall-clock-rotated slices, each covered by its own small,
# separately bounded Drive query.
DEFAULT_FAIRNESS_SLICES = 24
DEFAULT_FAIRNESS_SLICE_CANDIDATES = 5


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


def _modified_time_indicates_stale(metadata, now, margin_seconds=STALE_METADATA_SKIP_MARGIN_SECONDS):
    """Optimization only, never authoritative: skip an obviously-ancient
    candidate's get_media() download using Drive's own metadata
    `modifiedTime`, conservatively margined for clock skew.

    `createdTime` is NOT used here and must never be: a Drive file's
    createdTime never changes even if the file is later overwritten with a
    fresh body, so an old createdTime does not prove the current CONTENT is
    stale. `modifiedTime` reflects the most recent content write, so "old
    modifiedTime" is the only metadata-only signal that safely proves the
    body has not changed inside the accepted age window.

    A missing, malformed, or not-old-enough-to-skip modifiedTime always
    falls through to a real download -- read_request()'s own `created_at`
    request-body check remains the sole authority on staleness/future-dating
    whenever metadata alone does not prove otherwise; this can only ever
    skip a download early, never accept or reject a request."""
    if not isinstance(metadata, dict):
        return False
    modified_time = metadata.get("modifiedTime")
    if not isinstance(modified_time, str) or not modified_time:
        return False
    try:
        modified = datetime.fromisoformat(modified_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (now - modified.astimezone(timezone.utc)).total_seconds()
    return age > (MAX_AGE_SECONDS + margin_seconds)


def _fairness_slice_bounds(now, max_age_seconds=MAX_AGE_SECONDS, slices=DEFAULT_FAIRNESS_SLICES):
    """Pick one deterministic, wall-clock-rotated time slice of the ~24h
    acceptance window (now - max_age_seconds, now]. No cursor or other
    state is persisted anywhere: the slice index is derived solely from
    `now`, so a fresh process picks up exactly where the wall clock says it
    should, and the same instant always yields the same slice. Because the
    slice index only changes once per `max_age_seconds / slices` seconds
    (1 hour by default), a single slice stays selected across many
    consecutive polls, giving each slice a real window in which its
    dedicated bounded query can find and admit an eligible request."""
    slice_seconds = max_age_seconds / slices
    window_start = now - timedelta(seconds=max_age_seconds)
    slice_index = int(now.timestamp() // slice_seconds) % slices
    slice_start = window_start + timedelta(seconds=slice_index * slice_seconds)
    slice_end = slice_start + timedelta(seconds=slice_seconds)
    return slice_start, slice_end


def _fairness_slice_metadata(service, folder_id, slice_start, slice_end,
                             candidates=DEFAULT_FAIRNESS_SLICE_CANDIDATES, deadline=None):
    """One separate, small, server-side-filtered Drive `files.list()` call
    scoped to a single deterministic age slice (see `_fairness_slice_bounds`)
    inside the accepted ~24h window. This is deliberately independent of
    the main newest-first metadata listing and its `max_metadata_pages`
    bound -- a request old enough to have scrolled past that bounded
    listing can still be found here, because this query is filtered by
    Drive itself (`createdTime` range in `q`) rather than by paging through
    everything newer first. Bounded to `candidates` results and never more
    than a single page, so it costs the same regardless of how much history
    exists in the folder."""
    if deadline is not None and time.monotonic() >= deadline:
        return []
    start_str = slice_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = slice_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = (f"'{folder_id}' in parents and trashed=false and "
             f"createdTime > '{start_str}' and createdTime < '{end_str}'")
    params = {
        "q": query, "spaces": "drive",
        "fields": f"nextPageToken,files({METADATA_FIELDS})",
        "pageSize": candidates, "orderBy": "createdTime desc",
    }
    response = service.files().list(**params).execute()
    page = response.get("files") if isinstance(response, dict) else None
    if not isinstance(page, list):
        raise TaskError("malformed Drive ingress listing response")
    return page[:candidates]


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
      listing is bounded regardless of total historical file count
      (`max_metadata_pages` pages of up to 100 entries each, so
      `DEFAULT_MAX_METADATA_PAGES` = 3 caps it at 300 newest entries).
    - The front of that listing (`recent_candidates` entries) is
      considered first, so a brand-new request is never starved behind
      history.
    - An obviously-ancient candidate is skipped before ever calling
      get_media(), using Drive's own metadata `modifiedTime` (never
      `createdTime` -- a file's createdTime never changes even when its
      body is later overwritten, so it cannot prove the current content is
      stale; `modifiedTime` reflects the most recent content write and
      margined conservatively for clock skew) older than MAX_AGE_SECONDS
      plus that margin. This is purely an optimization and is
      metadata-content-safe: it can only skip a download when metadata
      itself proves the content has not changed inside the accepted body-
      age window; in every other case (including a fresh `modifiedTime`
      behind an old `createdTime`) it falls through to a real download.
      read_request()'s own request-body `created_at` check remains the
      sole authority whenever metadata alone does not conclusively prove
      staleness, and is never weakened or bypassed.
    - Between the recent pass and the in-listing tail pass below, a second,
      independent, small bounded fairness query (`_fairness_slice_metadata`)
      covers one deterministic age slice of the full ~24h acceptance window
      on every poll (see `_fairness_slice_bounds`; `DEFAULT_FAIRNESS_SLICES`
      slices, rotated by wall clock, up to `DEFAULT_FAIRNESS_SLICE_CANDIDATES`
      results per slice). This is what makes a still-valid request deeper
      than `max_metadata_pages` * 100 (i.e. never present in the bounded
      newest-first listing at all) reachable in the first place: the slice
      query is filtered server-side by Drive on a `createdTime` range in
      `q`, rather than by paging through everything newer first. It shares
      the same overall `max_candidates` ceiling as every other pass in this
      function -- that bound is never exceeded -- but is deliberately run
      *before* the tail pass below so it is not starved by it: the tail
      pass routinely has ample same-listing candidates and will spend its
      entire remaining budget every poll once it runs, which is exactly
      when a deep request needs this query's turn most. Because this query
      only actually consumes budget when its slice genuinely contains an
      eligible candidate (an empty slice, the common case, costs nothing),
      the tail pass keeps its full ordinary budget on every poll where this
      query finds nothing. Guarantee actually provided: any request inside
      the accepted window falls into exactly one of `DEFAULT_FAIRNESS_SLICES`
      deterministic slices, and that slice is queried at least once per full
      rotation (i.e. within a bounded number of poll cycles determined by
      the trigger interval), PROVIDED the volume of still-eligible
      candidates within that one slice does not exceed
      `DEFAULT_FAIRNESS_SLICE_CANDIDATES` and the recent/tail passes above
      have not already exhausted `max_candidates` for that poll (Drive
      returns that slice's own newest-first, so more candidates than the
      bound in a single slice can still push an older one out for as long
      as that slice remains oversubscribed). This is a bounded-load
      guarantee, not an unconditional no-starvation guarantee -- an
      unconditional guarantee is not achievable without persistent cursor
      state or an explicit bound on new-arrival rate, neither of which this
      function has.
    - After that, remaining budget (`max_candidates` total downloads per
      poll, shared across every pass above) is spent on the *rest* of the
      same bounded newest-first listing -- the candidates recency pushed
      just past the front. This tail fairness pass rotates deterministically
      from the wall clock (current UTC minute) through that bounded
      remainder, exactly as before this fix.
    - Every individual candidate is still fault-isolated exactly as
      before: a malformed/rejected/stale file can never abort any other
      candidate, and always resolves to `{"file_id": ..., "accepted": False}`
      rather than raising.
    - `deadline` (a `time.monotonic()` value; defaults to now +
      `time_budget_seconds`) governs a "stop starting new work after the
      deadline" admission contract, not a hard wall-clock cutoff: it is
      checked before starting the metadata listing and before starting
      each new candidate's read/dispatch or fairness-slice query, but an
      already-started Drive API call or dispatch that is in progress when
      the deadline is reached is never interrupted and can run past it.
      A true hard cutoff is out of scope here and is expected to come from
      the calling Scheduled Task's own execution time limit instead.
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
            if _modified_time_indicates_stale(metadata, current):
                continue
            _handle_one(metadata)
            used += 1

    if time.monotonic() >= deadline:
        return results

    metadata_list = _request_files(service, folder_id, order_by="createdTime desc",
                                    max_pages=max_metadata_pages, deadline=deadline)
    _scan(metadata_list[:recent_candidates], recent_candidates)

    # Deep-slice fairness query: runs between the recent pass and the
    # in-listing tail pass below, sharing the same hard `max_candidates`
    # ceiling for the whole poll (that bound is never exceeded). It is
    # placed here, ahead of the tail pass, specifically so it is not
    # starved out by it: the tail below routinely has ample same-listing
    # candidates and will happily spend its *entire* remaining budget every
    # poll once it runs, which is exactly when a request sitting deeper
    # than the bounded newest-first listing (`max_metadata_pages` * 100
    # entries -- where this query, and only this query, can still reach)
    # needs a turn. Because it only actually consumes budget when its
    # slice genuinely contains an eligible candidate (the common case is an
    # empty slice, costing nothing), the tail pass keeps its full original
    # budget on every poll where this query finds nothing.
    remaining_after_recent = max_candidates - downloads_used
    if remaining_after_recent > 0 and time.monotonic() < deadline:
        slice_start, slice_end = _fairness_slice_bounds(current)
        fairness_slice_metadata = _fairness_slice_metadata(
            service, folder_id, slice_start, slice_end,
            candidates=DEFAULT_FAIRNESS_SLICE_CANDIDATES, deadline=deadline)
        _scan(fairness_slice_metadata, min(DEFAULT_FAIRNESS_SLICE_CANDIDATES, remaining_after_recent))

    tail = metadata_list[recent_candidates:]
    fairness_budget = max_candidates - downloads_used
    if tail and fairness_budget > 0 and time.monotonic() < deadline:
        rotation = int(current.timestamp() // 60) % len(tail)
        _scan(tail[rotation:] + tail[:rotation], fairness_budget)

    return results
