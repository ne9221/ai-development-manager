# KERNEL_V5 — Safety + Reachability Kernel

version: 5.0.0-draft  
status: **DRAFT_COMPLETE** — **INDEPENDENT_REVIEW = REQUIRED**  
date: 2026-09-05  
session: grokbuild-adm-verification-loop-v5-20260905  
implementation: `docs/verification-loop/v5/` executable reference model  
production runtime: **NOT modified, NOT activated**

V5_IMPLEMENTATION_STATUS = DRAFT_COMPLETE  
INDEPENDENT_REVIEW = REQUIRED  
V4_STATUS = **DO_NOT_ADOPT**

This file is the prose twin of `docs/verification-loop/v5/src/v5_kernel/kernel.py`.
If they disagree, the executable model is the contract under test; this file must be
updated rather than the tests weakened.

---

## 0. Discovery note (honest provenance)

KERNEL_V3 / KERNEL_V4.md / artifacts A–F / v3 22-attack source files were **not
found** in:

- GitHub `ne9221/ai-development-manager` (all 311 remote branches; no path or commit named KERNEL_V*)
- Drive `01-ADM`, `AI Development Manager` TASKS/HANDOFFS/WORK-LOGS
- this Grok project `/workspace/artifacts` (only unrelated `adversarial-review-convergence`)

v3/v4 conclusions below are therefore **task-charter-fixed** from the v5 dispatch,
not independently hashed against a missing KERNEL_V4.md. They are recorded so they
cannot be silently dropped. They are **not** a claim that those files were read.

Artifacts A–F: **untouched** (and not present in this worktree). Do not patch them
to fit v5. Re-derive only after independent review PASSes.

---

## 1. Master classes (fixed; do not reinvent)

| ID | Name | Rule |
|---|---|---|
| MC-A | Vacuous satisfaction | Absence / empty domain MUST NOT satisfy a universal obligation. |
| MC-B | Assertion in place of derivation | A value written somewhere is not a trusted source. Must be derived in this `decide()` or attested by a producer that cannot mint the attestation. |
| MC-C | Binding to a movable reference | Verification MUST NOT bind only to an attacker-movable instant or pointer. |
| MC-D | Remedy-induced unreachability | Closing a false-ACCEPTED path MUST NOT make a governance-complete honest path unable to reach ACCEPTED. |

v4 scoping rule **REJECTED**: “if a finding cannot be filed under INV-1..3 it must wait for v5”.
v5 allows `NEW_INVARIANT_CANDIDATE` records (prove + human adjudicate). They do **not**
auto-expand the invariant set.

---

## 2. Invariants (mechanical)

| ID | Name | Mechanism |
|---|---|---|
| INV-1 | Totality | Every required obligation exists (controller floor) and has an explicit disposition ≠ implicit absence. Floor insertion creates PENDING, never SATISFIED. |
| INV-2 | Derivation-or-Attestation | Critical facts: derived inside this `decide()` **or** attested by LAUNCHER / PINNED_CONTROLLER / HUMAN_OPERATOR. Candidate-minted values are ignored. |
| INV-3 | Interval Binding | Critical predicates have OPEN observation **and** CLOSE re-derivation. Entrance-only is forbidden. |
| INV-4 | Reachability | For any task that satisfies governance, has complete evidence, and has no blocker: there exists a finite, allowed sequence of transitions to ACCEPTED that does not require unauthorized human action. First-class, not an appendix. |

---

## 3. Fixed outputs

```
V5_STATE_MODEL = [OPEN, VERIFYING, WAITING_RECOVERABLE, REQUIRES_RE_ADJUDICATION, ACCEPTED, REJECTED, HUMAN_REQUIRED]
OBLIGATION_STATE_MODEL = [PENDING, SATISFIED, ADVERSE, UNAVAILABLE_RECOVERABLE, UNAVAILABLE_HUMAN, INVALIDATED]
CONTROLLER_TRUST_ROOT = LAUNCHER_CAPTURED_CONTROLLER_AND_POLICY_DIGEST
REVIEWER_OUTPUT_ROLE = CLAIM
REVIEW_CONTEXT_BINDING = LAUNCHER_CAPTURED_MANIFEST_DIGEST
INVALIDATION_POLICY = ASYMMETRIC
```

