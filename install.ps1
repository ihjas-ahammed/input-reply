# Install Input Reply for the current user (Windows), register it to start at login, and launch it.
$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "       Installing Input Reply           " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $here) { $here = (Get-Location).Path }
$data = Join-Path $env:LOCALAPPDATA "InputReply"
$venv = Join-Path $data "venv"

# Step 1: Checking Python
Write-Host "`n[1/6] Checking Python installation..." -ForegroundColor Yellow
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
Write-Host "`n[2/6] Setting up virtual environment at $venv..." -ForegroundColor Yellow
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
$app = Join-Path $venv "Scripts\input-reply-app.exe"
$cli = Join-Path $venv "Scripts\input-reply.exe"

# Step 3: Upgrading pip
Write-Host "`n[3/6] Upgrading pip in virtual environment..." -ForegroundColor Yellow
& $python -m pip install --quiet --upgrade pip
Write-Host "  pip is up to date." -ForegroundColor Gray

# Step 4: Installing Input Reply
Write-Host "`n[4/6] Installing Input Reply and dependencies..." -ForegroundColor Yellow
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

# Step 5: Start Menu shortcut
Write-Host "`n[5/6] Creating Start Menu shortcut..." -ForegroundColor Yellow
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

# Step 6: Setup autostart & launch service
Write-Host "`n[6/6] Configuring startup and launching Input Reply service..." -ForegroundColor Yellow
if (Test-Path $cli) {
    & $cli setup
} else {
    & $python -m input_reply setup
}

Write-Host "`n========================================================" -ForegroundColor Green
Write-Host " [OK] Input Reply successfully installed and running!   " -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "Open the desktop app from the Start Menu, or run:" -ForegroundColor White
Write-Host "  $cli app" -ForegroundColor Cyan
Write-Host "Sign in to sync recordings and remote control:" -ForegroundColor White
Write-Host "  $cli login" -ForegroundColor Cyan
Write-Host ""
