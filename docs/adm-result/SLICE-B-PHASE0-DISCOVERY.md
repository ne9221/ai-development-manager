# Slice B Phase 0 — Read-Only Change-Point Discovery

**Status:** PHASE_0_COMPLETE — discovery only; no implementation edits  
**Base / HEAD verified:** `c0ac422c3dfee7f69b5a62820b091a5a3f662293`  
**Branch:** `feat/adm-result-live-wiring-20260920`  
**Authority (Fable):** `ADM_RESULT_AUTHORITY = manager.execution_lifecycle.terminalize_execution`  
backed by `manager.task_root.commit_terminal_bind`  
**Measured at:** Asia/Taipei (box-local) 2026-09-20  
**Worktrees:** Linux `/workspace/adm-slice-b-wt` (primary); Windows
`C:\Users\neen9.BOBO\dev\adm-slice-b-20260920` machineId `47ec0695-1304-44db-b46f-15d514ec1df2`
(not mounted on this box — Windows mirror of this file is a residual for coordinator)

**Frozen Slice-A five (must NOT be planned for edit):**

1. `schema/adm_result.schema.json`
2. `manager/adm_result.py`
3. `manager/test_adm_result.py`
4. `docs/adm-result/ADM-RESULT-PRODUCER-CONTRACT.md`
5. `docs/adm-result/SLICE-A-EVIDENCE.md`

---

## 1. Call-path map (exact)

Conceptual Slice-B path (Fable):  
dispatch → reserve/start → provider execution → ADM validation / evidence →
`terminalize_execution` → produce+persist adm-result → `commit_terminal_bind`
(same epoch) → Handoff/Task materialization → NextPlan adapter.

### 1.1 Current live path (BASE `c0ac422c`)

```
Command / CLI / scheduler ingress
  └─ manager/command_watcher.py::process_command (or execution_runner.main)
       └─ manager/execution_runner.py::launch_task
            ├─ manager/dispatcher.py::dispatch          (provider selection)
            ├─ manager/executions.py::reserve_execution (status=reserved)
            └─ manager/execution_runner.py::run_execution
                 ├─ manager/execution_lifecycle.py::enter_running_gate
                 │    └─ task_root.acquire_task_root / writer lease
                 │       (reserved → running; claim epoch opens)
                 ├─ launcher.prepare / start / wait     (CodexLauncher / ClaudeLauncher)
                 ├─ [bounded repo-write only]
                 │    manager/repo_write_enforcement.py
                 │      enforce_allowed_paths
                 │      commit_and_push_repo_write_changes
                 │      capture_repo_write_evidence  ← legacy validation_command
                 │                                    via _run_validation_command
                 │                                    (exit-code only; NOT
                 │                                     nextplan.runner.run_validation
                 │                                     / ExecutionRegistry)
                 │      executions.record_repo_write_evidence
                 └─ manager/execution_lifecycle.py::terminalize_execution   ★ AUTHORITY
                      ├─ executions.persist_terminal     (execution → completed|failed|interrupted)
                      ├─ task_root.commit_terminal_bind  (GCS CAS epoch bind)
                      ├─ _materialize_terminal_handoff   (Drive HANDOFFS, fixed-file-id)
                      ├─ store.put("tasks", ...)         (Task projection)
                      └─ cleanup_execution / release claim
```

Alternate terminal entry (recovery / watcher):  
`command_watcher.py` also calls `terminalize_execution` directly and
`retry_incomplete_terminal_persistence` for incomplete persistence.

Retry (same execution_id, increment retry_count):  
`executions.prepare_task_retry` ← failed/interrupted (or prelaunch-cancelled);
no `repair_of_execution_id` path exists on Execution today.

### 1.2 Key functions (file → symbol)

