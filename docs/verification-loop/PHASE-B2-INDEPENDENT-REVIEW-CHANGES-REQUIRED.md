Review complete. Full evidence gathered.

## Verdict: `CHANGES_REQUIRED`

**Phase B-2 @ `794db70`** vs accepted baseline `62b403b`. Read-only throughout; production untouched.

---

## Preflight readback (measured, not quoted)

| Item | Measured |
|---|---|
| Governance SSOT | Drive `1vSX5Bi…SYrU` — **AI-DEVELOPMENT-RULES v0.5.0**, 47 rules, §10 rules 46–47 present |
| Project rules | Drive `1NOZMdOo…GxfAQ` — PROJECT-RULES — ADM |
| Repo / branch | `ne9221/ai-development-manager`, `feat/verification-loop-phase-b2-runtime-ready-20260906`, tip == `794db70` |
| base→HEAD | `62b403b` is a true ancestor; 4 commits; 25 files |
| main | `047b218` — **unmerged**, untouched |
| Production checkout | `…-home-recovery-combined-20260822` @ `047b218`, `main`, **0 dirty**, marker intact (re-read at end: unchanged) |
| Production home | `~/.ai-development-manager` — no `verification/` dir created; stores refused it |
| Lineage | TESTED == ACTIVATED == RUNNING == `047b218` |
| Durable records | Phase A v3 / B-1 / B-1R all present in Drive **and** GitHub; all 3 sha256 digests **byte-exact** vs claim |
| Diff scope | Entirely `manager/verification_loop/` + its tests + `docs/verification-loop/`. `acceptance_gate` untouched |

---

## What I verified independently (and it held)

**159 hostile probes across sections D–Y — zero bypasses**, all written against raw constructors, each with a live control:

- **D identity (13)** — account/session/provider swaps, blank fields, unclassified, conflicting signals, low confidence, absent, self-asserted, executor==checker, same-persona-different-triple. All rejected. `resolution_failure` is correctly checked *before* `same_actor`, closing the B-1 `None`-is-safe fail-open.
- **E lineage (12) / F ticket (8) / G TOCTOU (6)** — every swap rejected; head drift and ticket-issued-against-different-head correctly escalate to `INVALIDATE_ROUND` for the **whole round**; PASS+FAIL on one gate/round is order-independent.
- **L/M/N/O (36)** — 12 hostile path spellings behave exactly as specified (`..`, absolute, drive-letter, UNC rejected, not normalised; `manager` ≠ `manager-extra`); risk is monotonic; **19 distinct bundle inputs each move `bundle_hash`**; a hand-edited golden with a stale hash routes to `OPEN_BUNDLE_REVISION`.
- **R production (10)** — including the escape the implementer says they fixed: a `production_write` gate the tier does **not** require now routes `ROUTE_TO_PFP`, for `PASS`, `FAIL` and `DEFERRED_TO_PFP` alike. I confirmed it's load-bearing (mutant R19 killed), not passing for an unrelated reason.
- **H/I/J/K/P/Q/T/Y (60)** — baseline comparison refuses to answer without an attestation at the same `base_sha` in an equal environment; transient needs a checker-issued allowlisted code on a retryable gate and never outranks a concrete defect; budgets fold over the chain and survive a new Execution; STALE vs REVOKED are genuinely distinct and both block Task close; all three original fixtures still derive, and their clean variants still reach ACCEPTED.
- **S — no second truth system.** The only filesystem writes in the package are the two create-only ledgers. No persisted verdict, no `LoopRun`, no second registry.

**Regression, measured myself** (equal 130-char paths, fresh `AI_MANAGER_HOME` per run, run twice each):

| SHA | passed | failed | subtests |
|---|---|---|---|
| `62b403b` base | 2878 / 2878 | 2 / 2 | 434 |
| `794db70` HEAD | 3035 / **3036** | 3 / **2** | 434 |

Second runs reproduce the implementer's claim exactly: **+158 = the verification-loop growth (37→195)**, same two pre-existing failure IDs. HEAD run 1 showed a third failure, `test_auth_ready_but_stale_quota_falls_back_to_fresh_sibling` — it passes in isolation at *both* SHAs, is not in the delta, doesn't import `verification_loop`, and did not recur. **Pre-existing flake, not a delta regression.** Noted, not charged.

**My own 33 mutants: 29 killed, 4 survived, 0 not-applied.** Of the brief's mandated R1–R20, 17 killed; three survivors analysed below.

---

## Blocking findings

### B-1 (BLOCKER) — durable evidence is trusted on read; the ledgers verify nothing

Two vectors, one root cause: `stored_reports()` / `stored_tickets()` rehydrate whatever JSON is on disk. The content-address is computed **only at write time**; nothing recomputes it on read.

**1a — edit a stored report, and a blocked run becomes ACCEPTED:**
```
honest store              -> REPAIR
edit 1ae33a35ba01.json: "result": "FAIL" -> "PASS", drop observations
after tamper              -> ACCEPTED
```
The filename is still the digest of the *FAIL* content. Nothing compares them.

**1b — edit a stored ticket, and any checker becomes authorised:**
`ticket_id = sha256(execution‖candidate‖bundle‖gate‖round)`. `expected_checker_identity`, `forbidden_identity`, `task_id`, `base_sha` and `status` sit **outside** that hash, so `ticket_self_consistency_reason` still passes. Rewriting `expected_checker_identity` to a `rogue` actor and answering as `rogue` gives `ACCEPTED`, **invalidated reasons: none**. (Executor self-certification is still blocked separately — `EXECUTOR_EQUALS_CHECKER` is independent. The bypass admits an *unaccountable third party*, not the executor.)

