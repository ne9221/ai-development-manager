param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [string]$CodexBin,
    [string]$CodexHome,
    [string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$AllowlistPath,
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [Parameter(Mandatory=$true)][string]$GcsObject,
    [string]$IngressFolderId,
    [string]$IngressOwner,
    [string]$EmbeddedIngress,
    [string]$ClaudeAccountsConfig,
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot
)

$env:ADM_SCHEDULER_INVOCATION_ID = [guid]::NewGuid().ToString("N")
$env:ADM_SCHEDULER_TASK_NAME = "AI Development Manager - Command Watcher"
$env:ADM_SCHEDULER_WRAPPER_PID = "$PID"
$wrapperParentPid = $null
try {
    $wrapperProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop
    if ($null -ne $wrapperProcess -and $null -ne $wrapperProcess.ParentProcessId -and [int64]$wrapperProcess.ParentProcessId -gt 0) {
        $wrapperParentPid = [int64]$wrapperProcess.ParentProcessId
    }
} catch {}
$env:ADM_SCHEDULER_WRAPPER_PARENT_PID = "$wrapperParentPid"
# A wrapper cannot distinguish Task Scheduler's Run button from a time trigger.
$env:ADM_SCHEDULER_TRIGGER_ORIGIN = "unknown"

# Fail-closed WorkspaceRoot validation, before ADM_WORKSPACE_ROOT is ever
# exported and before any provenance/Python invocation below: non-empty,
# absolute, an existing directory, and not equal to or under the OS temp
# directory. A Scheduled Task carrying an invalid/temp WorkspaceRoot must
# never reach Python at all.
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    Write-Error "WORKSPACE_ROOT_REQUIRED: -WorkspaceRoot must be a non-empty trusted workspace path; refusing to launch without explicit workspace authority"
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
# Explicit trusted authority for manager.project_registry's
# ADM_WORKSPACE_ROOT resolution (resolve_authoritative_working_directory_
# with_project() in manager/project_registry.py) -- set here, on every
# tick, rather than relying on this Scheduled Task process inheriting
# whatever ADM_WORKSPACE_ROOT happens to be ambient. That ambient-inheritance
# gap is what let a Task materialize working_directory under %TEMP% (see
# fix/home-watcher-workspace-truth-bootstrap-20260823).
$env:ADM_WORKSPACE_ROOT = $WorkspaceRoot

$env:AI_MANAGER_HOME = $ManagerHome
$env:GOOGLE_DRIVE_TOKEN = Join-Path $ManagerHome "google-drive-token.json"
if ($CodexBin) { $env:CODEX_BIN = $CodexBin }
if ($CodexHome) { $env:CODEX_HOME = $CodexHome }
if ($PythonDeps) { $env:PYTHONPATH = $PythonDeps }
$env:ADM_WATCHER_ALLOWLIST_PATH = $AllowlistPath
$env:ADM_LOCK_GCS_BUCKET = $GcsBucket
$env:ADM_LOCK_GCS_OBJECT = $GcsObject
if ($IngressFolderId) { $env:ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID = $IngressFolderId }
if ($IngressOwner) { $env:ADM_DRIVE_DISPATCH_INGRESS_OWNER = $IngressOwner }
# Explicit embedded-ingress switch (see manager/command_watcher.py's
# embedded_ingress_enabled()). Only exported when the installer was given a
# value at all -- unset leaves the env var absent, which
# embedded_ingress_enabled() itself treats as "enabled" (pre-migration
# default), not this script silently choosing a default of its own.
if ($EmbeddedIngress) { $env:ADM_COMMAND_WATCHER_EMBEDDED_INGRESS = $EmbeddedIngress }

if (-not $ClaudeAccountsConfig) {
    $ClaudeAccountsConfig = Join-Path $ManagerHome "config\claude_accounts.json"
}
if (-not (Test-Path -LiteralPath $ClaudeAccountsConfig)) {
    Write-Error "CLAUDE_ACCOUNTS_CONFIG_MISSING: Claude accounts configuration file not found at '$ClaudeAccountsConfig'"
    exit 1
}
$env:CLAUDE_ACCOUNTS_CONFIG = $ClaudeAccountsConfig

# CWD must be $RepositoryPath before any `python -m manager.*` invocation so
# the manager package resolves regardless of the caller's own working
# directory (PYTHONPATH above only covers third-party deps, not this repo).
Set-Location -LiteralPath $RepositoryPath

# Production provenance contract: refuse to launch a real provider unless the
# TESTED, ACTIVATED, and RUNNING git SHAs all independently resolve to the
# same commit. $contract.running_sha below is captured fresh by
# manager.provenance re-running `git rev-parse HEAD` against $RepositoryPath
# on every tick -- it is never trusted from a caller-supplied value.
$provenanceOutput = & $PythonPath -m manager.provenance verify-running --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "PROVENANCE_MISMATCH: Watcher provenance contract failed, refusing to launch provider.`n$provenanceOutput"
    exit 1
}
$contract = $provenanceOutput | ConvertFrom-Json
$env:ADM_WATCHER_GIT_SHA = $contract.running_sha
$env:ADM_TESTED_GIT_SHA = $contract.tested_sha
$env:ADM_ACTIVATED_GIT_SHA = $contract.activated_sha

& $PythonPath -m manager.command_watcher --once
exit $LASTEXITCODE