| Stage | File | Function |
|-------|------|----------|
| Reserve | `manager/executions.py` | `reserve_execution` |
| Start gate | `manager/execution_lifecycle.py` | `enter_running_gate` |
| Launch orchestration | `manager/execution_runner.py` | `launch_task`, `run_execution` |
| Repo-write evidence | `manager/repo_write_enforcement.py` | `capture_repo_write_evidence`, `_run_validation_command` |
| Persist evidence on Execution | `manager/executions.py` | `record_repo_write_evidence` |
| Terminal authority | `manager/execution_lifecycle.py` | `terminalize_execution` |
| Persist execution terminal | `manager/executions.py` | `persist_terminal` |
| GCS bind | `manager/task_root.py` | `commit_terminal_bind`, `terminal_proposal`, `proposal_hash` |
| Handoff materialize | `manager/execution_lifecycle.py` | `_materialize_terminal_handoff`, `_resolve_handoff_drive_id_factory` |
| Handoff create | `manager/tasks.py` | `create_handoff` (+ `DriveRecords.put_with_fixed_file_id`) |
| Slice-A producer (UNWIRED) | `manager/adm_result.py` | `produce_adm_result`, `persist_adm_result`, `RecordStore` |
| Validation runner (UNWIRED to live path) | `manager/nextplan/runner.py` | `run_validation`, `ExecutionRegistry`, `pytest_argv` |
| NextPlan planner (UNWIRED consumer) | `manager/nextplan/pipeline.py`, `planner.py` | `process` / `plan` / `apply` |
| Review contracts (fixture-level) | `manager/nextplan/contracts.py`, `classify.py` | `review_authority`, `review_expectation` |

### 1.3 Insertion point for Slice B (Fable semantics — do not redesign)

Inside `terminalize_execution`, **after** `persist_terminal` succeeds and
ADM-owned evidence is attached, **before** Handoff/Task materialization:

1. produce worker/reviewer `adm-result` via frozen `produce_adm_result`
2. persist create-only to Drive `ADM-RESULTS/...` via a Drive `RecordStore`
3. attach `adm_result_ref` on the Execution
4. extend the same-epoch `commit_terminal_bind` with `{result_id, result_digest, drive_file_id}`
5. only then materialize Handoff/Task

No second authority. No Loop supervisor.

---

## 2. Schema map

### 2.1 Execution — `schema/execution.schema.json`

- **Closedness:** top-level `"additionalProperties": false` (closed). Nested
  objects that declare `additionalProperties` are also false; some nested
  nodes leave it unset (legacy).
- **Present lineage today:** `retry_count`, `retry_of_execution_id` only.
- **ABSENT (Slice-B required):**
  - `adm_result_ref` `{ result_id, result_digest, drive_file_id }`
  - `validation_results` / structured validation evidence binding
  - `repair_of_execution_id`, `continues_execution_id`, `reviews_execution_id`
  - `reviewer_run_id` / review-dispatch pre-issue fields on Execution
  - `agent_output` `{ ref, sha256 }`
- **Top-level keys at BASE:**  
  `access`, `account_id`, `cleanup_evidence`, `completed_at`, `effort`,
  `elapsed_minutes`, `execution_id`, `finished_at`, `hard_timeout_at`,
  `heartbeat_at`, `last_provider_event`, `lease_evidence`, `mode`, `notes`,
  `progress_updated_at`, `project_id`, `provider`, `provider_evidence`,
  `provider_session_id`, `quota_*`, `recovery_reason`, `repo_write_evidence`,
  `reserved_at`, `retry_count`, `retry_of_execution_id`, `session_id`,
  `source_confidence`, `stale_at`, `started_at`, `status`, `task_id`,
  `task_snapshot`, `terminal_reason`.

### 2.2 Task — `schema/task.schema.json`

- **Closedness:** `"additionalProperties": false`.
- Relevant existing fields: `validation_command`, `allow_no_change_success`,
  `baseline_head`, `allowed_paths`, `branch`, `working_directory`,
  `source_context` (carries retry linkage today).
- No adm-result pointer on Task (Fable: pointer lives on terminal bind +
  Execution.adm_result_ref).

### 2.3 Task-root terminal bind — **not a JSON Schema file**

Documented/implemented in `manager/task_root.py`:

- Object location: GCS `task-claims/<project_id>/<task_id>.json`
- `SCHEMA_VERSION = "1.0.0"`, `CANONICALIZATION_VERSION = "1"`
- Proposal fields (`_PROPOSAL_FIELDS`): project/task/execution identity,
  retry_count, epoch, terminal_status, provider_outcome, terminal_reason,
  completed_at, session/provider/account identity, schema + canon versions.
