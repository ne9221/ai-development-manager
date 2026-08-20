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
    try {
        Confirm-AdmTaskEnabled -TaskName $AdmSupervisorTask
        Confirm-AdmTaskEnabled -TaskName $AdmWatcherTask
        Start-ScheduledTask -TaskName $AdmSupervisorTask -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $AdmWatcherTask -ErrorAction SilentlyContinue
        Start-AdmDashboardBackground -RepositoryPath $RepositoryPath -ErrorAction SilentlyContinue
    } catch {
        # Non-fatal during background start
    }
}

function Open-AdmDashboardSafe {
    param([int]$TimeoutSec = 8)
    try {
        $dashHealth = Get-AdmDashboardHealth
        if ($dashHealth.Listening) {
            Start-Process "$AdmDashboardUrl/"
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
            Start-Process "$AdmDashboardUrl/"
            return
        }

        # Fallback: check Session Center
        $sc = Get-AdmSessionCenterHealth
        if ($sc.Listening) {
            Start-Process "$AdmSessionCenterUrl/"
            return
        }

        # Fallback: diagnostic status html
        $health = Get-AdmComprehensiveHealth
        $statusPath = Join-Path $env:TEMP "adm-status.html"
        $html = New-AdmStatusHtml -SupervisorStatus $health.SupervisorObject -WatcherStatus $health.WatcherObject -SessionCenter $health.SessionCenterObject -DashboardStatus $health.DashboardObject
        $html | Out-File -FilePath $statusPath -Encoding utf8
        Start-Process $statusPath
    } catch {
        Start-Process "$AdmDashboardUrl/"
    }
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

# Context Menu
$contextMenu = New-Object System.Windows.Forms.ContextMenuStrip

# 1. Open Dashboard (Default / Bold)
$menuOpenDash = New-Object System.Windows.Forms.ToolStripMenuItem
$menuOpenDash.Text = "Open Dashboard"
$menuOpenDash.Font = New-Object System.Drawing.Font($menuOpenDash.Font, [System.Drawing.FontStyle]::Bold)
$menuOpenDash.add_Click({
    Open-AdmDashboardSafe
})
$contextMenu.Items.Add($menuOpenDash) | Out-Null

# 2. Status Details
$menuStatus = New-Object System.Windows.Forms.ToolStripMenuItem
$menuStatus.Text = "Status"
$menuStatus.add_Click({
    try {
        $health = Get-AdmComprehensiveHealth
        $msg = "AI Development Manager Status:`n`n" +
               "Streamlit Dashboard: $($health.DashboardStatus)`n" +
               "Session Center:     $($health.SessionCenterStatus)`n" +
               "Command Watcher:    $($health.WatcherStatus)`n" +
               "Supervisor Task:    $($health.SupervisorStatus)"
        [System.Windows.Forms.MessageBox]::Show($msg, "ADM Health Status", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    } catch {
        [System.Windows.Forms.MessageBox]::Show("Could not check ADM status: $($_.Exception.Message)", "ADM Health Status", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    }
})
$contextMenu.Items.Add($menuStatus) | Out-Null

# 3. Start / Restart ADM Services
$menuRestart = New-Object System.Windows.Forms.ToolStripMenuItem
$menuRestart.Text = "Start / Restart Services"
$menuRestart.add_Click({
    try {
        Start-AdmServicesSafe
        $notifyIcon.ShowBalloonTip(3000, "ADM Services", "Scheduled tasks & Dashboard verified and triggered.", [System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        [System.Windows.Forms.MessageBox]::Show("Service start failed: $($_.Exception.Message)", "ADM Service Manager", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    }
})
$contextMenu.Items.Add($menuRestart) | Out-Null

# Separator
$contextMenu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

# 4. Exit Launcher
$menuExit = New-Object System.Windows.Forms.ToolStripMenuItem
$menuExit.Text = "Exit Launcher"
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
    Open-AdmDashboardSafe
})

# Show initial balloon notification
$notifyIcon.ShowBalloonTip(3000, "AI Development Manager", "ADM is running in the system tray. Double-click to open Dashboard.", [System.Windows.Forms.ToolTipIcon]::Info)

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
