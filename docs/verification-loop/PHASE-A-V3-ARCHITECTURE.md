我已完成全部必讀項的唯讀取得。先確認 grounding 事實（這些不是引述，是我剛量到的）：

| 項目 | 實測值 |
|---|---|
| Governance SSOT | Drive doc `1vSX5Bi…SYrU`，**v0.5.0**，modified 2026-09-01（含 §10 rules 46–47）。GitHub 副本未採信 |
| PROJECT-RULES — ADM | Drive doc `1NOZMd…GxfAQ`，**v1.1.0**，2026-08-21（rule 4/5/12/13/20/23/34 直接相關） |
| production canonical checkout | `…ai-development-manager-home-recovery-combined-20260822`，HEAD `047b218`，**working tree clean**；TESTED==ACTIVATED==RUNNING 三者皆 `047b218` |
| 既有 `acceptance-gate/` | 只有 `applied-log.jsonl`，tag `CONTROLLED_ACCEPTANCE_GATE` = provider 不可用時的 reroute，**與本設計無關** |
| CAS 先例 | `runtime/phase1-cursor.json` 有 `generation: 2783` + `.init-state{txid, state: committed}` 兩階段發布 |
| 既有 schema | Execution 已有 `retry_of_execution_id`、`provider_evidence{host,pid,creation_identity,scheduler_invocation_id}`、`lease_evidence{lock_id: repo-<sha256>, generation, baseline_head}`、`access: production_write`；Task 已有 `governance{rules_version, rules_digest, mandatory_rule_ids}`；Session 已有 `classification_status/confidence/method`（含 `conflicting_deterministic_signals`、`unclassified`） |

**這一條是本輪最重要的發現**：B1/B2/I10 要求的機制，ADM schema 裡**已經存在對應原語**，不需要發明新 truth system。我把 v3 全部綁到這些既有原語上。

未寫入任何檔案；依「唯讀」指示我也**沒有**寫 memory 記錄（可另行授權）。

---

# Phase A v3 — Verification Loop / Acceptance Layer 正式架構規格

**判定先行：`READY_FOR_PHASE_B`**（條件與殘留在最末節，請務必讀完再採信）

---

## 0. 命名與定位（I7 處置）

| 名稱 | 意義 | 衝突處理 |
|---|---|---|
| `verification-loop` | 本設計整體 | 新名 |
| `verification-controller` | 唯一能發 ticket、唯一能執行 derivation 的角色 | 新名 |
| `runtime/verification/` | 本設計的 store 根 | 新路徑 |
| ~~`acceptance-gate/`~~ | **既有**：provider 不可用 reroute | **禁止沿用**，禁止再產生 `CONTROLLED_ACCEPTANCE_GATE` tag |
| ~~UEVL~~ | 前稿未定義 | **本版刪除該詞**，不進 Phase B |

**正交性宣告（原則 6 的機械形式）**：Execution 既有 `status ∈ {reserved, running, completed, failed, interrupted, cancelled}` 是 **execution lifecycle**，本設計**完全不動它**。acceptance 是另一個維度，且**不是 Execution 上的欄位**（見 A/B1）。`status=completed ∧ acceptance=REJECTED_NEEDS_REPAIR` 是完全合法且預期常見的組合 —— 這正是「Executor says done ≠ ACCEPTED」的資料表現。

---

## A. Entity / Lineage Contract

### A.1 各實體擁有什麼 truth（不變更既有語意）

| Entity | 擁有的 truth | 本設計新增 | 明確**不**擁有 |
|---|---|---|---|
| **Task** | 意圖、scope、`allowed_paths`、`baseline_head`、`acceptance_criteria`、`governance{rules_version,rules_digest}`、close eligibility | `verification{}` 聚合欄位群（見 A.4） | 不擁有任何 gate 結果 |
| **Command** | 派工意圖與 idempotency key | 無 | 不參與 acceptance |
| **Execution** | 一次 executor 執行的事實：provider、identity、`repo_write_evidence`、`provider_evidence`、`lease_evidence`、terminal 原因 | `candidate_sha`、`base_sha`、`repair_of_execution_id`、`trigger`、`acceptance_bundle_ref` | **不擁有 `acceptance_state`**（不是可寫欄位） |
| **Session** | provider 端身分與 correlation：`provider/account_id/session_id/provider_session_id` + `classification_status/confidence/method` | 無 | 不擁有結果，只提供 **identity 解析真相** |
| **Handoff** | 交接敘事、`remote_sha`、`push_status`、`tests_status` | 無 | **`tests_status` 永遠是 input，不是 evidence** |

### A.2 acceptance 資料掛在哪（B1 的正式回答）

> **`ACCEPTED` 不持久化為欄位。它是對 VerificationReport 集合的 deterministic derivation，只做 materialized cache。**

理由不是美學，是 ADM 現行 truth model 的同構：`provenance/tested_sha.json` 與 `activated_sha.json` 是**寫一次的原語 attestation**；`runtime_evidence.json` 是**每 tick 重算的 materialization**；而閘門 `TESTED == ACTIVATED == RUNNING` **從來沒有被存成一個 boolean**，它每次被求值。Acceptance 同款。

```
持久化原語（append-only, immutable, content-addressed）
  runtime/verification/tickets/<ticket_id>.json         ← Controller 先發
  runtime/verification/reports/<report_digest>.json     ← checker 後交
  runtime/verification/human/<decision_id>.json         ← 人類寫入（I9）
  runtime/verification/bundles/<bundle_hash>/manifest.json

Materialized（可隨時丟棄重建，永不被信任為 truth）
  runtime/verification/view/<execution_id>.json
```

```
acceptance_state(execution) := DERIVE(
    admissible_reports(execution),        # 見 C.3 准入
    bundle(execution.acceptance_bundle_ref),
    execution.candidate_sha,
    effective_risk(execution)             # 見 E
)

derivation_key := sha256(
    DERIVE_ALGO_VERSION ‖ bundle_hash ‖ candidate_sha ‖
    sorted(report_digest for r in admissible_reports)
)
```

Cache 只有在**重算出的 `derivation_key` 與 cache 內記載者相等**時才可被讀取為快取；不等即忽略並重算。Cache 寫入用 CAS（expected prior `derivation_key`，沿用 phase1-cursor 的 generation/`.init-state{txid,state}` 兩階段發布形態），避免 TOCTOU。

**這一個決定同時關掉了三個攻擊面**：沒有 `acceptance_state` 欄位可寫（攻擊 1 的落點消失）；契約升版自動使舊結論失效（I3 STALE 免費，攻擊 9）；新證據出現可自動翻案（I3 REVOKED，攻擊 12）。

