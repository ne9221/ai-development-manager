# Static/behavioral proof for the hidden Windows Scheduled Task
# trigger/wrapper around the (not-yet-implemented) interface contract
#     python -m manager.drive_dispatch_watcher --once
#
# This suite never registers a real Scheduled Task: New-ScheduledTaskAction,
# New-ScheduledTaskTrigger, New-ScheduledTaskSettingsSet,
# New-ScheduledTaskPrincipal and Register-ScheduledTask are all mocked in
# install_drive_dispatch_ingress.ps1's Describe block, and
# run_drive_dispatch_ingress.ps1 is only ever invoked against a fake
# "python" stub batch file under $TestDrive, never a real interpreter or
# provider binary.

# Isolation contract (see fix/pester-scheduled-task-isolation-20260823):
# this suite must be incapable of touching the real, already-registered
# "AI Development Manager - Drive Dispatch Ingress" / "... - Command
# Watcher" Scheduled Tasks or their live generated .vbs wrapper files, even
# when run against a real production checkout (where $repository below
# resolves to that checkout, not a throwaway clone).
#
# Four independent layers enforce that:
#   1. Every install invocation below uses a unique test-only -TaskName,
#      never one of the two canonical production names.
#   2. Every install invocation passes an isolated -GeneratedWrapperDir
#      under $TestDrive, so the generated .vbs wrapper is never written to
#      the real checkout's manager\generated\ (see AdmHiddenLaunch.ps1).
#   3. $env:ADM_PESTER_TEST_ACTIVE is set for the lifetime of this file's
#      test execution: install_drive_dispatch_ingress.ps1 itself refuses
#      (exit 1, before any provenance activation or file write) to run
#      with a production -TaskName while that sentinel is set.
#   4. New-AdmHiddenScheduledTaskAction itself (the shared helper both this
#      installer AND install_command_watcher.ps1 route their wrapper write
#      through -- see AdmHiddenLaunch.ps1 and AdmHiddenLaunch.Tests.ps1)
#      refuses, before any file write, to run under that same sentinel
#      without an explicit -GeneratedWrapperDir that is neither equal to
#      nor a descendant of $RepositoryPath\manager\generated. This is the
#      system-level backstop: it protects every installer that calls the
#      shared helper, not just this one, so even a future regression in
#      this file (or in a Command Watcher test suite) that forgets (1)/(2)
#      fails closed inside the shared helper, not silently.
# On top of that, Register-ScheduledTask/Get-ScheduledTask/
# Unregister-ScheduledTask/Start-ScheduledTask/Stop-ScheduledTask are all
# mocked below to either capture (Register-ScheduledTask only) or throw
# (the other four, which neither installer script ever legitimately calls)
# -- and a read-only snapshot (task Action identity AND the SHA-256 of the
# .vbs wrapper file that Action points at) of the two production task names
# is taken before and after this file's Describe blocks run, outside any
# Mock scope, and compared with Assert-AdmProductionSnapshotUnchanged (see
# AdmProductionSnapshotGuard.ps1 / AdmProductionSnapshotGuard.Tests.ps1),
# which THROWS -- a genuine terminating error, unlike a bare Write-Error --
# on any mismatch, so a real mutation makes this Pester invocation
# definitively fail rather than merely print a message.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here "AdmProductionSnapshotGuard.ps1")
$repository = (Resolve-Path (Join-Path $here "..")).Path
$installScript = Join-Path $here "install_drive_dispatch_ingress.ps1"
$runScript = Join-Path $here "run_drive_dispatch_ingress.ps1"
$ingressTaskName = "AI Development Manager - Drive Dispatch Ingress [PESTER-TEST-ISOLATED]"
$watcherTaskName = "AI Development Manager - Command Watcher"
$admProductionTaskNames = @(
    "AI Development Manager - Drive Dispatch Ingress",
    "AI Development Manager - Command Watcher"
)

