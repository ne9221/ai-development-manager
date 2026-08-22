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

function New-TestWscriptTask([string]$Name, [string]$VbsPath, [string]$State = "Ready") {
    [pscustomobject]@{
        TaskName = $Name
        TaskPath = "\"
        State = $State
        Settings = [pscustomobject]@{ Enabled = ($State -ne "Disabled") }
        Actions = @([pscustomobject]@{
            Execute = "wscript.exe"
            Arguments = "`"$VbsPath`""
        })
    }
}

function New-RealHiddenWatcherVbs([string]$Repo) {
    # Exercises the actual production generator (manager\AdmHiddenLaunch.ps1)
    # so these tests verify Test-AdmWatcherTaskIdentity against the real
    # generated wrapper shape, not a hand-rolled stand-in.
    . (Join-Path $here "..\manager\AdmHiddenLaunch.ps1")
    $runner = Join-Path $Repo "manager\run_command_watcher.ps1"
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"python.exe`" -RepositoryPath `"$Repo`" -ManagerHome `"$AdmManagerHome`" -CodexBin `"codex.exe`" -CodexHome `"codex-home`" -PythonDeps `"python-deps`" -AllowlistPath `"allowlist`" -GcsBucket `"bucket`" -GcsObject `"object`" -IngressFolderId `"folder`" -IngressOwner `"owner`" -ClaudeAccountsConfig `"accounts.json`""
    $action = New-AdmHiddenScheduledTaskAction -RepositoryPath $Repo -WrapperName "command-watcher" -PowerShellArguments $arguments
    return $action.Arguments.Trim('"')
}

Describe "ADM watcher maintenance sentinel" {
    BeforeEach {
        $global:admTestHome = Join-Path $TestDrive "adm-home"
        $global:admTestWatcher = $watcherName
        $global:admTestSupervisor = $supervisorName
        $global:admTestRepository = $repository
        $env:AI_MANAGER_HOME = $global:admTestHome
        . (Join-Path $here "AdmCommon.ps1")
        Mock New-ScheduledTaskAction { param($Execute, $Argument) [pscustomobject]@{ Execute = $Execute; Arguments = $Argument } }
        $global:admTestWatcherVbs = New-RealHiddenWatcherVbs -Repo $global:admTestRepository
        $global:admTestWatcherState = "Ready"
        Mock Start-Sleep {}
        Mock Start-ScheduledTask {}
        Mock Start-Process {}
        Mock Get-ScheduledTask {
            param($TaskName)
            if ($TaskName -eq $global:admTestWatcher) { return New-TestWscriptTask $global:admTestWatcher $global:admTestWatcherVbs $global:admTestWatcherState }
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
        Test-Path -LiteralPath (Join-Path $global:admTestHome "runtime\watcher-maintenance-last.json") | Should Be $true
    }

    It "a scratch clone identity cannot disable the production watcher" {
        . (Join-Path $here "AdmCommon.ps1")
        $scratchTask = New-TestTask $watcherName "Ready" (Join-Path $global:admTestHome "scratch")
        Test-AdmWatcherTaskIdentity -Task $scratchTask -RepositoryPath $repository | Should Be $false
    }
}

Describe "Watcher hidden-VBS (wscript.exe) identity guard" {
    BeforeEach {
        . (Join-Path $here "AdmCommon.ps1")
        Mock New-ScheduledTaskAction { param($Execute, $Argument) [pscustomobject]@{ Execute = $Execute; Arguments = $Argument } }
        $script:testRoot = Join-Path $TestDrive "wscript-identity"
        New-Item -ItemType Directory -Force -Path $script:testRoot | Out-Null
        $script:repoA = Join-Path $script:testRoot "repo-a"
        $script:repoB = Join-Path $script:testRoot "repo-b"
        New-Item -ItemType Directory -Force -Path (Join-Path $script:repoA "manager") | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $script:repoB "manager") | Out-Null
        $script:realVbsPath = New-RealHiddenWatcherVbs -Repo $script:repoA
    }

    It "accepts the real generated wscript production task for its own repository" {
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $true
    }

    It "rejects the correct VBS bound to a different (wrong) repository" {
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoB | Should Be $false
    }

    It "rejects an arbitrary wscript target that is not the expected generated VBS path" {
        $arbitraryVbs = Join-Path $script:repoA "manager\generated\something-else.vbs"
        New-Item -ItemType Directory -Force -Path (Split-Path $arbitraryVbs) | Out-Null
        Copy-Item -LiteralPath $script:realVbsPath -Destination $arbitraryVbs
        $task = New-TestWscriptTask $watcherName $arbitraryVbs
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects a wrong/hand-edited VBS content at the exact expected path" {
        Set-Content -LiteralPath $script:realVbsPath -Value @(
            "Set shell = CreateObject(""WScript.Shell"")",
            "shell.Run ""cmd.exe /c calc.exe"", 0, False"
        ) -Encoding ASCII
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects an otherwise valid wrapper with an appended hidden command" {
        Add-Content -LiteralPath $script:realVbsPath -Value 'shell.Run "cmd.exe /c calc.exe", 0, False' -Encoding ASCII
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects a wrapper whose ManagerHome is not this desktop's production binding" {
        $wrongHome = Join-Path $script:testRoot "wrong-manager-home"
        $wrongVbs = New-RealHiddenWatcherVbs -Repo $script:repoA
        (Get-Content -LiteralPath $wrongVbs -Raw).Replace($AdmManagerHome, $wrongHome) | Set-Content -LiteralPath $wrongVbs -Encoding ASCII
        $task = New-TestWscriptTask $watcherName $wrongVbs
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects a wrapper that prepends a PowerShell command before the correct binding" {
        (Get-Content -LiteralPath $script:realVbsPath -Raw).Replace('powershell.exe -NoProfile', 'powershell.exe -Command calc.exe -NoProfile') | Set-Content -LiteralPath $script:realVbsPath -Encoding ASCII
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects a stale scratch checkout's own generated VBS as if it were production" {
        $staleRepo = Join-Path $script:testRoot "stale-scratch-checkout"
        New-Item -ItemType Directory -Force -Path (Join-Path $staleRepo "manager") | Out-Null
        $staleVbs = New-RealHiddenWatcherVbs -Repo $staleRepo
        $task = New-TestWscriptTask $watcherName $staleVbs
        # A stale scratch checkout must not be able to pass identity for the
        # real production repository, even though its own VBS is a
        # legitimately-generated wrapper -- just for the wrong repo.
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects malformed arguments (unquoted path)" {
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        $task.Actions[0].Arguments = $script:realVbsPath
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects malformed arguments (trailing garbage after the quoted path)" {
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        $task.Actions[0].Arguments = "`"$($script:realVbsPath)`" -extra garbage"
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects a missing VBS file at the expected path" {
        Remove-Item -LiteralPath $script:realVbsPath -Force
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects a legacy powershell.exe task because it can flash a console" {
        $task = New-TestTask $watcherName "Ready" $script:repoA
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "rejects an unrecognized action executable" {
        $task = New-TestWscriptTask $watcherName $script:realVbsPath
        $task.Actions[0].Execute = "cmd.exe"
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $false
    }

    It "the production launcher shape stays wscript.exe (hidden), never a directly-registered powershell.exe" {
        $helperContent = Get-Content -Raw (Join-Path $here "..\manager\AdmHiddenLaunch.ps1")
        $helperContent | Should Match 'New-ScheduledTaskAction -Execute "wscript\.exe"'
    }
}
