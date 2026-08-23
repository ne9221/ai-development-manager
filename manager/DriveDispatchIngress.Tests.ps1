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

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repository = (Resolve-Path (Join-Path $here "..")).Path
$installScript = Join-Path $here "install_drive_dispatch_ingress.ps1"
$runScript = Join-Path $here "run_drive_dispatch_ingress.ps1"
$ingressTaskName = "AI Development Manager - Drive Dispatch Ingress"
$watcherTaskName = "AI Development Manager - Command Watcher"

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
        "  echo ENV FOLDER=%ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID% OWNER=%ADM_DRIVE_DISPATCH_INGRESS_OWNER% BUCKET=%ADM_LOCK_GCS_BUCKET% WORKSPACE=%ADM_WORKSPACE_ROOT% >> `"%FAKE_PYTHON_LOG%`"",
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
    }

    AfterEach {
        if (Test-Path -LiteralPath $global:admOutsideCase) {
            Remove-Item -LiteralPath $global:admOutsideCase -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    function Invoke-Install {
        & $installScript `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -PythonDeps "deps" `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot $global:admWorkspaceRoot
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
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot $global:admWorkspaceRoot `
            -ExecutionTimeLimitMinutes 2 | Out-Null
        $global:admCapturedSettings.ExecutionTimeLimit | Should Be "PT2M"
    }

    foreach ($case in @(
        @{ Name = "relative"; Value = "relative\path" },
        @{ Name = "nonexistent"; Value = "C:\this\path\does\not\exist\adm-ingress" }
    )) {
        It "fails closed and never registers a Scheduled Task when WorkspaceRoot is $($case.Name)" {
            & $installScript `
                -PythonPath $global:admFakePython `
                -RepositoryPath $repository `
                -ManagerHome $global:admManagerHome `
                -IngressFolderId "folder-id" `
                -IngressOwner "owner@example.com" `
                -GcsBucket "idem-bucket" `
                -WorkspaceRoot $case.Value 2>$null
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
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
    }

    It "fails closed and never registers a Scheduled Task when WorkspaceRoot resolves under Temp" {
        & $installScript `
            -PythonPath $global:admFakePython `
            -RepositoryPath $repository `
            -ManagerHome $global:admManagerHome `
            -IngressFolderId "folder-id" `
            -IngressOwner "owner@example.com" `
            -GcsBucket "idem-bucket" `
            -WorkspaceRoot ([IO.Path]::GetTempPath()) 2>$null
        $LASTEXITCODE | Should Not Be 0
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
    }

    It "leaves the existing Command Watcher Scheduled Task installer untouched" {
        $baseline = & git -C $repository show cb3870ef3e72bed214cdd89086f86e0eb02f4ced:manager/install_command_watcher.ps1
        $current = Get-Content -Raw -LiteralPath (Join-Path $repository "manager\install_command_watcher.ps1")
        ($current -replace "`r`n", "`n").Trim() | Should Be (($baseline -join "`n") -replace "`r`n", "`n").Trim()
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
        $global:admLog = Join-Path $global:admCase "fakepython.log"
        $global:admFakePython = New-FakePython -Dir (Join-Path $global:admCase "python")
        $global:admFakePythonFailing = New-FakePython -Dir (Join-Path $global:admCase "python-fail") -ProvenanceFails $true
        $env:FAKE_PYTHON_LOG = $global:admLog
    }

    AfterEach {
        Remove-Item Env:FAKE_PYTHON_LOG -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_DRIVE_DISPATCH_INGRESS_OWNER -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_LOCK_GCS_BUCKET -ErrorAction SilentlyContinue
        Remove-Item Env:ADM_WORKSPACE_ROOT -ErrorAction SilentlyContinue
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
