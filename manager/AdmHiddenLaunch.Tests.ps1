# Regression coverage for the shared, system-level fail-closed guard in
# New-AdmHiddenScheduledTaskAction (manager\AdmHiddenLaunch.ps1) -- see
# fix/pester-scheduled-task-isolation-20260823.
#
# This is the one function every hidden Scheduled Task installer
# (install_command_watcher.ps1, install_drive_dispatch_ingress.ps1, and any
# future one) routes its generated .vbs wrapper write through, so a guard
# here protects all of them regardless of whether a given installer script
# itself remembers to pass an isolated -GeneratedWrapperDir. This suite
# never calls Register-ScheduledTask or any other real Task Scheduler
# cmdlet: New-AdmHiddenScheduledTaskAction itself never calls one -- it only
# constructs an in-memory New-ScheduledTaskAction CIM object and writes a
# single .vbs file.
#
# Every "repository" used below is a throwaway directory under $TestDrive,
# never the real production checkout -- the guard's equal/descendant-path
# logic is exercised against a stand-in that has the same shape
# ($RepositoryPath\manager\generated) as the real one, so this proves the
# guard's actual comparison logic without ever touching or naming the real
# checkout path.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here "AdmHiddenLaunch.ps1")

Describe "New-AdmHiddenScheduledTaskAction -- ADM_PESTER_TEST_ACTIVE fail-closed guard" {
    BeforeEach {
        $global:admCase = Join-Path $TestDrive ([Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $global:admCase | Out-Null
        $global:admFakeRepo = Join-Path $global:admCase "fake-repo"
        New-Item -ItemType Directory -Force -Path $global:admFakeRepo | Out-Null
        # The "production-shaped" directory: exactly what
        # New-AdmHiddenScheduledTaskAction falls back to when
        # -GeneratedWrapperDir is omitted. The guard must refuse to write
        # here (or anywhere under here) whenever the sentinel is set.
        $global:admProductionShapedDir = Join-Path $global:admFakeRepo "manager\generated"
        $global:admIsolatedDir = Join-Path $global:admCase "isolated-wrapper"
        $env:ADM_PESTER_TEST_ACTIVE = "1"
    }

    AfterEach {
        Remove-Item Env:ADM_PESTER_TEST_ACTIVE -ErrorAction SilentlyContinue
    }

    It "A: fails closed before any wrapper write when GeneratedWrapperDir is omitted" {
        { New-AdmHiddenScheduledTaskAction -RepositoryPath $global:admFakeRepo -WrapperName "drive-dispatch-ingress" -PowerShellArguments "-NoProfile test" } | Should Throw "GENERATED_WRAPPER_DIR_REQUIRED_UNDER_TEST"
        (Test-Path -LiteralPath $global:admProductionShapedDir) | Should Be $false
    }

    It "B1: fails closed when GeneratedWrapperDir equals the production-shaped generated directory" {
        { New-AdmHiddenScheduledTaskAction -RepositoryPath $global:admFakeRepo -WrapperName "drive-dispatch-ingress" -PowerShellArguments "-NoProfile test" -GeneratedWrapperDir $global:admProductionShapedDir } | Should Throw "GENERATED_WRAPPER_DIR_FORBIDDEN_UNDER_TEST"
        (Test-Path -LiteralPath (Join-Path $global:admProductionShapedDir "drive-dispatch-ingress.vbs")) | Should Be $false
    }

    It "B2: fails closed when GeneratedWrapperDir is a descendant of the production-shaped generated directory" {
        $descendant = Join-Path $global:admProductionShapedDir "nested"
        { New-AdmHiddenScheduledTaskAction -RepositoryPath $global:admFakeRepo -WrapperName "drive-dispatch-ingress" -PowerShellArguments "-NoProfile test" -GeneratedWrapperDir $descendant } | Should Throw "GENERATED_WRAPPER_DIR_FORBIDDEN_UNDER_TEST"
        (Test-Path -LiteralPath $descendant) | Should Be $false
    }

    It "C: succeeds when GeneratedWrapperDir is an isolated directory outside the repository tree" {
        $action = New-AdmHiddenScheduledTaskAction -RepositoryPath $global:admFakeRepo -WrapperName "drive-dispatch-ingress" -PowerShellArguments "-NoProfile test" -GeneratedWrapperDir $global:admIsolatedDir
        $action.Execute | Should Be "wscript.exe"
        $vbsPath = Join-Path $global:admIsolatedDir "drive-dispatch-ingress.vbs"
        (Test-Path -LiteralPath $vbsPath) | Should Be $true
        (Test-Path -LiteralPath $global:admProductionShapedDir) | Should Be $false
    }

    It "E: the Command Watcher wrapper path is protected identically -- cannot reach the production-shaped generated directory without an isolated override" {
        { New-AdmHiddenScheduledTaskAction -RepositoryPath $global:admFakeRepo -WrapperName "command-watcher" -PowerShellArguments "-NoProfile test" } | Should Throw "GENERATED_WRAPPER_DIR_REQUIRED_UNDER_TEST"
        (Test-Path -LiteralPath (Join-Path $global:admProductionShapedDir "command-watcher.vbs")) | Should Be $false

        $action = New-AdmHiddenScheduledTaskAction -RepositoryPath $global:admFakeRepo -WrapperName "command-watcher" -PowerShellArguments "-NoProfile test" -GeneratedWrapperDir $global:admIsolatedDir
        $action.Execute | Should Be "wscript.exe"
        (Test-Path -LiteralPath (Join-Path $global:admIsolatedDir "command-watcher.vbs")) | Should Be $true
    }

    It "does not require GeneratedWrapperDir when the test sentinel is absent -- production behavior unchanged" {
        Remove-Item Env:ADM_PESTER_TEST_ACTIVE -ErrorAction SilentlyContinue
        $action = New-AdmHiddenScheduledTaskAction -RepositoryPath $global:admFakeRepo -WrapperName "command-watcher" -PowerShellArguments "-NoProfile test"
        $action.Execute | Should Be "wscript.exe"
        (Test-Path -LiteralPath (Join-Path $global:admProductionShapedDir "command-watcher.vbs")) | Should Be $true
    }
}
