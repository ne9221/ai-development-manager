param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [string]$CodexBin,
    [string]$CodexHome,
    [string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [Parameter(Mandatory=$true)][string]$GcsObject
)

$env:AI_MANAGER_HOME = $ManagerHome
if ($CodexBin) { $env:CODEX_BIN = $CodexBin }
if ($CodexHome) { $env:CODEX_HOME = $CodexHome }
if ($PythonDeps) { $env:PYTHONPATH = $PythonDeps }
$env:ADM_LOCK_GCS_BUCKET = $GcsBucket
$env:ADM_LOCK_GCS_OBJECT = $GcsObject
Set-Location -LiteralPath $RepositoryPath
& $PythonPath -m manager.command_watcher --once
exit $LASTEXITCODE
