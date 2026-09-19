# Round 7 evidence — measurements, not intentions

Status: **IMPLEMENTED, PENDING INDEPENDENT REVIEW. DO NOT MERGE / ACTIVATE / RELEASE.**

Companion to [`REMEDIATION-ROUND-7-20260920.md`](REMEDIATION-ROUND-7-20260920.md)
and [`TRUST-MODEL.md`](TRUST-MODEL.md) §7.

Every number below was produced by running something. Where a number differs
from the one the independent review reported, the one observed here is the one
printed, and the difference is explained rather than smoothed over.

## 1. Preflight

| check | result |
|---|---|
| Governance SSOT | Drive `AI-DEVELOPMENT-RULES` **v0.5.0** (2026-09-01), `PROJECT-RULES — ADM` **v1.1.0** (2026-08-21) — both read from Drive, not from the repo copy |
| Branch | `feat/nextplan-failure-atlas-foundation-20260916` |
| Expected reviewed HEAD | `899d383e9b808581a1e14a36bef83b7e02b4694e` |
| Remote HEAD at preflight | `899d383e9b808581a1e14a36bef83b7e02b4694e` — **equal, no lineage drift** |
| `git status --porcelain` at clone | empty |
| Production runtime HOME | not touched; no `~/.ai-development-manager` exists on this machine |

Lineage cross-check: the base clone reproduces Round 6's reported NextPlan
totals exactly — Round 6 recorded "372 passed, 1626 subtests" for `-k nextplan`,
and this base run gives **371 passed + 1 environment failure = 372, 1626
subtests**.

### Quota evidence

Rule 11 and rule 30 require fresh evidence with source, confidence and
`last_updated`, and forbid guessing.

| provider | source | confidence | reading | last_updated |
|---|---|---|---|---|
| Claude Code (this session) | first-party session usage API | high | plan **Max**; 5-hour **40%** used (resets 2026-09-19T19:40Z); weekly all-models **32%**; weekly per-model **33%**; extra usage disabled | 2026-09-19T17:22Z (2026-09-20 01:22 Asia/Taipei) |
| Codex | none found | — | **UNKNOWN** | — |
| Gemini / Google AI Pro | none found | — | **UNKNOWN** | — |
| Antigravity | none found | — | **UNKNOWN** | — |

No fresh evidence was found for the three non-Claude providers, so they are
reported UNKNOWN rather than estimated.

## 2. Reproduced at base before any edit

`scratchpad/repro_r7.py`, run against an unmodified clone pinned at `899d383e`,
then against head. Full captures: `repro_base.txt`, `repro_head.txt`.

The scratchpad scripts are session-local, as in Round 6. **Every corpus they
measure is also pinned as a test** in `manager/test_nextplan_round7_findings.py`,
which is the durable and re-runnable form: an independent reviewer does not need
the scripts to re-derive any number below, only to see how it was first
obtained.

Every case ends at a **planner action**, because a helper returning `False` is
not a gate.

### R6-IR-1 — decision-field grammar (HIGH)

| corpus | base `899d383e` | head |
|---|---|---|
| the 6 fields the review named | 6 read as **0 statements**; 5 MARK_COMPLETE | 6 read; **0** MARK_COMPLETE |
| 4 fresh mutations not in the review's corpus | 4 read as **0 statements**; 4 MARK_COMPLETE | 4 read; **0** MARK_COMPLETE |
| 2 known-good controls | read; 0 MARK_COMPLETE | read; 0 MARK_COMPLETE |
| **total false completions** | **9 / 12** | **0 / 12** |

The review asserted `decision_statements(...) == []` for its six sentences.
That is confirmed exactly: all six read as zero statements at base.

**Why 9/12 and not 12/12.** Three of the twelve were routed away from
completion at base by rules unrelated to the contract under test. 9/12 is what
was observed here; it is not the reviewer's number and is not presented as one.

### R6-IR-2 — summary contradiction (MEDIUM)

| case | base | head |
|---|---|---|
| bound PASS + `summary: "Current decision: reject"` | `authorized=True`, MARK_COMPLETE | `reason=conflict`, SEND_TO_REVIEW |
| bound PASS + `summary: "My final decision: reject"` | `authorized=True`, MARK_COMPLETE | `reason=conflict`, SEND_TO_REVIEW |
| bound PASS + `summary: "I reject this patch."` | `authorized=True`, MARK_COMPLETE | `reason=conflict`, SEND_TO_REVIEW |
| **false completions** | **3 / 3** | **0 / 3** |

