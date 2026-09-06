# ADM REVIEW — Verification Loop Predicate 11 Crash-Consistency Focused Re-review

- **Project**: AI 一體化 / `ne9221/ai-development-manager`
- **Reviewed delta**: `8ba1aaa96c72ac8035acece75c659814bdeb6d25` → `b38ceb26e29e46e83829cb1569de76525b7531e9`
- **Branch under review**: `fix/verification-loop-predicate11-crash-consistency-20260906` (single commit, unmerged)
- **Reviewer**: Claude Opus 5, independent read-only adversarial re-review
- **Implementer**: Claude Opus 5 (separate session) — reviewer did not become implementer
- **Session**: `verification-loop-predicate11-crash-consistency-rereview-20260906`
- **Date**: 2026-09-06
- **Mode**: read-only. No production code changed, nothing merged, nothing activated, Phase B-3 not started.

---

## Verdict

```
PREDICATE11_FIX_ACCEPTED
```

The `8ba1aaa` → `b38ceb2` crash-consistency repair may close.

**This is explicitly NOT `PHASE_B2_ACCEPTED`.**

- Phase B-2 acceptance remains **pending a focused B-2R independent re-review**.
- The full B-2R focused independent re-review is **still owed** and has no recorded verdict.
- **Phase B-3 must not start.**

Rationale: the repository's own lineage table at `b38ceb2` still reads
`B-2R repair | this branch | pending focused independent re-review`, and no B-2R
re-review verdict document exists. This round's mandate was scoped to the
`8ba1aaa` → `b38ceb2` delta only. All seven prior B-2 blockers were re-verified as
still closed by their own guards (section K below), which is strong supporting
evidence, but it is not a substitute for the recorded B-2R re-review.

---

## A. Preflight readback (measured, not quoted from the implementer)

| Item | Measured value |
|---|---|
| Governance SSOT | Google Drive `AI-DEVELOPMENT-RULES.md` **v0.5.0**, 2026-09-01, 47 rules, §10 Production Fix Protocol rules 46–47 (read from Drive, **not** the stale GitHub copy) |
| PROJECT-RULES — ADM | v1.1.0, 2026-08-21, Drive id `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ` |
| production-fix-protocol | skill present at `~/.claude/skills/production-fix-protocol/SKILL.md`; Phase A (read-only diagnosis) and Phase B (reproduce before repair) applied to this review |
| Phase A v3 predicate 11 | `PHASE-A-V3-ARCHITECTURE.md` line 366: "`report_digest` 與 ticket **消費時**記載者相等（append-only ledger，`ticket_seq` 單調、CAS）" |
| B-2 independent review | `PHASE-B2-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md`, blockers B-1 (evidence trusted on read) and B-2 (self-declared `base_result` outranks attestation), non-blocking N-1…N-5 |
| Delta topology | `8ba1aaa` is an ancestor of `b38ceb2`; exactly one commit between them |
| Branch | `origin/fix/verification-loop-predicate11-crash-consistency-20260906` |
| `origin/main` | `047b21899116350a867d5031acb2b128ab04d235` — **unmoved**; `b38ceb2` is **not** merged into main |
| Production checkout | `C:\Users\EE\Documents\ChatGPT\AI\ai-development-manager-home-recovery-combined-20260822`, branch `main` @ `047b218`, **0 dirty paths** |
| Production AI_MANAGER_HOME | `C:\Users\EE\.ai-development-manager` — no `verification/` store present before or after |
| TESTED / ACTIVATED / RUNNING | all three = `047b218` (`provenance/tested_sha.json`, `activated_sha.json`, `runtime_evidence.json`) |
| Runtime liveness | component heartbeats advanced 15:52Z → 16:17Z **during** the review; runtime live and untouched |

**Isolation strategy.** Two scratch clones at **87 characters each**, exactly equal to the
production checkout path length, to reproduce the repository's known path-length sensitivity:

```
C:\Users\EE\Documents\ChatGPT\AI\adm-p11-crash-rereview-20260906-basedir-scratchclone-a   (8ba1aaa)
C:\Users\EE\Documents\ChatGPT\AI\adm-p11-crash-rereview-20260906-headdir-scratchclone-b   (b38ceb2)
C:\Users\EE\Documents\ChatGPT\AI\adm-p11-crash-rereview-20260906-mutdir-scratchclone-c    (mutation runs, deleted after)
```

A fresh temporary `AI_MANAGER_HOME` was used for every regression run. Checkout is CRLF
(`core.autocrlf=true`), which is what makes the section M evidence meaningful.

