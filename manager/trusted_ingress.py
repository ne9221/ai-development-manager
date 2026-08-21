"""Shared v1 contract between cloud.dispatch_ingress (stamps evidence) and
manager.command_watcher (verifies it) for Safe Auto-Admission.

A disposable read-only Task/Command created through the authenticated
Direct Dispatch ingress may be launched by the Command Watcher without a
static ADM_WATCHER_ALLOWLIST_PATH entry -- but only after independently
proving two separate things, not one:

1. The Task/Command's own claimed evidence is self-consistent and
   satisfies the same disposable/read-only policy gate any allowlisted
   task must satisfy (REQUIRED_TASK_POLICIES).
2. That evidence is corroborated by the separate dispatch-request
   idempotency record in manager.dispatch_requests -- a store only ever
   written by the authenticated ingress path (cloud.dispatch_ingress,
   gated behind ADM_API_KEY). Fields on the Task/Command themselves are
   never sufficient alone: both collections share Drive write access with
   other, non-ingress code, so a self-declared `created_via` on a
   Task/Command record is not, by itself, proof of origin.

Any single missing/malformed/mismatched piece of evidence fails closed
(returns None from verify_trusted_ingress_admission) -- this module never
raises for an untrusted/absent record, and never grants admission on
partial evidence.
"""

from manager.dispatch_requests import dispatch_request_registry
from manager.tasks import TaskError, validate


TRUSTED_INGRESS_ORIGIN = "direct_dispatch_ingress"
ADMISSION_VERSION = "v1"
ADMISSION_VERSION_V1 = ADMISSION_VERSION
# Slice A of the Global Hands-off Execution Layer: a second, explicitly
# versioned admission contract for bounded repo-write Tasks. This slice only
# establishes the admission contract (evidence shape + cross-checks) -- it
# does not implement worktree materialization; that is a later slice built
# on top of this one.
ADMISSION_VERSION_V2_REPO_WRITE = "v2-repo-write"
SUPPORTED_ADMISSION_VERSIONS = frozenset({ADMISSION_VERSION_V1, ADMISSION_VERSION_V2_REPO_WRITE})
REQUIRED_TASK_POLICIES = frozenset({"disposable", "read_only", "no_repo_writes", "no_external_writes"})
# v2-repo-write's own bounded policy marker set -- deliberately disjoint from
# REQUIRED_TASK_POLICIES (a write task can never claim "read_only"/
# "no_repo_writes"); "bounded_repo_write" is the explicit marker that this
# Task's repo-write authority is bounded to its own allowed_paths/
# baseline_head/repo evidence, not an open-ended write grant.
REQUIRED_REPO_WRITE_TASK_POLICIES = frozenset({"disposable", "bounded_repo_write", "no_external_writes"})


def task_policy_satisfied(task):
    """Even a trusted-ingress or allowlisted task must independently prove
    it is still disposable/read-only -- this is the one gate every launch
    path shares, no matter how it got admitted."""
    if task.get("read_only") is not True:
        return False
    policies = task.get("execution_policies")
    return isinstance(policies, list) and REQUIRED_TASK_POLICIES.issubset(set(policies))


