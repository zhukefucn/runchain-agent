[CmdletBinding()]
param(
    [switch]$SkipE2E,
    [switch]$RunRealModel
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontendRoot = Join-Path $projectRoot "frontend"

function Assert-LastExitCode {
    param([string]$Stage)
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed (exit code $LASTEXITCODE)."
    }
}

if (-not (Test-Path $pythonPath)) {
    throw "Python environment is missing. Run scripts\setup.ps1 first."
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw "pnpm is required."
}

$previousRealModelTestsEnvironment = Get-Item Env:RUN_REAL_MODEL_TESTS -ErrorAction SilentlyContinue
$locationPushed = $false
try {
    if ($RunRealModel) {
        $env:RUN_REAL_MODEL_TESTS = "1"
    }
    Push-Location $projectRoot
    $locationPushed = $true

    Write-Host "[1/5] Backend tests (real model excluded)"
    & $pythonPath -m pytest backend/tests -q -m "not real_model"
    Assert-LastExitCode "Backend tests"

    Write-Host "[2/5] Frontend Vitest"
    & pnpm --dir $frontendRoot exec vitest run --exclude "tests/e2e/**"
    Assert-LastExitCode "Frontend Vitest"

    Write-Host "[3/5] Frontend typecheck"
    & pnpm --dir $frontendRoot run typecheck
    Assert-LastExitCode "Frontend typecheck"

    Write-Host "[4/5] Frontend production build"
    & pnpm --dir $frontendRoot run build
    Assert-LastExitCode "Frontend production build"

    if ($SkipE2E) {
        Write-Host "[5/5] Playwright E2E explicitly skipped by -SkipE2E"
    }
    else {
        Write-Host "[5/5] Playwright E2E"
        & pnpm --dir $frontendRoot exec playwright test tests/e2e
        Assert-LastExitCode "Playwright E2E"
    }

    $runRealModelTests = $RunRealModel -or $env:RUN_REAL_MODEL_TESTS -eq "1"
    if ($runRealModelTests) {
        Write-Host "[opt-in] Real model smoke tests"
        & $pythonPath -m pytest backend/tests/smoke -q -m real_model
        Assert-LastExitCode "Real model smoke tests"
    }
    else {
        Write-Host "Real model smoke tests were not run. Use -RunRealModel or RUN_REAL_MODEL_TESTS=1 separately."
    }
}
finally {
    if ($locationPushed) {
        Pop-Location
    }
    if ($RunRealModel) {
        if ($null -eq $previousRealModelTestsEnvironment) {
            Remove-Item Env:RUN_REAL_MODEL_TESTS -ErrorAction SilentlyContinue
        }
        else {
            $env:RUN_REAL_MODEL_TESTS = $previousRealModelTestsEnvironment.Value
        }
    }
}

Write-Host "Verification complete."
