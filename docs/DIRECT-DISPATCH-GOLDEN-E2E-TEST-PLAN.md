# Direct Dispatch Golden E2E Test Plan

Status: **not yet run**. This is the final hands-off gate -- it requires the
infra delta in `docs/DIRECT-DISPATCH-DEPLOYMENT-PLAN.md` to be approved and
live (a deployed `adm-runtime-bridge` with `ADM_LOCK_GCS_BUCKET` set and
Drive write permission, plus a running desktop Command Watcher pointed at
the same bucket and an allowlist/quota-eligible provider account) before it
can execute for real. It is written now, against the actual tool/endpoint
names this branch ships, so it is ready to run the moment that infra lands.

## Scenario

The user says, in ChatGPT: *"建立一個 disposable read-only test task"*. No
prompt is copy-pasted, no Task/Command is hand-created, no watcher allowlist
entry is added, no Claude account is chosen, no provider is manually
started.

## Preconditions

- `docs/DIRECT-DISPATCH-DEPLOYMENT-PLAN.md` items 1-4 approved and deployed.
- ChatGPT has this repo's MCP endpoint configured as a connector/tool
  source (`adm_create_task` and `adm_task_status` visible to it) --
  configuring that connector is outside this repo's scope (product/plan
  gated per `docs/MCP-INTEGRATION.md`'s Authentication section) and is a
  precondition of this test, not a step it performs.
- The desktop Command Watcher (`manager/command_watcher.py`, e.g. via
  `manager/launch_task.ps1` or the one-click launcher) is running with
  `ADM_LOCK_GCS_BUCKET` set to the same bucket the Cloud Run service uses,
  and at least one provider account has a reliable/fresh quota signal.
- `ai-development-manager` (or another real project already in Drive
  `PROJECTS`) is used as `project_id`; no throwaway project needs to be
  created for this test.

## Steps and checkpoints

1. **Tool call accepted** -- ChatGPT calls `adm_create_task` with
   `project_id`, `title="Disposable read-only smoke test"`,
   `goal="Reply OK. Read only. Touch nothing."`, and a fresh `request_id`
   (e.g. `golden-e2e-<timestamp>`). Verify the tool response is
   `{"accepted": true, "request_id": ..., "task_id": "dispatch-<request_id>",
   "command_id": "dispatch-<request_id>", "status": "queued"}` -- no error.
2. **request_id persisted** -- read the GCS object at
   `dispatch-requests/<project_id>/<request_id>.json` (via
   `manager.dispatch_requests.dispatch_request_registry(...).read()`, or
   `gcloud storage cat`) and confirm it matches `task_id`/`command_id`.
3. **Task created** -- `adm_task_status` (or `python -m manager.tasks
   task-read <project_id> dispatch-<request_id>`) returns a Task with
   `read_only: true`, `execution_policies` containing all four of
   `disposable/read_only/no_repo_writes/no_external_writes`, and
   `source_context.origin == "direct_dispatch_ingress"`.
4. **Command queued** -- the same read shows a Command with
   `status: "queued"`, `created_via: "direct_dispatch_ingress"`,
   `admission_version: "v1"`, `request_id` matching.
5. **Auto-admission PASS** -- within one Command Watcher poll interval
   (`POLL_SECONDS`, default 60s), the Command's `status` moves off
   `"queued"` without any `ADM_WATCHER_ALLOWLIST_PATH` entry for
   `(project_id, dispatch-<request_id>)` -- this is the actual proof that
   `manager.trusted_ingress.verify_trusted_ingress_admission` passed. If it
   stays `"queued"` past several poll intervals with reason
   `not_allowlisted` in watcher logs, admission failed closed; treat that as
   a blocker, not a flake, and check the bucket/permission wiring first.
6. **Watcher claims** -- Command `status: "claimed"`, `claimed_at` set,
   `execution_id` set to `command-dispatch-<request_id>`.
7. **Dispatcher selects provider/account** -- Command's `provider` is one of
   `codex`/`claude`; `selection_reason`/`quota_evidence` are populated
   (already computed at ingress time by `manager.dispatcher.dispatch()`, but
   re-verify the field survived the launch transition).
8. **Auth preflight** -- for `provider: "claude"`, confirm no
   `AccountSelectionError`/`unknown_or_disabled_claude_account` rejection
   appears; for `codex`, confirm `codex login status` was satisfied before
   launch (existing `execution_runner`/`ClaudeLauncher`/`CodexLauncher`
   preflight, unmodified by this branch).
9. **Real provider spawn** -- an actual Codex/Claude process starts on the
   watcher's host; confirm via the existing per-launch local diagnostic log
   (`%LOCALAPPDATA%\AI Development Manager\logs`) or OS process list, not by
   trusting the Drive record alone.
10. **Session/Execution persisted** -- an Execution record exists at
    `command-dispatch-<request_id>` with `status` transitioning
    `reserved -> running -> completed`, `access: "read_only"` (never
    `production_write` for this task), and provider evidence
    (`host`/`pid`/`creation_identity`) populated.
