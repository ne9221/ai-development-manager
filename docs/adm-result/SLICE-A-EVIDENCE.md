# Slice A Evidence — ADM Result Producer Contract

**Status: SLICE_A_IMPLEMENTED — PENDING FRESH INDEPENDENT REVIEW**

Do not merge / activate / release until a fresh independent review passes.

## 1. Governance and provenance

| item | value |
|------|-------|
| AI-DEVELOPMENT-RULES | **v0.6.0** (Drive SSOT; repo markdown copy may lag) |
| PROJECT-RULES — ADM | **v1.1.0** |
| Handoff | Claude started Slice A; **Grok Bot / AI Development continued** after Claude quota exhaustion |
| PFP identity | tag `pfp-v2.0.9-reviewed` · commit `b9a7afbba6242dabe7c729b8d2e96c9da51d20a4` — **not modified** |
| Base SHA | `b86773d8aedb1877127114637d2354ec2dc161f9` (NextPlan Round 8: one decision-field grammar, one source) |
| Branch | `feat/adm-result-producer-contract-20260920` |
| HEAD (pre-commit) | `b86773d8…` + **untracked** Slice A files only |
| Measurement worktree | Linux mirror `/workspace/adm-slice-a-wt` matching Windows `C:\Users\neen9.BOBO\dev\adm-slice-a-20260920` (same branch/HEAD/untracked set). Windows `machineId` Shell was not available to this executor; BASE proof used detached worktree `/workspace/adm-slice-a-base-b86773d8`. |

## 2. Five allowed paths (only)

1. `schema/adm_result.schema.json`
2. `manager/adm_result.py`
3. `manager/test_adm_result.py`
4. `docs/adm-result/ADM-RESULT-PRODUCER-CONTRACT.md`
5. `docs/adm-result/SLICE-A-EVIDENCE.md`

Local scratch `_evidence/` is untracked measurement only — **do not git-add**.

## 3. BASE-red (capability absence)

At base SHA `b86773d8` (read-only worktree):

| check | result |
|-------|--------|
| `schema/adm_result.schema.json` | **ABSENT** |
| `manager/adm_result.py` | **ABSENT** |
| `manager/test_adm_result.py` | **ABSENT** |
| `python -c "from manager import adm_result"` | `ImportError: cannot import name 'adm_result' from 'manager'` |
| Disposable external shim | Expected symbols (`produce_adm_result`, `persist_adm_result`, `validate_adm_result`, `result_id_for`, `result_digest_for`, `FORBIDDEN_FIELDS`, `TERMINAL_STATUSES`, `AdmResultConflict`, `AdmResultIntegrityError`, `SCHEMA_VERSION`) all **Missing** — no BASE files edited |
| `pytest manager/test_adm_result.py --collect-only` | cannot collect — module/file absent |

Invariant-level absence: **producer module, schema, and store API** are not present at BASE. This is capability absence, not syntax damage.

Artifacts: `_evidence/base-red-import.txt`, `_evidence/base-red-absence.txt`, `_evidence/base-red-shim.txt`, `_evidence/base-red-collect.txt`.

## 4. HEAD-green reconfirm

```
python -m pytest manager/test_adm_result.py -q
→ 88 passed, 842 subtests passed
```

Reconfirmed after every mutation restore (byte-identical to pristine backups) and again after docs were written.

Artifact: `_evidence/head-green.txt`, `_evidence/final-green.txt`.

## 5. M1–M8 mutation controls

Baseline backups (sha256): `_evidence/backups/sha256-pristine.txt`.  
Each mutation: edit implementation/schema only → targeted suite RED with **named behavioral assertion failures** (not import/syntax-only) → restore byte-identical → GREEN 88/842.

