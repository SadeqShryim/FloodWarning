# Downloads cloudflared (Cloudflare's tunnel client) into tools\cloudflared.exe.
#
# FloodLine uses a free "quick tunnel" so judges' phones get an https:// link to the laptop.
# Phones need https: browsers only allow microphone access on secure origins.
#
# Idempotent: does nothing if tools\cloudflared.exe already exists (pass -Force to re-download).
# Prefers a native windows-arm64 build if the release has one; otherwise takes windows-amd64,
# which runs fine under Windows 11's x64 emulation on ARM laptops.
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 defaults to old TLS versions that GitHub rejects.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
# The progress bar makes Invoke-WebRequest many times slower in PowerShell 5.1.
$ProgressPreference = 'SilentlyContinue'

$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $repoRoot 'tools'
$target = Join-Path $toolsDir 'cloudflared.exe'

if ((Test-Path $target) -and -not $Force) {
    Write-Host "cloudflared already present: $target"
    exit 0
}

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

Write-Host 'Looking up the latest cloudflared release on GitHub...'
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/cloudflare/cloudflared/releases/latest' `
    -Headers @{ 'User-Agent' = 'FloodLine-setup' }

$names = @('cloudflared-windows-arm64.exe', 'cloudflared-windows-amd64.exe')
$asset = $null
foreach ($name in $names) {
    $asset = $release.assets | Where-Object { $_.name -eq $name } | Select-Object -First 1
    if ($asset) { break }
}
if (-not $asset) {
    throw "No Windows cloudflared build found in release $($release.tag_name)."
}

Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB, 1)) MB) from release $($release.tag_name)..."
# Download to a temp name first so an interrupted download never leaves a broken cloudflared.exe.
$partial = "$target.partial"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $partial -UseBasicParsing
Move-Item -Force $partial $target

$version = & $target --version 2>&1 | Select-Object -First 1
Write-Host "Installed: $target"
Write-Host "  $version"