Executor-done, missing-evidence, recoverable-failure, adjudication-required, and
accepted are **not** the same state.

`absence ≠ SATISFIED`.

---

## 4. State machine

Form: **state + event + guard → next_state + next_action**.

| from | event | guard | to | next_action |
|---|---|---|---|---|
| OPEN | EXECUTOR_DONE | always (done is an event, never ACCEPTED) | VERIFYING | replay + bind |
| OPEN | VERIFY_START | always | VERIFYING | replay + bind |
| VERIFYING | MECHANICAL_REPLAY | attester ∈ {LAUNCHER, PINNED_CONTROLLER} ∧ result=PASS | VERIFYING | SATISFY mechanical.tests; decide |
| VERIFYING | MECHANICAL_REPLAY | attester=CANDIDATE | VERIFYING | leave PENDING (MC-B) |
| VERIFYING | REVIEW_CLAIM | files ⊆ allowed manifest | VERIFYING | store CLAIM; verdict is not DECISION |
| VERIFYING | REVIEW_CLAIM | files include candidate CLAUDE.md/AGENTS.md | VERIFYING | ADVERSE review.claim (F02b) |
| * | VERIFIER_UNAVAILABLE | kind ∈ recoverable ∧ budget remaining | WAITING_RECOVERABLE | retry identity + remaining |
| WAITING_RECOVERABLE | RETRY | budget remaining | VERIFYING | re-enter verification |
| WAITING_RECOVERABLE | RETRY | budget exhausted | HUMAN_REQUIRED | closed-set: automated_recovery_budget_exhausted |
| * | STALE_BINDING | OPEN/CLOSE disagree | REQUIRES_RE_ADJUDICATION | REDERIVE (not human) |
| REQUIRES_RE_ADJUDICATION | REDERIVE | OPEN PASS ∧ CLOSE PASS | VERIFYING | SATISFY interval; decide |
| REQUIRES_RE_ADJUDICATION | REDERIVE | OPEN PASS ∧ CLOSE FAIL | REQUIRES_RE_ADJUDICATION | do not keep old PASS |
| REQUIRES_RE_ADJUDICATION | REDERIVE | OPEN ADVERSE ∧ CLOSE MISSING | REJECTED | not clean (asymmetric) |
| * | ADVERSE_BLOCKER | real adverse | REJECTED | must not ACCEPTED |
| * | HUMAN_GATE | reason ∈ closed set | HUMAN_REQUIRED | wait allowed-issuer record |
| * | HUMAN_GATE | reason ∉ closed set | WAITING_RECOVERABLE | treat as recoverable |
| HUMAN_REQUIRED | ADJUDICATE | issuer ∈ {HUMAN_OPERATOR, PINNED_CONTROLLER} | VERIFYING | apply freeze; decide |
| HUMAN_REQUIRED | other | sticky | HUMAN_REQUIRED | wait human (no loop) |
| VERIFYING | CLOSE_WINDOW | close bound | VERIFYING | decide |
| VERIFYING | (decide) | INV-1..4 ∧ no blockers | ACCEPTED | derived terminal |
| ACCEPTED/REJECTED | * | terminal | same | ignore |

`decide()` is the only producer of ACCEPTED. Executor `status=ACCEPTED` is ignored (MC-B)
and is not a permanent taint (MC-D).

---

## 5. Fail-closed states — recovery is mandatory

Every non-accepting state declares why, owner, action, retry condition, terminal
condition, and human-escalation eligibility. **FAIL → ESCALATE** is forbidden.

| state | why non-accepting | recovery owner | recovery action | retry condition | terminal | human eligible |
|---|---|---|---|---|---|---|
| OPEN | obligations pending | CONTROLLER | await EXECUTOR_DONE → VERIFY_START | executor_done | never | no |
| VERIFYING | decide() has not derived ACCEPTED | CONTROLLER | replay, bind CLOSE, dispose obligations | evidence/replay available | ACCEPTED/REJECTED/HUMAN/WAIT | no |
| WAITING_RECOVERABLE | UNAVAILABLE_RECOVERABLE | LAUNCHER | bounded retry / rederive, same identity, idempotent | retry identity ready ∧ budget remaining | exhaust → HUMAN_REQUIRED or REJECTED, **never ACCEPTED** | no |
| REQUIRES_RE_ADJUDICATION | OPEN/CLOSE disagree; old PASS discarded | CONTROLLER | rederive CLOSE; typed adjudication if semantic | close_rederived or typed record | SATISFIED→VERIFYING; adverse→REJECTED | no |
| REJECTED | explicit adverse / invalid write | NONE | new task required | none | REJECTED | no |
| HUMAN_REQUIRED | closed-set reason active | HUMAN | named human action + allowed-issuer record | human record with allowed issuer | stays until record or withdraw | yes (closed set only) |

