# Verification Loop — design and review lineage

This directory is the GitHub-side durable copy of the Verification Loop design
and review record. Google Drive (ADM project folder
`16MO98FfbnwXsin-m6F1rgdmk-o6ZslVC`) remains the human-readable governance
store per AI-DEVELOPMENT-RULES v0.5.0 rules 25/29; these files exist so the
same text is also version-controlled, diffable and hash-verifiable.

Until 2026-09-06 the Phase A v3 architecture and the Phase B-1 independent
review existed **only** in local Claude Code session transcripts. Both
independent reviewers flagged that as a durable-record gap. This directory
closes it.

## Lineage

| Stage | Artifact | Verdict |
|---|---|---|
| Phase A v3 | [PHASE-A-V3-ARCHITECTURE.md](PHASE-A-V3-ARCHITECTURE.md) | `READY_FOR_PHASE_B` |
| B-1 implementation | commit `109be49` (`manager/verification_loop/`) | — |
| B-1 independent review | [PHASE-B1-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md](PHASE-B1-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md) | `CHANGES_REQUIRED` |
| B-1R repair | commit `62b403b` | — |
| B-1R independent re-review | [PHASE-B1R-INDEPENDENT-REVIEW-ACCEPTED.md](PHASE-B1R-INDEPENDENT-REVIEW-ACCEPTED.md) | `PHASE_B1_ACCEPTED` |
| B-2 implementation | commit `794db70` | — |
| B-2 independent review | [PHASE-B2-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md](PHASE-B2-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md) | `CHANGES_REQUIRED` |
| B-2R repair | commit `8ba1aaa` | — |
| Predicate 11 repair | commit `b38ceb2` | `PREDICATE11_FIX_ACCEPTED` (focused re-review) |
| B-2 final independent re-review | [PHASE-B2-FINAL-INDEPENDENT-REREVIEW-ACCEPTED.md](PHASE-B2-FINAL-INDEPENDENT-REREVIEW-ACCEPTED.md) | `PHASE_B2_ACCEPTED` |
| B3-1 NB-A consumption binding | [PHASE-B3-1-NBA-CONSUMPTION-BINDING-EVIDENCE.md](PHASE-B3-1-NBA-CONSUMPTION-BINDING-EVIDENCE.md) | `B3-1 IMPLEMENTATION COMPLETE — INDEPENDENT REVIEW PENDING` |
| B3-2 NB-B/NB-C/NB-D reproducibility cleanup | [PHASE-B3-2-REPRO-CLEANUP-EVIDENCE.md](PHASE-B3-2-REPRO-CLEANUP-EVIDENCE.md) | `B3-2 IMPLEMENTATION COMPLETE — INDEPENDENT REVIEW PENDING` |

## Reproducers

`repro/B31-NBA-FORGED-CONSUMPTION.py` is the Phase B3-1 reproducer for the
B-2 final review's residual NB-A. It uses only API present at both `b38ceb2`
and the B3-1 head, so the two runs are one experiment measured twice:

```
PYTHONPATH=. python docs/verification-loop/repro/B31-NBA-FORGED-CONSUMPTION.py <short-scratch-dir>
```

Measured at `b38ceb2`: **5 of 16 forged-consumption shapes derive ACCEPTED**
(the rogue-expected-checker vector A1, the erased forbidden identity, the
rewritten issuer and issue time, and forged consumption plus forged PASS);
the other 11 are stopped only by a sibling admission guard, which the script
reports as such rather than counting as a defence. At the B3-1 head all 16 are
refused by the target guard, `TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE`, and the
six controls (honest ACCEPTED, honest FAIL → REPAIR, crash window → PENDING,
recovery → ACCEPTED, identical replay idempotent, conflicting consumption
invalidated in both orders) are identical at both commits. Use a short scratch
directory: the ledger files records flat precisely because this repository is
tested near the Windows path-length cliff.

