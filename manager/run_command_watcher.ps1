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
    [string]$IngressOwner
)

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
Set-Location -LiteralPath $RepositoryPath
& $PythonPath -m manager.command_watcher --once
exit $LASTEXITCODE
