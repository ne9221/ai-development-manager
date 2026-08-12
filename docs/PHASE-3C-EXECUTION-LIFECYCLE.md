# Phase 3C Execution Lifecycle

Status: acceptance definition; full lifecycle implementation blocked

This document is the single GitHub SSOT acceptance definition for Phase 3C.
It defines the lifecycle boundary between the existing Dispatcher, Scheduler,
execution records, Session Registry, runtime quota data, and Drive-backed
working-tree writer leases. It does not implement that lifecycle or expand the
scope of the reviewed components.

## Evidence baseline

This definition is derived from:

- `AI-DEVELOPMENT-RULES.md` v0.1.3, especially quota freshness, provider
  selection, 20-minute task slicing, handoff, usage evidence, and minimal-change
  requirements.
- The Dispatcher and Scheduler behavior documented in `README.md`: both create
  recommendations or batches and do not launch providers.
- The execution, task, handoff, session, and working-tree lock contracts and
  schemas.
- Session Hardening at `fix/session-adversarial-round1`
  (`efeabbc42582b267dcf4fc3c8dbd28201be66f7d`), review-pass at definition time.
- Runtime Quota Bridge at `feat/runtime-quota-bridge-p0`
  (`7953cb05e03c6cc2b93434e3703937d5ff29b7db`), review-pass at definition time.
- Working-tree Lock P0 at `feat/working-tree-lock-p0`
  (`424540bb3f6ab7f2fc0d41c4de8cdbd170807574`), awaiting Claude 3 second-round
  review at definition time.

Feature-branch references above are evidence, not permission to copy, modify,
merge, or bypass their review state.

## Scope

Phase 3C coordinates one selected task through an auditable execution lifecycle.
It reuses the existing quota, session, and lock boundaries. It does not redesign
quota collection, session classification, Drive storage, lock granularity,
provider capability scoring, cloud transports, or the Scheduler's batching
policy.

## Intended lifecycle

```text
dispatch + fresh quota decision
-> reserve execution
-> classify read-only / production-write
-> if production-write:
     local repo/origin/branch/baseline preflight
     -> authoritative acquire
-> mark execution running / task in_progress
-> launch provider
-> discover/import canonical session
-> link execution <-> session
-> renew lease while writer active
-> terminal state
-> persist execution/task/handoff
-> release lease in finally
```

The authoritative acquire is a mandatory ordering boundary for production
writes. Session discovery occurs after provider launch because the provider
owns and creates its session identity.

## Prohibited behavior

Phase 3C must never:

- mark an execution `running` or a task `in_progress` before authoritative
  acquire succeeds for production-write work;
- treat lock `check()` as write authority;
- upgrade a read-only execution directly into a writer;
- allow a provider to modify the repository before the writer lease is held;
- place an execution ID in a field that claims to contain a canonical session
  identity;
- omit release after completion, failure, interruption, cancellation, launch
  error, session-link error, persistence error, or another terminal exit;
- infer fresh quota from stale, unknown, malformed, or unavailable evidence; or
- log lease tokens, credentials, provider transcript content, or raw quota
  payloads.

## Acceptance criteria

### 1. Execution reservation

- A reservation creates a unique execution record before provider launch.
- A reservation identifies project, task, selected provider, mode/effort, task
  snapshot, and quota evidence without claiming that work is running.
- Reservation is idempotent for an explicitly supported retry identity and
  rejects conflicting duplicate execution IDs.
- Slice 2 defines that retry identity as project-scoped `execution_id`: an
  identical reservation payload returns the original record and timestamp;
  any identity, task snapshot, or quota-evidence conflict fails closed.
- Quota decision evidence is a caller-supplied, non-empty object preserved
  verbatim. Reservation does not collect runtime usage evidence.
- Reserving does not set the task to `in_progress`.

### 2. Authoritative write gate

- Every production-write execution must receive a successful authoritative
  `acquire()` result before its running transition or provider launch.
- Advisory `check()` output cannot satisfy this criterion.
- Contention or an invalid/missing registry blocks the running transition and
  provider launch.

