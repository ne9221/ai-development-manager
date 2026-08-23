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
        # This suite's own fixtures live under $TestDrive, which Pester
        # itself creates under the real OS temp directory -- a pure test-
        # isolation artifact, not a real contaminated ADM_WORKSPACE_ROOT.
        # The contamination gate has its own dedicated, unmocked coverage
        # below ("Set-AdmWorkspacePointer workspace-root contamination
        # guard"); mocked out here so it never confuses $TestDrive itself
        # for real TEMP contamination.
        Mock Test-AdmWorkspaceRootContaminated { $false }
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

Describe "Set-AdmWorkspacePointer workspace-root contamination guard" {
    # Real-world root cause (fix/home-watcher-workspace-truth-bootstrap-
    # 20260823): an inherited ADM_WORKSPACE_ROOT that happened to resolve
    # under %TEMP% was trusted as canonical authority, so a real Task
    # ended up materializing working_directory under
    # %TEMP%\ai-development-manager. Test-AdmWorkspaceRootContaminated is
    # exercised for real here (unmocked), against the real
    # [IO.Path]::GetTempPath() -- deliberately not $TestDrive (see the
    # Describe block above for why that would be a false positive).
    BeforeEach {
        Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue
        Mock Set-AdmPersistentUserEnvironmentVariable {}
        $global:admContamCaseRoot = Join-Path $TestDrive ([Guid]::NewGuid().ToString("N"))
        $global:admContamRepo = Join-Path $global:admContamCaseRoot "checkout"
        New-Item -ItemType Directory -Force -Path $global:admContamRepo | Out-Null
    }
    AfterEach { Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue }

    It "flags the exact temp root and any subpath of it as contaminated" {
        $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        Test-AdmWorkspaceRootContaminated -CandidateRoot $temp | Should Be $true
        Test-AdmWorkspaceRootContaminated -CandidateRoot (Join-Path $temp "ai-development-manager") | Should Be $true
    }

    It "does not flag a legitimate root that merely shares a prefix with the temp path" {
        $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        $lookalike = $temp.Substring(0, $temp.Length - 1) + "-not-actually-temp"
        Test-AdmWorkspaceRootContaminated -CandidateRoot $lookalike | Should Be $false
    }

    It "refuses to trust an inherited ADM_WORKSPACE_ROOT that resolves under the real temp directory" {
        $contaminated = Join-Path ([IO.Path]::GetTempPath()) "ai-development-manager-workspace-root-contamination-test"
        $env:ADM_WORKSPACE_ROOT = $contaminated
        $pointer = Set-AdmWorkspacePointer -RepositoryPath $global:admContamRepo -ProjectId "ai-development-manager"
        $expectedRoot = Split-Path $global:admContamRepo -Parent
        $pointer | Should Be (Join-Path $expectedRoot "ai-development-manager")
        $env:ADM_WORKSPACE_ROOT | Should Be $expectedRoot
        Assert-MockCalled Set-AdmPersistentUserEnvironmentVariable -Times 1 -Exactly -Scope It -ParameterFilter { $Name -eq "ADM_WORKSPACE_ROOT" -and $Value -eq $expectedRoot }
    }

    # "A legitimate, non-temp inherited ADM_WORKSPACE_ROOT is still trusted"
    # is covered by the main "Set-AdmWorkspacePointer" Describe block above
    # (Test-AdmWorkspaceRootContaminated mocked $false there) rather than
    # here: every path available inside this sandboxed test run -- including
    # $TestDrive -- is itself a real subdirectory of the OS temp folder, so
    # exercising the real (unmocked) gate end-to-end here could only ever
    # legitimately return "contaminated" for any fixture this suite is
    # allowed to create.
}
