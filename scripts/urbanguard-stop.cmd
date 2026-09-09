@echo off
REM ===========================================================================
REM Stop UrbanGuard: ends the scheduled task AND kills the processes.
REM
REM Run from cmd.exe, from PowerShell, or by double-clicking in Explorer.
REM
REM Killing only the uvicorn child is NOT enough -- the supervisor
REM (scripts\serve.py) brings it back after 5 seconds. The PowerShell script
REM ends the supervisor first, then sweeps leftovers, then verifies the port
REM is actually free before reporting success.
REM
REM ASCII only on purpose: cmd.exe parses .cmd files in the console code page.
REM ===========================================================================
setlocal

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8033"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\urbanguard-service.ps1" -Action stop -Port %PORT%
set "RC=%ERRORLEVEL%"

REM Pause only when double-clicked from Explorer, never when called from a
REM script -- a blocking pause in automation looks exactly like a hang.
echo %CMDCMDLINE% | find /i "%~nx0" >nul
if not errorlevel 1 (
  echo.
  echo Press any key to close...
  pause >nul
)
exit /b %RC%
