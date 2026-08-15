# Disables the two ADM Scheduled Tasks so they stop picking up new work.
#
# Deliberately does NOT kill any currently-running child process (Session
# Center, a live Claude/Codex execution, etc.) -- disabling a Scheduled Task
# only prevents its *next* trigger from firing; an already-running instance,
# or an already-spawned provider process being supervised by it, is left
# completely alone. Stopping the automation layer is not the same as
# stopping in-progress AI work, and this script never does the latter.

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "AdmCommon.ps1")

foreach ($name in @($AdmSupervisorTask, $AdmWatcherTask)) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Output "SKIP (not found): $name"
        continue
    }
    if ($task.State -eq "Disabled") {
        Write-Output "ALREADY DISABLED: $name"
        continue
    }
    Disable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
    Write-Output "DISABLED: $name"
}

Write-Output ""
Write-Output "Note: any AI execution already in progress keeps running -- only new launches are now prevented."