function Get-AdmProductionTaskSnapshot {
    # Deliberately does NOT capture .State: these are live, running
    # production tasks polling on their own 1-minute triggers, so State
    # legitimately flips between Ready/Running/Queued from one snapshot to
    # the next with zero relation to this test file -- comparing it produced
    # a false positive during earlier verification of this suite and has
    # been removed.
    #
    # What this DOES capture, and what the before/after assertion below
    # requires unchanged, is everything the historical contamination
    # incident actually touched: the registered task's Action (Execute +
    # Arguments -- unchanged even in that incident, since Register-
    # ScheduledTask was mocked and never re-registered) AND, separately,
    # the SHA-256 of the .vbs wrapper file that Action's Arguments point at
    # on disk. That file's *content* -- not its path or the task
    # registration -- is what a contaminated Pester run silently rewrote:
    # the task kept pointing at the same real .vbs path the whole time,
    # while New-AdmHiddenScheduledTaskAction (called with the real
    # checkout's own path as -RepositoryPath) regenerated that exact file
    # with test values. A snapshot that only compared Actions (as an
    # earlier revision of this function did) cannot detect that -- the
    # Arguments string is unchanged because it's still the same path; only
    # the bytes at that path differ. Hashing the wrapper file closes that
    # gap.
    $admProductionTaskNames | ForEach-Object {
        $task = $null
        try { $task = Get-ScheduledTask -TaskName $_ -ErrorAction Stop } catch { $task = $null }
        if ($task) {
            $execute = $task.Actions[0].Execute
            $arguments = $task.Actions[0].Arguments
            $vbsPath = $null
            if ($arguments -match '"([^"]+\.vbs)"') { $vbsPath = $Matches[1] }
            $vbsExists = $false
            $vbsHash = $null
            if ($vbsPath -and (Test-Path -LiteralPath $vbsPath -PathType Leaf)) {
                $vbsExists = $true
                $vbsHash = (Get-FileHash -LiteralPath $vbsPath -Algorithm SHA256).Hash
            }
            [PSCustomObject]@{
                TaskName  = $_
                Exists    = $true
                Execute   = $execute
                Arguments = $arguments
                VbsPath   = $vbsPath
                VbsExists = $vbsExists
                VbsHash   = $vbsHash
            }
        } else {
            [PSCustomObject]@{
                TaskName = $_; Exists = $false; Execute = $null; Arguments = $null
                VbsPath = $null; VbsExists = $false; VbsHash = $null
            }
        }
    }
}

# Read-only, unmocked, taken before this file's Describe blocks (and their
# It-scoped Mocks) exist at all.
$admBeforeSnapshot = @(Get-AdmProductionTaskSnapshot)
$env:ADM_PESTER_TEST_ACTIVE = "1"

function New-FakePython([string]$Dir, [bool]$ProvenanceFails = $false, [string]$LogFile) {
    # A real, invokable external command (not a PowerShell Mock) standing
    # in for python.exe -- run_drive_dispatch_ingress.ps1 and
    # install_drive_dispatch_ingress.ps1 both shell out to $PythonPath
    # directly via `&`, which Pester's function-level Mock cannot intercept.
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $stub = Join-Path $Dir "fakepython.cmd"
    $verifyRunningBody = if ($ProvenanceFails) {
        @("exit /b 1")
    } else {
        @(
            "echo {`"running_sha`":`"deadbeef`",`"tested_sha`":`"deadbeef`",`"activated_sha`":`"deadbeef`"}",
            "exit /b 0"
        )
    }
    $lines = @(
        "@echo off",
        "if not `"%FAKE_PYTHON_LOG%`"==`"`" echo %* >> `"%FAKE_PYTHON_LOG%`"",
        "echo %*| findstr /C:`"provenance activate`" >nul",
        "if %ERRORLEVEL%==0 exit /b 0",
        "echo %*| findstr /C:`"provenance verify-running`" >nul",
        "if %ERRORLEVEL%==0 ("
    ) + ($verifyRunningBody | ForEach-Object { "  $_" }) + @(
        ")",
        "echo %*| findstr /C:`"manager.drive_dispatch_watcher`" >nul",
        "if %ERRORLEVEL%==0 (",
        # Logged unconditionally on the actual ingress-poll invocation, not
        # just its args, so a test can prove the specific env vars this
        # script is required to carry (ingress folder/owner, the canonical
        # ADM_LOCK_GCS_BUCKET idempotency bucket, WorkspaceRoot) actually
        # reached the child process environment rather than merely existing
        # in the caller's $env:.
        "  echo ENV FOLDER=%ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID% OWNER=%ADM_DRIVE_DISPATCH_INGRESS_OWNER% BUCKET=%ADM_LOCK_GCS_BUCKET% WORKSPACE=%ADM_WORKSPACE_ROOT% CLAUDEACCOUNTS=%CLAUDE_ACCOUNTS_CONFIG% >> `"%FAKE_PYTHON_LOG%`"",
        "  exit /b 0",
        ")",
        "exit /b 1"
    )
    Set-Content -LiteralPath $stub -Value $lines -Encoding ASCII
    return $stub
}

# Pester's own $TestDrive is itself always created under the OS temp
# directory -- so it cannot be used for a *valid* WorkspaceRoot in these
# tests without tripping the very "not under Temp" guard being tested.
# Real WorkspaceRoot values are created under $env:USERPROFILE instead
# (outside Temp, like a real checkout would be) and removed in AfterEach;
# nothing here is a production path and no Scheduled Task is ever touched.
$outsideTempRoot = Join-Path $env:USERPROFILE ".adm-pester-drive-ingress-test"

