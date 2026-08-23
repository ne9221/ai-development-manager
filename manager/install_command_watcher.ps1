param(
    [string]$TaskName = "AI Development Manager - Command Watcher",
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [Parameter(Mandatory=$true)][string]$CodexBin,
    [Parameter(Mandatory=$true)][string]$CodexHome,
    [Parameter(Mandatory=$true)][string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$AllowlistPath,
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [Parameter(Mandatory=$true)][string]$GcsObject,
    [string]$IngressFolderId,
    [string]$IngressOwner,
    [string]$ClaudeAccountsConfig,
    # Trusted authority for manager.project_registry's ADM_WORKSPACE_ROOT
    # resolution -- explicitly serialized into the Scheduled Task action
    # (below) and re-exported by run_command_watcher.ps1 on every tick,
    # rather than relying on whatever ADM_WORKSPACE_ROOT the Scheduled
    # Task's process happens to inherit. Mandatory + fail-closed: an
    # install with no real workspace authority must never silently produce
    # a Task that falls back to an ambient/TEMP value at launch time (see
    # fix/home-watcher-workspace-truth-bootstrap-20260823).
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot
)

# Fail-closed WorkspaceRoot validation, before any provenance activation or
# Scheduled Task mutation below: non-empty, absolute, an existing directory,
# and not equal to or under the OS temp directory. A Claude scratch clone's
# own checkout parent (e.g. %TEMP%\claude\...\scratchpad) is exactly the
# kind of value that could otherwise slip through a bare non-empty check.
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    Write-Error "WORKSPACE_ROOT_REQUIRED: -WorkspaceRoot must be a non-empty trusted workspace path; refusing to install the Command Watcher without one"
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

. (Join-Path $PSScriptRoot "AdmHiddenLaunch.ps1")

# Production provenance contract: refuse to (re)install the Scheduled Task
# unless this exact checkout's HEAD was already proven by a TESTED evidence
# capture (manager.provenance capture-tested). This is what makes "source
# tests must pass before the Scheduled Task changes" fail-closed instead of
# a procedural reminder. Runs with CWD set to $RepositoryPath so the
# `manager` package resolves regardless of the caller's own working
# directory (this script itself is not otherwise CWD-sensitive).
Push-Location -LiteralPath $RepositoryPath
try {
    $activationOutput = & $PythonPath -m manager.provenance activate --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
    $activationExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($activationExit -ne 0) {
    Write-Error "PROVENANCE_MISMATCH: cannot activate Watcher install, TESTED evidence missing or does not match this checkout's HEAD.`n$activationOutput"
    exit 1
}

if (-not $ClaudeAccountsConfig) {
    $ClaudeAccountsConfig = Join-Path $ManagerHome "config\claude_accounts.json"
}

$runner = Join-Path $RepositoryPath "manager\run_command_watcher.ps1"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"$PythonPath`" -RepositoryPath `"$RepositoryPath`" -ManagerHome `"$ManagerHome`" -CodexBin `"$CodexBin`" -CodexHome `"$CodexHome`" -PythonDeps `"$PythonDeps`" -AllowlistPath `"$AllowlistPath`" -GcsBucket `"$GcsBucket`" -GcsObject `"$GcsObject`" -IngressFolderId `"$IngressFolderId`" -IngressOwner `"$IngressOwner`" -ClaudeAccountsConfig `"$ClaudeAccountsConfig`" -WorkspaceRoot `"$WorkspaceRoot`""
$action = New-AdmHiddenScheduledTaskAction -RepositoryPath $RepositoryPath -WrapperName "command-watcher" -PowerShellArguments $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes 125)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