The three ordinary summaries the review named as needing protection —
*"The previous blocker was fixed."*, *"This review rejects the old approach, but
the submitted patch now satisfies the contract."*, *"No blocking issues
remain."* — complete at base **and** at head. They read as zero decision
statements: no second rejection scanner was built.

### R6-IR-3 — wrong-channel fence (MEDIUM)

Bound authoritative PASS, plus an ordinary fence quoting a rejection.

| fence | base | head |
|---|---|---|
| ` ```json ` `{"verdict": "REJECT"}` | SEND_TO_REVIEW (**stall**) | MARK_COMPLETE |
| ` ```yaml ` `verdict: REJECT` | SEND_TO_REVIEW (**stall**) | MARK_COMPLETE |
| ` ```text ` `Current decision: reject` | HUMAN_GATE (**stall**) | MARK_COMPLETE |
| ` ```markdown ` rejection sentence | SEND_TO_REVIEW (**stall**) | MARK_COMPLETE |
| ` ```example ` rejection sentence | SEND_TO_REVIEW (**stall**) | MARK_COMPLETE |
| **stalls** | **5 / 5** | **0 / 5** |

The test suite covers seven fence languages (adding a bare fence and
` ```console `), all inert in both directions.

The authoritative channel still fails closed at head, unchanged:

| authoritative `adm-review-result` block | head |
|---|---|
| `verdict: REJECT` | RETURN_TO_WORKER |
| `PASS` with a blocking finding | SEND_TO_REVIEW |
| unknown verdict (`MAYBE`) | SEND_TO_REVIEW |
| unknown field (`can_merge: false`) | does not complete |
| wrong `reviewer_run_id` | does not complete |
| malformed / unparseable JSON | does not complete |

### R6-IR-4 — authority call convergence

At base, `classify.review_proof` passed `decision_statements` to
`contracts.review_authority` and `classify.signals_for` did not. At head both
go through `classify.review_authority_for`, and a test asserts that neither
function calls `contracts.review_authority` directly.

**The convergence has one visible consequence, and it is an improvement.** A
contradicted PASS at base reached the planner only through `review_proof`, fell
to the generic catch-all `review result neither failed nor approved a verified
candidate`, and became HUMAN_GATE. At head it arrives as the atlas code written
for it:

```
base:  action=HUMAN_GATE      reason="review result neither failed nor approved a verified candidate"
head:  action=SEND_TO_REVIEW  reason="contradictory_result: SEND_TO_REVIEW (attempt 1/1)"
```

`contradictory_result` is retryable once for a reviewer and
`terminal_if_unresolved: HUMAN_GATE`. Neither route completes; the second is
the designed one and names the failure in the record.

## 3. False-positive sweep — what the grammar changed that was not a finding

`scratchpad/fp_sweep.py` runs 33 sentences of ordinary reviewer prose, genuine
approvals and genuine rejections through `decision_statements` at base and at
head, and diffs the two.

**Exactly three lines differ**, and two of them are the finding being closed:

| sentence | base | head | |
|---|---|---|---|
| `My current decision: reject` | — | reject | the finding |
| `My current decision: approve` | — | approve | the finding's other side: the grammar must also read approvals |
| `Our recommendation: add a regression test…` | — | unreadable | **the only widening** |

The third is recorded rather than hidden. `Recommendation: add a regression
test.` **already** read as an unmappable decision at `899d383e`, because
`recommendation` was already a decisive label; Round 7 makes the possessive form
behave identically, which is the grammar doing its job. It fails closed — it
costs a round, never a completion — and it is pinned by
`test_the_owner_form_inherits_the_bare_forms_treatment`.

Everything else is unchanged, including the deliberate non-promotions:
`Final result: 3 passed, 0 failed.`, `Result: 12 passed.`, `Final status: all
green.`, `Overall: this is a good change.`, `Test outcome: 12 passed.` and
`The recommendation engine module was refactored.` all still read as **no
decision at all**.

## 4. Test results

### New

`manager/test_nextplan_round7_findings.py` — **37 passed, 149 subtests**.

Five groups: decision-field grammar (named / mutation / already-read corpora,
plus the ordinary-prose protection and the two non-promotion invariants),
summary contradiction (contradicting / ordinary / cannot-grant / cannot-stall /
schema still open to the documented field), wrong-channel channel rule (7 fence
languages in both directions, the unclosed-fence guard, and the authoritative
channel still failing closed), authority convergence (including the routing
change above), and the trust contract end to end.

### Mutation controls — the tests are not vacuous

Each fix was switched off in turn (`scratchpad/mutate.py`) and its group re-run.
All four go red, and the sources were byte-compared against their pre-mutation
copies afterwards:

