# ADM EVIDENCE — Verification Loop Phase B3-1 Predicate 10 / NB-A Closure — 2026-09-07

**Status: `B3-1 IMPLEMENTATION COMPLETE — INDEPENDENT REVIEW PENDING`**

Not merged. Not activated. Not Phase B-3 complete. Not production ready. The
next step is a read-only adversarial review by a different AI session; this
implementer session does not review its own work.

## Provenance / lineage

| Field | Value |
|---|---|
| Project | AI 一体化 / ai-development-manager |
| Repository | `ne9221/ai-development-manager` (GitHub, formal source SSOT) |
| Task | Verification Loop Phase B3-1 — Predicate 10 / NB-A Closure |
| Session | `adm-verification-loop-b3-1-predicate10-20260907` |
| Implementer | Claude Fable 5.1 (model `claude-fable-5-1`, reasoning high) |
| Governance | AI-DEVELOPMENT-RULES **v0.5.0** (2026-09-01, 47 rules incl. §10 rules 46–47), Drive `1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU` |
| Project rules | PROJECT-RULES — ADM **v1.1.0** (2026-08-21), Drive `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ` |
| Reviewed input | ADM REVIEW — Phase B-2 Final Independent Re-review (PHASE_B2_ACCEPTED), Drive `1ddfjjme7a9zL429S0ga_EFlar-ajAOQXjlBgCDydK6U`; ADM DESIGN — Phase A v3, Drive `1WyCghNxlB-vdu3qGvge01kcNu04Mz7Z7EZNKbMATmBA` |
| Protocol | `production-fix-protocol` skill (`~/.claude/skills/production-fix-protocol/SKILL.md`), Phases A–F applied; G–J are out of scope for an unmerged slice |
| Accepted B-2 head | `b38ceb26e29e46e83829cb1569de76525b7531e9` |
| Branch base | `668b7feb319414f93c4e2907a4aca1aa524d2ef7` (docs-only descendant of `b38ceb2` carrying the B-2 acceptance record; verified linear, 2 commits, 2 files, no code) |
| Branch | `fix/verification-loop-b3-1-nba-consumption-binding-20260907` |
| Implementation commit | recorded in the cross-link section below, added by the follow-up commit |
| Drive evidence doc | recorded in the cross-link section below |
| Production `origin/main` | `047b21899116350a867d5031acb2b128ab04d235` — unmoved |

## Scope

Exactly one residual: **NB-A** from the B-2 final review. In scope: the
immutable binding of a ticket's consumption record to its issue record, its
tests, its reproducer, its mutation matrix, this record and the README rows
that point at them. Deliberately **not** touched: NB-B (audit not runnable at
base), NB-C (B2R matrix ROOT), NB-D (README 13/14), NB-E..NB-I, Dashboard,
rule 44, production activation, any refactor.

## Phase A — read-only diagnosis

**Expected truth.** A verification ticket is issued by the controller before a
checker runs and freezes who may answer it (`expected_checker_identity`), who
may not (`forbidden_identity`), which lease and head it was issued under, and
the round tuple. A consumption record may only say *that* the ticket was
answered and *by which report digest* (Phase A v3 predicate 11).

**Observed truth at `b38ceb2`.** `FileTicketStore._fold_ticket` returned the
consumption record **wholesale** as the ticket's current state and never
compared it with the issue record it claims to answer. `ticket_id` hashes only
(execution, candidate, bundle, gate, round), so every other frozen field sits
outside it. An attacker with store-write authority can append a new, correctly
named consumption record naming themselves as the expected checker. No
existing file is edited, so the B-2R digest-on-read guard cannot fire by
construction. The forged PASS then satisfies `CHECKER_NOT_TICKET_EXPECTED_IDENTITY`
against the attacker's own identity and derives ACCEPTED.

**Invariant violated.** The issue record is the authority on what a ticket
authorised; consumption is a state transition of that ticket, not a
restatement of it.

**Smallest failing path.** `issue_round` → honest V0..V2 → attacker
`reports.put(PASS from rogue)` + append `replace(issue, status=consumed,
consumed_report_digest=rogue, expected_checker_identity=rogue, ticket_seq=1)`
→ `derive()` → ACCEPTED.

