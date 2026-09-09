@echo off
REM ===========================================================================
REM UrbanGuard service launcher.
REM
REM The scheduled task runs THIS file, and a person can run it directly too.
REM Keeping both on one path means the task and the operator start the service
REM in exactly the same way -- no "works by hand, fails as a task".
REM
REM Output is redirected to a file on purpose. The task runs hidden, so there
REM is no console to print to; without this the child dies and the reason is
REM lost.
REM
REM NOTE: this file is intentionally ASCII only. cmd.exe reads .cmd files in
REM the console code page (cp949 here), so non-ASCII text can break parsing.
REM Korean documentation lives in scripts\urbanguard-service.ps1.
REM
REM Usage:  urbanguard-run.cmd [port]      (default 8033)
REM ===========================================================================
setlocal

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8033"

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [urbanguard-run] venv python not found: "%PY%"
  exit /b 9
)

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

REM PostgreSQL is not registered as a Windows service (see docs\pending_tasks.md
REM item 1-1). It can vanish after console close, sleep/resume, or reboot, and
REM once that happens every request just times out -- the app process looks
REM alive while nothing works. Check/start it here so BOTH launch paths (the
REM scheduled task and a person running urbanguard-start.cmd) are covered
REM before serve.py ever touches the database.
REM
REM IMPORTANT: this writes to its OWN log file, not serve-console.log, even
REM though an operator would rather check one file. Reason (found the hard
REM way, 2026-08-21): when this step actually has to start PostgreSQL, pg_ctl
REM spawns postgres.exe from inside this redirected command -- and on
REM Windows, a spawned child inherits ALL of its parent's inheritable handles,
REM not just stdin/stdout/stderr. postgres.exe (and it stays running for the
REM life of the service) ends up holding a stray open handle to whatever file
REM THIS line was redirected to, permanently blocking any later ">>" to that
REM same file from any other process. If that file were serve-console.log,
REM the very next line below (serve.py's own redirect) would fail every time
REM PostgreSQL had to be freshly started -- "The process cannot access the
REM file because it is being used by another process" -- and serve.py would
REM silently never launch. A dedicated file confines the stray handle to
REM something nobody else needs to append to.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\ensure-postgres.ps1" >> "%ROOT%\data\logs\ensure-postgres-console.log" 2>&1
if errorlevel 1 (
  echo [urbanguard-run] PostgreSQL did not come up - see data\logs\ensure-postgres-console.log and data\logs\pg-console.log >> "%ROOT%\data\logs\serve-console.log"
  exit /b 10
)

REM CCTV re-stream hub (MediaMTX). The 4 detection domains route through this
REM one process instead of each opening its own connection to the origin
REM CCTV, avoiding the "3rd concurrent connection refused" failure (see
REM road/live_analyzer.py comment). Same class of problem as PostgreSQL
REM above: it is not a Windows service, so it can vanish after sleep/resume
REM or reboot -- found the hard way on 2026-08-29, when this very scheduled
REM task restarted the app three times (18:17, 20:58, 06:58) while MediaMTX
REM stayed dead the whole time, because nothing on this auto-restart path
REM ever checked it (only the manual "urbanguard-service.ps1 -Action start"
REM path did). The road domain kept failing with a generic connection-refused
REM message that (wrongly, for this cause) blamed concurrent-connection
REM contention.
REM
REM Unlike PostgreSQL this step is NOT fatal: restream is an optional add-on
REM (core/settings.py restream.enabled defaults to off), so a failure here
REM only logs a warning and the app keeps starting -- it must never block on
REM this the way it blocks on PostgreSQL above.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\ensure-mediamtx.ps1" >> "%ROOT%\data\logs\ensure-mediamtx-console.log" 2>&1
if errorlevel 1 (
  echo [urbanguard-run] MediaMTX did not come up - restream stays unavailable until fixed - see data\logs\ensure-mediamtx-console.log and data\logs\mediamtx.log >> "%ROOT%\data\logs\serve-console.log"
)

REM --priority above_normal (2026-09-02, speed improvement step 1): this is
REM platform-shell -- the process the admin screen and health checks live on.
REM Under CPU saturation from the 4 domain services it was measured to slow
REM down far more than they do (up to 3s on a plain /api/health, see
REM docs/pending_tasks.md). Only THIS launcher passes the flag -- the 4
REM domain services stay at normal priority so they keep competing fairly
REM with each other.
cd /d "%ROOT%"
"%PY%" "%ROOT%\scripts\serve.py" --host 127.0.0.1 --port %PORT% --priority above_normal >> "%ROOT%\data\logs\serve-console.log" 2>&1
exit /b %ERRORLEVEL%
