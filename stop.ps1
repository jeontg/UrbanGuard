# UrbanGuard 개발 서버 종료
#
#   .\stop.ps1              # 웹 서비스만 종료 (PostgreSQL은 유지)
#   .\stop.ps1 -All         # PostgreSQL까지 함께 종료
#   .\stop.ps1 -Port 8080   # 다른 포트로 띄운 경우
#   .\stop.ps1 -Status      # 종료하지 않고 상태만 확인
#
# 포트를 점유한 프로세스를 무조건 죽이지 않습니다. 그 프로세스가 이 프로젝트의
# .venv 파이썬인지 먼저 확인합니다 — 같은 포트를 쓰는 남의 프로그램을 끄면
# 안 되기 때문입니다.

param(
    [int]$Port = 8000,
    [switch]$All,
    [switch]$Status
)

$ErrorActionPreference = 'Continue'

$Root    = $PSScriptRoot
$PgBin   = Join-Path $Root '.tools\pgsql\bin'
$PgData  = Join-Path $Root '.tools\pgdata'
$Py      = Join-Path $Root '.venv\Scripts\python.exe'
$PidFile = Join-Path $Root '.tools\urbanguard.pid'

function Write-Step($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "  $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "  $m" -ForegroundColor Red }

function Test-IsUrbanGuard {
    <#
      해당 PID 가 우리 서비스인지 **명령줄**로 판정한다.

      실행 파일 경로로는 판정할 수 없다. venv 의 python.exe 는 기본 파이썬을
      다시 실행하는 런처라, 뜬 프로세스의 Path 가 venv 가 아니라 시스템
      파이썬(C:\...\Python312\python.exe)으로 보고되기 때문이다.
      명령줄에는 uvicorn 대상 모듈이 그대로 남으므로 이쪽이 확실하다.
    #>
    param([int]$ProcessId)
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $cim) { return $false }
    return ($cim.CommandLine -like '*tot_dashboard.service.main*')
}

function Get-WebProcess {
    <#
      우리 서버 프로세스를 찾는다. PID 파일을 먼저 보고, 없으면 포트로 찾는다.
      어느 쪽이든 우리 서비스가 맞는지 확인한 뒤에만 돌려준다 — 같은 포트를
      쓰는 남의 프로그램을 끄면 안 되기 때문이다.
    #>
    $ids = @()
    if (Test-Path $PidFile) {
        $savedPid = (Get-Content $PidFile -Raw).Trim()
        if ($savedPid -match '^\d+$') { $ids += [int]$savedPid }
    }
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) { $ids += [int]$c.OwningProcess }

    $ids | Sort-Object -Unique | Where-Object { Test-IsUrbanGuard -ProcessId $_ } |
        ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue } |
        Where-Object { $_ }
}

Write-Host ''
Write-Host 'UrbanGuard 개발 서버 종료' -ForegroundColor White
Write-Host '----------------------------------------'

# --- 상태 확인 --------------------------------------------------------------
$web = @(Get-WebProcess)
& (Join-Path $PgBin 'pg_isready.exe') -h 127.0.0.1 -p 5433 | Out-Null
$pgUp = ($LASTEXITCODE -eq 0)

if ($Status) {
    if ($web.Count -gt 0) {
        Write-Ok "웹 서비스   실행 중 (PID $($web.Id -join ', '), 포트 $Port)"
    } else {
        Write-Host "  웹 서비스   종료됨 (포트 $Port)" -ForegroundColor Gray
    }
    if ($pgUp) { Write-Ok 'PostgreSQL  실행 중 (127.0.0.1:5433)' }
    else { Write-Host '  PostgreSQL  종료됨' -ForegroundColor Gray }
    Write-Host ''
    exit 0
}

# --- 웹 서비스 --------------------------------------------------------------
if ($web.Count -gt 0) {
    foreach ($p in $web) {
        Write-Step "웹 서비스 종료 중 (PID $($p.Id))..."
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    if (@(Get-WebProcess).Count -eq 0) {
        Write-Ok "웹 서비스 종료됨 (포트 $Port)"
    } else {
        Write-Err '[오류] 웹 서비스를 종료하지 못했습니다.'
    }
} else {
    $other = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($other) {
        Write-Warn2 "[주의] 포트 $Port 를 다른 프로그램이 쓰고 있습니다 (PID $($other.OwningProcess -join ', '))."
        Write-Host  '         UrbanGuard 프로세스가 아니므로 종료하지 않았습니다.'
    } else {
        Write-Host "  웹 서비스는 이미 종료된 상태입니다 (포트 $Port)." -ForegroundColor Gray
    }
}
if (Test-Path $PidFile) { Remove-Item $PidFile -Force }

# --- PostgreSQL -------------------------------------------------------------
if ($All) {
    if ($pgUp) {
        Write-Step 'PostgreSQL 종료 중...'
        & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData stop -m fast | Out-Null
        Start-Sleep -Seconds 1
        & (Join-Path $PgBin 'pg_isready.exe') -h 127.0.0.1 -p 5433 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Ok 'PostgreSQL 종료됨' }
        else { Write-Err '[오류] PostgreSQL을 종료하지 못했습니다.' }
    } else {
        Write-Host '  PostgreSQL은 이미 종료된 상태입니다.' -ForegroundColor Gray
    }
} elseif ($pgUp) {
    Write-Host ''
    Write-Host '  PostgreSQL은 계속 실행 중입니다.' -ForegroundColor DarkGray
    Write-Host '  함께 내리려면:  .\stop.ps1 -All' -ForegroundColor DarkGray
}

Write-Host ''
