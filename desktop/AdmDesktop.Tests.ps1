$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repository = (Resolve-Path (Join-Path $here "..")).Path
$watcherName = "AI Development Manager - Command Watcher"
$supervisorName = "AI Development Manager - Session Center Supervisor"
# Dot-sourced once at file scope (not just inside the one It that used to do
# this locally) so Mock can see Set-AdmWorkspacePointer/Set-AdmPersistent
# UserEnvironmentVariable from every Describe block below, including ones
# that invoke Start-ADM.ps1/Stop-ADM.ps1 as a child script rather than
# calling these functions directly.
. (Join-Path $here "AdmCommon.ps1")

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
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -RepositoryPath `"$Repo`""
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
        $global:admTestWatcherState = "Ready"
        Mock Start-Sleep {}
        Mock Start-ScheduledTask {}
        Mock Start-Process {}
        # Set-AdmWorkspacePointer touches real filesystem junctions and a
        # real registry-level User environment variable -- it has its own
        # isolated test coverage below. It must never run for real just
        # because these tests exercise Start-ADM.ps1 end-to-end.
        Mock Set-AdmWorkspacePointer {}
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
        Test-Path -LiteralPath (Join-Path $global:admTestHome "runtime\watcher-maintenance-last.json") | Should Be $true
    }

    It "a scratch clone identity cannot disable the production watcher" {
        $scratchTask = New-TestTask $watcherName "Ready" (Join-Path $global:admTestHome "scratch")
        Test-AdmWatcherTaskIdentity -Task $scratchTask -RepositoryPath $repository | Should Be $false
    }
}

Describe "Set-AdmWorkspacePointer" {
    # All paths here live under $TestDrive (Pester's own disposable temp
    # directory) and Set-AdmPersistentUserEnvironmentVariable is always
    # mocked -- this suite must never create a real junction anywhere under
    # the real Documents\ChatGPT\AI tree or persist a real registry-level
    # ADM_WORKSPACE_ROOT on the machine running the tests.
    BeforeEach {
        # A fresh, uniquely-named case root per It -- never a fixed path
        # reused across tests in this Describe -- so one test's junction/
        # directory can never leak into and silently change the outcome of
        # another (Pester's $TestDrive itself is shared across every It in a
        # Describe, it is not reset between them).
        $global:admCaseRoot = Join-Path $TestDrive ([Guid]::NewGuid().ToString("N"))
        $global:admWorkspaceRoot = Join-Path $global:admCaseRoot "workspace-root"
        $global:admRepoA = Join-Path $global:admCaseRoot "checkout-a"
        $global:admRepoB = Join-Path $global:admCaseRoot "checkout-b"
        New-Item -ItemType Directory -Force -Path $global:admWorkspaceRoot, $global:admRepoA, $global:admRepoB | Out-Null
        Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue
        Mock Set-AdmPersistentUserEnvironmentVariable {}
    }
    AfterEach { Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue }

    It "creates the junction and persists ADM_WORKSPACE_ROOT when neither exists yet" {
        # ADM_WORKSPACE_ROOT deliberately left unset here: persistence is
        # only ever triggered by a value actually changing, so pre-seeding
        # the env var to what the function would compute anyway would make
        # this assert a call that correctly never happens.
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
        # ADM_WORKSPACE_ROOT left unset: the first call persists the
        # computed default exactly once, then updates $env:ADM_WORKSPACE_ROOT
        # in-process, so the second call sees it already matches and must
        # not persist again.
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

    It "defaults the workspace root to the repository's parent when ADM_WORKSPACE_ROOT is unset" {
        $pointer = Set-AdmWorkspacePointer -RepositoryPath $global:admRepoA -ProjectId "ai-development-manager"
        $pointer | Should Be (Join-Path (Split-Path $global:admRepoA -Parent) "ai-development-manager")
    }
}

Describe "Watcher hidden-VBS (wscript.exe) identity guard" {
    BeforeEach {
        . (Join-Path $here "AdmCommon.ps1")
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

    It "still accepts a fully-proven legacy powershell.exe production task" {
        $task = New-TestTask $watcherName "Ready" $script:repoA
        Test-AdmWatcherTaskIdentity -Task $task -RepositoryPath $script:repoA | Should Be $true
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
