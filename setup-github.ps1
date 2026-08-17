<#
    Finishes the GitHub side of Noriki in one run.

    You create three empty repos first (30 seconds each, in the browser —
    this script can't do it, since creating repos on your account needs the
    GitHub API and `gh` isn't installed here). Create them EMPTY: no README,
    no .gitignore, no licence, or the first push will be rejected.

      https://github.com/new

      1. noriki         PUBLIC   — code only, no data. Public so Pages is free.
      2. noriki-state   PRIVATE  — your tasks, chats, check-ins. Never public.
      3. overseer       PRIVATE  — your agent infrastructure.

    Then:

        .\setup-github.ps1

    It pushes all three, seeds the state repo, and tells you the two
    settings to flip.
#>

param(
    [string]$Owner = "bmack08"
)

# Deliberately NOT "Stop": every git call below is a native command, and in
# PowerShell 5.1 a native command writing to stderr raises a NativeCommandError
# that a Stop preference turns terminating — even on success. Exit codes are
# checked explicitly instead.
$ErrorActionPreference = "Continue"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$overseer = "C:\Users\Neurasthetic\Documents\overseer"

function Test-Repo($name) {
    $null = & git ls-remote --heads "https://github.com/$Owner/$name.git" 2>&1
    return ($LASTEXITCODE -eq 0)
}

function Add-Remote($path, $name) {
    $url = "https://github.com/$Owner/$name.git"

    # list remotes rather than asking for one that may not exist — `git remote
    # get-url` on a missing remote writes to stderr, which is the trap above
    $remotes = @(& git -C $path remote)
    if ($remotes -contains "origin") {
        $existing = (& git -C $path remote get-url origin | Select-Object -First 1)
        if ($existing -and $existing.Trim() -ne $url) {
            Write-Host "  origin already points at $existing - leaving it alone." -ForegroundColor Yellow
            return $false
        }
    } else {
        $null = & git -C $path remote add origin $url
    }
    return $true
}

function Invoke-Push($path, $label) {
    $null = & git -C $path branch -M main 2>&1
    & git -C $path push -u origin main 2>&1 | ForEach-Object { "    $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  push FAILED for $label (exit $LASTEXITCODE)" -ForegroundColor Red
        return $false
    }
    Write-Host "  pushed" -ForegroundColor Green
    return $true
}

# ---- 1. check the repos exist ------------------------------------------------

Write-Host "`nChecking repos..." -ForegroundColor Cyan
$missing = @()
foreach ($r in @("noriki", "noriki-state", "overseer")) {
    if (Test-Repo $r) {
        Write-Host "  $r  found" -ForegroundColor Green
    } else {
        Write-Host "  $r  MISSING" -ForegroundColor Red
        $missing += $r
    }
}

if ($missing.Count -gt 0) {
    Write-Host "`nCreate these at https://github.com/new before running again:" -ForegroundColor Yellow
    foreach ($m in $missing) {
        $vis = if ($m -eq "noriki") { "PUBLIC " } else { "PRIVATE" }
        Write-Host "    $vis  $Owner/$m" -ForegroundColor Yellow
    }
    Write-Host "`nCreate them EMPTY — no README, no .gitignore, no licence.`n" -ForegroundColor Gray
    exit 1
}

# ---- 2. push noriki ----------------------------------------------------------

Write-Host "`nPushing noriki..." -ForegroundColor Cyan
if (Add-Remote $here "noriki") {
    $null = Invoke-Push $here "noriki"
}

# ---- 3. push overseer --------------------------------------------------------

Write-Host "`nPushing overseer (the unbacked-up one)..." -ForegroundColor Cyan
if (Test-Path $overseer) {
    if (Add-Remote $overseer "overseer") {
        $null = Invoke-Push $overseer "overseer"
    }
} else {
    Write-Host "  not found at $overseer - skipped" -ForegroundColor Yellow
}

# ---- 4. seed the private state repo -----------------------------------------

Write-Host "`nSeeding noriki-state..." -ForegroundColor Cyan
$stateDir = Join-Path $here ".state"

if (-not (Test-Path (Join-Path $stateDir ".git"))) {
    if (Test-Path $stateDir) { Remove-Item $stateDir -Recurse -Force }
    $null = & git clone "https://github.com/$Owner/noriki-state.git" $stateDir 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  clone FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "inbox")  | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "outbox") | Out-Null

# .gitkeep so the directories survive a clone
foreach ($d in @("inbox", "outbox")) {
    $keep = Join-Path $stateDir "$d\.gitkeep"
    if (-not (Test-Path $keep)) { New-Item -ItemType File -Path $keep | Out-Null }
}

$readme = Join-Path $stateDir "README.md"
if (-not (Test-Path $readme)) {
@"
# noriki-state

Private. This repo is a database and a message bus, not a project.

| Path | Written by | What it is |
|---|---|---|
| ``state.json`` | phone | projects, the two-lane queue, check-ins, captures |
| ``inbox/`` | phone | messages waiting for the PC |
| ``outbox/`` | relay | replies waiting for the phone |
| ``relay-status.json`` | relay | heartbeat, so the phone knows the PC is listening |

Never make this public — it holds your task history and every conversation.
"@ | Set-Content -Path $readme -Encoding utf8
}

$null = & git -C $stateDir add -A
$dirty = @(& git -C $stateDir status --porcelain)
if ($dirty.Count -gt 0) {
    $null = & git -C $stateDir -c user.name="$Owner" -c user.email="bmccoy67@gmail.com" `
        commit -q -m "Seed the Noriki state repo" 2>&1
    $null = Invoke-Push $stateDir "noriki-state"
} else {
    Write-Host "  already seeded" -ForegroundColor Gray
}

# ---- 5. what's left, which only you can do ----------------------------------

Write-Host @"

Done. Three things left that need your account:

  1. Pages
     https://github.com/$Owner/noriki/settings/pages
     Source: Deploy from a branch -> main -> /docs -> Save
     (only root and /docs are offered; that is why the build lives in docs/)
     Your app lands at https://$Owner.github.io/noriki/

  2. A token for the phone
     https://github.com/settings/personal-access-tokens/new
     Repository access : ONLY $Owner/noriki-state
     Permissions       : Contents -> Read and write
     Expiration        : set one. 90 days.

  3. Start the relay
     cd relay
     copy config.example.json config.json
     .\run-relay.ps1 -Install

Then open the Pages URL on your phone, Add to Home Screen, and paste
$Owner/noriki-state plus the token.

"@ -ForegroundColor Cyan
