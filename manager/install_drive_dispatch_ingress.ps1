# Installs a dedicated, cheap Scheduled Task whose only job is polling the
# Drive dispatch-request folder and turning new requests into Task + Command
# records. It never launches a provider (claude/codex/gemini) itself -- the
# existing "AI Development Manager - Command Watcher" Scheduled Task (see
# install_command_watcher.ps1) is left completely untouched and remains the
# only thing that ever launches a provider:
#
#   Drive request -> this ingress task -> Task + Command records
#                  -> existing Command Watcher -> provider
#
# This keeps request ingestion (cheap, high-frequency, no provider quota
# impact) separate from provider launching (expensive, quota-sensitive).
#
# Interface contract for the polling core (owned by a separate lane, not
# implemented here):
#     python -m manager.drive_dispatch_watcher --once
#
# This script only installs the hidden Windows trigger/wrapper around that
# interface -- it does not implement it.
param(
    [string]$TaskName = "AI Development Manager - Drive Dispatch Ingress",
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$IngressFolderId,
    [Parameter(Mandatory=$true)][string]$IngressOwner,
    # The canonical idempotency bucket -- manager.gcs_lock_registry.
    # BUCKET_ENV ("ADM_LOCK_GCS_BUCKET"), the same bucket env
    # manager.command_watcher and cloud.app already use, resolved by
    # manager.drive_dispatch_watcher via os.environ.get(BUCKET_ENV).
    # There is no separate ingress-specific idempotency object: dispatch
    # request idempotency object names are generated dynamically by
    # manager.dispatch_requests.dispatch_request_registry() as
    # dispatch-requests/{project_id}/{request_id}.json, not a static path.
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [string]$GoogleDriveToken,
    # Canonical Claude account registry path, propagated verbatim into the
    # hidden wrapper's -ClaudeAccountsConfig argument so
    # run_drive_dispatch_ingress.ps1 can export CLAUDE_ACCOUNTS_CONFIG on
    # every tick -- the same env var manager/run_command_watcher.ps1 already
    # exports and cloud.dispatch_ingress._claude_account_registry() reads.
    # Without it, this task's process never has that var set, so any Drive
    # request carrying an explicit account_id is unconditionally rejected as
    # "unknown_account" before a claim is ever attempted (confirmed live).
    # Resolved to a default below when omitted, exactly like
    # install_command_watcher.ps1 already does for the existing Command
    # Watcher install -- no file-existence check here; that fail-closed
    # check belongs to run_drive_dispatch_ingress.ps1 at actual run time,
    # matching run_command_watcher.ps1's own split of responsibility.
    [string]$ClaudeAccountsConfig,
    # Trusted authority for manager.project_registry's ADM_WORKSPACE_ROOT
    # resolution -- explicitly serialized into the Scheduled Task action
    # (below) and re-exported by run_drive_dispatch_ingress.ps1 on every
    # tick, rather than relying on whatever ADM_WORKSPACE_ROOT the
    # Scheduled Task's process happens to inherit. Mandatory + fail-closed:
    # an install with no real workspace authority must never silently
    # produce a Task that falls back to an ambient/TEMP value at launch
    # time (same contract as install_command_watcher.ps1).
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot,
    # Bounded execution timeout for this task. Defaults far shorter than
    # the Command Watcher's 125 minutes: this task only does one cheap
    # Drive poll + idempotency-bucket check per tick and must never be
    # allowed to run long enough to overlap its own next 1-minute trigger
    # in a way IgnoreNew alone can't cheaply bound.
    [int]$ExecutionTimeLimitMinutes = 5
)

