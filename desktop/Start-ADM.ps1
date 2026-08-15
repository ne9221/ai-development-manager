# One-click ADM start: confirm the two production Scheduled Tasks are
# enabled, kick one immediate cycle of each (safe/idempotent -- both tasks
# already have MultipleInstances=IgnoreNew, so this never starts a second
# concurrent run if one is already in progress), then show status and open
# either the live Session Center dashboard (if an AI execution is currently
# active) or a static status page (if idle -- the normal state).
#
# Never creates/modifies a Scheduled Task definition, never touches
# execution lifecycle, launchers, or credentials.

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "AdmCommon.ps1")

try {
    Confirm-AdmTaskEnabled -TaskName $AdmSupervisorTask
    Confirm-AdmTaskEnabled -TaskName $AdmWatcherTask
} catch {
    Show-AdmError "ADM could not start: $($_.Exception.Message)`n`nCheck that the ADM Scheduled Tasks were installed on this machine."
    exit 1
}

try {
    Start-ScheduledTask -TaskName $AdmSupervisorTask -ErrorAction Stop
} catch {
    # Non-fatal: the task still fires on its own 1-minute trigger regardless.
}
try {
    Start-ScheduledTask -TaskName $AdmWatcherTask -ErrorAction Stop
} catch {
    # Non-fatal: same as above.
}

Start-Sleep -Seconds 3

$supervisorStatus = Get-AdmTaskStatus -TaskName $AdmSupervisorTask
$watcherStatus = Get-AdmTaskStatus -TaskName $AdmWatcherTask
$sessionCenter = Get-AdmSessionCenterHealth

if (-not $supervisorStatus.Exists -or -not $watcherStatus.Exists) {
    Show-AdmError "ADM started with a problem: one or more required Scheduled Tasks are missing.`n`nSupervisor found: $($supervisorStatus.Exists)`nWatcher found: $($watcherStatus.Exists)"
}

$html = New-AdmStatusHtml -SupervisorStatus $supervisorStatus -WatcherStatus $watcherStatus -SessionCenter $sessionCenter
$statusPath = Join-Path $env:TEMP "adm-status.html"
$html | Out-File -FilePath $statusPath -Encoding utf8

if ($sessionCenter.Listening) {
    Start-Process "$AdmSessionCenterUrl/"
} else {
    Start-Process $statusPath
}
