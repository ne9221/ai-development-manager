# Pure comparison helper for the production Scheduled Task / generated .vbs
# wrapper safety snapshot taken by DriveDispatchIngress.Tests.ps1 (see
# fix/pester-scheduled-task-isolation-20260823). Extracted into its own
# side-effect-free function -- it only compares the two arrays it's given,
# it never calls Get-ScheduledTask or touches disk itself -- so it can be
# exercised directly against synthetic before/after objects in
# AdmProductionSnapshotGuard.Tests.ps1 without ever needing a real
# Scheduled Task or a real contamination incident to prove the failure path
# actually fails.
#
# Uses `throw` (a terminating error) rather than Write-Error: Write-Error is
# non-terminating by default and would let a mismatch print a message while
# the Pester run otherwise reports success -- exactly the gap that let the
# original contamination go undetected by any automated check. `throw`'s
# failure semantics do not depend on $ErrorActionPreference.
function Assert-AdmProductionSnapshotUnchanged {
    param(
        [Parameter(Mandatory = $true)][object[]]$Before,
        [Parameter(Mandatory = $true)][object[]]$After
    )

    foreach ($beforeEntry in $Before) {
        $afterEntry = $After | Where-Object { $_.TaskName -eq $beforeEntry.TaskName } | Select-Object -First 1
        if (-not $afterEntry) {
            throw "PRODUCTION_SCHEDULED_TASK_MUTATED: '$($beforeEntry.TaskName)' is missing from the 'after' snapshot (expected an entry with the same TaskName)."
        }
        # Intentionally excludes .State: a live production task's State
        # legitimately flips Ready/Running/Queued on its own every tick,
        # unrelated to test execution -- comparing it produced a real false
        # positive during earlier verification of this safety net.
        # VbsHash is the field that actually catches the original
        # incident's contamination class: Action/Arguments/VbsPath stayed
        # identical (Register-ScheduledTask was mocked, never
        # re-registered) while the .vbs file's bytes were silently
        # overwritten at that same, unchanged path.
        if ($beforeEntry.Exists -ne $afterEntry.Exists `
            -or $beforeEntry.Execute -ne $afterEntry.Execute `
            -or $beforeEntry.Arguments -ne $afterEntry.Arguments `
            -or $beforeEntry.VbsPath -ne $afterEntry.VbsPath `
            -or $beforeEntry.VbsExists -ne $afterEntry.VbsExists `
            -or $beforeEntry.VbsHash -ne $afterEntry.VbsHash) {
            throw "PRODUCTION_SCHEDULED_TASK_MUTATED: '$($beforeEntry.TaskName)' changed during this Pester file's execution.`nBefore: Exists=$($beforeEntry.Exists) Execute=$($beforeEntry.Execute) Arguments=$($beforeEntry.Arguments) VbsPath=$($beforeEntry.VbsPath) VbsExists=$($beforeEntry.VbsExists) VbsHash=$($beforeEntry.VbsHash)`nAfter:  Exists=$($afterEntry.Exists) Execute=$($afterEntry.Execute) Arguments=$($afterEntry.Arguments) VbsPath=$($afterEntry.VbsPath) VbsExists=$($afterEntry.VbsExists) VbsHash=$($afterEntry.VbsHash)"
        }
    }
}
