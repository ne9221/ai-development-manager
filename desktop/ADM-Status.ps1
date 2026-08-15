# Read-only ADM status/health check. Never enables, triggers, disables, or
# otherwise mutates any Scheduled Task -- safe to run at any time, including
# while an AI execution is actively running.

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "AdmCommon.ps1")

$supervisorStatus = Get-AdmTaskStatus -TaskName $AdmSupervisorTask
$watcherStatus = Get-AdmTaskStatus -TaskName $AdmWatcherTask
$sessionCenter = Get-AdmSessionCenterHealth

$html = New-AdmStatusHtml -SupervisorStatus $supervisorStatus -WatcherStatus $watcherStatus -SessionCenter $sessionCenter
$statusPath = Join-Path $env:TEMP "adm-status.html"
$html | Out-File -FilePath $statusPath -Encoding utf8

if ($sessionCenter.Listening) {
    Start-Process "$AdmSessionCenterUrl/"
} else {
    Start-Process $statusPath
}
