# Remediation Round 5 — the structured evidence boundary

Base `5d305fc98d0f355a0b8fb0d1f34a5a72f14efde3` (Round 4).
Implementer: Claude Opus 5. **Not reviewed. Not merged. Not activated.**

Round 4's independent review (Codex job `review-mu4akg9z-11yhm4`) returned
REJECT with 4 high findings and 1 medium. This round does not answer them one by
one, because they are not five bugs. They are five views of one:

> **Natural language was being used as authority.** A sentence has no target, no
> run identity and no provenance, so there is nothing in it to check — and a
> command string plus its stdout has the same problem about processes.

## 1. Reproduction at base, before anything was changed

All five findings were reproduced at `5d305fc9` (rule 24). Script:
`scratchpad/repro_r4.py` (not committed; it is a harness, not a test — the
committed equivalents are in `manager/test_nextplan_round5_findings.py`).

| # | Finding | Reproduced at base |
|---|---|---|
| 1 | echo-only execution → VERIFIED tests | **yes** — real subprocess, exit 0, `tests_run=12 VERIFIED`, `MARK_COMPLETE` |
| 2 | forged anchors authorize | **yes** — 3/3 (`## Example`, `Previous reviewer statement:`, `Example session:` + blank line) |
| 3 | resolved wording cancels a live blocker | **yes** — 2/3 of the reported constructions completed |
| 4 | unknown/empty decision read as silence | **yes, in the anchored form** — see below |
| 5 | over-refusal of genuine wordings | **yes** — 7/9 genuine approvals refused |
| — | headline: rejection corpus with a PASS anchor | **yes, as a class** — 6/25 on my fresh corpus (the reviewer measured 20/25 on theirs) |

**One correction to the finding as written.** Finding 4 states that
`Current decision: reject` and `Decision:` *complete*. On their own they do not —
they produce UNKNOWN and block. They reproduce in the **anchored** form, which
is what "read as silence" must mean: with `Verdict: PASS` also present, an
unreadable or unrecognised decision fails to withdraw it and the task completes.
The defect is real and the diagnosis is exactly right (`Decision: REJECT` blocks
while `Current decision: reject` does not — authority by vocabulary); only the
minimal repro differs.

## 2. What changed

Two new contracts, both published as JSON Schema so an external agent can emit
them, and both validated in one place.

### `adm-review-result/v1` — reviewer authority

`manager/nextplan/contracts.py`, `schema/adm_review_result.schema.json`.

A reviewer authorizes by returning a structured object, never by writing a
sentence. Shape is necessary but never sufficient — a forged JSON block is no
harder to type than a forged sentence. What a forger cannot supply is the
**binding**: `target_sha` must be the candidate ADM is holding, and
`reviewer_run_id` / `provenance` must match the reviewer run **ADM itself
dispatched**. With no dispatch record on file, nothing can authorize.

`SEND_TO_REVIEW` clears `review_dispatch`, so a superseded approval cannot be
replayed against a later candidate.

Conflict semantics (requirement B) live in `contracts.review_authority` and are
listed in `TRUST-MODEL.md` §1. The one that mattered most: an empty, unknown or
malformed decision is **invalid, and invalid blocks**. It is never silence.

### `adm-validation-result/v1` — execution provenance

`manager/nextplan/runner.py`, `schema/adm_validation_result.schema.json`.