# Fail-closed WorkspaceRoot validation, before any provenance activation or
# Scheduled Task mutation below: non-empty, absolute, an existing
# directory, and not equal to or under the OS temp directory. A Claude
# scratch clone's own checkout parent (e.g. %TEMP%\claude\...\scratchpad)
# is exactly the kind of value that could otherwise slip through a bare
# non-empty check.
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    Write-Error "WORKSPACE_ROOT_REQUIRED: -WorkspaceRoot must be a non-empty trusted workspace path; refusing to install the Drive Dispatch Ingress task without one"
    exit 1
}
if (-not [IO.Path]::IsPathRooted($WorkspaceRoot)) {
    Write-Error "WORKSPACE_ROOT_INVALID: -WorkspaceRoot must be an absolute path: '$WorkspaceRoot'"
    exit 1
}
if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
    Write-Error "WORKSPACE_ROOT_INVALID: -WorkspaceRoot does not exist or is not a directory: '$WorkspaceRoot'"
    exit 1
}
$resolvedWorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\')
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
if ($resolvedWorkspaceRoot -eq $tempRoot -or $resolvedWorkspaceRoot.StartsWith($tempRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "WORKSPACE_ROOT_INVALID: -WorkspaceRoot resolves under the OS temp directory ('$resolvedWorkspaceRoot'); refusing to trust it as workspace authority"
    exit 1
}
if ($ExecutionTimeLimitMinutes -le 0) {
    Write-Error "EXECUTION_TIME_LIMIT_INVALID: -ExecutionTimeLimitMinutes must be a positive number of minutes: '$ExecutionTimeLimitMinutes'"
    exit 1
}

. (Join-Path $PSScriptRoot "AdmHiddenLaunch.ps1")

# Production provenance contract: refuse to (re)install this Scheduled Task
# unless this exact checkout's HEAD was already proven by a TESTED evidence
# capture (manager.provenance capture-tested) -- same gate
# install_command_watcher.ps1 applies to the provider-launching task. Runs
# with CWD set to $RepositoryPath so the `manager` package resolves
# regardless of the caller's own working directory.
Push-Location -LiteralPath $RepositoryPath
try {
    $activationOutput = & $PythonPath -m manager.provenance activate --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
    $activationExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($activationExit -ne 0) {
    Write-Error "PROVENANCE_MISMATCH: cannot activate Drive Dispatch Ingress install, TESTED evidence missing or does not match this checkout's HEAD.`n$activationOutput"
    exit 1
}

if (-not $GoogleDriveToken) {
    $GoogleDriveToken = Join-Path $ManagerHome "google-drive-token.json"
}
if (-not $ClaudeAccountsConfig) {
    $ClaudeAccountsConfig = Join-Path $ManagerHome "config\claude_accounts.json"
}

$runner = Join-Path $RepositoryPath "manager\run_drive_dispatch_ingress.ps1"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"$PythonPath`" -RepositoryPath `"$RepositoryPath`" -ManagerHome `"$ManagerHome`" -PythonDeps `"$PythonDeps`" -IngressFolderId `"$IngressFolderId`" -IngressOwner `"$IngressOwner`" -GcsBucket `"$GcsBucket`" -GoogleDriveToken `"$GoogleDriveToken`" -ClaudeAccountsConfig `"$ClaudeAccountsConfig`" -WorkspaceRoot `"$WorkspaceRoot`""
# Same "route through a generated hidden VBS wrapper" mechanism as the
# Command Watcher -- WshShell.Run(cmd, 0, True) sets the window style to
# hidden before the process is even created, which is what actually
# eliminates the console flash that plain "-WindowStyle Hidden" on a
# directly-registered powershell.exe action cannot prevent. Distinct
# WrapperName so the generated .vbs never collides with command-watcher.vbs.
$action = New-AdmHiddenScheduledTaskAction -RepositoryPath $RepositoryPath -WrapperName "drive-dispatch-ingress" -PowerShellArguments $arguments

# Every 1 minute, starting 1 minute from install time -- matches the
# Command Watcher's own cadence, since Drive dispatch requests should be
# picked up promptly, and this task is cheap enough to poll that often.
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)

# -MultipleInstances IgnoreNew: if a tick is still running when the next
# one fires, the new one is skipped rather than stacking concurrent
# pollers. -Hidden keeps the Task itself out of the default Task Scheduler
# view. -ExecutionTimeLimit bounds a stuck/hanging poll so it can never run
# indefinitely and starve later ticks.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

# -Force: registering an already-existing $TaskName updates it in place
# rather than erroring or creating a second Task under the same name --
# safe reinstall/update behavior, no duplicate Scheduled Tasks.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
