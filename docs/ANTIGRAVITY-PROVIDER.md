# Antigravity Provider Integration

Everything below was established by probing the real installation on this
machine (Antigravity IDE **1.107.0**, language server build
`agy_ls_release_branch/2.7`, 2026-09-02 and 2026-09-05). Request shapes were
cross-checked against public language-server clients but only kept once they
worked on this machine; nothing here is taken from vendor marketing or from an
unprobed version number.

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

## 3. Execution: transport `ide_bridge` (working, live-verified 2026-09-05)

`manager/ag_cli_runner.OfficialAgCliRunner` implements the adapter against the
same `prepare/start/wait/close` contract `CodexLauncher` and `ClaudeLauncher`
use, routed by the existing `manager/ag_runner.AgRunner` facade. No second
dispatcher, execution engine, session system or quota framework was created.
The adapter has two transports; the one actually used is written into every
run-state record and into the execution outcome `stats.transport`:

| transport | AgRunner mode | how a task reaches AG | status on this build |
|---|---|---|---|
| `ide_bridge` (default) | `live_ide` | the IDE-hosted language server's own cascade RPCs | **works** (live 2026-09-05) |
| `agentapi` | `cli` | official `agentapi new-conversation` CLI | blocked: needs a projects store |

### `ide_bridge` dispatch sequence (verified live on IDE 1.107.0)

```
discover_language_server()      -> the app-level cascade host (see below), CSRF nonce in memory only
GetStatus / GetUserStatus /     -> READY handshake, account, quota, live model catalog
RetrieveUserQuotaSummary
GetAllCascadeTrajectories       -> dispatch-route probe (cascade subsystem answers)
AddTrackedWorkspace {workspace: <abs path>}
StartCascade {source: CORTEX_TRAJECTORY_SOURCE_AGENT_API, workspaceUris: [file:///...]}
                                -> {cascadeId}  == AG conversation id (always NEW, never adopted)
GetConversationMetadata         -> workspaces[].workspaceFolderAbsoluteUri must equal the ADM working directory,
                                   checked BEFORE the first model turn (mismatch => cancel + workspace_mismatch)
SendUserCascadeMessage {cascadeId, items: [{text}], metadata: {ideName, ideVersion, extensionName, locale},
                        cascadeConfig.plannerConfig: {conversational: {plannerMode: DEFAULT, agenticMode: true},
                                                      requestedModel: {model: MODEL_PLACEHOLDER_*}}}
poll GetAllCascadeTrajectories / GetCascadeTrajectorySteps / GetCascadeTrajectoryExecutorMetadatas
                                -> terminal truth (unchanged classifier)
```

Two things the earlier (2026-09-02) attempt got wrong and this build proves:
the user turn goes in top-level `items: [{text}]`, not in a `step` object, and
the model is the server's own `MODEL_PLACEHOLDER_*` enum read from
`GetUserStatus.cascadeModelConfigData.clientModelConfigs[].modelOrAlias.model`
(`manager/ag_language_server.resolve_model_placeholder`; an unknown or
exhausted model refuses to launch -- nothing is guessed). No `apiKey` and no
tool-policy widening are sent: the server applies its own defaults, which on
this build auto-apply file edits (`toolConfig.code.applyEdits: true`) -- the
live smoke created `pong.txt` in the disposable workspace with no permission
prompt. Commands stay under the IDE's own permission policy.

**Which language server.** The IDE starts two per window: the app-level one
(no `--workspace_id`) hosts every cascade, the per-workspace one
(`--enable_lsp --workspace_id ...`) answers the cascade RPCs with an empty
map. `parse_command_line` labels them `cascade_host` / `workspace_lsp` and
discovery prefers the cascade host (then the newest). The role and workspace
id are part of the endpoint evidence.

