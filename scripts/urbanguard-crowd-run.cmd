@echo off
REM ===========================================================================
REM UrbanGuard 인파관리 서비스(crowd-service) 실행 — API 게이트웨이 Phase 1
REM (2026-08-31, service/crowd_service.py). urbanguard-run.cmd(platform-shell)
REM 과 같은 구조지만, PostgreSQL·MediaMTX 확인은 하지 않는다 — 이 스크립트는
REM urbanguard-service.ps1이 그것들을 이미 확인·기동한 뒤에만 부른다(중복
REM 확인은 무해하지만 불필요하다).
REM
REM Usage:  urbanguard-crowd-run.cmd [port]      (default 8034)
REM ===========================================================================
setlocal

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8034"

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [urbanguard-crowd-run] venv python not found: "%PY%"
  exit /b 9
)

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

cd /d "%ROOT%"
"%PY%" "%ROOT%\scripts\serve.py" --app tot_dashboard.service.crowd_service:app ^
    --label serve-crowd --host 127.0.0.1 --port %PORT% ^
    >> "%ROOT%\data\logs\serve-crowd-console.log" 2>&1
exit /b %ERRORLEVEL%