### A.3 acceptance 狀態集合（derived，非 stored）

| 狀態 | 推導條件 |
|---|---|
| `PENDING` | 尚無 admissible report |
| `IN_VERIFICATION` | 有 report，required 覆蓋未滿 |
| `ACCEPTED` | required gates 在**同一 final candidate_sha** 全部有 admissible PASS，且無 active failure |
| `ACCEPTED_WITH_RESIDUAL` | 同上，但存在 human-approved residual 記錄（I9）；**與 ACCEPTED 是不同狀態，不可互換顯示** |
| `REJECTED_NEEDS_REPAIR` | 有 admitted C3/C4/C5 |
| `BLOCKED_HUMAN` | 有 C7 UNKNOWN、或 budget exhausted |
| `DEFERRED_TO_PFP` | effective_risk = R5，或存在 `production_write` gate |
| `ROUND_INVALIDATED` | candidate ref 在輪次中位移（I6） |
| `STALE` | 曾 ACCEPTED，但 `bundle_hash` / `governance.rules_digest` / `checker_version` 改變 → derivation 無法在現行輸入下重現 |
| `REVOKED` | 同一 `(candidate_sha, bundle_hash)` 下出現 admissible 的反證 report 或 ReproductionArtifact |

**STALE ≠ REVOKED（I3 明確分離）**：STALE 是「輸入變了、結論失去依據」，**不蘊含缺陷**，自動計算，無需人；REVOKED 是「同樣輸入下出現矛盾證據」，**蘊含缺陷**，必須帶 `revocation_evidence`。兩者都令 `Task.close_eligible` 重算為 false，且都不改寫歷史（append-only）。

### A.4 Repair lineage（I2 正式裁定）

**裁定：repair = 新 Execution。** 理由：Execution 在 ADM 已是 append-only / terminal，且 Handoff 與 provenance 都假設其不可變；在同一 Execution 內迭代會撞既有語意。

```
Execution 新增（鏡像既有 retry_of_execution_id 的形態）：
  repair_of_execution_id : string|null   # 新 candidate_sha（程式碼改了）
  retry_of_execution_id  : string|null   # 既有：同 candidate_sha（transient）
  trigger : enum{ human, goal_loop, verification_repair,
                  verification_retry, bundle_revision, pfp_return }
```

**retry 與 repair 是兩個不同預算、兩個不同欄位**：retry 不改 candidate_sha（僅 C6 transient 可用）；repair 必然改 candidate_sha（因而作廢舊 report）。混用即為攻擊 7/8 的入口，所以在 schema 層就分開。

計數住在 **Task**（`verification{}`），且被定義為**對 Execution chain 的 fold**（可重算、可稽核），因此**不構成第二套 SSOT**：

```
Task.verification = {
  deliverable_sha              : string|null,   # 見 A.5
  acceptance_bundle_ref        : string,
  repair_round_count           : int,   # = |{e in chain : e.repair_of_execution_id != null}|
  retry_round_count            : int,
  review_round_count           : int,
  failure_signature_counts     : { <signature> : int },
  budget = { repair_max, retry_max, review_max, wallclock_minutes,
             provider_quota_share },
  budget_consumed              : {...},
  human_decisions              : [decision_id...]
}
```
每個欄位都必須能由 chain 重算；重算不符 = 資料損毀 = fail closed。

### A.5 Task aggregation（B5 的 Task 層，攻擊 5）

> **Task 的可交付物是「一個 SHA」，不是「一組 PASS」。**

```
close_eligible(task) :=
    ∃ e ∈ task.executions :
        e.candidate_sha == task.verification.deliverable_sha
      ∧ acceptance_state(e) ∈ {ACCEPTED, ACCEPTED_WITH_RESIDUAL}
      ∧ required_gates(effective_risk(e)) ⊆ passing_gates(e, e.candidate_sha)
      ∧ ¬STALE ∧ ¬REVOKED
```

早期 Execution 的 PASS 是**歷史，不是額度**。跨 SHA 拼裝 required gate 的 close_eligible 在定義上不可能成立。

**代價要講明白**：最終 SHA 上必須有一次全 required gate 的完整跑。中途 targeted rerun（B5/E）純粹是省錢手段，不能取代這一次。這條會提高每次合併的成本；這是為了關掉 ledger「改一處 HTML → 十組 golden 在 PASS 之後才被抓到」那個歷史事故形狀，我認為值得，但**成本上升是真的，請人類知情**。

### A.6 明確不新增的 entity

不新增：`LoopRun`、`AcceptanceRecord`（作為可寫終態）、`VerificationSession`、第二套 project/task registry。
只新增：**Ticket**、**VerificationReport**、**Bundle manifest**、**HumanDecisionRecord** —— 四者皆為 Execution 的附屬證據物，皆 append-only，皆不持有終態。

---

## B. Frozen Acceptance Bundle Schema（欄位級；architecture schema，非 production code schema）

