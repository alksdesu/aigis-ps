@echo off
setlocal
if exist "%~dp0AigisConsole.exe" (
    start "" "%~dp0AigisConsole.exe"
    exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-console.ps1"
if errorlevel 1 (
    echo.
    echo The console did not start. Run install.cmd first.
    pause
    exit /b 1
)
