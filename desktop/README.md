# ADM Windows Launcher & System Tray Entry

A native Windows desktop entry point so users never have to remember commands, ports, URLs, or PowerShell scripts.

## Components

- `AdmTrayLauncher.ps1` -- Native Windows `.NET` System Tray application.
  - Sits in the Windows system tray with the "AI Development Manager" icon.
  - **Double-click:** Opens the active ADM Dashboard / Session Center.
  - **Right-click menu:**
    - `Open Dashboard` -- Opens Dashboard in default browser.
    - `Status` -- Displays real-time health status of Dashboard, Session Center, and Scheduled Tasks.
    - `Start / Restart Services` -- Triggers background Scheduled Tasks safely.
    - `Exit Launcher` -- Exits the tray icon (does not terminate background tasks).
  - **Single Instance:** Protected by named Mutex `Local\ADM_Windows_Tray_Launcher_Mutex`. Repeated launches simply focus/open Dashboard without creating duplicate tray icons or duplicate processes.
- `Start-ADM-Tray.vbs` -- Silent launcher that starts `AdmTrayLauncher.ps1` with hidden window style (no flashing console window).
- `Install-AdmStartup.ps1` -- Idempotent installer that creates Desktop, Start Menu, and Windows Startup shortcuts named **AI Development Manager**. No administrator privileges required.
- `Uninstall-AdmStartup.ps1` -- Removes the created shortcuts and startup entries.
- `AdmCommon.ps1` -- Shared status-gathering & health-diagnostic helpers.
- `Start-ADM.ps1` / `ADM-Status.ps1` / `Stop-ADM.ps1` / `Restart-ADM.ps1` -- Legacy CLI/script launchers.

## Quick Setup

Run in PowerShell:
```powershell
.\desktop\Install-AdmStartup.ps1
```
This registers ADM to start automatically on Windows login and creates Desktop & Start Menu shortcuts.