Counts come from a runner adapter that spawns an **argv list** with
`shell=False` and reads the numbers from the **structured report the runner
itself wrote** (pytest's JUnit XML). `echo` cannot write a JUnit XML.

Fixing the reported regex (`_SEGMENT` splitting on `;` but not `&`) would have
closed that one instance and left the shape intact, because designation was
decided for the *command string* while counts came from the *whole output*:
`pytest && echo "===== 999 passed ====="` defeats a perfect classifier. So
legacy shell-string records can no longer produce counts **at all** — including
with an explicit `kind: test`. They still carry their exit code; they simply
cannot say how many tests ran.

### Demotion, not deletion (requirement D)

`is_test_command`, the summary parser and the rejection-prose families are all
kept and are all still accurate. None of them gates anything any more:

- prose extraction → annotation, recorded at `REPORTED` for the audit trail;
- structured reviewer result → authority;
- structured validation result → execution evidence.

Prose retains exactly one power: it may **withdraw** a structured PASS it
contradicts. Withdrawal-only rules are safe because their failure mode is
costing a round.

One narrow structural fix on that net: `_resolved_after` now stops at a
contrastive conjunction, because a resolution after `although` / `but` /
`even though` is about the other side of the contrast. That closes the reported
`A blocker remains although the timeout is fixed.` It is a grammatical
constraint on where a cancellation may be read from, not a new vocabulary.

## 3. Tests

`manager/test_nextplan_round5_findings.py`, 9 groups. Requirements E1–E14 are
mapped onto test names (`test_e1_…` … `test_e14_…`). Every security-sensitive
case ends at a **planner decision**, not a return value (requirement F):
`GroupEightPlannerLevelGate` drives 20 forged/ambiguous reviewer cases and 10
unproven-execution cases to `plan()` and asserts `MARK_COMPLETE` is unreachable,
then asserts both honest paths still reach it.

Two tests spawn **real processes** rather than using fixtures:

- `test_e1_the_exploit_run_for_real_still_cannot_verify_tests` runs Codex's exact
  command through a shell, confirms it exits 0 and prints `12 passed`, confirms
  the parser still reads `12`, and then shows ADM leaving `tests_run` UNKNOWN;
- `test_e14_a_real_pytest_execution_verifies_and_completes` writes a 3-test file,
  runs pytest through the adapter, and completes on the JUnit counts.

Corpora are **fresh**: 30 rejections and 24 approvals, none appearing in Codex's
report or in the Round-3/Round-4 files. Round 4 passed its own corpus and then
lost 20 of 25 on the reviewer's, so a corpus reused from the implementation is
worth nothing.

### Existing tests changed

Six files. Every edit carries an in-file comment naming what it used to assert
and why the contract changed. No test was deleted, and no assertion was
weakened without a stronger one taking its place:

| File | Change |
|---|---|
| `test_nextplan_round4_findings.py` | `test_an_explicit_decision_authorizes` **inverted** — prose decisions must now NOT authorize, and a bound decision must. Group 2 approvals now assert no withdrawal + completion with a bound decision. Group 3 execution cases moved to the adapter, each additionally pinning that the legacy channel proves nothing. |
| `test_nextplan_review_findings.py` | 5 "genuine summary → VERIFIED" cases now pin **both** halves (legacy proves nothing; adapter still completes). Negative cases unchanged. |
| `test_nextplan_round3_findings.py` | `review_text()` also emits a bound decision, so the rejection cases still fail *because of the prose*. Zero-test case pinned on both channels. |
| `test_nextplan_scenarios.py` | Reviewer envelope emits a decision block; `dispatched()` records the run after `SEND_TO_REVIEW`. |
| `test_nextplan_verify.py` | One evidence fixture gains `execution_id`/`argv`; assertion unchanged. |
| `test_nextplan_planner.py` | One fixture gains `review_dispatch`. |

## 4. Residuals, measured

Stated as numbers, not as claims they do not matter. Full text in
`TRUST-MODEL.md` §5.

1. **The prose withdrawal net has wording coverage.** 8 of 30 fresh rejections
   still complete **when the reviewer also returns a correctly bound structured
   PASS with an empty `findings` list** — a reviewer contradicting its own
   machine-readable decision. Pinned exactly as
   `GroupSevenFreshCorpora.RESIDUAL` so it cannot drift unnoticed. Without a
   bound decision the same corpus is **0 of 30**; Round 4's equivalent number
   was 20 of 25 on an ordinary written rejection. Supplying the blocker in
   `findings`, as the contract asks, closes every case.
2. `_resolved_after` is narrowed, not complete; it is still lexical.
3. **Built, not activated.** `repo_write_enforcement.py` still records legacy
   steps and nothing in production emits a reviewer decision. Today's effect is
   fail-closed: real ADM validation records cannot satisfy the completion proof.
   Wiring the adapter into the live write path and injecting the reviewer
   contract into dispatch prompts are separate reviewed changes. The
   `adm-result` milestone is deliberately **not** started (requirement G).
4. The runner adapter covers pytest only.

## 5. For the independent reviewer

Governance (rule 32, and this round's own constraint): the reviewer must not be
the implementer, and **must not be Codex job `review-mu4akg9z-11yhm4`**, which
produced the findings being remediated.

⚠ **You need a writable isolated test environment.** Two reviewers running could
not verify the claimed test counts (temp-directory creation blocked; `--noconftest`
runs; adjacent suites failing from the environment rather than from the code).
This round's numbers were reproduced in a scratch clone with working temp dirs —
if yours cannot create temp directories, `test_e14` and
`test_e1_the_exploit_run_for_real…` cannot run at all and the counts stay
unverified for a third time.

Attack this first:

1. **Is the binding real, or can it be forged?** `contracts._binding_problems` is
   the whole gate. Anything that makes `review_authority` return
   `authorized: True` without ADM having dispatched that run is a critical
   finding.
2. **Can counts reach `VERIFIED` from anything other than a JUnit report?**
   `verify.adm_test_evidence` and `TestEvidenceProbe`'s `counted` predicate.
3. **Is `MARK_COMPLETE` reachable** from any forged, ambiguous or unproven input
   not already in `GroupEightPlannerLevelGate`? Bring your own corpus; mine is
   fresh but it is still mine.
4. **Did the six changed test files smuggle a weakening?** Each edit is
   commented; check the comments against the diff.
5. **Is the residual honestly bounded?** Specifically: is case 1 above really
   limited to a self-contradicting reviewer, or is there a path where an honest
   reviewer's rejection is accompanied by a bound PASS it did not intend?
