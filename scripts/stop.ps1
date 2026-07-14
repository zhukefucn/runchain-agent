[CmdletBinding()]
param(
    [string]$RunDirectory,
    [int]$ShutdownTimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($RunDirectory)) {
    $RunDirectory = Join-Path $projectRoot ".run"
}
$resolvedRunDirectory = [System.IO.Path]::GetFullPath($RunDirectory)
$recordPath = Join-Path $resolvedRunDirectory "server.json"
$expectedPython = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".venv\Scripts\python.exe"))
$expectedLauncher = [System.IO.Path]::GetFullPath((Join-Path $resolvedRunDirectory "launcher.py"))

function Test-SamePath {
    param([string]$Left, [string]$Right)

    try {
        return [string]::Equals(
            [System.IO.Path]::GetFullPath($Left),
            [System.IO.Path]::GetFullPath($Right),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Assert-OwnedProcessIdentity {
    param($Record)

    $recordedPid = [int]$Record.pid
    $cim = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $recordedPid" -ErrorAction SilentlyContinue
    if ($null -eq $cim) {
        return $null
    }

    $identityErrors = [System.Collections.Generic.List[string]]::new()
    if (-not (Test-SamePath ([string]$Record.projectRoot) $projectRoot)) {
        $identityErrors.Add("projectRoot")
    }
    if (-not (Test-SamePath ([string]$Record.executablePath) $expectedPython)) {
        $identityErrors.Add("executablePath record")
    }
    if (-not (Test-SamePath ([string]$cim.ExecutablePath) $expectedPython)) {
        $identityErrors.Add("executablePath process")
    }
    if (-not (Test-SamePath ([string]$Record.launcherPath) $expectedLauncher)) {
        $identityErrors.Add("launcherPath")
    }
    $parsedRunId = [guid]::Empty
    if (-not [guid]::TryParseExact([string]$Record.runId, "D", [ref]$parsedRunId)) {
        $identityErrors.Add("runId format")
    }
    $commandLine = [string]$cim.CommandLine
    if ($commandLine -notmatch [regex]::Escape($expectedLauncher) -or
        $commandLine -notmatch [regex]::Escape([string]$Record.runId)) {
        $identityErrors.Add("commandLine marker")
    }

    try {
        $expectedStart = [datetime]::Parse(
            [string]$Record.processStartTimeUtc,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
        $actualStart = (Get-Process -Id $recordedPid -ErrorAction Stop).StartTime.ToUniversalTime()
        if ([math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 2) {
            $identityErrors.Add("processStartTimeUtc")
        }
    }
    catch {
        $identityErrors.Add("processStartTimeUtc")
    }

    if ($identityErrors.Count -gt 0) {
        throw "Process identity check failed ($($identityErrors -join ', ')); refusing to stop PID $recordedPid."
    }
    return $cim
}

if (-not (Test-Path -LiteralPath $recordPath)) {
    Write-Host "No DEMO process record found; nothing to stop."
    exit 0
}

try {
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    if ([int]$record.schemaVersion -ne 1 -or [int]$record.pid -le 0) {
        throw "Process identity record is invalid."
    }
}
catch {
    throw "Process identity record cannot be trusted: $($_.Exception.Message)"
}

$ownedProcess = Assert-OwnedProcessIdentity $record
if ($null -eq $ownedProcess) {
    Remove-Item -LiteralPath $recordPath -Force
    Remove-Item -LiteralPath $expectedLauncher -Force -ErrorAction SilentlyContinue
    Write-Host "Removed stale DEMO process record; no process was stopped."
    exit 0
}

$recordedPid = [int]$record.pid
Stop-Process -Id $recordedPid
$deadline = [datetime]::UtcNow.AddSeconds($ShutdownTimeoutSeconds)
while ([datetime]::UtcNow -lt $deadline) {
    if ($null -eq (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)) {
        break
    }
    Start-Sleep -Milliseconds 200
}

if ($null -ne (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)) {
    # Recheck all identity markers before escalating so PID reuse can never
    # turn a delayed force-stop into a broad or unrelated process kill.
    [void](Assert-OwnedProcessIdentity $record)
    Stop-Process -Id $recordedPid -Force
    Wait-Process -Id $recordedPid -Timeout 5 -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $recordPath -Force
Remove-Item -LiteralPath $expectedLauncher -Force -ErrorAction SilentlyContinue
Write-Host "Stopped verified DEMO process PID $recordedPid."

