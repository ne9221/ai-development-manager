# ADM System Tray Launcher
# Native Windows .NET NotifyIcon + Single Instance Mutex
# Provides quick access to ADM Dashboard, Real-time Status, Service Management, and Clean Exit.

param(
    [string]$RepositoryPath = $(Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Continue"

# Load shared status & helper utilities
$admCommonPath = Join-Path $PSScriptRoot "AdmCommon.ps1"
if (Test-Path -LiteralPath $admCommonPath) {
    . $admCommonPath
}

# Single Instance Enforcement via named Mutex
$mutexName = "Local\ADM_Windows_Tray_Launcher_Mutex"
$createdNew = $false
$mutex = $null
try {
    $mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
} catch {
    $createdNew = $false
}

function Start-AdmServicesSafe {
    # -AllowEnable must only ever be passed from an explicit user action (the
    # "啟動服務"/"重新啟動服務" menu clicks below, or Restart-AdmServicesSafe
    # which is itself only reachable from that same menu click). Cold start
    # (Tray launch / user login) always runs with the default $false, so a
    # task the user disabled on purpose -- via Stop-ADM.ps1 or directly in
    # Task Scheduler -- is never silently re-enabled just because the Tray
    # happened to (re)start. That silent auto-re-enable, running every time
    # Explorer relaunched the Tray, is what let a disabled Command Watcher
    # keep coming back and popping a console every minute even after the
    # user disabled it.
    param([switch]$AllowEnable)
    try {
        foreach ($name in @($AdmSupervisorTask, $AdmWatcherTask)) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if (-not $task) { continue }
            if ($task.State -eq "Disabled") {
                if ($AllowEnable) { Confirm-AdmTaskEnabled -TaskName $name }
                else { continue }
            }
            Start-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        }
        Start-AdmDashboardBackground -RepositoryPath $RepositoryPath -ErrorAction SilentlyContinue
    } catch {
        # Non-fatal during background start
    }
}

function Restart-AdmServicesSafe {
    # Same disable-then-enable-and-trigger pattern as Stop-ADM.ps1 +
    # Start-ADM.ps1, done inline so the tray doesn't spawn extra processes.
    # Never kills an already-running child process -- disabling only blocks
    # the *next* Scheduled Task trigger. Only reachable from the "重新啟動
    # 服務" menu click, i.e. always an explicit user action -- so re-enabling
    # here is intentional, not the automatic cold-start self-heal this fixes.
    try {
        foreach ($name in @($AdmSupervisorTask, $AdmWatcherTask)) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($task -and $task.State -ne "Disabled") {
                Disable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null
            }
        }
        Start-Sleep -Seconds 1
        Start-AdmServicesSafe -AllowEnable
    } catch {
        # Non-fatal during background restart
    }
}

function Open-AdmDashboardSafe {
    param([int]$TimeoutSec = 8)
    try {
        $dashHealth = Get-AdmDashboardHealth
        if ($dashHealth.Listening) {
            $result = Open-AdmAppWindow -Url "$AdmDashboardUrl/"
            if (-not $result.Success) { Show-AdmError "無法開啟 ADM 視窗：$($result.Detail)" }
            return
        }

        # Cold start: trigger dashboard startup
        Start-AdmDashboardBackground -RepositoryPath $RepositoryPath -ErrorAction SilentlyContinue

        # Wait for health ready
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $ready = $false
        while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
            Start-Sleep -Milliseconds 500
            $dashCheck = Get-AdmDashboardHealth
            if ($dashCheck.Listening) {
                $ready = $true
                break
            }
        }

        if ($ready) {
            $result = Open-AdmAppWindow -Url "$AdmDashboardUrl/"
            if (-not $result.Success) { Show-AdmError "無法開啟 ADM 視窗：$($result.Detail)" }
            return
        }

        # Fallback: check Session Center
        $sc = Get-AdmSessionCenterHealth
        if ($sc.Listening) {
            $result = Open-AdmAppWindow -Url "$AdmSessionCenterUrl/"
            if (-not $result.Success) { Show-AdmError "無法開啟 ADM 視窗：$($result.Detail)" }
            return
        }

        # Fallback: diagnostic status html
        $health = Get-AdmComprehensiveHealth
        $statusPath = Join-Path $env:TEMP "adm-status.html"
        $html = New-AdmStatusHtml -SupervisorStatus $health.SupervisorObject -WatcherStatus $health.WatcherObject -SessionCenter $health.SessionCenterObject -DashboardStatus $health.DashboardObject
        $html | Out-File -FilePath $statusPath -Encoding utf8
        $result = Open-AdmAppWindow -Url $statusPath
        if (-not $result.Success) { Show-AdmError "無法開啟 ADM 視窗：$($result.Detail)" }
    } catch {
        $result = Open-AdmAppWindow -Url "$AdmDashboardUrl/"
        if (-not $result.Success) { Show-AdmError "無法開啟 ADM 視窗：$($result.Detail)" }
    }
}

