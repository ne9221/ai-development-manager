' Flicker-free entry point for a "Status" shortcut, mirroring Start-ADM.vbs.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = """" & scriptDir & "\ADM-Status.ps1"""
shell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & target, 0, False
