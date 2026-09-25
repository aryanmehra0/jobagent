<#
.SYNOPSIS
  One command to start the job-agent dashboard and keep it reachable over
  your private Tailscale tailnet (never the public internet) -- forever,
  self-healing, without depending on Task Scheduler's own restart logic.

.DESCRIPTION
  1. Confirms Tailscale is installed and you're logged in (this script can't
     do that part for you -- it's tied to your personal account). If either
     is missing, it says what to run, then retries automatically -- so if
     this is launched at logon before Tailscale has finished starting, or
     before you've had a chance to sign in, it recovers on its own once you
     do rather than needing a manual re-run.
  2. Points `tailscale serve` at the dashboard's port, so your tailnet HTTPS
     hostname proxies straight through to it. This is a background daemon
     setting, not tied to this script's lifetime -- it keeps working even
     after this script stops, until you run `tailscale serve reset`.
  3. Refuses to serve beyond localhost if DASHBOARD_USERNAME/DASHBOARD_PASSWORD
     aren't both set in .env, since that would let anyone on your tailnet use
     the console with no login at all. No interactive prompt: this script
     also runs unattended (the JobAgentDashboard scheduled task, hidden, no
     console), where a prompt would just hang forever.
  4. Runs the dashboard, and if it ever exits for any reason -- crash, killed,
     the PC waking from sleep with a stale process -- restarts it after a
     short pause, indefinitely. This loop is deliberately the sole restart
     mechanism: this script's own JobAgentDashboard scheduled task has
     RestartCount/RestartInterval configured too, as defense in depth, but
     testing showed Task Scheduler does not reliably restart a logon-triggered
     task after its process is killed, so nothing here depends on that working.

  Press Ctrl+C twice to stop the loop. Tailscale serve keeps sharing the port
  independently; run `tailscale serve reset` to stop that too.

.PARAMETER Port
  Dashboard port. Must match DASHBOARD_ALLOWED_HOSTS's target if you change it.

.EXAMPLE
  .\scripts\start_dashboard.ps1
#>
param(
    [int]$Port = 8765
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Find-Tailscale {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $default = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $default) { return $default }
    return $null
}

function Has-DashboardLogin {
    $envPath = Join-Path $RepoRoot ".env"
    if (-not (Test-Path $envPath)) { return $false }
    $envText = Get-Content $envPath -Raw
    return ($envText -match "DASHBOARD_USERNAME=\S") -and ($envText -match "DASHBOARD_PASSWORD=\S")
}

$loggedSetupOnce = $false

while ($true) {
    $tailscaleExe = Find-Tailscale
    if (-not $tailscaleExe) {
        Write-Host "Tailscale isn't installed. Install it, then this will pick it up automatically:" -ForegroundColor Yellow
        Write-Host "  winget install Tailscale.Tailscale"
        Write-Host "Retrying in 60 seconds. (Ctrl+C twice to stop.)"
        Start-Sleep -Seconds 60
        continue
    }

    $statusOutput = & $tailscaleExe status 2>&1
    if ($statusOutput -match "Logged out") {
        Write-Host "Not signed in to Tailscale yet. Sign in with your own account:" -ForegroundColor Yellow
        Write-Host "  tailscale up"
        Write-Host "Retrying in 60 seconds. (Ctrl+C twice to stop.)"
        Start-Sleep -Seconds 60
        continue
    }

    if (-not (Has-DashboardLogin)) {
        Write-Host "WARNING: DASHBOARD_USERNAME/DASHBOARD_PASSWORD are not both set in .env." -ForegroundColor Red
        Write-Host "Anyone on your tailnet could reach this dashboard with no login. Add both, e.g.:" -ForegroundColor Red
        Write-Host "  DASHBOARD_USERNAME=your-name"
        Write-Host "  DASHBOARD_PASSWORD=$(([guid]::NewGuid()).ToString('N'))"
        Write-Host "Retrying in 60 seconds. (Ctrl+C twice to stop.)"
        Start-Sleep -Seconds 60
        continue
    }

    if (-not $loggedSetupOnce) {
        Write-Host "Pointing tailscale serve at http://127.0.0.1:$Port ..." -ForegroundColor Cyan
        & $tailscaleExe serve --bg "http://127.0.0.1:$Port" | Out-Null
        $serveStatus = & $tailscaleExe serve status 2>&1
        Write-Host $serveStatus

        $urlLine = $serveStatus | Select-String -Pattern "https://\S+\.ts\.net" | Select-Object -First 1
        if ($urlLine) {
            $hostname = ([regex]::Match($urlLine, "https://([^/\s]+)")).Groups[1].Value
            Write-Host ""
            Write-Host "Dashboard will be reachable at: https://$hostname/" -ForegroundColor Green
            $envPath = Join-Path $RepoRoot ".env"
            if (Test-Path $envPath) {
                $envText = Get-Content $envPath -Raw
                if ($envText -notmatch [regex]::Escape("DASHBOARD_ALLOWED_HOSTS=$hostname")) {
                    Write-Host "NOTE: add this to .env so the app accepts that hostname, then restart:" -ForegroundColor Yellow
                    Write-Host "  DASHBOARD_ALLOWED_HOSTS=$hostname"
                }
            }
        }
        Write-Host ""
        Write-Host "Starting the dashboard (Ctrl+C twice to stop; Tailscale serve keeps running)..." -ForegroundColor Cyan
        $loggedSetupOnce = $true
    }

    python main.py ui --no-browser --port $Port
    Write-Host "Dashboard process exited (code $LASTEXITCODE). Restarting in 5 seconds..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
