"""Thin authenticated ingress: turn an external high-level task request into
ADM's existing Task + Command contract.

This module only ever writes through manager.dispatcher.dispatch() (the
existing provider/quota decision) and manager.tasks (the existing Drive
persistence). It never launches a provider process, never calls
execution_runner/claude_launcher/codex_launcher, and never directly grants
Command Watcher launch authority -- a Command created here sits `queued`
for that existing, unmodified pipeline to pick up under its own rules.

v1 scope: every request accepted here becomes a disposable, read-only Task
(REQUIRED_TASK_POLICIES, forced server-side and never taken from the
caller's payload), stamped with trusted-ingress evidence
(manager.trusted_ingress) that the Command Watcher independently verifies
-- including cross-checking against this module's own idempotency record
-- before it will auto-admit the command without a static allowlist entry.
A caller cannot request anything else: `constraints.read_only: false` is
rejected outright, and ALLOWED_FIELDS/ALLOWED_CONSTRAINT_FIELDS make it
impossible to smuggle execution_policies or any other field into the
created record.

`provider`/`account_id` are the one deliberate exception: a trusted caller
may explicitly request a specific provider (and, for Claude, a specific
named account) instead of letting manager.dispatcher's quota-aware
auto-selector choose. This is routing evidence, not launch authority --
launch-time enforcement still lives entirely in manager.command_watcher
(trusted-ingress admission, the local Claude account registry, the
launcher's own permission profile). Here the contract is narrower and
purely about not silently discarding or overriding an explicit request:
- An explicit provider is passed straight to manager.dispatcher.dispatch()
  as preferred_provider, which already short-circuits its quota-based
  recommendation -- it is never downgraded to auto-selection.
- An explicit account_id is only accepted alongside provider="claude" (the
  only provider with multiple named accounts in this system) and is
  rejected up front unless it names a real, enabled entry in the local
  Claude account registry (CLAUDE_ACCOUNTS_CONFIG) -- fail closed rather
  than queuing a Command that could only be rejected later at launch time.
- After dispatch, the resolved provider/account_id are compared back
  against what was requested; any mismatch fails closed instead of
  silently substituting a different provider/account. Both the requested
  and the actually-assigned identity are persisted on the Command record.

`retry_of_execution_id` is the other deliberate exception: a trusted caller
may ask to retry a specific prior execution of an *existing* task instead
of this ingress's normal brand-new-task-per-request_id shape. The linkage
is never taken on faith -- a bare client-supplied id is not launch
authority:
- The prior execution is looked up strictly within the caller's own
  project_id; its task_id is read from that record, never taken from (or
  cross-checked against anything in) the caller's payload, so there is no
  field for a client to smuggle a foreign task_id through even in
  principle.
- manager.executions.retry_eligible() (the same authoritative check
  manager.executions.prepare_task_retry() itself re-verifies) decides
  whether the prior execution may be retried at all -- failed/interrupted,
  or cancelled only when structurally proven to have been cancelled by the
  trusted prelaunch-failure cleanup path. An ordinary or future
  cancellation reason is never retryable.
- retry_count is always computed server-side as one more than the prior
  execution's own retry_count; there is no field for a caller to supply or
  override it.
- provider/account_id are not accepted alongside retry_of_execution_id: a
  retry reuses the exact provider/account_id of the prior attempt's own
  Command, inherited verbatim, never re-selected or re-routed.
- No Task is created or mutated here -- the existing task_id's actual
  blocked -> ready transition is manager.executions.prepare_task_retry(),
  which already runs, unmodified, inside manager.command_watcher's normal
  claim flow once this Command is admitted. This ingress only ever writes
  the new, trusted-ingress-stamped Command that carries the linkage.
"""

import hashlib
import json
import os
import re
import time

from manager.claude_account_selector import load_claude_accounts
from manager.dispatch_requests import claim_dispatch_request, mark_dispatch_request_status
from manager.dispatcher import dispatch as dispatcher_dispatch
from manager.executions import MAX_RETRY_COUNT, linked_command_for_execution, list_executions, retry_eligible
from manager.tasks import TaskError, now_iso, update_task, validate
from manager.trusted_ingress import (
    ADMISSION_VERSION, ADMISSION_VERSION_V2_REPO_WRITE, REQUIRED_REPO_WRITE_TASK_POLICIES,
    REQUIRED_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN,
)


ALLOWED_FIELDS = {"request_id", "project_id", "title", "goal", "priority", "constraints",
                   "provider", "account_id", "retry_of_execution_id", "repo_write"}
