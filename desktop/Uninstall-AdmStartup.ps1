# Uninstaller for ADM Windows Startup & Desktop / Start Menu shortcuts.

$ErrorActionPreference = "Continue"

$desktopDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
$programsDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
$startupDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)

$shortcutName = "AI Development Manager.lnk"

$targets = @(
    (Join-Path $desktopDir $shortcutName),
    (Join-Path $programsDir $shortcutName),
    (Join-Path $startupDir $shortcutName)
)

foreach ($path in $targets) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
        Write-Host "Removed: $path" -ForegroundColor Yellow
    }
}

Write-Host "ADM Startup and shortcuts removed." -ForegroundColor Green