| fix disabled | its group |
|---|---|
| R6-IR-1 grammar | 48 failed, 11 passed |
| R6-IR-2 summary | 12 failed, 8 passed |
| R6-IR-3 channel | 6 failed, 9 passed |
| R6-IR-4 convergence | 4 failed, 4 passed |

A fifth control covers a hole this round's own rule would otherwise have opened:
only **closed** fences are quotation, because an unclosed one runs to the end of
the message and would let a reviewer bury its own rejection by opening a fence
and never closing it. Treating unclosed fences as quotation makes that test
red.

### NextPlan suite, base vs head

Both clones are pinned, at deliberately equal path lengths (99 characters), so
no path-sensitive test can produce a phantom difference.

| | base `899d383e` | head | delta |
|---|---|---|---|
| passed | 371 | 409 | **+38** |
| failed | 1 | 1 | **0 — same test** |
| subtests | 1626 | 1775 | **+149** |

The single failure is identical at both ends:
`test_nextplan_verify.py::GitProbeRealRepositoryTests::test_honest_claims_are_all_verified`,
which emits `verify.worktree.path_mismatch` because this checkout is not at the
path that probe expects. It is **environmental**, it fails identically at base,
and it was not touched.

The +38 is fully accounted for: **37** new Round-7 tests plus **1** net new
Round-3 test — one renamed and inverted in place (no net change), one added
beside it (§5). The +149 is exactly the Round-7 file's subtest count. Nothing
else moved in either direction.

### Whole repository, base vs head

See §6. Nothing outside `manager/nextplan/` imports the package
(`grep -rl nextplan` over `manager/**.py`, excluding the package and its tests,
returns **nothing**), which is Residual 5.3 restated as a measurement: NextPlan
is not activated anywhere, so the whole-repo run is a lineage and parity check
rather than a blast-radius check.

## 5. The one pre-existing assertion this round inverts

`test_nextplan_round3_findings.py::GroupCHeldOutRejections`

| | |
|---|---|
| was | `test_a_rejection_inside_a_fence_still_withdraws` |
| now | `test_a_rejection_inside_a_fence_is_quotation_in_both_directions` |
| added beside it | `test_a_fence_still_cannot_authorize_anything` |

Round 3's rule was correct when written: a payload `review_verdict: PASS` was a
live claim, so narrowing withdrawal to unfenced prose would have been a bypass.
Round 5 removed that claim and Round 6 narrowed authority to one fence, so
nothing is left for a fenced rejection to withdraw — only its cost remained, and
§2 measures that cost at 5 of 5.

This is **not** a test made green to close a finding. It is inverted because
TRUST-MODEL §6.4 already said in writing that a wrong-channel decision must
neither authorize nor block, and the replacement asserts **both** halves: the
quotation is inert, and a fence still cannot authorize anything. The reasoning
is recorded in three places — the test's own docstring, TRUST-MODEL §7.3, and
here.

If the independent reviewer disagrees with this supersession, it is the single
most important thing to say about this round.

## 6. Isolation, safety and SSOT state

Two clones from GitHub under a session temp directory, both at 99-character
paths, with dependencies in a venv inside that directory:

- `base-r7-…` — pinned at `899d383e`, never edited
- `impl-r7-…` — head

Not touched: any production runtime HOME (none exists on this machine), any
canonical checkout, any unrelated project. Staging is explicit per path; no
`git add .` or `git add -A`. No unrelated dirty file was touched.

Allowed-scope compliance — files modified:

| file | finding |
|---|---|
| `manager/nextplan/extract.py` | R6-IR-1, R6-IR-3 |
| `manager/nextplan/contracts.py` | R6-IR-2 |
| `manager/nextplan/classify.py` | R6-IR-4 |
| `manager/test_nextplan_round7_findings.py` | new |
| `manager/test_nextplan_round3_findings.py` | §5 |
| `docs/nextplan/TRUST-MODEL.md` | §7 amendment |
| `docs/nextplan/REMEDIATION-ROUND-7-20260920.md`, `ROUND-7-EVIDENCE.md` | evidence |

Deliberately not started: activation, merge, release, `run_validation` live
wiring, the `adm-result` milestone, ExecutionRegistry changes, Failure Atlas
expansion, rejection-vocabulary additions, and any unrelated ADM fix.

## 7. Rule 32 is not satisfied

This is the implementer's own report. It is **not** an acceptance, and nothing
here may be read as one. The next step is a fresh-context independent read-only
review by an AI that is neither Claude Opus 5 in this conversation nor the
Grok 4.6 session that produced the Round-6 verdict.
