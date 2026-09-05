# KERNEL_V5 — Safety + Reachability Kernel

version: 5.0.0-draft+r1  
status: **R1_REMEDIATED** — **INDEPENDENT_RE_REVIEW = REQUIRED**  
date: 2026-09-05 (Remediation Round 1)  
session: claude-adm-verification-loop-v5-r1-20260905  
repair base: `c11685417bce7824fbfa380426d2471f714aff7d`  
implementation: `docs/verification-loop/v5/` executable reference model  
production runtime: **NOT modified, NOT activated**

V5_IMPLEMENTATION_STATUS = R1_REMEDIATED  
INDEPENDENT_REVIEW = REQUIRED (delta-only re-review of B1–B7 closure)  
V5_READY = NO  
V4_STATUS = **DO_NOT_ADOPT**

Round 1 closed the seven blockers B1–B7 found by independent review, plus C7
(oracle lineage reuse) and C10 (human-escalation DoS). See section 21.

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
EVENT_SOURCE_BINDING = TYPED_ENVELOPE_INJECTED_AT_THE_CONTROLLER_LAUNCHER_BOUNDARY
REFERENCE_MODEL_TRUST_BOUNDARY = ENVELOPE_INJECTED_AT_CONTROLLER_LAUNCHER_API_BOUNDARY
RUNTIME_ATTESTATION_BOUNDARY = NOT_IMPLEMENTED_IN_THIS_SLICE
TRUST_ROOT_RUNTIME_PROVEN = NO
ACCEPTED_LIFECYCLE = TERMINAL_FOR_ONE_VERIFICATION_CYCLE_BOUND_TO_ITS_OPEN_CLOSE_WINDOW
ORACLE_LINEAGE_ARITY = 1:1
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
| VERIFYING | MECHANICAL_REPLAY | **envelope** source ∈ {LAUNCHER, PINNED_CONTROLLER} ∧ result=PASS | VERIFYING | SATISFY mechanical.tests; decide |
| VERIFYING | MECHANICAL_REPLAY | no envelope, unregistered capture id, or source=CANDIDATE | VERIFYING | leave PENDING (MC-B) |
| VERIFYING | REVIEW_CLAIM | claim complete ∧ launcher capture corroborates ∧ captured files ⊆ allowed manifest | VERIFYING | store CLAIM; verdict is not DECISION |
| VERIFYING | REVIEW_CLAIM | claim below minimum schema, or no launcher capture for the invocation | VERIFYING | leave PENDING (B4) |
| VERIFYING | REVIEW_CLAIM | captured files include candidate CLAUDE.md/AGENTS.md, or claim ≠ capture | VERIFYING | ADVERSE review.claim (F02b) |
| * | VERIFIER_UNAVAILABLE | kind ∈ recoverable ∧ budget remaining | WAITING_RECOVERABLE | retry identity + remaining |
| WAITING_RECOVERABLE | RETRY | budget remaining | VERIFYING | re-enter verification |
| WAITING_RECOVERABLE | RETRY | budget exhausted | HUMAN_REQUIRED | closed-set: automated_recovery_budget_exhausted |
| * | STALE_BINDING | OPEN/CLOSE disagree | REQUIRES_RE_ADJUDICATION | REDERIVE (not human) |
| REQUIRES_RE_ADJUDICATION | REDERIVE | OPEN PASS ∧ CLOSE PASS **over the whole required predicate domain** | VERIFYING | SATISFY interval; decide |
| REQUIRES_RE_ADJUDICATION | REDERIVE | any required predicate FAIL / MISSING / unbound at CLOSE | REQUIRES_RE_ADJUDICATION | do not keep old PASS (B3) |
| REQUIRES_RE_ADJUDICATION | REDERIVE | OPEN ADVERSE ∧ CLOSE MISSING | REJECTED | not clean (asymmetric) |
| * | ADVERSE_BLOCKER | real adverse | REJECTED | must not ACCEPTED |
| * | HUMAN_GATE | reason ∈ closed set | HUMAN_REQUIRED | wait allowed-issuer record |
| * | HUMAN_GATE | reason ∉ closed set | WAITING_RECOVERABLE | treat as recoverable (consumes budget) |
| * | HUMAN_GATE | reason = automated_recovery_budget_exhausted ∧ no exhausted controller budget | WAITING_RECOVERABLE | refuse the controller-owned reason (C10) |
| HUMAN_REQUIRED | ADJUDICATE | envelope issuer = HUMAN_OPERATOR ∧ subject = this gate's subject ∧ scope=obligation ∧ resolution=SATISFIED ∧ unexpired | VERIFYING | apply freeze; decide |
| HUMAN_REQUIRED | ADJUDICATE | issuer allowed but subject/scope/resolution/expiry mismatch | HUMAN_REQUIRED | ignore record; gate stays shut (B5) |
| REQUIRES_RE_ADJUDICATION | ADJUDICATE | envelope issuer ∈ {HUMAN_OPERATOR, PINNED_CONTROLLER} ∧ exact match ∧ resolution=SATISFIED | VERIFYING | apply freeze; decide |
| * | ADJUDICATE | resolution=ADVERSE | same | preserve the blocker; never discharge |
| HUMAN_REQUIRED | other | sticky | HUMAN_REQUIRED | wait human (no loop) |
| VERIFYING | CLOSE_WINDOW | close bound | VERIFYING | decide |
| VERIFYING | (decide) | INV-1..4 ∧ no blockers | ACCEPTED | derived terminal |
| ACCEPTED/REJECTED | * | terminal | same | ignore |