- Bind immutable fields (`_BIND_IMMUTABLE_FIELDS`): proposal +
  `proposal_hash`, `terminal_fence_epoch`, `task_projection_drive_id`,
  `handoff_drive_file_id`, expected projection digests.
- **ABSENT:** `result_id`, `result_digest`, `drive_file_id` (adm-result
  pointer) on bind / proposal.

### 2.4 Adm-result (FROZEN Slice A) — `schema/adm_result.schema.json`

- Closed at every object node (`additionalProperties: false`).
- Producer/store API frozen in `manager/adm_result.py`:
  `produce_adm_result`, `persist_adm_result`, abstract `RecordStore`.
- Persistence boundary explicitly deferred: Drive
  `ADM-RESULTS/<project_id>/<result_id>.json` is Slice B.

### 2.5 Related schemas (read-only reuse)

| Schema | Path | Role |
|--------|------|------|
| Validation result | `schema/adm_validation_result.schema.json` | Bound runner evidence shape |
| Review result (legacy/related) | `schema/adm_review_result.schema.json` | Present; Slice-A review block uses NextPlan contracts |
| Failure Atlas | `schema/failure_atlas.schema.json` + `manager/nextplan/failure_atlas.json` | Policy table |
| Handoff | `schema/handoff.schema.json` | Terminal projection |

---

## 3. Subsystem map (validation / review / NextPlan / Drive / Atlas)

### 3.1 `run_validation` + registry

- **Module:** `manager/nextplan/runner.py`
- **API:** `run_validation(argv, cwd, runner="pytest", ..., registry=...)`,
  `ExecutionRegistry`, `pytest_argv`
- **Contract:** counts from JUnit XML + registry; never from stdout prose;
  execution_id minted before spawn.
- **Live wiring:** **NONE.** Docstring states deliberately not wired into
  `repo_write_enforcement`. Live path uses
  `repo_write_enforcement._run_validation_command` (shell string,
  exit-code → `tests_status` only) — insufficient for adm-result
  `validation_runs` / registry binding.

### 3.2 Provider / agent output capture

- `execution_runner.run_execution` docstring: *"Full prompt, transcript,
  stderr, and raw provider failure text are not persisted by this runner."*
- Partial: Claude may keep a local stdout file; Codex captures status only
  (Fable §17 PARTIAL).
- **Gap:** no durable `agent_output.ref` + `agent_output.sha256` on
  Execution / produced into `produce_adm_result`.

### 3.3 Reviewer dispatch / `reviewer_run_id` / expectation

- **Contracts exist** (NextPlan Round 5+):  
  `nextplan.harness.review_dispatch`, `classify.review_expectation`,
  `contracts.review_authority` — require ADM-issued
  `{reviewer_run_id, provider, job_id}` before authority.
- **Slice-A producer** enforces review.dispatch / expectation binding.
- **Live path:** grep of `execution_runner.py`, `dispatcher.py`,
  `command_watcher.py`, `execution_lifecycle.py`, `executions.py` →
  **zero** `reviewer_run_id` / `review_dispatch` / pre-issue.  
  Reviewer launch does not pre-issue run id onto Execution before spawn.

### 3.4 Retry vs repair

- **Retry:** `executions.prepare_task_retry` — same execution_id, increment
  `retry_count`, `retry_of_execution_id`; max via `MAX_RETRY_COUNT`.
- **Repair:** lineage fields exist only inside frozen adm-result schema;
  Execution schema has **no** `repair_of_execution_id`. New repair Execution
  + candidate-diff rules are Slice B.

### 3.5 NextPlan event / state input

- Pipeline envelope keys: `event_id`, `task_id`, `role`, `session_id`,
  `agent`, `generation`, `format`, `content` (`pipeline.py`).
- Planner: pure `plan(state, event)` / `apply`; MARK_COMPLETE paths exist
  but consume **extracted/normalized** events, not Drive adm-result.
