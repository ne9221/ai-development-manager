# AI Development Manager

Independent, cross-project management layer for coordinating multiple AI
coding tools (ChatGPT, Claude Code, Codex, Antigravity, Gemini, ...).

This repo is **not** a runtime dependency of any business project. If this
repo/layer disappears, every business project must remain independently
developable.

## What lives here

- `AI-DEVELOPMENT-RULES.md` - the cross-project rules SSOT (versioned).
- `schema/status.schema.json` - provider-neutral quota/usage status schema
  (v0.1), plus `schema/status.example.json` showing real PoC-derived values
  for Codex / Claude Code / Antigravity / Gemini App.
- `collectors/` - provider quota collectors and Drive publication support;
  see `collectors/README.md` for supported sources and setup.
- `docs/QUOTA-SOURCES.md` - PoC findings on where each provider's quota data
  can (or cannot) be read from.
- `docs/PHASE-3C-EXECUTION-LIFECYCLE.md` - Phase 3C lifecycle scope,
  acceptance criteria, blockers, and implementation slices.

## What lives on Google Drive instead (not in this repo)

Google Drive is the runtime SSOT for Projects, Tasks, Executions, Sessions,
Handoffs, task history, and quota/runtime records. This repo holds code,
schemas, tests, documentation, and Git version history. GCS is the
authoritative concurrency/ownership surface for per-task execution claims and
the production writer registry. See `docs/DRIVE-STRUCTURE.md` for the Drive
layout.

## Status

Phase 3C real-use candidate. The first real Windows desktop-to-Codex read-only
execution completed end to end, so automatic single-task Codex execution is
dogfood-ready. The production-write path has authority primitives and test
coverage but has not completed dedicated real production-write dogfood. This
integration branch still requires final adversarial review and closure before
merging to `main`.

```text
Google Drive ready Task -> desktop launcher -> quota/dispatch -> reservation
-> running gate -> GCS task claim -> Codex app-server -> canonical Session
-> terminal Execution/Handoff/Task persistence -> authority cleanup
```

## Unified quota reader and assignment

From the repository root, read the Google Drive runtime SSOT or produce a
task recommendation with:

```powershell
python -m manager.quota_reader --max-age-minutes 60
python -m manager.assignment --task-type implementation --expected-minutes 20 --needs-repo-edit
```

## Runtime quota refresh (Windows)

`python -m manager.refresh_status` reads the current Drive SSOT, refreshes the
available official providers, validates the merged document, and updates the
same Drive `status.json`. Runtime output, the lock, OAuth token, and logs live
under the repo-external `AI_MANAGER_HOME` directory.

`manager/install_scheduler.ps1` installs the hidden
`AI Development Manager - Quota Refresh` task for the current user. It runs at
Windows logon and every 15 minutes; overlapping executions are ignored by Task
Scheduler and rejected by the runtime file lock. The task does not require an
open terminal or an active Codex/Claude conversation window.

## Tasks and handoffs

Drive-backed runtime records use `schema/project.schema.json`,
`schema/task.schema.json`, and `schema/handoff.schema.json`:

```powershell
python -m manager.tasks project-put templates/project.json
python -m manager.tasks task-create templates/task.json
python -m manager.tasks task-read example-project example-task
python -m manager.tasks task-update example-project example-task --status in_progress --progress "Started"
python -m manager.tasks handoff-create templates/handoff.json
python -m manager.tasks handoff-latest example-project example-task
python -m manager.tasks task-complete example-project example-task --summary "Acceptance criteria passed"
```

Task creation records the current assignment recommendation and quota evidence,
but never starts an AI provider.

## Drive authentication health

Normal Drive reads do not start a browser authorization flow. Check
non-sensitive authentication health first, then explicitly authorize only when
required:

```powershell
python -m manager.drive_auth status
python -m manager.drive_auth authorize
```

## Codex session preview

Session preview is read-only. It does not alter Codex session files, create a
local registry, or write `SESSIONS` in Drive. When Drive credentials are
available, export the minimal project classification input to stdout and keep
any redirected file temporary:

```powershell
python -m manager.sessions export-project-preview > project-preview.json
python -m manager.sessions preview-codex --projects-file project-preview.json
python -m manager.sessions preview-codex --projects-file project-preview.json --needs-review
```

