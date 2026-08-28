# Shared helpers for the ADM one-click desktop launcher scripts.
# Read-only status gathering + idempotent Scheduled Task enable/trigger + Dashboard focus.
# Deliberately does not touch execution lifecycle, launchers, or credentials --
# it confirms/starts the production Scheduled Tasks, ensures Dashboard is active,
# and manages single-instance focus and shortcuts.

$AdmSupervisorTask = "AI Development Manager - Session Center Supervisor"
$AdmWatcherTask = "AI Development Manager - Command Watcher"
$AdmDriveIngressTask = "AI Development Manager - Drive Dispatch Ingress"
$AdmQuotaRefreshTask = "AI Development Manager - Quota Refresh"
$AdmGithubIngressTask = "AI Development Manager - GitHub Dispatch Ingress"

$AdmAllTasks = @(
    $AdmSupervisorTask,
    $AdmWatcherTask,
    $AdmDriveIngressTask,
    $AdmQuotaRefreshTask,
    $AdmGithubIngressTask
)

$AdmSessionCenterUrl = "http://127.0.0.1:8765"
$AdmDashboardPort = 8501
$AdmDashboardUrl = "http://localhost:$AdmDashboardPort"
$AdmManagerHome = if ($env:AI_MANAGER_HOME) { $env:AI_MANAGER_HOME } else { Join-Path $env:USERPROFILE ".ai-development-manager" }
$AdmRuntimePath = Join-Path $AdmManagerHome "runtime"
$AdmWatcherMaintenancePath = Join-Path $AdmRuntimePath "watcher-maintenance.json"
$AdmWatcherMaintenanceLastPath = Join-Path $AdmRuntimePath "watcher-maintenance-last.json"
$AdmShortcutName = "AI 開發管理器"
$AdmLegacyShortcutName = "AI Development Manager"

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
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        throw "Required Scheduled Task not found: $TaskName"
    }
    if (-not $task.Settings.Enabled) {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
    }
}

function Confirm-AdmAllTasksEnabled {
    foreach ($taskName in $AdmAllTasks) {
        Confirm-AdmTaskEnabled -TaskName $taskName
    }
}

function Start-AdmAllTasks {
    foreach ($taskName in $AdmAllTasks) {
        try {
            Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        } catch {}
    }
}

function Test-AdmDashboardRunning {
    param([int]$Port = $AdmDashboardPort)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $success = $iar.AsyncWaitHandle.WaitOne(400, $false)
        if ($success -and $client.Connected) {
            $client.EndConnect($iar)
            $client.Close()
            return $true
        }
        $client.Close()
    } catch {}
    return $false
}

function Start-AdmDashboardProcess {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryPath,
        [string]$PythonPath = "C:\Users\EE\AppData\Local\Python\pythoncore-3.14-64\python.exe",
        [int]$Port = $AdmDashboardPort
    )
    if (Test-AdmDashboardRunning -Port $Port) {
        return
    }
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        $PythonPath = "python"
    }
    $dashboardScript = Join-Path $RepositoryPath "dashboard.py"
    if (-not (Test-Path -LiteralPath $dashboardScript)) {
        throw "Dashboard script not found at $dashboardScript"
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonPath
    $psi.Arguments = "-m streamlit run dashboard.py --server.port $Port --server.headless true"
    $psi.WorkingDirectory = $RepositoryPath
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false
    $proc = [System.Diagnostics.Process]::Start($psi)

    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Milliseconds 200
        if (Test-AdmDashboardRunning -Port $Port) { break }
    }
}

function Focus-AdmDashboard {
    param([string]$Url = $AdmDashboardUrl)
    if ($env:PESTER_TEST) {
        Start-Process $Url
        return
    }
    try {
        $wsh = New-Object -ComObject WScript.Shell
        $activated = $wsh.AppActivate("AI Development Manager") -or $wsh.AppActivate("Streamlit") -or $wsh.AppActivate("localhost:$AdmDashboardPort")
        if (-not $activated) {
            Start-Process $Url
        }
    } catch {
        Start-Process $Url
    }
}

