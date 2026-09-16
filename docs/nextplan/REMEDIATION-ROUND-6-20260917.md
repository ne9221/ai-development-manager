# NextPlan Remediation Round 6 — 2026-09-17

**Status: IMPLEMENTED, PENDING INDEPENDENT REVIEW. Do not merge, activate or
release.** AI-DEVELOPMENT-RULES rule 32 is not satisfied until a fresh-context
reviewer that is neither this implementer nor the Round-5 reviewer session has
run.

| | |
|---|---|
| Implementer | Claude Opus 5 (`claude-opus-5`), fresh implementation conversation |
| Reviewer being answered | Grok 4.6, fresh-context reviewer of Round 5 |
| Branch | `feat/nextplan-failure-atlas-foundation-20260916` |
| Base | `405c90c4b26d0851937bf05651cfc15b6ec6dfde` |
| Checkout | isolated clone in a session scratchpad; the production runtime HOME (`C:\Users\EE\.ai-development-manager`) and the canonical checkout were not touched |
| Scope | contracts / extract / verify / runner / result schema / direct regression tests / docs |
| Not started | `adm-result` contract, dispatcher changes, Failure Atlas expansion, activation |

## 0. The one-line reading

Round 5 moved authority out of prose and into a provenance-bound object. That
was right. **Round 6's four findings are all the same over-correction: the bound
object was treated as not merely necessary but final**, plus two places where
`TRUST-MODEL.md` described behaviour the code did not actually have.

- F1: the doc said prose "may withdraw a decision it contradicts". It could not.
- F2: the producer could not be fooled; the consumer never checked that the
  producer had run.
- F3/F4: the object and its channel were wider than the contract they published.

## 1. Reproduction before the fix

Run against the unmodified clone at `405c90c4`
(`scratchpad/repro_r6.py`, output preserved as `repro_r6_BASE.txt`):

```
FINDING 1: bound PASS + decision-shaped contradiction -> MARK_COMPLETE
  12/30 completed
    'Current decision: reject'      'Final call: do not complete'
    'Overall decision: negative'    'Disposition: rejected'
    'Recommendation: reject'        'Conclusion: fail'
    'Result: changes requested'     'I am rejecting this review.'
    'My decision is to reject.'     'Decision:'
    'Verdict:'                      'Outcome: 駁回'

FINDING 2: hand-written adm-validation-result (never spawned by ADM)
  tests_run  = 999  level=VERIFIED
  planner    = MARK_COMPLETE

FINDING 3: schema-valid PASS carrying a contradictory extra field
  6/6 completed  (blocking=true, required_action=repair, status=needs_changes,
                  can_merge=false, approved=false, decision=REJECT)

FINDING 4: non-authoritative fences collected as reviewer decisions
  json / yaml / text / markdown / example / <bare>  -> 6/6 MARK_COMPLETE
```

All four reproduce. **A note on F1's number:** the review reported 27 of 30 on
its own corpus; this is 12 of 30 on a corpus written here, because this corpus
is deliberately *decision-shaped* and several of its sentences happen to trip
the existing rejection matcher. The defect is the same one and the two sentences
the review named (`Current decision: reject`, `Final call: do not complete`) both
reproduce. The measured number reported is the one actually observed here, not
the reviewer's.

## 2. What changed

### F1 — reviewer contradiction contract (HIGH)

`contracts.review_authority(decisions, expectation, statements=())`. When the
object would otherwise authorize, a contradicting or unreadable **decision
statement** in the same output makes the pair `CONFLICT` and blocks.

`extract.decision_statements(text)` reads *shape*, not sentiment:

- a decision **field** on a line — `Verdict:`, `Decision:`, `Current decision:`,
  `Review outcome:`, `Approval:`, `Disposition:`, … (decisive tier), and
  `Result:`, `Conclusion:`, `Assessment:`, … (reporting tier);
