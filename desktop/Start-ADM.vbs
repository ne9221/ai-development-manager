' Flicker-free entry point for the desktop shortcut: wscript.exe itself has
' no visible window, and launching powershell.exe with window style 0
' (hidden) plus the `False` "wait" argument keeps the console fully
' invisible for the whole run. Point a desktop shortcut's Target at:
'   wscript.exe "<this file's full path>"
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = """" & scriptDir & "\Start-ADM.ps1"""
shell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & target, 0, False
