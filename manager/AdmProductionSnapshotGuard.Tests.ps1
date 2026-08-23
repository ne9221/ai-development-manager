# Regression coverage for Assert-AdmProductionSnapshotUnchanged
# (manager\AdmProductionSnapshotGuard.ps1) -- the terminating-failure
# comparison DriveDispatchIngress.Tests.ps1 runs against a real, read-only
# Get-ScheduledTask snapshot before and after its own Describe blocks. This
# suite never touches a real Scheduled Task or a real .vbs wrapper: every
# case below is built entirely from synthetic in-memory objects, proving
# the comparison/throw logic itself is correct without needing (or risking)
# an actual production contamination incident to exercise the failure path.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here "AdmProductionSnapshotGuard.ps1")

function New-AdmSnapshotEntry {
    param(
        [string]$TaskName = "AI Development Manager - Drive Dispatch Ingress",
        [bool]$Exists = $true,
        [string]$Execute = "wscript.exe",
        [string]$Arguments = '"C:\fake-repo\manager\generated\drive-dispatch-ingress.vbs"',
        [string]$VbsPath = "C:\fake-repo\manager\generated\drive-dispatch-ingress.vbs",
        [bool]$VbsExists = $true,
        [string]$VbsHash = "AAAA1111"
    )
    [PSCustomObject]@{
        TaskName = $TaskName; Exists = $Exists; Execute = $Execute; Arguments = $Arguments
        VbsPath = $VbsPath; VbsExists = $VbsExists; VbsHash = $VbsHash
    }
}

Describe "Assert-AdmProductionSnapshotUnchanged" {
    It "1: identical before/after snapshots do not throw" {
        $before = @(New-AdmSnapshotEntry)
        $after = @(New-AdmSnapshotEntry)
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Not Throw
    }

    It "1b: identical snapshots across both real production task names do not throw" {
        $before = @(
            New-AdmSnapshotEntry -TaskName "AI Development Manager - Drive Dispatch Ingress"
            New-AdmSnapshotEntry -TaskName "AI Development Manager - Command Watcher" -Arguments '"C:\fake-repo\manager\generated\command-watcher.vbs"' -VbsPath "C:\fake-repo\manager\generated\command-watcher.vbs" -VbsHash "BBBB2222"
        )
        $after = @(
            New-AdmSnapshotEntry -TaskName "AI Development Manager - Drive Dispatch Ingress"
            New-AdmSnapshotEntry -TaskName "AI Development Manager - Command Watcher" -Arguments '"C:\fake-repo\manager\generated\command-watcher.vbs"' -VbsPath "C:\fake-repo\manager\generated\command-watcher.vbs" -VbsHash "BBBB2222"
        )
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Not Throw
    }

    It "1c: a non-existent task (Exists = false) on both sides, otherwise identical, does not throw" {
        $before = @(New-AdmSnapshotEntry -Exists $false -Execute $null -Arguments $null -VbsPath $null -VbsExists $false -VbsHash $null)
        $after = @(New-AdmSnapshotEntry -Exists $false -Execute $null -Arguments $null -VbsPath $null -VbsExists $false -VbsHash $null)
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Not Throw
    }

    It "2: same Action/path but a different VbsHash MUST throw -- this is the exact original contamination class (task registration/path unchanged, wrapper bytes silently rewritten)" {
        $before = @(New-AdmSnapshotEntry -VbsHash "AAAA1111")
        $after = @(New-AdmSnapshotEntry -VbsHash "CONTAMINATED9999")
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Throw "PRODUCTION_SCHEDULED_TASK_MUTATED"
    }

    It "3: changed Arguments MUST throw" {
        $before = @(New-AdmSnapshotEntry -Arguments '"C:\fake-repo\manager\generated\drive-dispatch-ingress.vbs"')
        $after = @(New-AdmSnapshotEntry -Arguments '"C:\fake-repo\manager\generated\drive-dispatch-ingress-RETARGETED.vbs"' -VbsPath "C:\fake-repo\manager\generated\drive-dispatch-ingress-RETARGETED.vbs")
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Throw "PRODUCTION_SCHEDULED_TASK_MUTATED"
    }

    It "changed Execute MUST throw" {
        $before = @(New-AdmSnapshotEntry -Execute "wscript.exe")
        $after = @(New-AdmSnapshotEntry -Execute "cmd.exe")
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Throw "PRODUCTION_SCHEDULED_TASK_MUTATED"
    }

    It "the task disappearing entirely (Exists true -> false) MUST throw" {
        $before = @(New-AdmSnapshotEntry -Exists $true)
        $after = @(New-AdmSnapshotEntry -Exists $false -Execute $null -Arguments $null -VbsPath $null -VbsExists $false -VbsHash $null)
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Throw "PRODUCTION_SCHEDULED_TASK_MUTATED"
    }

    It "the wrapper file disappearing while the task registration stays the same MUST throw" {
        $before = @(New-AdmSnapshotEntry -VbsExists $true -VbsHash "AAAA1111")
        $after = @(New-AdmSnapshotEntry -VbsExists $false -VbsHash $null)
        { Assert-AdmProductionSnapshotUnchanged -Before $before -After $after } | Should Throw "PRODUCTION_SCHEDULED_TASK_MUTATED"
    }

    It "the failure is a genuine terminating error, not merely a non-terminating Write-Error swallowed by the caller" {
        $before = @(New-AdmSnapshotEntry -VbsHash "AAAA1111")
        $after = @(New-AdmSnapshotEntry -VbsHash "CONTAMINATED9999")
        $threw = $false
        try {
            Assert-AdmProductionSnapshotUnchanged -Before $before -After $after
        } catch {
            $threw = $true
        }
        $threw | Should Be $true
    }
}
