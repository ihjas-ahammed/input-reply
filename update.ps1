# Update Input Reply to the latest version, refresh dependencies, and restart the service.
$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "         Updating Input Reply           " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $here) { $here = (Get-Location).Path }
$data = Join-Path $env:LOCALAPPDATA "InputReply"
$venv = Join-Path $data "venv"
$python = Join-Path $venv "Scripts\python.exe"
$app = Join-Path $venv "Scripts\input-reply-app.exe"
$cli = Join-Path $venv "Scripts\input-reply.exe"

# Step 1: Git pull if running inside git repository
Write-Host "`n[1/6] Checking repository status..." -ForegroundColor Yellow
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
Write-Host "`n[2/6] Verifying installed virtual environment..." -ForegroundColor Yellow
if (-not (Test-Path $python)) {
    Write-Warning "  Virtual environment not found at $venv. Running full installer..."
    & (Join-Path $here "install.ps1")
    exit 0
}
Write-Host "  Found virtual environment at: $venv" -ForegroundColor Gray

# Step 3: Stop Running Service
Write-Host "`n[3/6] Stopping running Input Reply service..." -ForegroundColor Yellow
try {
    $conns = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($c in $conns) {
            if ($c.OwningProcess -gt 0) {
                Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
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
Write-Host "`n[4/6] Upgrading pip and updating Input Reply package..." -ForegroundColor Yellow
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
Write-Host "`n[5/6] Refreshing Start Menu shortcut..." -ForegroundColor Yellow
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

# Step 6: Restart Service
Write-Host "`n[6/6] Launching updated Input Reply service..." -ForegroundColor Yellow
if (Test-Path $cli) {
    & $cli setup
} else {
    & $python -m input_reply setup
}

Write-Host "`n========================================================" -ForegroundColor Green
Write-Host " [OK] Input Reply successfully updated to latest version!" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "Service is active on port 8765. Open the app with:" -ForegroundColor White
Write-Host "  $cli app" -ForegroundColor Cyan
Write-Host ""
