# Execution Plan V1 — Failure Atlas, Structured Result Extraction, NextPlan

Status: foundation, fixture-backed, read-only. Nothing here dispatches, writes a
repo, writes Drive, or changes an existing lifecycle path.

Measured against `origin/main` @ `2a004d4`. The machine-readable inventory is
[`lifecycle-inventory.json`](lifecycle-inventory.json).

## 1. What the inventory says

ADM already knows *whether a process ended* and, for bounded repo-write tasks,
it already verifies the most important facts itself: ADM commits, pushes, reads
the remote SHA back with `git ls-remote`, and runs the task's
`validation_command` on its own (`manager/repo_write_enforcement.py`,
`manager/remote_readback.py`). That part is strong and is reused, not rebuilt.

What ADM cannot do today:

| Stage | main status | The gap |
|---|---|---|
| result ingest | PARTIAL | Only the process outcome is read. What the agent *claims* — tests, commits, blockers, next step — is never parsed. `execution_completion_report` writes a generic "Execution completed". |
| validate | PARTIAL | Repo-write only. Read-only tasks and every non-git claim are unverified. |
| classify | PARTIAL | Each launcher has its own failure strings. No canonical taxonomy, no per-class policy. |
| next plan | MISSING | `finish_execution` sends every non-completed outcome to `blocked` + "Review failure and decide whether to resume". Every failure is a human decision. |
| review / repair | MISSING | The worker→reviewer→repair loop was proven only by the external DeepCode supervisor PoC, outside this repo. |

So "a person can walk away" fails at one specific seam: after an agent stops,
ADM has no way to understand what the agent did, whether it is true, what went
wrong, and what to do next.

## 2. Boundaries

- **Verification Loop (unmerged, `manager/verification_loop/`)** decides whether
  a candidate SHA is *accepted* from checker reports. It is not merged and its
  B3 rounds are still under review. This work does not import it, copy it, or
  change it. When it merges, its derived acceptance state becomes one more input
  to NextPlan (`verification.acceptance_state`), mapped in one adapter.
  Vocabulary is kept compatible where the concepts are the same
  (`RETRY_SAME_CANDIDATE` ≈ `RETRY_SAME_AGENT`, `REPAIR` ≈ `RETURN_TO_WORKER`,
  `ESCALATE_HUMAN` ≈ `HUMAN_GATE`).
- **Loop Engineering** is not re-proven here. NextPlan encodes the loop's
  *decisions* (review after verified work, return to the original worker on a
  reviewer FAIL, fresh reviewer after repair) as a pure function.
- **No production wiring.** `execution_runner`, `finish_execution`, schemas of
  existing records and the command watcher are untouched. The new package is
  imported by nothing in production.

## 3. Target pipeline

```text
agent output (native JSON / fenced payload / free text)
  → extract      manager/nextplan/extract.py      REPORTED facts, source tier recorded
  → normalize    manager/nextplan/result.py       canonical AI result, schema-validated
  → verify       manager/nextplan/verify.py       probes upgrade REPORTED → VERIFIED
                                                   or mark CONTRADICTED; otherwise UNKNOWN
  → classify     manager/nextplan/classify.py     Failure Atlas codes, deterministic
  → plan         manager/nextplan/planner.py      one action from a finite set
  (pipeline.py strings these together, read-only, fixture-backed)
```

### Epistemic levels (every fact carries one)

| Level | Meaning | Can support completion? |
|---|---|---|
| `VERIFIED` | An independent probe confirmed it (git, ADM-run tests, remote read-back, Drive read-back). | Yes |
| `REPORTED` | The agent said it, in a structured or deterministically parsed form. | No |
| `DERIVED` | Computed from other facts or by a constrained heuristic. | No |
| `CONTRADICTED` | A probe showed the claim is false. | No — it is a failure signal |
| `UNKNOWN` | No information, or the probe could not run. | No |

Only `VERIFIED` can be positive evidence for `MARK_COMPLETE`. Heuristics can
never produce `VERIFIED`; they cap at `DERIVED`.

### NextPlan actions (finite)

`CONTINUE_WORKER`, `SEND_TO_REVIEW`, `RETURN_TO_WORKER`, `RETRY_SAME_AGENT`,
`REROUTE_AGENT`, `WAIT_DEPENDENCY`, `HUMAN_GATE`, `MARK_BLOCKED`, `MARK_FAILED`,
`MARK_COMPLETE`, plus `NO_OP` for duplicate, stale, or already-terminal inputs.

Planner rules that are tested, not just stated:

- same input → same output (pure function, no clock, no I/O);
- the planner returns a decision; it never writes anything;
- `UNKNOWN` never becomes `MARK_COMPLETE`; `MARK_COMPLETE` is emitted by one code
  path that requires a completion proof in which every item is `VERIFIED`;
- a claimed PASS contradicted by a probe is classified `false_pass`;
- every retryable failure has a finite budget; exhausting it goes to that
  failure's `terminal_if_unresolved`, never back to a retry;
- failures that need a human gate cannot be routed to an automated action.

## 4. Phases

| Phase | Deliverable | Proof |
|---|---|---|
| A | this plan + inventory | inventory is JSON, every stage cites code/tests/persistence |
| B | `failure_atlas.json` + loader/validator | schema + semantic validation tests, fixtures |
| C | AI result contract (`schema/ai_result.schema.json`) | schema tests, level semantics |
| D | extraction pipeline + fixture corpus | ≥14 corpus cases with expected output |
| E | verification layer (probe interface, git probe, fake probes) | REPORTED→VERIFIED/CONTRADICTED/UNKNOWN tests |
| F | NextPlan engine | determinism + invariant tests |
| G | 20-scenario matrix | one fixture per scenario |
| H | atlas × planner cross-check | validator test over every failure code |
| I | read-only integration pipeline | end-to-end over the corpus |
| J | adversarial review in a fresh context | findings fixed or recorded |
