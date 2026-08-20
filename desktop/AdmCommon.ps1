# Shared helpers for the ADM one-click desktop launcher scripts.
# Read-only status gathering + idempotent Scheduled Task enable/trigger.
# Deliberately does not touch execution lifecycle, launchers, or credentials --
# it only confirms/starts the two existing production Scheduled Tasks and
# reads Session Center's already-public /health and /api/session endpoints.

$AdmSupervisorTask = "AI Development Manager - Session Center Supervisor"
$AdmWatcherTask = "AI Development Manager - Command Watcher"
$AdmSessionCenterUrl = "http://127.0.0.1:8765"
$AdmManagerHome = if ($env:AI_MANAGER_HOME) { $env:AI_MANAGER_HOME } else { Join-Path $env:USERPROFILE ".ai-development-manager" }
$AdmRuntimePath = Join-Path $AdmManagerHome "runtime"
$AdmWatcherMaintenancePath = Join-Path $AdmRuntimePath "watcher-maintenance.json"

function Get-AdmTaskStatus {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return [PSCustomObject]@{ Name = $TaskName; Exists = $false; State = "Missing"; LastResult = $null; LastRun = $null }
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    return [PSCustomObject]@{
        Name       = $TaskName
        Exists     = $true
        State      = $task.State.ToString()
        LastResult = if ($info) { $info.LastTaskResult } else { $null }
        LastRun    = if ($info) { $info.LastRunTime } else { $null }
    }
}

function Get-AdmSessionCenterHealth {
    $listening = $false
    $session = $null
    try {
        Invoke-RestMethod -Uri "$AdmSessionCenterUrl/health" -TimeoutSec 2 -ErrorAction Stop | Out-Null
        $listening = $true
    } catch {
        $listening = $false
    }
    if ($listening) {
        try {
            $session = Invoke-RestMethod -Uri "$AdmSessionCenterUrl/api/session" -TimeoutSec 2 -ErrorAction Stop
        } catch {
            $session = $null
        }
    }
    return [PSCustomObject]@{ Listening = $listening; Session = $session }
}

function Confirm-AdmTaskEnabled {
    # "Confirm/start" per the task's own idempotent-safe framing: only flips
    # Disabled -> Enabled; never touches an already-Enabled/Running task's
    # trigger, principal, or action. Throws (caller decides how to surface it)
    # if the task doesn't exist at all -- that's a real setup problem, not
    # something this launcher should silently paper over.
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        throw "Required Scheduled Task not found: $TaskName"
    }
    if ($task.State -eq "Disabled") {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
    }
}

function Test-AdmWatcherTaskIdentity {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$RepositoryPath
    )
    if ($Task.TaskName -ne $AdmWatcherTask -or $Task.TaskPath -ne "\" -or @($Task.Actions).Count -ne 1) { return $false }
    $action = @($Task.Actions)[0]
    if ([IO.Path]::GetFileName([string]$action.Execute) -ne "powershell.exe") { return $false }
    $repository = [IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
    $runner = Join-Path $repository "manager\run_command_watcher.ps1"
    $arguments = [string]$action.Arguments
    $singleQuote = [char]39
    $doubleQuote = [char]34
    $runnerExact = $arguments.Contains($singleQuote + $runner + $singleQuote) -or $arguments.Contains($doubleQuote + $runner + $doubleQuote)
    $repositoryFlag = "-RepositoryPath "
    $repositoryExact = $arguments.Contains($repositoryFlag + $singleQuote + $repository + $singleQuote) -or $arguments.Contains($repositoryFlag + $doubleQuote + $repository + $doubleQuote)
    return $runnerExact -and $repositoryExact
}

function Confirm-AdmWatcherTaskIdentity {
    param([Parameter(Mandatory = $true)][string]$RepositoryPath)
    $task = Get-ScheduledTask -TaskName $AdmWatcherTask -ErrorAction SilentlyContinue
    if (-not $task -or -not (Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $RepositoryPath)) {
        throw "Refusing to control Command Watcher: its exact root task action does not match this repository: $RepositoryPath"
    }
    return $task
}

function Write-AdmWatcherMaintenance {
    param(
        [Parameter(Mandatory = $true)][string]$Reason,
        [Parameter(Mandatory = $true)][string]$SourceRepository
    )
    New-Item -ItemType Directory -Force -Path $AdmRuntimePath | Out-Null
    $temporary = "$AdmWatcherMaintenancePath.tmp"
    [ordered]@{
        timestamp = [DateTime]::UtcNow.ToString("o")
        reason = $Reason
        source = $SourceRepository
    } | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $AdmWatcherMaintenancePath -Force
}

function Clear-AdmWatcherMaintenance {
    if (Test-Path -LiteralPath $AdmWatcherMaintenancePath) {
        Remove-Item -LiteralPath $AdmWatcherMaintenancePath -Force
    }
}

function New-AdmStatusHtml {
    param($SupervisorStatus, $WatcherStatus, $SessionCenter)

    function Badge($ok) {
        if ($ok) { return '<span style="color:#8ff0c0;background:#164e3b;padding:2px 8px;border-radius:99px;">OK</span>' }
        return '<span style="color:#ffb4bc;background:#55252b;padding:2px 8px;border-radius:99px;">ATTENTION</span>'
    }

    function TaskRow($status) {
        $ok = $status.Exists -and $status.State -ne "Disabled" -and $status.State -ne "Missing"
        $lastResult = if ($null -ne $status.LastResult) { "0x{0:X}" -f $status.LastResult } else { "n/a" }
        $lastRun = if ($status.LastRun) { $status.LastRun } else { "never" }
        return "<tr><td>$($status.Name)</td><td>$(Badge($ok))</td><td>$($status.State)</td><td>$lastResult</td><td>$lastRun</td></tr>"
    }

    $scRow = if ($SessionCenter.Listening) {
        $s = $SessionCenter.Session
        if ($s) {
            "<p>Session Center: $(Badge($true)) listening on 8765 &mdash; provider=$($s.provider), state=$($s.current_state), correlated=$($s.correlated)</p>"
        } else {
            "<p>Session Center: $(Badge($true)) listening on 8765, but /api/session did not respond</p>"
        }
    } else {
        "<p>Session Center: idle &mdash; no active AI execution right now (this is the normal state when nothing is running)</p>"
    }

    return @"
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ADM Status</title>
<style>
body{font:15px system-ui;margin:0;background:#10151d;color:#e8edf4}
main{max-width:900px;margin:40px auto;padding:0 20px}
h1{font-size:22px}
table{width:100%;border-collapse:collapse;margin-top:12px}
td,th{padding:8px 10px;border-bottom:1px solid #344255;text-align:left}
.card{background:#18212d;border:1px solid #344255;border-radius:10px;padding:20px;margin-top:16px}
</style></head><body><main>
<h1>AI Development Manager &mdash; Status</h1>
<div class="card">
<table><tr><th>Task</th><th></th><th>State</th><th>Last result</th><th>Last run</th></tr>
$(TaskRow($SupervisorStatus))
$(TaskRow($WatcherStatus))
</table>
</div>
<div class="card">
$scRow
</div>
<p><small>Generated $(Get-Date). This is a static snapshot -- reload this launcher to refresh.</small></p>
</main></body></html>
"@
}

function Show-AdmError {
    param([string]$Message)
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Message, "AI Development Manager", 'OK', 'Error') | Out-Null
}