```yaml
acceptance_bundle:
  bundle_id: "ab-<project_id>-<semver>"
  bundle_version: "1.4.0"
  bundle_hash: "<sha256 of canonical_json(manifest)>"   # 由下列全部成員遞迴涵蓋
  supersedes: "<bundle_hash|null>"
  approved_by: { human_decision_id, approved_at }        # 升版必須有人（I9）

  governance:                                            # I10
    source_uri: "gdrive://1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU"
    rules_version: "0.5.0"
    rules_digest: "<sha256>"          # 沿用 Task.governance 既有欄位形態
    project_rules_uri: "gdrive://1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ"
    project_rules_version: "1.1.0"
    project_rules_digest: "<sha256>"
    github_copy_authoritative: false   # 硬編：GitHub 副本永不採信

  repo_binding:
    repository: "github:<owner>/<repo>"        # 沿用 lease_evidence pattern
    default_branch: "refs/heads/main"
    production_canonical_checkout_paths: [ "<abs path>", ... ]   # V0 黑名單
    production_ai_manager_home: "<abs path>"                     # V0 黑名單

  contract:
    clauses:
      - clause_id: "L-ART-003"
        statement: "每張輸出工作表的凍結窗格必須等於 golden descriptor"
        oracle_ref: "ORA-ART-STRUCT"
        required_from_risk: R2
    clause_digest: "<sha256>"

  checkers:                                   # checker_id 是身分，不是名字
    - checker_id: "ledger_excel_structural"
      checker_version: "2.3.0"
      checker_impl_digest: "<sha256 of implementation bytes>"
      environment: isolated
      side_effect_class: sandbox_write
      quota_cost: 0
      production_write: false
      replayable: true
      transient_code_allowlist: [ "IO_TIMEOUT", "PROC_SPAWN_EAGAIN" ]

  frozen_oracles:                             # B3：hash 進 bundle_hash
    - oracle_id: "ORA-ART-STRUCT"
      role: golden
      members:
        - { path: "tests/golden/2026-09-03-golden-baseline.json", sha256: "<…>" }
        - { path: "tests/golden/workbook-descriptor.json",        sha256: "<…>" }
    - oracle_id: "ORA-SUITE-V2"
      role: test_selector
      members:                                 # 只凍「required gate 引用到的」測試檔
        - { path: "tests/test_case_state.py", sha256: "<…>" }
      note: "新增測試不改 bundle_hash（additive 合法）；修改被引用測試會改"
    - oracle_id: "ORA-SNAPSHOT-UI"
      role: snapshot
      members: [ ... ]

  baseline:                                    # I4
    base_sha: "<40hex>"
    environment_fingerprint:
      os: "win32"
      checkout_path_length: 78                 # 記憶：installer 測試對路徑長度敏感
      python: "3.x.y"
      ai_manager_home_class: "ephemeral"
    known_baseline_failures:
      - { test_id: "...", normalized_signature: "<sha256>", note: "installer path length" }
    attested_by_report_digest: "<sha256>"      # base 跑的那份 report

  gate_matrix: [ ... ]                         # 見 C.1

  risk_rules:                                  # B6 / E，寫進 bundle 才不與 freeze 衝突
    declared_risk_floor: R1
    path_risk_rules:
      - { glob: "manager/dispatch/**", risk: R3 }
      - { glob: "manager/provenance/**", risk: R4 }
      - { glob: "**/credentials*", risk: R4 }
    component_escalation_rules:
      - { component: "production_canonical_checkout", risk: R5, action: ROUTE_TO_PFP }

  impact_rules:                                # B5
    always_rerun: [ V0, V1 ]
    by_path:
      - { glob: "manager/artifact/**", gates: [ V1, V2, V3 ] }
      - { glob: "dashboard/**",        gates: [ V2, V3 ] }
      - { glob: "manager/dispatch/**", gates: [ V2, V3, V4 ] }
    unmatched_path_policy: RERUN_ALL           # fail closed，未知路徑不得縮小重跑面

  test_defect_policy:                          # B3
    admission: [ TD1_BASE_ALSO_FAILS, TD2_CONTRACT_CONTRADICTION_PROOF ]
    repair_allowed_paths_expr: "test_paths \\ frozen_oracles.members[].path"
    frozen_oracle_touch_action: FORCE_RECLASSIFY_TO_ACCEPTANCE_CONTRACT_DEFECT

  budgets:
    repair_max: 3
    retry_max: 2
    review_max: 2
    provider_quota_share: { max_percent_of_window: 15, subordinate_to_adm_dispatch: true }
```

**B3 的兩個機械後果**：
1. 動 golden / snapshot / baseline / 被引用測試 → `bundle_hash` 改變 → **所有既有 report 綁的是舊 hash，全部不可採信**，且新 bundle 需人類核准。「改 golden 讓它過」在同一 hash 下**不存在**。
2. TEST_DEFECT 的修補 diff 只要碰到 `frozen_oracles.members[].path`，分類被**強制**升為 `ACCEPTANCE_CONTRACT_DEFECT`（新版本、新 Execution），不是「改 test 就過」。

---

## C. Gate Matrix + VerificationReport / Evidence Contract

### C.1 Gate Matrix

| Gate | checker identity | environment | evidence source | pass predicate | oracle | side effects | required from risk | invalidation |
|---|---|---|---|---|---|---|---|---|
| **V0 Preflight / Identity** | `controller_preflight` | isolated | external truth（git、fs、Drive doc digest） | repo/branch/base 可解析 ∧ worktree ∉ production_canonical_checkout_paths ∧ `AI_MANAGER_HOME` ≠ production home ∧ dirty ⊆ allowed_paths ∧ bundle_hash 可解析 ∧ 現行 Drive rules_digest == bundle.governance.rules_digest | repo_binding + governance | pure_read | **R0（永遠必要）** | 任何 candidate/bundle/governance 變動 |
| **V1 Build / Artifact integrity** | `<project>_build` | isolated | checker replayable | 建置退出碼 0 ∧ 產物 digest 可重現（同輸入兩次同 digest） | 建置決定論契約 | sandbox_write | R1 | candidate_sha |
| **V2 Regression（baseline-aware）** | `<project>_suite` | isolated | checker replayable | ∀t: ¬(PASS@base ∧ FAIL@candidate)，env_fp 相等；新失敗必須落在 `known_baseline_failures` 否則為 C3/C7 | ORA-SUITE-V2 + baseline | sandbox_write | R1 | candidate_sha、被引用測試 digest、env_fp |
| **V3 Contract oracle（含全覆蓋維度）** | 專案專屬 | isolated | checker replayable | ∀clause ∈ required_clauses(risk)：oracle 判定為真，**且 report 宣告的 `coverage_dimensions` 必須等於 gate 宣告的完整集合**（子集 ⇒ UNKNOWN，非 PASS） | frozen_oracles | sandbox_write | R2 | candidate_sha、oracle digest |
| **V4 Real-world observation** | `<project>_runtime_probe` | **staging**（隔離 project_id + 全新 `AI_MANAGER_HOME`） | **external truth verified**（Session Center、provider log、Drive record、`git ls-remote`、process identity） | 見 C.4 ADM 專用謂詞 | 外部真相 | provider_dispatch / external_write，**quota_cost > 0** | R3 | candidate_sha、identity 解析結果 |
| **V5 Independent adversarial review** | 人或 AI，**identity 必須與 executor 三元組不相交** | isolated | reviewer 產生的 ReproductionArtifact | 無 open blocking finding；finding 必附 reproduce（存活 mutant 算 reproduce） | 契約 + reproduce | pure_read | R1 | candidate_sha |
| **V6 Production activation** | — | **production** | — | **本迴圈不得執行**。`production_write: true` ⇒ 一律 `DEFERRED_TO_PFP` | PFP | production_write | R5 | — |

**I5 的機械形式**：任何 `environment == production` 或 `production_write == true` 的 gate，verification-controller **在結構上無法**產生它的 PASS —— 它只能產生 `DEFERRED_TO_PFP`。因此 R5 風險的 Execution 永遠到不了 `ACCEPTED`，只能到 `DEFERRED_TO_PFP`。這就是「不與 production-fix-protocol 搶終態」的機械保證，而非口頭約定。