### 3. Read-only bypass

- A task explicitly classified read-only does not request or hold a writer
  lease.
- Read-only classification must be recorded in execution audit evidence.
- A read-only execution that later requires a write must stop and create a new
  production-write reservation; it cannot upgrade in place.

### 4. Repository and baseline preflight

- Before acquire, the local working directory must match the requested
  canonical repository, full branch ref, and baseline HEAD.
- Detached HEAD, wrong origin, wrong branch, changed baseline, unsafe scope, or
  unavailable local Git evidence fails closed.
- Preflight success alone does not authorize a write.

### 5. Lease ownership

- Lease ownership must be bound to the reserved execution strongly enough that
  another project, task, execution, provider, or token cannot renew or release
  it.
- The lease token remains private and only its approved digest may be persisted.
- A pre-launch writer lease must not depend on a provider session identity that
  does not yet exist.
- The exact resolution of the current Lock P0 owner-contract mismatch is not
  selected by this document.

### 6. Provider launch ordering

- Production-write provider launch occurs only after preflight, authoritative
  acquire, and the running transition succeed.
- Read-only launch occurs only after reservation and recorded read-only
  classification.
- A launch failure enters a non-success terminal path and performs cleanup.

### 7. Session discovery and link

- After launch, provider session discovery/import uses the canonical Session
  Registry identity rules.
- The execution may link to one primary canonical session only when provider
  and project identity match.
- Linking is idempotent for the same session and rejects replacement by a
  different session.
- Failure to discover or link a session is explicit audit evidence; no execution
  ID may be substituted as a session ID.

### 8. Heartbeat and renew ownership

- The runtime component that launches/monitors the provider and privately holds
  the lease token owns heartbeat and renew.
- Dispatcher and Scheduler do not own heartbeat because they return plans and
  do not supervise provider processes.
- Renew occurs only while the production writer is active, before expiry, and
  stops at terminal transition.
- Renew failure blocks further production writing and enters a controlled
  failure/interruption path.

### 9. Terminal semantics

- `completed` records successful work, terminal timestamps, quota-after/delta,
  task completion, and a final handoff.
- `failed` records the failure and leaves the task in an explicit non-success
  state suitable for review or retry.
- `interrupted` records partial progress and does not claim acceptance criteria
  passed.
- `cancelled` records cancellation consistently on execution and task; a
  reservation cancelled before launch never launches the provider.
- Slice 6 owns any required schema-compatible status transition; earlier slices
  must not invent inconsistent terminal mappings.

### 10. Finally and release guarantee

- A production lease is released from a `finally`-equivalent boundary covering
  every exit after successful acquire.
- Release is attempted after terminal state/evidence persistence and also when
  that persistence fails.
- Release is idempotent. Release failure is surfaced and audited rather than
  hidden, without replacing the original execution outcome.
- Read-only executions never call release because they never acquire.

### 11. Quota evidence

- Dispatch reads current provider-neutral quota evidence before selection.
- Evidence records source, confidence, last-updated/freshness, and applicable
  windows or an explicit unknown/stale/unavailable state.
- Stale or unavailable quota cannot be represented as fresh or as a fabricated
  percentage.
- Reservation retains the decision evidence used; terminal processing records
  quota-after and conservative delta attribution when available.

### 12. Task, execution, and session traceability

- One execution identifies exactly one project and task.
- The task records the selected/assigned provider and lifecycle progress without
  confusing recommendation with an active execution.
- The execution records its primary canonical session when discovered.
- Handoff `from_session` contains the canonical session identity or null, never
  an execution ID; execution identity remains separately traceable.

### 13. Crash and failure behavior

- Failures before acquire leave no writer lease and do not mark running.
- Failures after acquire cannot silently continue writing after renew failure or
  lease expiry.
- Process crash recovery relies on bounded lease expiry and auditable execution
  state; it must not revive an expired lease generation.
- Retry must revalidate repository/baseline and ownership rather than trusting
  prior advisory results.

