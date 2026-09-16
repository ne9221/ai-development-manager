# NextPlan foundation

Understand what an AI agent did, whether it is true, what failed, and what to
do next — as data and pure functions, not prose.

Read-only and fixture-backed. Nothing in `manager/nextplan/` dispatches work,
writes a repository, writes Drive, or changes an existing ADM record, and no
production module imports it. Design record:
[EXECUTION-PLAN-V1.md](EXECUTION-PLAN-V1.md); the state of the lifecycle it
plugs into is [lifecycle-inventory.json](lifecycle-inventory.json).

## The pipeline

```text
agent output
  → extract.py     native → fenced → deterministic → heuristic → none
  → verify.py      probes upgrade REPORTED to VERIFIED, or CONTRADICTED
  → classify.py    Failure Atlas codes + the completion proof
  → planner.py     one action from a closed set
  (pipeline.py composes them and keeps every intermediate for audit)
```

### Evidence levels

Every fact carries a level bound to its source, and the binding is validated
(`result.fact_problems`), so no stage can promote a claim:

| Level | Who may produce it | Counts as proof? |
|---|---|---|
| `VERIFIED` | a `probe:*` source only | yes |
| `REPORTED` | agent native / fenced / deterministic parse | no |
| `DERIVED` | a heuristic, or arithmetic over other facts | no |
| `CONTRADICTED` | a `probe:*` that disproved the claim | no — it is a failure |
| `UNKNOWN` | nothing said, or a probe could not decide | no |

`MARK_COMPLETE` has one code path and requires every item of
`classify.completion_proof` to be `VERIFIED`. UNKNOWN never completes.

### Actions

`CONTINUE_WORKER`, `SEND_TO_REVIEW`, `RETURN_TO_WORKER`, `RETRY_SAME_AGENT`,
`REROUTE_AGENT`, `WAIT_DEPENDENCY`, `HUMAN_GATE`, `MARK_BLOCKED`,
`MARK_FAILED`, `MARK_COMPLETE`, `NO_OP`.

`plan(state, event)` is pure: no clock, no I/O, no randomness, and it returns a
decision rather than performing it. `apply(state, event, decision)` is the
equally pure transition, so any sequence can be replayed in a test.

## The Failure Atlas

`failure_atlas.json` is the canonical taxonomy (53 codes across repo/git, agent
execution, test/review, SSOT sync and orchestration). Each entry carries its
detection signals, required evidence, safe default, finite retry budget,
reroute and human-gate policy, and what happens once the budget is spent.

`atlas.py` refuses an atlas that could loop forever, turn a failure into a
completion, route a human-gated failure to automation, or leave behaviour
undefined after exhaustion. `crosscheck.py` then drives the real planner for
every code, in both roles, fresh and exhausted, and reports any of those
situations plus silent drops and bypassed gates.

### What is honest about the coverage

- `crosscheck.NOT_PRODUCED` — four classes whose policy is defined but that no
  stage can raise yet (`regression_unknown`, `local_only_artifact`,
  `provenance_unknown`, `unsafe_retry`).
- `crosscheck.UNDETECTED_SIGNALS` — thirteen declared detection signals no
  stage emits yet.

Both lists are pinned by tests, so they cannot grow unnoticed.

## Boundaries

- **manager/verification_loop/** (unmerged) decides whether a candidate SHA is
  *accepted* from checker reports. This package does not import, copy or change
  it; when it merges, its derived acceptance state becomes one more planner
  input. Vocabulary is deliberately compatible where the concepts match.
- **Loop Engineering** is not re-proven here. NextPlan encodes the loop's
  decisions (review after verified work, return to the original worker on a
  reviewer FAIL, a fresh reviewer after repair) as a pure function.
- Verification happens only through probes the caller passes. With no probes,
  claims stay claims — and therefore cannot complete a task.

## Tests

| Module | Covers |
|---|---|
| `test_nextplan_atlas.py` | atlas schema + the semantic rules, 7 invalid fixtures |
| `test_nextplan_result.py` | the contract and every legal/illegal level-source binding |
| `test_nextplan_extract.py` | a 28-case corpus of real agent-output shapes |
| `test_nextplan_verify.py` | the fold rules, and GitProbe against a real temporary repository |
| `test_nextplan_planner.py` | guard order, budgets, purity, completion gating |
| `test_nextplan_scenarios.py` | the twenty required scenarios, end to end |
| `test_nextplan_crosscheck.py` | atlas × planner invariants and termination |
| `test_nextplan_pipeline.py` | the whole pipeline over the corpus, read-only |
