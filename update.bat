@echo off
setlocal EnableDelayedExpansion

echo ========================================
echo         Updating Input Reply
echo ========================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% NEQ 0 (
    echo.
    echo [X] Update failed with error code %EXIT_CODE%.
    echo Please review the error messages above.
    pause
    exit /b %EXIT_CODE%
)

echo.
pause
