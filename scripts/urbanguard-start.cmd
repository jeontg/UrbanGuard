@echo off
REM ===========================================================================
REM Start UrbanGuard through the scheduled task (or directly if not installed).
REM
REM Run from cmd.exe, from PowerShell, or by double-clicking in Explorer.
REM Waits until the port actually accepts connections before reporting, so a
REM slow model load is not mistaken for a successful start.
REM
REM ASCII only on purpose: cmd.exe parses .cmd files in the console code page.
REM ===========================================================================
setlocal

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8033"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\urbanguard-service.ps1" -Action start -Port %PORT%
set "RC=%ERRORLEVEL%"

echo %CMDCMDLINE% | find /i "%~nx0" >nul
if not errorlevel 1 (
  echo.
  echo Press any key to close...
  pause >nul
)
exit /b %RC%
