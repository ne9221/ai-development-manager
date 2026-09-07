# ADM EVIDENCE — Verification Loop Phase B3-2 NB-B/NB-C/NB-D Reproducibility Cleanup — 2026-09-07

**Status: B3-2 IMPLEMENTATION COMPLETE — INDEPENDENT REVIEW PENDING**

Not merged. Not activated. Not Phase B-3 complete. Not production ready. Not
Hands-off complete. The next step is a read-only adversarial review by a
different AI session; this implementer session does not review its own work.

## Provenance / lineage

- Project: AI 一体化 / ai-development-manager
- Repository: ne9221/ai-development-manager (GitHub, formal source SSOT)
- Task: Verification Loop Phase B3-2 — NB-B / NB-C / NB-D Reproducibility Cleanup
- Session: adm-verification-loop-b3-2-repro-cleanup-20260907
- Implementer: Claude Fable 5.1 (model claude-fable-5-1, reasoning medium-high)
- Governance: AI-DEVELOPMENT-RULES v0.5.0 (2026-09-01, 47 rules incl. §10 rules 46–47), Drive `1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU`
- Project rules: PROJECT-RULES — ADM v1.1.0 (2026-08-21), Drive `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ`
- Inputs read in full before any edit: ADM REVIEW — Phase B-2 Final Independent Re-review (PHASE_B2_ACCEPTED), Drive `1ddfjjme7a9zL429S0ga_EFlar-ajAOQXjlBgCDydK6U`; ADM EVIDENCE — Phase B3-1 Predicate 10 / NB-A Closure, Drive `1vugFHNgSZupxflnPl_T3iQCHXhbauxrPyIkAUjarv1I`; `production-fix-protocol` skill SKILL.md (Phases A–E applied; F is the pending review; G–J out of scope for an unmerged slice)
- Accepted B-2 lineage: `b38ceb26e29e46e83829cb1569de76525b7531e9`; B-2 reviewed base: `794db7011de4561b186a690e49f363e5120dc4cb`
- B3-1 implementation: `c296710ac999951eebea9ae6f9a8c25f7717bda1`; branch base for this slice (B3-1 cross-link head): `a44c775948ead2989c39db732125be45e0dfb210`
- Branch: `fix/verification-loop-b3-2-repro-cleanup-20260907`
- **Implementation commit (final SHA): `87eefdc2a9fb253bb896d5ebd745c7c262105755`**, pushed; remote == local (verified by `git ls-remote`). A docs-only follow-up commit on the same branch adds this record and the Drive cross-link.
- GitHub artifact: `docs/verification-loop/PHASE-B3-2-REPRO-CLEANUP-EVIDENCE.md` (this record); Drive counterpart: `1-LEnPzBJOlhjbQEugAdM0X_pZFLN2nCmRsyAqnWoVGA` (ADM project folder `16MO98FfbnwXsin-m6F1rgdmk-o6ZslVC`)
- Production origin/main: `047b21899116350a867d5031acb2b128ab04d235` — unmoved

## Scope

Exactly three residuals from the B-2 final review: NB-B, NB-C, NB-D. All three
are reproducibility / documentation defects in committed evidence tooling.
**No production code changed.** Deliberately not touched: NB-A / B3-1 logic,
Predicate 10, NB-E..NB-I, Dashboard, rule 44, production merge or activation,
any unrelated cleanup.

## Modified files (4, scoped staging by name)

- `docs/verification-loop/repro/B2R-ADVERSARIAL-AUDIT.py` (+128/−41): version adapter `bound()`, capability detection from the ticket dataclass, `N/A` verdict, control C3, version-profile banner, A5/A6/A10 made version-honest
- `docs/verification-loop/repro/B2R-MUTATION-MATRIX.py` (+132/−54): `resolve_root()` = `parents[3]` verified against repository markers; `apply_mutation()` / `restore()` / `run_suite(root)` / `main()`; `git status` of the mutated package compared before and after; docstring says fourteen
- `manager/test_verification_loop_b3_2_repro_harness.py` (new, 11 tests / 2 subtests)
- `docs/verification-loop/README.md` (+66/−18): B3-2 lineage row; audit and matrix paragraphs rewritten to the measured numbers; this record linked