- **No** `from manager.adm_result` import outside `adm_result.py` itself
  and its unit tests.
- **No** adapter `adm-result → pipeline event` (`event_id = result_id`).

### 3.6 Failure Atlas

- `manager/nextplan/failure_atlas.json` — 53 entries.
- Slice-B required codes **ABSENT:**  
  `adm_result_missing`, `adm_result_malformed`,
  `adm_result_identity_mismatch`, `pfp_deferred`.

### 3.7 DriveRecords fixed-file-id / create / readback

- **Class:** `manager/tasks.py::DriveRecords`
- **Areas today (`ROOT_FOLDERS`):** TASKS, HANDOFFS, TASK-HISTORY, PROJECTS,
  EXECUTIONS, SESSIONS, SESSION-REVIEWS, OVERVIEWS, WORKTREE-LOCKS, COMMANDS.
- **No `ADM-RESULTS` area.**
- **Fixed-ID path:** `put_with_fixed_file_id(area, project_id, name, document, drive_file_id)`
  — create with explicit Drive file id; on conflict, readback; same bytes →
  idempotent; different bytes → `TaskError` conflict; post-create media
  readback must match.
- **Readback helpers:** `get_by_file_id` / `get_record_by_file_id`,
  `get_with_token` (sha256).
- **Slice-A store:** abstract `RecordStore.read/create` +
  `persist_adm_result` (canonical bytes, digest conflict, readback hash).
  Slice B implements `RecordStore` over DriveRecords + new ROOT folder
  `ADM-RESULTS` (create-only; prefer fixed-file-id semantics aligned with
  handoff path).

---

## 4. BASE unwired gap — mechanical proof (`c0ac422c`)

Executed on `/workspace/adm-slice-b-wt` at HEAD = frozen SHA:

| # | Check | Command / observation | Result |
|---|--------|----------------------|--------|
| G1 | No adm-result production in terminalize | `grep adm_result\|produce_adm\|persist_adm\|ADM-RESULTS\|adm_result_ref manager/execution_lifecycle.py` | **NONE** |
| G2 | No `adm_result_ref` (and siblings) on Execution schema | `grep` + `json.load` of `schema/execution.schema.json` | **ABSENT** (`adm_result_ref`, `validation_results`, `reviewer_run_id`, repair/continues/reviews lineage) |
| G3 | No live `ADM-RESULTS` area | `ROOT_FOLDERS` in `tasks.py`; grep `ADM-RESULTS` outside Slice-A comments | **ABSENT** from ROOT_FOLDERS; only Slice-A deferred comments |
| G4 | No NextPlan ↔ adm-result adapter | `grep from manager.adm_result\|produce_adm_result\|load_adm_result` under `manager/` excluding `test_adm_result` | **Only** definitions inside `adm_result.py` — no consumer |
| G5 | No review pre-issue on live path | `grep reviewer_run_id\|review_dispatch` in runner/dispatcher/watcher/lifecycle/executions | **NONE** |
| G6 | Terminal bind has no result pointer | `grep result_id\|result_digest\|adm_result manager/task_root.py` | **NONE** |
| G7 | Atlas codes missing | load `failure_atlas.json` | four Slice-B codes **ABSENT** |
| G8 | Slice-A producer exists but unwired | `adm_result.py` present; contract docs state Slice B owns wiring | **Capability present, lifecycle unused** |

This is **capability unwiredness**, not missing Slice-A bytes. Slice-A suites
remain the producer proof surface; Slice B must not reopen them.

---

## 5. Gaps vs Slice B requirements