Each mechanism must answer:

1. Which false-ACCEPTED path does it close?
2. How does an honest task pass it (mechanical sequence)?

If (2) has no mechanical answer, the mechanism **cannot enter v5**.

---

## 6. CONTROLLER_TRUST_ROOT (minimum sufficient; no PKI)

```
TrustRoot = {
  captured_by: LAUNCHER,            # never CANDIDATE_EXECUTOR
  controller_src_sha256: sha256(KERNEL_V5 | version | kernel.py bytes),
  policy_id, policy_sha256,         # frozen at OPEN
  kernel_id: KERNEL_V5,
  authoritative: true
}
```

- Launcher captures the digest **before** `decide()`.
- CLOSE re-derives the live digest; mismatch ⇒ not ACCEPTED.
- Candidate-supplied digest is rejected if capturer is CANDIDATE or bytes differ.
- Placeholder / `KERNEL_V5_AUTHORITATIVE=false` cannot enter the fleet (H-M3).
- Policy identity is the OPEN policy digest; moving policy after OPEN fails INV-3.

This is not a PKI platform. The launcher is the attester. The candidate cannot
write the capture.

---

## 7. Obligation model

`OBLIGATION_STATE_MODEL = [PENDING, SATISFIED, ADVERSE, UNAVAILABLE_RECOVERABLE, UNAVAILABLE_HUMAN, INVALIDATED]`

- Required set is computed from the **pinned policy**, not the candidate bundle.
- Missing required ids are floored as PENDING (existence ≠ satisfaction).
- SATISFIED only via DERIVED (this `decide()`) or ATTESTATION/CLAIM from a non-candidate attester.
- `discharged` is **not** a state (v4 undefined discharged is REJECTED).

---

## 8. Reviewer output

`REVIEWER_OUTPUT_ROLE = CLAIM` (not ATTESTATION, not DECISION).

- Reviewer prose/verdict/findings are evidence input.
- Controller replays mechanical criteria itself (launcher-attested replay).
- `findings=[]` cannot ACCEPT a mechanically failing task (closes F12).
- Semantic findings require a typed adjudication record from an allowed issuer.
- Honest L2 still works: required `review.claim` must be **present and context-bound**;
  controller derivation, not reviewer verdict, produces ACCEPTED.

---

## 9. REVIEW_CONTEXT_BINDING

```
allowed_context_manifest  — frozen at OPEN
launcher-captured context digest / file set
review invocation identity
actual files used ⊆ allowed
```

Candidate-controlled `CLAUDE.md` / `AGENTS.md` / `prompt.override.md` cannot be
silently included. Candidate prompt text cannot change review policy. Closes F02b
without a surveillance platform.

---

## 10. Writer / freeze ownership (closes F13)

Every ACCEPTED-affecting override / freeze / re-adjudication record has:

- allowed issuer ∈ {HUMAN_OPERATOR, PINNED_CONTROLLER}
- scope, subject, expiry/lifetime, provenance
- **cannot** be minted by CANDIDATE_EXECUTOR (ignored, not applied)

No IAM platform. Closed issuer predicate + expiry + subject is the minimum.

---

## 11. ORACLE_ATTRITION

Identity = **oracle item id**, not path.

- expected set frozen at OPEN
- observed set at CLOSE
- missing expected id → ADVERSE / non-accepting
- rename only via authenticated lineage map (`old_id → new_id`)
- `git mv` / delete / relocate cannot shrink the domain by making the old id vanish

---

## 12. Interval binding

`INVALIDATION_POLICY = ASYMMETRIC`