11. **PC Dashboard visible** -- open the dashboard; the task appears on the
    "Ready" board immediately after step 3-4, moves to "In progress" during
    step 10's `running` state, and to "Completed" at step 13 -- per the
    exact lifecycle proven by
    `manager/test_dashboard_core.py::DirectDispatchDashboardVisibilityTests`.
12. **Provider replies OK** -- the provider's actual output/transcript
    (never logged/printed per this repo's existing safety rules) reflects
    the goal was satisfied: it replied and made no repo or external write.
13. **Cleanup release** -- Execution `cleanup_evidence` shows
    `task_claim_release: "released"` and (for `access: "read_only"`)
    `writer_release: "not_required"`; the task claim registry no longer
    holds a live claim for this task.
14. **Task status completed** -- Task `status: "completed"`,
    `completed_at` set, Command `status: "completed"` with
    `result.status == "completed"`.
15. **ChatGPT can read back the result** -- `adm_task_status` called with
    the same `request_id` returns the completed Task/Command status and a
    `current_progress`/`result` summary ChatGPT can relay to the user
    without any further manual Drive/dashboard lookup.

## Pass/fail

Pass requires all 15 checkpoints to hold for one run, plus: no manual step
(prompt copy, Task/Command creation, allowlist edit, account selection, or
provider start) was performed by a human at any point after the initial
ChatGPT message. Any checkpoint failing closed (e.g. step 5 staying
`not_allowlisted`) is a blocker to fix before re-running, not something to
route around by hand-launching the command -- doing so would prove the
manual path still works, not that hands-off dispatch does.

## Relationship to the automated test suite

Every checkpoint above already has unit/integration coverage that does not
require live infra or a real provider (see the full suite: 813 passed as of
this writing):

- 1-5: `cloud/test_dispatch_ingress.py`, `cloud/test_dispatch_route.py`,
  `manager/test_dispatch_requests.py`,
  `manager/test_command_watcher.py::TrustedIngressAdmissionTests`,
  `manager/test_mcp_adapter.py::MCPCreateTaskToolTests`.
- 6-10: `manager/test_command_watcher.py` (existing claim/launch/reconcile
  coverage, provider-agnostic), `manager/test_execution_runner.py`,
  `manager/test_dispatcher.py`.
- 11: `manager/test_dashboard_core.py::DirectDispatchDashboardVisibilityTests`.
- 13-14: existing `manager/test_command_watcher.py`
  completion/reconciliation tests (unchanged by this branch) plus
  `manager/test_mcp_adapter.py::MCPTaskStatusToolTests`.
- 15: `manager/test_mcp_adapter.py::MCPTaskStatusToolTests`.

The golden E2E run is the one thing that automated coverage cannot
substitute for: proof that the real, deployed, authenticated path -- with a
real GCS bucket, a real Drive write permission, a real desktop watcher, and
a real provider -- behaves the same way the fakes/mocks say it should.

## Adversarial coverage already in the automated suite (not re-run live)

These are exercised as unit/integration tests against fakes, not as part of
the live golden run above, since they require no real infra to prove:

| Adversarial case | Where it is proven |
|---|---|
| Forged trusted-ingress metadata (self-declared, no/mismatched idempotency record) | `manager/test_command_watcher.py::TrustedIngressAdmissionTests::test_forged_created_via_without_any_idempotency_record_never_admitted`, `::test_forged_created_via_with_mismatched_idempotency_record_never_admitted` |
| Duplicate concurrent dispatch | `cloud/test_dispatch_ingress.py::test_simultaneous_duplicate_requests_create_exactly_one_task_and_command`, `::test_retry_after_ambiguous_transport_failure_still_completes_creation` |
| Invalid auth | `cloud/test_dispatch_route.py::test_missing_auth_rejected`, `::test_invalid_auth_rejected` |
| Request replay | `cloud/test_dispatch_ingress.py::test_duplicate_request_id_does_not_create_two_tasks_or_commands` |
| Non-read-only request | `cloud/test_dispatch_ingress.py::test_read_only_false_is_rejected_outright` |
| Write-policy injection | `manager/test_command_watcher.py::TrustedIngressAdmissionTests::test_injected_write_policy_never_admitted` |
| Provider/account injection | `cloud/test_dispatch_ingress.py::test_executable_and_env_and_config_path_fields_rejected`, `manager/test_mcp_adapter.py::MCPCreateTaskToolTests::test_tool_only_accepts_the_four_narrow_fields` |
| Direct-spawn spy = 0 | `cloud/test_dispatch_route.py::test_no_direct_provider_spawn_from_http_handler`, `manager/test_mcp_adapter.py::MCPAdapterTests::test_no_direct_provider_spawn_from_mcp_adapter` |
| Normal untrusted task remains blocked | `manager/test_command_watcher.py::TrustedIngressAdmissionTests::test_ordinary_untrusted_command_off_allowlist_still_rejected`, `::test_no_gcs_bucket_configured_fails_closed_even_with_full_evidence` |
