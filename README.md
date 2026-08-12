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
- `collectors/` - not yet implemented; see `collectors/README.md` for the
  planned per-provider sources.
- `docs/QUOTA-SOURCES.md` - PoC findings on where each provider's quota data
  can (or cannot) be read from.

## What lives on Google Drive instead (not in this repo)

Runtime state - `PROJECTS.md`, `AI-RESOURCE-STATUS/status.json`, `TASKS/`,
`HANDOFFS/`, `TASK-HISTORY/`, `CHANGELOG.md` - is the Drive folder's
responsibility, not this repo's. This repo holds code, schema, and version
history; Drive holds the live/mutable data. See `docs/DRIVE-STRUCTURE.md`
for the exact folder layout to create.

## Status

v0.1 - skeleton only. No collectors, no scheduler, no Drive/Sheets sync, no
task/handoff workflow yet.

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
python -m manager.executions start example-project example-task example-run --provider codex
python -m manager.executions finish example-project example-run --note "Acceptance criteria passed"
python -m manager.executions read example-project example-run
python -m manager.estimator example-project --task-type implementation --provider codex --mode code --effort medium
python -m manager.assignment --project-id example-project --task-type implementation --needs-repo-edit
```

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
python -m manager.runtime_bridge --project-id ai-development-manager --request "Continue Phase 9" --json
```

See `docs/CHATGPT-INTEGRATION.md` for the upper-level client contract, explicit
fallback behavior, shared-rule priority, and optional Ponytail policy.

## Working-tree preflight locks

Drive-backed logical leases prevent production writes from starting on the
same repository/branch or overlapping file scope. Read-only work remains
parallel-safe; expired and released leases do not block. This P0 is an explicit
CLI and is not yet wired into Dispatcher or Scheduler.

```powershell
python -m manager.worktree_locks check example-project task-1 run-1 --provider codex --repository https://github.com/example/repo --branch feature/a --baseline-head abc123 --scope manager/tasks.py
python -m manager.worktree_locks acquire lock-1 example-project task-1 run-1 --provider codex --repository https://github.com/example/repo --branch feature/a --baseline-head abc123 --scope manager/tasks.py
python -m manager.worktree_locks inspect example-project lock-1
python -m manager.worktree_locks list example-project
python -m manager.worktree_locks release example-project lock-1
```
