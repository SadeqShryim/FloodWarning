# Starts FloodLine: .\run.ps1 [--open] [--no-tunnel] [--port 8000] [--reset] [--build]
# All arguments go straight to run.py (see `.\run.ps1 --help`).
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'No .venv found. Run .\setup.ps1 first.' -ForegroundColor Yellow
    exit 1
}
& $python (Join-Path $PSScriptRoot 'run.py') @args
exit $LASTEXITCODE