## Phase B — reproduction before repair

`docs/verification-loop/repro/B31-NBA-FORGED-CONSUMPTION.py`, run unmodified
at both commits (sha256 of the script identical for both runs:
`2f1d79a95654e591da01e9ccb733350c303c08624eba8a2f6fe58042606b0915`), fresh
temporary `AI_MANAGER_HOME` per clone, both clones at equal 135-character
paths.

| Probe | base `b38ceb2` | B3-1 head |
|---|---|---|
| A1 forged consumption names rogue as expected checker; rogue files PASS | **ACCEPTED** | REFUSED `TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE` |
| A2.forbidden_identity erased | **ACCEPTED** | REFUSED (same guard) |
| A2.issued_by rewritten | **ACCEPTED** | REFUSED |
| A2.issued_at rewritten | **ACCEPTED** | REFUSED |
| A2.candidate_head_at_issue | ROUND_INVALIDATED (sibling guard) | REFUSED |
| A2.worktree_lock_id | ROUND_INVALIDATED (sibling guard) | REFUSED |
| A2.worktree_generation | ROUND_INVALIDATED (sibling guard) | REFUSED |
| A2.task_id | TICKET_TASK_MISMATCH (sibling guard) | REFUSED |
| A2.base_sha | TICKET_BASE_SHA_MISMATCH (sibling guard) | REFUSED |
| A2.candidate_sha / execution_id / bundle_hash / gate_id / round | NO_MATCHING_TICKET (sibling guard) | REFUSED |
| A3 V2's honest consumption transplanted onto V3's ticket id | NO_MATCHING_TICKET (sibling guard) | REFUSED, names `gate_id` |
| A4 forged consumption + forged PASS + forbidden identity erased | **ACCEPTED** | REFUSED |
| C1 honest round | ACCEPTED | ACCEPTED |
| C2 honest FAIL | REJECTED_NEEDS_REPAIR / REPAIR | identical |
| C3a crash window (report persisted, no consumption) | IN_VERIFICATION / CONTINUE_VERIFICATION, TICKET_NOT_CONSUMED | identical |
| C3b append only the missing consumption | ACCEPTED | identical |
| C3c identical consumption replayed | idempotent (record set unchanged), ACCEPTED | identical |
| C4 conflicting consumption, both orders | invalidated, identical fingerprint both orders | identical |

Base: `attacks_refused_by_target_guard=0/16`, `false_accepts=[A1, A2.forbidden_identity, A2.issued_at, A2.issued_by, A4]`, controls 6/6, exit 1.
Head: `attacks_refused_by_target_guard=16/16`, `false_accepts=[]`, controls 6/6, exit 0.

The script separates "false-accept" from "blocked by a sibling guard only" so
the base measurement is honest about which guard did the work: at base the
lease, task, base-SHA and round-tuple rewrites were already stopped by
admission comparing those fields against Execution and preflight facts. The
five that were not stopped are exactly the fields nothing else compares.

## Phase C — minimal fix

Two production files, one new pure function, one new refusal.

- `manager/verification_loop/tickets.py`
  - `CONSUMPTION_MUTABLE_FIELDS = {status, consumed_report_digest, consumed_at, ticket_seq}` — the only fields a consumption record may differ on. Enumerated by exclusion so a field added to `VerificationTicket` later is bound the moment it exists.
  - `issue_frozen_fields()` — every other `VerificationTicket` field.
  - `consumption_divergence_field(issued, consumption)` — the first frozen field the consumption rewrites, or None. Pure, so it can be tested and mutated on its own.
- `manager/verification_loop/stores.py`
  - `_fold_ticket` checks every consumption record against the single issue record **before** the conflict fold, and raises `EvidenceIntegrityError("TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE: … rewrites '<field>' …")`.

Why raise rather than fold identity from the issue record: the store's own
`consume()` builds a consumption record by `replace()` on the issue record, so
an honest consumption **never** differs on a frozen field. Any difference is a
record this store did not write. That is the same class of event as a record
that does not hash to its name (`TICKET_CONTENT_DIGEST_MISMATCH`) or a ticket
with two issue records (`TICKET_LEDGER_INCOHERENT`), and it gets the same
treatment: the ledger is refused, not derived around. Folding the identity
from the issue record would silently accept the forged file as harmless
noise, and a ledger that tolerates forged records is a ledger nobody can
audit. This is also why tampering outranks the conflict fold (NBA-5): an
honest consumption beside a forged one is not a contest between claimants.