The snapshot contains only `project_id`, `name`, `aliases`, `repo`, and
`working_directory`; it never contains credentials, task data, handoffs, or
session transcripts.

## Thin Windows Session Center

Show one live Codex session in a minimal localhost UI without AASC, native Node
modules, or a scheduled watcher:

```powershell
python -m manager.session_center --provider-session-id <native-thread-id> `
  --project-id <project> --task-id <task> --branch <branch>
```

Open `http://127.0.0.1:8765`. Pass `--execution-file <execution.json>` instead
of manual project/task metadata to enable deterministic ADM correlation; the
provider session ID and cwd must match exactly. See
`docs/M0-GATE0-THIN-SESSION-CENTER.md` for the Windows Gate 0 evidence.

For a visible ADM launch, start the page before the runner; it waits for the
authoritative Execution session link and correlates without a local metadata
copy:

```powershell
python -m manager.session_center --execution-project-id ai-development-manager `
  --execution-id <execution-id> --wait-seconds 180
```

## Continuation context pack

Read bounded continuation context from Drive without writing session data:

```powershell
python -m manager.context_pack --project-id ai-development-manager --request "continue session organizer" --json
```

The pack includes the project, active task, latest handoff, all shared rules,
and at most five recent session metadata records. It deliberately excludes full
provider transcripts and directs the next AI to resume current progress and
the handoff before exploring completed work.

## Development Overview

The Drive-backed Development Overview is a compact project management view; it
does not automatically synchronize with TASKS or HANDOFFS:

```powershell
python -m manager.overview init ai-development-manager
python -m manager.overview read ai-development-manager
python -m manager.overview summary ai-development-manager
python -m manager.overview item-add ai-development-manager P06 --title "Example work"
python -m manager.overview item-update ai-development-manager P06 --status awaiting_validation --progress "Implementation complete" --next-action "Run validation"
```

## Execution metrics and estimates

Execution records capture elapsed time and quota snapshots before and after a
run. Missing windows and quota resets produce unknown deltas instead of guessed
usage. Records remain in Drive under `EXECUTIONS/<project_id>/`.

```powershell
python -m manager.executions reserve example-project example-task example-run --provider codex --quota-evidence-json $env:CURRENT_QUOTA_EVIDENCE_JSON
# After manager.execution_lifecycle.enter_running_gate() and the later supervised provider lifecycle:
python -m manager.executions finish example-project example-run --note "Acceptance criteria passed"
python -m manager.executions read example-project example-run
python -m manager.estimator example-project --task-type implementation --provider codex --mode code --effort medium
python -m manager.assignment --project-id example-project --task-type implementation --needs-repo-edit
```

The legacy direct `start` command is retired. Production running transitions use
`manager.execution_lifecycle.enter_running_gate()` after reservation and
authoritative writer acquire.

## Start one Codex task execution

With Drive auth and the existing GCS lock/claim environment configured, start
exactly one ready task through dispatch, reservation, the running gate, Codex,
and terminal persistence:

```powershell
python -m manager.execution_runner ai-development-manager phase-7-task --execution-id phase-7-run-1
```

Stdout is one machine-readable JSON object. Exit code `0` means `completed`;
`1` means the run failed, was interrupted, or could not start. Provider prompt,
transcript, stderr, and raw provider error details are never printed or stored.

The Task must be in a valid `ready` state. Startup RPCs use a separate bounded
timeout from turn completion. The turn timeout is derived from
`expected_minutes`, with a 20-minute maximum; `--timeout-seconds` explicitly
overrides it but remains subject to that maximum.

### Windows prerequisites

- Python with the Google authentication dependencies and
  `google-cloud-storage` available.
- Node.js and Codex installed and authenticated (`codex login status`).
- `CODEX_BIN` may point to a standard npm `codex.cmd`. The manager resolves
  that shim to its packaged native `codex.exe`, which owns app-server pipes.
- `ADM_LOCK_GCS_BUCKET` is required for the per-task execution claim.
- Production-write execution additionally requires `ADM_LOCK_GCS_OBJECT` for
  the writer registry.

Use environment-specific resource names and normal Google credentials; never
put tokens, credential contents, or production bucket names in this repo.

## Windows desktop launcher

`manager\launch_task.ps1` is a shortcut-compatible thin wrapper around the
command above. Double-click it or make a shortcut that targets PowerShell with
`-File` and it will ask for a project and task ID. It displays only starting,
completed, or failed/interrupted status; lifecycle authority remains entirely in
the Python runner. Per-launch safe diagnostic summaries are local-only under
`%LOCALAPPDATA%\AI Development Manager\logs`; Drive and GitHub remain SSOT.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\manager\launch_task.ps1 `
  -ProjectId '<project>' `
  -TaskId '<task>' `
  -ExecutionId '<execution-id>'
