[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

function Test-ContainedPath {
    param([string]$Candidate, [string]$ExpectedParent)

    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $parentPath = [System.IO.Path]::GetFullPath($ExpectedParent).TrimEnd('\')
    return $candidatePath.StartsWith(
        $parentPath + '\',
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NoReparsePoint {
    param([string]$Path)

    $current = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $boundary = $projectRoot.TrimEnd('\')
    if (-not (Test-ContainedPath $current $boundary)) {
        throw "Reset target escaped the project root: $Path"
    }
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse point is not allowed in DEMO reset targets: $current"
            }
        }
        if ([string]::Equals($current, $boundary, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent) {
            throw "Reset target escaped the project root: $Path"
        }
        $current = $parent.FullName.TrimEnd('\')
    }
}

$approvedTargets = @(
    [pscustomobject]@{ Name = "data\demo.db"; Path = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "data\demo.db")) },
    [pscustomobject]@{ Name = "data\demo.db-shm"; Path = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "data\demo.db-shm")) },
    [pscustomobject]@{ Name = "data\demo.db-wal"; Path = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "data\demo.db-wal")) },
    [pscustomobject]@{ Name = "workspace"; Path = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "workspace")) },
    [pscustomobject]@{ Name = "skills"; Path = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "skills")) },
    [pscustomobject]@{ Name = "runner"; Path = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "runner")) },
    [pscustomobject]@{ Name = ".run\e2e"; Path = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".run\e2e")) }
)

# Validate every fixed target before stopping the server or removing anything.
foreach ($target in $approvedTargets) {
    Assert-NoReparsePoint $target.Path
}

# stop.ps1 verifies the recorded executable, start time, launcher and run ID
# before stopping anything.  No broad process-name or port kill is used.
& (Join-Path $PSScriptRoot "stop.ps1")

foreach ($target in $approvedTargets) {
    # Recheck immediately before deletion to close the validation/use window.
    Assert-NoReparsePoint $target.Path
    if (Test-Path -LiteralPath $target.Path) {
        Remove-Item -LiteralPath $target.Path -Recurse -Force
        Write-Host "Removed DEMO runtime state: $($target.Name)"
    }
}

# Deliberately preserved: .env and demo-skills (the checked-in Skill fixture).
Write-Host "DEMO runtime state reset. Configuration and source Skill fixtures were preserved."
