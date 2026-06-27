param(
    [string]$RepoUrl = $env:ARGUS_REPO_URL,
    [string]$PackageSpec = $env:ARGUS_PACKAGE_SPEC,
    [string]$Python = $env:ARGUS_PYTHON,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "argus installer: $Message"
}

function Invoke-Step {
    param([string[]]$Command)
    Write-Host ("+ " + ($Command -join " "))
    if (-not $DryRun) {
        & $Command[0] @($Command | Select-Object -Skip 1)
    }
}

function Test-Python {
    param([string]$Candidate)
    try {
        $code = @"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
"@
        & $Candidate -c $code *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-Python {
    if ($Python) {
        if ((Get-Command $Python -ErrorAction SilentlyContinue) -and (Test-Python $Python)) {
            return $Python
        }
        throw "ARGUS_PYTHON is not Python 3.11+: $Python"
    }

    foreach ($candidate in @("py", "python3.12", "python3.11", "python")) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) {
            continue
        }

        if ($candidate -eq "py") {
            try {
                & py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
                if ($LASTEXITCODE -eq 0) {
                    $resolved = & py -3.12 -c "import sys; print(sys.executable)"
                    return $resolved.Trim()
                }
            } catch {
                continue
            }
        } elseif (Test-Python $candidate) {
            return $candidate
        }
    }

    throw "Python 3.11+ not found. Install Python 3.12 or 3.11, then rerun."
}

if (-not $RepoUrl) {
    $RepoUrl = "https://github.com/dangogit/argus.git"
}

if (-not $PackageSpec) {
    $PackageSpec = "git+$RepoUrl"
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git not found. Install Git for Windows first."
}

$PythonCommand = Find-Python
Write-Step "using $PythonCommand"
Write-Step "installing $PackageSpec"

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Invoke-Step @("pipx", "install", "--force", "--python", $PythonCommand, $PackageSpec)
} else {
    Write-Step "pipx not found, installing pipx for current user"
    Invoke-Step @($PythonCommand, "-m", "pip", "install", "--user", "--upgrade", "pipx")
    Invoke-Step @($PythonCommand, "-m", "pipx", "install", "--force", "--python", $PythonCommand, $PackageSpec)
    Invoke-Step @($PythonCommand, "-m", "pipx", "ensurepath")
}

Write-Host ""
Write-Host "Argus installed. Restart PowerShell if argus is not on PATH, then run:"
Write-Host "  argus --version"
Write-Host "  argus doctor"
