@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
if %errorlevel% neq 0 (
    echo.
    echo Task failed with exit code: %errorlevel%
    ping 127.0.0.1 -n 6 >nul
) else (
    ping 127.0.0.1 -n 4 >nul
)