function Update-AdmActionNotifications {
    try {
        $cachePath = Join-Path $env:LOCALAPPDATA "AI-Development-Manager\tray-actions.json"
        $cacheDir = Split-Path $cachePath -Parent; New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
        $python = Get-Command python.exe -ErrorAction Stop
        Push-Location -LiteralPath $RepositoryPath
        try { $raw = & $python.Source -m manager.tray_actions 2>$null } finally { Pop-Location }
        $snapshot = $raw | ConvertFrom-Json
        if ($snapshot.state -ne "ok") { $notifyIcon.Text = "ADM: Action state Unknown"; return }
        $seen = @{}
        if (Test-Path $cachePath) {
            try {
                $cached = Get-Content -Raw $cachePath | ConvertFrom-Json
                foreach ($property in $cached.PSObject.Properties) { $seen[$property.Name] = $property.Value }
            } catch { $seen = @{} }
        }
        foreach ($action in $snapshot.actions) {
            $key = "$($action.id)|$($action.status)|$($action.timestamp)"
            if (-not $seen.ContainsKey($key)) { $notifyIcon.ShowBalloonTip(5000, "ADM Action: $($action.severity)", $action.title, [System.Windows.Forms.ToolTipIcon]::Warning); $seen[$key] = $true }
        }
        $seen | ConvertTo-Json | Set-Content -Encoding utf8 $cachePath
        $notifyIcon.Text = "ADM: $($snapshot.count) actionable actions; severity $($snapshot.highest_severity)"
    } catch { $notifyIcon.Text = "ADM: Action state Unknown" }
}

if (-not $createdNew) {
    # Another instance is already running. Open the dashboard directly and exit this secondary instance.
    Open-AdmDashboardSafe
    exit 0
}

# Cold start: idempotently ensure required ADM background tasks and Dashboard are enabled and triggered
Start-AdmServicesSafe

# Add required .NET assemblies for WinForms NotifyIcon & System Icons
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# Create NotifyIcon
$notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$notifyIcon.Icon = [System.Drawing.SystemIcons]::Application
$notifyIcon.Text = "AI Development Manager"
$notifyIcon.Visible = $true
Update-AdmActionNotifications
$pollTimer = New-Object System.Windows.Forms.Timer
$pollTimer.Interval = 60000
$pollTimer.add_Tick({
    Update-AdmActionNotifications
    # P1-G Global Self-Heal: this existing 60s tick *is* Dashboard's
    # periodic supervisor -- no separate Python tray, no new Scheduled
    # Task. Confirm-AdmDashboardAlive is idempotent (Start-AdmDashboardBackground
    # no-ops if already listening), so running it every tick can never
    # spawn a duplicate backend.
    try { Confirm-AdmDashboardAlive -RepositoryPath $RepositoryPath | Out-Null } catch {}
})
$pollTimer.Start()