`decide()` is the only producer of ACCEPTED. Executor `status=ACCEPTED` is ignored (MC-B)
and is not a permanent taint (MC-D).

**Trusted identity never comes from the payload.** Every event may carry a typed
`EventEnvelope` injected by the controller/launcher API boundary. `payload["attester"]`
and `record["issuer"]` are recorded and then ignored. An event with no envelope, or
with a capture id the launcher never registered, resolves to CANDIDATE_EXECUTOR —
omission fails closed rather than defaulting to LAUNCHER.

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

**Honest boundary statement.** Inside this reference model the separation is the
typed `EventEnvelope`: it is a distinct parameter of `apply()`, so a candidate
payload cannot become one, and its `capture_id` must be one the launcher
registered at OPEN. That is `REFERENCE_MODEL_TRUST_BOUNDARY`. It is **not** a
runtime attestation that a real launcher process produced the event —
`RUNTIME_ATTESTATION_BOUNDARY = NOT_IMPLEMENTED_IN_THIS_SLICE` and
`TRUST_ROOT_RUNTIME_PROVEN = NO`. Do not report this slice as a production-grade
trust root.

---

## 7. Obligation model

`OBLIGATION_STATE_MODEL = [PENDING, SATISFIED, ADVERSE, UNAVAILABLE_RECOVERABLE, UNAVAILABLE_HUMAN, INVALIDATED]`

- Required set is computed from the **pinned policy**, not the candidate bundle.
- Missing required ids are floored as PENDING (existence ≠ satisfaction).
- SATISFIED only via DERIVED (this `decide()`) or ATTESTATION/CLAIM from a non-candidate attester.
- `discharged` is **not** a state (v4 undefined discharged is REJECTED).
- **INV-1 acceptance predicate: EVERY required obligation must be SATISFIED.**
  Not "present", not "not ADVERSE", not "not PENDING". PENDING,
  UNAVAILABLE_RECOVERABLE, UNAVAILABLE_HUMAN, INVALIDATED and ADVERSE are all
  non-accepting, and a floor row whose `source` is still `POLICY_FLOOR` is
  existence rather than satisfaction.
- An obligation SATISFIED by adjudication carries its backing record id, and
  that record's subject/scope/expiry is re-checked at every `decide()`.

---

## 8. Reviewer output

`REVIEWER_OUTPUT_ROLE = CLAIM` (not ATTESTATION, not DECISION).

- Reviewer prose/verdict/findings are evidence input.
- Controller replays mechanical criteria itself (launcher-attested replay).
- `findings=[]` cannot ACCEPT a mechanically failing task (closes F12).
- Semantic findings require a typed adjudication record from an allowed issuer.
- Honest L2 still works: required `review.claim` must be **present and context-bound**;
  controller derivation, not reviewer verdict, produces ACCEPTED.
- A CLAIM must meet a minimum schema: `invocation_id`, `context_manifest_digest`,
  `launcher_capture_ref`, `reviewer_identity`, `completion_status`, a non-empty
  `files_used`, and an explicitly present `findings` (absent ≠ empty).
- A CLAIM alone is never truth. The **actual** files used and the **captured**
  context digest come from the launcher's `ReviewCapture` for that invocation.
  No capture ⇒ non-satisfying. Claim disagrees with capture ⇒ ADVERSE.
  The allowlist check runs against the captured file set, not the claimed one.

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

The OPEN manifest digest is **never back-filled from a claim**. A reviewer that
supplies its own digest into an empty binding would be authenticating itself.

---

## 10. Writer / freeze ownership (closes F13)

Every ACCEPTED-affecting override / freeze / re-adjudication record must match
**exactly**, on every field. A single mismatch means the record is ignored:

- issuer comes from the **envelope**, not the record body, and must be in
  {HUMAN_OPERATOR, PINNED_CONTROLLER}; a `human.*` obligation requires
  HUMAN_OPERATOR specifically — a pinned controller cannot self-serve a human gate
- `subject` must be an obligation of this task (or a declared virtual subject),
  and to release an active human gate it must be **that gate's own subject**
- `scope` must be exactly `obligation`
- `resolution` ∈ {SATISFIED, ADVERSE}; **ADVERSE never discharges anything** —
  it preserves the adverse finding
- `issued_tick` ≤ now, `expires_tick` > now, non-empty lifetime
- non-empty `provenance_digest`
- **cannot** be minted by CANDIDATE_EXECUTOR (ignored, not applied)

Expiry is re-checked **at every `decide()`**, not only when the record is written:
an approval that was valid when spent and has since expired reverts its obligation
and re-raises the human gate.

No IAM platform. Exact-match closed predicate is the minimum.

---

## 11. ORACLE_ATTRITION

Identity = **oracle item id**, not path.

- expected set frozen at OPEN
- observed set at CLOSE
- missing expected id → ADVERSE / non-accepting
- rename only via authenticated lineage map (`old_id → new_id`), captured by the launcher
- `git mv` / delete / relocate cannot shrink the domain by making the old id vanish
- lineage arity is **1:1** (`ORACLE_LINEAGE_ARITY`). The map must be injective, and a
  lineage target may not itself be an expected id — otherwise one observation would
  satisfy two required identities. Matching consumes each observed id at most once.
- 1:N lineage is deliberately **not** implemented in this slice; it is carried forward.

---

## 12. Interval binding

`INVALIDATION_POLICY = ASYMMETRIC`

- OPEN + CLOSE required
- OPEN true / CLOSE false → cannot keep old PASS → REQUIRES_RE_ADJUDICATION
- OPEN adverse / CLOSE disappeared → not clean
- Recovery (MC-D): REDERIVE then honest CLOSE PASS can SATISFY and reach ACCEPTED (L4)

**Every CLOSE_WINDOW, every REDERIVE, and every `decide()` recomputes the interval
disposition from scratch over the whole required predicate domain**, and the result
atomically replaces the previous one. It is never incremental and never OR-ed with an
earlier PASS, so a partial rederive cannot leave a stale SATISFIED standing (B3).
A required predicate that is absent, MISSING, FAIL or unbound at CLOSE invalidates
the interval regardless of how the other predicates fared.

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

`automated_recovery_budget_exhausted` is **controller-owned**. It may only be
produced by recovery state where `used >= maximum`. A candidate that asks for it
in a payload is refused and routed to bounded recovery instead, so human
escalation cannot be used as a denial-of-service lever (C10).

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

**One function owns every recoverable route** - named kinds and the unknown/default
branch alike. Entering it always: (1) invalidates the affected obligation, (2)
consumes budget under the canonical identity `recovery:<kind>:<obligation>`, (3) is
idempotent per identity, (4) exhausts at a finite limit, and (5) leaves an obligation
state `decide()` cannot accept over. There is no route that reaches
WAITING_RECOVERABLE without spending budget (B6).

`RETRY` must name an identity that is already bound. An unknown identity is ignored
rather than allowed to borrow another obligation's budget.

Budget exhausted ≠ ACCEPTED. Usually HUMAN_REQUIRED (closed-set
`automated_recovery_budget_exhausted`) or REJECTED by failure type. HUMAN_REQUIRED
is sticky; further RETRY does not loop. Closing this must not create a permanent
latch: after RETRY the invalidated obligations return to PENDING and an honest
re-verification still reaches ACCEPTED (INV-4, L3, B6-R6).

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

- **H1** the expected kernel digest is *computed* from the authoritative kernel
  bytes; the caller supplies only an observed digest, and the two must be equal.
  A non-empty string is not a digest check (a fabricated `deadbeef` used to pass).
- **H2** roster and safety-attack set are *exact expected sets* - no missing, no
  extra. `0/0` is never a PASS.
- **H3** every safety effective row carries the full canonical schema:
  `attack_id`, `original_attack_blocked`, `new_variant_found`, `status`, `usable`,
  `kernel_digest`, `klass`, `evidence`. One missing field ⇒ NOT USABLE.
- **H4** replicated rows for one logical attack must agree on
  (`original_attack_blocked`, `new_variant_found`, `status`). A flip is divergence:
  aggregation is forbidden, and the latest row never wins.
- **H5** any *required* agent that is NOT_RUN, ERROR, unusable, sentinel-bearing or
  without an effective result makes the harness unusable - safety or not.