**quota 從屬（I5 後半）**：V4 的 provider 呼叫必須經 ADM 既有 quota-aware dispatch，且受 `budgets.provider_quota_share` 上限拘束。Goal loop 不得因驗證把正式派工餓死（Codex quota 到 9/7 那次的形狀）。

### C.2 VerificationReport（欄位級）

```yaml
verification_report:
  report_id, report_digest              # digest = sha256(canonical_json(report \ report_digest))
  ticket_id                             # 必須對應一張「先發」的 ticket，見 C.3
  execution_id, task_id, project_id
  gate_id, verification_round

  candidate_sha, base_sha               # 40hex
  acceptance_bundle_hash
  governance: { rules_version, rules_digest, source_uri }

  checker_id, checker_version, checker_impl_digest
  oracle_set_digest                     # 本次實際使用的 oracle 成員 digest 集合

  producer_identity:                    # B1 三元組
    provider, account_id, session_id, provider_session_id
  identity_resolution:                  # 必須「被解析」，不得自我宣告
    method:      <Session.classification_method>
    status:      <Session.classification_status>
    confidence:  <Session.classification_confidence>
    resolved_by: "session_center"

  environment:
    class: isolated|staging             # production 不可能出現於 report
    worktree_path
    worktree_head_before, worktree_head_after     # I6：必須相等且 == candidate_sha
    ai_manager_home_id, ai_manager_home_class
    host, checkout_path_length          # 進 env_fp

  evidence_source: checker_produced_replayable | external_truth_verified
  replay_spec:                          # 反偽造核心
    command, deterministic: true, expected_evidence_digest
  external_truth_refs: [ ... ]          # evidence_source=external 時必填
  evidence_digest, evidence_ref
  coverage_dimensions: [ ... ]          # V3 用；必須等於 gate 宣告的完整集合

  result: PASS | FAIL | UNKNOWN | DEFERRED_TO_PFP
  failure_observations:
    - { observation_id, locus, observed, expected, transient_code|null,
        proposed_class|null,           # 模型只能「提議」
        reproduction: <ReproductionArtifact|null> }
  started_at, completed_at
```

**Evidence 的三分法（B2 核心）**：

| 來源 | 可否成為 evidence |
|---|---|
| executor 提交的任何內容（含 `Handoff.tests_status`、`repo_write_evidence.tests[]`、prompt 檔、log 檔） | **永遠只是 input，永不是 evidence** |
| checker 自己產生且 **replayable**（第三方可在同一 candidate_sha 重跑得到相同 `evidence_digest`） | evidence |
| checker 對**外部真相**驗證後的結果（`git ls-remote` 實得 remote_sha、Session Center correlation、provider 端記錄、Drive execution 檔讀回） | evidence |

### C.3 Ticket / 准入（B1 的「機制」而非「一句話」）

Controller **先發 ticket，後收 report**：

```yaml
verification_ticket:
  ticket_id: "vt-<sha256(execution_id‖candidate_sha‖bundle_hash‖gate_id‖round)>"   # 即 CAS key（I6）
  issued_at, issued_by_identity: {provider, account_id, session_id}
  execution_id, candidate_sha, acceptance_bundle_hash, gate_id, verification_round
  expected_checker_identity: {provider, account_id, session_id}
  forbidden_identity: <executor 三元組（從 Execution/Session 解析）>
  worktree_lease: { lock_id: "repo-<sha256>", generation }     # 沿用既有 worktree_lock 形態
  candidate_head_at_issue: "<40hex>"
  status: issued | consumed | invalidated
  consumed_report_digest
```

**admissible(report) 的完整謂詞（全部為 AND）**：

1. `∃ ticket : ticket.ticket_id == report.ticket_id ∧ ticket.status == issued`
2. ticket 的 `(execution_id, candidate_sha, bundle_hash, gate_id, round)` 與 report 逐欄相等
3. `report.producer_identity == ticket.expected_checker_identity`
4. `report.producer_identity ≠ ticket.forbidden_identity`（三元組整體比較；同 provider 不同 account 視為不同，同 account 不同 session 亦視為不同）
5. `identity_resolution.status == "classified" ∧ confidence == "high" ∧ method ∉ {conflicting_deterministic_signals, unclassified}`
6. `report.governance.rules_digest == bundle.governance.rules_digest`
7. `report.acceptance_bundle_hash == execution.acceptance_bundle_ref.bundle_hash`
8. `worktree_head_before == worktree_head_after == candidate_sha`
9. `checker_impl_digest` 與 bundle 登記者相等
10. gate.replayable ⇒ `replay_spec` 存在 ∧ 重跑得到 `expected_evidence_digest`；¬replayable ⇒ `evidence_source == external_truth_verified` ∧ `external_truth_refs` 非空
11. `report_digest` 與 ticket 消費時記載者相等（append-only ledger，`ticket_seq` 單調、CAS）

任一不成立 ⇒ **該 report 不進 derivation**（不是 FAIL，是不存在）。

### C.4 ADM V4 的正式謂詞（B2 指名項）

```
V4_ADM_PASS :=
    provider_evidence.{host, pid, creation_identity} 存在
  ∧ provider_evidence.scheduler_invocation_id 可在 runtime/scheduler-invocations/ 對上
  ∧ Session.classification_status == "classified"
  ∧ Session.classification_confidence == "high"
  ∧ Session.classification_method ∉ {conflicting_deterministic_signals, unclassified}
  ∧ execution_id 出現於 provider 端記錄
  ∧ Drive execution 檔可讀回且唯一（無重複同名）
```

**correlation 失敗的處置（硬性）**：`classification_status == "needs_review"` 或 `method ∈ {unclassified, conflicting_deterministic_signals}` ⇒ `result = UNKNOWN` ⇒ C7 ⇒ `ESCALATE_HUMAN`。
**明令禁止**：不得判為 PASS，不得歸為 `ENVIRONMENT_TRANSIENT` 進入無限 retry。（此即記憶中 Session Center correlation 既存缺陷的正確吸收方式：把已知缺陷變成 UNKNOWN，不是變成噪音。）

以下**單獨或組合皆不足以**構成 V4 PASS，且必須被明確拒絕：HTTP 200、localhost port listening、prompt 檔已產生、`Handoff.tests_status == passed`、executor 自述「已啟動」。

---

## D. Failure Admission Predicate → Unique Next Action

### D.1 准入謂詞（模型只能提議；Controller 依謂詞准入）

