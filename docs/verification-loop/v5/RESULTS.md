# V5 executable run — 2026-09-05

Command: `python3 docs/verification-loop/v5/tests/run_v5.py`  
Worktree: isolated `feat/verification-loop-v5-kernel-20260905`  
Base: `047b21899116350a867d5031acb2b128ab04d235` (`origin/main`)

```
LIVENESS_REQUIRED = 7/7 PASS
HARNESS_USABLE = YES
H-M1..H-M4 = KILLED
ORIGINAL_ATTACK_BLOCKED = 22/22 YES
NEW_VARIANT_FOUND = 0/22
```

| Attack | Original blocked? | New variant? | Class | Evidence |
|---|---|---|---|---|
| F01 | YES | NO | MC-A | empty oracle domain stays PENDING/ADVERSE |
| F02b | YES | NO | MC-C/context | disallowed CLAUDE.md/AGENTS.md poisons review |
| F03 | YES | NO | MC-A | review.claim PENDING |
| F04 | YES | NO | MC-A/semantic | semantic findings require typed adjudication |
| F05 | YES | NO | MC-B | candidate_status ignored until derived |
| F06 | YES | NO | MC-B/trust | candidate-minted trust root rejected |
| F07 | YES | NO | MC-B | APPROVE + mechanical FAIL is not ACCEPTED |
| F08 | YES | NO | MC-B | candidate attester cannot satisfy mechanical.tests |
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

Corpus identity: charter-aligned reconstruction (historical KERNEL_V3 attack
files not in SSOT). Named literal survivors F02b/F12/F13/F21 are present.

Artifacts A–F modified: **NO**  
Production code modified: **NO**  
Activation: **NONE**  
Independent review: **REQUIRED**