**Harness independence.** All probes below were written by the reviewer against API present at
**both** commits (no use of the new `fixtures.consumed_against`), so the identical script runs at
base and head. The implementer's own scripts were run separately and are reported separately.

---

## B. Core finding — the crash-window reproducer, confirmed at base

`VerificationController.submit()` performs two filesystem writes: persist the report, then append
the ticket's consumption record. A crash between them leaves the report durable and the ticket
still `issued`. Base read predicate 11 conditionally — compare the recorded digest *only if* the
ticket says `consumed` — so that state passed straight through.

Reviewer-measured, identical script at both commits:

```
BASE 8ba1aaa   persisted_reports=4  ticket_status=['issued']
               derivation -> ACCEPTED / ACCEPTED
               invalidated_report_reasons = ()

HEAD b38ceb2   persisted_reports=4  ticket_status=['issued']
               derivation -> PENDING / CONTINUE_VERIFICATION
               invalidated_report_reasons = [('V0:r1:0','TICKET_NOT_CONSUMED'), ...]
```

**The reproducer is genuine.** The base state derives ACCEPTED with *no invalidation reasons at
all* — a false-complete on durable evidence no ticket ever authorised.

### The production change

Only `manager/verification_loop/admission.py` changes behaviour. Predicate 11 moves from a
conditional to a positive requirement:

```python
if ticket.status == "issued":
    return "TICKET_NOT_CONSUMED"
if ticket.status != "consumed":
    return "TICKET_NOT_OPEN"
if ticket.consumed_report_digest != report_digest(report):
    return "REPORT_DIGEST_NOT_TICKET_CONSUMED"
```

### Delta isolation — exactly one probe flips

Running the reviewer's full crash-consistency suite at both commits:

| Probe | base `8ba1aaa` | head `b38ceb2` |
|---|---|---|
| CRASH-1 report persisted, consumption absent | **BYPASSED (ACCEPTED)** | BLOCKED |
| CRASH-2 consumption persisted, report absent | BLOCKED | BLOCKED |
| CRASH-3 consumption names a nonexistent digest | BLOCKED | BLOCKED |
| CRASH-4 partial/malformed consumption record | BLOCKED | BLOCKED |
| CRASH-5 duplicate identical consumption | IDEMPOTENT | IDEMPOTENT |
| CRASH-6 duplicate conflicting consumption | BLOCKED | BLOCKED |
| CRASH-7 PASS consumed, FAIL appears later | BLOCKED | BLOCKED |
| CRASH-8 FAIL consumed, PASS appears later | BLOCKED | BLOCKED |
| CRASH-9 same ticket, two different report digests | BLOCKED | BLOCKED |
| F-ORDER order independence | ORDER-INDEPENDENT | ORDER-INDEPENDENT |
| G-REPLAY / G-CLOCK idempotency | IDEMPOTENT | IDEMPOTENT |

Everything except the crash window was already closed at base. The delta is tightly scoped.

---

## C. Predicate 11 as a positive requirement

Measured at `b38ceb2` (reviewer harness, store-backed via `VerificationController`):

| Probe | Expectation | Measured |
|---|---|---|
| P11-1 issued ticket + persisted report | rejected / pending | `PENDING / CONTINUE_VERIFICATION`, reasons `['TICKET_NOT_CONSUMED']` |
| P11-2 consumed ticket + matching digest | admitted | `ACCEPTED / ACCEPTED`, reasons `()` |
| P11-3 consumed ticket + wrong digest | refused for the digest reason specifically | `PENDING`, reasons `['REPORT_DIGEST_NOT_TICKET_CONSUMED']` |
| P11-4 missing consumption not covered by other PASS evidence (3 gates answered, 1 crashed) | not accepted | `IN_VERIFICATION / CONTINUE_VERIFICATION`, reasons `['TICKET_NOT_CONSUMED']` |
| P11-5 status manually flipped issued to consumed, no legitimate matching consumption evidence | fail closed | `PENDING`, reasons `['REPORT_DIGEST_NOT_TICKET_CONSUMED']` |
| P11-6 `consumed_report_digest` tampered in place on disk | fail closed | `EvidenceIntegrityError: TICKET_CONTENT_DIGEST_MISMATCH` (4 records tampered) |

At base, P11-1, P11-4 and the recovery probe all FAILED (returned ACCEPTED); P11-2, P11-3, P11-5
and P11-6 already passed. The delta closes exactly the crash window and nothing else regresses.

---

## D. Crash recovery semantics: recoverable, not terminal

