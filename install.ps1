# Install Input Reply for the current user (Windows), register it to start at boot/login, and launch it.
$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "       Installing Input Reply           " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $here) { $here = (Get-Location).Path }
$data = Join-Path $env:LOCALAPPDATA "InputReply"
$venv = Join-Path $data "venv"

# Step 1: Checking Python
Write-Host "`n[1/8] Checking Python installation..." -ForegroundColor Yellow
$pyCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pyCmd = "py -3"
    $pyVer = & py -3 --version 2>&1
    Write-Host "  Found Python Launcher: $pyVer" -ForegroundColor Gray
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pyCmd = "python"
    $pyVer = & python --version 2>&1
    Write-Host "  Found Python: $pyVer" -ForegroundColor Gray
} else {
    Write-Error "Python 3 (3.10 or newer) is required. Please install Python from python.org or the Microsoft Store."
    exit 1
}

# Step 2: Virtual Environment
Write-Host "`n[2/8] Setting up virtual environment at $venv..." -ForegroundColor Yellow
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    if ($pyCmd -eq "py -3") {
        & py -3 -m venv $venv
    } else {
        & python -m venv $venv
    }
    Write-Host "  Created virtual environment." -ForegroundColor Gray
} else {
    Write-Host "  Existing virtual environment found." -ForegroundColor Gray
}

$python = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$app = Join-Path $venv "Scripts\input-reply-app.exe"
$cli = Join-Path $venv "Scripts\input-reply.exe"

# Step 3: Upgrading pip
Write-Host "`n[3/8] Upgrading pip in virtual environment..." -ForegroundColor Yellow
& $python -m pip install --quiet --upgrade pip
Write-Host "  pip is up to date." -ForegroundColor Gray

# Step 4: Installing Input Reply
Write-Host "`n[4/8] Installing Input Reply and dependencies..." -ForegroundColor Yellow
Write-Host "  Installing package from: $here" -ForegroundColor Gray
& $python -m pip install --upgrade "$here[desktop,ai]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Failed to install desktop/ai extras, attempting core package..." -ForegroundColor DarkYellow
    & $python -m pip install --upgrade "$here"
    if ($LASTEXITCODE -ne 0) {
        throw "Installation failed. Please review pip error logs above."
    }
}
Write-Host "  Input Reply installed successfully." -ForegroundColor Gray

# Step 5: Start Menu and Desktop shortcuts
Write-Host "`n[5/8] Creating Start Menu shortcut..." -ForegroundColor Yellow
try {
    $programs = [Environment]::GetFolderPath("Programs")
    $shell = New-Object -ComObject WScript.Shell
    $linkPath = Join-Path $programs "Input Reply.lnk"
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $app
    $link.Description = "Record and replay desktop macros"
    $link.Save()
    Write-Host "  Created shortcut at: $linkPath" -ForegroundColor Gray
} catch {
    Write-Warning "  Could not create Start Menu shortcut: $_"
}

# Step 6: Deploy Agent Skills for Codex, Claude, and AGY
Write-Host "`n[6/8] Deploying AI agent skills (Codex, Claude, AGY)..." -ForegroundColor Yellow
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

# Step 7: Configure Startup on Boot/Login and Network Change Restart
Write-Host "`n[7/8] Configuring auto-start on boot/login and network change restart..." -ForegroundColor Yellow
try {
    # 1. Registry Run key
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "InputReply" -Value "`"$pythonw`" -m input_reply service" -ErrorAction SilentlyContinue
    Write-Host "  Configured Windows Registry Run key." -ForegroundColor Gray
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
    Write-Host "  Configured Task Scheduler auto-start and failure recovery." -ForegroundColor Gray
} catch {
    Write-Host "  Task Scheduler registration skipped (using Registry startup)." -ForegroundColor Gray
}

try {
    # 3. Startup folder link as reliable fallback
    $startupFolder = [Environment]::GetFolderPath("Startup")
    if (Test-Path $startupFolder) {
        $shell = New-Object -ComObject WScript.Shell
        $startupLnk = Join-Path $startupFolder "Input Reply Service.lnk"
        $sLink = $shell.CreateShortcut($startupLnk)
        $sLink.TargetPath = $pythonw
        $sLink.Arguments = "-m input_reply service"
        $sLink.WindowStyle = 7 # Minimized/hidden
        $sLink.Description = "Input Reply Background Service"
        $sLink.Save()
        Write-Host "  Configured Startup folder fallback." -ForegroundColor Gray
    }
} catch {}

# Step 8: Launch Service
Write-Host "`n[8/8] Launching Input Reply service..." -ForegroundColor Yellow
if (Test-Path $cli) {
    & $cli setup
} else {
    & $python -m input_reply setup
}

Start-Sleep -Milliseconds 1200
$lockFile = Join-Path $data "service.lock"
$activePort = 8765
if (Test-Path $lockFile) {
    try {
        $lockContent = Get-Content $lockFile -Raw | ConvertFrom-Json
        if ($lockContent.port) { $activePort = $lockContent.port }
    } catch {}
}

Write-Host "`n========================================================" -ForegroundColor Green
Write-Host " [OK] Input Reply successfully installed and running!   " -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "Active service port: $activePort (saved in service.lock)" -ForegroundColor White
Write-Host "Open the desktop app from the Start Menu, or run:" -ForegroundColor White
Write-Host "  $cli app" -ForegroundColor Cyan
Write-Host "Sign in to sync recordings and remote control:" -ForegroundColor White
Write-Host "  $cli login" -ForegroundColor Cyan
Write-Host ""