def _repo_write_evidence_valid(task):
    """The bounded repo-write evidence a v2-repo-write Task must carry,
    stamped once at creation by cloud.dispatch_ingress and never taken on
    faith afterward: a non-empty explicit allowed_paths list, an explicit
    baseline_head, and an explicit repo identity (cross-checked by the
    ingress against the Project's own registered repo at admission time,
    not re-verified here since this module has no Project access)."""
    allowed_paths = task.get("allowed_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        return False
    if not all(isinstance(path, str) and path for path in allowed_paths):
        return False
    baseline_head = task.get("baseline_head")
    if not isinstance(baseline_head, str) or not baseline_head:
        return False
    source_context = task.get("source_context")
    if not isinstance(source_context, dict):
        return False
    repo = source_context.get("repo")
    return isinstance(repo, str) and bool(repo)


def repo_write_policy_satisfied(task):
    """The v2-repo-write analogue of task_policy_satisfied(): a bounded
    repo-write Task must independently prove read_only is explicitly False,
    needs_repo_edit is explicitly True, its execution_policies carry the
    bounded-write marker set (never the read-only set), and its own
    allowed_paths/baseline_head/repo evidence is present and well-formed."""
    if task.get("read_only") is not False:
        return False
    if task.get("needs_repo_edit") is not True:
        return False
    policies = task.get("execution_policies")
    if not (isinstance(policies, list) and REQUIRED_REPO_WRITE_TASK_POLICIES.issubset(set(policies))):
        return False
    return _repo_write_evidence_valid(task)


def task_policy_satisfied_for_admission(task, admission_version):
    """Dispatch to the policy gate matching `admission_version`. Callers
    must pass ADMISSION_VERSION_V1 (never a task-supplied or command-supplied
    value) for anything admitted via the static allowlist -- a static
    allowlist entry alone must never be able to elevate a Task into the
    v2-repo-write policy gate; only a command that independently passed
    verify_trusted_ingress_admission() under v2-repo-write may use it."""
    if admission_version == ADMISSION_VERSION_V2_REPO_WRITE:
        return repo_write_policy_satisfied(task)
    return task_policy_satisfied(task)


def verify_trusted_ingress_admission(store, command, bucket, registry_factory=dispatch_request_registry):
    """Return the validated Task dict if `command` is safely auto-admissible
    under the v1 trusted-ingress contract, else None.

    Fails closed -- never raises -- on any missing GCS bucket config, any
    backend error, or any mismatch between the command's self-declared
    evidence and the corroborating idempotency record.

    A retry-linked command (retry_of_execution_id is not None) targets a
    pre-existing task, so source_context.external_request_id on that task
    legitimately still names its *original* creation request, never this
    retry's own request_id -- that one cross-check is skipped only for
    retries. Every other check, including the idempotency-record
    cross-check below (which for a retry independently proves this exact
    request_id legitimately targets this exact task_id/command_id, via a
    store only the authenticated ingress can write), still applies
    unchanged and is what actually establishes trust for a retry.
    """
    if command.get("created_via") != TRUSTED_INGRESS_ORIGIN:
        return None
    admission_version = command.get("admission_version")
    if admission_version not in SUPPORTED_ADMISSION_VERSIONS:
        # Fails closed on any version this module does not know how to
        # verify -- including a not-yet-implemented future version -- rather
        # than falling through to either policy gate.
        return None
    request_id = command.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    project_id, task_id = command.get("project_id"), command.get("task_id")
    try:
        task = store.get("tasks", project_id, task_id)
        validate("task", task)
    except TaskError:
        return None
    source_context = task.get("source_context")
    if not isinstance(source_context, dict):
        return None
    if source_context.get("origin") != TRUSTED_INGRESS_ORIGIN:
        return None
    is_retry = command.get("retry_of_execution_id") is not None
    if not is_retry and source_context.get("external_request_id") != request_id:
        return None
    # Task / Command / source_context admission_version must all agree --
    # tampering with only one of them (e.g. bumping just the Command's
    # admission_version) fails closed instead of silently admitting under
    # whichever version looks most permissive.
    if source_context.get("admission_version") != admission_version:
        return None
    if not task_policy_satisfied_for_admission(task, admission_version):
        return None
    if not bucket:
        return None
    try:
        existing = registry_factory(bucket, project_id, request_id).read_if_exists()
    except Exception:
        return None
    if existing is None:
        return None
    document, _generation, _server_time = existing
    if document.get("project_id") != project_id or document.get("request_id") != request_id:
        return None
    if document.get("task_id") != task_id or document.get("command_id") != command.get("command_id"):
        return None
    return task