ALLOWED_CONSTRAINT_FIELDS = {"read_only"}
ALLOWED_PRIORITIES = {"low", "normal", "high", "urgent"}
# Matches schema/command.schema.json's provider enum -- the only providers
# the Command Watcher can actually launch today.
ALLOWED_PROVIDERS = {"codex", "claude", "antigravity"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Matches schema/command.schema.json's execution_id pattern exactly (longer
# max length than the other ingress ids).
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
MAX_TITLE_LENGTH = 300
MAX_GOAL_LENGTH = 4000

# --- v2-repo-write request shape -------------------------------------------------
#
# A caller opts into bounded repo-write (constraints.read_only: false) only
# by also supplying `repo_write`, an explicit, narrow, server-validated
# request for edit authority. The server -- never the caller -- decides the
# resulting execution_policies/admission_version; `repo_write` can only ever
# name *what* the caller wants touched, not grant itself write policies
# directly (ALLOWED_REPO_WRITE_FIELDS makes it impossible to smuggle any
# other field in).
ALLOWED_REPO_WRITE_FIELDS = {"allowed_paths", "baseline_head", "repo"}
MAX_ALLOWED_PATH_ENTRIES = 100
MAX_ALLOWED_PATH_LENGTH = 300
# repo-relative POSIX-style path segments only: letters/digits/._- and `/`
# as separator. No leading `/`, no `\`, no drive letter, no glob metachar
# (`*?[]`), no `..`/`.` segment, no `.git` segment -- this pattern already
# excludes glob metacharacters and backslashes by construction (unbounded
# wildcards and Windows-style absolute paths cannot even match it).
ALLOWED_PATH_ENTRY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
# schema/task.schema.json's own baseline_head pattern: a full (not
# abbreviated) 40-char SHA-1 or 64-char SHA-256 hex commit id.
BASELINE_HEAD_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REPO_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,300}$")
# Path segments/filenames that name credentials/secrets rather than source --
# rejected regardless of case, wherever they appear in the path.
CREDENTIAL_PATH_DENYLIST = re.compile(
    r"(^|/)(\.env(\..+)?|\.npmrc|\.netrc|\.pypirc|\.aws|\.ssh|id_rsa(\..+)?|"
    r"[^/]*\.(pem|key|pfx|p12)|[^/]*(secret|credential)[^/]*)(/|$)",
    re.IGNORECASE,
)

# A claim record alone is never sufficient proof a retry can report success:
# the original claimant may have died between winning the CAS and finishing
# the Task/Command write. Before trusting a claim, bounded-retry-verify the
# canonical Task/Command it points at actually exists -- long enough to
# tolerate a still-in-flight concurrent winner (its writes are ordinary,
# non-CAS Drive puts, so a retrying caller must never attempt them itself)
# without waiting so long the ingress call blocks indefinitely.
CLAIM_VERIFICATION_ATTEMPTS = 5
CLAIM_VERIFICATION_DELAY_SECONDS = 0.02

# Bounds manager.dispatcher.dispatch()'s historical-estimate lookup
# (manager.executions.list_executions_bounded(), via the `history_deadline`
# it accepts) on THIS ingress's own request -> Task/Command creation path --
# the same class of fix manager.execution_runner.launch_task() already
# applies on the separate claimed -> reserved path (see
# DISPATCH_HISTORY_BUDGET_SECONDS there). Unlike that path, dispatch() here
# also runs manager.tasks.create_task() itself (see dispatcher.py's own
# `task = create_task(...)` call for a brand-new task_id) -- so an unbounded
# history lookup here does not just delay a launch-time estimate, it delays
# the Task record's own FIRST WRITE, i.e. the exact request -> Task
# visibility gap this task exists to close.
INGRESS_DISPATCH_HISTORY_BUDGET_SECONDS = 15.0

# Bounds manager.dispatcher.dispatch()'s own manager.quota_reader.
# read_drive_status() call (via the `quota_timeout_seconds` it accepts) on
# this ingress's request -> Task/Command creation path. A slow/hanging
# quota read can then never silently block visibility beyond this budget --
# it surfaces as a definite, durable "failed" claim-record outcome (see
# handle_dispatch()'s create()) with an exact reason instead. This never
# causes UNKNOWN quota to be guessed as any specific provider: on timeout,
# dispatch() itself never runs past the quota read, so no provider
# selection happens at all -- the request simply fails closed and becomes
# retryable, exactly like any other pre-artifact dispatch() failure.
INGRESS_QUOTA_READ_BUDGET_SECONDS = 10.0


