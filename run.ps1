# Starts FloodLine: .\run.ps1 [--open] [--no-tunnel] [--port 8000] [--reset] [--build]
# All arguments go straight to run.py (see `.\run.ps1 --help`).
# If Windows says "running scripts is disabled on this system", start it as
#   powershell -ExecutionPolicy Bypass -File .\run.ps1 --open
# or skip PowerShell entirely:  .venv\Scripts\python.exe run.py --open
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'No .venv found. Run setup first:  powershell -ExecutionPolicy Bypass -File .\setup.ps1' -ForegroundColor Yellow
    exit 1
}
& $python (Join-Path $PSScriptRoot 'run.py') @args
exit $LASTEXITCODE
