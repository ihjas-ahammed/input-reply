# Install Input Reply for the current user (Windows), register it to start at login, and launch it.
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $env:LOCALAPPDATA "InputReply"
$venv = Join-Path $data "venv"

py -3 -m venv $venv
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet "$here[desktop]"
if ($LASTEXITCODE -ne 0) { throw "Installation failed" }

$app = Join-Path $venv "Scripts\input-reply-app.exe"
$cli = Join-Path $venv "Scripts\input-reply.exe"

# Start Menu shortcut
$programs = [Environment]::GetFolderPath("Programs")
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $programs "Input Reply.lnk"))
$link.TargetPath = $app
$link.Save()

& $cli setup
Write-Host "Installed. Sign in from the app (Account), or run: $cli login"
