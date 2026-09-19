# NextPlan Remediation Round 7 — one contract, written down twice

Status: **IMPLEMENTED, PENDING INDEPENDENT REVIEW. DO NOT MERGE / ACTIVATE / RELEASE.**

| | |
|---|---|
| Implementer | Claude Opus 5 (`claude-opus-5`), fresh implementation conversation |
| Answering | Grok 4.6 fresh independent reviewer of Round 6 — verdict **REJECT** |
| Project | AI 一體化 / ADM |
| Repo | `github.com/ne9221/ai-development-manager` |
| Branch | `feat/nextplan-failure-atlas-foundation-20260916` |
| Base SHA | `899d383e9b808581a1e14a36bef83b7e02b4694e` |
| Date | 2026-09-20 (Asia/Taipei) |

Read with [`TRUST-MODEL.md`](TRUST-MODEL.md) section 7, which states the amended
contract, and [`ROUND-7-EVIDENCE.md`](ROUND-7-EVIDENCE.md), which holds the
measurements.

## 1. The one-line reading

Round 6 was right to replace rejection *vocabulary* with decision *shape*. It
then wrote that shape down twice and built only one of the copies as a grammar.

All four findings are the same defect at different distances from the centre:

| | the two copies that disagreed |
|---|---|
| R6-IR-1 | the copula form was a grammar; the label form was a list of strings |
| R6-IR-2 | prose outside the object was read; the object's own `summary` was not |
| R6-IR-3 | a wrong-channel fence was barred from authority but not from withdrawal |
| R6-IR-4 | `review_proof` asked the contract one way; `signals_for` asked it another |

Nothing here widens the rejection vocabulary. `_REJECT_VALUES` is byte-for-byte
unchanged, and there is a test that says so.

## 2. Reproduced at base before any edit

Every finding was reproduced against an unmodified clone pinned at `899d383e`
(`scratchpad/repro_r7.py`) before a line was written.

| finding | at base `899d383e` | at head |
|---|---|---|
| R6-IR-1 decision field beside a bound PASS | **9 / 12 MARK_COMPLETE** | 0 / 12 |
| R6-IR-2 bound PASS with a contradicting `summary` | **3 / 3 MARK_COMPLETE** | 0 / 3 |
| R6-IR-3 bound PASS beside a quoted rejection | **5 / 5 stalled** | 0 / 7 stall |
| R6-IR-4 `signals_for` passes statements | **no** | yes, via one helper |

All six decision fields the review named read as **zero** statements at base.
The three that did not complete were routed away by unrelated rules, not by the
contract under test — which is exactly why 9/12 rather than 12/12 is the honest
number to report.

**Honesty note on R6-IR-1's count.** The review asserted
`decision_statements(...) == []` for its six sentences; that is confirmed
exactly. The 9/12 is this round's own corpus (the six named, four fresh
mutations, two known-good controls), measured here rather than quoted from the
reviewer.

## 3. What changed

### R6-IR-1 — the decisive label becomes a finite grammar (HIGH)

`manager/nextplan/extract.py`. A decisive label is now

```
(owner)? (modifier)* (decision-noun)
```

with all three parts closed sets: owners `my / our / the / its / their`;
modifiers `current / final / overall / review / reviewer / official / formal /
considered`; decision nouns `decision / verdict / recommendation / disposition /
outcome`. `_label_tier()` consults the explicit Round-5/6 tables first — so
every existing label keeps its tier, including the decisive ones the grammar
would not generate (`final call`, `approval`, `sign off`, the Chinese labels)
and the reporting ones it must not (`final status`, `summary verdict`) — and
only then tries the composition, and only for the decisive tier.

`_COPULA_DECISION` is rebuilt from the *same two frozensets*, so the two forms
cannot drift apart again. Drifting apart is what the finding was.

Two deliberate non-moves:

- **Reporting nouns are not promoted by a modifier.** `Final result: 3 passed`
  is a count. Reading it as an unreadable decision would stall honest reviews,
  which is the failure mode on the far side of fail-closed.
- **No wording was added.** `Final outcome: send back for revision` is read as a
  decision whose value ADM cannot map. Under the existing Round-6 rule that is a
  CONFLICT, and it blocks for that reason — not because anyone taught the
  matcher a new phrase.

### R6-IR-2 — the summary may withdraw the PASS it sits inside (MEDIUM)

`manager/nextplan/contracts.py`. `_summary_statements()` reads each **bound**
block's `summary` with `extract.decision_statements` — the same parser as the
prose rule, by deferred import, because a second rejection scanner would be the
next round's finding. A decision-shaped contradiction there makes that object's
`PASS` a `CONFLICT`.

The asymmetry is unchanged: a summary can withdraw, never grant. Summaries are
collected per bound block only, so a forged unbound decision carrying a
rejecting summary still cannot stall a task — the object it sits in cannot
block, and neither can its prose.

Ordinary summaries announce no decision and are read as none, which the tests
pin with the reviewer's own three examples.

### R6-IR-3 — a wrong-channel fence is quotation in both directions (MEDIUM)

