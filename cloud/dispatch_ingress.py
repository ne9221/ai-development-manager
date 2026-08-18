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

import os
import re
import time

from manager.claude_account_selector import load_claude_accounts
from manager.dispatch_requests import claim_dispatch_request
from manager.dispatcher import dispatch as dispatcher_dispatch
from manager.executions import MAX_RETRY_COUNT, linked_command_for_execution, retry_eligible
from manager.tasks import TaskError, now_iso, update_task, validate
from manager.trusted_ingress import ADMISSION_VERSION, REQUIRED_TASK_POLICIES, TRUSTED_INGRESS_ORIGIN


ALLOWED_FIELDS = {"request_id", "project_id", "title", "goal", "priority", "constraints",
                   "provider", "account_id", "retry_of_execution_id"}
ALLOWED_CONSTRAINT_FIELDS = {"read_only"}
ALLOWED_PRIORITIES = {"low", "normal", "high", "urgent"}
# Matches schema/command.schema.json's provider enum -- the only providers
# the Command Watcher can actually launch today.
ALLOWED_PROVIDERS = {"codex", "claude"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Matches schema/command.schema.json's execution_id pattern exactly (longer
# max length than the other ingress ids).
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
MAX_TITLE_LENGTH = 300
MAX_GOAL_LENGTH = 4000

# A claim record alone is never sufficient proof a retry can report success:
# the original claimant may have died between winning the CAS and finishing
# the Task/Command write. Before trusting a claim, bounded-retry-verify the
# canonical Task/Command it points at actually exists -- long enough to
# tolerate a still-in-flight concurrent winner (its writes are ordinary,
# non-CAS Drive puts, so a retrying caller must never attempt them itself)
# without waiting so long the ingress call blocks indefinitely.
CLAIM_VERIFICATION_ATTEMPTS = 5
CLAIM_VERIFICATION_DELAY_SECONDS = 0.02


class DispatchIngressError(TaskError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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
    if read_only is not True:
        # v1 Safe Auto-Admission only ever creates disposable read-only
        # tasks -- the caller cannot opt out of read_only, not even
        # explicitly. There is deliberately no server-side override to
        # true here: a caller that actually wants write access is rejected
        # outright, not silently downgraded.
        raise DispatchIngressError(
            "read_only_required",
            "direct dispatch ingress v1 only accepts disposable read-only tasks; constraints.read_only must be true or omitted",
        )
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
    return {
        "request_id": request_id, "project_id": project_id, "title": title.strip(),
        "goal": goal.strip(), "priority": priority, "read_only": read_only,
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


def _resolve_existing_claim(store, project_id, request_id, claim):
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

    A retry-linked Command (retry_of_execution_id is not None) targets a
    pre-existing task, so its Task's source_context.external_request_id
    legitimately still names that task's *original* creation request, never
    this retry's request_id -- that specific cross-check is skipped only
    for retries; the task_id/request_id linkage on the Command itself is
    still fully verified either way.
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
            return {"accepted": True, "request_id": request_id, "task_id": task_id,
                    "command_id": command_id, "status": command.get("status", "queued")}
        if attempt + 1 < CLAIM_VERIFICATION_ATTEMPTS:
            time.sleep(CLAIM_VERIFICATION_DELAY_SECONDS)
    raise DispatchIngressError(
        "dispatch_incomplete",
        f"request {request_id} was claimed but its Task/Command was never confirmed created; "
        "not retryable as success -- retry the same request_id later or investigate the idempotency record",
    )


def _handle_retry_dispatch(store, lock_registry_factory, project_id, request_id, retry_of_execution_id):
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
        claim = claim_dispatch_request(registry, project_id, request_id, task_id, command_id, now_iso())
    except DispatchIngressError:
        raise
    except Exception as exc:
        raise DispatchIngressError("idempotency_backend_unavailable", "could not establish request idempotency") from exc

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


def handle_dispatch(store, service, lock_registry_factory, payload):
    """Idempotently create a queued Task+Command for one external request and
    return its identity. Never launches a provider.

    `lock_registry_factory(project_id, request_id)` must return a
    GCSLockRegistry-compatible object (create_if_absent/read/read_if_exists).
    """
    clean = validate_dispatch_payload(payload)
    project_id, request_id = clean["project_id"], clean["request_id"]
    try:
        store.get("projects", project_id, project_id)
    except TaskError as exc:
        raise DispatchIngressError("unknown_project", f"unknown project: {project_id}") from exc

    if clean["retry_of_execution_id"] is not None:
        return _handle_retry_dispatch(store, lock_registry_factory, project_id, request_id, clean["retry_of_execution_id"])

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
        claim = claim_dispatch_request(registry, project_id, request_id, task_id, command_id, now_iso())
    except DispatchIngressError:
        raise
    except Exception as exc:
        raise DispatchIngressError("idempotency_backend_unavailable", "could not establish request idempotency") from exc

    if not claim["claimed"]:
        return _resolve_existing_claim(store, project_id, request_id, claim)

    internal_request = {
        "project_id": project_id, "task_id": task_id, "title": clean["title"],
        "task_type": "general", "complexity": "medium",
        # This ingress is unconditionally read-only (read_only=True is forced
        # below, server-side, with no caller override -- see the read_only
        # rejection above). manager.dispatcher.dispatch() has no read_only
        # concept of its own and defaults needs_repo_edit=True for any new
        # task that doesn't specify it, which would create a self-contradictory
        # Task (read_only=True, needs_repo_edit=True) that
        # manager.execution_lifecycle.enter_running_gate() correctly refuses
        # to ever launch (read-only access requires needs_repo_edit is not
        # True). Setting it False here keeps the Task's own contract
        # internally consistent from the moment it is first persisted.
        "needs_repo_edit": False,
        "source_context": {
            "origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": request_id,
            "goal": clean["goal"], "admission_version": ADMISSION_VERSION,
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
    result = dispatcher_dispatch(store, service, internal_request)

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

    # read_only and execution_policies are forced here, server-side, from
    # the fixed REQUIRED_TASK_POLICIES set -- never from clean/payload --
    # so this Task always satisfies the Safe Auto-Admission policy gate
    # (manager.trusted_ingress.task_policy_satisfied) the Command Watcher
    # re-checks independently before ever launching it.
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
        "created_via": TRUSTED_INGRESS_ORIGIN, "admission_version": ADMISSION_VERSION, "request_id": request_id,
    }
    validate("command", command)
    store.put("commands", project_id, command_id, command)
    return {"accepted": True, "request_id": request_id, "task_id": task_id, "command_id": command_id, "status": "queued"}