- **H6** a non-empty expected roster with nothing collected is unusable.
- unresolved failures = 0
- effective results have no error sentinel (`[object]`, usage-limit, …)
- verdict / enums valid; prose-required agents carry prose
- no placeholder input; no BLOCKED ∧ `block_is_degenerate=true`

The runner is gated the same way: an empty safety roster exits non-zero instead of
printing `0/0` and returning success.

Historical harness failures remain on record in
`docs/verification-loop/v5/EVIDENCE_PRIOR_ROUNDS.md`. They are not rewritten as success.

Non-vacuity mutants, all killed: H-M1 missing agent, H-M2 `[object]` sentinel,
H-M3 placeholder kernel, H-M4 BLOCKED+degenerate. Round 1 adds the reviewer's own
harness escapes as permanent regressions: **RH1** empty safety collection aggregated
as a pass (gate *and* runner), **RH2** fabricated kernel digest accepted.

---

## 19b. ACCEPTED lifecycle

```
ACCEPTED_LIFECYCLE = TERMINAL_FOR_ONE_VERIFICATION_CYCLE_BOUND_TO_ITS_OPEN_CLOSE_WINDOW
```

ACCEPTED is terminal **for one verification cycle**, bound to the OPEN/CLOSE evidence
window that produced it. Later external change does not reopen that cycle; it opens a
**new** one. This kernel performs no monitoring inside an accepted state, and this
slice does not add permanent post-acceptance surveillance.

---

## 20. Out of scope this slice

- production runtime implementation / import from `manager/`
- activation
- patching artifacts A–F
- unifying risk lattices / gate_id / bundle schema
- self-signing READY
- runtime attestation of the launcher (`RUNTIME_ATTESTATION_BOUNDARY`)
- 1:N oracle lineage (carried forward from C7)
- permanent post-ACCEPTED monitoring (carried forward from C9)

Next: independent read-only review by a different provider or independent session
(Claude / Codex / AG). Reviewer MUST NOT modify v5.

---

## 21. Remediation Round 1 (2026-09-05)

Independent review of `c116854` returned `V5_INDEPENDENT_REVIEW = REPAIR_REQUIRED`
with seven concrete bypasses. All seven were **reproduced at the repair base before
being repaired** (`tests/prefix_repro_r1.py`: 21/21 probes REPRODUCED at `c116854`,
0/21 at this HEAD, same script both times).

| ID | Bypass at `c116854` | Repair |
|---|---|---|
| B1 | omitting `attester` defaulted to LAUNCHER; a payload could self-declare the trusted label | typed `EventEnvelope` injected at the controller/launcher boundary; payload identity ignored; no envelope means CANDIDATE |
| B2 | a required obligation left UNAVAILABLE_RECOVERABLE still reached ACCEPTED; an explicitly empty oracle domain was back-filled with `oracle.unit` | INV-1 predicate is *every required obligation SATISFIED*; declared-empty domains stay empty |
| B3 | a partial CLOSE rederive kept an earlier interval SATISFIED | interval recomputed over the full required domain and atomically replaced, in every CLOSE/REDERIVE and in `decide()` |
| B4 | a claim carrying only `context_digest` satisfied a MEDIUM review, and back-filled the binding it was checked against | minimum claim schema + mandatory launcher `ReviewCapture`; captured files are the allowlist input; no back-fill |
| B5 | unrelated subject, wrong scope, `resolution=ADVERSE` and expired records all discharged a destructive approval | exact-match adjudication on issuer/subject/scope/resolution/provenance/lifetime; ADVERSE preserves; expiry re-checked every `decide()` |
| B6 | the default recovery branch spent no budget; a wrong retry identity borrowed another budget | one `_enter_recoverable_failure` for every route; canonical retry identity; unknown identity ignored |
| B7 | fabricated digest, missing row fields, PASS/FAIL replication flips, unusable required agents and an empty safety roster all reported HARNESS_USABLE=YES | H1-H6 above, plus a fail-closed runner |

Also closed: **C7** (one observation satisfying two expected oracle ids through a
lineage target that was itself expected) and **C10** (candidate self-declaring
`automated_recovery_budget_exhausted` to force a human gate).

Test-side non-vacuity repairs: **F01** now constructs a genuinely empty oracle domain
(it previously resolved to a 1-item domain and never exercised MC-A); **F18** measures
its own original attack instead of hard-coding `original_false_accept_blocked = True`.

Negative controls: `tests/mutations_r1.py` reverts each repair in isolation -
`R1_MUTATIONS = 7/7 KILLED`.

`V5_READY = NO` until an independent delta-only re-review of B1-B7 closure.
