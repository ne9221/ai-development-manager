# V5 executable run — Remediation Round 1 — 2026-09-05

Command: `python3 docs/verification-loop/v5/tests/run_v5.py`
Worktree: isolated `fix/verification-loop-v5-r1-20260905`
Repair base: `c11685417bce7824fbfa380426d2471f714aff7d`
Production baseline: `047b21899116350a867d5031acb2b128ab04d235` (`origin/main`, **unchanged**)

```
LIVENESS_REQUIRED       = 7/7 PASS
R1_REGRESSIONS          = 53/53 PASS   (B1-B7, C7, C10)
HARNESS_TESTS           = 12/12 PASS   (H-M1..H-M4 + RH1/RH2)
SAFETY                  = 22/22 PASS
HARNESS_USABLE          = YES
ORIGINAL_ATTACK_BLOCKED = 22/22 YES
NEW_VARIANT_FOUND       = 0/22
R1_MUTATIONS            = 7/7 KILLED
TRUST_ROOT_RUNTIME_PROVEN = NO
runner exit             = 0
```

## Before / after — the same script at both commits

`tests/prefix_repro_r1.py` asserts the **broken** behaviour on purpose. It is the
evidence that B1–B7 were live bypasses at the repair base, not hypotheses inherited
from a report. The identical file was run at both commits:

| Commit | Result | Exit |
|---|---|---|
| `c116854` (repair base) | `PRE_FIX_BYPASSES_REPRODUCED = 21/21` | 21 |
| this HEAD | `PRE_FIX_BYPASSES_REPRODUCED = 0/21` | 0 |

| Probe | Bypass reproduced at `c116854` |
|---|---|
| B1 | `MECHANICAL_REPLAY {"result":"PASS"}` with no attester recorded `attester='LAUNCHER'`, obligation SATISFIED, status ACCEPTED |
| B1b | payload self-declaring `attester=LAUNCHER` honoured → ACCEPTED |
| B2 | required `security.audit` = UNAVAILABLE_RECOVERABLE → ACCEPTED |
| B2b | declared `oracle_expected=[]` resolved to `['oracle.unit']` |
| B3 | rederive `{mechanical.tests: PASS, oracle.set: MISSING}` left interval SATISFIED → ACCEPTED |
| B4 | claim `{"context_digest": "ctx-bound-1"}` (invocation_id `''`, files `()`) → review.claim SATISFIED → ACCEPTED |
| B4b | claim back-filled `captured_review_context_digest='digest-i-made-up'` → ACCEPTED |
| B5a | human record for `some.unrelated.thing` set destructive approval SATISFIED → ACCEPTED |
| B5b | `scope=totally-wrong-scope` never checked → ACCEPTED |
| B5c | `resolution=ADVERSE` left destructive approval SATISFIED → ACCEPTED |
| B5d | approval `expires_tick=6` spent at `tick=16` → ACCEPTED |
| B6 | 20× `network_timeout` left budget `used=0, maximum=3` |
| B6b | unresolved default-branch failure → ACCEPTED |
| B6c | `RETRY` with an unknown identity borrowed another budget → VERIFYING |
| C10 | candidate `HUMAN_GATE {reason: automated_recovery_budget_exhausted}` with zero budgets used → HUMAN_REQUIRED |
| C7 | lineage `oracle.lint → oracle.unit`, one observed id satisfied both expected ids → ACCEPTED |
| B7a | `kernel_digest="deadbeef"` → HARNESS_USABLE=YES |
| B7b | row missing the new-variant field → HARNESS_USABLE=YES |
| B7c | two `F01` rows disagreeing on both fields → HARNESS_USABLE=YES |
| B7d | `liveness_runner usable=False verdict=NOT_RUN` → HARNESS_USABLE=YES |
| B7e | 0 safety rows → HARNESS_USABLE=YES |

## Negative controls

`tests/mutations_r1.py` reverts each repair in an isolated copy of the tree and runs
the tests that must catch it. A mutation whose source text is not found is an ERROR,
never SURVIVED; a run collecting zero tests is an ERROR, never KILLED.

