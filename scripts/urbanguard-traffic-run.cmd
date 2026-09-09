@echo off
REM ===========================================================================
REM UrbanGuard 교통위험 서비스(traffic-service) 실행 — API 게이트웨이 Phase 4
REM (2026-08-31, service/traffic_service.py). urbanguard-road-run.cmd와 같은
REM 구조 — PostgreSQL·MediaMTX 확인은 하지 않는다(urbanguard-service.ps1이
REM 이미 확인·기동한 뒤에만 부른다).
REM
REM Usage:  urbanguard-traffic-run.cmd [port]      (default 8037)
REM ===========================================================================
setlocal

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8037"

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [urbanguard-traffic-run] venv python not found: "%PY%"
  exit /b 9
)

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

cd /d "%ROOT%"
"%PY%" "%ROOT%\scripts\serve.py" --app tot_dashboard.service.traffic_service:app ^
    --label serve-traffic --host 127.0.0.1 --port %PORT% ^
    >> "%ROOT%\data\logs\serve-traffic-console.log" 2>&1
exit /b %ERRORLEVEL%