The design intent is that the crash window is a recoverable incomplete admission, not terminal
corruption. Measured at `b38ceb2`:

```
stage 1  reports persisted, consumption never appended
         -> PENDING / CONTINUE_VERIFICATION   (TICKET_NOT_CONSUMED)

stage 2  append ONLY the missing consumption record
         -> ACCEPTED / ACCEPTED               (invalidated_report_reasons = ())
```

Recovery requirements, all verified:

- **no report rewrite** - `reports_unchanged=True`; the set of stored report digests before and
  after recovery is byte-identical
- **no new ticket** - `distinct_tickets=4`, unchanged
- **no candidate SHA change** - same `candidate_sha`
- **no human override** - no gate, no escalation; `next_action` goes straight to `ACCEPTED`
- **no duplicate evidence ambiguity** - one issue record plus one consumption record per ticket
  (8 ticket records for 4 gates)

---

## E. Crash / partial-write attacks

All nine required attacks measured at `b38ceb2`:

| ID | Attack | Result | Guard |
|---|---|---|---|
| CRASH-1 | report persisted, consumption absent | BLOCKED | `TICKET_NOT_CONSUMED` |
| CRASH-2 | consumption persisted, report absent | BLOCKED | `stored_reports=0`, gate unsatisfied |
| CRASH-3 | consumption points at a nonexistent report digest | BLOCKED | `REPORT_DIGEST_NOT_TICKET_CONSUMED` |
| CRASH-4 | partial / malformed consumption record | BLOCKED | `EvidenceIntegrityError: TICKET_MALFORMED_JSON` |
| CRASH-5 | duplicate identical consumption | IDEMPOTENT | 8 ticket records (expected 8), still `ACCEPTED` |
| CRASH-6 | duplicate conflicting consumption | BLOCKED | `TICKET_NOT_OPEN` (ticket folds to `invalidated`) |
| CRASH-7 | PASS consumed, then a FAIL report appears | BLOCKED | `DUPLICATE_REPORT_FOR_TICKET` |
| CRASH-8 | FAIL consumed, then a PASS report appears | BLOCKED | `DUPLICATE_REPORT_FOR_TICKET` |
| CRASH-9 | same ticket, two different report digests | BLOCKED | `DUPLICATE_REPORT_FOR_TICKET` |

No false ACCEPTED arises from append order.

---

## F. Order independence

Two reports contesting one ticket, submitted in both orders through `submit()`:

```
Sequence A  PASS first, FAIL second -> IN_VERIFICATION / CONTINUE_VERIFICATION
                                       reasons ['DUPLICATE_REPORT_FOR_TICKET']
Sequence B  FAIL first, PASS second -> IN_VERIFICATION / CONTINUE_VERIFICATION
                                       reasons ['DUPLICATE_REPORT_FOR_TICKET']
```

The comparison used a full fingerprint - `(acceptance_state, next_action, reasons,
satisfied_gates, missing_required_gates, derivation_key)` - and the two sequences are
**identical including `derivation_key`**. The architecture's stated rule ("two claims on one
ticket leave it invalidated for everyone") holds, and the outcome does not depend on who wrote
first.

---

## G. Recovery idempotency

| Probe | Measured |
|---|---|
| Same consumption replayed 1x, 2x, 3x, 4x, 5x | every replay yields the identical tuple `('ACCEPTED', derivation_key=9db6021556ae, ticket_records=8)`; `len(set(observations)) == 1` |
| Same digest replayed under three different wall clocks (`00:00:05Z`, `09:59:59Z`, `2027-01-01T00:00:00Z`) | 8 ticket records, `ACCEPTED` - a differing timestamp does not create a conflicting claim |

No duplicate-create producing a wrong ACCEPTED, no second replay invalidating a legitimate
ticket, no silent overwrite, no sequence-dependent result. `FileTicketStore.consume()` returns the
existing record when the digest matches, so the append is a genuine no-op.

---

## H. Independent guard-removal mutants (reviewer's own, not the implementer's harness)

### The implementer's M54 is weaker than the commit message implies

`M54` replaces `if ticket.status == "issued":` with `if False and ticket.status == "issued":`.
Measured by the reviewer:

```
RR-P11-1a (= M54)   attack CRASH-1 -> PENDING / CONTINUE_VERIFICATION  reasons ['TICKET_NOT_OPEN']
                    test_csm_11 -> KILLED
                    restore byte-exact: True
```

The test dies, but **the attack still blocks** - control falls through to
`if ticket.status != "consumed": return "TICKET_NOT_OPEN"`. So M54 proves the reason string is
produced at that line; it does **not** demonstrate that removing the guard reopens the crash
window.

