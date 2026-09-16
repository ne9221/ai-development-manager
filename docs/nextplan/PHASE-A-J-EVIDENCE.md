# NextPlan foundation — Phase A–J evidence (2026-09-16)

Session: Claude A / Claude Code, model Claude Opus 5, autonomous implementation
+ adversarial self-check. Branch
`feat/nextplan-failure-atlas-foundation-20260916`, based on `origin/main` @
`2a004d4`. **Not merged. Not released. Not activated.**

Final status of this round: **PARTIALLY_READY**. The implementation and its own
checks are complete, but the required independent adversarial review (common
governance rule 32) did **not** run — see "Independent review" below. Nothing
here is a milestone acceptance; every PASS below is an AI self-report of a test
run, not human verification (rules 23/34).

## What this round built

`manager/nextplan/`: a read-only foundation that turns an AI agent's output
into a deterministic next action.

| Phase | Deliverable | Commit |
|---|---|---|
| A | Execution Plan V1 + machine-readable lifecycle inventory | `d7ebdd1` |
| B | Failure Atlas (53 codes) + schema + validator + invalid fixtures | `f4ea3f0` |
| C | AI result contract (agent report + normalized result) | `b120763` |
| D | Extraction pipeline + 28-case corpus | `8db4c15` |
| E | Verification layer (probes, fold rules, real-git proof) | `4b6206e` |
| F, G, I | Planner, 20-scenario matrix, read-only pipeline | `73cc180` |
| H | Atlas × planner cross-check | `1e513b7` |
| I | Corpus-wide pipeline replay + README | `50a7c69` |
| Backup 1, 3 | Property + mutation tests, governance drift audit | `02cf222` |
| Backup 4 | Extraction mappings the corpus missed (coverage) | `4e15bb6` |
| hardening | Only a reviewer may assert a verdict, in every tier | `53266ea` |

The branch is **purely additive**: 60 new files, ~7700 insertions, **zero
existing files modified**. No production module imports the new package, so no
existing ADM behaviour can change because of this branch.

## What the Phase A inventory found

Measured against `main` @ `2a004d4`, from code rather than documentation:

- result ingest **PARTIAL** — only the process outcome is read; what the agent
  *claims* is never parsed.
- validate **PARTIAL** — strong for bounded repo-write executions (ADM commits,
  pushes, reads the remote SHA back, runs the validation command itself);
  nothing for read-only or non-git claims.
- classify **PARTIAL** — ad-hoc failure strings per launcher, no taxonomy.
- next plan **MISSING** — `executions.finish_execution` sends every
  non-completed outcome to `blocked` + "Review failure and decide whether to
  resume": every failure is a human decision today.
- review / repair **MISSING** — proven only by the external DeepCode PoC.

## Test results (self-reported, reproducible)

| Run | Result |
|---|---|
| NextPlan suites (9 modules) | 161 tests, 0 failures; 653 property subtests included |
| Whole-repo regression (`manager` + `cloud`) | **5 failed, 2890 passed** in 12m21s |
| Baseline comparison at `origin/main` @ `2a004d4` | the **same 5** tests fail: 5 failed, 37 passed |
| Measured statement coverage of `manager/nextplan` | **95.0%** overall |

The 5 regression failures are therefore **pre-existing on main**, not caused by
this branch: `test_command_watcher_ag.py::test_gate_pass_reaches_launch_task`,
`test_github_dispatch_watcher.py::test_main_missing_token_env_returns_nonzero`,
and three `test_refresh_status.py::test_refresh_appends_to_history_store`. Two
further modules (`manager/test_mcp_adapter.py`, `cloud/test_asgi.py`) cannot be
collected at all in this environment because of third-party package versions
(`mcp.server.MCPServer` missing, starlette WSGI deprecation); both are
untouched by this branch and were excluded from the run.

Coverage by module: `pipeline` 100%, `vocabulary` 100%, `atlas` 99.0%,
`crosscheck` 98.6%, `result` 98.3%, `planner` 96.9%, `classify` 96.4%,
`verify` 95.3%, `extract` 88.2%. Measured with a stdlib tracer
(`ast` + `sys.settrace`), because coverage.py is not installed here.

