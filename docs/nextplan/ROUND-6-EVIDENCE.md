# NextPlan Round 6 — measured evidence

Companion to [`REMEDIATION-ROUND-6-20260917.md`](REMEDIATION-ROUND-6-20260917.md).
Every number here was observed; none is inferred from intent
(AI-DEVELOPMENT-RULES rule 23).

| | |
|---|---|
| Implementer | Claude Opus 5 (`claude-opus-5`) |
| Base SHA | `405c90c4b26d0851937bf05651cfc15b6ec6dfde` (tree `530ed8db8f10217fc68a2e9e26904599fceadafb`) |
| Head SHA | `25ad4afabfd6ac337e644643752a8ed3f32d7274` (tree `94cd0d7bd71a6441329c2ff364e0d82805cacfea`) |
| Branch | `feat/nextplan-failure-atlas-foundation-20260916` |
| Python | 3.14.7 (Windows 11) |

## 1. Isolation

Two separate clones from GitHub, in a session scratchpad:

- `…/scratchpad/base-r6` checked out at `405c90c4`, never edited;
- `…/scratchpad/impl-r6` at the head commit.

Both paths are **129 characters**, deliberately equal: three ADM installer tests
are sensitive to checkout path length (PowerShell line wrapping), so unequal
paths would have produced a phantom base/head difference.

Untouched: the production runtime HOME `C:\Users\EE\.ai-development-manager`
(live — a scheduler was writing `runtime/` throughout) and the canonical
checkout `C:\Users\EE\Documents\ChatGPT\ai-development-manager` (dirty, on an
unrelated branch). Other AI sessions were observed running concurrently on
*other* projects; none writes into either clone used here.

## 2. Reproduction at base, before any edit

`scratchpad/repro_r6.py` run against the unmodified `base-r6` clone. Full output
preserved as `repro_r6_BASE.txt`.

| Finding | At `405c90c4` | At `25ad4af` |
|---|---|---|
| F1 bound PASS + decision-shaped contradiction | **12 / 30 MARK_COMPLETE** | **0 / 30** |
| F2 hand-written validation record, nothing spawned | `tests_run=999` **VERIFIED**, MARK_COMPLETE | `UNKNOWN`, no completion |
| F3 schema-valid PASS + contradictory extra field | **6 / 6 MARK_COMPLETE** | **0 / 6** |
| F4 non-authoritative fence collected as authority | **6 / 6** (json, yaml, text, markdown, example, bare) | **0 / 6** |

The 12 of 30 includes both sentences the review named
(`Current decision: reject`, `Final call: do not complete`).

**On F1's number.** The review reported 27 of 30 on its own corpus; 12 of 30 is
what this corpus produced, because these sentences were chosen for decision
*shape* and several of them already tripped the Round-5 rejection matcher. The
defect is the same and the named cases reproduce. The observed number is
reported rather than the reviewer's.

## 3. F1 — both directions

| Corpus | Base | Head |
|---|---|---|
| 30 fresh decision-shaped contradictions + bound PASS | 12 complete | **0 complete** |
| Round-5 `APPROVALS` (24 genuine approvals) + bound PASS | 0 refused | **0 refused** |
| 18 decision-shaped genuine approvals (Round-6 corpus) | — | **0 refused** |
| Round-5 `RESIDUAL` (8 sentences) | 8 complete | **4 complete** |

The four now closed were decision-shaped: `Current decision: reject`,
`My assessment is negative.`, `This fails my review.`, `I am not able to approve
the patch.` The four that remain state a finding or an instruction and announce
no verdict; see §6.

## 4. F2 — execution registry, forged and legitimate

`scratchpad/check_registry.py`, driven to a planner action.

**Refused (`tests_run` UNKNOWN, no MARK_COMPLETE):** fabricated `execution_id`;
the review's exact `exec-forged` block; an id from a previous task; an id from a
previous run of this task; a copied artifact; a record naming an artifact digest
other than the one ADM read; argv rewritten after the run; wrong task binding;
wrong run binding; an issued id whose process never started; no registry at all;
one unbound record in a set of two withholding the whole total; an unbound
record trying to prove `tests_failed = 0` from its own exit code.

**Counts never read from the record:** a *genuine* run whose record was edited
to claim `passed: 999` reports `tests_run = 2 VERIFIED` — the registry's real
number. The forged value is not disbelieved; it is not consulted.

**Legitimate, really spawned pytest:**

| Case | Result |
|---|---|
| 2 passing tests | `tests_run=2 VERIFIED` → **MARK_COMPLETE** |
| 0 tests collected (exit 5) | `tests_run=0 VERIFIED` → no completion |
| 1 failing test (exit 1) | `tests_run=1 VERIFIED` → no completion |
| report absent / unparseable | `UNKNOWN` → no completion |

Pre-existing artifact (case 5) is closed by construction: the report path is
nonce-named (`report-<32 hex>.xml`) inside a `TemporaryDirectory` the call
created, and asserted absent before the spawn.

## 5. Test runs

All at head unless stated.

| Suite | Result |
|---|---|
| `test_nextplan_round6_findings.py` (new) | **48 passed, 162 subtests** |
| Round 1–5 finding suites (review/round3/round4/round5/round6) | **200 passed, 680 subtests** |
| extract / verify / result / planner / property / scenarios | **130 passed, 726 subtests** |
| Full NextPlan selection (`-k nextplan`) | **372 passed, 1626 subtests** |
| Adjacent `-k "execution or runner or repo_write or validation or dispatch"` | **941 passed, 179 subtests, 1 failed** |

