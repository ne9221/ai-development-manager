# Disable then re-enable + trigger the ADM Scheduled Tasks and refresh Dashboard.

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "AdmCommon.ps1")

& (Join-Path $PSScriptRoot "Stop-ADM.ps1")
Start-Sleep -Seconds 1
& (Join-Path $PSScriptRoot "Start-ADM.ps1")
