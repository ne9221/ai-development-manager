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
$provenanceOutput = & $PythonPath -m manager.provenance verify-running --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
if ($LASTEXITCODE -ne 0) { Write-Error "PROVENANCE_MISMATCH: Session Supervisor provenance contract failed.`n$provenanceOutput"; exit 1 }
& $PythonPath -m manager.session_center_supervisor `
    --python-path $PythonPath --repository-path $RepositoryPath `
    --manager-home $ManagerHome --state-file $StateFile --port $Port
exit $LASTEXITCODE