The one adjacent failure is
`test_github_dispatch_watcher.py::MainCliTests::test_main_missing_token_env_returns_nonzero`
— one of the five pre-existing whole-repo failures listed below, selected here
only because its filename contains "dispatch". It fails identically at base.

### Whole-repo, tree-pinned

Both runs are a bare `python -m pytest -q` at the repo root of their own clone.

| | base `405c90c4` | head `25ad4af` | delta |
|---|---|---|---|
| failed | 5 | 5 | 0 |
| passed | 3165 | 3214 | **+49** |
| subtests | 1921 | 2083 | **+162** |
| wall clock | 397.12s | 396.87s | — |

**The failure sets are identical**, verified by diffing the `FAILED` lines:

```
manager/test_command_watcher_ag.py::TestProcessCommandAgRouting::test_gate_pass_reaches_launch_task
manager/test_github_dispatch_watcher.py::MainCliTests::test_main_missing_token_env_returns_nonzero
manager/test_refresh_status.py::RefreshTests::test_refresh_appends_to_history_store
manager/test_refresh_status.py::ClaudeRefreshDiagnosticTests::test_refresh_appends_to_history_store
manager/test_refresh_status.py::ClaudeRateLimitDiagnosticTests::test_refresh_appends_to_history_store
```

These five pre-date this branch and are unrelated to NextPlan (command-watcher
AG routing, a dispatch-watcher CLI env check, and quota history in
`refresh_status`). They were **not** fixed here: that would be scope expansion
(AI-DEVELOPMENT-RULES rule 19).

The +49 is fully accounted for: 48 new Round-6 tests, plus a net +1 from
replacing two Round-5 residual tests with three (§6). The +162 is exactly the
Round-6 file's subtest count. Nothing else moved in either direction.

## 6. The Round-5 `RESIDUAL` contract

`GroupSevenFreshCorpora.RESIDUAL` asserted with `assertEqual` that eight
sentences **must keep completing**. Round 5 wrote it as an honest measurement;
the Round-6 reviewer ruled it a contract violation, because a test that pins
false-completes as correct behaviour makes them green.

Replaced by `OUT_OF_CONTRACT_COMMENTARY` — four sentences, **none required to
complete**:

| Sentence | Why it is left open |
|---|---|
| `Changes are required before I can sign off.` | states a finding; announces no verdict |
| `There remain unresolved concerns about locking.` | states a finding |
| `Two defects block acceptance.` | states a finding |
| `Hold the merge until CI is green.` | an instruction |

Three tests replace the two removed ones, and they assert the invariant that
actually holds: each of the four is **not decision-shaped**
(`decision_statements()` returns `[]`); no decision-shaped sentence in the
corpus completes; and putting the blocker in `findings` closes all four. So the
set can only shrink by the contract getting stricter, never by a decision
slipping through.

Reaching any of the four still requires a reviewer that returns a correctly
bound `PASS` with an **empty** `findings` list and then contradicts it in prose.

### One pre-existing over-refusal, recorded not chased

`A blocker was present last time; it is resolved.` is split at the semicolon by
the Round-5 clause splitter, so the resolution sits in a different clause from
the blocker and the vocabulary matcher cannot see it. Verified identical at
`405c90c4` before any Round-6 edit (`rejection_signal` returns
`'blocker was present'` there too), so it is neither a regression nor one of
Grok's findings. Closing it means widening rejection vocabulary — the move
Rounds 2–4 lost with. It fails closed: it costs a round, never a completion.
Recorded as a test so it cannot drift silently.

## 7. Git and SSOT state

```
branch  feat/nextplan-failure-atlas-foundation-20260916
base    405c90c4b26d0851937bf05651cfc15b6ec6dfde
head    25ad4afabfd6ac337e644643752a8ed3f32d7274
push    405c90c..25ad4af   (fast-forward)
```

`git ls-remote origin refs/heads/…` returns `25ad4afabfd6ac337e644643752a8ed3f32d7274`
— local == remote, verified against the remote rather than against the push
output. `git status --porcelain` is empty. Staging was explicit per path; no
`git add .`/`-A`, and no unrelated dirty file was touched.

**Not merged. Not activated. Not released.**

## 8. Backward compatibility

`decision_statements` became a required field of the normalized result
(`adm-normalized-result/1`). Normalized results are produced by `extract()` and
consumed in-process by `planner.plan` and `verify`; they are never persisted, and
NextPlan is not activated anywhere in production, so there are no stored records
to break. `blank_result` always supplies the field. A hand-built result missing
it fails validation and routes to a human gate — fail-closed.

## 9. Blocker and next step

**Blocker: none technical.**

**Rule 32 is NOT satisfied.** The next step is a fresh-context independent
review by an AI that is neither Claude Opus 5 in this implementation
conversation nor the Grok 4.6 session that produced the Round-5 verdict.

Attack order: the reviewer-contradiction contract; execution-registry
provenance; semantic extra-field rejection; the authoritative-channel
restriction. If those hold, **stop extending the natural-language matcher.**