- the same field with a copula — `My decision is to reject`;
- a first-person decision verb whose object is the work — `I reject this patch`,
  `I cannot approve this change`, `I will not sign off on this patch`;
- the review's outcome predicated of the work — `This fails my review`.

Values map against a closed vocabulary. **An unmappable value in a decisive
field is `unreadable`, and unreadable is a conflict, not silence.** A reporting
label counts only on an unambiguous rejection, so `Conclusion: the fix is
correct` does not withdraw a genuine approval.

The asymmetry is enforced, not just documented: statements are consulted **only
when a bound PASS already exists**, so they can revoke and can never grant — and
a statement with no object cannot block either, or a pasted `Decision: reject`
would stall any task.

Two closed-class gaps were fixed rather than vocabulary added: `_NOT` was
missing the first-person copula (`am not`) and the analytic form of an entry it
already had (`not able to`, beside `unable to`).

**Measured after: 0 of 30 contradictions complete, 0 of 24 Round-5 genuine
approvals refused.**

### F2 — execution registry provenance (HIGH)

`runner.ExecutionRegistry` mints the `execution_id` **before** the spawn, binds
it to the task and run, and records what the adapter observed: `argv_sha256`, a
nonce, `artifact_path`, `artifact_sha256`, `exit_code`, `counts`.
`run_validation(..., registry=, task_id=, run_id=)` issues before spawning and
completes from its own observations; the report path is nonce-named inside a
directory the call created and asserted absent before the spawn.

`verify.adm_test_evidence(execution, registry=, task_id=, run_id=)`:

> **counts are read from the registry entry, never from the record.**

A fabricated number is not disbelieved — it is never consulted. Refused: no
registry, no entry for the id, an entry from another task or run, an argv that
does not digest to the one ADM spawned, an artifact digest other than the one
ADM read, an entry with no artifact, an entry whose process never started. An
unbound record also no longer proves `tests_failed = 0` from its own
`exit_code`; a **legacy** record still can, because ADM spawned that command, so
its exit status genuinely is an observation.

### F3 — semantic closure (MEDIUM)

`contracts.review_problems` closes the decision object, its `findings` items and
its `provenance` against unknown keys, and
`schema/adm_review_result.schema.json` is set to `additionalProperties: false`
to match. Unknown field → INVALID → blocks. The documented optional `summary`
still validates.

### F4 — authoritative channel (LOW)

