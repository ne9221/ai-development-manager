param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [string]$CodexBin,
    [string]$CodexHome,
    [string]$PythonDeps,
    [string]$ClaudePayload
)

$env:AI_MANAGER_HOME = $ManagerHome
$env:GOOGLE_DRIVE_TOKEN = Join-Path $ManagerHome "google-drive-token.json"
if ($CodexBin) { $env:CODEX_BIN = $CodexBin }
if ($CodexHome) { $env:CODEX_HOME = $CodexHome }
if ($PythonDeps) { $env:PYTHONPATH = $PythonDeps }
if ($ClaudePayload) { $env:CLAUDE_STATUSLINE_PAYLOAD = $ClaudePayload }

# Wrapper-level start/end + PID lines exist so a hang or non-launch is
# visible even when it happens before manager.refresh_status ever writes
# its own "refresh start" line to this same log (e.g. the python process
# never starts, or an earlier invocation is still alive past
# ExecutionTimeLimit and orphaned outside Task Scheduler's tracking).
$logPath = Join-Path $ManagerHome "logs\refresh.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null
$startedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Add-Content -LiteralPath $logPath -Value "$startedAt wrapper start pid=$PID" -Encoding utf8

Set-Location -LiteralPath $RepositoryPath
& $PythonPath -m manager.refresh_status
$exitCode = $LASTEXITCODE

$endedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Add-Content -LiteralPath $logPath -Value "$endedAt wrapper end pid=$PID exit=$exitCode" -Encoding utf8
exit $exitCode