| Requirement | BASE state | Gap |
|-------------|------------|-----|
| Wire producer into `terminalize_execution` | Producer exists; terminalize ignores it | Insert produce+persist before materialize |
| Execution schema fields | Closed; missing refs/lineage/validation/review/output | Additive closed fields + tests |
| Validation evidence before adm-result | Legacy `_run_validation_command` only | Wire `nextplan.runner.run_validation` + registry into evidence; never trust provider prose counts |
| Agent output capture | Not persisted by runner | Persist ref+sha256; feed producer |
| Review pre-issue | Contracts only in NextPlan/fixtures | Pre-issue reviewer_run_id/provider/job_id/target SHA/reviewed execution **before** reviewer launch; fail closed |
| Drive ADM-RESULTS | Abstract RecordStore only | Implement Drive store; add ROOT folder; create-only + readback |
| Terminal bind epoch pointer | Bind lacks result fields | Extend bind (and proposal canon if required) with result_id/digest/drive_file_id **same epoch** |
| NextPlan adapter | Planner pure; no adm-result input | Deterministic adapter; `event_id=result_id`; independent verify |
| Atlas codes | 53 entries; 4 missing | Add bounded four codes + routing tests |
| MARK_COMPLETE invariant | Exists in planner but not fed by live adm-results | Adapter + wiring must preserve Round-8 review authority |
| Retry vs repair | Retry only | Add repair lineage on Execution + tests |
| PFP / human gate | Planner/atlas partial; no `pfp_deferred` | Atlas + task marker / evidence path; no PFP code rewrite |
| Real non-prod E2E | None for adm-result live path | One controlled disposable E2E |
| finish_execution legacy | Still maps failures → blocked task | Supersede via lifecycle+NextPlan; do not invent second authority |

---

## 6. STOP conditions check

| STOP if… | Assessment |
|----------|------------|
| Must change frozen Slice-A five files | **NO** — producer/schema/tests/docs sufficient; wiring is callers + Drive adapter + schema *Execution/task-root* additions |
| Must redesign terminal authority | **NO** — insert inside existing `terminalize_execution` / `commit_terminal_bind` |
| Must modify PFP | **NO** — pin/consume only; `pfp_deferred` atlas routing |
| Unrelated infrastructure / production activation | **NO** — non-prod E2E; no merge/activate |
| Materially broader than Fable | **NO** — Fable milestone B list matches this change set |

**STOP verdict: CLEAR TO IMPLEMENT (Phase 1+).**  
If implementation discovers a Slice-A contract defect requiring frozen-byte
change → halt and report `SLICE_A_CONTRACT_REOPEN_REQUIRED` (do not silently
amend).

---

## 7. PREDICTED CHANGE SET (exact paths + why)

### 7.1 Production / schema (expected edits)

| Path | Why |
|------|-----|
| `schema/execution.schema.json` | Add closed fields: `adm_result_ref`, validation evidence binding, lineage (`repair_of_execution_id`, `continues_execution_id`, `reviews_execution_id`), review dispatch / `reviewer_run_id`, `agent_output` |
| `manager/execution_lifecycle.py` | ★ Wire produce → Drive persist → attach ref → extend bind inputs **after** `persist_terminal`, **before** Handoff/Task materialize; fail closed |
| `manager/task_root.py` | Extend terminal bind (+ proposal field set / hash if pointer is bind-authoritative) with `result_id`, `result_digest`, `drive_file_id`; stale/conflict semantics |
| `manager/executions.py` | Helpers to attach validation/agent_output/adm_result_ref; repair reservation helpers; keep `persist_terminal` as outcome-only |
| `manager/execution_runner.py` | Capture agent_output; invoke bound `run_validation` / registry for evidence before terminalize; do not trust provider prose counts |
| `manager/repo_write_enforcement.py` | Bridge or replace legacy `_run_validation_command` counts path with registry-bound validation where Slice B requires structured runs (minimal; keep path enforcement) |
| `manager/tasks.py` | Add `ADM-RESULTS` to `ROOT_FOLDERS`; Drive `RecordStore` implementation (create-only / fixed-id / readback) used by `persist_adm_result` |
| `manager/command_watcher.py` | Reviewer launch: pre-issue `reviewer_run_id` / provider / job_id / target candidate / reviews_execution_id before spawn; fail closed on mismatch |
| `manager/dispatcher.py` and/or review-command path | Ensure review Commands carry pre-issued dispatch record (minimal touch) |
| `manager/nextplan/failure_atlas.json` | Add `adm_result_missing`, `adm_result_malformed`, `adm_result_identity_mismatch`, `pfp_deferred` |
| `manager/nextplan/atlas.py` | Load/validate new codes if registry is code-gated |
| **NEW** `manager/nextplan/adm_result_adapter.py` (or equivalent under `manager/`) | Deterministic adm-result → pipeline event; independent verify schema/digest/task/execution/candidate/repo/review/tests/PFP/terminal |
| `manager/nextplan/pipeline.py` / `planner.py` | Thin hooks only if required so adapter events hit existing branches without weakening Round-8 |
| `docs/DRIVE-STRUCTURE.md` | Document `ADM-RESULTS/` area |
| `docs/adm-result/SLICE-B-PHASE0-DISCOVERY.md` | This file |
| `docs/adm-result/SLICE-B-EVIDENCE.md` | Later (implementation evidence; not Phase 0) |

