param(
    [string]$TaskName = "AI Development Manager - Command Watcher",
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [Parameter(Mandatory=$true)][string]$CodexBin,
    [Parameter(Mandatory=$true)][string]$CodexHome,
    [Parameter(Mandatory=$true)][string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$AllowlistPath,
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [Parameter(Mandatory=$true)][string]$GcsObject,
    [string]$IngressFolderId,
    [string]$IngressOwner
)

. (Join-Path $PSScriptRoot "AdmHiddenLaunch.ps1")

# Production provenance contract: refuse to (re)install the Scheduled Task
# unless this exact checkout's HEAD was already proven by a TESTED evidence
# capture (manager.provenance capture-tested). This is what makes "source
# tests must pass before the Scheduled Task changes" fail-closed instead of
# a procedural reminder.
$activationOutput = & $PythonPath -m manager.provenance activate --repository-path $RepositoryPath --manager-home $ManagerHome 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "PROVENANCE_MISMATCH: cannot activate Watcher install, TESTED evidence missing or does not match this checkout's HEAD.`n$activationOutput"
    exit 1
}

$runner = Join-Path $RepositoryPath "manager\run_command_watcher.ps1"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"$PythonPath`" -RepositoryPath `"$RepositoryPath`" -ManagerHome `"$ManagerHome`" -CodexBin `"$CodexBin`" -CodexHome `"$CodexHome`" -PythonDeps `"$PythonDeps`" -AllowlistPath `"$AllowlistPath`" -GcsBucket `"$GcsBucket`" -GcsObject `"$GcsObject`" -IngressFolderId `"$IngressFolderId`" -IngressOwner `"$IngressOwner`""
$action = New-AdmHiddenScheduledTaskAction -RepositoryPath $RepositoryPath -WrapperName "command-watcher" -PowerShellArguments $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes 125)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
