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
if ($CodexBin) { $env:CODEX_BIN = $CodexBin }
if ($CodexHome) { $env:CODEX_HOME = $CodexHome }
if ($PythonDeps) { $env:PYTHONPATH = $PythonDeps }
if ($ClaudePayload) { $env:CLAUDE_STATUSLINE_PAYLOAD = $ClaudePayload }

Set-Location -LiteralPath $RepositoryPath
& $PythonPath -m manager.refresh_status
exit $LASTEXITCODE
