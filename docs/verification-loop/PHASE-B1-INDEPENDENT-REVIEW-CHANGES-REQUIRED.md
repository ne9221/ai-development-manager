AI：Claude Fable 5.1　Conversation：新開獨立對話　Model：Fable　Mode：Read-only adversarial review
Project：AI 一體化 / ai-development-manager　Task：Verification Loop Phase B-1 review　Session：verification-loop-phase-b1-independent-review-20260906

# VERDICT：CHANGES_REQUIRED

Nothing was modified: no repo write, no Drive write, no runtime-home write, no commit or push. All work ran in scratch clones of 109be49 and 047b218.

**Preflight facts.** Governance read from Drive: AI-DEVELOPMENT-RULES v0.5.0 and PROJECT-RULES — ADM v1.1.0. Phase A v3 was not attached to this task and does not exist on Drive or GitHub. I recovered it from the transcript of the Opus session titled "Verification Loop Acceptance Layer Phase A 架構" and reviewed against that text. The implementer never saw it. Their commit message and Drive evidence state honestly that they built against the B-1 task prompt's own sections D–J instead.

## 1. Blocking findings

Every item below was reproduced with a probe against the shipped code. The shipped 27 tests stay green in every case.

- **B1. A report-authored field decides the admitted class.** `_classify_observation` returns `observation.category` verbatim with no predicate ([evaluator.py:92](manager/verification_loop/evaluator.py:92)). A checker can label a clause-matched regression as ENVIRONMENT_TRANSIENT and the evaluator answers RETRY_SAME_CANDIDATE. The same trick converts an unclaused defect (the contract-gap case) into a retry. TEST_DEFECT is reachable only this way, with no TD-1 or TD-2 predicate. This reopens Phase A attack 7 through a second field. The suite only proves that `proposed_class` is ignored.
- **B2. Duplicate (gate, round) reports are order-dependent.** The "latest round wins" loop uses `>=`, so the last report in the list wins a tie. The same three reports give ACCEPTED when the FAIL precedes the PASS and REPAIR when the PASS precedes the FAIL. That breaks "deterministic derivation" outright and is a false-complete path.
- **B3. An admissible FAIL or UNKNOWN on a non-required gate is discarded.** Medium risk with V0 and V1 PASS plus an admissible V3 FAIL for a freeze-pane mismatch yields ACCEPTED. Phase A A.3 requires no active failure across all admissible reports. This is exactly the ledger shape whenever the path rule does not fire.
- **B4. PRODUCTION_SCOPE_VIOLATION from an observation never reaches ROUTE_TO_PFP.** Alone it yields ESCALATE_HUMAN plus a spurious UNKNOWN added by the fallback branch. Combined with a CONTRACT_VIOLATION it yields REPAIR, so the ordinary loop would keep repairing a production-scope change. Only the diff-derived production tier short-circuits correctly.

## 2. Important findings

- **Executor identity None skips the executor==checker check** and the run is ACCEPTED. Identity is also a single opaque string, not the Phase A (provider, account_id, session_id) triple, so "codex-impl" can never equal "unit_checker" on real data. Test 04 forces the collision artificially.
- **REGRESSION is a report flag.** `is_regression` is trusted; `baseline_data` is never read. Question 14 answer: not baseline-aware. Consequence is bounded because REGRESSION and CONTRACT_VIOLATION both map to REPAIR.
- **Tier→gate map is not monotone.** Escalating medium→artifact_sensitive with a bundle whose upper tier lists fewer gates drops V1 and V2 and ACCEPTs. The linear order also places artifact_sensitive above high, unlike Phase A E.2.
- **Diff-path match is raw `startswith`.** A path spelled `./manager/execution_runner.py`, with a backslash, or in different case evades the production rule and ACCEPTs.
- **Attack test 05 (mixed SHA) is vacuous.** Its wrong-SHA V1 report also fails on checker identity, so under mutant M1 the test still passes. The mechanism is right, but only test 01 proves it.
- **Next-action order deviates from Phase A D.2.** UNKNOWN is evaluated before ACCEPTANCE_CONTRACT_DEFECT (spec G3 before G4); GOVERNANCE_CONFLICT collapses into ESCALATE_HUMAN; the production short-circuit precedes GOVERNANCE_CONFLICT. Human-facing either way, no fail-open.
- **Lineage cross-binding is absent.** Task and execution task_id mismatch, deliverable_sha mismatch, and execution status running or failed all ACCEPT. The bundle has no id to compare with `acceptance_bundle_ref`. Impact rules and the component-escalation term are absent from the model; the docstring itself says max of two terms.
- **Regression reasoning.** "0 files modified so zero regression risk by construction" is wrong: new files under manager/ are enumerated by the git-reconciling writer audit, which is precisely the test that tripped in their run. The actual evidence is what carries the claim, and it does (section 6).