- OPEN + CLOSE required
- OPEN true / CLOSE false → cannot keep old PASS → REQUIRES_RE_ADJUDICATION
- OPEN adverse / CLOSE disappeared → not clean
- Recovery (MC-D): REDERIVE then honest CLOSE PASS can SATISFY and reach ACCEPTED (L4)

---

## 13. Human escalation closed set

Allowed:

- login / authentication / consent
- permission / credential provisioning
- destructive action approval
- intentional human gate
- unresolved governance conflict
- automated recovery budget exhausted
- genuinely semantic decision with no objective verifier

**Not** human (first: WAITING_RECOVERABLE + bounded retry):

- stale observation
- temporary reviewer unavailable
- transient read error
- retryable provider failure
- cache miss
- missing recomputable evidence

---

## 14. Recovery budget

Every recoverable state has retry count, retry identity, idempotency, next-attempt
condition, exhaustion outcome.

Budget exhausted ≠ ACCEPTED. Usually HUMAN_REQUIRED (closed-set
`automated_recovery_budget_exhausted`) or REJECTED by failure type. HUMAN_REQUIRED
is sticky; further RETRY does not loop.

---

## 15. KEEP from v4 (re-checked against INV-4)

- MC-A / MC-B / MC-C (and MC-D as first-class)
- ACCEPTED derived
- verification layer over old acceptance-gate production semantics (this slice does not replace `manager/acceptance_gate.py`)
- ORACLE_ATTRITION by item id
- asymmetric invalidation
- harness self-certification before aggregation
- repair lineage does not add schema in this slice

---

## 16. REJECT from v4

- v4 as a whole — **DO_NOT_ADOPT**
- 18/19 stacked mechanisms glued by prose
- unprotected controller digest
- undefined `discharged`
- entrance-only binding
- reviewer `findings=[]` as acceptance oracle
- undefined freeze writer
- unrestricted human escalation
- “fourth class must wait for next version” scoping rule
- fail-closed terminal without recovery semantics

---

## 17. Liveness (executed before attacks)

LIVENESS_REQUIRED = 7/7 PASS (see `tests/test_liveness.py`).

| ID | Honest case | Required destination |
|---|---|---|
| L1 | LOW, complete evidence, automated verifier | ACCEPTED |
| L2 | MEDIUM, independent review claim, no blocker | ACCEPTED |
| L3 | first verifier unavailable | WAITING_RECOVERABLE → retry → ACCEPTED (not HUMAN, not ACCEPTED on fail) |
| L4 | stale at CLOSE | rederive / re-adjudicate → ACCEPTED |
| L5 | real adverse blocker | not ACCEPTED (REJECTED) |
| L6 | destructive approval | HUMAN_REQUIRED; after allowed human record, INV-4 still allows ACCEPTED |
| L7 | retry exhausted | stable HUMAN_REQUIRED; no infinite loop; not ACCEPTED |

If any honest non-human-required case cannot ACCEPT, v5 = FAIL.

---

## 18. Safety measurement rule

Report separately:

- ORIGINAL_ATTACK_BLOCKED = YES|NO
- NEW_VARIANT_FOUND = YES|NO

Do **not** score “original killed, new variant found” as original NOT_BLOCKED.

Named charter survivors that v4 failed to close: **F02b, F12, F13, F21**.

---

## 19. Harness integrity

Aggregation is forbidden unless `HARNESS_USABLE = YES`.

Gate:

- roster complete
- unresolved failures = 0
- effective results have no error sentinel (`[object]`, usage-limit, …)
- verdict / enums valid
- prose-required agents usable
- no placeholder input
- authoritative kernel digest captured
- no BLOCKED ∧ `block_is_degenerate=true`
- original-vs-new-variant fields complete

Historical harness failures remain on record in
`docs/verification-loop/v5/EVIDENCE_PRIOR_ROUNDS.md`. They are not rewritten as success.

Non-vacuity mutants, all killed: H-M1 missing agent, H-M2 `[object]` sentinel,
H-M3 placeholder kernel, H-M4 BLOCKED+degenerate.

---

## 20. Out of scope this slice

- production runtime implementation / import from `manager/`
- activation
- patching artifacts A–F
- unifying risk lattices / gate_id / bundle schema
- self-signing READY

Next: independent read-only review by a different provider or independent session
(Claude / Codex / AG). Reviewer MUST NOT modify v5.
