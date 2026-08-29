# C-line Stability Gate: Unattended 10-round Acceptance Harness

Status: **designed and unit-tested; live execution blocked**.

This is the C-line acceptance layer for the existing
[`DIRECT-DISPATCH-GOLDEN-E2E-TEST-PLAN.md`](DIRECT-DISPATCH-GOLDEN-E2E-TEST-PLAN.md).
The golden plan is a single manual live scenario with 15 checkpoints. The
existing `manager/dispatch_3of3_acceptance.py` is a read-only evidence
collector/evaluator for three fresh consecutive records. This harness reuses
that evaluator and adds a fixed ten-round unattended run contract.

## Contract

`manager.dispatch_10round_acceptance.run_unattended_ten_rounds()` receives two
caller-owned adapters:

```python
report = run_unattended_ten_rounds(
    project_id=project_id,
    tick_seconds=60,
    dispatch_round=dispatch_round_once,
    collect_round=collect_until_terminal,
    recorder=JsonlEvidenceRecorder("stability-gate-c-<run-id>.jsonl"),
)
```

The harness itself does not create Tasks, call an ingress, run the Watcher,
launch a provider, or touch lifecycle code. `dispatch_round()` is called once
per round, in order. `collect_round()` owns the adapter's unattended polling
until it can return one terminal evidence snapshot. Exceptions fail the
current round and the harness continues to the next round without retrying or
redispatching it.

Exactly ten unique request IDs are required. A JSONL recorder rejects a request
ID already recorded by an earlier run. All ten results are evaluated; a single
FAIL/UNKNOWN, missing round, borrowed ID, or mismatched evidence makes the
overall result FAIL.

## Evidence recorded per round

The bounded JSONL round record contains:

- claim latency: `claimed_at - ingress_first_observed_at` in seconds;
- Execution ID, `reserved`/`running`/`terminal` timestamps, status, and
  provider process provenance;
- exact Session ID matching the Execution/Task/provider;
- provider/account and a bounded output verdict (`observed`,
  `matched_expected`, verification method, optional SHA-256 digest);
- terminal state, Command/Execution terminal status, and claim/writer cleanup;
- independently observed Dashboard status and whether it matches canonical
  backend truth.

Raw prompts, transcripts, provider output, stderr, credentials, and raw error
messages are never copied into the evidence record. The provider adapter must
compute the bounded `provider_output` verdict locally and return metadata only.

## Live gate (not run in this worktree yet)

The live 10-round run is blocked until all of the following are supplied by
the coordination lane:

1. A explicitly identified A-line fix is merged/deployed or otherwise named,
   with its commit and validation evidence; C does not modify or race the
   lifecycle core.
2. Current UI v3 Command evidence proves the exact test Command is
   `claimed` + `running` and its Session is present and identity-matching.
3. The golden-plan live prerequisites are satisfied: authenticated ingress,
   same Drive/GCS configuration, running desktop Watcher, quota-eligible
   provider account, and an explicitly approved disposable read-only project.

Until that gate is satisfied, C runs only fake-store/pure unit checks. No real
provider E2E, dispatch, Scheduled Task trigger, or provider launch is part of
this branch's current evidence.

