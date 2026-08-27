param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$IngressRepo,
    [string]$IngressBranch = "dispatch-requests",
    [string]$IngressPath = "dispatch-requests",
    # A file containing the raw GitHub PAT, never the token value itself --
    # mirrors run_drive_dispatch_ingress.ps1's own -GoogleDriveToken (a file
    # path to the OAuth token JSON, not an inline secret) so a PAT is never
    # visible in the Scheduled Task's stored Action string or in any process
    # listing of this wrapper's own command line.
    [Parameter(Mandatory=$true)][string]$GitHubTokenFile,
    # The canonical idempotency bucket -- manager.gcs_lock_registry.
    # BUCKET_ENV ("ADM_LOCK_GCS_BUCKET"), the SAME bucket env
    # manager.drive_dispatch_watcher/manager.command_watcher/cloud.app
    # already use. There is no separate GitHub-ingress-specific idempotency
    # object env: dispatch request idempotency object names are generated
    # dynamically by manager.dispatch_requests.dispatch_request_registry()
    # as dispatch-requests/{project_id}/{request_id}.json, shared by both
    # ingress paths by design.
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [string]$GoogleDriveToken,
    [string]$ClaudeAccountsConfig,
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot
)

$env:ADM_SCHEDULER_INVOCATION_ID = [guid]::NewGuid().ToString("N")
$env:ADM_SCHEDULER_TASK_NAME = "AI Development Manager - GitHub Dispatch Ingress"
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
# exported and before any provenance/Python invocation below -- mirrors
# manager\run_drive_dispatch_ingress.ps1's guard exactly.
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
$env:ADM_WORKSPACE_ROOT = $WorkspaceRoot

$env:AI_MANAGER_HOME = $ManagerHome
# This ingress path never talks to Google Drive for request discovery, but
# manager.tasks.DriveRecords / manager.dispatcher's own quota read still
# need the existing Drive service -- exactly as manager.github_dispatch_
# watcher.run_once() documents. Same GOOGLE_DRIVE_TOKEN contract as the
# Drive ingress wrapper.
if ($GoogleDriveToken) {
    $env:GOOGLE_DRIVE_TOKEN = $GoogleDriveToken
} else {
    $env:GOOGLE_DRIVE_TOKEN = Join-Path $ManagerHome "google-drive-token.json"
}
if ($PythonDeps) { $env:PYTHONPATH = $PythonDeps }

# Required GitHub ingress repo/branch/path config -- this task's only job is
# turning GitHub-committed dispatch requests into Task + Command records for
# the existing Command Watcher to pick up (see
# manager\install_github_dispatch_ingress.ps1 header comment); it must
# never run without knowing which repo/branch/directory it is authoritative
# for.
if ([string]::IsNullOrWhiteSpace($IngressRepo)) {
    Write-Error "INGRESS_REPO_REQUIRED: -IngressRepo must be a non-empty 'owner/repo' string"
    exit 1
}
$env:ADM_GITHUB_DISPATCH_INGRESS_REPO = $IngressRepo
$env:ADM_GITHUB_DISPATCH_INGRESS_BRANCH = $IngressBranch
$env:ADM_GITHUB_DISPATCH_INGRESS_PATH = $IngressPath

# Required GitHub PAT -- read from a file, never taken as an inline
# parameter value, so it is never visible in the Scheduled Task's stored
# Action string. Fails closed (exits before any Python invocation) if the
# file is missing or empty; the raw token content is never echoed or
# logged, only exported into this process's own environment for the child
# Python process to read.
if (-not (Test-Path -LiteralPath $GitHubTokenFile -PathType Leaf)) {
    Write-Error "GITHUB_TOKEN_FILE_MISSING: GitHub PAT file not found at '$GitHubTokenFile'"
    exit 1
}
$githubToken = (Get-Content -LiteralPath $GitHubTokenFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($githubToken)) {
    Write-Error "GITHUB_TOKEN_FILE_EMPTY: GitHub PAT file at '$GitHubTokenFile' is empty"
    exit 1
}
$env:ADM_GITHUB_DISPATCH_INGRESS_TOKEN = $githubToken
$githubToken = $null

# Required idempotency bucket config, exported under the canonical env name
# manager.gcs_lock_registry.BUCKET_ENV already defines ("ADM_LOCK_GCS_BUCKET")
# -- the SAME variable the Drive ingress wrapper, manager.command_watcher,
# and cloud.app read, so a request claimed via either ingress path collides
# in the same registry by design.
$env:ADM_LOCK_GCS_BUCKET = $GcsBucket

# Canonical Claude account registry, exported under the exact env var name
# cloud.dispatch_ingress._claude_account_registry() reads
# (CLAUDE_ACCOUNTS_CONFIG) -- same contract and same fail-closed semantics
# as manager/run_drive_dispatch_ingress.ps1 / manager/run_command_watcher.ps1.
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

# Production provenance contract: refuse to run against the real GitHub
# ingress branch unless the TESTED, ACTIVATED, and RUNNING git SHAs all
# independently resolve to the same commit -- same contract the Command
# Watcher and the Drive ingress wrapper enforce, applied here even though
# this task never launches a provider itself, because it still writes
# Task/Command records the Command Watcher will act on.
$provenanceOutput = & $PythonPath -m manager.provenance verify-running --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "PROVENANCE_MISMATCH: GitHub Dispatch Ingress provenance contract failed, refusing to ingest.`n$provenanceOutput"
    exit 1
}
$contract = $provenanceOutput | ConvertFrom-Json
$env:ADM_WATCHER_GIT_SHA = $contract.running_sha
$env:ADM_TESTED_GIT_SHA = $contract.tested_sha
$env:ADM_ACTIVATED_GIT_SHA = $contract.activated_sha

# Interface contract owned by this same lane -- this wrapper only ever
# invokes the ingress poller, never a provider CLI (claude/codex/gemini
# binaries) or manager.command_watcher directly. The ingress poller's own
# job is limited to turning GitHub-committed dispatch requests into Task +
# Command records; the existing Command Watcher (a separate Scheduled Task,
# left untouched by this file) is what actually launches providers.
& $PythonPath -m manager.github_dispatch_watcher --once
exit $LASTEXITCODE