### 14. Drive unavailable fails closed

- Unavailable, malformed, or conflicting Drive lock state blocks production
  acquire and provider write.
- Quota unavailability is represented explicitly and follows the existing
  assignment degradation policy; it is never fabricated.
- Read-only work may proceed only when it does not require a Drive write that is
  itself unavailable.

### 15. Logging and audit evidence

- Audit evidence includes execution ID, project/task, provider, access class,
  baseline, lifecycle transitions, lock ID/generation without token, session
  link result, renew/release result, terminal outcome, timestamps, and quota
  evidence status.
- Logs redact secrets and use bounded error details suitable for handoff and
  incident diagnosis.
- State transitions remain reconstructable from Drive records after the runtime
  process exits.

### 16. Regression requirements

- Unit tests cover allowed and rejected transitions, read-only bypass,
  authoritative acquire ordering, contention, wrong owner/token, expiry/renew,
  all terminal paths, idempotent release, and session-link identity checks.
- Integration tests prove that no production write launch occurs without a
  successful acquire and that every post-acquire exit attempts release.
- Adversarial tests cover Drive errors, launch errors, session-link errors,
  renew failure, terminal persistence failure, cancellation, and retry.
- Existing Session Hardening, Runtime Quota Bridge, execution attribution,
  Dispatcher, Scheduler, and Lock P0 regressions remain green.

## Slice 1 ownership blocker

The Phase 3 Foundation baseline required `session_id` as part of the lease owner
contract, while a provider session is normally created only after provider
launch. Slice 1 resolves this ordering blocker by making the reserved execution
and secret token authoritative and treating `session_id` only as optional,
post-launch metadata.

Required invariant:

> A pre-launch writer lease must not depend on a provider session identity that
> does not yet exist.

The ownership blocker is resolved by Slice 1. Full Phase 3C lifecycle behavior
remains unimplemented and must proceed through Slices 2-8; no later lifecycle
guarantee is implied by the lock ownership change alone.

## Implementation slices

Every slice is independently timeboxed to no more than 20 minutes and must leave
a concise handoff with actual time and quota/token usage when available.

### Slice 0: Acceptance definition

- Prerequisite: reviewed preflight evidence and `AI-DEVELOPMENT-RULES.md` v0.1.3.
- Likely files: this document and one README index entry.
- Acceptance: one GitHub-located lifecycle, acceptance, blocker, and slice SSOT
  exists and is safe for read-only review.
- Forbidden scope: production Python, schemas, tests, feature branches, or
  implementation decisions for the unresolved lease owner contract.

### Slice 1: Resolve pre-launch lease ownership contract

- Prerequisite: Claude 3 second-round Lock P0 review completed; review findings
  incorporated or explicitly dispositioned.
- Likely files: `manager/worktree_locks.py`, lock schema, and focused lock tests.
- Acceptance: authoritative pre-launch ownership satisfies criterion 5 without
  requiring a nonexistent provider session; wrong-owner/token protections and
  CAS behavior remain intact.
- Forbidden scope: execution reservation, provider launch, session classifier
  redesign, lock granularity changes, or unrelated Drive refactoring.

### Slice 2: Execution reservation state

- Prerequisite: Session Hardening integrated; reserved-state contract agreed.
- Likely files: `manager/executions.py`, execution schema, and execution tests.
- Acceptance: reservation is distinct from running, preserves decision evidence,
  is conflict-safe, and does not mark the task `in_progress`.
- Known integration risk: cross-machine simultaneous reservation for the same
  `execution_id` is not yet backed by an atomic create-if-absent authority.
- Legacy `start` CLI remains a bypass of the future Phase 3C lifecycle and must
  be gated or retired in Slice 3 before it is a production lifecycle entrypoint.
- Forbidden scope: acquire wiring, provider launch, heartbeat, terminal release,
  Dispatcher/Scheduler refactoring, or session import changes.

### Slice 3: Reservation to authoritative acquire to running gate

