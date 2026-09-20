# Round 8 evidence — measurements, not intentions

Status: **ROUND_8_REMEDIATED — PENDING FRESH INDEPENDENT REVIEW. DO NOT MERGE / ACTIVATE / RELEASE.**

Companion to [`REMEDIATION-ROUND-8-20260920.md`](REMEDIATION-ROUND-8-20260920.md)
and [`TRUST-MODEL.md`](TRUST-MODEL.md) §8.

Every number below was produced by running something. Scratchpad scripts
(`repro_r8.py`, `fp_sweep_r8.py`, `mutate_r8.py`) are session-local, as in
Rounds 6 and 7; **every corpus they measure is also pinned as a test** in
`manager/test_nextplan_round8_findings.py`, which is the durable and
re-runnable form.

## 1. Preflight

| check | result |
|---|---|
| Governance SSOT | Drive `AI-DEVELOPMENT-RULES` **v0.6.0** (`last_updated: 2026-09-20`, doc `1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU`, modifiedTime `2026-09-20T03:14:09Z`); Drive `PROJECT-RULES — ADM` **v1.1.0** (`last_updated: 2026-08-21`, doc `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ`). Both read from Drive. The repo copy `AI-DEVELOPMENT-RULES.md` is v0.1.5 and is not the SSOT (NFP-004). |
| Governance delta since Round 7 | v0.5.0 → v0.6.0: rule 47 rewritten; 47.6 names the Production Fix Protocol canonical identity as `production-fix-protocol` `2.0.9-candidate` @ `b9a7afbba6242dabe7c729b8d2e96c9da51d20a4`, `SKILL.md` sha256 `c6144dc4…094e`. The installed skill on this machine hashes to exactly that value (verified with `sha256sum`), so the protocol followed here is the one rule 47.6 names. |
| Reviewed object | `57f8ed79dc1acd04012bc273cd911eec9b465b13` |
| Remote tip of the Round-7 branch at preflight | `ec5da6f7e29a3eb9777fb89e14b1a04d3bbf1a69` — `git diff --stat 57f8ed79 ec5da6f7`: `docs/nextplan/REMEDIATION-ROUND-7-20260920.md` (+8) and `docs/nextplan/ROUND-7-EVIDENCE.md` (+39/−5) only; one commit between them. **Docs-only confirmed independently**, so `ec5da6f7` is the Round-8 base. |
| Branch | `fix/nextplan-round8-finite-grammar-20260920`, created from `ec5da6f7` in a fresh clone; the Round-7 branch untouched |
| `git status --porcelain` at clone | empty |
| Production runtime HOME | not touched; none exists on this machine |
| Baseline failure list (captured at base, before any edit) | NextPlan suites at `ec5da6f7`: **409 passed, 1 failed, 1775 subtests**. The one failure is `test_nextplan_verify.py::GitProbeRealRepositoryTests::test_honest_claims_are_all_verified` (`verify.worktree.path_mismatch`, environmental, as in Rounds 6 and 7). |

### Quota evidence

Rule 11 and rule 30: source, confidence, `last_updated`; no guessing.

| provider | source | confidence | reading | last_updated |
|---|---|---|---|---|
| Claude Code (this session) | first-party session usage API (`get_usage`) | high | plan **Max**; 5-hour **25%** used (resets 2026-09-20T07:40Z); weekly all-models **39%**; weekly Fable **39%**; extra usage disabled | 2026-09-20T03:52Z (11:52 Asia/Taipei) |
| Codex | none found | — | **UNKNOWN** | — |
| Gemini / Google AI Pro | none found | — | **UNKNOWN** | — |
| Antigravity | none found | — | **UNKNOWN** | — |

## 2. Reproduced at base before any edit

`repro_r8.py` against the unmodified branch at `ec5da6f7`, then against head.
Each case is a full planner run (`plan(reviewing(), reviewer event)`) with a
bound, correctly targeted `adm-review-result` PASS beside the prose.

### R7-IR-1 — collector drops grammatical fields (HIGH)

