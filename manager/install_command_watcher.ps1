param(
    [string]$TaskName = "AI Development Manager - Command Watcher",
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [Parameter(Mandatory=$true)][string]$CodexBin,
    [Parameter(Mandatory=$true)][string]$CodexHome,
    [Parameter(Mandatory=$true)][string]$PythonDeps,
    [Parameter(Mandatory=$true)][string]$GcsBucket,
    [Parameter(Mandatory=$true)][string]$GcsObject
)

$runner = Join-Path $RepositoryPath "manager\run_command_watcher.ps1"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"$PythonPath`" -RepositoryPath `"$RepositoryPath`" -ManagerHome `"$ManagerHome`" -CodexBin `"$CodexBin`" -CodexHome `"$CodexHome`" -PythonDeps `"$PythonDeps`" -GcsBucket `"$GcsBucket`" -GcsObject `"$GcsObject`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