### 7.2 Tests (expected new / extended)

| Path | Why |
|------|-----|
| **NEW** `manager/test_adm_result_live_wiring.py` (name TBD) | Terminalize produces+persists; bind carries ref; fail closed paths |
| **NEW** `manager/test_adm_result_adapter.py` | Adapter verify / MARK_COMPLETE invariants / mismatch fail closed |
| `manager/test_execution_lifecycle.py` | Terminal path with adm-result epoch |
| `manager/test_task_root.py` | Bind fields + stale writer + idempotent same digest |
| `manager/test_executions.py` / `test_execution_runner.py` | Schema fields, validation evidence, agent_output |
| `manager/test_tasks.py` / `test_drive_records.py` | ADM-RESULTS create-only / conflict / readback |
| `manager/test_nextplan_atlas.py` (+ round fixtures as needed) | New atlas codes routing |
| **NEW** non-prod E2E module or script under `manager/` / `docs/` | One disposable happy-path MARK_COMPLETE (+ controlled failures) |
| Mutation harness notes under `_evidence/` (untracked) | M1–M12 measurement |

### 7.3 Explicitly NOT in change set

- Frozen Slice-A five files  
- PFP skill / manifest / protocol repo  
- Continuous Orchestration / Slice C  
- Verification Loop package reactivation  
- Dashboard / desktop / unrelated providers / quota redesign  
- Production activation / merge to main  

### 7.4 Frozen files — confirm

No predicted edit to:  
`schema/adm_result.schema.json`, `manager/adm_result.py`,
`manager/test_adm_result.py`,
`docs/adm-result/ADM-RESULT-PRODUCER-CONTRACT.md`,
`docs/adm-result/SLICE-A-EVIDENCE.md`.

---

## 8. Proposed test matrix outline

1. **Execution schema closure** — every new field + nested object closed;
   unknown keys rejected; required-when rules for terminal+ref.
2. **Terminalize wiring** — after persist_terminal, adm-result created;
   Drive readback; Execution.adm_result_ref set; bind carries same triple;
   Handoff/Task only after bind; idempotent replay; conflict retain authority.
3. **Drive ADM-RESULTS** — create-only; same id+digest idempotent; same
   id+different digest conflict; readback mismatch fail closed; no update API;
   no mutable "latest".
4. **Validation** — registry-bound runs; forged counts ignored; provider prose
   "N passed" never authoritative; evidence persisted before produce.
5. **Agent output** — ref+sha256 persisted; digest mismatch fail closed.
6. **Review pre-issue** — run_id/provider/job/target/execution issued before
   launch; wrong candidate/execution/run_id/job fail closed; same implementer
   session freshness; malformed/missing/conflicting review.
7. **Lineage** — retry vs repair mechanical distinction; repair new
   execution_id + repair_of; retry cannot overwrite predecessor result;
   stale writer cannot attach.
8. **NextPlan adapter** — event_id=result_id; independent verify of
   schema/digest/task/execution/candidate/repo/review/tests/PFP/terminal;
   no NL inference for structured states.
9. **MARK_COMPLETE invariant** — worker success alone / tests alone /
   reviewer prose / unbound PASS never complete; full W+R+validation+PFP
   path only.
10. **Atlas** — four new codes: classification, retry/repair/human behavior,
    non-vacuous routing.
11. **PFP / human gate** — missing/stale PFP cannot complete when required;
    PFP cannot rewrite candidate/execution/tests/review/adm-result;
    HUMAN_GATE only via authoritative human evidence.
