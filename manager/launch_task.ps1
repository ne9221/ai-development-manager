param(
    [string]$ProjectId,
    [string]$TaskId,
    [string]$ExecutionId,
    [string]$PythonPath = "python",
    [string]$RepositoryPath = (Split-Path -Parent $PSScriptRoot),
    [string]$ManagerHome = (Join-Path $env:LOCALAPPDATA "AI Development Manager")
)

$ErrorActionPreference = "Stop"

function Quote-WindowsArgument([string]$Value) {
    if ($Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $quoted = New-Object System.Text.StringBuilder
    [void]$quoted.Append('"'); $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void]$quoted.Append(('\' * ($slashes * 2 + 1)))
            [void]$quoted.Append('"'); $slashes = 0; continue
        }
        if ($slashes) { [void]$quoted.Append(('\' * $slashes)); $slashes = 0 }
        [void]$quoted.Append($character)
    }
    if ($slashes) { [void]$quoted.Append(('\' * ($slashes * 2))) }
    [void]$quoted.Append('"')
    return $quoted.ToString()
}

function Safe-Value([string]$Value) {
    if (-not $Value) { return $null }
    return ([regex]::Replace($Value.Substring(0, [Math]::Min($Value.Length, 100)), '[^A-Za-z0-9_.-]', '_'))
}

if (-not $ProjectId) { $ProjectId = Read-Host "Project ID" }
if (-not $TaskId) { $TaskId = Read-Host "Task ID" }
if (-not $ProjectId -or -not $TaskId) { Write-Host "Failed: Project ID and Task ID are required."; exit 1 }

$logDirectory = Join-Path $ManagerHome "logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$diagnosticPath = Join-Path $logDirectory ("launcher-" + [guid]::NewGuid().ToString("N") + ".jsonl")
$status = "error"; $exitCode = 1; $errorKind = "launcher"

try {
    Write-Host "Starting task execution..."
    $arguments = @("-m", "manager.execution_runner", $ProjectId, $TaskId)
    if ($ExecutionId) { $arguments += @("--execution-id", $ExecutionId) }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $PythonPath
    $startInfo.Arguments = (($arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join " ")
    $startInfo.WorkingDirectory = $RepositoryPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "runner process did not start" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $null = $stderrTask.GetAwaiter().GetResult() # Never display or persist raw stderr.
    $exitCode = $process.ExitCode
    try { $result = $stdout | ConvertFrom-Json -ErrorAction Stop } catch { throw "runner returned invalid JSON" }
    if (-not $result.status -or $result.status -notin @("completed", "failed", "interrupted", "error")) { throw "runner returned invalid status" }
    $status = [string]$result.status
    $ExecutionId = [string]$result.execution_id
    $errorKind = if ($result.error -and $result.error.kind) { Safe-Value ([string]$result.error.kind) } else { $null }
    if ($status -eq "completed" -and $exitCode -eq 0) {
        Write-Host "Completed. Execution ID: $ExecutionId"
    } else {
        Write-Host "Failed/interrupted. Execution ID: $ExecutionId. Safe diagnostic: $diagnosticPath"
    }
} catch {
    $status = "error"; $errorKind = "launcher"
    Write-Host "Failed. Safe diagnostic: $diagnosticPath"
} finally {
    $diagnostic = [ordered]@{
        timestamp = [DateTime]::UtcNow.ToString("o")
        status = $status
        execution_id = Safe-Value $ExecutionId
        runner_exit_code = $exitCode
        error_kind = $errorKind
    } | ConvertTo-Json -Compress
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::AppendAllText($diagnosticPath, $diagnostic + [Environment]::NewLine, $utf8NoBom)
}

if ($status -eq "completed" -and $exitCode -eq 0) { exit 0 }
exit 1
