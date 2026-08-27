"""Admit GitHub-Issue-carried JSON dispatch requests into ADM's existing
trusted ingress -- a second GitHub-write-connector mechanism alongside
manager.github_dispatch_ingress's file-based one, added because some
GitHub-write connectors (confirmed: ChatGPT's) are granted Issues:write but
denied Contents:write, so a file-commit-based ingress is not reachable from
them even though the underlying repo/credential is otherwise the same.

Mechanism: an open GitHub Issue on THIS repo, opened by an allowed author
(see ALLOWED_AUTHORS_ENV), whose body is (or contains, in a ```json fenced
block) the exact same dispatch_request JSON document the file-based ingress
and Drive ingress already require. This module discovers such issues via
the GitHub REST Issues API (manager.github_dispatch_client), validates them
with the IDENTICAL schema/admission path every other ingress uses
(schema/dispatch_request.schema.json via manager.tasks.validate(), and
cloud.dispatch_ingress.handle_dispatch() for admission -- via the same
manager.github_dispatch_ingress.build_dispatch_payload() mapping, not a
second copy of it), and shares the SAME idempotency registry
(manager.dispatch_requests.dispatch_request_registry()/claim_dispatch_
request(), via handle_dispatch() -- never forked). A request submitted as
an Issue and one submitted as a file or via Drive with the same request_id
collide in that shared registry by design: all three resolve to the same
deterministic (task_id, command_id) = f"dispatch-{request_id}".

Provenance: unlike a private Drive folder (single verifiable owner) or a
dedicated file-ingress branch (repo-push access implies trust), an Issues
tracker is reachable by anyone who can open issues on this repo. The
non-negotiable extra check here is author identity, checked against the
issue's own `user.login` before the body is ever parsed as a candidate
request. ALLOWED_AUTHORS_ENV, when set, names the exact GitHub login(s)
trusted to submit a dispatch request this way and always wins outright;
when unset, this defaults to the configured repo's own owner (see
default_allowed_authors_from_repo()) -- the same implicit trust boundary
the file-based ingress already relies on (push access to this repo), so
turning this on for the repo owner needs no new required configuration.
Fails closed only if neither resolves to anything at all.

This module never launches a provider process and never imports/calls
anything from manager.command_watcher -- exactly the same non-negotiable
boundary every other ingress observes. A Command created here (indirectly,
via handle_dispatch()) sits `queued` for Command Watcher's own unmodified
pipeline to pick up under its own rules. It never comments on, labels, or
closes the source issue -- exactly like the file-based ingress never
deletes/reverts the source file; idempotency is proven solely by the
shared claim registry, not by mutating the ingress source.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from manager.dispatch_requests import dispatch_rejection_registry, dispatch_request_registry, record_dispatch_rejection
from manager.github_dispatch_client import GitHubApiError
from manager.github_dispatch_ingress import REPO_ENV, build_dispatch_payload
from manager.tasks import TaskError, now_iso, validate


ALLOWED_AUTHORS_ENV = "ADM_GITHUB_DISPATCH_ISSUE_ALLOWED_AUTHORS"
MAX_AGE_SECONDS = 86400
MAX_BODY_CHARS = 16384

# Same "one bounded call, no growth with lifetime history" discipline as
# manager.github_dispatch_ingress -- a single newest-first Issues API page.
DEFAULT_TIME_BUDGET_SECONDS = 20.0
DEFAULT_MAX_CANDIDATES_PER_POLL = 12
DEFAULT_ISSUES_PER_PAGE = 20

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def parse_allowed_authors(raw):
    """Comma-separated GitHub logins, case-insensitively compared (GitHub
    logins are themselves case-insensitive). Returns a frozenset; empty/
    unset input yields an empty frozenset, which the caller must treat as
    "not explicitly configured" -- resolve_allowed_authors() is what
    decides what that means (default-to-repo-owner), never this function
    on its own."""
    if not raw or not isinstance(raw, str):
        return frozenset()
    return frozenset(login.strip().lower() for login in raw.split(",") if login.strip())


def default_allowed_authors_from_repo(repo):
    """The sole built-in trust default when ALLOWED_AUTHORS_ENV is not
    explicitly set: the repo's own owner (the "owner" segment of an
    "owner/repo" identity string, e.g. "ne9221" from
    "ne9221/ai-development-manager"). This is a deliberate, narrow default
    -- not "allow everyone who can open an issue" -- chosen because the
    repo owner is already the exact same trust boundary the file-based
    ingress implicitly relies on (only someone with push access to this
    repo can write a request file at all); defaulting to that same identity
    here means no NEW required configuration (a second Scheduled Task
    install/env var) is needed just to turn this ingress on for the
    repo's own owner. An explicit ALLOWED_AUTHORS_ENV always overrides
    this default outright, never merges with it."""
    if not isinstance(repo, str) or "/" not in repo:
        return frozenset()
    owner = repo.split("/", 1)[0].strip().lower()
    return frozenset({owner}) if owner else frozenset()


def resolve_allowed_authors(repo, explicit=None, env_value=None):
    """explicit (a caller-supplied frozenset/set/None) wins outright over
    everything else -- used by tests and by any future caller that wants
    total control. Otherwise: an explicitly configured ALLOWED_AUTHORS_ENV
    (`env_value`) wins over the repo-owner default. Only when NEITHER is
    set does this fall back to default_allowed_authors_from_repo(repo)."""
    if explicit is not None:
        return frozenset(login.strip().lower() for login in explicit)
    configured = parse_allowed_authors(env_value)
    if configured:
        return configured
    return default_allowed_authors_from_repo(repo)


def _verify_repo_identity(client, repo):
    """Provenance check adapted to the Issues API: no branch/directory
    concept exists here, so this is only the repo-identity half of
    manager.github_dispatch_ingress.verify_ingress_repo() -- the issue-
    author allowlist check in read_request() below is what actually gates
    who may submit a request this way."""
    try:
        repository = client.get_repo(repo)
    except GitHubApiError as exc:
        raise TaskError("GitHub issue ingress repo is missing or unverifiable") from exc
    if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str) \
            or repository["full_name"].lower() != repo.lower():
        raise TaskError("GitHub issue ingress repo provenance is ambiguous or unverifiable")
    return repository


def _extract_json_document(body):
    """A dispatch request Issue's body is either the raw JSON document
    itself, or that same JSON inside one ```json fenced code block (many
    GitHub-write connectors, including ChatGPT's, tend to wrap structured
    content in markdown fencing when composing an issue body) -- both are
    accepted; nothing else is. Fails closed (TaskError) on anything that
    does not parse as JSON after that one optional unwrap."""
    if not isinstance(body, str) or not body.strip():
        raise TaskError("GitHub issue body is missing or empty")
    if len(body) > MAX_BODY_CHARS:
        raise TaskError("GitHub issue body is outside the accepted size range")
    text = body.strip()
    match = _JSON_FENCE_RE.search(text)
    candidate = match.group(1).strip() if match else text
    try:
        document = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise TaskError("GitHub issue body is not valid dispatch-request JSON") from exc
    if not isinstance(document, dict):
        raise TaskError("GitHub issue body JSON must be an object")
    return document


def read_request(issue, allowed_authors, now=None):
    """Verify author provenance, extract, and validate one candidate
    Issue's body -- the Issue-shape counterpart of
    manager.github_dispatch_ingress.read_request()."""
    if not isinstance(issue, dict) or "pull_request" in issue:
        raise TaskError("GitHub issues listing entry is not an issue")
    author = (issue.get("user") or {}).get("login") if isinstance(issue.get("user"), dict) else None
    if not isinstance(author, str) or author.strip().lower() not in allowed_authors:
        raise TaskError("GitHub issue author is not an allowed dispatch requester")
    document = _extract_json_document(issue.get("body"))
    validate("dispatch_request", document)
    created = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    age = (current - created.astimezone(timezone.utc)).total_seconds()
    if age < -300 or age > MAX_AGE_SECONDS:
        raise TaskError("GitHub issue dispatch request is stale or future-dated")
    return document


def poll_github_issue_dispatch_requests(store, service, bucket, client, repo=None, allowed_authors=None, now=None,
                                        registry_factory=dispatch_request_registry,
                                        rejection_registry_factory=dispatch_rejection_registry,
                                        max_candidates=DEFAULT_MAX_CANDIDATES_PER_POLL,
                                        issues_per_page=DEFAULT_ISSUES_PER_PAGE,
                                        deadline=None, time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS):
    """Bounded, fault-isolated poll of open GitHub Issues on `repo` for
    dispatch requests -- the Issue-shape counterpart of
    manager.github_dispatch_ingress.poll_github_dispatch_requests(). See
    that function's docstring for the shared contract (bounded work,
    per-candidate fault isolation, durable rejection recording, never
    mutates the ingress source, shared idempotency registry); this
    docstring only calls out what differs.

    `store`/`service` are the EXISTING Drive-backed record store / Drive
    service, exactly as every other ingress uses them -- unrelated to
    GitHub, but still required by handle_dispatch(). `client` is a
    manager.github_dispatch_client.GitHubApiClient (or test double with
    the same list_issues/get_repo shape).

    Differences from the file-based ingress:
    - One bounded Issues API page (`issues_per_page`, newest-first) instead
      of a directory listing; no rotation is needed since a single page is
      already the full bounded candidate set for one poll.
    - Provenance is repo identity (`_verify_repo_identity`) PLUS a required
      author allowlist (`allowed_authors` / ALLOWED_AUTHORS_ENV) checked
      per-issue in read_request() -- an Issues tracker has no equivalent of
      "push access to a dedicated branch" as an implicit trust boundary.
      An explicit ALLOWED_AUTHORS_ENV always wins; when unset, this
      defaults to the configured repo's own owner (see
      default_allowed_authors_from_repo()) -- the same implicit trust
      boundary the file-based ingress already relies on, so no new
      required configuration is needed just to use this for the repo
      owner. This still fails closed (TaskError) in the one case neither
      resolves anything (e.g. a malformed `repo` string with no "/").
    - Rejection records are keyed by the issue's own global `id` (stable,
      unique across the whole GitHub instance -- GitHub's analogue of the
      file-based ingress's blob `sha`), not by issue `number` (which is
      only unique within one repo and is reused if issues are ever
      transferred).
    - Pull requests, which the Issues API also returns, are filtered out by
      read_request() before any JSON parsing is attempted.
    """
    repo = repo or os.environ.get(REPO_ENV)
    if not repo:
        raise TaskError(f"{REPO_ENV} is required")
    allowed_authors = resolve_allowed_authors(repo, explicit=allowed_authors, env_value=os.environ.get(ALLOWED_AUTHORS_ENV))
    if not allowed_authors:
        raise TaskError(f"{ALLOWED_AUTHORS_ENV} is required (and {REPO_ENV}'s own owner could not be derived as a default)")
    _verify_repo_identity(client, repo)
    current = now or datetime.now(timezone.utc)
    if deadline is None:
        deadline = time.monotonic() + time_budget_seconds

    results = []

    def _handle_one(issue):
        issue_id = str(issue.get("id")) if isinstance(issue, dict) and issue.get("id") is not None else None
        try:
            request = read_request(issue, allowed_authors, now=current)
            payload = build_dispatch_payload(request)
            result = handle_dispatch(store, service, lambda project_id, request_id:
                                     registry_factory(bucket, project_id, request_id), payload,
                                     request_created_at=request.get("created_at"))
            results.append({"issue_id": issue_id, "issue_number": issue.get("number"), **result})
        except (TaskError, DispatchIngressError) as exc:
            reason_code = exc.code if isinstance(exc, DispatchIngressError) else "ingress_rejected"
            if issue_id:
                try:
                    record_dispatch_rejection(rejection_registry_factory(bucket, issue_id), issue_id,
                                              reason_code, str(exc), now_iso())
                except Exception:
                    # Best-effort only, matching every other ingress's
                    # established contract: never let a failure to durably
                    # record the rejection mask the real rejection outcome
                    # this poll already produced below.
                    pass
            results.append({"issue_id": issue_id, "accepted": False})

    if time.monotonic() >= deadline:
        return results

    try:
        issues = client.list_issues(repo, state="open", per_page=issues_per_page)
    except GitHubApiError as exc:
        raise TaskError("GitHub issue ingress listing failed") from exc

    processed = 0
    for issue in issues:
        if processed >= max_candidates or time.monotonic() >= deadline:
            break
        processed += 1
        _handle_one(issue)

    return results
