# NextPlan Remediation Round 8 — the published grammar was not the implemented grammar

Status: **ROUND_8_REMEDIATED — PENDING FRESH INDEPENDENT REVIEW. DO NOT MERGE / ACTIVATE / RELEASE.**

| | |
|---|---|
| Implementer | Claude Fable 5.1 (`claude-fable-5-1`), Claude Code, fresh Round-8 implementation session. The dispatch named `claude-opus-5`; the session that executed it ran on Fable 5.1 and says so rather than reporting the requested id. |
| Answering | Grok fresh independent reviewer of Round 7 — verdict **REJECT**, `NEXT_ALLOWED_MILESTONE: ROUND_8_REMEDIATION` |
| Project | AI 一體化 / ADM — Loop Engineering / NextPlan |
| Repo | `github.com/ne9221/ai-development-manager` |
| Reviewed object | `57f8ed79dc1acd04012bc273cd911eec9b465b13` |
| Base SHA | `ec5da6f7e29a3eb9777fb89e14b1a04d3bbf1a69` — the Round-7 branch tip, independently verified docs-only after `57f8ed79` (two files under `docs/nextplan/`, 47 insertions, 5 deletions, no code) |
| Branch | `fix/nextplan-round8-finite-grammar-20260920`, created from `ec5da6f7`; the Round-7 branch was not rewritten |
| Date | 2026-09-20 (Asia/Taipei) |
| Process | Production Fix Protocol v2 (`2.0.9-candidate`, rule 47.6 identity) — read-only diagnosis → reproduce → isolated minimal repair → targeted tests → regression failure-set comparison → mutation proof → freeze → independent fresh reviewer |

Read with [`TRUST-MODEL.md`](TRUST-MODEL.md) section 8, which states the
corrected contract, and [`ROUND-8-EVIDENCE.md`](ROUND-8-EVIDENCE.md), which holds
the measurements.

## 1. The one-line reading

Round 7 published the decisive-field grammar

```
(owner)? (modifier)* (decision-noun)
```

with three closed sets, said "both forms are now built from the same two sets,
so they cannot drift apart again" — and then implemented it three different
ways. The grammar in `_label_tier` was correct. It was never asked.

| | published | implemented at `57f8ed79` |
|---|---|---|
| label form, field length | unbounded | `_LABEL_LINE` captured `[A-Za-z][A-Za-z \-]{0,24}` — **25 characters** — so a longer label never reached `_label_tier` |
| copula form, owner | optional | **required** |
| copula form, modifiers | `*` | **`{0,2}`** |

The two HIGH findings are one defect seen from two places:

| finding | surface |
|---|---|
| R7-IR-1 | `extract.decision_statements` drops grammatical fields: `My current official decision: reject` (28 chars), `Current decision is to reject` (no owner), `My current official final decision is to reject` (3 modifiers) |
| R7-IR-2 | `contracts._summary_statements` correctly reuses that collector, so a bound PASS whose `summary` is any of the above completed too |

Unsafe result at base: **bound authoritative PASS + explicit reject in the
documented grammar → MARK_COMPLETE**, 7 of 7 reviewer attacks.

## 2. Reproduced at base before any edit

`scratchpad/repro_r8.py`, run against the unmodified branch at `ec5da6f7`
before a line was changed, then against head. Every case ends at a planner
action.

| | at base `ec5da6f7` | at head |
|---|---|---|
| A1 `My current official decision: reject` + bound PASS | **MARK_COMPLETE**, 0 statements | SEND_TO_REVIEW (`contradictory_result`) |
| A2 `Our considered disposition: reject` | **MARK_COMPLETE**, 0 statements | SEND_TO_REVIEW |
| A3 `Their considered official recommendation: reject` | **MARK_COMPLETE**, 0 statements | SEND_TO_REVIEW |
| A4 `My current official final overall review decision: reject` | **MARK_COMPLETE**, 0 statements | SEND_TO_REVIEW |
| A5 `My current official final decision is to reject` | **MARK_COMPLETE**, 0 statements | SEND_TO_REVIEW |
| A6 `Current decision is to reject` | **MARK_COMPLETE**, 0 statements | SEND_TO_REVIEW |
| A7 bound PASS, `summary="My current official decision: reject"` | **MARK_COMPLETE**, 0 statements | SEND_TO_REVIEW |
| **attacks completing** | **7 / 7** | **0 / 7** |
| P1–P4 positive controls (ordinary approval, `Final result: 3 passed`, `I recommend adding a regression test.`, quoted rejection in a closed wrong-channel fence) | MARK_COMPLETE | MARK_COMPLETE |
| P5 wrong-channel PASS alone | SEND_TO_REVIEW | SEND_TO_REVIEW |
| P6 unclosed fence then reviewer reject | SEND_TO_REVIEW | SEND_TO_REVIEW |

## 3. What changed — `manager/nextplan/extract.py` only

The remediation is structural. No vocabulary moved.

1. **The 25-character label capture is gone.** `_LABEL_LINE` now captures the
   whole text before the colon (`[A-Za-z][A-Za-z \-]*`). A character cap is
   not a grammar rule; which labels count is decided only by the explicit
   tables and the grammar, in `_label_tier`.