This contradicts the module's own stated guarantee in `stores.py`: *"overwrite the report that failed with one that passes is not an operation this store offers."* True of the API; false of the store.

**Phase A v3 mapping** — this is not a hardening idea I invented, it is unimplemented spec. v3 specifies the persistence primitive as *"append-only, immutable, **content-addressed**"*, gives the ticket a `consumed_report_digest` field, and states admission **predicate 11**: `report_digest` 與 ticket 消費時記載者相等（append-only ledger, `ticket_seq` 單調、CAS）. In B-2, `VerificationReportFixture` has no `report_digest` field, `VerificationTicket` has no `consumed_report_digest`, `status` never leaves `"issued"`, and there is no consumption step. **Predicate 11 is absent, and its absence is exactly what makes both vectors work.** It is also not listed among the implementer's §12 residuals.

**Minimal repair scope:** verify the content address on read — recompute `report_digest` over the rehydrated record and refuse a mismatch (inadmissible or raise), in `FileReportStore.get` / `stored_reports`; and bind the ticket's authorisation fields, either by widening `derive_ticket_id` or by storing and re-verifying a full-content digest in `stored_tickets`. Implementing predicate 11 (`consumed_report_digest` recorded at consumption, compared at admission) closes both at the architectural level.

### B-2 (BLOCKING) — a self-declared `base_result` outranks the frozen baseline attestation

`classification.base_evidence` consults report-authored `observation.base_result` **before** the bundle's attestation, and never checks the two for contradiction.

```
bundle attests: known_baseline_failures=()  attested_by='baseline-report-digest-1'
   => the contract attests this signature PASSED at base

report does not measure base   base=PASS  ACCEPTANCE_CONTRACT_DEFECT -> OPEN_BUNDLE_REVISION
report CLAIMS base also failed base=FAIL  TEST_DEFECT                -> REPAIR_TEST_ONLY
```

**Expected** (brief §I test 2): a claimed TD-1 that the attestation contradicts must not be admitted. **Actual:** the claim wins, converting a human-gated contract-revision on a proven regression into an automated test-edit licence — the "adjust the test until it passes" route this layer exists to close. It re-introduces the self-declared-field class that `category` and `is_regression` were deleted to eliminate.

I state the severity plainly: this does **not** by itself produce a false ACCEPTED inside B-2, since no repair executor exists yet. I am grading it blocking because the brief mandates the test, the frozen-oracle guard only covers goldens (an ordinary test file is freely editable), and the fix is small.

**Minimal repair scope:** in `base_evidence`, when `observation.base_result` contradicts an attestation that covers the same `base_sha` and environment, return `UNKNOWN` (or let the attestation win) rather than the claim.

---

## Non-blocking (must not block B-2; recorded so they aren't lost)

- **N-1 — `REPORT_TASK_ID_MISMATCH` is load-bearing and untested.** Mutant R3 survived all 195 tests. Removing it yields cross-Task evidence reuse → `ACCEPTED`. It is the *only* barrier, because `task_id` is not in the ticket-id hash — Task A's and Task B's tickets share an identical `ticket_id` (I verified this). Brief §E item 10 has no test.
- **N-2 — `worktree_lock_id` / `worktree_generation` are inert.** Recorded on every ticket, never compared to anything; `PreflightFacts` carries no lease state, so brief §G items 4 and 7 (lease generation change, stale generation) cannot be detected. Head-drift covers the common case; a lease change at identical HEAD is invisible. Write-only fields create false assurance.
- **N-3 — `result="PASS"` with non-empty `failure_observations` silently discards them** and satisfies the gate. Grants a compromised checker no new power, but a self-contradictory report should not be admissible.
- **N-4 — mutants R15 and R20 survived but are not defects.** R15 (ticket candidate-SHA binding) is genuinely redundant — the id changes, and `CANDIDATE_SHA_MISMATCH` catches it. R20 (cached-ACCEPTED shortcut) is *additive*: the production code is correct and re-derives, but the "no ACCEPTED shortcut" invariant is not test-locked against that shape. R23 (duplicate tie-break) is unreachable behind ticket binding, exactly as the source comment says.
- **N-5 — v3 predicate 10 (replay) unimplemented** — already disclosed honestly in §12.

Two of the implementer's four recorded deviations are worth a human decision, not a fix: `INVALIDATE_ROUND` as a new action, and `OPEN_BUNDLE_REVISION → BLOCKED_HUMAN`. Both are defensible; neither fails open.

---

## Assessment

The derivation core is genuinely strong. Every identity, lineage, ticket, CAS, risk, path, impact, bundle-freeze, STALE/REVOKED, budget and production-boundary attack I could construct was rejected, the three original false-complete fixtures still derive rather than hard-code, and the happy paths still accept — this is not a system that has been sealed shut to look safe. The implementer's regression numbers and the production-write escape fix both replicate exactly, and their evidence document is candid about its own gaps.

What is missing sits one layer below the logic: the loop reasons perfectly over evidence it never checks the integrity of. Both blocking findings are that same shape — trusting a record because of where it came from rather than what it hashes to.

**Phase B-2 may not close.** Do not start B-3.

Everything above is reproducible from a single script I left at `scratchpad/REPRO_B2_REVIEW.py` (3/3 reproduce at `794db70`); the mutation harness is `scratchpad/mutate.py` and the probe suites are `probe_defg.py`, `probe_lmnor.py`, `probe_hijkpqt.py`, `probe_z.py`. I made no repairs and touched no code, Drive doc, or production path.

Want me to publish this as a shareable review page you can hand to the implementer?