Describe "install_drive_dispatch_ingress.ps1 -- Scheduled Task shape" {
    BeforeEach {
        $global:admCase = Join-Path $TestDrive ([Guid]::NewGuid().ToString("N"))
        $global:admOutsideCase = Join-Path $outsideTempRoot ([Guid]::NewGuid().ToString("N"))
        $global:admWorkspaceRoot = Join-Path $global:admOutsideCase "workspace-root"
        New-Item -ItemType Directory -Force -Path $global:admWorkspaceRoot | Out-Null
        $global:admManagerHome = Join-Path $global:admCase "manager-home"
        New-Item -ItemType Directory -Force -Path $global:admManagerHome | Out-Null
        $global:admFakePython = New-FakePython -Dir (Join-Path $global:admCase "python")
        # Isolated wrapper output -- never the real checkout's
        # manager\generated\ -- see the file-header note above.
        $global:admGeneratedWrapperDir = Join-Path $global:admCase "generated-wrapper"

        $global:admCapturedAction = $null
        $global:admCapturedTrigger = $null
        $global:admCapturedSettings = $null
        $global:admCapturedTaskName = $null

        # Only Register-ScheduledTask -- the one call that actually
        # mutates Task Scheduler -- is mocked. New-ScheduledTaskAction /
        # -Trigger / -SettingsSet / -Principal are left real: they only
        # construct in-memory CIM objects (no registration, no disk/
        # registry mutation), and Pester's Mock proxy for
        # Register-ScheduledTask preserves that cmdlet's real
        # [CimInstance[]]-typed -Action parameter -- a hand-built
        # PSCustomObject standing in for a real action CIM instance fails
        # that type binding even inside the mock, so the real constructors
        # must be used to produce genuinely typed objects to capture.
        Mock Register-ScheduledTask {
            param($TaskName, $Action, $Trigger, $Settings, $Principal, $Force)
            $global:admCapturedTaskName = $TaskName
            $global:admCapturedAction = $Action
            $global:admCapturedTrigger = $Trigger
            $global:admCapturedSettings = $Settings
        }
        # Neither installer script legitimately calls any of these four --
        # confirmed by reading install_drive_dispatch_ingress.ps1 and
        # install_command_watcher.ps1 (Register-ScheduledTask is the only
        # Task Scheduler cmdlet either one invokes). Mocking them to throw
        # turns any future accidental call (a regression that adds a
        # reinstall-by-unregister-then-register path, for example) into an
        # immediate test failure instead of a real mutation.
        Mock Get-ScheduledTask { throw "ADM_TEST_SAFETY: Get-ScheduledTask must not be called from install_drive_dispatch_ingress.ps1 under test." }
        Mock Unregister-ScheduledTask { throw "ADM_TEST_SAFETY: Unregister-ScheduledTask must not be called from install_drive_dispatch_ingress.ps1 under test." }
        Mock Start-ScheduledTask { throw "ADM_TEST_SAFETY: Start-ScheduledTask must not be called from install_drive_dispatch_ingress.ps1 under test." }
        Mock Stop-ScheduledTask { throw "ADM_TEST_SAFETY: Stop-ScheduledTask must not be called from install_drive_dispatch_ingress.ps1 under test." }
    }

    AfterEach {
        if (Test-Path -LiteralPath $global:admOutsideCase) {
            Remove-Item -LiteralPath $global:admOutsideCase -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    function Invoke-Install {
        & $installScript `
            -TaskName $ingressTaskName `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -PythonDeps "deps" `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot $global:admWorkspaceRoot `
            -GeneratedWrapperDir $global:admGeneratedWrapperDir
    }

    It "invokes the exact runner interface via a hidden wscript wrapper, not a visible PowerShell host" {
        Invoke-Install | Out-Null
        $global:admCapturedAction.Execute | Should Be "wscript.exe"
        $vbsPath = $global:admCapturedAction.Arguments.Trim('"')
        (Test-Path -LiteralPath $vbsPath) | Should Be $true
        $vbsContent = Get-Content -Raw -LiteralPath $vbsPath
        $vbsContent | Should Match ([regex]::Escape('shell.Run("powershell.exe'))
        $vbsContent | Should Match ([regex]::Escape(', 0, True'))
        $vbsContent | Should Match ([regex]::Escape('run_drive_dispatch_ingress.ps1'))
        $vbsContent | Should Match ([regex]::Escape('-WorkspaceRoot'))
        $vbsContent | Should Not Match "manager\.command_watcher"
    }

    It "registers the exact expected task identity" {
        Invoke-Install | Out-Null
        $global:admCapturedTaskName | Should Be $ingressTaskName
    }

    It "carries the explicit WorkspaceRoot into the wrapped command line" {
        Invoke-Install | Out-Null
        $vbsPath = $global:admCapturedAction.Arguments.Trim('"')
        $vbsContent = Get-Content -Raw -LiteralPath $vbsPath
        $vbsContent | Should Match ([regex]::Escape("-WorkspaceRoot `"`"$global:admWorkspaceRoot`"`""))
    }

    It "carries required ingress folder/owner and the canonical GCS bucket config into the wrapped command line" {
        Invoke-Install | Out-Null
        $vbsPath = $global:admCapturedAction.Arguments.Trim('"')
        $vbsContent = Get-Content -Raw -LiteralPath $vbsPath
        $vbsContent | Should Match ([regex]::Escape('-IngressFolderId'))
        $vbsContent | Should Match ([regex]::Escape('folder-id'))
        $vbsContent | Should Match ([regex]::Escape('-IngressOwner'))
        $vbsContent | Should Match ([regex]::Escape('owner@example.com'))
        $vbsContent | Should Match ([regex]::Escape('-GcsBucket'))
        $vbsContent | Should Match ([regex]::Escape('idem-bucket'))
        # No separate static idempotency-object parameter: dispatch request
        # idempotency object names are generated dynamically by
        # manager.dispatch_requests.dispatch_request_registry(), not a
        # static per-install value.
        $vbsContent | Should Not Match ([regex]::Escape('-IdempotencyObject'))
    }

    It "propagates a default-resolved ClaudeAccountsConfig into the wrapped command line when omitted" {
        Invoke-Install | Out-Null
        $vbsPath = $global:admCapturedAction.Arguments.Trim('"')
        $vbsContent = Get-Content -Raw -LiteralPath $vbsPath
        $expectedDefault = Join-Path $global:admManagerHome "config\claude_accounts.json"
        $vbsContent | Should Match ([regex]::Escape('-ClaudeAccountsConfig'))
        $vbsContent | Should Match ([regex]::Escape($expectedDefault))
    }

    It "preserves an explicit ClaudeAccountsConfig verbatim through the installer into the wrapped command line" {
        $explicitConfig = Join-Path $global:admCase "explicit-claude-accounts.json"
        & $installScript `
            -TaskName $ingressTaskName `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -PythonDeps "deps" `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -ClaudeAccountsConfig $explicitConfig `
            -WorkspaceRoot $global:admWorkspaceRoot `
            -GeneratedWrapperDir $global:admGeneratedWrapperDir | Out-Null
        $vbsPath = $global:admCapturedAction.Arguments.Trim('"')
        $vbsContent = Get-Content -Raw -LiteralPath $vbsPath
        $vbsContent | Should Match ([regex]::Escape("-ClaudeAccountsConfig `"`"$explicitConfig`"`""))
        $defaultConfig = Join-Path $global:admManagerHome "config\claude_accounts.json"
        $vbsContent | Should Not Match ([regex]::Escape($defaultConfig))
    }

    It "runs every 1 minute with no overlapping instances" {
        Invoke-Install | Out-Null
        # Real MSFT_TaskRepetitionPattern CIM instance -- Interval is an
        # ISO-8601 duration string ("PT1M" == 1 minute), not a TimeSpan.
        $global:admCapturedTrigger.Repetition.Interval | Should Be "PT1M"
        $global:admCapturedSettings.MultipleInstances | Should Be "IgnoreNew"
    }

    It "is hidden and bounded to the default 5-minute execution timeout" {
        Invoke-Install | Out-Null
        $global:admCapturedSettings.Hidden | Should Be $true
        $global:admCapturedSettings.ExecutionTimeLimit | Should Be "PT5M"
    }

    It "honors a caller-supplied bounded execution timeout" {
        & $installScript `
            -TaskName $ingressTaskName `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot $global:admWorkspaceRoot `
            -GeneratedWrapperDir $global:admGeneratedWrapperDir `
            -ExecutionTimeLimitMinutes 2 | Out-Null
        $global:admCapturedSettings.ExecutionTimeLimit | Should Be "PT2M"
    }

    foreach ($case in @(
        @{ Name = "relative"; Value = "relative\path" },
        @{ Name = "nonexistent"; Value = "C:\this\path\does\not\exist\adm-ingress" }
    )) {
        It "fails closed and never registers a Scheduled Task when WorkspaceRoot is $($case.Name)" {
            & $installScript `
                -TaskName $ingressTaskName `
                -PythonPath $global:admFakePython `
                -RepositoryPath $repository `
                -ManagerHome $global:admManagerHome `
                -IngressFolderId "folder-id" `
                -IngressOwner "owner@example.com" `
                -GcsBucket "idem-bucket" `
                -WorkspaceRoot $case.Value `
                -GeneratedWrapperDir $global:admGeneratedWrapperDir 2>$null
            $LASTEXITCODE | Should Not Be 0
            Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
        }
    }

    It "fails closed and never registers a Scheduled Task when WorkspaceRoot is missing (empty)" {
        # An empty string for a Mandatory string parameter is rejected by
        # PowerShell's own parameter binding before the script body (and
        # its own WORKSPACE_ROOT_REQUIRED check) ever runs -- still a
        # closed failure with zero Scheduled Task mutation, just raised one
        # layer earlier than the other invalid-WorkspaceRoot shapes.
        $threw = $false
        try {
            & $installScript `
                -TaskName $ingressTaskName `
                -PythonPath $global:admFakePython `
                -RepositoryPath $repository `
                -ManagerHome $global:admManagerHome `
                -IngressFolderId "folder-id" `
                -IngressOwner "owner@example.com" `
                -GcsBucket "idem-bucket" `
                -WorkspaceRoot "" `
                -GeneratedWrapperDir $global:admGeneratedWrapperDir 2>$null
        } catch {
            $threw = $true
        }
        $threw | Should Be $true
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
    }

    It "fails closed and never registers a Scheduled Task when WorkspaceRoot resolves under Temp" {
        & $installScript `
            -TaskName $ingressTaskName `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot ([IO.Path]::GetTempPath()) `
            -GeneratedWrapperDir $global:admGeneratedWrapperDir 2>$null
        $LASTEXITCODE | Should Not Be 0
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
    }

    It "leaves the existing Command Watcher Scheduled Task installer functionally untouched (comment-provenance fix excepted)" {
        # install_command_watcher.ps1 legitimately differs from the
        # cb3870e base by more than this merge: the embedded-ingress
        # decoupling commit (0faee84, already part of this branch's history
        # by the time this Describe block runs) added the -EmbeddedIngress
        # parameter and its surrounding comment block. This merge's only
        # *additional* intended change on top of that is the documentation-
        # correction hardening (spec item B): a stale comment misattributed
        # the dedicated Drive Dispatch Ingress Scheduled Task to 6a7f0df
        # (the standalone Python runner commit) instead of 6d62cea (the
        # actual Windows dedicated trigger commit). Compare against this
        # branch's own pre-hardening HEAD revision (with that one
        # substitution applied) rather than the cb3870e base, so this test
        # still catches any *other*, unintended drift in this other lane's
        # installer without re-litigating 0faee84's already-merged content.
        # Pinned to that commit's exact SHA (not HEAD) so this assertion
        # stays meaningful once the hardening-B fix itself is committed on
        # top and HEAD moves past it.
        $preHardening = & git -C $repository show 0faee84962e40192aecb252af273552755d5f90f:manager/install_command_watcher.ps1
        $expectedText = (($preHardening -join "`n") -replace "`r`n", "`n") -replace "6a7f0df", "6d62cea"
        $current = Get-Content -Raw -LiteralPath (Join-Path $repository "manager\install_command_watcher.ps1")
        ($current -replace "`r`n", "`n").Trim() | Should Be $expectedText.Trim()
    }
}

Describe "run_drive_dispatch_ingress.ps1 -- runtime behavior" {
    BeforeEach {
        $global:admCase = Join-Path $TestDrive ([Guid]::NewGuid().ToString("N"))
        $global:admOutsideCase = Join-Path $outsideTempRoot ([Guid]::NewGuid().ToString("N"))
        $global:admWorkspaceRoot = Join-Path $global:admOutsideCase "workspace-root"
        New-Item -ItemType Directory -Force -Path $global:admWorkspaceRoot | Out-Null
        $global:admManagerHome = Join-Path $global:admCase "manager-home"
        New-Item -ItemType Directory -Force -Path $global:admManagerHome | Out-Null
        # Default-resolution fixture: run_drive_dispatch_ingress.ps1 now
        # fails closed if ClaudeAccountsConfig (explicit or default-resolved
        # to $ManagerHome\config\claude_accounts.json) does not exist on
        # disk. Tests below that omit -ClaudeAccountsConfig rely on this
        # default file being present so they keep exercising the rest of
        # the runtime contract (folder/owner/bucket/workspace env vars,
        # provenance gate, exact python invocation) unaffected by this
        # change; the dedicated "missing config" tests below explicitly
        # remove or redirect it instead.
        $global:admClaudeAccountsConfig = Join-Path $global:admManagerHome "config\claude_accounts.json"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $global:admClaudeAccountsConfig) | Out-Null
        Set-Content -LiteralPath $global:admClaudeAccountsConfig -Value '{"accounts": [{"account_id": "account-a", "enabled": true, "config_dir": null}]}' -Encoding utf8
        $global:admLog = Join-Path $global:admCase "fakepython.log"
        $global:admFakePython = New-FakePython -Dir (Join-Path $global:admCase "python")
        $global:admFakePythonFailing = New-FakePython -Dir (Join-Path $global:admCase "python-fail") -ProvenanceFails $true
        $env:FAKE_PYTHON_LOG = $global:admLog

        # run_drive_dispatch_ingress.ps1 never calls any Task Scheduler
        # cmdlet (confirmed by reading the script -- it only shells out to
        # $PythonPath). Mocked to throw anyway as a defense-in-depth
        # tripwire against a future regression, matching the install-script
        # Describe block above.
        Mock Get-ScheduledTask { throw "ADM_TEST_SAFETY: Get-ScheduledTask must not be called from run_drive_dispatch_ingress.ps1 under test." }
        Mock Register-ScheduledTask { throw "ADM_TEST_SAFETY: Register-ScheduledTask must not be called from run_drive_dispatch_ingress.ps1 under test." }
        Mock Unregister-ScheduledTask { throw "ADM_TEST_SAFETY: Unregister-ScheduledTask must not be called from run_drive_dispatch_ingress.ps1 under test." }
        Mock Start-ScheduledTask { throw "ADM_TEST_SAFETY: Start-ScheduledTask must not be called from run_drive_dispatch_ingress.ps1 under test." }
        Mock Stop-ScheduledTask { throw "ADM_TEST_SAFETY: Stop-ScheduledTask must not be called from run_drive_dispatch_ingress.ps1 under test." }
    }

    AfterEach {
        Remove-Item Env:FAKE_PYTHON_LOG -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_DRIVE_DISPATCH_INGRESS_OWNER -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_LOCK_GCS_BUCKET -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue
        Remove-Item Env:CLAUDE_ACCOUNTS_CONFIG -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $global:admOutsideCase) {
            Remove-Item -LiteralPath $global:admOutsideCase -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    function Invoke-Run([string]$PythonPath) {
        & $runScript `
            -PythonPath $PythonPath `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot $global:admWorkspaceRoot
    }

    It "invokes exactly the drive_dispatch_watcher --once interface, never a provider launcher or command_watcher" {
        Invoke-Run -PythonPath $global:admFakePython | Out-Null
        $LASTEXITCODE | Should Be 0
        $logLines = Get-Content -LiteralPath $global:admLog
        # This script only ever shells out to the fake python stub twice
        # (provenance verify-running, then the ingress poll itself); the
        # ingress-poll call also logs an ENV diagnostic line (see
        # New-FakePython). Every logged args-line must be one of exactly
        # the two expected `-m manager.*` module invocations -- never a
        # provider CLI (claude.exe/codex.exe/gemini.exe) and never
        # manager.command_watcher, which stays the existing Command
        # Watcher's job alone.
        $argLines = $logLines | Where-Object { $_ -notlike "ENV *" }
        $argLines.Count | Should Be 2
        foreach ($line in $argLines) {
            $line | Should Match '^-m manager\.(provenance verify-running|drive_dispatch_watcher --once)\b'
        }
        ($argLines -join "`n") | Should Match ([regex]::Escape('-m manager.drive_dispatch_watcher --once'))
    }

    It "carries required ingress folder/owner env vars and the canonical ADM_LOCK_GCS_BUCKET" {
        # ADM_* env vars are asserted from inside the fake python stub's own
        # process (via cmd.exe %ADM_...% expansion into the ENV log line),
        # since the values must survive into the child process environment,
        # not merely exist in this test's own $env: scope. ADM_LOCK_GCS_BUCKET
        # is the exact name manager.gcs_lock_registry.BUCKET_ENV expects
        # (see the cross-lane contract Describe block below).
        Invoke-Run -PythonPath $global:admFakePython | Out-Null
        $log = Get-Content -Raw -LiteralPath $global:admLog
        $log | Should Match "FOLDER=folder-id"
        $log | Should Match "OWNER=owner@example.com"
        $log | Should Match "BUCKET=idem-bucket"
        $log | Should Match ([regex]::Escape("WORKSPACE=$global:admWorkspaceRoot"))
    }

    It "exports CLAUDE_ACCOUNTS_CONFIG into the ingress poller's process environment (default-resolved path)" {
        # No -ClaudeAccountsConfig supplied: must default-resolve to
        # $ManagerHome\config\claude_accounts.json (the same fixture file
        # BeforeEach created) and export it under the exact env var name
        # cloud.dispatch_ingress._claude_account_registry() reads.
        Invoke-Run -PythonPath $global:admFakePython | Out-Null
        $LASTEXITCODE | Should Be 0
        $log = Get-Content -Raw -LiteralPath $global:admLog
        $log | Should Match ([regex]::Escape("CLAUDEACCOUNTS=$global:admClaudeAccountsConfig"))
    }

    It "exports an explicitly-supplied CLAUDE_ACCOUNTS_CONFIG verbatim, not the default path" {
        $explicitConfig = Join-Path $global:admCase "explicit-claude-accounts.json"
        Set-Content -LiteralPath $explicitConfig -Value '{"accounts": []}' -Encoding utf8
        & $runScript `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -ClaudeAccountsConfig $explicitConfig `
            -WorkspaceRoot $global:admWorkspaceRoot | Out-Null
        $LASTEXITCODE | Should Be 0
        $log = Get-Content -Raw -LiteralPath $global:admLog
        $log | Should Match ([regex]::Escape("CLAUDEACCOUNTS=$explicitConfig"))
        $log | Should Not Match ([regex]::Escape("CLAUDEACCOUNTS=$global:admClaudeAccountsConfig"))
    }

    It "fails closed before touching python when CLAUDE_ACCOUNTS_CONFIG (default-resolved) does not exist on disk" {
        # Remove the fixture BeforeEach created, so default resolution
        # points at a path that genuinely does not exist.
        Remove-Item -LiteralPath $global:admClaudeAccountsConfig -Force
        Invoke-Run -PythonPath $global:admFakePython 2>$null
        $LASTEXITCODE | Should Not Be 0
        (Test-Path -LiteralPath $global:admLog) | Should Be $false
    }

    It "fails closed before touching python when an explicit CLAUDE_ACCOUNTS_CONFIG path does not exist on disk" {
        $missingConfig = Join-Path $global:admCase "does-not-exist-claude-accounts.json"
        & $runScript `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -ClaudeAccountsConfig $missingConfig `
            -WorkspaceRoot $global:admWorkspaceRoot 2>$null
        $LASTEXITCODE | Should Not Be 0
        (Test-Path -LiteralPath $global:admLog) | Should Be $false
    }

    It "fails closed on a provenance mismatch and never reaches the ingress poller" {
        Invoke-Run -PythonPath $global:admFakePythonFailing 2>$null
        $LASTEXITCODE | Should Not Be 0
        $log = if (Test-Path -LiteralPath $global:admLog) { Get-Content -Raw -LiteralPath $global:admLog } else { "" }
        $log | Should Not Match "drive_dispatch_watcher"
    }

    foreach ($case in @(
        @{ Name = "relative"; Value = "relative\path" },
        @{ Name = "nonexistent"; Value = "C:\this\path\does\not\exist\adm-ingress" }
    )) {
        It "fails closed before touching python when WorkspaceRoot is $($case.Name)" {
            & $runScript `
                -PythonPath $global:admFakePython `
                -RepositoryPath $repository `
                -ManagerHome $global:admManagerHome `
                -IngressFolderId "folder-id" `
                -IngressOwner "owner@example.com" `
                -GcsBucket "idem-bucket" `
                -WorkspaceRoot $case.Value 2>$null
            $LASTEXITCODE | Should Not Be 0
            (Test-Path -LiteralPath $global:admLog) | Should Be $false
        }
    }

    It "fails closed before touching python when WorkspaceRoot is missing (empty)" {
        # Same PowerShell mandatory-parameter binding behavior as the
        # install script's empty-WorkspaceRoot case above: rejected before
        # this script's own body (and the fake python stub) ever runs.
        $threw = $false
        try {
            & $runScript `
                -PythonPath $global:admFakePython `
                -RepositoryPath $repository `
                -ManagerHome $global:admManagerHome `
                -IngressFolderId "folder-id" `
                -IngressOwner "owner@example.com" `
                -GcsBucket "idem-bucket" `
                -WorkspaceRoot "" 2>$null
        } catch {
            $threw = $true
        }
        $threw | Should Be $true
        (Test-Path -LiteralPath $global:admLog) | Should Be $false
    }

    It "fails closed before touching python when WorkspaceRoot resolves under Temp" {
        & $runScript `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot ([IO.Path]::GetTempPath()) 2>$null
        $LASTEXITCODE | Should Not Be 0
        (Test-Path -LiteralPath $global:admLog) | Should Be $false
    }
}

Describe "cross-lane contract -- ADM_LOCK_GCS_BUCKET resolves in the frozen Python runner" {
    # This is the one Describe block in this suite that touches a real
    # python.exe rather than the fake stub -- it proves the *other* half
    # of the contract the fake-stub tests above can't: not just that this
    # Windows wrapper exports ADM_LOCK_GCS_BUCKET, but that the actual
    # frozen manager.drive_dispatch_watcher.run_once() (from
    # fix/home-drive-auto-ingress-runner-20260823 @
    # 6a7f0df28f27f2f77edcc5ce224353174197d7ee, a separate lane's branch,
    # never merged into this one) reads that exact env var name via
    # manager.gcs_lock_registry.BUCKET_ENV and resolves it correctly.
    #
    # It never calls the real `--once` CLI entry point (that would build a
    # real Drive service and hit real GCS/Drive) -- instead it calls the
    # frozen module's own run_once(build_service_fn=..., store_factory=...,
    # poll=...) with fakes injected for exactly those three parameters,
    # the same dependency-injection seam the frozen lane's own
    # test_drive_dispatch_watcher.py test suite uses. No real Drive/GCS
    # call is made in either direction.
    $frozenRunnerSha = "6a7f0df28f27f2f77edcc5ce224353174197d7ee"
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) { $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue }

    BeforeEach {
        $global:admCrossLaneCase = Join-Path $TestDrive ([Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $global:admCrossLaneCase | Out-Null
        $global:admFrozenWatcherPath = Join-Path $global:admCrossLaneCase "drive_dispatch_watcher.py"
        # Extracted fresh from the frozen lane's commit on every run -- never
        # written into this repo/branch's own working tree or committed.
        & git -C $repository show "${frozenRunnerSha}:manager/drive_dispatch_watcher.py" | Out-File -LiteralPath $global:admFrozenWatcherPath -Encoding utf8
        $global:admHarnessPath = Join-Path $global:admCrossLaneCase "cross_lane_harness.py"
        $harness = @'
import importlib.util
import json
import sys

repo_root, frozen_watcher_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo_root)

spec = importlib.util.spec_from_file_location("manager.drive_dispatch_watcher", frozen_watcher_path)
mod = importlib.util.module_from_spec(spec)
sys.modules["manager.drive_dispatch_watcher"] = mod
spec.loader.exec_module(mod)

captured = {}


def fake_build_service():
    captured["build_service_called"] = True
    return "FAKE_SERVICE"


def fake_store_factory(service):
    return "FAKE_STORE"


def fake_poll(store, service, bucket):
    captured["bucket"] = bucket
    return {"polled": True}


try:
    mod.run_once(build_service_fn=fake_build_service, store_factory=fake_store_factory, poll=fake_poll)
    print(json.dumps({"outcome": "resolved", "bucket_env_name": mod.BUCKET_ENV, "bucket": captured.get("bucket")}))
except mod.TaskError as exc:
    print(json.dumps({"outcome": "fail_closed", "bucket_env_name": mod.BUCKET_ENV, "error": str(exc)}))
'@
        Set-Content -LiteralPath $global:admHarnessPath -Value $harness -Encoding utf8
    }

    It "resolves ADM_LOCK_GCS_BUCKET when the Windows wrapper's env var is set" {
        if (-not $pythonCmd) { Set-TestInconclusive "no python.exe/python3.exe found on PATH in this environment" }
        # Mirrors exactly what run_drive_dispatch_ingress.ps1 line
        # `$env:ADM_LOCK_GCS_BUCKET = $GcsBucket` does before invoking
        # `python -m manager.drive_dispatch_watcher --once`.
        $env:ADM_LOCK_GCS_BUCKET = "bucket-from-windows-wrapper"
        try {
            $output = & $pythonCmd.Source $global:admHarnessPath $repository $global:admFrozenWatcherPath
        } finally {
            Remove-Item Env:ADM_LOCK_GCS_BUCKET -ErrorAction SilentlyContinue
        }
        $LASTEXITCODE | Should Be 0
        $result = $output | ConvertFrom-Json
        $result.bucket_env_name | Should Be "ADM_LOCK_GCS_BUCKET"
        $result.outcome | Should Be "resolved"
        $result.bucket | Should Be "bucket-from-windows-wrapper"
    }

    It "fails closed in the frozen runner when ADM_LOCK_GCS_BUCKET is absent, proving no silent env mismatch" {
        if (-not $pythonCmd) { Set-TestInconclusive "no python.exe/python3.exe found on PATH in this environment" }
        Remove-Item Env:ADM_LOCK_GCS_BUCKET -ErrorAction SilentlyContinue
        $output = & $pythonCmd.Source $global:admHarnessPath $repository $global:admFrozenWatcherPath
        $LASTEXITCODE | Should Be 0
        $result = $output | ConvertFrom-Json
        $result.bucket_env_name | Should Be "ADM_LOCK_GCS_BUCKET"
        $result.outcome | Should Be "fail_closed"
        $result.error | Should Match "ADM_LOCK_GCS_BUCKET"
    }
}

# Clear the sentinel now that every Describe block above has run, then take
# the read-only "after" snapshot -- unmocked (all Mocks created inside a
# Describe block are scoped to that block and are gone by the time control
# reaches back here) and outside any It, exactly mirroring $admBeforeSnapshot
# at the top of this file. Assert-AdmProductionSnapshotUnchanged THROWS on
# any mismatch (task existence, Action Execute/Arguments, the resolved .vbs
# path, or its SHA-256 -- see AdmProductionSnapshotGuard.ps1), which is a
# terminating error independent of $ErrorActionPreference: a real mutation
# here makes this Pester file's execution itself fail, not just print a
# message that could be missed.
Remove-Item Env:ADM_PESTER_TEST_ACTIVE -ErrorAction SilentlyContinue
$admAfterSnapshot = @(Get-AdmProductionTaskSnapshot)
Assert-AdmProductionSnapshotUnchanged -Before $admBeforeSnapshot -After $admAfterSnapshot