2. **One grammar source.** `_field_grammar(nouns)` returns the regex source
   for `(owner)? (modifier)* (noun)` over the closed owner and modifier sets.
   `_DECISION_FIELD = re.compile(_field_grammar(_DECISION_NOUNS))` is the
   decisive grammar, compiled once.
3. **Both syntaxes derive from it.** `_label_tier` consults the Round-5/6
   tables first, then `_DECISION_FIELD.fullmatch(label)`. `_COPULA_DECISION`
   embeds `_DECISION_FIELD.pattern` verbatim as its decisive branch, followed
   by the copula and the value. Owner is therefore optional and modifiers are
   unbounded in both forms, because there is one place that says so.
4. The copula's reporting nouns (`call`, `assessment`, `conclusion`,
   `judgement`, `judgment`) are the same composition over a separate frozenset
   (`_COPULA_REPORTING_NOUNS`), still reject-only, still never promoted.
   Keeping the set apart means the decisive grammar cannot grow by accident.

Untouched, and proven untouched in [`ROUND-8-EVIDENCE.md`](ROUND-8-EVIDENCE.md):
`_REJECT_VALUES` (sha256 `1284e63e…`, 53 entries), `_APPROVE_VALUES`, the five
owners, eight modifiers and five decision nouns, `_DECISIVE_LABELS`,
`_REPORTING_LABELS`, the reporting-noun policy, the Round-7 fence semantics and
the single authority path. `contracts.py` was **not** changed: its summary rule
already reads through the shared collector, which is the architecture the
review confirmed, and the summary hole closed the moment the collector did.

## 4. Tests — `manager/test_nextplan_round8_findings.py`

40 tests, 1051 subtests, in six groups: the seven reviewer attacks at the
planner; the colon-form coverage matrix; the copula-form matrix; structural
equivalence of the two syntaxes; summary reuse; positive controls and the
Round-7 invariants. The matrix is **generated from the closed sets read off
production** — every owner (and no owner) × {0, 1, 2, 3, 5} modifiers × every
decision noun, 150 compositions per syntax, with field lengths past 25, 32 and
40 characters — not a list of named examples.

At base the module is red in 25 of 40 tests (695 failures); the 15 green at
base are invariants that already held and must keep holding. Five mutation
controls (M1–M5) each turn intended tests red and restore byte-identically.
Details and counts are in the evidence file.

## 5. Two things stated rather than hidden

**5.1 The one syntax difference.** The label form is line-anchored: the whole
text before the colon is the field, so `My tentative decision: reject` is not
a field (`tentative` is outside the set) and is not read. The copula form is a
sentence, and reads the grammatical run that ends at the noun, so `My
tentative decision is to reject` reads `decision is to reject` — a rejection.
This is the behaviour Round 7 already had for owned fields; making the owner
optional extends it to ownerless ones. It fails closed and is pinned by
`test_the_only_syntax_difference_is_the_anchor_and_it_fails_closed`.

**5.2 The measured cost of the ownerless copula.** A sweep of every string
literal in the existing NextPlan tests and fixtures (1421 literals) found
**4** whose reading changed between base and head. All four are test
docstrings, not reviewer prose (`A decision is collected and bound…`, `an
unreadable decision was read as no decision`), all read as `unreadable`, none
as an approval. An ownerless `decision is …` mid-sentence can now cost a round.
It was the reviewer's explicit requirement (A6) and it is the published
grammar; it never buys a completion.

## 6. Isolation and safety

Fresh clone at `~/dev/adm-round8-20260920`, branch created from the verified
remote tip. A second worktree pinned at `ec5da6f7` and never edited served as
the base for reproduction, the baseline failure list and the pre-fix red run
of the new module; it is removed after the measurements. Staging is explicit
per path. No production runtime HOME exists on this machine. The Round-7
branch was not rewritten or force-pushed.

Deliberately **not** started, as instructed: `adm-result`, live ADM→Loop
wiring, ExecutionRegistry, Failure Atlas expansion, Continuous Orchestration,
AG IDE, multi-agent parallelism, production activation, UI work, unrelated
ADM backlog. The PFP was not modified.

## 7. Blocker and next step

**BLOCKER: none technical.**

**AI-DEVELOPMENT-RULES rule 32 is not satisfied.** This is the implementer's own
report and is not an acceptance. Final status: **ROUND_8_REMEDIATED — PENDING
FRESH INDEPENDENT REVIEW.**

**NEXT:** a fresh independent reviewer only. Do not request Fable-architecture
closure; that comes only after a Round-8 independent PASS.

Reviewer attack order:

1. the one grammar source — find a composition from the closed sets that one
   syntax reads and the other does not, other than the anchor difference in
   §5.1;
2. the removed cap — find a label that the tables or grammar would accept and
   that still never reaches `_label_tier`;
3. the summary — every long composition the collector reads must withdraw in a
   bound summary and must not stall in an unbound one;
4. over-refusal — ordinary reviewer prose containing a decision noun that now
   stalls a genuine PASS and did not before (§5.2 measured four docstrings;
   find a fifth that a reviewer would actually write).

The standing instruction holds: **no wording was added.** `_REJECT_VALUES` is
byte-for-byte unchanged and pinned by hash.