| Class | 准入謂詞（deterministic） |
|---|---|
| **C0 GOVERNANCE_CONFLICT** | V0 實測 Drive `rules_digest` ≠ `bundle.governance.rules_digest`，或 `source_uri` 非指定 Drive doc |
| **C1 PRODUCTION_SCOPE_VIOLATION** | worktree ∈ `production_canonical_checkout_paths` ∨ `AI_MANAGER_HOME` == production home ∨ diff ∩ R5 path class ≠ ∅ |
| **C2 ACCEPTANCE_CONTRACT_DEFECT** | (a) 有效 `ReproductionArtifact` ∧ `contract_coverage_query(repro) == NONE`；∨ (b) 已准入的 C5 之修補 diff ∩ `frozen_oracles.paths` ≠ ∅；∨ (c) 兩個 required gate 對同一謂詞給出矛盾裁決 |
| **C3 REGRESSION** | ∃t：`result(t, base_sha, env_fp) == PASS ∧ result(t, candidate_sha, env_fp) == FAIL ∧ env_fp 相等`；base 結果須來自本輪實跑或 bundle 中對**該精確 base_sha** 的 attestation |
| **C4 CONTRACT_VIOLATION** | gate pass_predicate 為假 ∧ `clause_id` 可解析 ∧ ¬C3（base 本來就沒過，非退步） |
| **C5 TEST_DEFECT** | (TD-1: 同 test 在 `base_sha` 亦 FAIL ∧ normalized_signature 相等 ∧ env_fp 相等) ∨ (TD-2: 存在可重現的 `contract_contradiction_proof{test_id, asserted_predicate, clause_id, witness}`)；**且**修補 diff ∩ frozen_oracles == ∅ |
| **C6 ENVIRONMENT_TRANSIENT** | failure 帶 **checker 自身發出**的 `transient_code` ∈ `bundle.checkers[].transient_code_allowlist` ∧ gate.retryable |
| **C7 UNKNOWN** | 以上皆不成立，或比較所需資料缺席（base 未跑、correlation 失敗、coverage 不完整） |

### D.2 唯一 next_action（total、single-valued）

`ADMITTED` = 本輪所有 active failure 之已准入 class 集合。**依固定順序求值第一個成立的 guard**：

| # | Guard | next_action |
|---|---|---|
| G1 | `C0 ∈ ADMITTED` | `HALT_GOVERNANCE_CONFLICT`（人類） |
| G2 | `C1 ∈ ADMITTED` ∨ `effective_risk == R5` | `ROUTE_TO_PFP` |
| G3 | `C2 ∈ ADMITTED` | `OPEN_BUNDLE_REVISION`（新 bundle 版本 + 人類核准 + 新 Execution） |
| G4 | `C7 ∈ ADMITTED` | `ESCALATE_HUMAN(UNKNOWN)` |
| G5 | `C3 ∈ ADMITTED` | `REPAIR(scope = regression set)`，新 Execution + `repair_of_execution_id` |
| G6 | `C4 ∈ ADMITTED` | `REPAIR(scope = violated clauses)` |
| G7 | `C5 ∈ ADMITTED` | `REPAIR_TEST_ONLY(allowed = test_paths \ frozen_oracles)` |
| G8 | `ADMITTED == {C6}`（**全等，非包含**） | `RETRY_SAME_CANDIDATE` |
| G9 | `ADMITTED == ∅` ∧ required 覆蓋已滿 | `EMIT_ACCEPTED`（即 derivation 得 ACCEPTED） |
| G10 | 其餘（覆蓋未滿） | `CONTINUE_VERIFICATION(missing gates)` |

**後置預算過濾**：任何消耗預算的 action（G5–G8），若對應 budget 已耗盡 ⇒ 改為 `ESCALATE_HUMAN(BUDGET_EXHAUSTED)`。仍為單值。

**單值性論證**：`ADMITTED` 是有限集合上的固定序列 guard 求值，第一個成立即返回，G10 為全稱兜底 ⇒ 全域、單值。

**兩個刻意的排序決定，請人類特別檢視**：

1. **G8 用「集合全等」而非「包含」**（B4 直接要求）。只要同時存在任何具體缺陷，`RETRY` 就不可能被選中 —— transient 遮蔽 regression 的路徑在定義上關閉。
2. **G4（UNKNOWN）排在 G5（REGRESSION）之前**，這是我在本輪新發現的洞：若先修 regression，repair 會改變 `candidate_sha`，而綁在舊 sha 上的 UNKNOWN observation 會**隨之無聲蒸發**。攻擊者只要製造一個瑣碎 regression，就能讓一個 UNKNOWN 消失。因此 UNKNOWN 必須先被人類處置。代價是效率（人類會被較早叫醒），我選擇正確性。

---

## E. Risk Escalation + Diff → Gate Impact Matrix

### E.1 effective_risk（B6）

```
effective_risk(execution) = max(
    task.declared_risk,                                  # 只是下限，不被信任
    max{ rule.risk : rule ∈ path_risk_rules, diff_paths ∩ glob ≠ ∅ },
    max{ rule.risk : rule ∈ component_escalation_rules, component 命中 }
)
```
**只升不降**，且**每一輪對當前 candidate diff 重算**（repair 後 diff 變了，風險可能升）。escalation 規則寫在 bundle 內，因此「升級到 PFP」不構成修改 contract，與 freeze 不衝突。

### E.2 Risk 層級 × Diff → Gate 影響矩陣（涵蓋指定十類）

| 變更類別 | 代表路徑 | risk | required gates | 特殊 invalidation / 備註 |
|---|---|---|---|---|
| ordinary code | `manager/**`（非下列） | R1 | V0,V1,V2,V5 | — |
| artifact generation | `**/artifact/**`, 產檔器 | R2 | +V3(ORA-ART-STRUCT) | 產物 digest 決定論；V2 綠燈**不得**替代 V3 |
| UI | `dashboard/**`, `**/*.css`, 模板 | R2 | +V3(ORA-SNAPSHOT-UI，`coverage_dimensions` 含 desktop+mobile viewport) | 缺 viewport ⇒ UNKNOWN 非 PASS |
| ADM dispatch | `manager/dispatch/**` | R3 | +V4(staging 實派工 + Session correlation) | quota_cost>0；受 quota share 拘束 |
| Watcher | `manager/watcher/**`, 排程 | R3 | +V4(多 cycle 輪替觀察) | 記憶：單次 `--once` 會重打同一 project，需真實多分鐘多 cycle |
| Drive write | `cloud/**`, Drive client | R3→R4 | +V4(external：讀回 Drive 記錄) + idempotency/重複同名檢查 | 已知 execution reservation race：重複同名 ⇒ FAIL 非 PASS |
| launcher | `desktop/**`, `*.cmd`, `*.lnk`, 啟動器 | R3 | +V4(process identity：`creation_identity`/`launcher_pid`) | 記憶：`DETACHED_PROCESS` 殺 powershell launcher，須用 `CREATE_NO_WINDOW` |
| runtime provenance | `manager/provenance/**`, cursor/CAS | **R4** | +V4(TESTED==ACTIVATED==RUNNING、generation 單調、CAS 不變式) | 每次跑必須全新 `AI_MANAGER_HOME`（記憶：不換會出現幻影第三個失敗） |
| credentials | `**/credentials*`, token/OAuth | **R4** | +V0 secret-scope（密鑰不得進任何 evidence）+ 人類 gate | evidence 內出現疑似密鑰 ⇒ 報告作廢 |
| **production canonical checkout** | worktree 命中黑名單 / 動到 live runtime | **R5** | **無**（本迴圈不可執行） | `ROUTE_TO_PFP`，永不 ACCEPTED |

