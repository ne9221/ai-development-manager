param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$IngressFolderId,
    [Parameter(Mandatory=$true)][string]$IngressOwner,
    # The canonical idempotency bucket -- manager.gcs_lock_registry.
    # BUCKET_ENV ("ADM_LOCK_GCS_BUCKET"), the same bucket env
    # manager.command_watcher and cloud.app already use. There is no
    # separate ingress-specific idempotency object env: dispatch request
    # idempotency object names are generated dynamically by
    # manager.dispatch_requests.dispatch_request_registry() as
    # dispatch-requests/{project_id}/{request_id}.json.
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [string]$GoogleDriveToken,
    [string]$ClaudeAccountsConfig,
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot
)

$env:ADM_SCHEDULER_INVOCATION_ID = [guid]::NewGuid().ToString("N")
$env:ADM_SCHEDULER_TASK_NAME = "AI Development Manager - Drive Dispatch Ingress"
$env:ADM_SCHEDULER_WRAPPER_PID = "$PID"
$wrapperParentPid = $null
try {
    $wrapperProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop
    if ($null -ne $wrapperProcess -and $null -ne $wrapperProcess.ParentProcessId -and [int64]$wrapperProcess.ParentProcessId -gt 0) {
        $wrapperParentPid = [int64]$wrapperProcess.ParentProcessId
    }
} catch {}
$env:ADM_SCHEDULER_WRAPPER_PARENT_PID = "$wrapperParentPid"
$env:ADM_SCHEDULER_TRIGGER_ORIGIN = "unknown"

# Fail-closed WorkspaceRoot validation, before ADM_WORKSPACE_ROOT is ever
# exported and before any provenance/Python invocation below: non-empty,
# absolute, an existing directory, and not equal to or under the OS temp
# directory. Mirrors manager\run_command_watcher.ps1's guard exactly -- a
# separate ingress Scheduled Task carrying an invalid/temp WorkspaceRoot
# must never reach Python at all, same as the Command Watcher.
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
# ADM_WORKSPACE_ROOT resolution -- set here, on every tick, rather than
# relying on this Scheduled Task process inheriting whatever
# ADM_WORKSPACE_ROOT happens to be ambient (see run_command_watcher.ps1 for
# the incident this guards against).
$env:ADM_WORKSPACE_ROOT = $WorkspaceRoot

$env:AI_MANAGER_HOME = $ManagerHome
if ($GoogleDriveToken) {
    $env:GOOGLE_DRIVE_TOKEN = $GoogleDriveToken
} else {
    $env:GOOGLE_DRIVE_TOKEN = Join-Path $ManagerHome "google-drive-token.json"
}
if ($PythonDeps) { $env:PYTHONPATH = $PythonDeps }

# Required ingress folder/owner config -- this task's only job is turning
# Drive dispatch requests into Task + Command records for the existing
# Command Watcher to pick up (see manager\install_drive_dispatch_ingress.ps1
# header comment); it must never run without knowing which Drive folder and
# owner it is authoritative for.
$env:ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID = $IngressFolderId
$env:ADM_DRIVE_DISPATCH_INGRESS_OWNER = $IngressOwner

# Required idempotency bucket config, exported under the canonical env
# name manager.gcs_lock_registry.BUCKET_ENV already defines
# ("ADM_LOCK_GCS_BUCKET") -- the same variable manager.command_watcher and
# cloud.app read. manager.drive_dispatch_watcher.run_once() requires this
# exact name (os.environ.get(BUCKET_ENV)) and fails closed (TaskError) if
# it is unset; there is no separate ingress-specific bucket/object pair.
$env:ADM_LOCK_GCS_BUCKET = $GcsBucket

# Canonical Claude account registry, exported under the exact env var name
# cloud.dispatch_ingress._claude_account_registry() reads
# (CLAUDE_ACCOUNTS_CONFIG) -- same contract and same fail-closed semantics
# as manager/run_command_watcher.ps1. Without this, _claude_account_registry()
# returns None in this process and handle_dispatch() rejects every request
# that carries an explicit account_id as "unknown_account" before a claim is
# ever attempted, no matter how valid the request or how fresh quota is.
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

# Production provenance contract: refuse to run against a real Drive
# ingress folder unless the TESTED, ACTIVATED, and RUNNING git SHAs all
# independently resolve to the same commit -- same contract the Command
# Watcher enforces in run_command_watcher.ps1, applied here even though
# this task never launches a provider itself, because it still writes
# Task/Command records the Command Watcher will act on.
$provenanceOutput = & $PythonPath -m manager.provenance verify-running --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "PROVENANCE_MISMATCH: Drive Dispatch Ingress provenance contract failed, refusing to ingest.`n$provenanceOutput"
    exit 1
}
$contract = $provenanceOutput | ConvertFrom-Json
$env:ADM_WATCHER_GIT_SHA = $contract.running_sha
$env:ADM_TESTED_GIT_SHA = $contract.tested_sha
$env:ADM_ACTIVATED_GIT_SHA = $contract.activated_sha

# Interface contract owned by a separate lane (see
# manager\install_drive_dispatch_ingress.ps1 header) -- this wrapper only
# ever invokes the ingress poller, never a provider CLI (claude/codex/gemini
# binaries) or manager.command_watcher directly. The ingress poller's own
# job is limited to turning Drive dispatch requests into Task + Command
# records; the existing Command Watcher (a separate Scheduled Task, left
# untouched by this file) is what actually launches providers.
& $PythonPath -m manager.drive_dispatch_watcher --once
exit $LASTEXITCODE