`repro/B31-MUTATION-MATRIX.py` is the B3-1 mutation matrix: three mutants,
each neutralising exactly the binding B3-1 added (the pure comparison, the
fold's use of it, and the mutable-field allowlist), each with a named target
test that must be among the failures, and each re-running the NB-A attack so
the matrix shows the attack *reopening* rather than merely a test going red.
Its `ROOT` is `parents[3]` — the repository root — and it verifies every
mutated file is restored byte-for-byte and that the working tree is exactly
as it found it. Measured at the B3-1 head: **3/3 killed by their named
target, 0 survived, 0 not applied, 0 killed for the wrong reason**; under the
first two mutants A1 re-derives ACCEPTED.

`repro/B2R-ADVERSARIAL-AUDIT.py` is the Phase B-2R self-adversarial audit: 11
attacks and 3 live controls. It runs **unmodified** at the Phase B-2 base
`794db70` and at every later head, from a scratch clone with a temporary
`AI_MANAGER_HOME` and a short scratch directory:

```
PYTHONPATH=. python docs/verification-loop/repro/B2R-ADVERSARIAL-AUDIT.py <short-scratch-dir>
```

It is committed rather than left in a scratch directory so the next reviewer can
re-measure the claim instead of taking it. The B-2 final review found (residual
NB-B) that the first committed version imported the head-only fixture helper
`consumed_against` and so died with `ImportError` at `794db70` — the headline
below had been measured with a scratch harness, not with the committed script.
Phase B3-2 replaced that import with a version adapter, `bound()`: it detects
from the ticket dataclass which admission contract is present (a consumed ticket
naming the report digest at the repair HEAD; an *issued* ticket at base, where
no digest exists), uses the production fixture helper where it exists and the
issued ticket where it does not, and exits rather than guessing at any other
combination. It copies no production code. Control **C3** runs the honest round
through the pure evaluator with exactly that binding, so a wrong adapter shows
as `BROKEN` at the version where it is wrong instead of scoring attacks as
blocked on a refusal they never reached.

Measured at `794db70`: **7 bypassed, 3 blocked, 1 not applicable (A6)**, exit
status 1. Bypassed: A1 stored FAIL edited into PASS → ACCEPTED; A2 stored PASS
edited into FAIL moves the derivation (ACCEPTED → BLOCKED_HUMAN); A3 rogue
expected checker → ACCEPTED; A5 consumed ticket reopened as issued is honoured;
A8 base-FAIL claim against a PASS attestation buys `REPAIR_TEST_ONLY`; A10 a
moved worktree lease is invisible because base `PreflightFacts` carries no
lease; A11 PASS with hidden failure observations → ACCEPTED. Blocked by guards
Phase B-2 already had: A4 (`IN_VERIFICATION`), A7 (`IN_VERIFICATION`), A9
(`PENDING`, the cross-Task barrier). Not applicable: A6 repoints
`consumed_report_digest`, a field base tickets do not have, so the tamper would
edit nothing the loop reads and the script reports `N/A` rather than a block
it did not earn. Measured at the B3-1/B3-2 head: **11/11 blocked, 0 bypassed,
0 not applicable**, exit status 0. **3/3 controls** healthy at both.

Both measurements are locked by
`manager/test_verification_loop_b3_2_repro_harness.py`, which runs the
committed script at this head from a foreign cwd and at `794db70` from a
`git archive` extraction (skipping, and saying so, only if that commit is not
in the local object store) and pins the verdict sets above.

The script is deliberately layout-agnostic: it finds ticket records by reading
the `ticket_id` inside them rather than by any filename convention. An earlier
version assumed the layout, silently matched nothing after the ledger changed
shape, and scored four attacks as bypasses that had never touched a byte.

`repro/B2R-MUTATION-MATRIX.py` is the Phase B-2R mutation matrix: 14 mutants (M41–M54),
each neutralising exactly one guard the repair added, each with a
**named target test** that must be among the failures. A mutant killed by some
unrelated test elsewhere says nothing about whether the guard is under test,
which is the vacuity trap both earlier reviews found; this harness scores that
case as `KILLED_WRONG_REASON`, not as a kill. It also reports `NOT_APPLIED`
when a mutation's search text is absent, counts a crash as a kill,
sha256-verifies every mutated file is restored byte-for-byte before the next
mutant runs, and compares `git status` of the mutated package before and after
the whole run.

```
PYTHONPATH=. python docs/verification-loop/repro/B2R-MUTATION-MATRIX.py
```

Its `ROOT` is `parents[3]` of the script — the repository root — verified
against repository markers before anything runs. The B-2 final review found
(residual NB-C) that the first committed version resolved `ROOT` to this
`repro/` directory, handed pytest no tests and exited 2 with `BASELINE NOT
GREEN` on every checkout: fail-closed, but never measuring anything; the review
also found (NB-D) that this paragraph said "thirteen" for a matrix that defines
fourteen. Both were repaired in Phase B3-2, and
`manager/test_verification_loop_b3_2_repro_harness.py` now pins the root
resolution (from any cwd, refusing a moved copy), the mutant count against this
paragraph, every mutant's search text and named target, and byte-exact restore
on LF and CRLF files.

Measured at the B3-2 head with the committed script: **14/14 killed by their named target, 0 survived,
0 not applied, 0 killed for the wrong reason**, tree unchanged by the matrix.
Two of the fourteen survived the first B-2R run and are worth reading about in
the commit history: both were guards the suite did not actually depend on,
masked by a guard one layer away, and the fix was a test that isolates them
rather than a change to the production code.

`repro/P11-CRASH-WINDOW.py` reproduces the Codex review finding that Phase B-2R's
first cut of predicate 11 left open. The controller persists a report and appends
the ticket's consumption record as two filesystem steps; a crash between them left
the report durable and the ticket still `issued`, and admission only compared the
consumed digest once a ticket said `consumed`. Measured before the fix:
`persisted_reports=4`, all four tickets `issued`, derivation **ACCEPTED** with
`invalidated_report_reasons=()`. After: **TICKET_NOT_CONSUMED**, and appending the
missing consumption record afterwards accepts again — the window is recoverable,
not terminal, which is what separates an interrupted round from a forged one.

## Provenance

`PHASE-A-V3-ARCHITECTURE.md` is the **verbatim, unedited** final assistant
message of the Opus session titled "Verification Loop Acceptance Layer Phase A
架構", transcript
`~/.claude/projects/C--Users-EE--ai-development-manager/5ad214ad-a62a-4ccc-b16a-1655360fd6d2.jsonl`,
message index 93. It was extracted programmatically and **not** rewritten,
summarised, corrected or re-scoped. Where Phase B-1/B-1R deviate from it, the
deviation is recorded in the review documents, never by editing this file.

The three review files are likewise the verbatim final verdict messages of
their respective review sessions (`f22f728a-…` index 250, `3baee210-…` index
389, `b73955eb-3926-4931-bbfc-3c9c9dfd7a62` index 474). Verbatim means
verbatim: the B-2 file's closing line is the reviewer's conversational offer
to publish, kept rather than trimmed, because an implementer editing a
reviewer's verdict — even to tidy it — is the thing this directory exists to
make impossible.

Recorded digests (sha256 of the file bytes as committed):

| File | Bytes | sha256 |
|---|---|---|
| `PHASE-A-V3-ARCHITECTURE.md` | 46055 | `2c5dc883a7039de60433f73f187a063babad8db8071cf875b778bce81df7903c` |
| `PHASE-B1-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md` | 9327 | `8462d21a7d8523ad02e7ffa224b2553045c79a780e9232f92a699ae242cb950a` |
| `PHASE-B1R-INDEPENDENT-REVIEW-ACCEPTED.md` | 5028 | `adbf856eeba391386266bce1516c27b2d1c5cde71dac7d552d73b69cfa9ed7c0` |
| `PHASE-B2-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md` | 11300 | `59f9b6607b479dda8c1ea29894a8a491f6bc8d5406778bbd7a250596908a8a02` |

## Governance sources these documents are bound to

- `AI-DEVELOPMENT-RULES` — Google Drive `1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU`, **v0.5.0**, 2026-09-01.
- `PROJECT-RULES — ADM` — Google Drive `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ`, **v1.1.0**, 2026-08-21.

The repository's own top-level `AI-DEVELOPMENT-RULES.md` is a **stale v0.1.5
copy** and is not authoritative. Phase A v3 section B hard-codes
`github_copy_authoritative: false` for exactly this reason.
