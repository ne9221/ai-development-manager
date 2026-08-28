$env:PESTER_TEST = '1'
$env:ADM_SKIP_DASHBOARD_LAUNCH = '1'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repository = (Resolve-Path (Join-Path $here "..")).Path
$watcherName = "AI Development Manager - Command Watcher"
$supervisorName = "AI Development Manager - Session Center Supervisor"
$driveIngressName = "AI Development Manager - Drive Dispatch Ingress"
$quotaRefreshName = "AI Development Manager - Quota Refresh"
$githubIngressName = "AI Development Manager - GitHub Dispatch Ingress"

. (Join-Path $here "AdmCommon.ps1")

# Fail-closed production-checkout guard, evaluated at discovery time (before
# any `Mock Get-ScheduledTask` below can shadow the real cmdlet, and before
# any BeforeEach in this file runs). This suite exercises New-RealHiddenWatcherVbs
# / New-AdmHiddenScheduledTaskAction against `$repository` (this file's own
# checkout root) -- those helpers have a REAL, unmocked side effect of writing
# `$repository\manager\generated\command-watcher.vbs` to disk, which is exactly
# the file the real, live Command Watcher Scheduled Task reads its launch
# arguments from on every tick. See Assert-AdmNotProductionCheckoutForTests in
# AdmCommon.ps1 for the check itself and its dedicated regression tests.
Assert-AdmNotProductionCheckoutForTests -Repository $repository -WatcherTaskName $watcherName

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
    . (Join-Path $here "..\manager\AdmHiddenLaunch.ps1")
    $runner = Join-Path $Repo "manager\run_command_watcher.ps1"
    $homeDir = if ($env:AI_MANAGER_HOME) { $env:AI_MANAGER_HOME } else { $AdmManagerHome }
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -PythonPath `"python.exe`" -RepositoryPath `"$Repo`" -ManagerHome `"$homeDir`" -CodexBin `"codex.exe`" -CodexHome `"codex-home`" -PythonDeps `"python-deps`" -AllowlistPath `"allowlist`" -GcsBucket `"bucket`" -GcsObject `"object`" -IngressFolderId `"folder`" -IngressOwner `"owner`" -ClaudeAccountsConfig `"accounts.json`" -WorkspaceRoot `"workspace-root`""
    $action = New-AdmHiddenScheduledTaskAction -RepositoryPath $Repo -WrapperName "command-watcher" -PowerShellArguments $arguments
    return $action.Arguments.Trim('"')
}

Describe "ADM watcher maintenance sentinel and task lifecycle" {
    BeforeEach {
        $global:admTestHome = Join-Path $TestDrive "adm-home"
        $global:admTestWatcher = $watcherName
        $global:admTestSupervisor = $supervisorName
        $global:admTestRepository = $repository
        $env:AI_MANAGER_HOME = $global:admTestHome
        Mock New-ScheduledTaskAction { param($Execute, $Argument) [pscustomobject]@{ Execute = $Execute; Arguments = $Argument } }
        $global:admTestWatcherVbs = New-RealHiddenWatcherVbs -Repo $global:admTestRepository
        $global:admTestWatcherState = "Ready"
        Mock Start-Sleep {}
        Mock Start-ScheduledTask {}
        Mock Start-Process {}
        Mock Show-AdmError {}
        Mock Get-AdmSessionCenterHealth { [PSCustomObject]@{ Listening = $false; Session = $null } }
        Mock Test-AdmDashboardRunning { $true }
        Mock Focus-AdmDashboard {}
        Mock Start-AdmDashboardProcess {}
        Mock Install-AdmShortcuts {}
        Mock Set-AdmWorkspacePointer {}
        Mock Get-ScheduledTask {
            param($TaskName)
            if ($TaskName -eq $global:admTestWatcher) { return New-TestWscriptTask $global:admTestWatcher $global:admTestWatcherVbs $global:admTestWatcherState }
            return New-TestTask $TaskName "Ready" $global:admTestRepository
        }
        Mock Get-ScheduledTaskInfo { [pscustomobject]@{ LastTaskResult = 0; LastRunTime = Get-Date } }
    }

    AfterEach { Remove-Item Env:AI_MANAGER_HOME -ErrorAction SilentlyContinue }

    It "Stop writes the sentinel before disable operations" {
        Mock Disable-ScheduledTask {
            (Test-Path -LiteralPath (Join-Path $global:admTestHome "runtime\watcher-maintenance.json")) | Should Be $true
        }
        & (Join-Path $here "Stop-ADM.ps1") | Out-Null
        Assert-MockCalled Disable-ScheduledTask -Times 5 -Exactly
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
        $scratchRepo = Join-Path $TestDrive "scratch-clone"
        New-Item -ItemType Directory -Force (Join-Path $scratchRepo "desktop") | Out-Null
        New-Item -ItemType Directory -Force (Join-Path $scratchRepo "manager") | Out-Null
        Copy-Item (Join-Path $here "AdmCommon.ps1") (Join-Path $scratchRepo "desktop")
        Copy-Item (Join-Path $here "Stop-ADM.ps1") (Join-Path $scratchRepo "desktop")
        Mock Disable-ScheduledTask { throw "Should not be called" }
        { & (Join-Path $scratchRepo "desktop\Stop-ADM.ps1") } | Should Throw
        Test-Path -LiteralPath (Join-Path $global:admTestHome "runtime\watcher-maintenance.json") | Should Be $false
    }
}