`unmatched_path_policy: RERUN_ALL` —— 未被任何規則命中的新路徑**不得**縮小重跑面（fail closed）。

---

## F. 三組 False-Complete Fixtures 規格（machine-readable，I8）

三者皆為 Phase B 第一件事：用「只跑狀態機、無 adapter 的參考實作」跑這三個 fixture，Phase A 的 PASS 因此可機器判定。

### F.1 Ledger — 測試全過，但 Excel 凍結窗格錯

```json
{ "fixture_id": "FX-LEDGER-FREEZE-PANE",
  "task": { "task_id":"T-LED-01", "declared_risk":"R1",
            "deliverable_sha":"aaaa…01",
            "acceptance_bundle_ref":"ab-ledger-1.4.0" },
  "execution": { "execution_id":"E-LED-01", "status":"completed",
                 "candidate_sha":"aaaa…01", "base_sha":"bbbb…00",
                 "repair_of_execution_id":null,
                 "diff_paths":["manager/artifact/workbook_writer.py"] },
  "bundle": { "bundle_hash":"H_led_140",
              "frozen_oracles":["ORA-ART-STRUCT(golden workbook descriptor 含 freeze_panes)"],
              "impact_rules.by_path":[{"glob":"manager/artifact/**","gates":["V1","V2","V3"]}] },
  "reports": [
    {"gate":"V0","result":"PASS","admissible":true},
    {"gate":"V1","result":"PASS","admissible":true},
    {"gate":"V2","result":"PASS","admissible":true,"note":"套件從未斷言 freeze_panes"}
  ],
  "expected": {
    "effective_risk":"R2  (diff 命中 artifact ⇒ 由 R1 升級)",
    "required_gates":["V0","V1","V2","V3","V5"],
    "acceptance_state":"IN_VERIFICATION",
    "next_action":"CONTINUE_VERIFICATION(missing=[V3,V5])",
    "rejected_false_passes":[
      "V2 全綠不得推導 ACCEPTED（required 覆蓋未滿）",
      "executor 的『測試全過』敘述為 input 非 evidence"
    ]
  },
  "round_2": {
    "reports_add":[{"gate":"V3","result":"FAIL","clause_id":"L-ART-003",
                    "observed":"freeze_panes=A1","expected":"freeze_panes=A4"}],
    "expected":{ "admitted":["C4"], "next_action":"REPAIR(scope=[L-ART-003])",
                 "acceptance_state":"REJECTED_NEEDS_REPAIR" }
  },
  "variant_I1": {
    "premise":"bundle 尚無 L-ART-003 條款；由 V5 reviewer 提出可重現的凍結窗格缺陷",
    "expected":{ "contract_coverage_query":"NONE", "admitted":["C2"],
                 "next_action":"OPEN_BUNDLE_REVISION",
                 "forbidden":"不得標為 ADVISORY 後 ACCEPTED" }
  }
}
```

### F.2 Obsidian Dashboard — DOM/桌面過，但手機長文溢出

```json
{ "fixture_id": "FX-OB-MOBILE-OVERFLOW",
  "execution": { "execution_id":"E-OB-01","status":"completed",
                 "candidate_sha":"cccc…01","diff_paths":["dashboard/home.css"] },
  "bundle": { "gate_matrix.V3.coverage_dimensions":
                ["viewport:desktop-1440","viewport:tablet-768","viewport:mobile-375"],
              "gate_matrix.V3.clauses":["OB-UI-011 no_overflow: scrollWidth <= clientWidth",
                                        "OB-UI-012 長文語料 fixture 必須套用"] },
  "reports": [
    {"gate":"V0","result":"PASS"},
    {"gate":"V2","result":"PASS"},
    {"gate":"V3","result":"PASS","coverage_dimensions":["viewport:desktop-1440"],
     "admissible":true,
     "note":"report 本身格式合法，但宣告覆蓋是 gate 完整集合的真子集"}
  ],
  "expected": {
    "effective_risk":"R2",
    "gate_outcome_override":"V3 → UNKNOWN（coverage_dimensions ⊊ 宣告集合）",
    "admitted":["C7"],
    "acceptance_state":"BLOCKED_HUMAN",
    "next_action":"ESCALATE_HUMAN(UNKNOWN: V3 coverage incomplete)",
    "rejected_false_passes":[
      "桌面 DOM PASS 不得計為 V3 PASS",
      "『Obsidian 裡看起來正常』的人工目視敘述不是 evidence",
      "缺 mobile 維度不得降級為 ADVISORY"
    ]
  },
  "round_2_after_full_coverage": {
    "reports_add":[{"gate":"V3","result":"FAIL","clause_id":"OB-UI-011",
                    "locus":"viewport:mobile-375","observed":"scrollWidth 431 > clientWidth 375"}],
    "expected":{"admitted":["C4"],"next_action":"REPAIR(scope=[OB-UI-011])"}
  }
}
```

### F.3 ADM — API 200 / port listening / prompt 已產生，但 provider 未真啟動或 correlation 失敗