- Prerequisite: Slices 1 and 2 complete; Lock registry configuration available.
- Likely files: execution lifecycle entrypoint, `manager/worktree_locks.py` only
  if its public contract requires use-site support, and focused lifecycle tests.
- Acceptance: production-write reservation passes local preflight and acquire
  before running; read-only bypass is recorded; every rejection prevents launch.
- `manager.execution_lifecycle.enter_running_gate()` is the Slice 3 lifecycle
  boundary. It stops after execution/task persistence and returns the active
  lease privately; it does not launch a provider. The legacy direct `start`
  API/CLI is retired.
- Production lock input uses the canonical Project repository plus the
  reservation snapshot's working directory, full branch, baseline, and
  non-empty `allowed_paths`; missing or conflicting evidence fails closed.
- An ordinary persistence exception after acquire rolls execution/task back and
  releases the lease. A process crash before running persistence leaves the
  reservation unchanged and relies on bounded lease expiry; crash reconciliation
  remains deferred.
- Forbidden scope: provider-specific launch logic, session linking, heartbeat,
  terminal policy, quota scoring changes, or advisory `check()` as authority.

### Slice 4: Provider launch and canonical session link

- Prerequisite: Slice 3 complete; reviewed Session Registry contract integrated.
- Likely files: execution orchestration entrypoint, `manager/executions.py`, and
  focused execution/session tests; session code only for a demonstrated contract
  defect.
- Acceptance: launch follows the gate, discovered sessions are canonical and
  provider/project matched, repeat links are idempotent, and execution IDs are
  never stored as session IDs.
- Forbidden scope: session classification redesign, transcript persistence,
  multi-session execution, heartbeat implementation, or terminal release policy.

### Slice 5: Heartbeat and renew lifecycle

- Prerequisite: Slice 4 provides a supervising runtime owner with the private
  lease token.
- Likely files: execution orchestration entrypoint and focused renew tests.
- Acceptance: the supervising runtime renews only while writing, stops at
  terminal transition, and fails safely on expiry or renew error.
- Forbidden scope: Scheduler/Dispatcher heartbeat, background service framework,
  token persistence in Drive/logs, lock redesign, or terminal outcome policy.

### Slice 6: Terminal, failure, cancellation, and finally release

- Prerequisite: Slices 3-5 complete; terminal status mapping agreed.
- Likely files: `manager/executions.py`, task/handoff integration, execution/task
  schemas if required, and focused terminal tests.
- Acceptance: completed, failed, interrupted, and cancelled outcomes satisfy
  criteria 9-10; every post-acquire exit attempts idempotent release; handoff
  uses canonical session identity.
- Forbidden scope: quota scoring, new retry scheduler, provider transcript
  storage, unrelated task workflow changes, or swallowing release errors.

### Slice 7: Quota decision integration

- Prerequisite: Runtime Quota Bridge review-pass code integrated; execution
  reservation accepts decision evidence.
- Likely files: runtime bridge/Dispatcher integration boundary, assignment or
  execution call site, and focused quota-decision tests.
- Acceptance: one fresh provider-neutral read feeds selection and reservation;
  stale/unknown/unavailable evidence degrades explicitly and remains auditable.
- Forbidden scope: collector redesign, new quota thresholds, transport/API work,
  fabricated percentages, or unrelated capability tuning.

### Slice 8: Full end-to-end and adversarial regression

- Prerequisite: Slices 1-7 complete on one integration branch.
- Likely files: focused integration/adversarial tests and minimal test helpers.
- Acceptance: criterion 16 passes across read-only and production-write happy
  paths plus contention, Drive failure, launch/link/renew/persistence failure,
  cancellation, crash recovery, and retry.
- Forbidden scope: production feature expansion, refactoring for style, new test
  framework/dependencies, transport deployment, or fixing unrelated failures.

## Implementation readiness

The first implementation task after Claude 3 review is Slice 1: resolve the
pre-launch lease ownership contract while preserving Lock P0's authoritative
CAS, token, wrong-owner, expiry, and fail-closed guarantees. Full lifecycle
wiring must not begin before that invariant is satisfied.
