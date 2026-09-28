<#
.SYNOPSIS
  Registers the JobAgentDashboard scheduled task, so the dashboard (and its
  Tailscale tunnel) survive a reboot, a crash, and long stretches of sleep
  without you having to notice and restart it by hand.

.DESCRIPTION
  Runs scripts/start_dashboard.ps1 (which has its own internal retry loop --
  see that file's header) under two triggers:
    - At logon: the normal case.
    - Every 15 minutes, indefinitely: because a laptop that mostly sleeps
      rather than fully logging out/in may not trigger "At logon" again for
      days, and because Windows Task Scheduler's own "restart if the task
      fails" setting was tested against this task and found unreliable for
      logon-triggered tasks (it did not restart a killed process even after
      minutes, with RestartCount/RestartInterval configured correctly). The
      15-minute trigger is a safety net, not the primary mechanism -- if an
      instance is already running, MultipleInstances=IgnoreNew makes each
      firing a no-op; it only matters when nothing is running at all, in
      which case it caps the worst-case downtime at ~15 minutes instead of
      "until you notice and start it yourself."

  Safe to re-run: it replaces the existing task definition rather than
  erroring if JobAgentDashboard already exists.

.EXAMPLE
  .\scripts\install_autostart.ps1
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\start_dashboard.ps1"

if (-not (Test-Path $ScriptPath)) {
    Write-Host "Could not find $ScriptPath -- run this from a checkout of the repository." -ForegroundColor Red
    exit 1
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$HeartbeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "JobAgentDashboard" -Action $Action `
    -Trigger @($LogonTrigger, $HeartbeatTrigger) -Settings $Settings -Principal $Principal -Force | Out-Null

Write-Host "Registered the JobAgentDashboard scheduled task (logon + every 15 minutes)." -ForegroundColor Green
Write-Host "Starting it now..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName "JobAgentDashboard"
Start-Sleep -Seconds 5

$info = Get-ScheduledTaskInfo -TaskName "JobAgentDashboard"
Write-Host "Last result: $($info.LastTaskResult)  Next scheduled check: $($info.NextRunTime)"
Write-Host ""
Write-Host "To remove this: Unregister-ScheduledTask -TaskName JobAgentDashboard -Confirm:`$false" -ForegroundColor DarkGray