No new persistence, no new record kind, no new entity, no change to
`ticket_id`, to `consume()`, or to any admission reason. The B-2 review's
architecture-boundary statement still holds: the package's only write
primitive is the single `O_CREAT | O_EXCL` open.

### Predicate 10 judgment

The task asked for the **minimal necessary** part of Phase A v3 predicate 10
(`replay_spec` / `expected_evidence_digest`) required to close NB-A. My
judgment, recorded for the reviewer to challenge:

- **NB-A is closed by the ticket-ledger binding alone.** The vector is a
  forged *ticket* record, not forged *evidence*; replay is orthogonal to it.
- **Any predicate-10 code without a real replayer is vacuous.** A report's
  `expected_evidence_digest` compared against its own `evidence_digest` is a
  self-declared field checked against a self-declared field — precisely the
  shape B-1R deleted `is_regression` for. The only non-vacuous half of
  predicate 10 is *re-executing the checker at `candidate_sha`*, which is an
  I/O component that does not exist in this package (every checker is still
  fixture data), would be a new module rather than a minimal change, and
  would touch every report fixture and test in the package.
- **The residual it would close is T7/P1b, not NB-A**: an attacker who forges
  a correctly named PASS *and* a consumption record that agrees with the issue
  record on every frozen field, claiming the honest checker's identity. That
  remains the pre-existing unsigned-ledger boundary at both base and head,
  exactly as the B-2 review classified it. It is **not** closed by this slice
  and this record does not claim it is.

Predicate 10 therefore remains deferred as its own slice (NB-F), with the
replayer as its first deliverable. Nothing in this slice pre-empts that design.

## Phase D — regression, three layers

1. **Targeted**: `manager/test_verification_loop_b3_1_consumption_binding.py` — 17 tests, 29 subtests, all passing at head (4.6 s). At base the module cannot import (`CONSUMPTION_MUTABLE_FIELDS` does not exist); the cross-version behavioural proof is the reproducer above.
2. **Focused subsystem**: the five existing verification-loop suites at head — **249 passed, 12 subtests passed**, 0 failed.
3. **Full project regression**, fresh temporary `AI_MANAGER_HOME` per run, both clones at 135-character paths:

| Run | failed | passed | subtests | wall |
|---|---|---|---|---|
| base `b38ceb2` | 2 | 3090 | 446 | 418.00 s |
| head (this slice) | 3 | 3106 | 475 | 408.19 s |

Base failure set: `manager/test_command_watcher_ag.py::TestProcessCommandAgRouting::test_gate_pass_reaches_launch_task` and `manager/test_github_dispatch_watcher.py::MainCliTests::test_main_missing_token_env_returns_nonzero` — the two pre-existing NB-G failures, neither in the delta.

Head failure set: the same two, plus
`manager/test_phase1_cursor_writer_audit.py::RepositoryAuditTests::test_repository_coverage_is_reconciled_against_an_independent_enumeration`.
That third failure is a measurement artefact, not a delta regression: the audit
reconciles the `.py` files it examined against `git ls-files`, and the full run
was taken while the two new reproducer scripts under `docs/verification-loop/repro/`
were still untracked, so it reported them as "examined but not tracked". After
scoped staging of exactly the seven intended paths the whole audit module
passes (**27 passed, 58 subtests**), and the committed tree has no untracked
`.py` file. The audit also confirmed the new scripts introduce no unaudited
cursor writer (`missing == []`).

Delta = **+16 tests, +29 subtests, all passing; no new delta failure.**
3092 collected at base + 17 new = 3109 collected at head.

## Phase E — non-vacuous test proof

**Behavioural**: reproducer at base derives ACCEPTED on A1/A4 and three A2
rows; at head all 16 are refused by the target guard's own reason.

