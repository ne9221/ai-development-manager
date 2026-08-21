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
    Write-Host "找不到設定的 PythonPath（$PythonPath），改用 PATH 中的 'python'。" -ForegroundColor Yellow
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
        "找不到 Google Drive 驗證權杖：`n$($env:GOOGLE_DRIVE_TOKEN)`n`n儀表板需要這個檔案才能讀取即時 Drive 資料。",
        "AI Development Manager", 'OK', 'Error'
    ) | Out-Null
    exit 1
}

Set-Location -LiteralPath $RepositoryPath
Write-Host "正在從 $RepositoryPath 啟動 ADM 儀表板（連接埠 $Port）..." -ForegroundColor Cyan
# --server.headless true: Streamlit must never pop its own browser tab --
# the tray/app-window layer (Open-AdmAppWindow) is the only product entry.
& $PythonPath -m streamlit run dashboard.py --server.port $Port --server.headless true
exit $LASTEXITCODE
