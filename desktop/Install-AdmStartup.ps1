# Idempotent installer for ADM Windows Startup & Desktop / Start Menu shortcuts.
# No administrator privileges required.

param(
    [string]$RepositoryPath = $(Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$desktopDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
$programsDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
$startupDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)

$vbsPath = Join-Path $PSScriptRoot "Start-ADM-Tray.vbs"
if (-not (Test-Path -LiteralPath $vbsPath)) {
    throw "Target VBS launcher not found at: $vbsPath"
}

$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
if (-not (Test-Path -LiteralPath $wscript)) {
    $wscript = "wscript.exe"
}

$wshShell = New-Object -ComObject WScript.Shell

function Create-AdmShortcut {
    param(
        [string]$ShortcutPath,
        [string]$TargetPath,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$Description
    )
    $shortcut = $wshShell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = $Description
    $shortcut.Save()
    Write-Host "Created/Updated shortcut: $ShortcutPath" -ForegroundColor Green
}

$shortcutName = "AI Development Manager.lnk"
$args = "`"$vbsPath`""
$desc = "AI Development Manager - System Tray & Dashboard Launcher"

# 1. Desktop Shortcut
$desktopShortcut = Join-Path $desktopDir $shortcutName
Create-AdmShortcut -ShortcutPath $desktopShortcut -TargetPath $wscript -Arguments $args -WorkingDirectory $PSScriptRoot -Description $desc

# 2. Start Menu Programs Shortcut
$programsShortcut = Join-Path $programsDir $shortcutName
Create-AdmShortcut -ShortcutPath $programsShortcut -TargetPath $wscript -Arguments $args -WorkingDirectory $PSScriptRoot -Description $desc

# 3. Windows Startup Folder (runs on user login)
$startupShortcut = Join-Path $startupDir $shortcutName
Create-AdmShortcut -ShortcutPath $startupShortcut -TargetPath $wscript -Arguments $args -WorkingDirectory $PSScriptRoot -Description $desc

Write-Host "ADM Windows Startup & Shortcut installation complete!" -ForegroundColor Cyan
