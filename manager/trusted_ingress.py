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
REQUIRED_TASK_POLICIES = frozenset({"disposable", "read_only", "no_repo_writes", "no_external_writes"})


def task_policy_satisfied(task):
    """Even a trusted-ingress or allowlisted task must independently prove
    it is still disposable/read-only -- this is the one gate every launch
    path shares, no matter how it got admitted."""
    if task.get("read_only") is not True:
        return False
    policies = task.get("execution_policies")
    return isinstance(policies, list) and REQUIRED_TASK_POLICIES.issubset(set(policies))


def verify_trusted_ingress_admission(store, command, bucket, registry_factory=dispatch_request_registry):
    """Return the validated Task dict if `command` is safely auto-admissible
    under the v1 trusted-ingress contract, else None.

    Fails closed -- never raises -- on any missing GCS bucket config, any
    backend error, or any mismatch between the command's self-declared
    evidence and the corroborating idempotency record.
    """
    if command.get("created_via") != TRUSTED_INGRESS_ORIGIN:
        return None
    if command.get("admission_version") != ADMISSION_VERSION:
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
    if source_context.get("external_request_id") != request_id:
        return None
    if source_context.get("admission_version") != ADMISSION_VERSION:
        return None
    if not task_policy_satisfied(task):
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
