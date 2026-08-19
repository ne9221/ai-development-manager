param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [Parameter(Mandatory=$true)][string]$StateFile,
    [string]$PythonDeps,
    [string]$AllowlistPath,
    [int]$Port = 8765,
    [string]$GcsBucket
)

$env:AI_MANAGER_HOME = $ManagerHome
$env:GOOGLE_DRIVE_TOKEN = Join-Path $ManagerHome "google-drive-token.json"
if ($PythonDeps) { $env:PYTHONPATH = $PythonDeps }
if ($AllowlistPath) { $env:ADM_WATCHER_ALLOWLIST_PATH = $AllowlistPath }
if ($GcsBucket) { $env:ADM_LOCK_GCS_BUCKET = $GcsBucket }
Set-Location -LiteralPath $RepositoryPath
& $PythonPath -m manager.session_center_supervisor `
    --python-path $PythonPath --repository-path $RepositoryPath `
    --state-file $StateFile --port $Port
exit $LASTEXITCODE