| ID | Attack (invariant) | Named RED tests |
|----|--------------------|-----------------|
| **M1** | Remove nested schema closure (nested additionalProperties→true; top-level stays false) → unknown nested-field | `manager/test_adm_result.py::GroupSchemaClosure::test_2_every_object_node_in_the_schema_is_closed`<br>`manager/test_adm_result.py::GroupSchemaClosure::test_2_nested_unknown_field_rejected_at_every_object_path` |
| **M2** | Remove retry_count from result_id_for length-prefixed tuple → identity/retry result-id | `manager/test_adm_result.py::GroupIdentityAndDigest::test_6_every_tuple_component_changes_the_id`<br>`manager/test_adm_result.py::GroupIdentityAndDigest::test_6_result_id_is_length_prefixed_and_pinned`<br>`manager/test_adm_result.py::GroupIdentityAndDigest::test_7_retry_count_participates_in_result_id`<br>`manager/test_adm_result.py::GroupIdentityAndDigest::test_result_id_tampering_is_detected`<br>`manager/test_adm_result.py::GroupLineage::test_18_legal_review_lineage` |
| **M3** | Permit same result_id with changed digest in _reconcile → conflict/idempotency | `manager/test_adm_result.py::GroupPersistence::test_21_same_result_id_different_payload_rejected`<br>`manager/test_adm_result.py::GroupPersistence::test_22_same_identity_different_candidate_rejected`<br>`manager/test_adm_result.py::GroupPersistence::test_partial_creation_converges_or_conflicts` |
| **M4** | Disable _candidate_block supplied≠derived conflict → stale/mixup<br>*Note: Closest equivalent attacking candidate SHA identity protection. test_22 is persist-digest conflict (M3 territory): a different evidence-derived candidate changes digest under the same identity tuple.* | `manager/test_adm_result.py::GroupBoundaries::test_37_stale_candidate_rejected` |
| **M5** | Expand TERMINAL_STATUSES + schema status enum; empty NON_PRODUCIBLE_STATUSES → terminal-state | `manager/test_adm_result.py::GroupTerminalGate::test_12_non_terminal_execution_rejected`<br>`manager/test_adm_result.py::GroupTerminalGate::test_13_cancelled_execution_rejected`<br>`manager/test_adm_result.py::GroupTerminalGate::test_terminal_states_all_produce` |
| **M6** | Skip review decision shape raise; ignore authority recompute; open schema authority.verdict → review contract | `manager/test_adm_result.py::GroupReview::test_34_unknown_review_verdict_rejected`<br>`manager/test_adm_result.py::GroupReview::test_authority_is_recomputable_and_not_editable` |
| **M7** | Ignore readback digest mismatch in persist_adm_result → store integrity | `manager/test_adm_result.py::GroupPersistence::test_23_readback_digest_mismatch_rejected` |
| **M8** | Empty FORBIDDEN_FIELDS (surgical) → authority-boundary | `manager/test_adm_result.py::GroupBoundaries::test_41_no_acceptance_state_or_next_action_anywhere`<br>`manager/test_adm_result.py::GroupBoundaries::test_41_observed_value_cannot_carry_planner_semantics` |

Full node IDs and pytest logs: `_evidence/mutations/M{n}-failed-names.txt`, `M{n}-pytest.txt`, `M{n}-restore-green.txt`, `_evidence/mutations/SUMMARY.md`.

## 6. Regression failure-set

| suite | result | failing test names |
|-------|--------|--------------------|
| `manager/test_adm_result.py` | **88 passed, 842 subtests** | ∅ |
| `manager/test_nextplan*.py` (all Round-8 nextplan suites) | **450 passed, 2826 subtests** | ∅ |
| Related normalized-result / schema validation (collect `-k`) | related `test_adm_result` boundary cases collected; unrelated collect errors only for optional deps `streamlit`/`mcp` | ∅ for adm-result invariants |

Conceptual BASE comparison: at BASE, `test_adm_result` cannot collect (module absent). Round-8 nextplan suites stay green at HEAD with Slice A files present but unwired.

Artifacts: `_evidence/regression/test_adm_result.txt`, `test_nextplan.txt`.

## 7. Residuals

1. **Not committed / not pushed** — parent must commit the five allowed paths.
2. **Windows machineId Shell unavailable** to this executor; measurements taken on the matching Linux worktree mirror (same git identity and untracked set). Re-run M1–M8 on Windows if the parent requires same-machine attestation.
3. **Slice B unwired** — no `terminalize_execution` call, no Drive `ADM-RESULTS`, no NextPlan consumer.
4. **PFP untouched** — identity pinned only; no skill/manifest/protocol edits.
5. `_evidence/` scratch must stay untracked.

## 8. Final status

**SLICE_A_IMPLEMENTED — PENDING FRESH INDEPENDENT REVIEW**