| | statements read at base | planner at base | planner at head |
|---|---|---|---|
| A1 `My current official decision: reject` (label 28 chars) | `[]` | **MARK_COMPLETE** | SEND_TO_REVIEW, `contradictory_result` |
| A2 `Our considered disposition: reject` (26) | `[]` | **MARK_COMPLETE** | SEND_TO_REVIEW |
| A3 `Their considered official recommendation: reject` (40) | `[]` | **MARK_COMPLETE** | SEND_TO_REVIEW |
| A4 `My current official final overall review decision: reject` (49) | `[]` | **MARK_COMPLETE** | SEND_TO_REVIEW |
| A5 `My current official final decision is to reject` (3 modifiers) | `[]` | **MARK_COMPLETE** | SEND_TO_REVIEW |
| A6 `Current decision is to reject` (no owner) | `[]` | **MARK_COMPLETE** | SEND_TO_REVIEW |

### R7-IR-2 — summary inherits the collector hole

| | at base | at head |
|---|---|---|
| A7 bound PASS with `summary: "My current official decision: reject"` | **MARK_COMPLETE** | SEND_TO_REVIEW |

**Total: 7 / 7 false completions at base, 0 / 7 at head.** The review's claim
that these fields read as zero statements is confirmed exactly.

### Positive controls, base and head

| | base | head |
|---|---|---|
| P1 ordinary approval prose + bound PASS | MARK_COMPLETE | MARK_COMPLETE |
| P2 `Final result: 3 passed` + bound PASS | MARK_COMPLETE | MARK_COMPLETE |
| P3 `I recommend adding a regression test.` + bound PASS | MARK_COMPLETE | MARK_COMPLETE |
| P4 quoted rejection in a closed ` ```text ` fence + bound PASS | MARK_COMPLETE | MARK_COMPLETE |
| P5 wrong-channel (` ```json `) PASS alone | SEND_TO_REVIEW | SEND_TO_REVIEW |
| P6 unclosed fence, then `Current decision: reject` | SEND_TO_REVIEW | SEND_TO_REVIEW |

## 3. The implementation change

One production file: `manager/nextplan/extract.py`.

| symbol | at `ec5da6f7` | at head |
|---|---|---|
| `_LABEL_LINE` label group | `[A-Za-z][A-Za-z \-]{0,24}` | `[A-Za-z][A-Za-z \-]*` — no cap |
| `_field_grammar(nouns)` | — | **new**: returns the regex source `(?:(?:owner)\s+)?(?:(?:modifier)\s+)*(?P<noun>…)` from the closed sets |
| `_DECISION_FIELD` | — | **new**: `re.compile(_field_grammar(_DECISION_NOUNS))` — the grammar authority |
| `_label_tier` grammar branch | hand-rolled token check | `_DECISION_FIELD.fullmatch(label)` |
| `_COPULA_DECISION` | hand-written: owner required, `{0,2}` modifiers, literal noun list | `\b(?:` + `_DECISION_FIELD.pattern` + `|` + `_field_grammar(_COPULA_REPORTING_NOUNS, "rnoun")` + `)` + copula + value |
| `_COPULA_REPORTING_NOUNS` | inline in the regex | **new** frozenset `{call, assessment, conclusion, judgement, judgment}` |
| `_DECISIVE_NOUNS` | alias of `_DECISION_NOUNS` | removed; decisiveness is "matched the `_DECISION_FIELD` branch" |
| `decision_statements` | `noun in _DECISIVE_NOUNS` | `match.group("noun") is not None` |

`manager/nextplan/contracts.py` is unchanged.

### Grammar authority location

`manager/nextplan/extract.py`: `_field_grammar` (source), `_DECISION_FIELD`
(compiled). Consumers: `_label_tier` (colon form, full match) and
`_COPULA_DECISION` (copula form, embedded by reference).

### Colon / copula structural equivalence

Pinned by `GroupThreeStructuralEquivalence`:

* `_DECISION_FIELD.pattern in _COPULA_DECISION.pattern` — literal containment;
* `_DECISION_FIELD.pattern == _field_grammar(_DECISION_NOUNS)`, and the
  pattern carries an optional owner group and a starred modifier group;
* `inspect.getsource(_label_tier)` contains `_DECISION_FIELD.fullmatch`;
* neither `{0,24}` nor `{0,2}` appears in either compiled pattern;
* for all 150 generated compositions, `_label_tier(field) == decisive` iff
  `_COPULA_DECISION.fullmatch(field + " is reject")` matched the decisive
  branch; for seven near-misses both are false;