## Defects found and fixed inside this round

Found by writing the adversarial checks, and fixed rather than documented
around:

1. **Human gate bypass** (`planner.py`, `1e513b7`). Governing failures were
   chosen by severity alone, so a critical automated failure (`false_pass`)
   outranked a human-gated one (`parallel_writer_collision`) and would have
   decided the task without a person.
2. **Generic gap outranking the specific cause** (`failure_atlas.json`,
   `73cc180`). `evidence_missing` outranked `drive_unavailable`, so a Drive
   outage would have made ADM ask the worker for evidence it could not produce
   instead of waiting.
3. **False contradiction on push state** (`verify.py`, `73cc180`). git cannot
   distinguish a failed push from no push, so an agent honestly reporting
   `push_status: failed` was being called a liar.
4. **Path normalisation bug** (`verify.py`, `4b6206e`): `lstrip("./")` would
   have mangled `./.github/x` into `github/x`.
5. **Verdict authority depended on the tier** (`extract.py`, `53266ea`): a
   worker's fenced payload could carry `review_verdict`. Inert today, dropped
   now.

## Non-vacuous proof

Four mutation controls each break one guard and assert the behaviour changes,
so the suite cannot be passing for the wrong reason: without the completion
proof, unverified work reaches review; without human-gate precedence, a writer
collision is automated; without claim comparison, a false pass goes unnoticed;
without the level binding, an agent can forge VERIFIED.

## Backup 2 — historical replay (read-only)

39 real final agent messages from this project's past sessions (written long
before this contract existed) were replayed through the extractor. Nothing was
modified and no transcript content was copied anywhere.

| Extraction tier | Count |
|---|---|
| none | 22 |
| heuristic | 16 |
| deterministic | 1 |

Not one historical output carried a structured payload, so 38/39 classify as
`malformed_result` → `CONTINUE_WORKER` (ask for a structured report), and one
`rate_limited`. A label-frequency scan of the same 39 outputs found almost no
machine-readable lines (`git status` appears twice in total): historical final
messages are prose written for a human.

**The honest headline**: the structured result contract cannot be assumed from
agents that were never asked for it. Until ADM injects the contract into
dispatch prompts, the planner will choose the safe route (ask again), every
time. That injection is the next minimal task and is deliberately **not** in
this branch.

## Independent review — NOT DONE

Two attempts to run a fresh-context read-only adversarial review were made. The
first terminated on the account session limit (HTTP 429, "resets 1pm"); a
second was launched with a different model late in the session. **No
independent review verdict is recorded in this document.** Per rule 32 the
reviewer must not be the implementer, so nothing in this round may be treated
as reviewed: the author's own checks are implementer reasoning, not review.

## Coverage, stated honestly

- Failure Atlas: 53 codes, covering all 48 classes the task required plus
  `scope_violation`, `agent_reported_blocked`, `agent_reported_fail`,
  `review_target_mismatch`, `duplicate_result`.
- 4 classes have policy but no detector yet (`crosscheck.NOT_PRODUCED`):
  `regression_unknown`, `local_only_artifact`, `provenance_unknown`,
  `unsafe_retry`.
- 13 declared detection signals are not emitted by any stage yet
  (`crosscheck.UNDETECTED_SIGNALS`).
- Both lists are pinned by tests so they cannot grow unnoticed.
- Extraction corpus: 28 cases. Scenario matrix: 20/20 required scenarios.

## Residual risks

- The planner has never run against a live ADM record. Every input in these
  tests is a fixture or a real historical transcript, never a live dispatch.
- `GitProbe` is proven against a real temporary repository, not against a real
  ADM task worktree under load.
- Drive read-back is proven with an injected reader, not against live Drive.
- The unmerged verification loop is not integrated; when it merges, the two
  vocabularies need one adapter, which does not exist yet.
- The schedule slipped: the session was paused by the account session limit and
  resumed after the 11:00 target, so the closing sequence ran in the afternoon.
