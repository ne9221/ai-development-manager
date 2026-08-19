# ADM one-click desktop launcher

A Windows entry point so the user never has to open PowerShell manually.
Confirms/starts the existing production Scheduled Tasks and shows status --
it does not create any new background service and does not modify
execution lifecycle, launchers, or credentials.

## Files

- `AdmCommon.ps1` -- shared status-gathering + task-enable helpers, dot-sourced by the others.
- `Start-ADM.ps1` / `Start-ADM.vbs` -- confirm the Session Center Supervisor and
  Command Watcher Scheduled Tasks are enabled, kick one immediate cycle of
  each (safe: both tasks already use `MultipleInstances=IgnoreNew`, so this
  can never start a duplicate concurrent run), then open either the live
  Session Center dashboard (`http://127.0.0.1:8765/`, if an AI execution is
  currently active) or a static status page (if idle -- the normal state).
- `ADM-Status.ps1` / `ADM-Status.vbs` -- read-only version: same status view,
  never enables/triggers/disables anything. Safe to run at any time.
- `Stop-ADM.ps1` -- disables both Scheduled Tasks so no *new* work is picked
  up. Never kills an already-running AI execution or Session Center process.
- `Restart-ADM.ps1` -- `Stop-ADM.ps1` then `Start-ADM.ps1`. Same "never kills
  in-progress work" guarantee.
- `Start-Dashboard.ps1` -- opens the Streamlit Operations Dashboard
  (`dashboard.py`) in the browser: quota, active executions, task board,
  session/handoff inspector, and Watcher/Session Center health. Read-only,
  keeps a visible console running the Streamlit server (unlike the other
  scripts above, this one does not exit -- close the window to stop it).

## Desktop shortcut setup (one-time, per machine)

1. Right-click the desktop -> New -> Shortcut.
2. Target: `wscript.exe "C:\<repo path>\desktop\Start-ADM.vbs"`
3. Name it "Start ADM" (or similar). This is the one icon the user needs.
4. Optionally repeat for `ADM-Status.vbs` as a second "ADM Status" icon.

`wscript.exe` itself has no visible window, and it launches PowerShell with
`-WindowStyle Hidden` and a non-waiting `Run` call, so normal startup never
flashes a console window. `Stop-ADM.ps1`/`Restart-ADM.ps1` are power-user
actions (no default desktop icon) -- run them from an existing PowerShell
window when needed.

## Failure behavior

If a required Scheduled Task is missing entirely (not just disabled),
`Start-ADM.ps1` shows a plain Windows message box explaining exactly which
task wasn't found, instead of failing silently or leaving a stray console
window open.
