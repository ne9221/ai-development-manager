"""Admit private Drive JSON requests into ADM's existing trusted ingress."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.dispatch_requests import (
    annotate_partial_identity, dispatch_rejection_by_request_registry, dispatch_rejection_registry,
    dispatch_request_registry, record_dispatch_rejection, record_dispatch_rejection_by_request,
)
from manager.tasks import MIME_FOLDER, MIME_JSON, TaskError, now_iso, validate


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
# Bounded, stateless fairness bucket rotation (see poll_drive_dispatch_requests
# docstring): time is divided into fixed ABSOLUTE UTC 1-hour buckets
# (bucket_number = floor(unix_seconds / DEFAULT_FAIRNESS_BUCKET_SECONDS), an
# absolute hour index since the epoch -- never relative to `now`). Each poll
# selects exactly one of DEFAULT_FAIRNESS_SLICES possible bucket residues
# (bucket_number % DEFAULT_FAIRNESS_SLICES) to query, rotated by wall clock,
# each covered by its own small, separately bounded Drive query.
DEFAULT_FAIRNESS_SLICES = 24
DEFAULT_FAIRNESS_BUCKET_SECONDS = 3600
DEFAULT_FAIRNESS_SLICE_CANDIDATES = 5
# Assumed poll cadence used only to pick how often the rotation advances to
# a new slot (see _fairness_rotation_slot). The real cadence is
# manager.command_watcher's own POLL_SECONDS (default 60s, configurable
# 10-900s) -- this constant does not need to match it exactly: a faster
# real cadence just revisits some slots more than once before the bucket
# they'd match changes, a slower one advances the slot less than once per
# poll. Either way this is a rotation-speed tuning knob only, never a
# correctness assumption -- see the "conditional bounded-load guarantee"
# note in poll_drive_dispatch_requests's docstring.
ASSUMED_POLL_INTERVAL_SECONDS = 60


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


def _fairness_rotation_slot(now, slices=DEFAULT_FAIRNESS_SLICES,
                            poll_interval_seconds=ASSUMED_POLL_INTERVAL_SECONDS):
    """Deterministic wall-clock rotation slot in `[0, slices)`. No cursor or
    other state is persisted anywhere: the slot is derived solely from
    `now`, so a fresh process picks up exactly where the wall clock says it
    should, and the same instant always yields the same slot. With the
    default 60s `poll_interval_seconds` and `slices=24` this advances to a
    new slot roughly once per minute, cycling through all 24 residues every
    24 minutes -- i.e. within a bounded number of polls, any one residue
    (and therefore any one absolute bucket matching it) gets its turn."""
    return int(now.timestamp() // poll_interval_seconds) % slices


def _fairness_bucket_bounds(now, bucket_seconds=DEFAULT_FAIRNESS_BUCKET_SECONDS,
                            slices=DEFAULT_FAIRNESS_SLICES,
                            poll_interval_seconds=ASSUMED_POLL_INTERVAL_SECONDS):
    """Pick ONE fixed ABSOLUTE UTC time bucket to query this poll.

    `bucket_number = floor(unix_seconds / bucket_seconds)` is an absolute
    hour index since the epoch -- it depends only on a file's own
    `modifiedTime`, NEVER on `now`. This is the key property that fixes the
    prior moving-age-window slice mechanism: a FIXED request's bucket
    number can never change as `now` advances, so it does not matter how
    much wall-clock time passes -- only WHICH bucket gets selected each
    poll rotates, a request's own bucket membership never does.

    Each poll first derives a rotation `slot` from the wall clock (see
    `_fairness_rotation_slot`), then selects the most recent absolute
    bucket `<= now`'s own bucket whose `bucket_number % slices == slot`.
    Since bucket numbers increase by exactly 1 every `bucket_seconds`,
    exactly one of the last `slices` consecutive buckets satisfies this for
    any slot, so this always resolves within `slices` buckets back from the
    current one -- i.e. within a bounded lookback of `slices * bucket_seconds`
    seconds (24 hours with the defaults), matching the overall ~24h
    acceptance window (MAX_AGE_SECONDS)."""
    slot = _fairness_rotation_slot(now, slices=slices, poll_interval_seconds=poll_interval_seconds)
    current_bucket = int(now.timestamp() // bucket_seconds)
    back = (current_bucket - slot) % slices
    bucket_number = current_bucket - back
    bucket_start = datetime.fromtimestamp(bucket_number * bucket_seconds, tz=timezone.utc)
    bucket_end = bucket_start + timedelta(seconds=bucket_seconds)
    return bucket_start, bucket_end


def _fairness_bucket_metadata(service, folder_id, bucket_start, bucket_end,
                              candidates=DEFAULT_FAIRNESS_SLICE_CANDIDATES, deadline=None):
    """One separate, small, server-side-filtered Drive `files.list()` call
    scoped to a single deterministic absolute UTC time bucket (see
    `_fairness_bucket_bounds`). This is deliberately independent of the
    main newest-first metadata listing and its `max_metadata_pages` bound
    -- a request old enough to have scrolled past that bounded listing can
    still be found here, because this query is filtered by Drive itself
    (a `modifiedTime` range in `q` -- NOT `createdTime`, so this stays
    consistent with `_modified_time_indicates_stale`'s own metadata-only
    staleness signal: a `createdTime` range would let a file's bucket
    membership be pinned forever at creation even if its body is later
    rewritten, and would also disagree with the metadata-skip check right
    above it in this module) rather than by paging through everything
    newer first. Bounded to `candidates` results and never more than a
    single page, so it costs the same regardless of how much history
    exists in the folder."""
    if deadline is not None and time.monotonic() >= deadline:
        return []
    start_str = bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = bucket_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = (f"'{folder_id}' in parents and trashed=false and "
             f"modifiedTime >= '{start_str}' and modifiedTime < '{end_str}'")
    params = {
        "q": query, "spaces": "drive",
        "fields": f"nextPageToken,files({METADATA_FIELDS})",
        "pageSize": candidates, "orderBy": "modifiedTime desc",
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
        # "utf-8-sig" strips a leading UTF-8 byte-order-mark if present and
        # is otherwise identical to "utf-8" -- several real submitters
        # (confirmed live: ChatGPT-originated Drive uploads) write a BOM
        # prefix, which plain "utf-8" decodes into a literal U+FEFF before
        # `{`, causing json.loads() to reject every such file forever
        # (Drive files are immutable once written, and every poll re-reads
        # the same bytes) with the same generic "not valid UTF-8 JSON"
        # message as truly malformed content. RFC 8259 SS8.1 explicitly
        # permits ignoring a BOM rather than treating it as an error.
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskError("Drive request is not valid UTF-8 JSON") from exc
    # Everything from here on validates an already-successfully-parsed
    # `document` -- any TaskError raised in this block is annotated with
    # partial_request_id/partial_project_id (best-effort, never trusted as
    # claim authority) so the caller's rejection handler can still durably
    # index this rejection by (project_id, request_id), not only by this
    # Drive file's own id (see annotate_partial_identity()'s docstring for
    # the visibility gap this closes).
    try:
        validate("dispatch_request", document)
        if metadata.get("name") != f'{document["request_id"]}.json':
            raise TaskError("Drive request filename does not match request_id")
        created = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        age = (current - created.astimezone(timezone.utc)).total_seconds()
        if age < -300 or age > MAX_AGE_SECONDS:
            raise TaskError("Drive request is stale or future-dated")
    except TaskError as exc:
        raise annotate_partial_identity(exc, document) from exc
    return document


def poll_drive_dispatch_requests(store, service, bucket, folder_id=None, expected_owner=None, now=None,
                                 registry_factory=dispatch_request_registry,
                                 rejection_registry_factory=dispatch_rejection_registry,
                                 rejection_by_request_registry_factory=dispatch_rejection_by_request_registry,
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
      independent, small bounded fairness query (`_fairness_bucket_metadata`)
      covers one deterministic ABSOLUTE UTC time bucket on every poll (see
      `_fairness_bucket_bounds`; `DEFAULT_FAIRNESS_SLICES` possible bucket
      residues, rotated by wall clock, up to
      `DEFAULT_FAIRNESS_SLICE_CANDIDATES` results per bucket). This is what
      makes a still-valid request deeper than `max_metadata_pages` * 100
      (i.e. never present in the bounded newest-first listing at all)
      reachable in the first place: the bucket query is filtered server-side
      by Drive on a `modifiedTime` range in `q` (never `createdTime` -- for
      the same reason `_modified_time_indicates_stale` never uses it: a
      file's `createdTime` cannot prove anything about when its current
      body was written), rather than by paging through everything newer
      first. It shares the same overall `max_candidates` ceiling as every
      other pass in this function -- that bound is never exceeded -- but is
      deliberately run *before* the tail pass below so it is not starved by
      it: the tail pass routinely has ample same-listing candidates and
      will spend its entire remaining budget every poll once it runs, which
      is exactly when a deep request needs this query's turn most. Because
      this query only actually consumes budget when its bucket genuinely
      contains an eligible candidate (an empty bucket, the common case,
      costs nothing), the tail pass keeps its full ordinary budget on every
      poll where this query finds nothing.

      Bucket selection is a STATELESS, ABSOLUTE rotation, not a window
      relative to `now`: `bucket_number = floor(unix_seconds / 3600)` is an
      absolute hour index since the epoch, derived only from a candidate's
      own `modifiedTime` -- it never moves as `now` advances. Each poll
      derives a rotation slot from the wall clock
      (`_fairness_rotation_slot`, cycling through all `DEFAULT_FAIRNESS_SLICES`
      residues roughly once per minute by default) and queries the most
      recent absolute bucket `<= now` whose `bucket_number % DEFAULT_FAIRNESS_SLICES`
      equals that slot. This fixes a real flaw in the prior relative
      moving-window slice mechanism: there, a fixed request's slice index
      was computed relative to `now`, so its own slice assignment could
      drift or be skipped as time passed. Here, a fixed request's bucket
      number is fixed forever (as long as its `modifiedTime` does not
      change), so it is guaranteed to be selected within `DEFAULT_FAIRNESS_SLICES`
      poll slots of rotation, not merely "eventually, maybe."

      Guarantee actually provided -- CONDITIONAL bounded load, NOT
      unconditional no-starvation: any request's `modifiedTime` falls into
      exactly one fixed absolute UTC hour bucket, and that bucket's residue
      is queried at least once every `DEFAULT_FAIRNESS_SLICES` poll slots
      (a bounded number of poll cycles, not "eventually" in an open-ended
      sense), PROVIDED (a) the request remains body-valid per
      `read_request()`'s own `created_at` check, (b) its `modifiedTime`
      still lies within the overall ~24h acceptance window enforced
      elsewhere, (c) the volume of still-eligible candidates within its one
      bucket does not exceed `DEFAULT_FAIRNESS_SLICE_CANDIDATES` (Drive
      returns that bucket's own newest-first, so more candidates than the
      bound in a single bucket can still push an older one out for as long
      as that bucket remains oversubscribed), and (d) the recent/tail
      passes above have not already exhausted `max_candidates` for that
      poll. An unconditional guarantee is not achievable without persistent
      cursor state or an explicit bound on new-arrival rate, neither of
      which this function has.
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
      via handle_dispatch(). A candidate rejected before ever reaching that
      registry (malformed JSON, schema-invalid payload, unverifiable
      provenance, invalid project_id/provider/account, oversized/wrong-MIME,
      or any other TaskError/DispatchIngressError _handle_one() catches) now
      durably records that rejection instead -- keyed by the Drive file's
      own id, via manager.dispatch_requests.record_dispatch_rejection() --
      so it is never silently lost with only this poll's own in-memory
      return value as evidence (see P0 dispatch-two-tick-final-20260824
      Phase 3). This is still best-effort/non-authoritative: a failure to
      record the rejection itself never masks or replaces the real
      rejection outcome already reflected in this function's return value.
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
            }
            # Explicit opt-in only, by KEY PRESENCE -- not `is not None` --
            # a request is write-mode if and only if it names its own
            # repo_write key. schema/dispatch_request.schema.json's own
            # repo_write.type is "object" (not ["object", "null"]), so
            # `"repo_write": null` already fails schema validation inside
            # read_request() above and never reaches this line at all --
            # this dict membership check is never actually asked to
            # distinguish null from absence itself, but is written as
            # membership regardless so the security contract ("only two
            # valid states: absent, or a valid object") is legible here too,
            # not just enforced one layer up. There is deliberately no
            # inference from title/goal text and no server-side default to
            # write mode. cloud.dispatch_ingress._validate_repo_write_request()
            # remains the single canonical field-level validator (path
            # safety, baseline_head hex pattern, repo identity shape) --
            # forwarding repo_write here unmodified, rather than
            # re-validating it, avoids two divergent implementations of
            # those checks ever drifting apart. A repo_write that fails
            # that canonical check raises DispatchIngressError, caught by
            # this same function's existing except clause below exactly
            # like any other rejected candidate -- it is recorded as a
            # rejection, never silently downgraded to a read-only Task.
            if "repo_write" in request:
                payload["constraints"] = {"read_only": False}
                payload["repo_write"] = request["repo_write"]
            else:
                payload["constraints"] = {"read_only": True}
            if request.get("preferred_provider") is not None:
                payload["provider"] = request["preferred_provider"]
            if request.get("account_id") is not None:
                payload["account_id"] = request["account_id"]
            # Same explicit-opt-in-by-key-presence posture as repo_write above:
            # a request names its own local_action key or it does not (never
            # inferred from title/goal text). schema/dispatch_request.
            # schema.json's own `not: {required: [local_action, repo_write]}`
            # rule already makes these two mutually exclusive before this
            # line is ever reached, so this is never asked to arbitrate a
            # conflict between them. Without this line, a Drive-submitted
            # local_action request silently fell through cloud.dispatch_
            # ingress.handle_dispatch() as an ordinary provider-dispatch
            # request instead (local_action defaulting to None there) --
            # discovered live via a real ChatGPT-facing OPEN_EXISTING_ADM_UI
            # E2E that unexpectedly went through full quota/provider
            # selection instead of the local-action fast path.
            if request.get("local_action") is not None:
                payload["local_action"] = request["local_action"]
            # SLA evidence only (see cloud.dispatch_ingress.handle_dispatch()'s
            # own docstring) -- the request body's own declared created_at,
            # threaded through as a separate argument rather than a payload
            # field, since it is not part of validate_dispatch_payload's
            # strict schema and must never be used as the Two-Tick
            # Visibility SLA's own start point.
            result = handle_dispatch(store, service, lambda project_id, request_id:
                                     registry_factory(bucket, project_id, request_id), payload,
                                     request_created_at=request.get("created_at"))
            results.append({"file_id": metadata["id"], **result})
        except (TaskError, DispatchIngressError) as exc:
            file_id = metadata.get("id") if isinstance(metadata, dict) else None
            reason_code = exc.code if isinstance(exc, DispatchIngressError) else "ingress_rejected"
            if file_id:
                try:
                    record_dispatch_rejection(rejection_registry_factory(bucket, file_id), file_id,
                                              reason_code, str(exc), now_iso())
                except Exception:
                    # Best-effort only (see docstring): never let a failure
                    # to durably record the rejection mask the real
                    # rejection outcome this poll already produced below.
                    pass
            # Mirror the same rejection by (project_id, request_id), when
            # read_request() managed to recover a plausible identity before
            # failing (see annotate_partial_identity()) -- so a caller
            # holding only the request_id (the normal case) can still
            # discover this rejection via resolve_dispatch_status_for_
            # request(), not only via this Drive file's own id. Best-effort,
            # same as the file_id-keyed record above.
            partial_request_id = getattr(exc, "partial_request_id", None)
            partial_project_id = getattr(exc, "partial_project_id", None)
            if partial_request_id and partial_project_id:
                try:
                    record_dispatch_rejection_by_request(
                        rejection_by_request_registry_factory(bucket, partial_project_id, partial_request_id),
                        partial_project_id, partial_request_id, file_id, reason_code, str(exc), now_iso())
                except Exception:
                    pass
            results.append({"file_id": file_id, "accepted": False})

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

    # Absolute-bucket fairness query: runs between the recent pass and the
    # in-listing tail pass below, sharing the same hard `max_candidates`
    # ceiling for the whole poll (that bound is never exceeded). It is
    # placed here, ahead of the tail pass, specifically so it is not
    # starved out by it: the tail below routinely has ample same-listing
    # candidates and will happily spend its *entire* remaining budget every
    # poll once it runs, which is exactly when a request sitting deeper
    # than the bounded newest-first listing (`max_metadata_pages` * 100
    # entries -- where this query, and only this query, can still reach)
    # needs a turn. Because it only actually consumes budget when its
    # bucket genuinely contains an eligible candidate (the common case is
    # an empty bucket, costing nothing), the tail pass keeps its full
    # original budget on every poll where this query finds nothing.
    remaining_after_recent = max_candidates - downloads_used
    if remaining_after_recent > 0 and time.monotonic() < deadline:
        bucket_start, bucket_end = _fairness_bucket_bounds(current)
        fairness_bucket_metadata = _fairness_bucket_metadata(
            service, folder_id, bucket_start, bucket_end,
            candidates=DEFAULT_FAIRNESS_SLICE_CANDIDATES, deadline=deadline)
        _scan(fairness_bucket_metadata, min(DEFAULT_FAIRNESS_SLICE_CANDIDATES, remaining_after_recent))

    tail = metadata_list[recent_candidates:]
    fairness_budget = max_candidates - downloads_used
    if tail and fairness_budget > 0 and time.monotonic() < deadline:
        rotation = int(current.timestamp() // 60) % len(tail)
        _scan(tail[rotation:] + tail[:rotation], fairness_budget)

    return results
