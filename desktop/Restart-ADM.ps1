# Disable then re-enable + trigger the two ADM Scheduled Tasks.
#
# Like Stop-ADM.ps1, this never kills a currently-running child process --
# disabling only blocks the *next* trigger, and re-enabling plus one manual
# Start-ScheduledTask kick is the same idempotent, safe-to-repeat operation
# Start-ADM.ps1 already performs (MultipleInstances=IgnoreNew on both tasks
# means this can never create a second concurrent run).

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "AdmCommon.ps1")

& (Join-Path $PSScriptRoot "Stop-ADM.ps1")
Start-Sleep -Seconds 1
& (Join-Path $PSScriptRoot "Start-ADM.ps1")