```

The launcher owns no lifecycle, claim, cancellation, or writer authority. Its
stdout and local diagnostic record contain only bounded safe status. Raw
prompts, transcripts, stderr, credentials, and provider errors are not
persisted.

## Retry lifecycle

A reservation that provably never started may transition from `reserved` to
`cancelled` through `manager.executions.cancel_reserved_execution`; it cannot
masquerade as completed, failed, or interrupted. A blocked Task linked to an
interrupted or failed execution may return to `ready` through
`manager.executions.prepare_task_retry` only after authoritative terminal
persistence and claim cleanup are complete, including writer release for
production-write work. Resolve any other running or reserved execution first.
Do not edit Drive JSON or delete a GCS claim manually to bypass these gates.

Terminal cleanup is fail-closed: the provider must be proven stopped before
terminal persistence and authority release. Read-only executions require the
per-task GCS claim but no writer lease. Production-write executions require
both the task claim and repository writer lease. Manual claim deletion is not
a normal recovery mechanism.

The estimator uses medians from similar completed executions. Estimates over
20 minutes recommend multiple phases; they do not split or execute tasks.

## Dispatcher and AI-ready prompt

The dispatcher reads the Drive project/task/latest handoff, current quota, and
execution estimates, then creates a short recommendation and paste-ready prompt.
It never starts or sends work to an AI provider.

```powershell
python -m manager.dispatcher --project-id ai-development-manager --title "Phase 7 dispatcher" --task-type implementation --complexity medium --scope "Implement dispatcher" --acceptance "Tests pass"
python -m manager.dispatcher --project-id ai-development-manager --task-id phase-7-dispatcher --title "Phase 7 dispatcher" --task-type implementation --complexity medium --json
```

Existing tasks prioritize `current_progress`, `next_action`, and the latest
compact handoff. Requests estimated above 20 minutes emit a bounded phase plan
and instruct the selected provider to execute Phase 1 only.

## Multi-task scheduling

The scheduler groups ready Drive tasks into parallel-safe batches. It respects
dependencies, edit/read-only state, working tree and allowed-path conflicts,
one provider slot per batch, quota freshness, and near resets. Every scheduled
item contains the unchanged Dispatcher result and generated prompt; nothing is
started or sent automatically.

```powershell
python -m manager.scheduler --project-id ai-development-manager `
  --task-id implementation-task --task-id architecture-review `
  --task-id docs-update --task-id dependent-tests