**Mutation** (`docs/verification-loop/repro/B31-MUTATION-MATRIX.py`, ROOT =
`parents[3]`, baseline green first, harness re-run under each mutant, sha256
restore check, tree-unchanged check):

| Mutant | Guard neutralised | Attack under mutant | Named target | Verdict |
|---|---|---|---|---|
| M-B31-1 | `consumption_divergence_field` always returns None | A1 **ACCEPTED**, `false_accepts` = the same 5 as base | `test_nba_1_…` | KILLED |
| M-B31-2 | `_fold_ticket` never consults the binding (faithful revert to the `b38ceb2` fold) | A1 **ACCEPTED**, same 5 | `test_nba_4_…` | KILLED |
| M-B31-3 | `expected_checker_identity` added to the mutable allowlist | A1 **ACCEPTED** (only A1) | `test_pin_1_…` | KILLED |

3/3 killed by their named target, 0 survived, 0 not applied, 0 killed for the
wrong reason, every file restored byte-exact, tree status identical before and
after. None of the mutants is a syntax error; under M-B31-1 and M-B31-2 the
attack re-reaches exactly the base false-accept set, which is the proof that
no sibling guard is doing this guard's work.

**Why the tests are not vacuous against sibling guards**: each attack test
asserts the guard's own reason `TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE` and the
name of the rewritten field, not merely "not ACCEPTED". The 11 rows a sibling
guard already stopped at base would otherwise pass with the guard removed.

## Phase F — independent review: PENDING

Reviewer brief (a different AI session, read-only, adversarial):

1. Re-run `B31-NBA-FORGED-CONSUMPTION.py` unmodified at `b38ceb2` and at the implementation commit, from fresh clones at equal path length with a short scratch dir. Expect exit 1 / exit 0 with the tables above.
2. Re-run `B31-MUTATION-MATRIX.py` at head. Expect 3/3 KILLED, tree unchanged.
3. Try to write a consumption record the store would accept that differs from its issue record on a frozen field. `consume()` uses `replace()` on the issue record; find another writer or show there is none.
4. Try to reach ACCEPTED with the issue record untouched and no report edited. The known open shape is T7/P1b (forged report + agreeing consumption claiming the honest identity): confirm it is unchanged base→head and **not** claimed closed here.
5. Check the raise-vs-fold decision: is refusing the whole ticket on a divergent consumption ever worse than the B-2 invalidated fold? Consider an honest checker whose ticket was targeted.
6. Challenge the Predicate 10 judgment above.
7. Confirm production untouched (below) and that nothing was merged.

## Production untouched

| Item | Measured |
|---|---|
| Production checkout | `…\ai-development-manager-home-recovery-combined-20260822` @ `047b218`, branch `main`, 0 dirty paths (before and after) |
| Production manager home | `C:\Users\EE\.ai-development-manager` — no `verification/` store created; runtime files untouched |
| `origin/main` | `047b21899116350a867d5031acb2b128ab04d235`, unmoved |
| Work location | scratch clones under the session scratchpad only; every store root a temporary directory |

## Quota evidence (AI-DEVELOPMENT-RULES rule 11/30)

Source: `runtime/quota_history.json`, entries `source=claude_oauth_usage`,
`source_type=official`, `confidence=official`. Latest before this task:
`account-a` observed 2026-09-07T05:22:48Z — five_hour 8 % used (resets
09:59:59Z), seven_day 55 % used (resets 2026-09-09T19:59:59Z). The
`statusline-payload.json` snapshot is dated 2026-09-02 and was treated as
stale, not current. No model change was made on quota grounds.

## Cross-link

- Implementation commit: `c296710ac999951eebea9ae6f9a8c25f7717bda1` on branch `fix/verification-loop-b3-1-nba-consumption-binding-20260907` (pushed; remote == local)
- Drive evidence document: `1vugFHNgSZupxflnPl_T3iQCHXhbauxrPyIkAUjarv1I` — "ADM EVIDENCE — Verification Loop Phase B3-1 Predicate 10 / NB-A Closure — 2026-09-07", in ADM project folder `16MO98FfbnwXsin-m6F1rgdmk-o6ZslVC`. The Drive document names the implementation commit; this file names the Drive document. The cross-link itself is a docs-only follow-up commit on the same branch.
