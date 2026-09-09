@echo off
REM ===========================================================================
REM UrbanGuard 침수 서비스(flood-service) 실행 — API 게이트웨이 Phase 4
REM (2026-08-31, service/flood_service.py). urbanguard-road-run.cmd와 같은
REM 구조 — PostgreSQL·MediaMTX 확인은 하지 않는다(urbanguard-service.ps1이
REM 이미 확인·기동한 뒤에만 부른다).
REM
REM Usage:  urbanguard-flood-run.cmd [port]      (default 8036)
REM ===========================================================================
setlocal

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8036"

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [urbanguard-flood-run] venv python not found: "%PY%"
  exit /b 9
)

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

cd /d "%ROOT%"
"%PY%" "%ROOT%\scripts\serve.py" --app tot_dashboard.service.flood_service:app ^
    --label serve-flood --host 127.0.0.1 --port %PORT% ^
    >> "%ROOT%\data\logs\serve-flood-console.log" 2>&1
exit /b %ERRORLEVEL%