```json
{ "fixture_id": "FX-ADM-FALSE-DISPATCH",
  "execution": { "execution_id":"E-ADM-01","status":"completed",
                 "candidate_sha":"dddd…01",
                 "diff_paths":["manager/dispatch/launcher.py"] },
  "bundle": { "gate_matrix.V4":{ "environment":"staging",
                                 "side_effect_class":"provider_dispatch",
                                 "production_write":false,
                                 "replayable":false,
                                 "evidence_source":"external_truth_verified" } },
  "executor_submitted_inputs": [
    "HTTP 200 from localhost",
    "socket LISTENING on :8501",
    "prompt file generated",
    "Handoff.tests_status = passed"
  ],
  "reports": [
    {"gate":"V0","result":"PASS"},
    {"gate":"V2","result":"PASS"},
    {"gate":"V4","result":"UNKNOWN","evidence_source":"external_truth_verified",
     "external_truth_refs":["session_center:S-…"],
     "identity_resolution":{"status":"needs_review",
                            "method":"conflicting_deterministic_signals",
                            "confidence":null},
     "failure_observations":[{"locus":"session_correlation",
                              "observed":"needs_review","expected":"classified/high",
                              "transient_code":null}]}
  ],
  "expected": {
    "effective_risk":"R3 (dispatch/launcher)",
    "required_gates":["V0","V1","V2","V3","V4","V5"],
    "admitted":["C7"],
    "acceptance_state":"BLOCKED_HUMAN",
    "next_action":"ESCALATE_HUMAN(UNKNOWN: session correlation unresolved)",
    "rejected_false_passes":[
      "HTTP 200 / LISTENING / prompt 檔存在：executor 提交之 input，永不構成 evidence",
      "Handoff.tests_status=passed：input，非 evidence",
      "correlation_failed 不得歸為 ENVIRONMENT_TRANSIENT（無 checker 發出之 transient_code）",
      "不得因『看起來啟動了』推導 V4 PASS"
    ]
  },
  "variant_production": {
    "premise":"同一修補改到 production canonical checkout 或 live runtime 路徑",
    "expected":{"effective_risk":"R5","admitted":["C1"],
                "next_action":"ROUTE_TO_PFP",
                "acceptance_state":"DEFERRED_TO_PFP",
                "note":"verification loop 在結構上無法產生此案的 ACCEPTED"}
  }
}
```

---

## I1–I10 逐項處置對照（無一略過）

| # | 處置 | 落點 |
|---|---|---|
| I1 | 第三條路成立：reproduce 有效但 `contract_coverage_query == NONE` ⇒ **C2 ACCEPTANCE_CONTRACT_DEFECT**（契約不完整），非 ADVISORY。**存活 mutant 明訂為有效 reproduce**（對齊 PFP Phase E/F） | D.1 C2、F.1 variant_I1 |
| I2 | repair = **新 Execution** + `repair_of_execution_id` + `trigger`；retry 沿用既有 `retry_of_execution_id`（兩套預算分離）；counters/budget/signature 住 **Task.verification**，定義為對 chain 的可重算 fold ⇒ 非第二套 SSOT | A.4 |
| I3 | **STALE**（輸入變、無缺陷、自動）與 **REVOKED**（同輸入下反證、有缺陷、須 `revocation_evidence`）語意分離；兩者皆令 `close_eligible` 重算，皆不改寫歷史 | A.3、A.5 |
| I4 | V2 baseline-aware：REGRESSION := base PASS ∧ candidate FAIL ∧ **env_fp 相等**（env_fp 含 `checkout_path_length`）；`known_baseline_failures` 凍在 bundle 並附 attesting report | B.baseline、C.1 V2、D.1 C3 |
| I5 | 每 gate 有 `environment` / `side_effect_class` / `quota_cost` / `production_write`；`production_write=true` ⇒ 只能 `DEFERRED_TO_PFP`，迴圈**在結構上**無法給它 PASS；驗證用 quota 從屬既有 quota-aware dispatch 且設上限 | C.1、B.budgets |
| I6 | 驗證在 **isolated worktree** 對 **immutable candidate_sha** 執行；**CAS key = `sha256(execution_id‖candidate_sha‖bundle_hash‖gate_id‖round)`**（即 `ticket_id`）；`worktree_head_before == after == candidate_sha`，否則 `ROUND_INVALIDATED` | C.3、A.3 |
| I7 | 正式名 `verification-loop` / `verification-controller`，store 在 `runtime/verification/`；**禁用** `acceptance-gate/` 與 `CONTROLLED_ACCEPTANCE_GATE`；UEVL 刪除 | §0 |
| I8 | 三案例已轉為 machine-readable fixture（F.1–F.3），含唯一 expected 終態與 next_action；Phase B 第一件事是無 adapter 的狀態機參考實作跑通三者 | F |
| I9 | 人類是唯一非 Controller 寫入者，且**只能寫這五類**：`PFP_ACCEPTED/PFP_ABORTED`、`BLOCKED` 解除、budget 上調、bundle 版本核准、REVOKED 裁決。每筆為 append-only `HumanDecisionRecord`（含人類 identity）。**關鍵限制：人類記錄永不能替代缺席的 gate PASS**；它只能授權路由，或接受被記錄的 residual —— 後者導出 `ACCEPTED_WITH_RESIDUAL`，是與 `ACCEPTED` **不同的狀態**，不得混同顯示。第二條無證據完成通道因此不存在 | A.3、B.approved_by |
| I10 | `bundle.governance` 綁 Drive doc id + version + digest（沿用既有 `Task.governance` 形態）；`github_copy_authoritative: false` 硬編；V0 每輪實測 digest 比對，不符 ⇒ C0 `HALT` | B、C.1 V0、D.1 C0 |
| Minor | `owner: artifact_adapter` 之類角色名一律改為可驗證 identity 三元組；Proactive/Proposal intake 不在 Phase A（原則 10），但已於此標記為 Phase C 前置，以免重演 DISPATCH-REQUESTS Gate 0 的提交格式事故 | C.2、範圍聲明 |

---

## Adversarial Self-Check — 12 項攻擊

