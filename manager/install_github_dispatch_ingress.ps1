# Installs a dedicated, cheap Scheduled Task whose only job is polling a
# GitHub repo's dispatch-requests ingress branch/directory and turning new
# requests into Task + Command records -- the GitHub-write-connector
# counterpart of install_drive_dispatch_ingress.ps1 (ChatGPT's Drive-write
# connector is unreliable; its GitHub-write connector is not). It never
# launches a provider (claude/codex/antigravity) itself -- the existing
# "AI Development Manager - Command Watcher" Scheduled Task (see
# install_command_watcher.ps1) is left completely untouched and remains the
# only thing that ever launches a provider:
#
#   GitHub commit -> this ingress task -> Task + Command records
#                  -> existing Command Watcher -> provider
#
# Both this task and the existing Drive Dispatch Ingress task
# (install_drive_dispatch_ingress.ps1) share the SAME idempotency bucket
# (-GcsBucket / ADM_LOCK_GCS_BUCKET) and the SAME
# manager.dispatch_requests.dispatch_request_registry() -- a request_id
# submitted through either path is claimed exactly once, by design.
#
# Interface contract for the polling core:
#     python -m manager.github_dispatch_watcher --once
#
# This script only installs the hidden Windows trigger/wrapper around that
# interface -- it does not implement it.
#
# THIS SCRIPT IS SOURCE-ONLY. Running it registers a real Windows Scheduled
# Task, which is intentionally OUT OF SCOPE for this change -- see the
# activation branch's own report for what a human must review/approve
# before ever invoking this installer against production.
param(
    [string]$TaskName = "AI Development Manager - GitHub Dispatch Ingress",
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [string]$PythonDeps,
    # "owner/repo" -- for this project's own dedicated ingress branch, this
    # is the SAME repo ADM itself lives in (e.g. "ne9221/ai-development-manager").
    [Parameter(Mandatory=$true)][string]$IngressRepo,
    [string]$IngressBranch = "dispatch-requests",
    [string]$IngressPath = "dispatch-requests",
    # Optional: a file containing a raw GitHub PAT -- see
    # run_github_dispatch_ingress.ps1's own -GitHubTokenFile comment for why
    # this is a file path, never an inline secret value, on this installer's
    # own command line. When omitted (preferred), the installed task relies
    # on this machine's own `git credential` helper at runtime instead (see
    # manager.github_dispatch_client.GitHubApiClient.default()) -- no PAT
    # is created or persisted anywhere by this installer.
    [string]$GitHubTokenFile,
    # The canonical idempotency bucket -- manager.gcs_lock_registry.
    # BUCKET_ENV ("ADM_LOCK_GCS_BUCKET"), the SAME bucket the Drive Dispatch
    # Ingress task and the Command Watcher already use.
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [string]$GoogleDriveToken,
    [string]$ClaudeAccountsConfig,
    # Trusted authority for manager.project_registry's ADM_WORKSPACE_ROOT
    # resolution -- same mandatory, fail-closed contract as
    # install_drive_dispatch_ingress.ps1 / install_command_watcher.ps1.
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot,
    # Bounded execution timeout for this task -- same rationale as
    # install_drive_dispatch_ingress.ps1's own default: one cheap poll +
    # idempotency-bucket check per tick, never allowed to run long enough to
    # overlap its own next trigger in a way IgnoreNew alone can't cheaply
    # bound.
    [int]$ExecutionTimeLimitMinutes = 5
)

# Fail-closed WorkspaceRoot validation, before any provenance activation or
# Scheduled Task mutation below -- identical guard to
# install_drive_dispatch_ingress.ps1.
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    Write-Error "WORKSPACE_ROOT_REQUIRED: -WorkspaceRoot must be a non-empty trusted workspace path; refusing to install the GitHub Dispatch Ingress task without one"
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
if ($GitHubTokenFile -and -not (Test-Path -LiteralPath $GitHubTokenFile -PathType Leaf)) {
    Write-Error "GITHUB_TOKEN_FILE_MISSING: GitHub PAT file not found at '$GitHubTokenFile'"
    exit 1
}

. (Join-Path $PSScriptRoot "AdmHiddenLaunch.ps1")

# Production provenance contract: refuse to (re)install this Scheduled Task
# unless this exact checkout's HEAD was already proven by a TESTED evidence
# capture (manager.provenance capture-tested) -- same gate
# install_drive_dispatch_ingress.ps1 / install_command_watcher.ps1 apply.
Push-Location -LiteralPath $RepositoryPath
try {
    $activationOutput = & $PythonPath -m manager.provenance activate --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
    $activationExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($activationExit -ne 0) {
    Write-Error "PROVENANCE_MISMATCH: cannot activate GitHub Dispatch Ingress install, TESTED evidence missing or does not match this checkout's HEAD.`n$activationOutput"
    exit 1
}

if (-not $GoogleDriveToken) {
    $GoogleDriveToken = Join-Path $ManagerHome "google-drive-token.json"
}
if (-not $ClaudeAccountsConfig) {
    $ClaudeAccountsConfig = Join-Path $ManagerHome "config\claude_accounts.json"
}

$runner = Join-Path $RepositoryPath "manager\run_github_dispatch_ingress.ps1"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"$PythonPath`" -RepositoryPath `"$RepositoryPath`" -ManagerHome `"$ManagerHome`" -PythonDeps `"$PythonDeps`" -IngressRepo `"$IngressRepo`" -IngressBranch `"$IngressBranch`" -IngressPath `"$IngressPath`""
if ($GitHubTokenFile) {
    # Only threaded through when explicitly given -- omitted entirely
    # (the preferred production path) leaves the runner to resolve a
    # token itself via this machine's own `git credential` helper.
    $arguments += " -GitHubTokenFile `"$GitHubTokenFile`""
}
$arguments += " -GcsBucket `"$GcsBucket`" -GoogleDriveToken `"$GoogleDriveToken`" -ClaudeAccountsConfig `"$ClaudeAccountsConfig`" -WorkspaceRoot `"$WorkspaceRoot`""
# Same "route through a generated hidden VBS wrapper" mechanism as the
# Command Watcher and Drive Dispatch Ingress tasks -- WshShell.Run(cmd, 0,
# True) sets the window style to hidden before the process is even created.
# Distinct WrapperName so the generated .vbs never collides with
# command-watcher.vbs or drive-dispatch-ingress.vbs.
$action = New-AdmHiddenScheduledTaskAction -RepositoryPath $RepositoryPath -WrapperName "github-dispatch-ingress" -PowerShellArguments $arguments

# Every 1 minute, starting 1 minute from install time -- matches the Drive
# Dispatch Ingress task's own cadence.
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)

# -MultipleInstances IgnoreNew: if a tick is still running when the next one
# fires, the new one is skipped rather than stacking concurrent pollers.
# -Hidden keeps the Task itself out of the default Task Scheduler view.
# -ExecutionTimeLimit bounds a stuck/hanging poll so it can never run
# indefinitely and starve later ticks.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

# -Force: registering an already-existing $TaskName updates it in place
# rather than erroring or creating a second Task under the same name --
# safe reinstall/update behavior, no duplicate Scheduled Tasks.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