* behaviourally: every generated composition reads `reject` in both syntaxes;
  the one syntax difference (anchor) is pinned separately and fails closed.

### Summary reuse

`contracts._summary_statements` still contains `extract.decision_statements`
and no `re.compile` (pinned). Every generated composition over 25 characters
from the {1, 3, 5}-modifier rows (63 of them, up to 62 characters) is tested
as a bound summary in both syntaxes: `authorized` false, reason `CONFLICT`.

## 4. Test results

### New

`manager/test_nextplan_round8_findings.py` — **40 passed, 1051 subtests** at head.

| group | what |
|---|---|
| 0 | A1–A7 at the planner: read as `reject`, not MARK_COMPLETE, routed SEND_TO_REVIEW as `contradictory_result`, reason CONFLICT |
| 1 | colon matrix: (none + 5 owners) × {0,1,2,3,5} modifiers × 5 nouns = 150 generated fields; reject, approve and unreadable values; required rows at the planner; labels > 25 / > 32 / > 40 chars; markdown dressing |
| 2 | copula matrix: same 150; ownerless; 3 and 5 modifiers; five copulas; each statement's `raw` begins with the whole field |
| 3 | structural equivalence (above) |
| 4 | summary reuse: 63 long compositions × 2 syntaxes withdraw a bound PASS; approve summary never grants; unbound / off-target forged rejecting summary never blocks; ordinary summary reads as nothing and completes |
| 5 | P1–P6; reporting nouns not promoted under long modifier chains; single authority path agrees on a long contradiction; `_REJECT_VALUES` sha256 pinned; ordinary prose with decision nouns still nothing |

### Pre-fix behavioural run

The same module against the untouched base worktree at `ec5da6f7`:
**695 failed, 34 passed (subtests counted), 362 subtests passed** — 25 of the
40 tests red, including every group-0, group-1, group-2 and summary-withdrawal
test. The 15 green at base are invariants that already held (P1–P5, the
approve/forged/ordinary summary rules, the pinned sets and hash, ordinary
prose) and are there so they keep holding.

### Mutation controls — the tests are not vacuous

`mutate_r8.py`: each mutation is applied to the production source, the Round-8
module (and for M5 the Round-7 summary group) is run, the source is restored
and sha256-compared. No collection or import errors in any run; every failure
is a behavioural or structural assertion.

| mutation | result | tests red | includes |
|---|---|---|---|
| M1 reintroduce `{0,24}` label capture | 357 failed | 15 | every colon-matrix test, A1–A4/A7, long summaries, P6, authority-path |
| M2 copula owner required again | 133 failed | 13 | ownerless copula, A5/A6, copula summaries, structural containment and iff |
| M3 copula modifiers `{0,2}` again | 189 failed | 7 | 3- and 5-modifier copula tests, length thresholds, other copulas, containment, tier/copula agreement |
| M4 copula bypasses the shared grammar (Round-7 hand-written branch) | 399 failed | 16 | every structural-equivalence test, whole copula matrix, A5/A6, copula summaries |
| M5 `_summary_statements` no longer reuses `decision_statements` | 145 failed | 8 | all long-summary withdrawal tests, A7, and 3 Round-7 summary tests |

Byte-identical restore confirmed after each of the five.

A note on M3: with an optional owner, a modifier cap is not observable by
"was a rejection read" alone — the regex re-anchors two modifiers before the
noun and still finds one. The copula tests therefore also assert that each
statement's recorded `raw` begins with the reviewer's whole field, which is
what M3 breaks.

### `_REJECT_VALUES` unchanged

Base module (`git show ec5da6f7:manager/nextplan/extract.py`, executed) versus
head module: `_REJECT_VALUES` equal, **53 entries**, sha256 of the sorted
newline-joined set `1284e63ef16df8b2fa6d5f4be753817d84cbbe79c3bfeff5546244e14ca14737`
at both ends; `git diff ec5da6f7 -- manager/nextplan/extract.py` contains no
line mentioning `_REJECT_VALUES`. Also equal: `_APPROVE_VALUES`,
`_LABEL_OWNERS`, `_LABEL_MODIFIERS`, `_DECISION_NOUNS`, `_DECISIVE_LABELS`,
`_REPORTING_LABELS`.

