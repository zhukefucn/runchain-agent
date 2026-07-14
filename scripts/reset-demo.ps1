[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

function Test-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$ExpectedParent
    )

    $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
    $parentPath = [System.IO.Path]::GetFullPath($ExpectedParent).TrimEnd('\') + '\'
    return $candidatePath.StartsWith($parentPath, [System.StringComparison]::OrdinalIgnoreCase)
}

function Remove-DemoTarget {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$AllowedParent
    )

    $target = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $RelativePath))
    $parent = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $AllowedParent))
    if (-not (Test-ContainedPath $target $parent)) {
        throw "Reset target escaped its approved runtime directory: $RelativePath"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Host "Removed DEMO runtime state: $RelativePath"
    }
}

# stop.ps1 verifies the recorded executable, start time, launcher and run ID
# before stopping anything.  No broad process-name or port kill is used.
& (Join-Path $PSScriptRoot "stop.ps1")

Remove-DemoTarget "data\demo.db" "data"
Remove-DemoTarget "data\demo.db-shm" "data"
Remove-DemoTarget "data\demo.db-wal" "data"
Remove-DemoTarget "workspace" "."
Remove-DemoTarget "skills" "."
Remove-DemoTarget "runner" "."
Remove-DemoTarget ".run\e2e" ".run"

# Deliberately preserved: .env and demo-skills (the checked-in Skill fixture).
Write-Host "DEMO runtime state reset. Configuration and source Skill fixtures were preserved."
