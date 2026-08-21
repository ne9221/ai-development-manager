# Shared helpers for the ADM one-click desktop launcher scripts.
# Read-only status gathering + idempotent Scheduled Task enable/trigger.
# Deliberately does not touch execution lifecycle, launchers, or credentials --
# it only confirms/starts the two existing production Scheduled Tasks and
# reads Session Center's already-public /health and /api/session endpoints
# and Streamlit Dashboard's /_stcore/health endpoint.

$AdmSupervisorTask = "AI Development Manager - Session Center Supervisor"
$AdmWatcherTask = "AI Development Manager - Command Watcher"
$AdmSessionCenterUrl = "http://127.0.0.1:8765"
$AdmDashboardUrl = "http://127.0.0.1:8501"

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

function Get-AdmDashboardHealth {
    param([string]$Url = $AdmDashboardUrl)
    $listening = $false
    try {
        $res = Invoke-WebRequest -Uri "$Url/_stcore/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($res.StatusCode -eq 200) {
            $listening = $true
        }
    } catch {
        try {
            $res = Invoke-WebRequest -Uri "$Url/" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($res.StatusCode -ge 200 -and $res.StatusCode -lt 400) {
                $listening = $true
            }
        } catch {
            $listening = $false
        }
    }
    return [PSCustomObject]@{ Listening = $listening; Url = $Url }
}

function Start-AdmDashboardBackground {
    param(
        [string]$RepositoryPath = $(Split-Path -Parent $PSScriptRoot)
    )
    $dashHealth = Get-AdmDashboardHealth
    if ($dashHealth.Listening) {
        return $true
    }

    $startDashboardScript = Join-Path $PSScriptRoot "Start-Dashboard.ps1"
    if (-not (Test-Path -LiteralPath $startDashboardScript)) {
        return $false
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startDashboardScript`" -RepositoryPath `"$RepositoryPath`""
    $psi.WorkingDirectory = $RepositoryPath
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false
    try {
        [System.Diagnostics.Process]::Start($psi) | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-AdmComprehensiveHealth {
    $supervisor = Get-AdmTaskStatus -TaskName $AdmSupervisorTask
    $watcher = Get-AdmTaskStatus -TaskName $AdmWatcherTask
    $sc = Get-AdmSessionCenterHealth
    $dash = Get-AdmDashboardHealth

    $dashState = if ($dash.Listening) { "running" } else { "unavailable" }
    $scState = if ($sc.Listening) {
        if ($sc.Session -and $sc.Session.current_state -and $sc.Session.current_state -ne "idle") {
            "running"
        } else {
            "idle"
        }
    } else {
        "unavailable"
    }

    $watcherState = if ($watcher.Exists) {
        $watcher.State.ToLowerInvariant()
    } else {
        "missing"
    }

    $supervisorState = if ($supervisor.Exists) {
        $supervisor.State.ToLowerInvariant()
    } else {
        "missing"
    }

    $preferredUrl = if ($dash.Listening) {
        "$AdmDashboardUrl/"
    } elseif ($sc.Listening) {
        "$AdmSessionCenterUrl/"
    } else {
        $null
    }

    return [PSCustomObject]@{
        DashboardStatus     = $dashState
        SessionCenterStatus = $scState
        WatcherStatus       = $watcherState
        SupervisorStatus    = $supervisorState
        PreferredUrl        = $preferredUrl
        SupervisorObject    = $supervisor
        WatcherObject       = $watcher
        SessionCenterObject = $sc
        DashboardObject     = $dash
    }
}

function Confirm-AdmTaskEnabled {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        throw "Required Scheduled Task not found: $TaskName"
    }
    if ($task.State -eq "Disabled") {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
    }
}

function New-AdmStatusHtml {
    param($SupervisorStatus, $WatcherStatus, $SessionCenter, $DashboardStatus = $null)

    if ($null -eq $DashboardStatus) {
        $DashboardStatus = Get-AdmDashboardHealth
    }

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

    $dashRow = if ($DashboardStatus -and $DashboardStatus.Listening) {
        "<p>Streamlit Dashboard: $(Badge($true)) listening on 8501</p>"
    } else {
        "<p>Streamlit Dashboard: unavailable (not currently listening on 8501)</p>"
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
$dashRow
$scRow
</div>
<p><small>Generated $(Get-Date). This is a static snapshot -- reload this launcher to refresh.</small></p>
</main></body></html>
"@
}

function Find-AdmEdgePath {
    # Looks in the normal per-machine/per-user install locations first, then
    # PATH, then the registry App Paths fallback that Edge's installer
    # always registers regardless of install location.
    $candidates = @()
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe") }
    $pf86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($pf86) { $candidates += (Join-Path $pf86 "Microsoft\Edge\Application\msedge.exe") }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe") }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }

    $cmd = Get-Command "msedge.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    try {
        $key = Get-Item -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe" -ErrorAction Stop
        $regValue = $key.GetValue("")
        if ($regValue -and (Test-Path -LiteralPath $regValue)) { return $regValue }
    } catch {
        # No registered App Paths entry -- Edge is genuinely not installed.
    }

    return $null
}

function Open-AdmAppWindow {
    # Single product-entry function for "open ADM as a Windows app window":
    # Edge --app=<url> gives a chromeless window (no address bar, no tabs)
    # so the user never sees or types a localhost URL. Callers must route
    # every dashboard/session-center open through this instead of a raw
    # Start-Process on an http:// URL.
    param(
        [Parameter(Mandatory = $true)][string]$Url
    )

    $targetUrl = $Url
    if ($Url -notmatch '^[a-zA-Z][a-zA-Z0-9+.-]*://') {
        # Local file path (e.g. the diagnostic status page) -- normalize to
        # a file:// URI so --app treats it the same as an http(s) URL.
        try {
            $targetUrl = ([uri]$Url).AbsoluteUri
        } catch {
            $targetUrl = $Url
        }
    }

    $edgePath = Find-AdmEdgePath
    if ($edgePath) {
        try {
            Start-Process -FilePath $edgePath -ArgumentList @("--app=$targetUrl") -ErrorAction Stop | Out-Null
            return [PSCustomObject]@{ Mode = "EdgeApp"; Success = $true; Detail = $edgePath }
        } catch {
            # Fall through to the truthful degraded fallback below.
        }
    }

    # Truthful degradation: Edge app-window mode is unavailable, so fall
    # back to the OS default browser rather than silently doing nothing or
    # claiming an app window opened when it didn't.
    try {
        Start-Process -FilePath $targetUrl -ErrorAction Stop | Out-Null
        return [PSCustomObject]@{ Mode = "BrowserFallback"; Success = $true; Detail = "msedge.exe not found or failed to launch; opened in the default browser instead" }
    } catch {
        return [PSCustomObject]@{ Mode = "Failed"; Success = $false; Detail = $_.Exception.Message }
    }
}

function Show-AdmError {
    param([string]$Message)
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Message, "AI Development Manager", 'OK', 'Error') | Out-Null
}