```

Low reliable quota with a reset within 30 minutes may return
`defer_until_reset` for non-high-priority work. Unknown quota stays eligible
with a warning rather than being treated as zero or abundant.

## ChatGPT runtime bridge

The stable one-call bridge resolves project aliases, reads current Drive quota,
active task/latest handoff and execution history, then reuses Dispatcher or
Scheduler. JSON output is compact and transport-neutral.

```powershell
python -m manager.runtime_bridge status --json
python -m manager.runtime_bridge --project-id ai-development-manager --request "Continue Phase 9" --json
```

The `status` command is the provider-neutral quota-only boundary. It reads the
same Drive `AI-RESOURCE-STATUS/status.json` SSOT, returns only Codex and Claude
normalized windows/source/freshness fields, and reports `known`, `unknown`,
`stale`, or `unavailable` without inventing a percentage. `known` requires a
fresh, verified official source; `unknown` never means 0%. `stale` preserves a
verified snapshot older than the configured freshness limit, while malformed,
future-dated, missing, or unreadable data is `unavailable`.

External source labels are fixed safe values, and provider window names are
allowlisted or replaced with stable generic names. Every window has `name`;
`duration_minutes`, `used_percent`, `remaining_percent`, and `resets_at` are
optional because providers expose different shapes. Duplicate names preserve
the first window and output is capped at eight windows per provider.

ChatGPT cannot call this local CLI directly yet. A future transport needs only
to call the self-contained `manager.runtime_bridge.read_runtime_status`
function; no transport, MCP, HTTP, or connector is implemented by this phase.
This contract assumes write access to the Drive `AI-RESOURCE-STATUS/status.json`
SSOT is controlled; the file is downloaded and parsed before per-provider/window
caps apply.

See `docs/CHATGPT-INTEGRATION.md` for the upper-level client contract, explicit
fallback behavior, shared-rule priority, and optional Ponytail policy.

## Working-tree preflight locks

Working-tree P0 uses one pre-provisioned Google Cloud Storage JSON object and
object-generation preconditions as the authoritative compare-and-swap boundary. The lock is
deliberately coarse: one production writer per canonical GitHub repository.
Scope is validated repo-relative metadata, not an arbitration boundary.

`check` is advisory only. Only a successful `acquire` authorizes writing.
Read-only work takes no writer lease and cannot upgrade one; before any write it
must pass local Git preflight and call `acquire`. The preflight requires the
clone origin, full branch ref, and current HEAD to match the request. Globs,
absolute paths, unresolved `.`/`..`, detached HEAD, non-GitHub remotes, and
credential-bearing URLs fail closed.

Set `ADM_LOCK_GCS_BUCKET` and `ADM_LOCK_GCS_OBJECT`, then provision the registry
once with `ifGenerationMatch=0`. Credentials use Application Default
Credentials; Cloud Run's service account needs bucket-scoped Storage Object
User (`roles/storage.objectUser`). `acquire` returns a lease token and does not
require a provider session. `link-session` may attach that metadata later
without changing ownership or reacquiring. Project, task, execution, provider,
and token remain the authoritative owner tuple. Existing records with a session
remain readable without migration. Store the token privately and provide it
through `AI_MANAGER_LEASE_TOKEN` for retry, renew, or release. GCS stores only
its SHA-256 digest, and `inspect`/`list` never return either token form. The
default lease is 60 minutes, bounded to 120
minutes; an owner must renew before expiry. Expired leases cannot be revived.
The execution runner uses this registry for production-write authority.
Dispatcher and Scheduler describe or group work but do not independently
acquire writer authority or start providers.

```powershell
$env:ADM_LOCK_GCS_BUCKET = "LOCK_BUCKET"
$env:ADM_LOCK_GCS_OBJECT = "worktree-locks/global-registry.json"
python -m manager.worktree_locks registry-init
python -m manager.worktree_locks check example-project task-1 run-1 --provider codex --repository https://github.com/example/repo.git --branch refs/heads/feature/a --baseline-head 0123456789abcdef0123456789abcdef01234567 --scope manager/tasks.py --working-directory C:\work\repo
python -m manager.worktree_locks acquire example-project task-1 run-1 --provider codex --repository git@github.com:example/repo.git --branch refs/heads/feature/a --baseline-head 0123456789abcdef0123456789abcdef01234567 --scope manager/tasks.py --working-directory C:\work\repo
$env:AI_MANAGER_LEASE_TOKEN = "TOKEN_FROM_ACQUIRE"
python -m manager.worktree_locks renew repo-SHA256 example-project task-1 run-1 --provider codex
python -m manager.worktree_locks link-session repo-SHA256 example-project task-1 run-1 --provider codex --session-id codex:session-1
python -m manager.worktree_locks inspect repo-SHA256
python -m manager.worktree_locks list --project-id example-project
python -m manager.worktree_locks release repo-SHA256 example-project task-1 run-1 --provider codex
```

For renames, callers must include both old and new repo-relative paths. P0 does
not resolve symlinks or infer generated outputs; use scope `.` when those effects
cannot be enumerated. The coarse repository lease remains the safety boundary.

## Current limitations and next work

- Codex single-task execution has completed real read-only Windows dogfood;
  production-write real E2E still needs a dedicated safe dogfood run.
- A Claude automatic runner is not part of the current Phase 3C real-use path.
- Automatic review and CI-feedback loops are next-stage work.
- A richer GUI, installer, scheduler UI, and tray application are not current
  core requirements; `manager/launch_task.ps1` remains the desktop entrypoint.
- Antigravity remains a manual channel rather than an automatic runner.
- Runtime schema compatibility and migrations require ongoing governance as
  the Drive SSOT evolves.
- Development remains real-use-first: dogfood should expose the next concrete
  need before more machinery is added.