### NextPlan suites, base vs head

| | base `ec5da6f7` | head | delta |
|---|---|---|---|
| passed | 409 | 449 | **+40** (the Round-8 module) |
| failed | 1 | 1 | **0 — same test** |
| subtests | 1775 | 2826 | **+1051** (the Round-8 module) |

Failure set compared by test name: **identical** —
`test_nextplan_verify.py::GitProbeRealRepositoryTests::test_honest_claims_are_all_verified`
at both ends, environmental, not touched. No existing test was deleted,
renamed or weakened.

### Whole repository, base vs head

Nothing outside `manager/nextplan/` imports the package, so this is a parity
check, not a blast-radius check. Both ends run with
`--continue-on-collection-errors`, because three modules (`cloud/test_asgi.py`,
`manager/test_dashboard_app.py`, `manager/test_mcp_adapter.py`) cannot import
here for want of `mcp`, `starlette` and `streamlit` — identically at base and
head.

| | base `ec5da6f7` | head | delta |
|---|---|---|---|
| passed | 2956 | 2996 | **+40** |
| failed (summary line) | 255 | 255 | 0 |
| collection errors | 3 | 3 | 0 |
| subtests passed | 2183 | 3234 | **+1051** |
| `FAILED`/`ERROR` set by name | 250 | 250 | **identical** (`diff` empty) |

The +40 and +1051 are exactly the Round-8 module. Nothing else moved in either
direction. The 255 pre-existing failures are outside NextPlan and outside this
round's scope; they fail identically at base.

### Accepted Round-7 semantics, re-proven

* R6-IR-3 fence semantics: `GroupFivePositiveControls.test_p4…` (closed
  ` ```text `/` ```markdown `/` ```json ` fences quoting long-grammar
  rejections complete) and `test_p6…` (an unclosed fence followed by
  `My current official final decision: reject` does not complete); the whole
  Round-7 `GroupThreeWrongChannelIsInert` still passes.
* R6-IR-4 authority topology: `test_the_single_authority_path_agrees_on_a_long_contradiction`
  asserts neither `review_proof` nor `signals_for` calls
  `contracts.review_authority` directly and that both refuse a 40-character
  contradiction; the Round-7 `GroupFourAuthorityConvergence` still passes.

## 5. False-positive sweep

`fp_sweep_r8.py`: every string literal (3 < length < 2000) in every existing
NextPlan test module plus every string in `manager/nextplan/fixtures/`, read by
`decision_statements` from the base module and from head.

| | |
|---|---|
| literals swept | 1421 |
| reading changed | **4** |

All four are test docstrings, not reviewer prose, and all change from `[]` to
`unreadable` via the ownerless copula (`A decision is collected and bound the
same way…`, `A decision *field* is a line…`, `…an unreadable decision was read
as no decision`). None becomes an approval. This is the cost of A6, stated in
TRUST-MODEL §8.5.1.

## 6. Isolation, safety and SSOT state

Fresh clone `~/dev/adm-round8-20260920` (branch) and a detached worktree
`~/dev/adm-round8-base` pinned at `ec5da6f7`, never edited, removed after the
measurements. `jsonschema` installed into the user site to run the suites;
the three modules the whole-repo suite cannot import here (`mcp`,
`starlette`, `streamlit`) are missing identically at base and head. Staging
explicit per path. Nothing under `~/.ai-development-manager` exists. No
force-push; the Round-7 branch tip is still `ec5da6f7`.

## 7. Postmortem note (PFP P10)

The PFP failure-pattern library lives in `ne9221/production-fix-protocol`,
which this task was instructed not to modify, so no `NFP-NNN` entry is added
here. Candidate pattern for the library owner, recorded so it is not lost: *a
contract published as a grammar was implemented with an unstated limit (a
character cap) ahead of the grammar, so the grammar was correct and never
consulted; the fix is one compiled source that every syntax form derives from,
with a containment test.* Whether that generalises is for the independent
reviewer and the library owner to decide.

## 8. Rule 32 is not satisfied

This is the implementer's own evidence. The remediation is not accepted until
a fresh independent reviewer, not this session and not the Grok session that
produced the Round-7 verdict, returns a verdict against it.
