' Silent launcher for ADM System Tray Launcher
' Starts AdmTrayLauncher.ps1 without flashing a PowerShell console window.

Set ws = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
psScript = scriptDir & "\AdmTrayLauncher.ps1"
cmd = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & psScript & """"
ws.Run cmd, 0, False
