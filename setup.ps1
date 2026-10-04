# One-time setup for a fresh clone of FloodLine (Windows, PowerShell 5.1+):
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Safe to run again: every step skips what is already done.
#
# Needs: Python 3.12 (python.org installer, which registers the `py` launcher) and Node.js 20+.

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }

# 1. Python virtual environment.
# We deliberately use the x64 build of Python 3.12 (`py -V:3.12`), even on ARM64 laptops:
# google-genai pulls in `cryptography`, which ships no Windows-ARM64 wheels for every release and
# would try (and fail) to compile from source with native ARM64 Python. x64 Python runs under
# Windows 11's emulation and installs the prebuilt x64 wheels. Override with $env:FLOODLINE_PY.
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path $venvPython) {
    Step 'Python venv already exists (.venv)'
} else {
    Step 'Creating the Python venv (.venv)'
    if ($env:FLOODLINE_PY) {
        & $env:FLOODLINE_PY -m venv .venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        # -V:3.12 is the x64 3.12 install; ARM64 builds register as -V:3.12-arm64.
        & py -V:3.12 -m venv .venv
    } else {
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw 'Could not create .venv. Install Python 3.12 (x64) from python.org, or set $env:FLOODLINE_PY to a python.exe.'
    }
}

Step 'Installing Python packages (backend/requirements.txt)'
# scripts\constraints.txt pins the exact versions the demo was tested with.
& $venvPython -m pip install --disable-pip-version-check -q -r backend\requirements.txt -c scripts\constraints.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

# 2. Frontend packages.
Step 'Installing frontend packages (npm install)'
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw 'npm not found. Install Node.js 20+ (nodejs.org), then run setup.ps1 again.'
}
Push-Location frontend
try {
    # npm writes warnings to stderr; do not let that count as a PowerShell error.
    $ErrorActionPreference = 'Continue'
    npm install --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw 'npm install failed.' }
} finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}

# 3. cloudflared, for the free https tunnel phones use.
Step 'Getting cloudflared (tools\cloudflared.exe)'
try {
    & (Join-Path $PSScriptRoot 'scripts\get_cloudflared.ps1')
} catch {
    Write-Host "Could not download cloudflared: $_" -ForegroundColor Yellow
    Write-Host 'FloodLine still runs locally; run scripts\get_cloudflared.ps1 later for phone access.' -ForegroundColor Yellow
}

# 4. Settings file.
if (Test-Path .env) {
    Step '.env already exists'
} else {
    Step 'Creating .env from .env.example'
    Copy-Item .env.example .env
}

# Windows' default execution policy ("Restricted") refuses to run .\run.ps1 directly. This script
# was started with -ExecutionPolicy Bypass, so look at the policy a new window will have instead.
$policy = 'Restricted'
foreach ($scope in 'MachinePolicy', 'UserPolicy', 'CurrentUser', 'LocalMachine') {
    $value = Get-ExecutionPolicy -Scope $scope
    if ($value -ne 'Undefined') { $policy = $value; break }
}
if ($policy -in 'Restricted', 'AllSigned') {
    $start = 'powershell -ExecutionPolicy Bypass -File .\run.ps1 --open'
} else {
    $start = '.\run.ps1 --open'
}

Write-Host "`nSetup done." -ForegroundColor Green
Write-Host '  Optional: put your Gemini key in .env (GEMINI_API_KEY=...). Without it, a keyword fallback is used.'
Write-Host "  Start the demo:  $start"
Write-Host '             (or:  .venv\Scripts\python.exe run.py --open)'
