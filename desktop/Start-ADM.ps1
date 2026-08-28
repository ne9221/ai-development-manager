# One-click ADM start: confirm all production Scheduled Tasks are enabled,
# kick one immediate cycle of each (safe/idempotent -- tasks have
# MultipleInstances=IgnoreNew), install/update shortcuts, and ensure the
# Operations Dashboard is running and focused without duplicate instances.

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "AdmCommon.ps1")

$repository = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

try {
    Confirm-AdmWatcherTaskIdentity -RepositoryPath $repository | Out-Null
    Set-AdmWorkspacePointer -RepositoryPath $repository -ProjectId "ai-development-manager" | Out-Null
    Confirm-AdmAllTasksEnabled
    Install-AdmShortcuts -RepositoryPath $repository
    Clear-AdmWatcherMaintenance
} catch {
    Show-AdmError "ADM could not start: $($_.Exception.Message)`n`nCheck that the ADM Scheduled Tasks were installed on this machine."
    exit 1
}

Start-AdmAllTasks

if (-not $env:ADM_SKIP_DASHBOARD_LAUNCH) {
    if (Test-AdmDashboardRunning) {
        Focus-AdmDashboard
    } else {
        Start-AdmDashboardProcess -RepositoryPath $repository
        Focus-AdmDashboard
    }
}
