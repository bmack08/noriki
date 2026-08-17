<#
    Start the Noriki relay and keep it up.

        .\run-relay.ps1

    To have it start automatically at login (no admin needed):

        .\run-relay.ps1 -Install

    To stop it starting automatically:

        .\run-relay.ps1 -Uninstall
#>

param(
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "Noriki Relay"

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed the '$taskName' logon task." -ForegroundColor Green
    } catch {
        Write-Host "No '$taskName' task was registered." -ForegroundColor Yellow
    }
    return
}

if ($Install) {
    $pwsh = (Get-Command powershell).Source
    $action = New-ScheduledTaskAction -Execute $pwsh `
        -Argument "-NoProfile -WindowStyle Hidden -File `"$here\run-relay.ps1`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 5) `
        -RestartCount 999

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Bridges the Noriki phone app to Claude Code on this PC." -Force | Out-Null

    Write-Host "Registered '$taskName' — the relay now starts at logon." -ForegroundColor Green
    Write-Host "Starting it once now..." -ForegroundColor Gray
}

$config = Join-Path $here "config.json"
if (-not (Test-Path $config)) {
    Write-Host "No config.json found." -ForegroundColor Red
    Write-Host "Copy config.example.json to config.json and edit the paths first." -ForegroundColor Yellow
    exit 1
}

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { Write-Host "python is not on PATH." -ForegroundColor Red; exit 1 }

Write-Host "Noriki starting. Ctrl+C to stop." -ForegroundColor Cyan

# The ask-bridge runs alongside the relay: the relay carries your messages to
# the PC, the bridge carries OVERSEER's questions back to your phone.
$bridge = Start-Process -FilePath $python.Source `
    -ArgumentList @((Join-Path $here "noriki_ask_bridge.py"), "--config", $config) `
    -WorkingDirectory $here -PassThru -WindowStyle Hidden

Write-Host "  ask-bridge running (pid $($bridge.Id))" -ForegroundColor Gray

try {
    & $python.Source (Join-Path $here "noriki_relay.py") --config $config
} finally {
    if ($bridge -and -not $bridge.HasExited) {
        Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue
        Write-Host "`n  ask-bridge stopped" -ForegroundColor Gray
    }
}