`manager/nextplan/extract.py`. `written` (all prose outside the agent's own
payload) gains a companion, `unfenced` (the same, minus **every** fenced
region). The reviewer's decision statements and the withdrawal check now read
`unfenced`. `written` still feeds the quota / rate-limit heuristics, which read
what happened to the *run* rather than what anyone decided about it.

This **supersedes the Round-3 rule** that a rejection inside a fence still
withdraws. That rule was correct when written — a payload `review_verdict: PASS`
was a live claim, so narrowing withdrawal to unfenced prose would have been a
bypass. Round 5 removed that claim and Round 6 narrowed authority to one fence,
so nothing is left for a fenced rejection to withdraw; only the cost remained.
`test_a_rejection_inside_a_fence_still_withdraws` has been **inverted on
purpose**, renamed, and carries the full reasoning, and the same file now also
asserts that a fence still cannot authorize anything. See §5.

### R6-IR-4 — one authority invocation

`manager/nextplan/classify.py`. `review_authority_for(result, review_context)`
is the only place in that module where authority is asked about; `review_proof`
and `signals_for` both go through it. A test asserts that neither function calls
`contracts.review_authority` directly, because a fixed call site is exactly the
kind of fix that comes undone.

## 4. Tests

`manager/test_nextplan_round7_findings.py` — **37 tests, 149 subtests**, five
groups: the decision-field grammar (named + mutation + already-read corpora, the
ordinary-prose protection that is the other half of it, and the two
non-promotion invariants), the summary contradiction (contradicting + ordinary +
cannot-grant + cannot-stall), the wrong-channel channel rule (7 fence languages
in both directions, the unclosed-fence guard, and the authoritative channel
still failing closed), authority convergence, and the trust contract end to end.

Each fix was also switched off in turn and its group confirmed red
(`scratchpad/mutate.py`), after which every source was byte-compared against its
pre-mutation copy. A fifth control covers a hole this round's own rule would
otherwise have opened: only **closed** fences are quotation, because an unclosed
one runs to the end of the message and would let a reviewer bury its own
rejection by opening a fence and never closing it. See ROUND-7-EVIDENCE §4.

NextPlan suite, both clones pinned at equal path lengths: base **371 passed /
1626 subtests**, head **409 passed / 1775 subtests**, with the same single
environmental failure at both ends.

Whole repository: **2938 collected at base, 2976 at head (+38, exactly the new
tests)**, and the failure sets diffed both ways are **identical** — 65 `FAILED`
and 3 `SUBFAILED` tests at each end, none of them NextPlan. This environment
lacks the credentials and manager HOME those families need, so it carries 73
environmental failures at *both* ends rather than Round 6's 5, and the same base
commit run twice differs by one. See ROUND-7-EVIDENCE §4 for why the identical
set and the collected count are the numbers being trusted.

Every security-sensitive case ends at a **planner action**, not a return value.

## 5. The one behaviour change to an existing test

`test_nextplan_round3_findings.py::GroupCHeldOutRejections`

| | |
|---|---|
| was | `test_a_rejection_inside_a_fence_still_withdraws` |
| now | `test_a_rejection_inside_a_fence_is_quotation_in_both_directions` |
| plus | `test_a_fence_still_cannot_authorize_anything` (new, same group) |

This is the only pre-existing assertion this round inverts, it is inverted
because TRUST-MODEL 6.4 already said so in writing, and it is recorded here,
in the test's own docstring, and in TRUST-MODEL 7.3. It is **not** a test made
green to close a finding: the rule it protected was made unreachable by Rounds 5
and 6, and the replacement asserts both halves of the contract that took its
place.

## 6. Isolation and safety

Two clones from GitHub under a session temp directory, deliberately the same
path length (99 characters) so no installer test can produce a phantom
base/head difference: `base-r7-…` pinned at `899d383e` and never edited,
`impl-r7-…` at head. Dependencies in a venv inside that directory.

No production runtime HOME was touched; none exists on this machine. Staging is
explicit per path — no `git add .` / `-A`. No unrelated dirty file was touched;
`git status --porcelain` was empty at clone time.

Deliberately **not** started, as instructed: activation, merge, release,
`run_validation` live wiring, the `adm-result` milestone, ExecutionRegistry
rewrites, Failure Atlas expansion, and any unrelated ADM fix.

## 7. Blocker and next step

**BLOCKER: none technical.**

**AI-DEVELOPMENT-RULES rule 32 is not satisfied.** This is the implementer's own
report and is not an acceptance.

**NEXT:** a fresh-context independent read-only review by an AI that is neither
Claude Opus 5 in this conversation nor the Grok 4.6 session that produced the
Round-6 verdict.

Reviewer attack order:

1. the decision-field grammar — attack it with compositions nobody listed here,
   and with ordinary review prose that must *not* be read as a decision;
2. the summary rule — attack both sides: a contradiction that should withdraw,
   and an honest summary that must not stall;
3. the channel rule in both directions, including the Round-3 inversion in §5 —
   if that supersession is wrong, it is the most important thing to say;
4. authority convergence — find a third path that asks the contract differently.

The standing instruction from Round 6 holds and was obeyed: **stop extending the
natural-language matcher.** R6-IR-1 was closed by making the grammar finite and
stated, not by adding wordings.
