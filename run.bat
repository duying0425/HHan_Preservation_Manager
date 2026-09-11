@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" (
    where pwsh.exe >nul 2>&1
    if not errorlevel 1 (
        set "PS_EXE=pwsh.exe"
    ) else (
        echo [ERROR] PowerShell not found.
        echo Please install/enable Windows PowerShell or PowerShell 7.
        exit /b 127
    )
)

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Task failed with exit code: %EXIT_CODE%
    echo.
    echo Diagnostic commands:
    echo   py -3 --version
    echo   python --version
    timeout /t 8 /nobreak >nul
) else (
    timeout /t 3 /nobreak >nul
)

exit /b %EXIT_CODE%