### The decisive mutant

`RR-P11-1b` is a faithful three-edit revert of the production hunk back to `8ba1aaa` semantics:

```
    if ticket.status == "issued":            ->  if False:
    if ticket.status != "consumed":          ->  if ticket.status not in ("consumed", "issued"):
    if ticket.consumed_report_digest != ...  ->  if ticket.status == "consumed" and ticket.consumed_report_digest != ...
```

```
RR-P11-1b   edits=3 crlf=True
            attack CRASH-1 -> ACCEPTED / ACCEPTED  reasons=[]
            test_csm_11 -> KILLED (1 failed, 53 deselected)
            restore byte-exact: True

RR-P11-2    edits=1 crlf=True   (digest comparison neutralised)
            attack wrong-digest -> ACCEPTED / ACCEPTED
            test_csm_14 -> KILLED (1 failed, 53 deselected)
            restore byte-exact: True
```

**Conclusion.** The base attack re-reaches `ACCEPTED` when the guard is genuinely removed, and the
named tests die with it. Both halves of predicate 11 - the status requirement and the digest
comparison - are independently load-bearing. The tests are **non-vacuous**. `git status` was 0
before and after every mutant.

---

## I. Audit vacuity re-check (A8 / A9 / A11)

The implementer disclosed that the new guard had masked three attacks in the committed adversarial
audit. Verified independently by dumping the **per-report admission reason**, not merely observing
"blocked".

Bound (as committed at `b38ceb2`), tickets all in legitimate `consumed` state:

| Attack | Per-report admission | Outcome | Target guard reached? |
|---|---|---|---|
| A8 base FAIL claimed against a PASS attestation | V0/V1/V2/V3 all `ADMISSIBLE` | `OPEN_BUNDLE_REVISION`, `base=UNKNOWN BASE_EVIDENCE_CONTRADICTION` | **yes** - all reports admitted, so the block came from classification, its intended target |
| A9 Task A evidence spent on Task B | all four `REPORT_TASK_ID_MISMATCH` | `PENDING` | **yes** - the cross-Task barrier itself (B-2 finding N-1) |
| A11 PASS report carrying failure observations | V0-V2 `ADMISSIBLE`, V3 `REPORT_SELF_CONTRADICTION` | `IN_VERIFICATION` | **yes** - only the offending report is rejected, by its own guard |

The masking was reproduced to confirm the disclosure was accurate:

```
A8-unbound    ['TICKET_NOT_CONSUMED'] -> CONTINUE_VERIFICATION   MASKED
A9-unbound    ['TICKET_NOT_CONSUMED'] -> PENDING                 MASKED
A11-unbound   ['TICKET_NOT_CONSUMED'] -> PENDING                 MASKED
```

Committed adversarial audit run at `b38ceb2`: **11/11 attacks blocked, 2/2 controls still healthy**
(C1 honest round to ACCEPTED, C2 honest failure to REPAIR). A8 is back at `OPEN_BUNDLE_REVISION`
rather than the `CONTINUE_VERIFICATION` it had silently drifted to.

---

## J. Helper safety: `fixtures.consumed_against` does not launder forged evidence

Ten probes. For each, the reviewer asserted **both** that the outcome fails closed **and** that the
forged field survives the helper byte-for-byte.

| Probe | Forged field preserved? | Guard that caught it |
|---|---|---|
| J-CONTROL nothing forged | - | `ACCEPTED` (helper does not seal the loop shut) |
| J1 forged `task_id` = `TASK-SOMEONE-ELSE` | preserved | `TICKET_TASK_MISMATCH` |
| J2 forged checker identity = `attacker/acct-evil/sess-evil` | preserved | `CHECKER_NOT_TICKET_EXPECTED_IDENTITY` |
| J3 forged `candidate_sha` | preserved | `NO_MATCHING_TICKET` |
| J4 forged `bundle_hash` | preserved | `NO_MATCHING_TICKET` |
| J5 forged `ticket_id` | preserved | `NO_MATCHING_TICKET` |
| J6 forged `base_sha` (not in the ticket-id hash, so binding still succeeds) | preserved | `TICKET_BASE_SHA_MISMATCH` |
| J7 ticket already consumed with a deliberately wrong digest | preserved (`ffff...`) | `REPORT_DIGEST_NOT_TICKET_CONSUMED` - helper leaves it alone |
| J8 no tickets at all | helper returns `()` | `NO_MATCHING_TICKET` - invents nothing |
| J9 forged **report** field (`base_sha`) | - | `TICKET_BASE_SHA_MISMATCH` |
| J10 field-level diff over every ticket field | changed = `['consumed_at','consumed_report_digest','status','ticket_seq']` = exactly the allowed set | - |