Describe "Set-AdmWorkspacePointer workspace-root canonical resolution" {
    BeforeEach {
        Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue
        Mock Set-AdmPersistentUserEnvironmentVariable {}
        $global:admTestRoot = Join-Path $TestDrive "workspace-test"
        New-Item -ItemType Directory -Force -Path $global:admTestRoot | Out-Null
        $global:admRepoA = Join-Path $global:admTestRoot "checkout-a"
        $global:admRepoB = Join-Path $global:admTestRoot "checkout-b"
        New-Item -ItemType Directory -Force -Path $global:admRepoA | Out-Null
        New-Item -ItemType Directory -Force -Path $global:admRepoB | Out-Null
        $global:admWorkspaceRoot = Join-Path $TestDrive "workspace-root"
        New-Item -ItemType Directory -Force -Path $global:admWorkspaceRoot | Out-Null
        Mock Test-AdmWorkspaceRootContaminated { $false }
    }
    AfterEach { Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue }

    It "creates the junction and persists ADM_WORKSPACE_ROOT when neither exists yet" {
        $pointer = Set-AdmWorkspacePointer -RepositoryPath $global:admRepoA -ProjectId "ai-development-manager"
        $expectedRoot = Split-Path $global:admRepoA -Parent
        Assert-MockCalled Set-AdmPersistentUserEnvironmentVariable -Times 1 -Exactly -Scope It -ParameterFilter { $Name -eq "ADM_WORKSPACE_ROOT" -and $Value -eq $expectedRoot }
        (Get-Item -LiteralPath $pointer).LinkType | Should Be "Junction"
        [IO.Path]::GetFullPath(@((Get-Item -LiteralPath $pointer).Target)[0]).TrimEnd('\') | Should Be ([IO.Path]::GetFullPath($global:admRepoA).TrimEnd('\'))
    }

    It "repoints the junction when the current checkout changes (the stale-checkout P0 case)" {
        $env:ADM_WORKSPACE_ROOT = $global:admWorkspaceRoot
        Set-AdmWorkspacePointer -RepositoryPath $global:admRepoA -ProjectId "ai-development-manager" | Out-Null
        $pointer = Set-AdmWorkspacePointer -RepositoryPath $global:admRepoB -ProjectId "ai-development-manager"
        [IO.Path]::GetFullPath(@((Get-Item -LiteralPath $pointer).Target)[0]).TrimEnd('\') | Should Be ([IO.Path]::GetFullPath($global:admRepoB).TrimEnd('\'))
    }

    It "is idempotent when repointed to the same checkout (no-op, no re-persist)" {
        Set-AdmWorkspacePointer -RepositoryPath $global:admRepoA -ProjectId "ai-development-manager" | Out-Null
        Set-AdmWorkspacePointer -RepositoryPath $global:admRepoA -ProjectId "ai-development-manager" | Out-Null
        Assert-MockCalled Set-AdmPersistentUserEnvironmentVariable -Times 1 -Exactly -Scope It
    }

    It "fails closed instead of hijacking a real, non-junction directory" {
        $env:ADM_WORKSPACE_ROOT = $global:admWorkspaceRoot
        $collision = Join-Path $global:admWorkspaceRoot "ai-development-manager"
        New-Item -ItemType Directory -Force -Path $collision | Out-Null
        '{"important": "not adm-managed"}' | Set-Content (Join-Path $collision "real-file.json")
        { Set-AdmWorkspacePointer -RepositoryPath $global:admRepoA -ProjectId "ai-development-manager" } | Should Throw
        Test-Path (Join-Path $collision "real-file.json") | Should Be $true
    }

    It "defaults the workspace root to the repository parent when ADM_WORKSPACE_ROOT is unset" {
        Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue
        $pointer = Set-AdmWorkspacePointer -RepositoryPath $global:admRepoA -ProjectId "ai-development-manager"
        $pointer | Should Be (Join-Path (Split-Path $global:admRepoA -Parent) "ai-development-manager")
    }
}

Describe "Dashboard single-instance lifecycle & focus" {
    BeforeEach {
        Mock Start-Process {}
    }

    It "Focus-AdmDashboard triggers Start-Process when window is not foregrounded" {
        Focus-AdmDashboard -Url "http://localhost:8501"
        Assert-MockCalled Start-Process -Times 1 -Exactly -Scope It
    }
}

Describe "Shortcut installation (AI 開發管理器)" {
    BeforeEach {
        $script:testShortcutsRoot = Join-Path $TestDrive "shortcuts"
        New-Item -ItemType Directory -Force -Path $script:testShortcutsRoot | Out-Null
        $script:dummyRepo = Join-Path $script:testShortcutsRoot "dummy-repo"
        New-Item -ItemType Directory -Force -Path (Join-Path $script:dummyRepo "desktop") | Out-Null
        'WScript.Quit 0' | Set-Content (Join-Path $script:dummyRepo "desktop\Start-ADM.vbs")
    }

    It "Install-AdmShortcuts executes without error on valid repository" {
        $testTarget = Join-Path $script:testShortcutsRoot "target-folder"
        { Install-AdmShortcuts -RepositoryPath $script:dummyRepo -TargetFolders @($testTarget) } | Should Not Throw
        Test-Path (Join-Path $testTarget "$AdmShortcutName.lnk") | Should Be $true
    }

    It "Install-AdmShortcuts fails closed when Start-ADM.vbs is missing" {
        Remove-Item -LiteralPath (Join-Path $script:dummyRepo "desktop\Start-ADM.vbs") -Force
        { Install-AdmShortcuts -RepositoryPath $script:dummyRepo } | Should Throw
    }
}

Describe "Watcher hidden-VBS (wscript.exe) identity guard" {
    BeforeEach {
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
        $actualHome = if ($env:AI_MANAGER_HOME) { $env:AI_MANAGER_HOME } else { $AdmManagerHome }
        (Get-Content -LiteralPath $wrongVbs -Raw).Replace($actualHome, $wrongHome) | Set-Content -LiteralPath $wrongVbs -Encoding ASCII
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

Describe "Assert-AdmNotProductionCheckoutForTests -- refuses to run test suites that would overwrite a live watcher launcher" {
    BeforeEach {
        $script:guardRoot = Join-Path $TestDrive ([Guid]::NewGuid().ToString("N"))
        $script:guardRealRepo = Join-Path $script:guardRoot "real-production-checkout"
        $script:guardOtherRepo = Join-Path $script:guardRoot "unrelated-checkout"
        $script:guardVbsDir = Join-Path $script:guardRealRepo "manager\generated"
        New-Item -ItemType Directory -Force -Path $script:guardVbsDir | Out-Null
        New-Item -ItemType Directory -Force -Path $script:guardOtherRepo | Out-Null
        $script:guardVbsPath = Join-Path $script:guardVbsDir "command-watcher.vbs"
        Set-Content -LiteralPath $script:guardVbsPath -Value "' placeholder generated content" -Encoding ASCII
    }

    It "throws when the real registered task's vbs already lives at this exact repository's generated path (the incident this guard exists to prevent)" {
        Mock Get-ScheduledTask {
            [pscustomobject]@{ Actions = @([pscustomobject]@{ Execute = "wscript.exe"; Arguments = "`"$script:guardVbsPath`"" }) }
        }
        { Assert-AdmNotProductionCheckoutForTests -Repository $script:guardRealRepo -WatcherTaskName $watcherName } | Should Throw "PESTER_PRODUCTION_CHECKOUT_GUARD"
    }

    It "does not throw when the registered task's vbs points at a different repository" {
        Mock Get-ScheduledTask {
            [pscustomobject]@{ Actions = @([pscustomobject]@{ Execute = "wscript.exe"; Arguments = "`"$script:guardVbsPath`"" }) }
        }
        { Assert-AdmNotProductionCheckoutForTests -Repository $script:guardOtherRepo -WatcherTaskName $watcherName } | Should Not Throw
    }

    It "does not throw when no Command Watcher task is registered at all" {
        Mock Get-ScheduledTask { $null }
        { Assert-AdmNotProductionCheckoutForTests -Repository $script:guardRealRepo -WatcherTaskName $watcherName } | Should Not Throw
    }

    It "does not throw when the registered task's vbs path no longer exists on disk" {
        Remove-Item -LiteralPath $script:guardVbsPath -Force
        Mock Get-ScheduledTask {
            [pscustomobject]@{ Actions = @([pscustomobject]@{ Execute = "wscript.exe"; Arguments = "`"$script:guardVbsPath`"" }) }
        }
        { Assert-AdmNotProductionCheckoutForTests -Repository $script:guardRealRepo -WatcherTaskName $watcherName } | Should Not Throw
    }

    It "does not throw for a legacy directly-registered powershell.exe action (not wscript.exe)" {
        Mock Get-ScheduledTask {
            [pscustomobject]@{ Actions = @([pscustomobject]@{ Execute = "powershell.exe"; Arguments = "-File `"$script:guardVbsPath`"" }) }
        }
        { Assert-AdmNotProductionCheckoutForTests -Repository $script:guardRealRepo -WatcherTaskName $watcherName } | Should Not Throw
    }
}
