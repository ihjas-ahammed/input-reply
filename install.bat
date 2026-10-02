@echo off
setlocal EnableDelayedExpansion

echo ========================================
echo        Installing Input Reply
echo ========================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% NEQ 0 (
    echo.
    echo [X] Installation failed with error code %EXIT_CODE%.
    echo Please check the error messages above.
    pause
    exit /b %EXIT_CODE%
)

echo.
pause