The helper only binds a legitimate ticket to the report digest that claims it, by `ticket_id`.
It does not repair attacker inputs. Single-factor discipline is preserved: the forged field
remains the only difference between a test and its control.

---

## K. Prior B-2 blockers: 7/7 still closed, each by its own guard

The concern was that the new invariant might short-circuit these attacks at
`TICKET_NOT_CONSUMED`, leaving the original guards unverified. Every probe below therefore
establishes a **legitimately consumed ticket first** (via `controller.submit()`), then applies its
attack. None was blocked by `TICKET_NOT_CONSUMED`.

| # | Prior blocker | Origin | Measured guard at `b38ceb2` |
|---|---|---|---|
| K1 | stored FAIL report tampered into PASS | B-2 blocker B-1a | `EvidenceIntegrityError: REPORT_CONTENT_DIGEST_MISMATCH` |
| K2 | stored ticket checker identity tampered | B-2 blocker B-1b | `EvidenceIntegrityError: TICKET_CONTENT_DIGEST_MISMATCH` |
| K3 | baseline attestation says PASS, report claims base also FAILED | B-2 blocker B-2 | `OPEN_BUNDLE_REVISION`, `base=UNKNOWN`, reasons `[]` |
| K4 | cross-Task evidence reuse | B-2 non-blocking N-1 | `PENDING`, `['REPORT_TASK_ID_MISMATCH']` |
| K5 | worktree lease **generation** change | B-2 non-blocking N-2 | `ROUND_INVALIDATED`, `['WORKTREE_GENERATION_CHANGED_DURING_ROUND']` |
| K6 | worktree lease **lock id** change | B-2 non-blocking N-2 | `ROUND_INVALIDATED`, `['WORKTREE_LOCK_ID_CHANGED_DURING_ROUND']` |
| K7 | PASS report carrying failure observations | B-2 non-blocking N-3 | `IN_VERIFICATION`, `['REPORT_SELF_CONTRADICTION']` |

**7/7 closed by their own guards.** The new invariant does not hide any of them.

---

## L. Regression (self-measured, not taken from the implementer)

Method: fresh temporary `AI_MANAGER_HOME` per run; both clones at equal 87-character path length;
`python -m pytest -q --no-header -p no:randomly` from the repo root.

| Run | failed | passed | subtests | wall time |
|---|---|---|---|---|
| base `8ba1aaa` | 2 | 3085 | 446 | 440.01s |
| head `b38ceb2` run 1 | 1 | 3091 | 446 | 439.75s |
| head `b38ceb2` run 2 | **2** | **3090** | 446 | 428.32s |

Collected: base 3087, head 3092. Delta = **+5 tests**, all passing:
`test_csm_11_a_persisted_report_whose_ticket_was_never_consumed_is_refused`,
`test_csm_12_an_interrupted_round_recovers_when_consumption_is_appended`,
`test_csm_13_a_partial_crash_refuses_only_the_unconsumed_gate`,
`test_csm_14_the_crash_guard_does_not_replace_the_digest_comparison`,
`test_csm_15_a_correctly_consumed_round_still_accepts`.

Head run 2 reproduces the implementer's claim (3090 passed / 2 failed) exactly.

**The run-1 discrepancy was investigated rather than reported as a delta.** The differing test is
`manager/test_github_dispatch_watcher.py::MainCliTests::test_main_missing_token_env_returns_nonzero`.
It is **non-hermetic**: it performs live GitHub ingress, and the `file_id` set it returns differs
between invocations. Run in isolation it **fails at both commits**:

```
BASE 8ba1aaa   FAILED ...test_main_missing_token_env_returns_nonzero    1 failed in 28.09s
HEAD b38ceb2   FAILED ...test_main_missing_token_env_returns_nonzero    1 failed in 25.00s
```

Failure set is therefore the same at base and head:
`test_command_watcher_ag.py::TestProcessCommandAgRouting::test_gate_pass_reaches_launch_task` and
the non-hermetic GitHub test. **No new delta failure.**

---

## M. CRLF / mutation harness restoration

Checkout is CRLF (`core.autocrlf=true`), verified: `admission.py` 217 CRLF / 0 bare LF.

**Reviewer protocol applied**: hash source bytes before, apply mutant preserving CRLF, run the
targeted test, restore from the original bytes, hash after.

