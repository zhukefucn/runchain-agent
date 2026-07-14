[CmdletBinding()]
param(
    [string]$RunDirectory,
    [int]$ShutdownTimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$approvedRunRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".run"))
if ([string]::IsNullOrWhiteSpace($RunDirectory)) {
    $RunDirectory = $approvedRunRoot
}
$resolvedRunDirectory = [System.IO.Path]::GetFullPath($RunDirectory)
$recordPath = Join-Path $resolvedRunDirectory "server.json"
$expectedPython = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".venv\Scripts\python.exe"))
$expectedLauncher = [System.IO.Path]::GetFullPath((Join-Path $resolvedRunDirectory "launcher.py"))

function Test-ContainedPath {
    param([string]$Candidate, [string]$ExpectedParent)

    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $parentPath = [System.IO.Path]::GetFullPath($ExpectedParent).TrimEnd('\')
    return [string]::Equals(
        $candidatePath,
        $parentPath,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -or $candidatePath.StartsWith(
        $parentPath + '\',
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NoReparsePoint {
    param([string]$Path, [string]$Boundary)

    $current = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $boundaryPath = [System.IO.Path]::GetFullPath($Boundary).TrimEnd('\')
    if (-not (Test-ContainedPath $current $boundaryPath)) {
        throw "Path escaped its approved boundary: $current"
    }
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse point is not allowed in DEMO runtime paths: $current"
            }
        }
        if ([string]::Equals($current, $boundaryPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent) {
            throw "Could not verify DEMO runtime path containment: $current"
        }
        $current = $parent.FullName.TrimEnd('\')
    }
}

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

function Assert-TrustedRecord {
    param($Record)

    $identityErrors = [System.Collections.Generic.List[string]]::new()
    if ([int]$Record.schemaVersion -ne 1 -or [int]$Record.pid -le 0) {
        $identityErrors.Add("schema or pid")
    }
    if (-not (Test-SamePath ([string]$Record.projectRoot) $projectRoot)) {
        $identityErrors.Add("projectRoot")
    }
    if (-not (Test-SamePath ([string]$Record.executablePath) $expectedPython)) {
        $identityErrors.Add("executablePath record")
    }
    if (-not (Test-SamePath ([string]$Record.launcherPath) $expectedLauncher)) {
        $identityErrors.Add("launcherPath")
    }
    $parsedRunId = [guid]::Empty
    if (-not [guid]::TryParseExact([string]$Record.runId, "D", [ref]$parsedRunId)) {
        $identityErrors.Add("runId format")
    }
    try {
        [void][datetime]::Parse(
            [string]$Record.processStartTimeUtc,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        )
    }
    catch {
        $identityErrors.Add("processStartTimeUtc")
    }
    if ($identityErrors.Count -gt 0) {
        throw "Process identity record failed validation ($($identityErrors -join ', '))."
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

if (-not (Test-ContainedPath $resolvedRunDirectory $approvedRunRoot)) {
    throw "RunDirectory must be contained by the project's .run directory."
}
Assert-NoReparsePoint $resolvedRunDirectory $approvedRunRoot
Assert-NoReparsePoint $expectedLauncher $approvedRunRoot

if (-not (Test-Path -LiteralPath $recordPath)) {
    Write-Host "No DEMO process record found; nothing to stop."
    exit 0
}
Assert-NoReparsePoint $recordPath $approvedRunRoot

try {
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    Assert-TrustedRecord $record
}
catch {
    throw "Process identity record cannot be trusted: $($_.Exception.Message)"
}

$ownedProcess = Assert-OwnedProcessIdentity $record
if ($null -eq $ownedProcess) {
    Assert-NoReparsePoint $recordPath $approvedRunRoot
    Assert-NoReparsePoint $expectedLauncher $approvedRunRoot
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

Assert-NoReparsePoint $recordPath $approvedRunRoot
Assert-NoReparsePoint $expectedLauncher $approvedRunRoot
Remove-Item -LiteralPath $recordPath -Force
Remove-Item -LiteralPath $expectedLauncher -Force -ErrorAction SilentlyContinue
Write-Host "Stopped verified DEMO process PID $recordedPid."