| Mutation | Reverts | Verdict |
|---|---|---|
| M-R1 | attester read from payload, defaulting to LAUNCHER | KILLED (3/4 targeted tests failed) |
| M-R2 | accept a required obligation merely because it is not ADVERSE; re-invent the empty oracle domain | KILLED (4/6) |
| M-R3 | keep a previous interval SATISFIED across a later partial CLOSE | KILLED (2/4) |
| M-R4 | claim with no invocation / no launcher capture still satisfies | KILLED (6/8) |
| M-R5 | ignore adjudication subject matching and record expiry | KILLED (2/9) |
| M-R6 | recoverable failure no longer consumes retry budget | KILLED (3/6) |
| M-R7 | empty safety roster usable again | KILLED (2/10) |

`R1_MUTATIONS = 7/7 KILLED`

## Safety matrix

| Attack | Original blocked? | New variant? | Class | Evidence |
|---|---|---|---|---|
| F01 | YES | NO | MC-A | explicitly empty oracle domain stays empty and PENDING |
| F02b | YES | NO | MC-C/context | captured context outside the allowed manifest is ADVERSE |
| F03 | YES | NO | MC-A | review.claim PENDING |
| F04 | YES | NO | MC-A/semantic | semantic findings require typed adjudication |
| F05 | YES | NO | MC-B | candidate_status ignored until derived |
| F06 | YES | NO | MC-B/trust | candidate-minted trust root rejected |
| F07 | YES | NO | MC-B | APPROVE + mechanical FAIL is not ACCEPTED |
| F08 | YES | NO | MC-B | candidate source cannot satisfy mechanical.tests |
| F09 | YES | NO | MC-C/attrition | git mv path != item id |
| F10 | YES | NO | MC-C/attrition | missing expected oracle.lint is ADVERSE |
| F11 | YES | NO | MC-C | entrance-only / missing CLOSE cannot ACCEPT |
| F12 | YES | NO | MC-B | findings=[] is CLAIM; controller replay decides |
| F13 | YES | NO | writer | candidate-minted freeze ignored |
| F14 | YES | NO | MC-C/asymmetric | OPEN adverse + CLOSE missing remains adverse |
| F15 | YES | NO | escalation/MC-D | stale -> REQUIRES_RE_ADJUDICATION |
| F16 | YES | NO | escalation | reviewer unavailable is recoverable |
| F17 | YES | NO | recovery | exhaustion -> HUMAN_REQUIRED, never ACCEPTED |
| F18 | YES | NO | MC-D | stale recovery still reaches ACCEPTED |
| F19 | YES | NO | liveness | budget bounds WAITING_RECOVERABLE |
| F20 | YES | NO | cross-class | poisoned context + empty findings + candidate freeze |
| F21 | YES | NO | MC-C | latest-review pointer != invocation id |
| F22 | YES | NO | harness/trust | placeholder kernel rejected by trust root |

Two rows changed meaning in Round 1 because their tests were vacuous, not because
the kernel got weaker:

- **F01** previously declared `oracle_expected=[]` but the runtime back-filled
  `['oracle.unit']`, so the case measured a 1-item domain and never exercised MC-A.
  It now asserts `w.oracle_expected == frozenset()` before attacking.
- **F18** previously hard-coded `original_false_accept_blocked = True` and inherited
  its original-attack result from F15. It now runs the stale replay itself and
  measures the outcome.

Corpus identity: charter-aligned reconstruction (historical KERNEL_V3 attack files
not in SSOT). Named literal survivors F02b/F12/F13/F21 are present.

## Honest boundary

`TRUST_ROOT_RUNTIME_PROVEN = NO`. The `EventEnvelope` separates event source from
payload *inside this reference model* and binds each envelope to a launcher-registered
capture id. It is not a runtime attestation that a real launcher process produced the
event. `RUNTIME_ATTESTATION_BOUNDARY = NOT_IMPLEMENTED_IN_THIS_SLICE`.

Carried forward, deliberately not implemented here: 1:N oracle lineage (C7),
permanent post-ACCEPTED monitoring (C9), runtime launcher attestation.

Artifacts A–F modified: **NO**
Production code (`manager/**`) modified: **NO**
Activation: **NONE**
`R1_IMPLEMENTATION = DONE` — `V5_READY = NO`
Independent delta-only re-review: **REQUIRED**
