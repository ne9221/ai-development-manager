param(
    [string]$TaskName = "AI Development Manager - Session Center Supervisor",
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RepositoryPath,
    [Parameter(Mandatory=$true)][string]$ManagerHome,
    [Parameter(Mandatory=$true)][string]$StateFile,
    [string]$PythonDeps,
    [string]$AllowlistPath,
    [int]$Port = 8765
)

$runner = Join-Path $RepositoryPath "manager\run_session_center_supervisor.ps1"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"$PythonPath`" -RepositoryPath `"$RepositoryPath`" -ManagerHome `"$ManagerHome`" -StateFile `"$StateFile`""
if ($PythonDeps) { $arguments += " -PythonDeps `"$PythonDeps`"" }
if ($AllowlistPath) { $arguments += " -AllowlistPath `"$AllowlistPath`"" }
$arguments += " -Port $Port"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
