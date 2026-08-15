# Handoff — Claude hands-off overnight run (2026-08-15)

AI: Claude | Project: ai-development-manager | Task: P0 Claude hands-off production candidate
Session: claude-hands-off-overnight-20260815
Branch: `integration/claude-hands-off-overnight` (based on `feat/claude-launcher-adapter` @ `ef60373`)
Ending HEAD: `5d86fcd`

## Verdict: CLAUDE HANDS-OFF READY (single account, real-provider path)

The full chain — Task → Execution → real `ClaudeLauncher` → real `claude.exe` (v2.1.226) →
stream-json → Session Center correlation via real `claude agents --json` → automatic result
capture → terminalize → cleanup → claim release — is proven end-to-end with real evidence,
not mocks, including through the actual unattended dispatch path
(`manager/command_watcher.py::process_command()`, the same code the real
"AI Development Manager - Command Watcher" Scheduled Task runs every minute).

## Real bugs found and fixed (both on this branch, both minimal, both verified)

1. `manager/claude_launcher.py::_build_argv()` was missing `--verbose`, which real `claude.exe`
   requires whenever `--print --output-format stream-json` is used. Every real Claude launch
   would have failed at spawn before this fix. Commit `7ca8708`.
2. `manager/execution_runner.py::_stopped()` only duck-typed Codex's
   `PreparedLaunch._client.process` shape, never Claude's `PreparedLaunch._process` shape, so
   `terminalize_execution()` could never confirm a Claude process had stopped and would refuse
   to terminalize forever. Fixed with a fallback, mirroring the existing
   `_provider_session_id()` duck-typing pattern. Commit `5d86fcd`.

No other code changes were needed. Phases 2-10 of the overnight plan (hands-off dispatch,
automatic result/handoff capture, Session Center status gates, the 21-item failure/recovery
matrix, persistent/restart survival, 4 independent real dogfood runs, adversarial review,
3x soak regression) all passed against the already-existing, already-provider-neutral
lifecycle/recovery/claims/session-center machinery. See conversation transcript for full
per-phase evidence; nothing further is summarized in a separate doc to avoid duplication.

## Still pending user action (not applied autonomously, by design)

**PowerShell window flicker.** Root cause: both real Scheduled Tasks
("AI Development Manager - Command Watcher" and "...Session Center Supervisor") fire every
1 minute via `powershell.exe -WindowStyle Hidden`, which still briefly flashes a console on
many Windows builds. Fix built and dry-tested (confirmed hidden execution + correct exit-code
propagation) but NOT applied, since changing a live Scheduled Task's action is a system-state
change:
- `C:\Users\EE\.config\ai-development-manager\invisible_run.vbs` (the wrapper)
- `C:\Users\EE\.config\ai-development-manager\apply_invisible_task_actions.ps1` (run this,
  manually, to apply — it only changes the Action of the two existing tasks, nothing else)

**Second Claude account isolation.** Confirmed (read-only, no login/logout) that the
`CLAUDE_CONFIG_DIR` env var relocates Claude Code's entire config directory, which is the
correct mechanism for per-account isolation. Not wired into `ClaudeLauncher` — deferred per
plan (P0.1, after single-account hands-off, which is what this run delivered).

## How to actually use this (minimal real-use entry)

- **Submit work**: `python -m manager.runtime_bridge --request "<text>" --project-id <id>`
  (see `manager/runtime_bridge.py` — the existing "stable one-call runtime contract"; it
  dispatches/schedules through the same machinery this run verified). A dedicated `.ps1`
  wrapper was not built this run since `runtime_bridge.py`'s CLI already covers it minimally.
- **Watch status**: Session Center HTTP server on `127.0.0.1:8765` (`/api/session`, `/health`),
  spawned by the real Supervisor Scheduled Task.
- **Stop safely**: `Disable-ScheduledTask -TaskName "AI Development Manager - Command Watcher"`
  / `"...Session Center Supervisor"` (does not kill an in-flight execution; it just stops new
  polling cycles).
- **Failures**: per-execution `cleanup_evidence`/`terminal_reason` fields on the Execution
  record; raw process logs at `%TEMP%\claude-<session_id>.std{out,err}.log`.

## Recommended next step

Not a merge-to-main recommendation yet — this branch is verification-and-two-bugfixes, not a
finished feature; recommend a normal PR review of `7ca8708` + `5d86fcd` against
`feat/claude-launcher-adapter` (or direct merge if the team trusts this verification depth),
then separately decide on applying the Scheduled Task flicker fix and starting P0.1
(second-account wiring).
