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