# Context Menu
$contextMenu = New-Object System.Windows.Forms.ContextMenuStrip

# 1. Open ADM (Default / Bold)
$menuOpenDash = New-Object System.Windows.Forms.ToolStripMenuItem
$menuOpenDash.Text = "開啟 ADM"
$menuOpenDash.Font = New-Object System.Drawing.Font($menuOpenDash.Font, [System.Drawing.FontStyle]::Bold)
$menuOpenDash.add_Click({
    Open-AdmDashboardSafe
})
$contextMenu.Items.Add($menuOpenDash) | Out-Null

# 2. Status Details
$menuStatus = New-Object System.Windows.Forms.ToolStripMenuItem
$menuStatus.Text = "系統狀態"
$menuStatus.add_Click({
    try {
        $health = Get-AdmComprehensiveHealth
        $msg = "AI Development Manager 狀態：`n`n" +
               "Streamlit 儀表板： $($health.DashboardStatus)`n" +
               "Session Center：   $($health.SessionCenterStatus)`n" +
               "Command Watcher：  $($health.WatcherStatus)`n" +
               "Supervisor Task：  $($health.SupervisorStatus)"
        [System.Windows.Forms.MessageBox]::Show($msg, "ADM 健康狀態", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    } catch {
        [System.Windows.Forms.MessageBox]::Show("無法檢查 ADM 狀態：$($_.Exception.Message)", "ADM 健康狀態", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    }
})
$contextMenu.Items.Add($menuStatus) | Out-Null

# 3. Start ADM Services (idempotent -- safe no-op if already running)
$menuStart = New-Object System.Windows.Forms.ToolStripMenuItem
$menuStart.Text = "啟動服務"
$menuStart.add_Click({
    try {
        # Explicit user click: unlike cold start, this may re-enable a
        # disabled task -- the user is asking for it right now.
        Start-AdmServicesSafe -AllowEnable
        $notifyIcon.ShowBalloonTip(3000, "ADM 服務", "已確認並觸發排程工作與儀表板。", [System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        [System.Windows.Forms.MessageBox]::Show("服務啟動失敗：$($_.Exception.Message)", "ADM 服務管理", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    }
})
$contextMenu.Items.Add($menuStart) | Out-Null

# 4. Restart ADM Services (disable then re-enable + trigger)
$menuRestart = New-Object System.Windows.Forms.ToolStripMenuItem
$menuRestart.Text = "重新啟動服務"
$menuRestart.add_Click({
    try {
        Restart-AdmServicesSafe
        $notifyIcon.ShowBalloonTip(3000, "ADM 服務", "已重新啟動排程工作與儀表板。", [System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        [System.Windows.Forms.MessageBox]::Show("服務重新啟動失敗：$($_.Exception.Message)", "ADM 服務管理", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    }
})
$contextMenu.Items.Add($menuRestart) | Out-Null

# Separator
$contextMenu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

# 5. Exit Launcher
$menuExit = New-Object System.Windows.Forms.ToolStripMenuItem
$menuExit.Text = "結束 ADM"
$menuExit.add_Click({
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Close()
    }
    [System.Windows.Forms.Application]::Exit()
})
$contextMenu.Items.Add($menuExit) | Out-Null

$notifyIcon.ContextMenuStrip = $contextMenu

# Double Click action
$notifyIcon.add_DoubleClick({
    Open-AdmDashboardSafe # Streamlit has no stable Action Center route; opens supported Dashboard entry point.
})

# Show initial balloon notification
$notifyIcon.ShowBalloonTip(3000, "AI Development Manager", "ADM 正在系統匣執行中。連按兩下即可開啟 ADM。", [System.Windows.Forms.ToolTipIcon]::Info)

try {
    # Run the application message loop
    [System.Windows.Forms.Application]::Run()
} finally {
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Close()
    }
}
