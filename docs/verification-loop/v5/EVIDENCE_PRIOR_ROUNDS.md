# Prior-round evidence — do not erase

These records exist so later slices cannot pretend the failures never happened.
KERNEL_V3 / KERNEL_V4.md were **not locatable** in GitHub or Drive SSOT at
2026-09-05. Content is **task-charter-fixed** from dispatch
`GrokBuild-ADM-VerificationLoop-v5-20260905`.

## v3 (charter)

- 22 / 22 exploit paths NOT_BLOCKED.
- Master classes MC-A / MC-B / MC-C already named.

## v4 (charter) — DO_NOT_ADOPT

Original attacks mostly improved, but:

- F02b literal still unsolved (planted reviewer instructions / context poisoning)
- F12 literal still unsolved (reviewer `findings=[]` as oracle)
- F13 literal still unsolved (freeze/override without writer predicate)
- F21 literal still unsolved (movable latest-review / reference binding)
- cross-class seams still present
- 3 / 3 liveness controls DEGENERATE
- v4 must not be adopted

v4 defects called out for rejection: stacked 18/19 mechanisms, unprotected
controller digest, undefined `discharged`, entrance-only binding, reviewer
findings as acceptance oracle, undefined freeze writer, unrestricted human
escalation, “new class waits for next version” scoping, fail-closed without
recovery.

## Harness integrity failures (must remain)

| ID | What happened | Why it must stay |
|---|---|---|
| HF-USAGE-LIMIT-SWALLOWED | agent usage-limit failure was incorrectly swallowed | gate: unresolved_failures=0; usage-limit is ERROR |
| HF-OBJECT-SENTINEL | `[object]` sentinel treated as a result | gate: no error sentinels in effective results |
| HF-PROSE-VERDICT-DIVERGENCE | prose divergence treated as verdict divergence | gate: prose usability ≠ verdict enum |
| HF-PLACEHOLDER-KERNEL | placeholder kernel nearly entered the attack fleet | gate: authoritative kernel digest captured |

Do not delete, rewrite-as-pass, or “clean the history” of these rows.

## Artifacts A–F

5 incompatible risk lattices, 3 gate_id schemes, 20+ bundle field mismatches,
0/4 fixtures emitting the claimed next_action — charter. **This slice does not
patch A–F.** Re-derive after independent review of v5.