Full 14-mutant matrix run at `b38ceb2` (with the ROOT defect below corrected):

```
M41 M42 M43 M44 M45 M46 M47 M48 M49 M50 M51 M52 M54 M53 -- all KILLED, target HIT
14/14 KILLED by their named target test; 0 survived, 0 not applied, 0 killed for the wrong reason
```

Byte verification across the whole run:

| File | sha256 (first 16) | CRLF | bare LF | after run |
|---|---|---|---|---|
| `manager/verification_loop/admission.py` | `9d8ac1383a27c176` | 217 | 0 | BYTE-IDENTICAL |
| `manager/verification_loop/stores.py` | `1b139cf03cb1c889` | 392 | 0 | BYTE-IDENTICAL |
| `manager/verification_loop/tickets.py` | `48edb02688602242` | 122 | 0 | BYTE-IDENTICAL |
| `manager/verification_loop/evaluator.py` | `2aa4acb2f550c390` | 444 | 0 | BYTE-IDENTICAL |
| `manager/verification_loop/fixtures.py` | `f6bf2c9137114273` | 467 | 0 | BYTE-IDENTICAL |
| `manager/verification_loop/controller.py` | `fd626c5911fc3b2f` | 168 | 0 | BYTE-IDENTICAL |

`git status` 0, `git diff --stat` empty after the run.

**The harness defect at base was real and the fix is load-bearing.** Running the `8ba1aaa` version
of the matrix on the same CRLF checkout aborts on its very first mutant and corrupts the tree:

```
AssertionError: M41: manager/verification_loop/stores.py not restored byte-for-byte
git status: 1   ->   M manager/verification_loop/stores.py
stores.py after: crlf=0 bare_lf=392    (was crlf=392 bare_lf=0)
```

---

## N. Windows path length

Production checkout path length is 87; production home is
`C:\Users\EE\.ai-development-manager` (31). Full write-read-recovery cycle
(issue round, crash window, derive, append consumption, derive again) at production-equal and
deeper roots:

| Case | store root length | longest record path | crash derive | recovery derive |
|---|---|---|---|---|
| production-equal | 87 | 178 | PENDING | ACCEPTED |
| deeper +40 | 127 | 218 | PENDING | ACCEPTED |
| deeper +80 | 167 | **258** | PENDING | ACCEPTED |

258 characters is just under the Windows `MAX_PATH` of 260 and still succeeds. The B-2R
long-path repair (flat record layout rather than a directory per ticket) is not regressed.
No production path was touched.

---

## O. Architecture boundary

Production modules changed by the delta: **`admission.py` and `fixtures.py` only.**

| Module | Changed by delta? |
|---|---|
| `models.py` | UNCHANGED |
| `stores.py` | UNCHANGED |
| `controller.py` | UNCHANGED |
| `evaluator.py` | UNCHANGED |
| `tickets.py` | UNCHANGED |
| `bundle.py` | UNCHANGED |
| `classification.py` | UNCHANGED |
| `risk.py` | UNCHANGED |

Confirmed absent from the delta:

- no writable `ACCEPTED` - `acceptance_state` exists only on the computed `EvaluationResult`;
  no store writes it
- no second acceptance registry
- no second Task truth, Execution truth or Session truth
- no `LoopRun`
- no new class, store, file write, `os.open`, or `mkdir` introduced anywhere in the delta

Predicate 11 consumption remains an admission/evidence primitive. **No architecture drift.**

---

## P. Reviewer-designed attacks (not on the implementer's list)

Eight attacks designed independently; seven fail closed.

| ID | Attack | Result | Guard |
|---|---|---|---|
| RP-1 | cross-gate consumption: V3's ticket consumed by V0's report digest | BLOCKED | `REPORT_DIGEST_NOT_TICKET_CONSUMED` |
| RP-2 | consumption record with **no issue record** (attacker deleted it) | BLOCKED | `EvidenceIntegrityError: TICKET_LEDGER_INCOHERENT` |
| RP-3 | double crash: two reports for one gate persisted, attacker consumes only its preferred one | BLOCKED | `DUPLICATE_REPORT_FOR_TICKET` |
| RP-4 | forged consumption filed at `ticket_seq 0` (a second "issue") | BLOCKED | `EvidenceIntegrityError: TICKET_LEDGER_INCOHERENT` |
| RP-5 | half-written record: status still `issued` but a digest already set | BLOCKED | `NO_MATCHING_TICKET` (self-consistency rejects it) |
| RP-6 | consumed with a blank digest (torn write of the digest field) | BLOCKED | `NO_MATCHING_TICKET` |
| RP-7 | recovery replay binding a **self-verified** report (producer == executor) | BLOCKED | `CHECKER_NOT_TICKET_EXPECTED_IDENTITY` |
| RP-8 | crash window on a gate the current risk tier does **not** require | **REACHED ACCEPTED** | see below - non-blocking |

