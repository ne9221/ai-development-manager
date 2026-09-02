# Antigravity Provider Integration

Everything below was established by probing the real installation on this
machine (Antigravity IDE **1.107.0**, language server build
`agy_ls_release_branch/2.7`, 2026-09-02). Nothing here is taken from vendor
documentation or third-party articles.

## 1. Automation surface

**There is no `agy` binary.** A whole-machine search finds none, and the IDE
install ships no such executable. The real machine-readable surface is the
bundled language server:

```
%LOCALAPPDATA%\Programs\Antigravity IDE\resources\app\extensions\antigravity\bin\language_server_windows_x64.exe
```

`~/.gemini/antigravity-ide/bin/agentapi.bat` is a one-line shim that runs that
same executable with the `agentapi` subcommand. The subcommands are:

```
agentapi get-conversation-metadata <conversation_id>
agentapi new-conversation [--model=<flash_lite|flash|pro>] [--title=<title>] [--profile=<profile>] <prompt>
agentapi send-message [--title=<title>] <recipient_id> <content>
```

The IDE starts the language server with a per-run `--csrf_token <uuid>` and two
random loopback ports (HTTPS/gRPC and plain HTTP; the log line is
`Language server listening on random port at N for HTTPS (gRPC)` followed by
`... for HTTP`). Both the IDE UI and `agentapi` talk to it:

* **Connect-RPC JSON**: `POST http://127.0.0.1:<http_port>/exa.language_server_pb.LanguageServerService/<Rpc>`
  with header `x-codeium-csrf-token: <token>`.
* **`agentapi`**: gRPC (h2c) to the *plain HTTP* port, via environment
  `ANTIGRAVITY_LS_ADDRESS=127.0.0.1:<http_port>`, `ANTIGRAVITY_CSRF_TOKEN`,
  `ANTIGRAVITY_LS_VERSION`, `ANTIGRAVITY_PROJECT_ID`. Pointing it at the HTTPS
  port fails with `error reading server preface`.

No API key is involved. The Google OAuth token stays in the IDE's own storage
and is **never read by ADM**; the language server performs every authenticated
call. The CSRF token is a local IPC nonce (`randomUUID()` in the extension) --
ADM treats it as a secret anyway: never logged, never persisted, excluded from
`repr` and from every evidence dict (`manager/ag_language_server.py`).

Preference order actually implemented: official `agentapi` CLI > official
language-server status/quota RPCs > local durable readback (reconciliation
only). CDP / UI automation is not used at all.

## 2. Quota (working, live-verified)

`RetrieveUserQuotaSummary` and `GetUserStatus` answer over the RPC surface and
**consume no model turn**:

* `RetrieveUserQuotaSummary` -> two model groups ("Gemini Models", "Claude and
  GPT models"), each with a `weekly` and a `5h` bucket carrying
  `remainingFraction` + `resetTime`. Bucket ids: `gemini-weekly`, `gemini-5h`,
  `3p-weekly`, `3p-5h`.
* `GetUserStatus` -> account `email`/`name`, `planStatus.planInfo.planName`
  (Pro), prompt/flow credits, and per-model `quotaInfo.remainingFraction`.

protobuf JSON omits zero-valued scalars, so a bucket with **no**
`remainingFraction` but a `resetTime` is an *exhausted* bucket (0), not an
unknown one. `collectors/antigravity.py` encodes that rule explicitly.

The collector publishes a normal `schema/status.schema.json` v0.1 entry:
`source = antigravity_language_server_quota_summary`, `source_type = official`,
`confidence = official`, four windows named by bucket id (300 / 10080 minutes).
It is wired into `manager/refresh_status.py` with the same last-good contract
Codex and Claude use: a failed read never overwrites the previous entry, it
only records `metadata.refresh` with the classification (e.g.
`ide_not_running`), so the Dashboard can say *why* instead of a bare STALE.
`manager/quota_reader.py` and `manager/quota_forecast.py` list the source in
`RELIABLE_SOURCES`, so the existing `has_reliable_quota` / `has_usable_quota`
rules apply unchanged -- no second quota framework.

Requires the IDE (or its language server, kept alive by the IDE setting
`antigravity.persistentLanguageServer`, default off) to be running.

## 3. Execution (implemented, gated OFF -- route blocked on this build)

`manager/ag_cli_runner.py` implements the full adapter against the same
`prepare/start/wait/close` contract `CodexLauncher` and `ClaudeLauncher` use,
routed by the existing `manager/ag_runner.AgRunner` facade. No second
dispatcher, execution engine, session system or quota framework was created.

* **READY handshake** -- never "process exists, send prompt". `prepare()`
  requires: an existing absolute working directory; a discovered language
  server; `GetStatus` answering; `GetUserStatus` carrying a signed-in account;
  a non-exhausted quota group; the requested model mapping onto an `agentapi`
  model; a resolvable `agentapi` entrypoint; and a usable **dispatch route**
  (below). All read-only, no model turn.
* **Terminal truth** -- exit code 0 is never success. The conversation runs
  *inside* the language server (the CLI returns as soon as the conversation
  exists), so `wait()` polls `GetAllCascadeTrajectories`,
  `GetCascadeTrajectorySteps` and `GetCascadeTrajectoryExecutorMetadatas`.
  Success requires run status `CASCADE_RUN_STATUS_IDLE` **and** an executor
  record **and** a non-empty final `PLANNER_RESPONSE`. Distinct failure
  classifications: `empty_response`, `prompt_not_started`, `permission_stall`,
  `permission_required`, `quota_exhausted`, `token_budget_exceeded`,
  `max_invocations`, `provider_error`, `auth_transient`, `turn_timeout`,
  `malformed_provider_state`, `ls_unreachable`, `cancelled` -- never one
  blanket `provider_failed`.
* **Cancellation** -- `CancelCascadeInvocation` + `ForceStopCascadeTree`, plus
  `taskkill /F /T` of the CLI process tree (never just the parent PID), then
  reconciliation until the server reports IDLE. Cancellation evidence
  (`reason`, per-RPC result, whether the process tree was killed, final run
  status, confirmation time) is persisted and returned in the outcome stats.
* **Identity separation** -- ADM Task / ADM Execution / ADM Session /
  `thread_id` (the ADM-assigned provider_session_id) / AG `conversation_id` /
  language-server process identity are all distinct fields. An AG conversation
  can outlive one ADM execution.
* **Recovery** -- `manager/ag_run_state.py` keeps one JSON file per thread id
  under `AI_MANAGER_HOME/runtime/antigravity/runs/` (scratch state, never an
  SSOT) with conversation id, process identity, timestamps, last event, step
  cursor, transcript path, terminal state and cancellation evidence, so a
  restarted ADM can read a run back. Durable provider-side readback also
  exists: `~/.gemini/antigravity-ide/conversations/<id>.db` (SQLite) and
  `brain/<id>/.system_generated/logs/transcript.jsonl` -- used for
  reconciliation only, never as the control plane.
* **Permissions** -- no blanket permission skipping. A run waiting on a
  permission/question is classified `permission_required`, and after a bounded
  stall it fails `permission_stall` and is cancelled, rather than being granted
  everything to make headless work.
* **Workspace isolation** -- after dispatch the adapter reads the
  conversation's own workspace mapping. A provable mismatch with the ADM
  working directory cancels the run and fails `workspace_mismatch`; an
  unverifiable mapping refuses any non-read-only task (`workspace_unverified`).

### Why dispatch is gated off

On this build the official `agentapi new-conversation` route **cannot create a
conversation against an IDE-hosted language server**:

```
$ agentapi new-conversation --model=flash "<prompt>"          # ANTIGRAVITY_PROJECT_ID unset
{"error": "failed to start conversation: rpc error: code = Unknown desc = project_id is required when providing project_env_config"}

$ ANTIGRAVITY_PROJECT_ID=outside-of-project agentapi new-conversation ...
{"error": "failed to start conversation: rpc error: code = Unknown desc = projectsStore is nil, but projectEnvConfig was provided"}
```

`agentapi` always sends a `project_env_config`, and this language server has no
projects store: `ReadProject {}` -> `projects store not initialized`,
`IsProjectsEnabledInternally` -> false. That is a platform state, not an ADM
defect.

`manager/ag_language_server.probe_dispatch_route()` checks exactly that
precondition (read-only `ReadProject`, no model turn). It feeds:

* `OfficialAgCliRunner.prepare()` -> `AgLaunchError("dispatch_route_unavailable", ...)`
  instead of spawning a CLI that cannot succeed;
* `command_watcher.ag_availability_check()` -> **False**, so the Command
  Watcher never routes a Task to Antigravity while the route is blocked;
* `availability_snapshot()` -> `status: degraded`, `can_accept_new_task: false`,
  `reason: projects_store_unavailable`, while quota stays truthfully fresh and
  readable.

Quota visibility and dispatchability are deliberately separate facts.

### Direct-RPC route: investigated, rejected for now

The private `StartCascade` + `SendUserCascadeMessage` RPCs do create a
conversation without the projects store
(`StartCascade {"source": "CORTEX_TRAJECTORY_SOURCE_AGENT_API", "workspaceUris": [...]}`
returns a `cascadeId`, and the workspace mapping comes back correctly in
`GetConversationMetadata`). The model must be supplied as
`SendUserCascadeMessage.cascadeConfig.plannerConfig.requestedModel.model` using
the `MODEL_PLACEHOLDER_*` enum from `GetUserStatus`'s `clientModelConfigs`;
without it the executor writes
`failed to construct executor: neither PlanModel nor RequestedModel specified`.
With it, the executor gets one step further and fails
`agent executor error: earliest step index is out of bounds: 0 vs 0` -- the
user-input step is not persisted by the shapes tried.

This is an undocumented, uncommitted private schema. Rather than keep guessing
message shapes, ADM does **not** ship this route: no schema guard could
honestly claim stability, and the project rules forbid treating an unpromised
private schema as a control plane. The finding is recorded here so a future
task can resume from the exact blocking error.

## 4. Capability and routing

Antigravity keeps its existing entry in `manager/assignment.py`'s capability
registry (`mode: interactive`, browser-leaning traits) and its existing
provider registration in `command_watcher.PROVIDER_RUNTIMES`. It has **not**
been marked `repo_write_capable`: no isolated-worktree repo-write turn has been
proven through it, and `files_changed` for AG would still have to come from git
evidence, never from the agent's self-report.

## 5. Testing layers

* **Layer 1/2** (`manager/test_ag_language_server.py`,
  `manager/test_ag_cli_runner.py`, `collectors/test_antigravity.py`,
  `manager/test_ag_run_state.py`): pure parsers, discovery, the RPC client, the
  terminal-truth state machine, and the whole adapter driven by a scripted
  fake language server and fake process -- no real IDE, no sleeping, no quota.
* **Layer 3** (local compatibility smoke, no model turn):
  `python -m manager.ag_language_server` prints the redacted availability
  snapshot; `python -m collectors.antigravity` prints the status document.
* **Layer 4** (live, real quota): deliberately **not** part of the default
  suite. The live dispatch smoke is blocked by the route above; the live quota
  smoke is the Layer-3 command against a running IDE.