12. **Real non-prod E2E** — one disposable path through real components.
13. **Regression** — Slice-A 88/842 green; Round-8 NextPlan suites green;
    lifecycle/task-root/schema/validation/review/DriveRecords failure-set
    compared BASE vs HEAD **by test name**.
14. **Mutations M1–M12** — see §9.

---

## 9. Proposed M1–M12 mutation targets

*(Coordinator task referenced “12 mutations listed in the user task” without
enumerating them in `/workspace/adm-slice-b-task-spec.md`. Proposed set below
is derived from Fable Slice-B load-bearing guards + requirements. Confirm
against full user task before execution; rename freely if the canonical list
differs.)*

| ID | Intended attack (break one guard) | Expected RED surface (named tests TBD at implement) |
|----|-----------------------------------|------------------------------------------------------|
| **M1** | Skip `produce_adm_result` / persist inside `terminalize_execution` (materialize without result) | Live-wiring tests: missing adm-result / bind pointer absent |
| **M2** | Persist adm-result **after** Handoff/Task materialize (order inversion) | Epoch/order tests; projection without result_id rejected |
| **M3** | Allow Drive overwrite / update path for ADM-RESULTS (break create-only) | DriveRecords/ADM-RESULTS conflict + idempotency tests |
| **M4** | Ignore readback digest mismatch on Drive persist | Integrity fail-closed tests |
| **M5** | Drop `adm_result_ref` / bind triple consistency check (pointer drift) | Bind↔Execution↔Drive identity mismatch tests |
| **M6** | Accept provider stdout test counts instead of registry-bound validation | Validation provenance tests (Round-5/6 class) |
| **M7** | Launch reviewer without pre-issued `reviewer_run_id` / dispatch | Review pre-issue + authority tests |
| **M8** | Accept review for wrong candidate SHA / wrong reviews_execution_id | Review binding fail-closed tests |
| **M9** | Treat retry as repair (or overwrite predecessor result_id on retry) | Lineage / result_id(+retry_count) identity tests |
| **M10** | Adapter MARK_COMPLETE from worker-only or unbound PASS / prose | Adapter + planner MARK_COMPLETE invariant tests |
| **M11** | Omit new atlas codes or route `adm_result_*` / `pfp_deferred` to COMPLETE | Atlas routing / non-vacuity tests |
| **M12** | Open Execution schema nested `additionalProperties` for new blocks | Schema closure tests on new fields |

Each mutation: edit **non-frozen** implementation only → targeted suite RED
with behavioral assertion failures → **byte-identical restore** → green.

---

## 10. Blockers / residuals (Phase 0)

1. **Windows dual-write:** `CopyFromBox` / Windows path
   `C:\Users\neen9.BOBO\dev\adm-slice-b-20260920\...` is **not available**
   from this Linux box (path not mounted; no CopyFromBox tool in MCP catalog).
   File written on Linux worktree only. Coordinator should mirror to Windows
   worktree machineId `47ec0695-1304-44db-b46f-15d514ec1df2` before push.
2. **M1–M12 canonical text:** full user-task mutation list not present in
   condensed `/workspace/adm-slice-b-task-spec.md`; §9 is a proposed set
   aligned to Fable — reconcile before mutation execution.
3. **No implementation yet** — Phase 0 only. Do not commit. Do not push.
4. **PFP provenance / quota evidence** — required before production-affecting
   edits (coordinator preflight noted as already verified; re-check at Phase 1).

---

## 11. Phase 0 conclusion

- HEAD verified at frozen Slice-A SHA on `feat/adm-result-live-wiring-20260920`.
- Call path, schemas, validation/review/NextPlan/Drive/Atlas mapped.
- BASE mechanically proven **unwired** for live adm-result.
- STOP conditions: **NO** Slice-A reopen, **NO** terminal redesign, **NO** PFP change.
- Predicted change set is minimal and Fable-aligned; authority remains
  `terminalize_execution` + `commit_terminal_bind`.

**NEXT:** Phase 1 implementation against the predicted change set only.