| # | 攻擊 | 判定 | 阻擋的機械機制（單一指名） |
|---|---|---|---|
| 1 | executor 偽造 report | **BLOCKED** | Ticket 先發後收：無 `ticket_id` 對應之 report 不進 derivation（C.3 謂詞 1）；且 `replay_spec` 使可重放 gate 的 evidence 必須在 candidate_sha 重跑得到相同 `evidence_digest`（謂詞 10），偽造內容通不過重放 |
| 2 | 換 Claude account / session | **BLOCKED** | 身分為三元組整體比對：`producer_identity == ticket.expected_checker_identity ∧ ≠ ticket.forbidden_identity`（謂詞 3+4）；且身分必須由 Session Center **解析**（謂詞 5），不接受自我宣告。換帳號改變三元組 ⇒ 與 ticket 不符 ⇒ 不可採信 |
| 3 | 改 golden | **BLOCKED** | golden ∈ `frozen_oracles`，其 sha256 進 `bundle_hash`；改動即換 hash ⇒ 舊 report 綁舊 hash 全部不可採信（謂詞 7），新 bundle 需 `approved_by` 人類；且 diff 觸及 frozen_oracles ⇒ 強制 C2 |
| 4 | 改 test | **BLOCKED** | 被 required gate 引用之測試檔在 `ORA-SUITE-V2` 內、digest 進 bundle_hash（同上）；欲以 TEST_DEFECT 正當化，須通過 TD-1/TD-2 准入謂詞；`test_defect_policy.repair_allowed_paths_expr` 明文排除 frozen_oracles |
| 5 | 混用不同 candidate SHA 的 PASS | **BLOCKED** | `ACCEPTED` 需 required gates 在**同一 final candidate_sha** 全有 admissible PASS（A.2/A.5）；謂詞 2 逐欄比對 candidate_sha；Task 層 `close_eligible` 綁單一 `deliverable_sha` |
| 6 | 低報 risk_class | **BLOCKED** | `effective_risk = max(declared, diff-derived, component)` 且每輪對當前 diff 重算、只升不降（E.1）；`declared` 僅作下限，永不作上限 |
| 7 | transient 蓋掉 regression | **BLOCKED** | G8 用**集合全等** `ADMITTED == {C6}`（非包含）；且 C6 准入要求 checker 自身發出的 allowlist `transient_code`，模型的分類提議不具准入力 |
| 8 | repair 後沿用舊 evidence | **BLOCKED** | report 綁 candidate_sha（謂詞 2）；repair 依定義改變 candidate_sha；`impact_rules` + `unmatched_path_policy: RERUN_ALL`；最終仍需在 final SHA 全 required 覆蓋 |
| 9 | contract 升版後沿用舊 ACCEPTED | **BLOCKED** | `derivation_key` 含 `bundle_hash` 與 `DERIVE_ALGO_VERSION`；升版後重算不符 ⇒ 自動 `STALE` ⇒ `close_eligible` 重算為 false。因 ACCEPTED 不是存下來的欄位，此處無「沿用」的物件可存在 |
| 10 | production-affecting 留在普通迴圈 | **BLOCKED** | V0 對 `production_canonical_checkout_paths` / production `AI_MANAGER_HOME` 黑名單實測 ⇒ C1；G2 於 G3–G10 之前 ⇒ `ROUTE_TO_PFP`；且 `production_write=true` 的 gate 只能產生 `DEFERRED_TO_PFP`，迴圈**無法**表達其 PASS |
| 11 | concurrent executor 驗證途中改 candidate | **BLOCKED** | ticket 記 `candidate_head_at_issue`，report 須 `worktree_head_before == after == candidate_sha`（謂詞 8）；不符 ⇒ `ROUND_INVALIDATED`（非 FAIL、非 PASS）；worktree lease 沿用既有 `lock_id`+`generation` |
| 12 | reviewer 可重現但契約未涵蓋 | **BLOCKED** | I1 第三條路：`contract_coverage_query == NONE` ⇒ C2 ⇒ G3 `OPEN_BUNDLE_REVISION`；存活 mutant 明訂為有效 reproduce。**沒有** ADVISORY-then-ACCEPTED 的路徑 |

**12 / 12 BLOCKED。**

### 必須明講的信任邊界（不是漏洞，是前提）

上述阻擋是**准入層**的，不是 OS 隔離層的。本機無 process 隔離：一個以使用者身分執行、且刻意惡意的行程能寫任何檔案。本設計的威脅模型是**「AI agent 走最便宜的路 / 宣稱完成」**，不是「取得使用者權限的敵意攻擊者」。在該模型下 12 項全部有確定性的機械拒斥；在敵意模型下，殘留風險是行程可同時偽造 ticket ledger 與 report —— 但需同時偽造 `ticket_seq` 單調 CAS、`checker_impl_digest`、以及可被第三方重放的 `evidence_digest`，且事後全可重算稽核。

**我把它列為 Phase B 的加固項（store 權限/ledger 加固），而不是 Phase A 的缺口**，因為 Phase A 的出場標準是「三案例不可被架構繞過」，而非「抵禦本機 root」。這一判斷是我做的，請人類覆核是否同意這條界線。

---

## 最終判定

# `READY_FOR_PHASE_B`

出場條件逐項核對：

- **B1–B6 全部機械化** — B1 → ACCEPTED 為不可寫的 derivation + CAS materialization；B2 → input/evidence 三分法 + ticket 先發 + identity 三元組解析 + replay_spec；B3 → frozen bundle 含 checker/oracle/golden/baseline/impact/risk/governance，TEST_DEFECT 有 TD-1/TD-2 准入與 frozen-oracle 強制升級；B4 → 七類准入謂詞 + G1–G10 單值決議；B5 → impact_rules + final-SHA 全覆蓋 + Task 單一 deliverable_sha；B6 → effective_risk 只升不降 + V0 六項前置（含 production checkout 與 AI_MANAGER_HOME）。
- **I1–I10 全部有明確處置**（逐項對照表，無一略過）。
- **12 個 adversarial attacks 全部 BLOCKED**，各有指名的單一機制。
- **三個 false-complete fixture 各有唯一 deterministic expected result**（含 F.1 的 I1 變體與 F.3 的 production 變體）。
- **未新增第二套 runtime truth** — 四個新物件皆為 Execution 附屬證據，Task counters 定義為對 Execution chain 的可重算 fold；沿用既有 `governance`/`lease`/`generation`/Session correlation 原語。
- **未與 PFP 或既有 acceptance-gate 搶終態** — R5 與 `production_write` 在結構上只能導出 `DEFERRED_TO_PFP`；`acceptance-gate/` 與 `CONTROLLED_ACCEPTANCE_GATE` 明令不沿用。

### 三件我要主動標記給人類決策（不影響 READY，但影響成本與體感）

1. **成本**：A.5 要求最終 SHA 上一次全 required gate 完整跑。這會讓每次合併變貴。這是為關掉 ledger 事故形狀而付的價。
2. **人類會被較早叫醒**：D.2 把 G4(UNKNOWN) 排在 G5(REGRESSION) 之前，以堵住「repair 換 SHA 使 UNKNOWN 蒸發」。正確性優先於效率，但體感上升級會變多。
3. **信任邊界**：上一節的 Phase B 加固項界線由我劃定，請覆核。

### 本輪未做（依指示）

未修改 repo / Drive / runtime home / 治理文件 / Skill；未 commit / push；未寫程式；未寫 memory 記錄（唯讀指示所致 —— 若要我把本輪結論存入 memory 或落到 Drive 稽核，請單獨授權）。