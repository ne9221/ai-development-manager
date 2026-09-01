# Execution Convergence + AUTO_OPEN_ADM — Production E2E Acceptance Plan

Fix branch: `fix/adm-execution-convergence-auto-open-20260901` (base `804de0a`).
Scope: `manager/command_watcher.py`, `manager/worktree_locks.py` + regression tests.

This plan runs ONLY after independent review, merge to `main`, and provenance
activation (TESTED == ACTIVATED == RUNNING) on the canonical production
checkout. No step below manually repairs any Command/Execution/claim: the
acceptance object converges (or fails) under the real scheduler only
(production-fix-protocol Phase H).

## A. Natural convergence of the live stuck Command (no new dispatch needed)

`dispatch-adm-close-gh-dispatch-test-determinism-20260901T155956Z`
(project `ai-development-manager`) is, at the time of writing, stuck in the
exact defect signature: Command `status=attention`,
`recovery_reason=terminal_writer_authority_reconciliation_unknown`, Execution
`status=cancelled`, repo lock slot owned by the PREVIOUS task (released).

1. Activate the fix; do nothing else.
2. Within 1–2 natural watcher ticks the cancelled-execution reconcile path
   must terminalize it: Command `failed` with
   `result.error_kind=prelaunch_failed`, Task `blocked`
   (`prelaunch_execution_cancelled`), Execution stays `cancelled`.
3. Verify the GCS registry lock slot for the ADM repo is byte-identical to
   before (the foreign released lock must NOT be touched).
4. Re-verify after several more ticks: state stays converged (no flapping,
   no relaunch, no duplicate execution) — FP-05/Phase I durability.

## B. Fresh hands-off dispatch survives slow prelaunch (the race half)

1. Submit one normal Direct Dispatch request (bounded repo-write, any small
   real task) through the existing ingress; do not touch it afterwards.
2. Observe: Command `queued -> claimed` (worker_pid recorded) →
   Execution `reserved` → `running` → terminal, across multiple watcher
   ticks. The pass condition is that ticks landing between `reserved_at`
   and the running-gate write return `prelaunch_in_flight` skips (visible
   in watcher stdout) and never cancel the reservation while
   `claimed_at` is fresh and the worker is alive.
3. Negative control stays intact: if a worker genuinely dies pre-gate, the
   Command must still converge to `failed` within CLAIM_TIMEOUT_SECONDS
   (20 min) + 1 tick — never sit in `attention` indefinitely.

## C. AUTO_OPEN_ADM at claim time (interactive desktop)

With the user's desktop logged in and unlocked:

1. During step B's dispatch, the ADM Dashboard window must appear or come
   to foreground within seconds of the Command reaching `claimed` — not
   minutes later at `running`.
2. ADM already open in background → window is focused, no second instance,
   no second browser window (check window count before/after).
3. ADM minimized → restored + focused.
4. Repeated ticks while the same Command stays claimed/running must not
   re-steal focus (interact with another app during the run and confirm
   focus is not yanked back).
5. Locked/no-desktop session: watcher stderr shows
   `AUTO_OPEN_ADM[claimed]: no_interactive_desktop` (or similar) and the
   dispatch itself is unaffected — a failed open never blocks execution.

## Rollback trigger

Any NEW non-convergent state (a Command in `attention` whose reason did not
exist pre-fix), duplicate provider launch, or foreign-lock mutation observed
in A3 → deactivate to `804de0a` and reopen the review.

## Independent finding (out of scope here, tracked separately)

Using a feature-branch SHA as the next task's `baseline_head` fails with
`WorktreeMaterializationError: baseline_lineage_mismatch`. This is
`_verify_baseline_lineage()`'s deliberate fail-closed canonical-lineage
policy (baselines must be ancestors of the default branch), not part of the
convergence chain. Changing it would be a Git-materialization policy
decision, recorded here as an independent finding instead of patched in
passing.
