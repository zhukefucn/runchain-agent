$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$failures = [System.Collections.Generic.List[string]]::new()

function Add-Failure {
    param([string]$Message)

    $script:failures.Add($Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

function Get-SupportedPythonVersion {
    $candidates = [System.Collections.Generic.List[object]]::new()
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

    if (Test-Path $venvPython) {
        $candidates.Add([pscustomobject]@{ Command = $venvPython; Arguments = @() })
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($minor in @(13, 12, 11)) {
            $candidates.Add([pscustomobject]@{ Command = "py"; Arguments = @("-3.$minor") })
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $candidates.Add([pscustomobject]@{ Command = "python"; Arguments = @() })
    }

    foreach ($candidate in $candidates) {
        try {
            $prefixArguments = $candidate.Arguments
            $versionOutput = (& $candidate.Command @prefixArguments --version 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $versionOutput -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -eq 3 -and $minor -ge 11 -and $minor -le 13) {
                    return $versionOutput
                }
            }
        }
        catch {
            continue
        }
    }

    return $null
}

$pythonVersion = Get-SupportedPythonVersion
if ($null -eq $pythonVersion) {
    Add-Failure "Python 3.11, 3.12, or 3.13 is required."
}
else {
    Write-Host "[OK] $pythonVersion"
}

try {
    $nodeVersion = (& node --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Node exited with code $LASTEXITCODE"
    }
    Write-Host "[OK] Node $nodeVersion"
}
catch {
    Add-Failure "Node.js is required."
}

$listeningPorts = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port
foreach ($port in @(8000, 5173)) {
    if ($listeningPorts -contains $port) {
        Add-Failure "Port $port is already in use."
    }
    else {
        Write-Host "[OK] Port $port is available"
    }
}

$dotenvValues = @{}
$dotenvPath = Join-Path $projectRoot ".env"
if (Test-Path $dotenvPath) {
    foreach ($line in Get-Content $dotenvPath) {
        if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$") {
            $name = $Matches[1]
            $value = $Matches[2].Trim()
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $dotenvValues[$name] = $value
        }
    }
}

foreach ($name in @("JWT_SECRET_KEY", "MODEL_API_KEY")) {
    $processValue = [Environment]::GetEnvironmentVariable($name)
    $isConfigured = -not [string]::IsNullOrWhiteSpace($processValue)
    if (-not $isConfigured -and $dotenvValues.ContainsKey($name)) {
        $isConfigured = -not [string]::IsNullOrWhiteSpace([string]$dotenvValues[$name])
    }

    if ($isConfigured) {
        Write-Host "[OK] $name is configured"
    }
    else {
        Add-Failure "$name is not configured. Set it in .env or the process environment."
    }
}

if ($failures.Count -gt 0) {
    Write-Host "Environment check failed with $($failures.Count) issue(s)." -ForegroundColor Red
    exit 1
}

Write-Host "Environment check passed."
