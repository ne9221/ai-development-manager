# Master installer for all ADM Windows Scheduled Tasks and Desktop/Start Menu/Startup shortcuts.
# Configures durable triggers, auto-recovery settings, and canonical paths.

param(
    [string]$RepositoryPath = $(Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "C:\Users\EE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$ManagerHome = "C:\Users\EE\.ai-development-manager",
    [string]$PythonDeps = "C:\Users\EE\Documents\ChatGPT\AI\ai-development-manager-codex3\.test-deps\Lib\site-packages",
    [string]$CodexBin = "C:\Users\EE\AppData\Roaming\npm\codex.cmd",
    [string]$CodexHome = "C:\Users\EE\.codex",
    [string]$AllowlistPath,
    [string]$GcsBucket = "adm-lock-smoke-551449082603-20260813-0147",
    [string]$GcsObject = "worktree-locks/global-registry.json",
    [string]$IngressFolderId = "1rowrUcrKP9v5qEzJ_LUPsEt2tCCr7Dxw",
    [string]$IngressOwner = "neen971229@gmail.com",
    [string]$ClaudeAccountsConfig = "C:\Users\EE\.ai-development-manager\config\claude_accounts.json",
    [string]$GoogleDriveToken = "C:\Users\EE\.ai-development-manager\google-drive-token.json",
    [string]$ClaudePayload = "C:\Users\EE\.claude\statusline-payload.json",
    [string]$StateFile = "C:\Users\EE\.ai-development-manager\session_center_supervisor_state.json",
    [string]$WorkspaceRoot = "C:\Users\EE\Documents\ChatGPT\AI"
)

$ErrorActionPreference = "Stop"
. (Join-Path $RepositoryPath "desktop\AdmCommon.ps1")

$resolvedRepo = [IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
if (-not $AllowlistPath) {
    $AllowlistPath = Join-Path $resolvedRepo "templates\watcher_allowlist.json"
}

Write-Host "Installing ADM Scheduled Tasks and Shortcuts for: $resolvedRepo" -ForegroundColor Cyan

# 1. Command Watcher
& (Join-Path $resolvedRepo "manager\install_command_watcher.ps1") `
    -PythonPath $PythonPath `
    -RepositoryPath $resolvedRepo `
    -ManagerHome $ManagerHome `
    -CodexBin $CodexBin `
    -CodexHome $CodexHome `
    -PythonDeps $PythonDeps `
    -AllowlistPath $AllowlistPath `
    -GcsBucket $GcsBucket `
    -GcsObject $GcsObject `
    -IngressFolderId $IngressFolderId `
    -IngressOwner $IngressOwner `
    -EmbeddedIngress "0" `
    -ClaudeAccountsConfig $ClaudeAccountsConfig `
    -WorkspaceRoot $WorkspaceRoot

# 2. Drive Dispatch Ingress
& (Join-Path $resolvedRepo "manager\install_drive_dispatch_ingress.ps1") `
    -PythonPath $PythonPath `
    -RepositoryPath $resolvedRepo `
    -ManagerHome $ManagerHome `
    -PythonDeps $PythonDeps `
    -IngressFolderId $IngressFolderId `
    -IngressOwner $IngressOwner `
    -GcsBucket $GcsBucket `
    -GoogleDriveToken $GoogleDriveToken `
    -ClaudeAccountsConfig $ClaudeAccountsConfig `
    -WorkspaceRoot $WorkspaceRoot

# 3. Session Center Supervisor
$pythoncore = "C:\Users\EE\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path -LiteralPath $pythoncore)) { $pythoncore = $PythonPath }

& (Join-Path $resolvedRepo "manager\install_session_center_supervisor.ps1") `
    -PythonPath $pythoncore `
    -RepositoryPath $resolvedRepo `
    -ManagerHome $ManagerHome `
    -StateFile $StateFile `
    -PythonDeps $resolvedRepo `
    -AllowlistPath $AllowlistPath `
    -Port 8765 `
    -GcsBucket $GcsBucket

# 4. Quota Refresh
& (Join-Path $resolvedRepo "manager\install_scheduler.ps1") `
    -PythonPath $pythoncore `
    -RepositoryPath $resolvedRepo `
    -ManagerHome $ManagerHome `
    -CodexBin $CodexBin `
    -CodexHome $CodexHome `
    -PythonDeps $resolvedRepo `
    -ClaudePayload $ClaudePayload

# 5. GitHub Dispatch Ingress (if available)
$installGh = Join-Path $resolvedRepo "manager\install_github_dispatch_ingress.ps1"
if (Test-Path -LiteralPath $installGh) {
    & $installGh `
        -PythonPath $PythonPath `
        -RepositoryPath $resolvedRepo `
        -ManagerHome $ManagerHome `
        -PythonDeps $PythonDeps `
        -IngressRepo "ne9221/ai-development-manager" `
        -IngressBranch "dispatch-requests" `
        -IngressPath "dispatch-requests" `
        -GcsBucket $GcsBucket `
        -GoogleDriveToken $GoogleDriveToken `
        -ClaudeAccountsConfig $ClaudeAccountsConfig `
        -WorkspaceRoot $WorkspaceRoot
}

# 6. Shortcuts (Desktop, Start Menu, Startup)
Install-AdmShortcuts -RepositoryPath $resolvedRepo

Write-Host "All ADM Scheduled Tasks and Shortcuts successfully installed." -ForegroundColor Green
