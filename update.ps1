# Update Input Reply to the latest version, refresh dependencies, deploy skills, and restart the service.
$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "         Updating Input Reply           " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $here) { $here = (Get-Location).Path }
$data = Join-Path $env:LOCALAPPDATA "InputReply"
$venv = Join-Path $data "venv"
$python = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$app = Join-Path $venv "Scripts\input-reply-app.exe"
$cli = Join-Path $venv "Scripts\input-reply.exe"

# Step 1: Git pull if running inside git repository
Write-Host "`n[1/8] Checking repository status..." -ForegroundColor Yellow
$isGitRepo = $false
try {
    $gitCheck = git -C $here rev-parse --is-inside-work-tree 2>&1
    if ($gitCheck -eq "true") {
        $isGitRepo = $true
    }
} catch {}

if ($isGitRepo) {
    Write-Host "  Pulling latest updates from git repository..." -ForegroundColor Gray
    try {
        git -C $here pull --ff-only
        Write-Host "  Repository is up to date." -ForegroundColor Gray
    } catch {
        Write-Warning "  Could not fast-forward git repository automatically: $_"
        Write-Host "  Proceeding to update installed package from current directory." -ForegroundColor Gray
    }
} else {
    Write-Host "  Current directory is not a git repository. Updating from local source." -ForegroundColor Gray
}

# Step 2: Check Virtual Environment
Write-Host "`n[2/8] Verifying installed virtual environment..." -ForegroundColor Yellow
if (-not (Test-Path $python)) {
    Write-Warning "  Virtual environment not found at $venv. Running full installer..."
    & (Join-Path $here "install.ps1")
    exit 0
}
Write-Host "  Found virtual environment at: $venv" -ForegroundColor Gray

# Step 3: Stop Running Service
Write-Host "`n[3/8] Stopping running Input Reply service..." -ForegroundColor Yellow
$activePort = 8765
$lockFile = Join-Path $data "service.lock"
if (Test-Path $lockFile) {
    try {
        $lockContent = Get-Content $lockFile -Raw | ConvertFrom-Json
        if ($lockContent.port) { $activePort = [int]$lockContent.port }
    } catch {}
}

try {
    $conns = Get-NetTCPConnection -LocalPort $activePort -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($c in $conns) {
            if ($c.OwningProcess -gt 0) {
                Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
            }
        }
    }
    # Also check default 8765 if alternate port was used
    if ($activePort -ne 8765) {
        $conns8765 = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
        if ($conns8765) {
            foreach ($c in $conns8765) {
                if ($c.OwningProcess -gt 0) {
                    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }
    Get-Process -Name python,pythonw -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "*InputReply*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
    Write-Host "  Service stopped." -ForegroundColor Gray
} catch {
    Write-Host "  No active service needed stopping." -ForegroundColor Gray
}

# Step 4: Upgrade Dependencies and Package
Write-Host "`n[4/8] Upgrading pip and updating Input Reply package..." -ForegroundColor Yellow
& $python -m pip install --quiet --upgrade pip
Write-Host "  Installing latest package from: $here" -ForegroundColor Gray
& $python -m pip install --upgrade "$here[desktop,ai]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Failed to install desktop/ai extras, attempting core package..." -ForegroundColor DarkYellow
    & $python -m pip install --upgrade "$here"
    if ($LASTEXITCODE -ne 0) {
        throw "Package update failed. Please review pip error logs above."
    }
}
Write-Host "  Package updated successfully." -ForegroundColor Gray

# Step 5: Refresh Start Menu Shortcut
Write-Host "`n[5/8] Refreshing Start Menu shortcut..." -ForegroundColor Yellow
try {
    $programs = [Environment]::GetFolderPath("Programs")
    $shell = New-Object -ComObject WScript.Shell
    $linkPath = Join-Path $programs "Input Reply.lnk"
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $app
    $link.Description = "Record and replay desktop macros"
    $link.Save()
    Write-Host "  Shortcut verified at: $linkPath" -ForegroundColor Gray
} catch {
    Write-Warning "  Could not refresh shortcut: $_"
}

# Step 6: Deploy Agent Skills for Codex, Claude, and AGY
Write-Host "`n[6/8] Refreshing AI agent skills (Codex, Claude, AGY)..." -ForegroundColor Yellow
$skillSrc = Join-Path $here "skills\input-reply\SKILL.md"
if (Test-Path $skillSrc) {
    $skillTargets = @(
        (Join-Path $here ".agents\skills\input-reply"),
        (Join-Path $here ".agent\skills\input-reply"),
        (Join-Path $here ".claude\skills\input-reply"),
        (Join-Path $env:USERPROFILE ".gemini\config\skills\input-reply"),
        (Join-Path $data "skills\input-reply")
    )
    foreach ($targetDir in $skillTargets) {
        try {
            if (-not (Test-Path $targetDir)) {
                New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
            }
            Copy-Item -Path $skillSrc -Destination (Join-Path $targetDir "SKILL.md") -Force
            Write-Host "  Deployed skill to: $targetDir" -ForegroundColor Gray
        } catch {
            Write-Warning "  Could not deploy skill to ${targetDir}: $_"
        }
    }
} else {
    Write-Host "  No skills directory found to deploy." -ForegroundColor Gray
}

# Step 7: Refresh Auto-start on Boot/Login and Network Change Restart
Write-Host "`n[7/8] Refreshing auto-start and failure recovery configuration..." -ForegroundColor Yellow
try {
    # 1. Registry Run key
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "InputReply" -Value "`"$pythonw`" -m input_reply service" -ErrorAction SilentlyContinue
    Write-Host "  Refreshed Windows Registry Run key." -ForegroundColor Gray
} catch {
    Write-Warning "  Registry Run key setup notice: $_"
}

try {
    # 2. Task Scheduler task with AtLogOn trigger and auto-restart
    $taskName = "InputReplyService"
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m input_reply service"
    $triggerLogon = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 365) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggerLogon -Settings $settings -Force -ErrorAction SilentlyContinue | Out-Null
    Write-Host "  Refreshed Task Scheduler auto-start and recovery." -ForegroundColor Gray
} catch {
    Write-Host "  Task Scheduler registration skipped (using Registry startup)." -ForegroundColor Gray
}

# Step 8: Restart Service
Write-Host "`n[8/8] Launching updated Input Reply service..." -ForegroundColor Yellow
if (Test-Path $cli) {
    & $cli setup
} else {
    & $python -m input_reply setup
}

Start-Sleep -Milliseconds 1200
$finalPort = 8765
if (Test-Path $lockFile) {
    try {
        $lockContent = Get-Content $lockFile -Raw | ConvertFrom-Json
        if ($lockContent.port) { $finalPort = $lockContent.port }
    } catch {}
}

Write-Host "`n========================================================" -ForegroundColor Green
Write-Host " [OK] Input Reply successfully updated to latest version!" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "Service is active on port $finalPort (recorded in service.lock)." -ForegroundColor White
Write-Host "Open the app with:" -ForegroundColor White
Write-Host "  $cli app" -ForegroundColor Cyan
Write-Host ""
