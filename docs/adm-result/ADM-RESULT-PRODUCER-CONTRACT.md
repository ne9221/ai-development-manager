# ADM Result Producer Contract (Slice A)

**Status:** SLICE_A_IMPLEMENTED — PENDING FRESH INDEPENDENT REVIEW  
**Schema:** `adm-result/v1` (`schema/adm_result.schema.json`)  
**Producer:** `manager.adm_result` v1.0.0  
**Authority (wired later):** `manager.execution_lifecycle.terminalize_execution`  
**PFP identity (pinned, not modified):** tag `pfp-v2.0.9-reviewed` · commit `b9a7afbba6242dabe7c729b8d2e96c9da51d20a4`

This document is the human-readable producer contract for Slice A. Slice A
defines and proves the producer only: closed schema, canonical identity,
validation, and create-only persistence against an injected record-store
boundary. It does **not** wire `terminalize_execution`, Drive `ADM-RESULTS`,
or the NextPlan consumer (Slice B).

## 1. What an adm-result is

One immutable, structured record of execution/review **truth** for one
**terminal** execution attempt. It answers: what ran, under which identity,
against which candidate, with which tests/review evidence, and with which
digest-bound agent output.

It does **not** answer acceptance or planning. Those are derived later by
the NextPlan consumer from the facts held here.

## 2. Authority boundary (hard refuse)

The following planner/acceptance fields are refused at every object level by
`FORBIDDEN_FIELDS` and by the closed schema:

- `accepted`
- `acceptance_state`
- `mark_complete`
- `planner_action`
- `next_action`

`execution.status` records process termination only. A completed worker may
still need repair; a failed reviewer execution is not a REJECT.

## 3. Terminal execution only

Producible statuses (exactly):

- `completed`
- `failed`
- `interrupted`

Non-producible (producer refuses):

- `reserved`
- `running`
- `cancelled`

Unknown statuses are also refused.

## 4. Identity: length-prefixed `result_id`

```
result_id = "ar-" + sha256(
    lp(project_id) || lp(task_id) || lp(execution_id) || lp(str(retry_count)) || lp(role)
)
```

where `lp(s)` is the 8-byte big-endian UTF-8 length of `s` followed by those
bytes (`length_prefixed`). No delimiter joining: `("a/b","c")` and
`("a","b/c")` cannot collide.

`retry_count` **participates** because the Command Watcher reuses an
`execution_id` across retries. Omitting it is a contract break (see M2).

## 5. Digest rules

```
result_digest = sha256(canonical_bytes(result minus result_digest))
```

- The only excluded field is `result_digest` itself (`DIGEST_EXCLUDED_FIELDS`).
- `canonical_bytes` is pinned: `json.dumps(sort_keys=True, separators=(",", ":"),
  ensure_ascii=False, allow_nan=False)` encoded UTF-8; floats and non-string
  keys are refused so equal semantics cannot serialize differently.
- Tampering after digest is a validation failure.

## 6. Store: create-only idempotency

`persist_adm_result(store, result)` against an injected `RecordStore`:

| situation | outcome |
|-----------|---------|
| new `result_id` | create; readback must match payload digest |
| same `result_id` + same digest | idempotent replay (`created=False`, `idempotent=True`) |
| same `result_id` + different digest | `AdmResultConflict` — nothing overwritten |
| readback bytes hash ≠ written payload | `AdmResultIntegrityError` |

There is no update API and no mutable "latest" record. Future canonical
location (Slice B): Drive `ADM-RESULTS/<project_id>/<result_id>.json`.

## 7. Candidate SHA protection

When the execution carries verified repo-write evidence, a supplied candidate
must equal the derived evidence candidate exactly. Otherwise
`AdmResultConflict` (stale/mixup refuse). Candidate A never authorizes
candidate B.

## 8. Review contract

- Verdicts are **only** `PASS` or `REJECT` (plus `null` on absent/invalid
  authority). Enumerated in schema and enforced via
  `manager.nextplan.contracts.review_problems` /
  `review_authority`.
- `BLOCKED_ENVIRONMENT` is **not** a verdict: it is a failed reviewer
  execution with a failure classification.
- Authority is recomputable; edited authority blocks fail validation.
- PFP-required reviews need a fresh reviewer session on a different provider
  (pinned identity above; this module never runs or modifies PFP).

## 9. Schema closure

The JSON Schema is closed at every object node (`additionalProperties: false`).
An unknown key at any path is a validation failure, never silently kept.

## 10. Triggers and lineage

Triggers: `initial`, `retry`, `repair`, `continuation`, `review`.  
Each trigger implies an exact link set; mixed or vague "rerun" lineage is
rejected. Retry requires `retry_count > 0`.

## 11. Out of scope for Slice A

- Wiring into `terminalize_execution`
- Drive `ADM-RESULTS` persistence
- NextPlan consumer / acceptance derivation
- Any mutation of PFP skill, manifest, or protocol

## 12. Proof surface

Targeted suite: `manager/test_adm_result.py` (88 tests, 842 subtests).  
Mutation controls M1–M8 and measured evidence:
`docs/adm-result/SLICE-A-EVIDENCE.md`.