## Phase A — read-only diagnosis (all three reproduced before any edit)

Preflight readback, measured: scratch clone at `…/scratchpad/b32` (130-char path), branch created from `a44c775`, 0 dirty files; base worktree `…/b32base` at `794db70`; production checkout `…\ai-development-manager-home-recovery-combined-20260822` at `047b218`, branch `main`, 0 dirty; `origin/main` = `047b218`.

### NB-B reproduction — committed audit cannot run at base

```
cd <worktree @ 794db70>; AI_MANAGER_HOME=<tmp>
PYTHONPATH=. python <committed B2R-ADVERSARIAL-AUDIT.py> <scratch>
```

Exit code **1**. Traceback: `ImportError: cannot import name 'consumed_against' from 'manager.verification_loop.fixtures'`. Confirmed head-only: `git diff 794db70 HEAD -- manager/verification_loop/fixtures.py` adds exactly `def consumed_against(` (+57/−3). The script is not tracked at `794db70` at all (it was added in B-2R), so "runnable at either SHA" could only ever mean "the head script run against a base tree", and that died at import before scoring a single attack.

### NB-C reproduction — matrix ROOT is the wrong directory

```
cd <clone @ a44c775>; AI_MANAGER_HOME=<tmp>
python docs/verification-loop/repro/B2R-MUTATION-MATRIX.py
```

`ROOT` as computed by the committed script: `…\b32\docs\verification-loop\repro` (its own directory). Output: `BASELINE NOT GREEN -- aborting` / `no tests ran in 0.01s`. Exit code **2**. Root cause: `ROOT = pathlib.Path(__file__).resolve().parent`; pytest with that cwd finds none of the five `manager/test_…` suites. Fail-closed, never measuring anything — exactly as the review said.

### NB-D reproduction — README count vs. matrix count (programmatic)

`ast`-parsed `MUTANTS` list: **14** entries, ids `M41..M54` (M54 listed before M53 in the source; both present, all unique). README before the fix, by regex: line 82 "thirteen", line 92 "13/13", line 93 "thirteen". Claim 13 ≠ actual 14.

## Phase B/C — what changed and why it is minimal

### NB-B — version adapter, not a copy of production

`fixtures.consumed_against` is imported inside `try/except ImportError`. The
script reads the ticket dataclass fields: at the repair head a ticket carries
`consumed_report_digest` (predicate 11: admission requires a *consumed* ticket
naming the answering report); at base that field does not exist and admission
requires an *issued* ticket (`admission.py:84` at `794db70`). `bound(tickets,
reports)` therefore returns `consumed_against(...)` where the helper exists and
the issued tickets unchanged where it does not. Any other combination
(helper present without the field, or the field without the helper) exits with
`UNKNOWN VERSION` rather than guessing. No production logic is duplicated: the
head path is the production fixture helper itself, and the base path is the
identity function on tickets the production controller issued.

Equivalence is **measured, not asserted**: new control **C3** hands the pure
evaluator the honest round with `bound()` tickets and must derive ACCEPTED. If
the adapter were wrong at either version, A8/A9/A11 (the three attacks that
use it) would score BLOCKED on a refusal they never earned, and C3 would show
BROKEN at that version.

