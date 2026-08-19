# One-click launcher for the ADM Streamlit Operations Dashboard.
#
# Read-only: renders the same Drive SSOT (tasks/executions/sessions/quota)
# the production Watcher/Supervisor already write to. Does not touch
# Scheduled Tasks, execution lifecycle, or credentials -- it only reads.
#
# Defaults match this machine's production Scheduled Task configuration
# (see `Get-ScheduledTask` on "AI Development Manager - Command Watcher").
# Override any parameter if running from a different clone/machine.

param(
    [string]$RepositoryPath = $(Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "C:\Users\EE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$ManagerHome = "C:\Users\EE\.ai-development-manager",
    [string]$PythonDeps = "C:\Users\EE\Documents\ChatGPT\AI\ai-development-manager-codex3\.test-deps\Lib\site-packages",
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Host "Configured PythonPath not found ($PythonPath) -- falling back to 'python' on PATH." -ForegroundColor Yellow
    $PythonPath = "python"
}

$env:AI_MANAGER_HOME = $ManagerHome
$env:GOOGLE_DRIVE_TOKEN = Join-Path $ManagerHome "google-drive-token.json"
if ($PythonDeps -and (Test-Path -LiteralPath $PythonDeps)) {
    $env:PYTHONPATH = $PythonDeps
}

if (-not (Test-Path -LiteralPath $env:GOOGLE_DRIVE_TOKEN)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "Google Drive token not found at:`n$($env:GOOGLE_DRIVE_TOKEN)`n`nThe Dashboard needs this to read live Drive data.",
        "AI Development Manager", 'OK', 'Error'
    ) | Out-Null
    exit 1
}

Set-Location -LiteralPath $RepositoryPath
Write-Host "Starting ADM Dashboard from $RepositoryPath on port $Port ..." -ForegroundColor Cyan
& $PythonPath -m streamlit run dashboard.py --server.port $Port --server.headless false
exit $LASTEXITCODE