### RP-8 (non-blocking finding, pre-existing)

At a low risk tier where only `V0` is required, `V0` legitimately consumed and `V3` left in the
crash window:

```
HEAD b38ceb2   V3 PASS in crash window -> ACCEPTED   required=('V0',) reasons=['TICKET_NOT_CONSUMED']
HEAD b38ceb2   V3 FAIL in crash window -> ACCEPTED   required=('V0',) reasons=['TICKET_NOT_CONSUMED']
BASE 8ba1aaa   V3 FAIL in crash window -> REJECTED_NEEDS_REPAIR
```

This looks like a behaviour regression, so it was chased down rather than reported raw. Dropping
an inadmissible report on a non-required gate turns out to be a **pre-existing general
architectural property, identical at both commits, for every inadmissibility reason**:

```
BASE and HEAD, identical:
  V3 FAIL, forged checker_id       -> ACCEPTED  reasons=['CHECKER_IDENTITY_MISMATCH']
  V3 FAIL, deliverable_sha wrong   -> ACCEPTED  reasons=['DELIVERABLE_SHA_MISMATCH']
  V3 FAIL, untrusted evidence src  -> ACCEPTED  reasons=['UNTRUSTED_EVIDENCE_SOURCE']
  V3 FAIL, clean (control)         -> REJECTED_NEEDS_REPAIR
```

This delta merely adds one more reason (`TICKET_NOT_CONSUMED`) to a bucket that already behaved
this way. It is consistent with the module's stated rule that an inadmissible report "does not
enter derivation at all; it does not exist", and with the deliberate refusal to let unauthorised
evidence *suppress* an acceptance. **Crucially, the acceptance never rests on the unconsumed
report** - required gates can only be satisfied by admissible reports, and unconsumed reports are
never admissible.

**Not a blocker for this delta.** Recommended for a human decision in Phase B-3: whether
`acceptance_state == ACCEPTED` should be permitted to coexist with a non-empty
`invalidated_report_reasons`.

### Trust-boundary note (pre-existing, unchanged)

An attacker with filesystem write access who can also impersonate the ticket's expected checker
identity can fabricate an acceptance. Measured **identical at both commits**:

```
BASE and HEAD:  fabricated well-formed PASS + appended consumption   -> ACCEPTED
BASE and HEAD:  fabricated PASS from an unexpected checker           -> PENDING
                                                  ['CHECKER_NOT_TICKET_EXPECTED_IDENTITY']
```

The ledger is append-only and content-addressed but **not signed**; that is the documented
pre-existing trust boundary. Identity binding still holds. This delta neither widens nor narrows
it. Recorded for honesty, not as a finding against this change.

---

## Q. PASS criteria

| # | Criterion | Result |
|---|---|---|
| 1 | base `8ba1aaa` original crash-window reproducer holds | PASS |
| 2 | HEAD `b38ceb2` no longer ACCEPTED | PASS |
| 3 | issued ticket cannot admit a report | PASS |
| 4 | consumed + matching digest admits normally | PASS |
| 5 | wrong digest fails closed | PASS |
| 6 | recovery append then ACCEPTED | PASS |
| 7 | recovery deterministic / idempotent | PASS |
| 8 | conflicting consumption order-independent | PASS |
| 9 | guard-removal mutant non-vacuously killed | PASS |
| 10 | A8 / A9 / A11 no longer masked by the new guard | PASS |
| 11 | B2R prior blockers still closed by their own guards | PASS |
| 12 | `consumed_against` does not launder forged evidence | PASS |
| 13 | CRLF restore byte-exact | PASS |
| 14 | fresh regression, no new delta failure | PASS |
| 15 | production untouched | PASS |
| 16 | no architecture drift | PASS |

**16/16.**

---

## Non-blocking findings

These do **not** block the Predicate 11 fix. The reviewer deliberately did not repair any of them
(AI-DEVELOPMENT-RULES rule 32: the reviewer must not silently become the implementer).

### NB-1 - the mutation matrix cannot run as committed (pre-existing)

`docs/verification-loop/repro/B2R-MUTATION-MATRIX.py` line 24:

```python
ROOT = pathlib.Path(__file__).resolve().parent
```

