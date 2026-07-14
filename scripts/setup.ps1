$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$agentscopePath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\agentscope-main"))
$lockPath = Join-Path $projectRoot "requirements.lock"

function Find-SupportedPython {
    $candidates = [System.Collections.Generic.List[object]]::new()
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
                    return $candidate
                }
            }
        }
        catch {
            continue
        }
    }

    return $null
}

if (-not (Test-Path $agentscopePath)) {
    throw "Local AgentScope checkout was not found at $agentscopePath"
}

if (-not (Test-Path $lockPath)) {
    throw "Locked dependencies were not found at $lockPath"
}

if (-not (Test-Path $venvPython)) {
    $python = Find-SupportedPython
    if ($null -eq $python) {
        throw "Python 3.11, 3.12, or 3.13 is required."
    }

    Write-Host "Creating virtual environment at $venvPath"
    $prefixArguments = $python.Arguments
    & $python.Command @prefixArguments -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Virtual environment creation failed."
    }
}

Write-Host "Installing locked dependencies"
& $venvPython -m pip install --disable-pip-version-check --requirement $lockPath
if ($LASTEXITCODE -ne 0) {
    throw "Locked dependency installation failed."
}

Write-Host "Installing the local demo in editable mode without resolving dependencies"
& $venvPython -m pip install --disable-pip-version-check --no-deps --editable "${projectRoot}[dev]"
if ($LASTEXITCODE -ne 0) {
    throw "Demo installation failed."
}

Write-Host "Installing the local AgentScope checkout in editable mode without resolving dependencies"
& $venvPython -m pip install --disable-pip-version-check --no-deps --editable $agentscopePath
if ($LASTEXITCODE -ne 0) {
    throw "AgentScope installation failed."
}

Write-Host "Setup complete. Copy .env.example to .env, set the required values, then run scripts\check-env.ps1."