## 3. Mutation results

All 12 required mutants KILLED by the shipped suite, each reverted afterwards with a clean tree.

| Mutant | Result | Killed by |
|---|---|---|
| M1 no candidate_sha check | KILLED | test_01 only |
| M2 no bundle_hash check | KILLED | test_02 |
| M3 executor==checker allowed | KILLED | test_04 |
| M4 retry by membership | KILLED | _retry_eligible unit test |
| M5 declared risk only | KILLED | 8 tests |
| M6 UNKNOWN after REPAIR | KILLED | test_08 |
| M7 production walks ACCEPTED branch | KILLED | 3 tests |
| M8 missing gate still ACCEPTED | KILLED | 3 tests |
| M9 coverage subset passes | KILLED | mobile coverage test |
| M10 contract gap advisory | KILLED | no-clause test |
| M11 no checker_version check | KILLED | test_03 |
| M12 PASS reports skip SHA check | KILLED | test_01 only |

The blocking findings are mutants the required set does not contain. My 19 probes are at scratchpad `probes.py`.

## 4. Fixture integrity

Expected results are derived by calling `evaluate()`; no fixture hard-codes an output. Fixture 1 uses distinct bundle hashes for the with-clause and no-clause contracts, which is correct. Fixture 2's coverage case is genuinely hostile. Weaknesses: Fixture 3's production variant uses a different execution_id, so every report is rejected on EXECUTION_ID_MISMATCH before the production logic runs, and only attack test 11 exercises production with admissible PASSes. No fixture contains a V5 review gate; the I1 contract-gap case is modeled as a V3 FAIL with an unclaused signature, which is the same mechanism but leaves V5 identity-disjointness untested.

## 5. Phase A v3 → implementation traceability

Faithfully implemented: ACCEPTED derived only, no persisted state; per-report binding on execution_id, candidate_sha, base_sha, bundle_hash; checker identity and version admission; frozen-oracle hash admission when the bundle declares one; effective risk only escalates; retry needs the exact set {ENVIRONMENT_TRANSIENT}; UNKNOWN outranks REPAIR; diff-derived production tier is structurally unable to reach ACCEPTED; coverage subset becomes UNKNOWN; missing required gate never fills from other PASSes; missing tier map fails closed to ESCALATE_HUMAN.

Not implemented or stubbed: failure admission predicates for C3, C5, C0 and C1 (B1, B4 above); ticket-based uniqueness per (execution, sha, bundle, gate, round) (B2); "no active failure" over all admissible reports (B3); D.2 guard order for C1 and C2 (B4, I8); identity triple; impact rules and diff-driven invalidation; baseline attestation.

## 6. Regression evidence

Independent rerun, same machine, Python 3.14.7, scratch clones with identical 124-character paths.

| SHA | passed | failed |
|---|---|---|
| 109be49 HEAD | 2868 | 2 |
| 047b218 base | 2841 | 2 |

Failure sets are identical: `test_gate_pass_reaches_launch_task` and `test_main_missing_token_env_returns_nonzero`, same assertion text at both SHAs. The pass delta is exactly the 27 new tests. The writer-audit failure did not reproduce at HEAD in a clean committed tree, which confirms the implementer's timing explanation. Their claim of environment equivalence stands, but they had not measured the base themselves; this run supplies it.

## 7. Architecture drift and Phase B-2

No drift: five new files only, no import from existing code, no persistence, no LoopRun, no touch of acceptance_gate, execution lifecycle, or production-fix-protocol.

**Phase B-2 may not start.** Minimal repair scope, in order, no code from me:

1. Stop reading `FailureObservation.category` (or delete it); add a test that category=ENVIRONMENT_TRANSIENT on a non-allowlisted signature cannot yield RETRY_SAME_CANDIDATE.
2. Duplicate (gate_id, round) among admissible reports → that gate becomes UNKNOWN or both reports are invalidated; add a permutation test proving order independence.
3. Classify failures over every admissible latest-per-gate report; required gates decide coverage only.
4. Add a `PRODUCTION_SCOPE_VIOLATION in admitted → ROUTE_TO_PFP` guard right after GOVERNANCE_CONFLICT and stop the fallback branch from adding UNKNOWN when admitted is non-empty.
5. Treat executor_identity None as inadmissible or UNKNOWN.
6. Fix test 05 so its V1 report differs from the execution only by SHA.

Path normalisation, monotone tier map, baseline-aware regression, the identity triple, and lineage cross-binding should be scheduled for B-2 rather than folded into this delta. The Phase A v3 text should also be placed on Drive so the next reviewer does not have to recover it from a transcript.