class DispatchIngressError(TaskError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def compute_request_fingerprint(clean):
    """Canonical, order-independent digest of everything that defines what
    one request_id actually MEANS: project_id, title, goal, priority,
    provider, account_id, retry_of_execution_id, and the full repo_write
    triple (repo, baseline_head, allowed_paths). This is the durable
    evidence manager.dispatch_requests.claim_dispatch_request() persists
    once, at first claim, and compares every later attempt for the same
    request_id against (see handle_dispatch() below) -- so a second,
    materially different payload uploaded under the same request_id (e.g.
    two distinct Drive files that happen to share a filename/request_id)
    fails closed with request_identity_conflict instead of either being
    silently absorbed as if it were a legitimate retry, or silently
    redefining what the request_id means.

    Deliberately EXCLUDES the request's own declared created_at: that is
    purely additive SLA evidence (see claim_dispatch_request()'s own
    `request_created_at` parameter) and must never be treated as identity --
    resubmitting the exact same request an instant later with a fresher
    created_at is still the same request, not a conflict.

    `clean` must be validate_dispatch_payload()'s own output -- already
    fully validated and normalized (stripped strings, defaulted priority,
    canonicalized repo_write shape) -- so this only ever hashes trusted,
    schema-correct values. `allowed_paths` is sorted before hashing since
    it names an unordered set of paths, not a meaningful sequence.
    """
    repo_write = clean.get("repo_write")
    canonical = {
        "project_id": clean["project_id"],
        "title": clean["title"],
        "goal": clean["goal"],
        "priority": clean["priority"],
        "provider": clean.get("provider"),
        "account_id": clean.get("account_id"),
        "retry_of_execution_id": clean.get("retry_of_execution_id"),
        "repo_write": None if repo_write is None else {
            "repo": repo_write["repo"],
            "baseline_head": repo_write["baseline_head"],
            "allowed_paths": sorted(repo_write["allowed_paths"]),
        },
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _safe_repo_write_path(path):
    """A single repo_write.allowed_paths entry: repo-relative only, no
    traversal, no `.git`, no credential/secret path, no glob metacharacter
    (ALLOWED_PATH_ENTRY_PATTERN's charset already excludes `*?[]\\` and any
    leading `/` or drive letter -- those simply cannot match)."""
    if not isinstance(path, str) or not path or len(path) > MAX_ALLOWED_PATH_LENGTH:
        return False
    if not ALLOWED_PATH_ENTRY_PATTERN.match(path):
        return False
    segments = path.split("/")
    if any(segment in ("..", ".") for segment in segments):
        return False
    if any(segment == ".git" for segment in segments):
        return False
    if CREDENTIAL_PATH_DENYLIST.search(path):
        return False
    return True


def _validate_repo_write_request(value):
    """Validate the caller-supplied `repo_write` object and return its
    cleaned form. This only validates shape/safety of what the caller
    named; handle_dispatch() separately cross-checks `repo` against the
    Project's own registered repo before trusting it as identity evidence."""
    if not isinstance(value, dict) or set(value) != ALLOWED_REPO_WRITE_FIELDS:
        raise DispatchIngressError(
            "malformed_repo_write", f"repo_write must be an object containing exactly {sorted(ALLOWED_REPO_WRITE_FIELDS)}")
    allowed_paths = value.get("allowed_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        raise DispatchIngressError("empty_allowed_paths", "repo_write.allowed_paths must be a non-empty list of repo-relative paths")
    if len(allowed_paths) > MAX_ALLOWED_PATH_ENTRIES:
        raise DispatchIngressError("empty_allowed_paths", f"repo_write.allowed_paths must not exceed {MAX_ALLOWED_PATH_ENTRIES} entries")
    for path in allowed_paths:
        if not _safe_repo_write_path(path):
            raise DispatchIngressError(
                "unsafe_allowed_path",
                f"repo_write.allowed_paths entry is not a safe, bounded, repo-relative path: {path!r}",
            )
    baseline_head = value.get("baseline_head")
    if not isinstance(baseline_head, str) or not BASELINE_HEAD_PATTERN.match(baseline_head):
        raise DispatchIngressError("invalid_baseline_head", "repo_write.baseline_head must be a full 40 or 64 character hex commit id")
    repo = value.get("repo")
    if not isinstance(repo, str) or not repo.strip() or not REPO_IDENTITY_PATTERN.match(repo):
        raise DispatchIngressError("missing_repo_identity", "repo_write.repo must be a non-empty repo identity string")
    return {"allowed_paths": list(allowed_paths), "baseline_head": baseline_head, "repo": repo}


def validate_dispatch_payload(payload):
    if not isinstance(payload, dict) or set(payload) - ALLOWED_FIELDS:
        raise DispatchIngressError("malformed_request", "request must be an object containing only request_id, project_id, title, goal, priority, constraints")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not ID_PATTERN.match(request_id):
        raise DispatchIngressError("malformed_request", "request_id is required and must match ^[A-Za-z0-9._-]{1,128}$")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ID_PATTERN.match(project_id):
        raise DispatchIngressError("malformed_request", "project_id is required and must match ^[A-Za-z0-9._-]{1,128}$")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LENGTH:
        raise DispatchIngressError("malformed_request", f"title is required (1-{MAX_TITLE_LENGTH} chars)")
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_LENGTH:
        raise DispatchIngressError("malformed_request", f"goal is required (1-{MAX_GOAL_LENGTH} chars)")
    priority = payload.get("priority", "normal")
    if priority not in ALLOWED_PRIORITIES:
        raise DispatchIngressError("malformed_request", f"priority must be one of {sorted(ALLOWED_PRIORITIES)}")
    constraints = payload.get("constraints", {})
    if not isinstance(constraints, dict) or set(constraints) - ALLOWED_CONSTRAINT_FIELDS:
        raise DispatchIngressError("malformed_request", "constraints must be an object containing only read_only")
    read_only = constraints.get("read_only", True)
    if not isinstance(read_only, bool):
        raise DispatchIngressError("malformed_request", "constraints.read_only must be a boolean")
    repo_write_payload = payload.get("repo_write")
    if read_only is not True:
        # v1 Safe Auto-Admission only ever creates disposable read-only
        # tasks -- the caller cannot opt out of read_only, not even
        # explicitly, unless it also supplies an explicit, validated
        # `repo_write` request: that is the only way to opt into the
        # separate, explicitly versioned v2-repo-write admission contract.
        # There is deliberately no server-side override to true here: a
        # caller that wants write access without naming repo_write is
        # rejected outright, not silently downgraded.
        if repo_write_payload is None:
            raise DispatchIngressError(
                "read_only_required",
                "constraints.read_only: false requires an explicit repo_write request (v2-repo-write); "
                "read-only requests must omit constraints.read_only or set it true",
            )
        repo_write = _validate_repo_write_request(repo_write_payload)
    else:
        if repo_write_payload is not None:
            raise DispatchIngressError(
                "malformed_request", "repo_write requires constraints.read_only: false explicitly")
        repo_write = None
    provider = payload.get("provider")
    if provider is not None and (not isinstance(provider, str) or provider not in ALLOWED_PROVIDERS):
        raise DispatchIngressError("malformed_request", f"provider must be one of {sorted(ALLOWED_PROVIDERS)} or omitted")
    account_id = payload.get("account_id")
    if account_id is not None:
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 200:
            raise DispatchIngressError("malformed_request", "account_id must be a non-empty string (max 200 chars)")
        if provider != "claude":
            raise DispatchIngressError(
                "malformed_request",
                "account_id requires provider=\"claude\" explicitly (only Claude has multiple named accounts)",
            )
    retry_of_execution_id = payload.get("retry_of_execution_id")
    if retry_of_execution_id is not None:
        if not isinstance(retry_of_execution_id, str) or not EXECUTION_ID_PATTERN.match(retry_of_execution_id):
            raise DispatchIngressError(
                "malformed_request", "retry_of_execution_id must match ^[A-Za-z0-9._-]{1,200}$")
        if provider is not None or account_id is not None:
            # A retry reuses the prior attempt's own provider/account_id
            # verbatim (see linked_command_for_execution below) -- it is
            # never a fresh routing request, so combining the two would be
            # ambiguous about which one wins. Reject outright rather than
            # silently picking one.
            raise DispatchIngressError(
                "malformed_request",
                "retry_of_execution_id cannot be combined with provider/account_id",
            )
        if repo_write is not None:
            # This slice's retry path only ever relaunches a prior attempt's
            # exact existing (and, so far, always read-only) Task -- it does
            # not carry its own repo_write evidence to validate or admit
            # under. Keeping the two mutually exclusive avoids an ambiguous
            # "retry of a write task" shape this slice does not define.
            raise DispatchIngressError(
                "malformed_request", "retry_of_execution_id cannot be combined with repo_write")
    return {
        "request_id": request_id, "project_id": project_id, "title": title.strip(),
        "goal": goal.strip(), "priority": priority, "read_only": read_only, "repo_write": repo_write,
        "provider": provider, "account_id": account_id, "retry_of_execution_id": retry_of_execution_id,
    }


def _claude_account_registry():
    """Load the Claude account registry from CLAUDE_ACCOUNTS_CONFIG if set,
    else None -- mirrors manager.command_watcher's own loader exactly (same
    env var, same "no registry configured" semantics) so an explicit
    account_id is validated here against the identical source of truth the
    Command Watcher will independently re-check at launch time."""
    path = os.environ.get("CLAUDE_ACCOUNTS_CONFIG")
    return load_claude_accounts(path) if path else None


def _account_is_registered_and_enabled(registry, account_id):
    """True only if a local registry is actually configured and the id
    names one of its enabled accounts. No registry configured (None) can
    never validate an explicit id -- fail closed rather than trusting a
    bare id with nothing to resolve it against."""
    if registry is None:
        return False
    return any(account["account_id"] == account_id and account["enabled"] for account in registry)


def _fetch_if_exists(store, area, project_id, name):
    """None means the record legitimately does not exist yet; any other
    backend failure still propagates instead of being mistaken for that."""
    try:
        return store.get(area, project_id, name)
    except TaskError as exc:
        message = str(exc)
        if "found 0" in message or "not found" in message:
            return None
        raise


def _mark_pre_artifact_failure(store, registry, project_id, request_id, claim, reason):
    """When THIS call is the definite, sole owner of a not-yet-materialized
    claim (created_by_this_call, and no Task/Command/Execution exists for its
    identity), persist status="failed" + failure_reason on the existing claim
    record -- never deleted/released -- so a request that fails before ever
    producing a Task/Command still leaves a durable, queryable "failed" truth
    (manager.dispatch_requests.read_dispatch_request_status) instead of
    becoming invisible again. (task_id, command_id) is deterministic from
    request_id, so releasing the identity to allow a "fresh" claim -- the old
    design -- never actually enabled anything a same-identity retry could not
    already do; handle_dispatch()'s own status=="failed" CAS-retry path is
    that same-identity retry, done safely (see its own comment for why no
    second existence re-check is needed there).

    Deliberately a no-op (leaves status untouched) whenever ownership is not
    definite (`created_by_this_call` False, i.e. an ambiguous create
    outcome) -- an ambiguous claim's real server-side outcome is unknown, so
    it must never be marked "failed" and made retryable-in-place; it keeps
    failing closed exactly as before this change (matches
    manager.dispatch_requests' own conservative ambiguous-write contract;
    see test_ambiguous_claim_is_never_rolled_back).

    Never raises: this is observability only. Any failure to verify
    non-existence, or to read/validate/CAS-update the claim record, is
    silently swallowed -- the caller's own exception (the actual dispatch
    failure) is what propagates, never this.
    """
    if not claim.get("created_by_this_call"):
        return False
    task_id, command_id = claim["task_id"], claim["command_id"]
    try:
        if _fetch_if_exists(store, "tasks", project_id, task_id) is not None:
            return False
        if _fetch_if_exists(store, "commands", project_id, command_id) is not None:
            return False
        # Execution records are independent durable authority. list_executions()
        # treats a missing EXECUTIONS folder as a proven empty set; any real
        # backend failure here just means this best-effort marker is skipped
        # (see the bare except below), never that the caller's real failure
        # is masked.
        if any(item.get("task_id") == task_id or item.get("command_id") == command_id
               for item in list_executions(store, project_id)):
            return False
        mark_dispatch_request_status(registry, project_id, request_id, claim["generation"],
                                     "failed", failure_reason=str(reason)[:500])
    except Exception:
        pass
    return True


def _mark_retry_failure(registry, project_id, request_id, generation, reason):
    """The retry-in-place counterpart of _mark_pre_artifact_failure, for
    handle_dispatch()'s status=="failed" CAS-retry path: `generation` is the
    new generation THIS call already won by CAS-transitioning the existing
    claim record's own status from "failed" back to "accepted" before
    attempting creation again. That CAS win is itself the ownership proof --
    only one caller can ever win transitioning one specific prior generation
    forward -- so, unlike _mark_pre_artifact_failure's original-claim path,
    no separate Task/Command/Execution existence re-check is performed (and,
    deliberately, the unbounded manager.executions.list_executions() scan
    this whole task exists to stop calling unnecessarily is never re-run
    here). Never raises: same best-effort contract as above."""
    try:
        mark_dispatch_request_status(registry, project_id, request_id, generation,
                                     "failed", failure_reason=str(reason)[:500])
    except Exception:
        pass


def _repo_write_replay_matches(task, requested_repo_write):
    """Same request_id, resubmitted with a repo_write shape: this must be a
    pure idempotent replay of the *original* contract, never a channel to
    widen (or otherwise change) allowed_paths/baseline_head/repo, and never
    a way to escalate a request_id originally admitted read-only into a
    write one. Any difference at all -- not just a widening -- fails
    closed, since a request_id is only ever supposed to name one fixed
    request."""
    if task.get("read_only") is not False:
        return False
    return (sorted(task.get("allowed_paths") or []) == sorted(requested_repo_write["allowed_paths"])
            and task.get("baseline_head") == requested_repo_write["baseline_head"]
            and (task.get("source_context") or {}).get("repo") == requested_repo_write["repo"])


def _resolve_existing_claim(store, project_id, request_id, claim, clean=None):
    """The retry path for an already-claimed request_id (`claim["claimed"]
    is False`). Cases:

    - claim + Task/Command both present and consistent: idempotent replay,
      return the existing result.
    - Command not (yet) found after bounded retries: the original claimant
      may have died before finishing the write, or -- if it is instead a
      concurrent in-flight winner -- it did not finish within the retry
      budget. Either way this must not report `accepted: true` for state
      that cannot be confirmed; fail closed instead. The caller is free to
      retry the same request_id later: once the real write lands (or is
      confirmed permanently lost), this resolves deterministically.
    - Command found but its own identity does not match the claim it was
      created under (task_id/request_id linkage): fail closed rather than
      trust a record that could belong to a different, colliding claim.
    - `clean` (the newly-submitted, freshly-validated payload) names a
      repo_write contract that does not exactly match the one the original
      request created -- or claims read-only replay of an originally
      repo-write request_id, or vice versa: fail closed rather than let a
      request_id replay silently widen or change write scope.

    A retry-linked Command (retry_of_execution_id is not None) targets a
    pre-existing task, so its Task's source_context.external_request_id
    legitimately still names that task's *original* creation request, never
    this retry's request_id -- that specific cross-check is skipped only
    for retries; the task_id/request_id linkage on the Command itself is
    still fully verified either way.

    This function is only ever reached once handle_dispatch() has already
    checked `claim.get("identity_conflict")` and found it False -- so for
    any claim record written with a fingerprint (every claim created after
    compute_request_fingerprint() existed), a genuinely different `clean`
    payload has already been rejected with request_identity_conflict before
    this function ever runs. The repo_write-scope check below (`clean` names
    a different allowed_paths/baseline_head/repo, or a read_only/repo_write
    mismatch) remains as defense in depth for a claim record with no stored
    fingerprint at all -- a legacy record written before this field existed,
    where identity_conflict can never be proven either way (see
    manager.dispatch_requests._fingerprint_conflict()) -- so this narrower,
    repo_write-only check is still this function's ONLY protection for that
    case, and is deliberately left unchanged rather than removed.
    """
    task_id, command_id = claim["task_id"], claim["command_id"]
    for attempt in range(CLAIM_VERIFICATION_ATTEMPTS):
        command = _fetch_if_exists(store, "commands", project_id, command_id)
        if command is not None:
            task = _fetch_if_exists(store, "tasks", project_id, task_id)
            is_retry = command.get("retry_of_execution_id") is not None
            task_linkage_ok = is_retry or (task is not None and task.get("source_context", {}).get("external_request_id") == request_id)
            if (task is None or command.get("task_id") != task_id or command.get("request_id") != request_id
                    or not task_linkage_ok):
                raise DispatchIngressError(
                    "dispatch_state_inconsistent",
                    f"claimed request {request_id} resolves to a Task/Command whose identity does not match the claim",
                )
            if clean is not None:
                requested_repo_write = clean.get("repo_write")
                if requested_repo_write is not None and not _repo_write_replay_matches(task, requested_repo_write):
                    raise DispatchIngressError(
                        "request_replay_scope_mismatch",
                        f"request {request_id} was already claimed under a different (or non-repo-write) "
                        "contract; the same request_id cannot widen or change allowed_paths/baseline_head/repo on replay",
                    )
                if requested_repo_write is None and task.get("read_only") is False:
                    raise DispatchIngressError(
                        "request_replay_scope_mismatch",
                        f"request {request_id} was originally claimed as a repo-write request; "
                        "a read-only replay of the same request_id is rejected rather than silently downgraded",
                    )
            return {"accepted": True, "request_id": request_id, "task_id": task_id,
                    "command_id": command_id, "status": command.get("status", "queued")}
        if attempt + 1 < CLAIM_VERIFICATION_ATTEMPTS:
            time.sleep(CLAIM_VERIFICATION_DELAY_SECONDS)
    raise DispatchIngressError(
        "dispatch_incomplete",
        f"request {request_id} was claimed but its Task/Command was never confirmed created; "
        "not retryable as success -- retry the same request_id later or investigate the idempotency record",
    )


def _handle_retry_dispatch(store, lock_registry_factory, project_id, request_id, retry_of_execution_id, fingerprint=None):
    """The retry-linkage branch of handle_dispatch(): server-side-validate
    the requested linkage, then create a new, trusted-ingress-stamped
    Command for the *existing* task (never a new task), carrying
    retry_count/retry_of_execution_id for manager.command_watcher's
    existing claim-time manager.executions.prepare_task_retry() call to act
    on -- this function never touches the Task record itself and never
    launches anything.
    """
    try:
        prior = store.get("executions", project_id, retry_of_execution_id)
        validate("execution", prior)
    except TaskError as exc:
        raise DispatchIngressError(
            "unknown_execution", f"unknown execution in project {project_id}: {retry_of_execution_id}") from exc

    # task_id is read from the validated execution record, never from the
    # caller's payload -- there is no field here for a client to smuggle a
    # foreign task_id through even in principle.
    task_id = prior.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise DispatchIngressError("unknown_execution", f"execution {retry_of_execution_id} has no valid task_id")
    try:
        store.get("tasks", project_id, task_id)
    except TaskError as exc:
        raise DispatchIngressError(
            "unknown_execution", f"execution {retry_of_execution_id}'s task no longer exists") from exc

    if not retry_eligible(store, project_id, task_id, prior):
        raise DispatchIngressError(
            "retry_not_eligible",
            f"execution {retry_of_execution_id} is not eligible for retry "
            "(requires failed/interrupted, or cancelled by a proven prelaunch failure)",
        )
    # Always server-computed from the prior execution's own retry_count --
    # there is no field for a caller to supply or override this.
    retry_count = int(prior.get("retry_count", 0)) + 1
    if retry_count > MAX_RETRY_COUNT:
        raise DispatchIngressError("retry_not_eligible", f"maximum retry count ({MAX_RETRY_COUNT}) already reached")

    linked = linked_command_for_execution(store, project_id, task_id, retry_of_execution_id)
    if linked is None:
        raise DispatchIngressError(
            "retry_not_eligible", f"no unique prior Command evidence to retry execution {retry_of_execution_id} from")

    command_id = f"dispatch-{request_id}"
    try:
        registry = lock_registry_factory(project_id, request_id)
        claim = claim_dispatch_request(registry, project_id, request_id, task_id, command_id, now_iso(),
                                       fingerprint=fingerprint)
    except DispatchIngressError:
        raise
    except Exception as exc:
        raise DispatchIngressError("idempotency_backend_unavailable", "could not establish request idempotency") from exc

    if claim.get("identity_conflict"):
        raise DispatchIngressError(
            "request_identity_conflict",
            f"request_id {request_id} was already claimed with different content; refusing to reuse "
            "or reinterpret it for this retry request",
        )

    if not claim["claimed"]:
        return _resolve_existing_claim(store, project_id, request_id, claim)

    # provider/account_id are inherited verbatim from the prior attempt's
    # own Command -- a retry is never a fresh routing decision.
    command = {
        "command_id": command_id, "project_id": project_id, "task_id": task_id,
        "provider": linked["provider"], "account_id": linked.get("account_id"),
        "requested_provider": None, "requested_account_id": None,
        "model": linked.get("model"), "fallback_model": linked.get("fallback_model"),
        "mode": linked.get("mode"), "effort": linked.get("effort"),
        "selection_reason": linked.get("selection_reason", []),
        "quota_evidence": linked.get("quota_evidence"), "created_at": now_iso(), "status": "queued",
        "execution_id": None, "claimed_at": None, "completed_at": None, "result": None,
        "created_via": TRUSTED_INGRESS_ORIGIN, "admission_version": ADMISSION_VERSION, "request_id": request_id,
        "retry_count": retry_count, "retry_of_execution_id": retry_of_execution_id,
    }
    validate("command", command)
    store.put("commands", project_id, command_id, command)
    return {"accepted": True, "request_id": request_id, "task_id": task_id, "command_id": command_id, "status": "queued"}


def handle_dispatch(store, service, lock_registry_factory, payload, request_created_at=None):
    """Idempotently create a queued Task+Command for one external request and
    return its identity. Never launches a provider.

    `lock_registry_factory(project_id, request_id)` must return a
    GCSLockRegistry-compatible object (create_if_absent/read/read_if_exists).

    `request_created_at`, if given, is the request's own separately-declared
    created_at (e.g. manager.drive_dispatch_ingress's Drive JSON body
    created_at) -- purely additive SLA evidence threaded straight through to
    manager.dispatch_requests.claim_dispatch_request(); it is NOT part of
    `payload`'s own strict schema (validate_dispatch_payload's ALLOWED_FIELDS)
    since not every caller of this function has such a value, and it must
    never be used as the Two-Tick Visibility SLA's own start point (see
    manager.dashboard_core.compute_dispatch_state()'s SLA_START_POINT
    contract for why).
    """
    clean = validate_dispatch_payload(payload)
    project_id, request_id = clean["project_id"], clean["request_id"]
    try:
        project = store.get("projects", project_id, project_id)
    except TaskError as exc:
        raise DispatchIngressError("unknown_project", f"unknown project: {project_id}") from exc

    if clean["repo_write"] is not None and clean["repo_write"]["repo"] != project.get("repo"):
        # The caller's repo identity is only ever trusted once cross-checked
        # against the Project's own registered repo -- never taken on its
        # own say-so, even though its shape was already validated above.
        raise DispatchIngressError(
            "repo_identity_mismatch",
            f"repo_write.repo does not match project {project_id}'s registered repo",
        )

    # Computed once, from the fully validated/normalized `clean` payload,
    # after every content-shape and repo-identity check above has already
    # passed -- see compute_request_fingerprint()'s own docstring for why
    # this, not the Drive filename or request_id alone, is what actually
    # proves two attempts under the same request_id are the SAME request.
    fingerprint = compute_request_fingerprint(clean)

    if clean["retry_of_execution_id"] is not None:
        return _handle_retry_dispatch(store, lock_registry_factory, project_id, request_id,
                                      clean["retry_of_execution_id"], fingerprint=fingerprint)

    requested_provider, requested_account_id = clean["provider"], clean["account_id"]
    if requested_account_id is not None:
        registry = _claude_account_registry()
        if not _account_is_registered_and_enabled(registry, requested_account_id):
            raise DispatchIngressError(
                "unknown_account",
                f"unknown or disabled Claude account_id: {requested_account_id}",
            )

    task_id = command_id = f"dispatch-{request_id}"
    try:
        registry = lock_registry_factory(project_id, request_id)
        claim = claim_dispatch_request(registry, project_id, request_id, task_id, command_id, now_iso(),
                                       request_created_at=request_created_at, fingerprint=fingerprint)
    except DispatchIngressError:
        raise
    except Exception as exc:
        raise DispatchIngressError("idempotency_backend_unavailable", "could not establish request idempotency") from exc

    if claim.get("identity_conflict"):
        # This request_id was already durably claimed with a DIFFERENT
        # fingerprint -- i.e. a different project_id/title/goal/repo_write/
        # provider/account_id/priority. Two distinct Drive files can share a
        # filename and a request_id (Drive does not enforce filename
        # uniqueness) with materially different content; without this check
        # the second, losing candidate would either be silently absorbed
        # into the first claimant's already-created Task/Command (falling
        # through to _resolve_existing_claim below, which only ever compares
        # a narrower repo_write-only subset of fields, and only for
        # repo_write requests) or -- worse -- could be misread as having
        # redefined what the request_id means. Fail closed instead, with an
        # explicit, distinct reason code so this is visible in the durable
        # rejection record (manager.drive_dispatch_ingress.
        # poll_drive_dispatch_requests()'s existing exception handling
        # already records any DispatchIngressError this way, unchanged).
        raise DispatchIngressError(
            "request_identity_conflict",
            f"request_id {request_id} was already claimed with different content (project_id/title/goal/"
            "repo_write/provider/account_id/priority must match exactly for the same request_id to replay); "
            "refusing to silently reinterpret or create a second Task/Command for it",
        )

    is_repo_write = clean["repo_write"] is not None
    admission_version = ADMISSION_VERSION_V2_REPO_WRITE if is_repo_write else ADMISSION_VERSION

    def create():
        """Create the Task+Command for this claimed (task_id, command_id)
        identity. Bounds manager.dispatcher.dispatch()'s own historical-
        estimate lookup (see INGRESS_DISPATCH_HISTORY_BUDGET_SECONDS above)
        so a large project's execution history can never again delay this
        call's own Task creation (dispatch() calls manager.tasks.create_task()
        internally for a brand-new task_id, before ever returning here)."""
        internal_request = {
            "project_id": project_id, "task_id": task_id, "title": clean["title"],
            "task_type": "general", "complexity": "medium",
            # v1: this ingress is unconditionally read-only (read_only=True is
            # forced below, server-side, with no caller override). v2-repo-write:
            # needs_repo_edit=True is likewise forced here, from clean/repo_write
            # having already been fully validated above, never a raw caller flag.
            # manager.dispatcher.dispatch() has no read_only concept of its own
            # and defaults needs_repo_edit=True for any new task that doesn't
            # specify it, which would create a self-contradictory read-only Task
            # (read_only=True, needs_repo_edit=True) that
            # manager.execution_lifecycle.enter_running_gate() correctly refuses
            # to ever launch. Setting needs_repo_edit explicitly, matching the
            # request shape, keeps the Task's own contract internally consistent
            # from the moment it is first persisted.
            "needs_repo_edit": is_repo_write,
            "source_context": {
                "origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": request_id,
                "goal": clean["goal"], "admission_version": admission_version,
                **({"repo": clean["repo_write"]["repo"]} if is_repo_write else {}),
            },
        }
        if requested_provider is not None:
            # preferred_provider short-circuits manager.dispatcher's quota-based
            # recommendation outright (`selected = request.get("preferred_provider")
            # or decision["recommended_provider"] or ...`) -- an explicit request
            # here is never subject to auto-selection.
            internal_request["preferred_provider"] = requested_provider
        if requested_account_id is not None:
            internal_request["account_id"] = requested_account_id
        result = dispatcher_dispatch(store, service, internal_request,
                                     history_deadline=time.monotonic() + INGRESS_DISPATCH_HISTORY_BUDGET_SECONDS,
                                     quota_timeout_seconds=INGRESS_QUOTA_READ_BUDGET_SECONDS)

        # Defense in depth: the explicit request was already validated above,
        # but never trust that dispatcher_dispatch() actually honored it --
        # fail closed on any mismatch instead of silently persisting a Command
        # for a different provider/account than what was requested.
        if requested_provider is not None and result["provider"] != requested_provider:
            raise DispatchIngressError(
                "dispatch_state_inconsistent",
                f"requested provider {requested_provider!r} but dispatcher resolved {result['provider']!r}",
            )
        if requested_account_id is not None and result.get("account_id") != requested_account_id:
            raise DispatchIngressError(
                "dispatch_state_inconsistent",
                f"requested account_id {requested_account_id!r} but dispatcher resolved {result.get('account_id')!r}",
            )

        # read_only and execution_policies are forced here, server-side, from a
        # fixed policy set -- never from clean/payload -- so this Task always
        # satisfies the matching policy gate in manager.trusted_ingress
        # (task_policy_satisfied for v1, repo_write_policy_satisfied for
        # v2-repo-write) the Command Watcher re-checks independently before
        # ever launching it. v2-repo-write additionally stamps allowed_paths/
        # baseline_head -- the Task's own bounded-write evidence.
        if is_repo_write:
            # working_directory=None here is deliberate and load-bearing: dispatcher_
            # dispatch() above already snapshotted *some* working_directory onto
            # this Task (from the Global Project Registry when configured,
            # otherwise the Drive Project record's literal) before this ingress
            # ever knew the request was v2-repo-write. manager.execution_runner.
            # _resolve_working_directory() only materializes an isolated worktree
            # (Slice C) when a Task's working_directory is still None -- so
            # without this reset, a bounded repo-write Task would silently run
            # directly in the shared canonical checkout instead of its own
            # isolated worktree, defeating this project's own registered
            # isolation_policy (worktree_per_task).
            update_task(store, project_id, task_id, clear=("working_directory",), priority=clean["priority"],
                        read_only=False, execution_policies=sorted(REQUIRED_REPO_WRITE_TASK_POLICIES),
                        allowed_paths=clean["repo_write"]["allowed_paths"], baseline_head=clean["repo_write"]["baseline_head"])
        else:
            update_task(store, project_id, task_id, priority=clean["priority"],
                        read_only=True, execution_policies=sorted(REQUIRED_TASK_POLICIES))

        command = {
            "command_id": command_id, "project_id": project_id, "task_id": task_id,
            "provider": result["provider"], "account_id": result.get("account_id"),
            "requested_provider": requested_provider, "requested_account_id": requested_account_id,
            "model": result["model"], "fallback_model": result["fallback_model"],
            "mode": result["mode"], "effort": result["effort"], "selection_reason": result["selection_reason"],
            "quota_evidence": result["quota_evidence"], "created_at": now_iso(), "status": "queued",
            "execution_id": None, "claimed_at": None, "completed_at": None, "result": None,
            "created_via": TRUSTED_INGRESS_ORIGIN, "admission_version": admission_version, "request_id": request_id,
        }
        validate("command", command)
        store.put("commands", project_id, command_id, command)
        return {"accepted": True, "request_id": request_id, "task_id": task_id, "command_id": command_id, "status": "queued"}

    if claim["claimed"]:
        try:
            result = create()
        except Exception as exc:
            _mark_pre_artifact_failure(store, registry, project_id, request_id, claim, exc)
            raise
        mark_dispatch_request_status(registry, project_id, request_id, claim["generation"], "dispatched")
        return result

    if claim.get("status") == "failed":
        # The prior attempt for this exact request_id is confirmed dead (see
        # _mark_pre_artifact_failure: only ever set when no Task/Command/
        # Execution was found for this identity). Try to become the
        # exclusive retrier by CAS-transitioning the existing claim record's
        # own status from "failed" back to "accepted" -- only one concurrent
        # caller can ever win that transition (it targets `claim["generation"]`
        # specifically). A loser (None) falls through to the ordinary
        # bounded-polling resolution below, exactly as before this change --
        # the safe default for "someone else is already retrying, or this
        # read was stale."
        retry_generation = mark_dispatch_request_status(
            registry, project_id, request_id, claim["generation"], "accepted")
        if retry_generation is not None:
            try:
                result = create()
            except Exception as exc:
                _mark_retry_failure(registry, project_id, request_id, retry_generation, exc)
                raise
            mark_dispatch_request_status(registry, project_id, request_id, retry_generation, "dispatched")
            return result

    return _resolve_existing_claim(store, project_id, request_id, claim, clean=clean)

