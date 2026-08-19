<#
.SYNOPSIS
Profiles each manager/test_*.py file in its own pytest process.

.EXAMPLE
./tools/profile_pytest_memory.ps1 -Python python -OutputPath ./pytest-memory-profile.csv

Reusing an output CSV resumes the run; pass -Reset to start over.
#>
param(
    [string]$Python = "python",
    [string]$OutputPath = (Join-Path $PSScriptRoot "pytest-memory-profile.csv"),
    [ValidateRange(10, 5000)]
    [int]$SampleIntervalMs = 100,
    [switch]$Reset
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

if ($Reset -and (Test-Path -LiteralPath $OutputPath)) {
    Remove-Item -LiteralPath $OutputPath -Force
}

$completed = @{}
if (Test-Path -LiteralPath $OutputPath) {
    Import-Csv -LiteralPath $OutputPath | ForEach-Object { $completed[$_.file] = $true }
}

$testFiles = Get-ChildItem -LiteralPath (Join-Path $repoRoot "manager") -Filter "test_*.py" -File | Sort-Object Name
Push-Location $repoRoot
try {
    foreach ($testFile in $testFiles) {
        $relativePath = "manager/$($testFile.Name)"
        if ($completed.ContainsKey($relativePath)) {
            Write-Host "SKIP $relativePath"
            continue
        }

        Write-Host "RUN  $relativePath"
        $stdoutPath = [IO.Path]::GetTempFileName()
        $stderrPath = [IO.Path]::GetTempFileName()
        $stopwatch = [Diagnostics.Stopwatch]::StartNew()
        $peakBytes = 0L
        $lastBytes = 0L
        $exitCode = -1

        try {
            $process = Start-Process -FilePath $Python -ArgumentList @(
                "-m", "pytest", $relativePath, "-q", "--tb=short"
            ) -NoNewWindow -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

            while (-not $process.HasExited) {
                try {
                    $process.Refresh()
                    $lastBytes = $process.WorkingSet64
                    $peakBytes = [Math]::Max($peakBytes, $lastBytes)
                }
                catch {
                    # The process may exit between HasExited and Refresh.
                }
                Start-Sleep -Milliseconds $SampleIntervalMs
            }

            $process.WaitForExit()
            $exitCode = $process.ExitCode
            try {
                $peakBytes = [Math]::Max($peakBytes, $process.PeakWorkingSet64)
            }
            catch {
                # Sampled peak remains valid if Windows has already released the process handle.
            }
        }
        catch {
            Write-Warning "$relativePath could not start: $($_.Exception.Message)"
        }
        finally {
            $stopwatch.Stop()
        }

        $row = [pscustomobject][ordered]@{
            file        = $relativePath
            exit_code   = $exitCode
            duration    = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
            peak_rss_mb = [Math]::Round($peakBytes / 1MB, 1)
            end_rss_mb  = [Math]::Round($lastBytes / 1MB, 1)
        }
        $row | Export-Csv -LiteralPath $OutputPath -NoTypeInformation -Append
        $row | Format-Table -AutoSize

        if ($exitCode -ne 0) {
            Get-Content -LiteralPath $stdoutPath, $stderrPath -Tail 20 | ForEach-Object { Write-Host "  $_" }
        }
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}
finally {
    Pop-Location
}

$results = Import-Csv -LiteralPath $OutputPath
$failedCount = @($results | Where-Object { [int]$_.exit_code -ne 0 }).Count
Write-Host "Results: $OutputPath ($($results.Count) files, $failedCount non-zero exits)"
Write-Host "Top 10 peak RSS:"
$results |
    Sort-Object { [double]$_.peak_rss_mb } -Descending |
    Select-Object -First 10 |
    Format-Table file, exit_code, duration, peak_rss_mb, end_rss_mb -AutoSize
