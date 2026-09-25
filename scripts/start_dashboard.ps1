<#
.SYNOPSIS
  One command to start the job-agent dashboard and make it reachable over
  your private Tailscale tailnet (never the public internet).

.DESCRIPTION
  1. Confirms Tailscale is installed and you're logged in (this script can't
     do that part for you -- it's tied to your personal account).
  2. Points `tailscale serve` at the dashboard's port, so your tailnet HTTPS
     hostname proxies straight through to it. This is a background daemon
     setting, not tied to this script's lifetime -- it keeps working even
     after you close this window, until you run `tailscale serve reset`.
  3. Warns if DASHBOARD_USERNAME/DASHBOARD_PASSWORD aren't set in .env, since
     serving beyond localhost without a login would let anyone on your
     tailnet use the console with no credentials at all.
  4. Runs the dashboard itself in the foreground, so you see its live log.
     Press Ctrl+C to stop the dashboard; Tailscale serve keeps running
     independently (run `tailscale serve reset` to stop sharing it too).

.PARAMETER Port
  Dashboard port. Must match DASHBOARD_ALLOWED_HOSTS's target if you change it.

.EXAMPLE
  .\scripts\start_dashboard.ps1
#>
param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Find-Tailscale {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $default = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $default) { return $default }
    return $null
}

$tailscaleExe = Find-Tailscale
if (-not $tailscaleExe) {
    Write-Host "Tailscale isn't installed. Install it first:" -ForegroundColor Yellow
    Write-Host "  winget install Tailscale.Tailscale"
    Write-Host "Then run 'tailscale up' to sign in, and re-run this script."
    Write-Host ""
    Write-Host "Starting the dashboard locally only (http://127.0.0.1:$Port) instead." -ForegroundColor Yellow
    python main.py ui --no-browser --port $Port
    exit
}

$statusOutput = & $tailscaleExe status 2>&1
if ($statusOutput -match "Logged out") {
    Write-Host "Not signed in to Tailscale yet. Run this, sign in with your own account, then re-run this script:" -ForegroundColor Yellow
    Write-Host "  tailscale up"
    exit 1
}

# .env may not have a login configured on a fresh checkout; warn loudly rather
# than silently exposing the console with no credentials once it's shared.
$envPath = Join-Path $RepoRoot ".env"
$hasLogin = $false
if (Test-Path $envPath) {
    $envText = Get-Content $envPath -Raw
    $hasLogin = ($envText -match "DASHBOARD_USERNAME=\S") -and ($envText -match "DASHBOARD_PASSWORD=\S")
}
if (-not $hasLogin) {
    Write-Host "WARNING: DASHBOARD_USERNAME/DASHBOARD_PASSWORD are not both set in .env." -ForegroundColor Red
    Write-Host "Anyone on your tailnet could reach this dashboard with no login. Add both to .env before continuing, e.g.:" -ForegroundColor Red
    Write-Host "  DASHBOARD_USERNAME=your-name"
    Write-Host "  DASHBOARD_PASSWORD=$(([guid]::NewGuid()).ToString('N'))"
    Write-Host ""
    $answer = Read-Host "Continue without a login anyway? [y/N]"
    if ($answer -notmatch "^[yY]") { exit 1 }
}

Write-Host "Pointing tailscale serve at http://127.0.0.1:$Port ..." -ForegroundColor Cyan
& $tailscaleExe serve --bg "http://127.0.0.1:$Port" | Out-Null
$serveStatus = & $tailscaleExe serve status 2>&1
Write-Host $serveStatus

$urlLine = $serveStatus | Select-String -Pattern "https://\S+\.ts\.net" | Select-Object -First 1
if ($urlLine) {
    $hostname = ([regex]::Match($urlLine, "https://([^/\s]+)")).Groups[1].Value
    Write-Host ""
    Write-Host "Dashboard will be reachable at: https://$hostname/" -ForegroundColor Green
    if (Test-Path $envPath) {
        $envText = Get-Content $envPath -Raw
        if ($envText -notmatch [regex]::Escape("DASHBOARD_ALLOWED_HOSTS=$hostname")) {
            Write-Host "NOTE: add this to .env so the app accepts that hostname, then re-run this script:" -ForegroundColor Yellow
            Write-Host "  DASHBOARD_ALLOWED_HOSTS=$hostname"
        }
    }
}

Write-Host ""
Write-Host "Starting the dashboard (Ctrl+C to stop it; Tailscale serve keeps running)..." -ForegroundColor Cyan
python main.py ui --no-browser --port $Port