**Identity separation and the binding invariant.** ADM Task / ADM Execution /
ADM Session / `thread_id` (the ADM-assigned provider_session_id, `ag-live-*`)
/ AG `conversation_id` (= `cascadeId`) / AG executor `provider_run_id`
(`executorMetadata[].executionId`) / language-server process identity are all
distinct fields in the run state. `ONE_EXECUTION_ONE_ACTIVE_AG_BINDING`: a
launch always creates a fresh cascade and records a `binding` record; a
cascade id already held by another non-terminal run state fails the launch
`binding_ambiguous` without touching that cascade (it is somebody else's live
session -- never cancelled, never adopted).

**Everything else is unchanged from the CLI transport**: READY handshake
before any side effect, exit-code-free terminal truth (IDLE + executor record
+ non-empty final `PLANNER_RESPONSE`), distinct failure classifications,
`CancelCascadeInvocation` + `ForceStopCascadeTree` + reconciliation (no CLI
process tree to kill on this transport -- `cancel_evidence.cli_process_killed`
stays false), run-state readback under `AI_MANAGER_HOME/runtime/antigravity/runs/`,
permission stalls failing closed instead of being auto-granted. Dispatch-time
RPC failures are normalized by `_classify_rpc_failure`: `quota_exhausted`,
`auth_transient`, `ls_unreachable`, `malformed_output`, `dispatch_failed`,
plus `workspace_bind_failed` (AddTrackedWorkspace refused) and
`binding_ambiguous`.

### `agentapi` transport: still blocked on this build

Re-verified 2026-09-05 on both language servers: `ReadProject {}` ->
`projects store not initialized`, `IsProjectsEnabledInternally` -> `{}`. The
official CLI always sends a `project_env_config`, so
`probe_dispatch_route(client, transport="agentapi")` keeps failing closed with
`projects_store_unavailable` and `OfficialAgCliRunner(transport="agentapi")`
refuses to spawn a CLI that cannot succeed. The CLI code path and its tests are
kept for the day the projects store exists.

### Headless / PTY fallback: not available on this build

The bundled `language_server_windows_x64.exe --help` (agy_ls_release_branch/2.7)
exposes no `-cli`, `-print`, `-agent_mode` or `-dangerously-skip-permissions`
flag; the public "agy" harnesses that use those target the separate
Antigravity CLI binary, which is not installed here (`~/.gemini/antigravity-cli`
absent, no `agy` on PATH). A `transport=agy_pty` therefore cannot be probed,
let alone built, on this machine -- it is recorded as unavailable rather than
stubbed.

## 4. Capability and routing

Antigravity keeps its existing entry in `manager/assignment.py`'s capability
registry and its existing provider registration in
`command_watcher.PROVIDER_RUNTIMES` (`launcher_factory: AgRunner`,
`quota_check: ag_availability_check`). `ag_availability_check` requires fresh
official SSOT quota **and** a discoverable language server **and**
`probe_dispatch_route()` -- which now defaults to the `ide_bridge` route, so
with the IDE running the Command Watcher may route a Task to Antigravity.
`AgRunner`'s hybrid order is IDE bridge first, `agentapi` CLI as the fallback
(only for `live_ide_not_found` / `live_ide_transport_unavailable`).
`manager/ag_ide_bridge.AgIdeBridge` (the older fail-closed IPC stub) is no
longer on the path.

Antigravity has **not** been marked `repo_write_capable`: `files_changed` for AG
comes from git evidence (`enforce_allowed_paths` / `capture_repo_write_evidence`
in `execution_runner`), never from the agent's self-report, and the bounded
repo-write admission for AG is a separate milestone (M4).

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
  suite, and the suite cannot reach it by accident: `conftest.py` fences the
  real process/loopback entry points for every test that is not marked
  `live_antigravity` (three ordinary unit tests dispatched three real model
  turns on 2026-09-05 the moment the bridge became real -- that fence is the
  postmortem). `python -m manager.ag_live_smoke` runs the minimal live
  dispatch: disposable git repo, one small file-creating task, adapter
  terminal state, then **independent** `git status`/diff verification.
