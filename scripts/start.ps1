[CmdletBinding()]
param(
    [switch]$SkipSetup,
    [int]$StartupTimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runDirectory = Join-Path $projectRoot ".run"
$recordPath = Join-Path $runDirectory "server.json"
$launcherPath = Join-Path $runDirectory "launcher.py"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontendDist = Join-Path $projectRoot "frontend\dist"
$frontendModules = Join-Path $projectRoot "frontend\node_modules"
$stdoutPath = Join-Path $runDirectory "server.stdout.log"
$stderrPath = Join-Path $runDirectory "server.stderr.log"
$port = 8000

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)."
    }
}

function Wait-HttpSuccess {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][datetime]$Deadline
    )

    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 300
        }
    } while ([datetime]::UtcNow -lt $Deadline)

    throw "Timed out waiting for $Url"
}

if (Test-Path $recordPath) {
    throw "A DEMO process record already exists at $recordPath. Run scripts\stop.ps1 first; it safely removes stale records."
}

if (-not $SkipSetup -and -not (Test-Path $pythonPath)) {
    Write-Host "Python environment is missing; running deterministic setup."
    Invoke-Checked -Command { & (Join-Path $PSScriptRoot "setup.ps1") } -FailureMessage "Backend setup failed"
}
if (-not (Test-Path $pythonPath)) {
    throw "Python environment was not found at $pythonPath. Run scripts\setup.ps1."
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw "pnpm is required. Install the package manager declared by frontend\package.json."
}
if (-not (Test-Path (Join-Path $projectRoot ".env"))) {
    throw ".env is required. Copy .env.example and configure JWT_SECRET_KEY and MODEL_API_KEY."
}

# The Windows Phase 1 topology has one listener: FastAPI serves both /api and frontend/dist.
& (Join-Path $PSScriptRoot "check-env.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Environment validation failed."
}

if (-not $SkipSetup -and -not (Test-Path $frontendModules)) {
    Invoke-Checked -Command {
        & pnpm --dir (Join-Path $projectRoot "frontend") install --frozen-lockfile
    } -FailureMessage "Frontend dependency installation failed"
}
if (-not (Test-Path $frontendModules)) {
    throw "Frontend dependencies are missing. Run pnpm --dir frontend install --frozen-lockfile."
}

Invoke-Checked -Command {
    & pnpm --dir (Join-Path $projectRoot "frontend") run build
} -FailureMessage "Frontend build failed"
if (-not (Test-Path (Join-Path $frontendDist "index.html"))) {
    throw "Frontend build did not produce frontend\dist\index.html."
}

New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$launcher = @'
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "backend"))
    from app.config import get_settings
    from app.main import create_root_app

    uvicorn.run(
        create_root_app(get_settings()),
        host="127.0.0.1",
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
'@
[System.IO.File]::WriteAllText($launcherPath, $launcher, [System.Text.UTF8Encoding]::new($false))

$runId = [guid]::NewGuid().ToString("D")
# Keep the fixed `--port 8000` token visible in the owned command line.
$arguments = "`"$launcherPath`" --run-id $runId --port 8000"
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru

try {
    $process = Get-Process -Id $process.Id -ErrorAction Stop
    $record = [ordered]@{
        schemaVersion = 1
        pid = $process.Id
        projectRoot = $projectRoot
        runId = $runId
        processStartTimeUtc = $process.StartTime.ToUniversalTime().ToString("o")
        executablePath = [System.IO.Path]::GetFullPath($pythonPath)
        launcherPath = [System.IO.Path]::GetFullPath($launcherPath)
        port = $port
        createdAtUtc = [datetime]::UtcNow.ToString("o")
    }
    $temporaryRecord = "$recordPath.tmp"
    $record | ConvertTo-Json | Set-Content -LiteralPath $temporaryRecord -Encoding UTF8
    Move-Item -LiteralPath $temporaryRecord -Destination $recordPath -Force

    $deadline = [datetime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    Wait-HttpSuccess -Url "http://127.0.0.1:8000/api/health" -Deadline $deadline
    Wait-HttpSuccess -Url "http://127.0.0.1:8000/api/ready" -Deadline $deadline
    Wait-HttpSuccess -Url "http://127.0.0.1:8000/" -Deadline $deadline
}
catch {
    Write-Warning "DEMO startup failed: $($_.Exception.Message)"
    if (Test-Path $recordPath) {
        & (Join-Path $PSScriptRoot "stop.ps1") -RunDirectory $runDirectory
    }
    elseif (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
    throw
}

Write-Host "DEMO ready at http://127.0.0.1:8000/ (PID $($process.Id))."
Write-Host "Logs: $stdoutPath and $stderrPath"