Three attacks were made version-honest without weakening them: A5 no longer
invents a `consumed_report_digest` key on a base ticket (it reopens the
ticket exactly as before); A6 repoints a field base tickets do not have, so at
base it reports **N/A** with the reason ("the tamper would edit nothing the
loop reads") instead of a BLOCKED it did not earn; A10 at base derives with the
real preflight facts (which carry no lease) instead of returning the literal
string "ACCEPTED" without deriving.

### NB-C — ROOT from repository structure, fail loudly if moved

`resolve_root()` takes `parents[3]` of the script and verifies two markers
under it (`manager/verification_loop/stores.py`, `docs/verification-loop/repro`).
A moved or copied script raises `SystemExit` naming the wrong directory and the
missing markers. The run loop is unchanged in logic and now lives in `main()`
with `apply_mutation()` / `restore()` extracted so the byte-exact restore is
unit-testable on LF and CRLF files; `git status --porcelain --
manager/verification_loop` is compared before and after the whole run (same
pattern as the B3-1 matrix) and the exit status is non-zero unless the tree is
unchanged. No absolute path anywhere; Windows CRLF checkouts handled as before.

### NB-D — number from the run, not from the count

README now says "14 mutants (M41–M54)" and "14/14 killed", in a form the new
test parses and compares against `len(MUTANTS)` and against this session's
actual run. The README also states, rather than hides, that the B-2R headline
for the audit had been measured with a scratch harness and not the committed
script, and what each base verdict is.

## Phase B/D — fixed behaviour, measured with the committed scripts

### Adversarial audit, same script at both versions

| | base `794db70` | head (this branch) |
|---|---|---|
| Version profile | issued-ticket admission (no consumption digest on tickets) | consumption-bound tickets (predicate 11; `consumed_against` present) |
| C1 honest round | OK ACCEPTED | OK ACCEPTED |
| C2 honest FAIL → REPAIR | OK | OK |
| **C3 adapter-bound honest round (equivalence)** | **OK ACCEPTED** | **OK ACCEPTED** |
| A1 stored FAIL edited into PASS | **BYPASSED** ACCEPTED | BLOCKED REPORT_CONTENT_DIGEST_MISMATCH |
| A2 stored PASS edited into FAIL | **BYPASSED** ACCEPTED → BLOCKED_HUMAN | BLOCKED REPORT_CONTENT_DIGEST_MISMATCH |
| A3 rogue expected checker | **BYPASSED** ACCEPTED | BLOCKED TICKET_CONTENT_DIGEST_MISMATCH |
| A4 ticket task_id rewritten | BLOCKED IN_VERIFICATION | BLOCKED TICKET_CONTENT_DIGEST_MISMATCH |
| A5 consumed ticket reopened | **BYPASSED** ACCEPTED (tamper honoured) | BLOCKED TICKET_CONTENT_DIGEST_MISMATCH |
| A6 consumed_report_digest repointed | **N/A** (field absent at base) | BLOCKED TICKET_CONTENT_DIGEST_MISMATCH |
| A7 second checker on an answered ticket | BLOCKED IN_VERIFICATION | BLOCKED IN_VERIFICATION |
| A8 base FAIL claimed vs PASS attestation | **BYPASSED** REPAIR_TEST_ONLY | BLOCKED OPEN_BUNDLE_REVISION / BASE_EVIDENCE_CONTRADICTION |
| A9 Task A evidence on Task B | BLOCKED PENDING | BLOCKED PENDING |
| A10 stale worktree lease generation | **BYPASSED** ACCEPTED (no lease on base PreflightFacts) | BLOCKED ROUND_INVALIDATED |
| A11 PASS with hidden failures | **BYPASSED** ACCEPTED | BLOCKED IN_VERIFICATION |
| Summary line | `3/11 attacks blocked, 7 bypassed, 1 not applicable at this version; 3/3 controls still healthy` | `11/11 attacks blocked, 0 bypassed, 0 not applicable at this version; 3/3 controls still healthy` |
| Exit | 1 (documented) | 0 |

The B-2R README headline "7 of 11 bypass at base" happens to match the count,
but its explanation ("the four that fail to bypass do so through guards B-2
already had") was wrong: three are stopped by existing guards, one (A6) is not
expressible at base. The README now says exactly that.

### Mutation matrix, committed script, run in place

```
cd <clone>; AI_MANAGER_HOME=<tmp>
PYTHONPATH=. python docs/verification-loop/repro/B2R-MUTATION-MATRIX.py
```

`ROOT: …\b32` (repository root). git status before (manager/verification_loop): clean. Baseline green: 249 passed, 12 subtests, 7.59 s.

| Mutant | Guard neutralised | Failing | Target |
|---|---|---|---|
| M41 | read-time content-digest verification (both ledgers) | 10 | HIT |
| M42 | ticket read-time verification only | 5 | HIT |
| M43 | canonical-bytes check | 1 | HIT |
| M44 | predicate 11 digest comparison | 3 | HIT |
| M45 | conflicting consumptions: first writer wins | 1 | HIT |
| M46 | baseline precedence | 8 | HIT |
| M47 | contradiction detection | 2 | HIT |
| M48 | cross-Task barrier | 1 | HIT |
| M49 | worktree generation comparison | 2 | HIT |
| M50 | worktree lock-id comparison | 1 | HIT |
| M51 | PASS-with-failures self-contradiction | 1 | HIT |
| M52 | consumption without a digest | 1 | HIT |
| M54 | predicate 11 as a positive requirement | 2 | HIT |
| M53 | unreported lease reads as matching | 1 | HIT |

**14/14 KILLED by their named target test; 0 survived, 0 not applied, 0 killed for the wrong reason.** git status after: clean; `tree unchanged by matrix: True`; exit 0. The full-tree `git status --porcelain` snapshots taken around the run differ only by this session's own concurrent README edit (outside the mutated package).

### NB-D old/new claim

| | Old README | Matrix (ast) | New README | Run |
|---|---|---|---|---|
| Mutant count | thirteen / 13/13 | 14 | 14 mutants (M41–M54) / 14/14 | 14/14 KILLED |

## Phase D — tests and regression

1. **Targeted**: `manager/test_verification_loop_b3_2_repro_harness.py` — 11 tests, 2 subtests, all passing at head (4.6 s). Coverage of the required list: audit runs at base (git-archive extraction of `manager/` at `794db70`, no worktree registered, skips with a stated reason only if the commit is absent); audit runs at head from a foreign cwd; output structure parsed and verdict sets pinned at both; matrix ROOT resolves to the repository root from a foreign cwd; a moved copy (both deeper and shallower) is refused by name; all 14 mutants unique, search text present, target test defined in the suites; byte-exact restore on LF and CRLF; absent search text leaves the file untouched; README mutant count == `len(MUTANTS)`; README audit numbers == pinned measurements; audit leaves `git status` unchanged. Red-first: the two README tests failed against the old README before it was edited.
2. **Focused subsystem** (with the new module tracked, so the writer audit reconciles): the six verification-loop suites + the new module + `test_phase1_cursor_writer_audit.py` — **304 passed, 101 subtests passed, 0 failed** (27.8 s).
3. **Full project regression** at this head, fresh temporary `AI_MANAGER_HOME`:

| Run | Scope | failed | passed | subtests | wall |
|---|---|---|---|---|---|
| head `87eefdc` | `manager/` package | 2 | 2837 | 435 | 436.96 s |
| head `87eefdc` | whole project (`pytest` at repo root) | 2 | 3118 | 477 | 409.19 s |
| reference: B3-1 head `c296710` (from its record) | whole project | 3 | 3106 | 475 | 408.19 s |

Failure IDs at this head, both scopes, are exactly the two NB-G tests below. Reconciliation with the B3-1 record: 3106 + 1 (the writer-audit test that failed there only because the B3-1 repro scripts were untracked at run time, and passes here) + 11 (this module) = 3118 passed; 475 + 2 = 477 subtests. **No new delta failure.**

No production code is in the delta, so the base run is the B3-1 record's head measurement (`c296710`: 3 failed / 3106 passed, where the third was the untracked-file audit artefact). Known pre-existing failures (NB-G): `manager/test_command_watcher_ag.py::TestProcessCommandAgRouting::test_gate_pass_reaches_launch_task` and `manager/test_github_dispatch_watcher.py::MainCliTests::test_main_missing_token_env_returns_nonzero` (non-hermetic).

## Phase E — non-vacuity

- The three residuals were reproduced with the committed scripts before any edit (exit 1 ImportError; exit 2 wrong ROOT; 13 vs 14 by `ast`).
- The README tests were written first and failed against the old README.
- The adapter's equivalence is a live control (C3) at both versions, not a comment.
- The matrix's own harness (14/14 with byte-exact restore) is the mutation evidence for the guards; this slice adds no production guard, so no new production mutant applies. The harness-level checks (moved script refused, absent search text → untouched file, CRLF restore) are unit-tested directly.

## Findings recorded, not fixed (out of scope)

- F1. `B2R-MUTATION-MATRIX.py` lists M54 before M53; harmless, left as is.
- F2. The audit is not tracked at `794db70`; "runnable at base" necessarily means the head script against a base tree. The README now says so.
- F3. Predicate 10 / NB-A / NB-E..NB-I untouched, as instructed.

## Production untouched proof (before and after)

- Production checkout `…\ai-development-manager-home-recovery-combined-20260822`: HEAD `047b21899116350a867d5031acb2b128ab04d235`, branch `main`, 0 dirty paths; B3 objects absent.
- Production manager home `C:\Users\EE\.ai-development-manager`: no `verification/` store created; every audit/matrix run used a fresh temporary `AI_MANAGER_HOME` and a `/tmp` store root.
- `origin/main`: `047b21899116350a867d5031acb2b128ab04d235`, unmoved. Nothing merged, nothing activated.

## Git status / GitHub push state

- Working tree clean after commit; the four paths above were added by name (no blanket staging).
- Branch `fix/verification-loop-b3-2-repro-cleanup-20260907` pushed to origin; local `87eefdc` == remote `87eefdc`.

## Quota evidence (rules 11/30)

Source: `runtime/quota_history.json`, `source=claude_oauth_usage`, `source_type=official`, `confidence=official`, observed 2026-09-07T05:53:30Z (task start 05:53Z). account-a: five_hour 51 % used (resets 10:00Z), seven_day 59 % used (resets 2026-09-09T20:00Z). account-b: five_hour 22 %, seven_day 4 %. `statusline-payload.json` is dated 2026-09-02 and is treated as **stale**. No model change on quota grounds.

## Next step — reviewer brief (a different AI session, read-only, adversarial)

1. Re-run `B2R-ADVERSARIAL-AUDIT.py` unmodified against a `794db70` tree and against this head, from short scratch directories with a temporary `AI_MANAGER_HOME`. Expect exit 1 with 7/3/1 and exit 0 with 11/0/0, C1–C3 OK at both.
2. Challenge the adapter: is "issued ticket" truly the base-equivalent of "consumed ticket naming the digest"? C3 measures that the honest round passes; try to construct a base case where an issued ticket admits something a base consumed ticket would not (or vice versa) and show whether any attack's verdict depends on it.
3. Re-run `B2R-MUTATION-MATRIX.py` at this head. Expect `ROOT` = repository root, 14/14 KILLED, tree unchanged, exit 0. Then copy the script one directory up and confirm it refuses by name.
4. Confirm the README's numbers are the ones the scripts print, and that `test_verification_loop_b3_2_repro_harness.py` would go red if either README number or either verdict set drifted.
5. Confirm no production code changed (`git diff a44c775..<final> --stat` touches only `docs/` and one new test module) and that production is untouched.

Verdict vocabulary for the reviewer: `B3_2_ACCEPTED` / `CHANGES_REQUIRED`. The reviewer must not repair anything (AI-DEVELOPMENT-RULES rule 32).