`extract._fenced_decisions` collects only a ` ```adm-review-result ` fence (the
native whole-body structured output still counts). Any other fence containing
the schema string produces a warning naming the correct channel, is **not**
collected, and is **not** treated as a blocking decision.

## 3. The Round-5 `RESIDUAL` contract, and why it was changed

`GroupSevenFreshCorpora.RESIDUAL` asserted with `assertEqual` that eight
sentences **must keep completing**. Round 5 wrote it as an honest measurement of
a known gap; the independent review ruled it a contract violation, because a
test that pins false-completes as correct behaviour makes them green, and green
is what gets read.

It is renamed `OUT_OF_CONTRACT_COMMENTARY` and now holds four sentences, none of
which is required to complete. The four that were decision-shaped now fail
closed:

| was in RESIDUAL | now |
|---|---|
| `Current decision: reject` | **closed** — decision field |
| `My assessment is negative.` | **closed** — copula decision |
| `This fails my review.` | **closed** — review outcome |
| `I am not able to approve the patch.` | **closed** — first-person decision |
| `Changes are required before I can sign off.` | commentary — states a finding |
| `There remain unresolved concerns about locking.` | commentary — states a finding |
| `Two defects block acceptance.` | commentary — states a finding |
| `Hold the merge until CI is green.` | commentary — an instruction |

The four remaining announce no verdict; the parser correctly reads none, which
is asserted as a test rather than assumed. Reaching one still requires a
reviewer that returns a bound `PASS` with an **empty** `findings` list and then
contradicts it in prose; putting the blocker in `findings` closes all four, and
that is asserted too. Closing them otherwise means widening rejection
vocabulary — the move that lost Rounds 2, 3 and 4, and which this round was
instructed not to repeat.

One further **pre-existing** over-refusal is recorded rather than chased: `A
blocker was present last time; it is resolved.` is split at the semicolon by the
Round-5 clause splitter. Verified to behave identically at `405c90c4` before any
edit here, so it is not a regression and not one of Grok's findings. It fails
closed.

## 4. Files changed

| File | Change |
|---|---|
| `manager/nextplan/contracts.py` | `statements` parameter and CONFLICT rule; key closure for decision / findings / provenance |
| `manager/nextplan/extract.py` | `decision_statements` and its three shape forms; two closed-class negation gaps; channel narrowed to one fence |
| `manager/nextplan/runner.py` | `ExecutionRegistry`, `argv_digest`, registry-aware `run_validation`, nonce-named artifact with pre-existence assertion |
| `manager/nextplan/verify.py` | registry binding in `adm_test_evidence`; counts from the registry; `bound` / `exit_observed` separation |
| `manager/nextplan/result.py` | `decision_statements` on the normalized result |
| `manager/nextplan/classify.py` | pass statements into `review_authority` |
| `manager/nextplan/harness.py` | `execution_registry` / `registry_for` fixtures (ADM's side) |
| `schema/ai_result.schema.json` | regenerated |
| `schema/adm_review_result.schema.json` | `additionalProperties: false` throughout |
| `schema/adm_validation_result.schema.json` | `artifact_sha256`; registry semantics documented |
| `manager/test_nextplan_round6_findings.py` | **new** — six groups |
| `manager/test_nextplan_round5_findings.py` | RESIDUAL contract replaced (§3) |
| `manager/test_nextplan_{review,round3,round4}_findings.py`, `test_nextplan_verify.py` | fixtures bind through the registry |
| `docs/nextplan/TRUST-MODEL.md` | §6 amendment |

## 5. New tests

`manager/test_nextplan_round6_findings.py` — 48 tests, 162 subtests.

| Group | Covers | Size |
|---|---|---|
| 1 — reviewer contradiction | 30 fresh decision-shaped contradictions + bound PASS; 18 genuine decision-shaped approvals; unreadable fields; prose cannot grant; a statement alone cannot block | ≥25 required |
| 2 — genuine structured PASS | 17 informational-prose cases; resolved history; empty/info findings; blocking findings still block; the quoted-vs-inline boundary; the pre-existing over-refusal recorded | ≥15 required |
| 3 — execution registry | 15 forged/stale/unbound cases + 6 legitimate, incl. **really spawned** pytest runs (pass / zero tests / failing / malformed report) | ≥15 required |
| 4 — schema semantics | 10 contradictory extras; nested closure; the published schema agrees with the validator | — |
| 5 — channel | 11 non-authoritative fences; prose-embedded and quoted JSON; wrong channel warns but does not block; worker still refused | — |
| 6 — trust contract | the layers as one table, both directions, plus the honest path completing | — |

Every security-sensitive case ends at a **planner action**, not at a helper's
return value.

## 6. Results

See `ROUND-6-EVIDENCE.md` for the raw numbers, tree-pinned base/head runs and
git state.

## 7. Blocker and next step

**Blocker:** none technical. The change is complete and pushed unmerged.

**Next:** a fresh-context independent review (AI-DEVELOPMENT-RULES rule 32). The
reviewer must be neither Claude Opus 5 in this conversation (the implementer)
nor the Grok 4.6 session that produced the Round-5 verdict. Attack order:

1. the reviewer-contradiction contract,
2. execution-registry provenance,
3. semantic extra-field rejection,
4. the authoritative-channel restriction.

If those hold, **stop extending the natural-language matcher** — that is the
standing instruction from this round, and the reason F1 was fixed by shape
rather than by vocabulary.