resolves to `docs/verification-loop/repro/`, so `ROOT / "manager/verification_loop/stores.py"`
does not exist and `cwd=ROOT` finds no test suites. Running it as committed produces:

```
Baseline: the unmutated tree must be green, or nothing below means anything.
BASELINE NOT GREEN -- aborting
no tests ran in 0.02s
```

**Pre-existing**: the identical line is present at `8ba1aaa` and this delta does not touch it.
Consequence: the headline "14/14 mutants killed" is **not reproducible from the committed script
without patching ROOT**. It fails closed (aborts rather than reporting a false result), and the
reviewer confirmed 14/14 after correcting ROOT. Suggested fix: `ROOT = pathlib.Path(__file__).resolve().parents[3]`.

### NB-2 - README stale mutant count (introduced by this delta)

`docs/verification-loop/README.md` still reads "thirteen mutants" and "**13/13 killed by their
named target**", but this delta added `M54`, bringing the matrix to 14. The README paragraph was
not updated alongside it.

### NB-3 - M54 overstates what it proves

See section H. `M54` kills `test_csm_11` but leaves the attack blocked at `TICKET_NOT_OPEN`.
Consider replacing or supplementing it with a full-hunk revert mutant (`RR-P11-1b`), which is the
one that actually demonstrates the crash window reopening.

### NB-4 - ACCEPTED coexisting with `invalidated_report_reasons`

See section P / RP-8. Pre-existing architectural property; worth a human decision in Phase B-3.

### NB-5 - Phase A v3 wording tension

`PHASE-A-V3-ARCHITECTURE.md` predicate 1 literally states
`∃ ticket : ticket.ticket_id == report.ticket_id ∧ ticket.status == issued`, while predicate 11
requires the digest recorded **at consumption**. A ticket cannot simultaneously be `issued` and
carry a consumption digest, so the two cannot both hold literally. The implementation's joint
reading - the ticket was issued by the controller and has since been consumed recording this
report's digest - is the only self-consistent one, and is strictly the safer of the two. The
architecture text deserves a clarifying edit so a future implementer does not re-derive the base
behaviour from predicate 1 alone.

### NB-6 - non-hermetic regression test

`manager/test_github_dispatch_watcher.py::MainCliTests::test_main_missing_token_env_returns_nonzero`
performs live GitHub ingress and flaps between runs. It must never be counted as a delta
regression without isolating it at both commits first.

---

## Governance compliance

| Rule | Applied |
|---|---|
| AI-DEVELOPMENT-RULES 17 | read-only diagnosis; HEAD/base/branch/dirty state confirmed before anything else |
| AI-DEVELOPMENT-RULES 24 | defect reproduced at base `8ba1aaa`, fix proved at `b38ceb2`, same script |
| AI-DEVELOPMENT-RULES 25 | this document is the durable record (Drive + GitHub) |
| AI-DEVELOPMENT-RULES 32 | independent read-only adversarial review; reviewer did not become implementer - no non-blocking finding was repaired |
| AI-DEVELOPMENT-RULES 46 | non-vacuous test proof: pre-fix behavioural runs plus independent mutation controls |
| PROJECT-RULES ADM 12 | every run bound to an isolated scratch clone, never the production checkout |
| PROJECT-RULES ADM 15 | diagnosis stayed read-only; no production activation or deployment |
| PROJECT-RULES ADM 23 | old defect proved, then fixed behaviour proved at HEAD |
| PROJECT-RULES ADM 31 | no production evidence or history rewritten |

## Production untouched (verified before and after)

```
production checkout : C:\Users\EE\Documents\ChatGPT\AI\ai-development-manager-home-recovery-combined-20260822
  HEAD              : 047b21899116350a867d5031acb2b128ab04d235
  branch            : main
  dirty paths       : 0
  main              : 047b21899116350a867d5031acb2b128ab04d235
origin/main         : 047b21899116350a867d5031acb2b128ab04d235   (unmoved; b38ceb2 NOT merged)
production home     : C:\Users\EE\.ai-development-manager
  verification store: not present (never created)
  provenance        : tested_sha = activated_sha = 047b218, unchanged
runtime heartbeats  : advanced 15:52Z -> 16:17Z during the review (live, untouched)
```

Nothing was merged. Nothing was activated. No production code was modified. Phase B-3 was not
started.

---

## Closing

```
PREDICATE11_FIX_ACCEPTED
```

The `8ba1aaa` to `b38ceb2` crash-consistency repair may close.

**Phase B-2 acceptance remains pending a focused B-2R independent re-review.**
**Phase B-3 must not start.**