function Install-AdmShortcuts {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryPath,
        [string[]]$TargetFolders
    )
    $repository = [IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
    $targetVbs = Join-Path $repository "desktop\Start-ADM.vbs"
    if (-not (Test-Path -LiteralPath $targetVbs)) {
        throw "Target VBS not found at $targetVbs"
    }
    $wsh = New-Object -ComObject WScript.Shell

    $locations = if ($TargetFolders) {
        $TargetFolders
    } else {
        @(
            [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop),
            [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs),
            [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
        )
    }

    foreach ($loc in $locations) {
        if (-not (Test-Path -LiteralPath $loc)) {
            New-Item -ItemType Directory -Force -Path $loc | Out-Null
        }
        $legacyLnk = Join-Path $loc "$AdmLegacyShortcutName.lnk"
        if (Test-Path -LiteralPath $legacyLnk) {
            Remove-Item -LiteralPath $legacyLnk -Force -ErrorAction SilentlyContinue
        }

        $lnkPath = Join-Path $loc "$AdmShortcutName.lnk"
        $shortcut = $wsh.CreateShortcut($lnkPath)
        $shortcut.TargetPath = "wscript.exe"
        $shortcut.Arguments = "`"$targetVbs`""
        $shortcut.WorkingDirectory = Join-Path $repository "desktop"
        $shortcut.Description = "AI 開發管理器 - AI Development Manager"
        $shortcut.Save()
    }
}

function Test-AdmWatcherPowerShellCommandLine {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Runner,
        [Parameter(Mandatory = $true)][string]$ManagerHome
    )
    $singleQuote = [char]39
    $quotedValue = '(?:"[^"]*"|''[^'']*'')'
    $runnerValue = '(?:"' + [regex]::Escape($Runner) + '"|' + $singleQuote + [regex]::Escape($Runner) + $singleQuote + ')'
    $repositoryValue = '(?:"' + [regex]::Escape($Repository) + '"|' + $singleQuote + [regex]::Escape($Repository) + $singleQuote + ')'
    $managerHomeValue = '(?:"' + [regex]::Escape($ManagerHome) + '"|' + $singleQuote + [regex]::Escape($ManagerHome) + $singleQuote + ')'
    $pattern = '\A-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + $runnerValue +
        ' -PythonPath ' + $quotedValue + ' -RepositoryPath ' + $repositoryValue + ' -ManagerHome ' + $managerHomeValue +
        ' -CodexBin ' + $quotedValue + ' -CodexHome ' + $quotedValue + ' -PythonDeps ' + $quotedValue +
        ' -AllowlistPath ' + $quotedValue + ' -GcsBucket ' + $quotedValue + ' -GcsObject ' + $quotedValue +
        ' -IngressFolderId ' + $quotedValue + ' -IngressOwner ' + $quotedValue +
        '(?: -EmbeddedIngress ' + $quotedValue + ')?' +
        ' -ClaudeAccountsConfig ' + $quotedValue +
        ' -WorkspaceRoot ' + $quotedValue + '\z'
    return $CommandLine -cmatch $pattern
}

function Test-AdmWatcherHiddenVbsIdentity {
    param(
        [Parameter(Mandatory = $true)]$Action,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Runner
    )
    $resolvedHome = if ($env:AI_MANAGER_HOME) { $env:AI_MANAGER_HOME } else { $AdmManagerHome }
    $doubleQuote = [char]34
    $rawArguments = ([string]$Action.Arguments).Trim()

    if ($rawArguments.Length -lt 2 -or $rawArguments[0] -ne $doubleQuote -or $rawArguments[-1] -ne $doubleQuote) { return $false }
    $vbsPath = $rawArguments.Substring(1, $rawArguments.Length - 2)
    if ($vbsPath.Length -eq 0 -or $vbsPath.Contains($doubleQuote)) { return $false }

    $expectedVbsPath = Join-Path $Repository "manager\generated\command-watcher.vbs"
    $resolvedVbsPath = [IO.Path]::GetFullPath($vbsPath)
    if ($resolvedVbsPath -ne [IO.Path]::GetFullPath($expectedVbsPath)) { return $false }
    if (-not (Test-Path -LiteralPath $resolvedVbsPath -PathType Leaf)) { return $false }

    $content = Get-Content -LiteralPath $resolvedVbsPath -Raw -ErrorAction SilentlyContinue
    if (-not $content) { return $false }

    $wrapperPattern = '\A'' Auto-generated by AdmHiddenLaunch\.ps1 -- regenerated on every install, do not edit by hand\.\r?\n'' Runs PowerShell with a truly hidden window \(WshShell\.Run windowStyle=0\) so Task\r?\n'' Scheduler never flashes a console, and propagates the real exit code so\r?\n'' LastTaskResult stays meaningful\.\r?\nSet shell = CreateObject\("WScript\.Shell"\)\r?\nexitCode = shell\.Run\("powershell\.exe (.*)", 0, True\)\r?\nWScript\.Quit exitCode\r?\n\z'
    if ($content -cnotmatch $wrapperPattern) { return $false }
    $escapedCommandLine = $Matches[1]

    $commandLine = $escapedCommandLine.Replace(([string]$doubleQuote) + $doubleQuote, [string]$doubleQuote)
    if ($commandLine -notmatch '-WindowStyle\s+Hidden') { return $false }
    if ($commandLine -notmatch ('-File\s+["' + [char]39 + ']' + [regex]::Escape($Runner) + '["' + [char]39 + ']')) { return $false }

    return Test-AdmWatcherPowerShellCommandLine -CommandLine $commandLine -Repository $Repository -Runner $Runner -ManagerHome $resolvedHome
}

function Test-AdmWatcherTaskIdentity {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$RepositoryPath
    )
    if ($Task.TaskName -ne $AdmWatcherTask -or $Task.TaskPath -ne "\" -or @($Task.Actions).Count -ne 1) { return $false }
    $action = @($Task.Actions)[0]
    $repository = [IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
    $runner = Join-Path $repository "manager\run_command_watcher.ps1"
    $execute = ([string]$action.Execute).Trim()
    if ([string]::Equals($execute, "wscript.exe", [StringComparison]::OrdinalIgnoreCase)) {
        return Test-AdmWatcherHiddenVbsIdentity -Action $action -Repository $repository -Runner $runner
    }
    return $false
}

function Set-AdmPersistentUserEnvironmentVariable {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$Value)
    if ($env:PESTER_TEST -or $env:AI_MANAGER_HOME) {
        Set-Item -Path "Env:$Name" -Value $Value
        return
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "User")
}

function Test-AdmWorkspaceRootContaminated {
    param([Parameter(Mandatory = $true)][string]$CandidateRoot)
    if ($env:PESTER_TEST -or $env:AI_MANAGER_HOME) { return $false }
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
    return ($candidate -eq $temp) -or $candidate.StartsWith($temp + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Set-AdmWorkspacePointer {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryPath,
        [Parameter(Mandatory = $true)][string]$ProjectId
    )
    $repository = [IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
    $inheritedRoot = $env:ADM_WORKSPACE_ROOT
    $workspaceRoot = if ($inheritedRoot -and -not (Test-AdmWorkspaceRootContaminated -CandidateRoot $inheritedRoot)) {
        [IO.Path]::GetFullPath($inheritedRoot).TrimEnd('\')
    } else {
        (Split-Path -Path $repository -Parent).TrimEnd('\')
    }

    if (Test-AdmWorkspaceRootContaminated -CandidateRoot $workspaceRoot) {
        throw "Refusing to establish workspace authority at $workspaceRoot -- it resolves under the OS temp directory (both the inherited ADM_WORKSPACE_ROOT and the repository's own parent are contaminated); no environment variable or junction was changed."
    }

    if ($env:ADM_WORKSPACE_ROOT -ne $workspaceRoot) {
        Set-AdmPersistentUserEnvironmentVariable -Name "ADM_WORKSPACE_ROOT" -Value $workspaceRoot
        $env:ADM_WORKSPACE_ROOT = $workspaceRoot
    }

    $pointerPath = Join-Path $workspaceRoot $ProjectId
    $existing = Get-Item -LiteralPath $pointerPath -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.LinkType -ne "Junction") {
            throw "Refusing to manage workspace pointer at $pointerPath -- it already exists and is not an ADM-managed junction (found: $($existing.GetType().Name), LinkType=$($existing.LinkType))"
        }
        $currentTarget = [IO.Path]::GetFullPath(@($existing.Target)[0]).TrimEnd('\')
        if ($currentTarget -eq $repository) {
            return $pointerPath
        }
        Remove-Item -LiteralPath $pointerPath -Force
    }
    New-Item -ItemType Junction -Path $pointerPath -Target $repository -ErrorAction Stop | Out-Null
    return $pointerPath
}

function Confirm-AdmWatcherTaskIdentity {
    param([Parameter(Mandatory = $true)][string]$RepositoryPath)
    $task = Get-ScheduledTask -TaskName $AdmWatcherTask -ErrorAction SilentlyContinue
    if (-not $task -or -not (Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $RepositoryPath)) {
        throw "Refusing to control Command Watcher: its exact root task action does not match this repository: $RepositoryPath"
    }
    return $task
}

# Fail-closed guard for Pester test files (AdmDesktop.Tests.ps1) that
# exercise New-AdmHiddenScheduledTaskAction against a caller-supplied
# repository path. That helper has a REAL, unmocked side effect of writing
# "$Repository\manager\generated\<WrapperName>.vbs" to disk -- for the
# Command Watcher this is exactly the file the real, live Scheduled Task
# reads its launch arguments from on every tick. If a test suite is ever
# invoked directly against the checkout that IS the real production Command
# Watcher's own checkout (instead of an isolated scratch clone, per this
# project's established workflow), exercising that helper would silently
# overwrite the live launcher file with test/placeholder arguments and break
# the real Scheduled Task on its very next tick -- invisibly, since the
# Task's own registered Action (a fixed "wscript.exe <vbs path>") never
# changes; only the vbs file's content does.
#
# Detects that condition by reading the REAL, currently-registered
# $WatcherTaskName task and checking whether its actual vbs path already
# resolves to "$Repository\manager\generated\command-watcher.vbs" -- if so,
# this checkout IS that task's live binding, and no test in this file may
# run against it.
function Assert-AdmNotProductionCheckoutForTests {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$WatcherTaskName
    )
    $realTask = Get-ScheduledTask -TaskName $WatcherTaskName -ErrorAction SilentlyContinue
    if (-not $realTask -or @($realTask.Actions).Count -ne 1) { return }
    $realAction = @($realTask.Actions)[0]
    if (-not [string]::Equals([string]$realAction.Execute, "wscript.exe", [StringComparison]::OrdinalIgnoreCase)) { return }
    $rawRealArgs = ([string]$realAction.Arguments).Trim()
    $doubleQuote = [char]34
    if ($rawRealArgs.Length -lt 2 -or $rawRealArgs[0] -ne $doubleQuote -or $rawRealArgs[-1] -ne $doubleQuote) { return }
    $realVbsPath = $rawRealArgs.Substring(1, $rawRealArgs.Length - 2)
    if (-not (Test-Path -LiteralPath $realVbsPath -PathType Leaf)) { return }
    $thisSuiteWouldWrite = Join-Path $Repository "manager\generated\command-watcher.vbs"
    if ([IO.Path]::GetFullPath($realVbsPath) -eq [IO.Path]::GetFullPath($thisSuiteWouldWrite)) {
        throw "PESTER_PRODUCTION_CHECKOUT_GUARD: refusing to run this test suite -- this checkout ('$Repository') is the real '$WatcherTaskName' task's own live checkout (its registered Scheduled Task action already points at the exact generated vbs path this suite would overwrite: '$realVbsPath'). Run this suite from an isolated scratch clone instead, never against the live production checkout."
    }
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
        Move-Item -LiteralPath $AdmWatcherMaintenancePath -Destination $AdmWatcherMaintenanceLastPath -Force
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
    Write-Error $Message
    if (-not $env:ADM_NON_INTERACTIVE -and -not $env:PESTER_TEST) {
        try {
            Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
            [System.Windows.Forms.MessageBox]::Show($Message, "AI Development Manager", 'OK', 'Error') | Out-Null
        } catch {}
    }
}
