$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repository = (Resolve-Path (Join-Path $here "..")).Path
$watcherName = "AI Development Manager - Command Watcher"
$supervisorName = "AI Development Manager - Session Center Supervisor"

function New-TestTask([string]$Name, [string]$State = "Ready", [string]$Repo = $repository) {
    [pscustomobject]@{
        TaskName = $Name
        TaskPath = "\"
        State = $State
        Settings = [pscustomobject]@{ Enabled = ($State -ne "Disabled") }
        Actions = @([pscustomobject]@{
            Execute = "powershell.exe"
            Arguments = "-File '$Repo\manager\run_command_watcher.ps1' -RepositoryPath '$Repo'"
        })
    }
}

Describe "ADM watcher maintenance sentinel" {
    BeforeEach {
        $global:admTestHome = Join-Path $TestDrive "adm-home"
        $global:admTestWatcher = $watcherName
        $global:admTestSupervisor = $supervisorName
        $global:admTestRepository = $repository
        $env:AI_MANAGER_HOME = $global:admTestHome
        $global:admTestWatcherState = "Ready"
        Mock Start-Sleep {}
        Mock Start-ScheduledTask {}
        Mock Start-Process {}
        Mock Get-ScheduledTask {
            param($TaskName)
            if ($TaskName -eq $global:admTestWatcher) { return New-TestTask $global:admTestWatcher $global:admTestWatcherState $global:admTestRepository }
            return New-TestTask $global:admTestSupervisor "Ready" $global:admTestRepository
        }
        Mock Get-ScheduledTaskInfo { [pscustomobject]@{ LastTaskResult = 0; LastRunTime = Get-Date } }
    }

    AfterEach { Remove-Item Env:AI_MANAGER_HOME -ErrorAction SilentlyContinue }

    It "Stop writes the sentinel before either disable operation" {
        Mock Disable-ScheduledTask {
            (Test-Path -LiteralPath (Join-Path $global:admTestHome "runtime\watcher-maintenance.json")) | Should Be $true
        }
        & (Join-Path $here "Stop-ADM.ps1") | Out-Null
        Assert-MockCalled Disable-ScheduledTask -Times 2 -Exactly
        $sentinel = Get-Content -Raw (Join-Path $global:admTestHome "runtime\watcher-maintenance.json") | ConvertFrom-Json
        $sentinel.reason | Should Be "Stop-ADM intentional maintenance"
        $sentinel.source | Should Be $repository
    }

    It "Start clears the sentinel only after the disabled watcher is restored" {
        New-Item -ItemType Directory -Force (Join-Path $global:admTestHome "runtime") | Out-Null
        '{}' | Set-Content (Join-Path $global:admTestHome "runtime\watcher-maintenance.json")
        $global:admTestWatcherState = "Disabled"
        Mock Enable-ScheduledTask { $global:admTestWatcherState = "Ready" }
        & (Join-Path $here "Start-ADM.ps1") | Out-Null
        Assert-MockCalled Enable-ScheduledTask -ParameterFilter { $TaskName -eq $watcherName } -Times 1 -Exactly
        Test-Path -LiteralPath (Join-Path $global:admTestHome "runtime\watcher-maintenance.json") | Should Be $false
    }

    It "a scratch clone identity cannot disable the production watcher" {
        . (Join-Path $here "AdmCommon.ps1")
        $scratchTask = New-TestTask $watcherName "Ready" (Join-Path $global:admTestHome "scratch")
        Test-AdmWatcherTaskIdentity -Task $scratchTask -RepositoryPath $repository | Should Be $false
    }
}
