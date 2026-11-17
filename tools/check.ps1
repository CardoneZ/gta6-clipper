<#
.SYNOPSIS
    GTA6-Clipper validation gate: Python syntax, Ruff blocking rules, unit tests, JS syntax.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File gta6-clipper\tools\check.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $AppRoot ".venv\Scripts\python.exe"
$failed = $false
$env:PYTHONDONTWRITEBYTECODE = "1"

function Step([string]$Name, [scriptblock]$Body) {
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & $Body 2>&1 | ForEach-Object { "$_" }; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
    if ($null -eq $code) { $code = 0 }
    $status = if ($code -eq 0) { "PASS" } else { "FAIL" }
    $color = if ($code -eq 0) { "Green" } else { "Red" }
    Write-Host ("[{0}] {1} ({2:N2}s)" -f $status, $Name, $watch.Elapsed.TotalSeconds) -ForegroundColor $color
    if ($code -ne 0) { $out | Select-Object -Last 40 | ForEach-Object { Write-Host "      $_" }; $script:failed = $true }
}

Push-Location $AppRoot
try {
    Step "py-syntax" { & $Python -B -c "import pathlib,sys; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in pathlib.Path('.').rglob('*.py')]" }
    Step "py-ruff" { & ruff check --no-cache --select "E9,F63,F7,F82" . }
    Step "py-tests" { & $Python -B -m unittest discover -s tests -p "test_*.py" }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        Step "js-syntax" { & node --check frontend/app.js }
    } else {
        Write-Host "[SKIP] js-syntax (node not on PATH)" -ForegroundColor Yellow
    }
} finally {
    Pop-Location
    Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
}

if ($failed) { exit 1 }
Write-Host "GTA6-Clipper gate: all steps passed." -ForegroundColor Green
