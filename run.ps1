# UrbanGuard 개발 서버 기동
#
#   .\run.ps1                   # 이 창에서 실행 (Ctrl+C 로 종료)
#   .\run.ps1 -Background       # 백그라운드로 실행 (.\stop.ps1 로 종료)
#   .\run.ps1 -Port 8080        # 포트 지정
#   .\run.ps1 -Reload           # 코드 수정 시 자동 재시작 (개발용)
#
# PostgreSQL이 꺼져 있으면 자동으로 켭니다. 로컬 개발용 인스턴스는
# .tools\pgsql 에 있으며, 시스템에 설치된 것이 아니라 이 폴더에만 존재합니다.

param(
    [int]$Port = 8000,
    [switch]$Background,
    [switch]$Reload
)

# 'Stop' 으로 두면 안 된다. PowerShell 5.1은 네이티브 exe 가 stderr 에 쓴 줄을
# ErrorRecord 로 감싸므로, alembic 의 INFO 로그만으로도 스크립트가 죽는다.
# 오류는 $LASTEXITCODE 로 직접 확인한다.
$ErrorActionPreference = 'Continue'

$Root    = $PSScriptRoot
$PgBin   = Join-Path $Root '.tools\pgsql\bin'
$PgData  = Join-Path $Root '.tools\pgdata'
$PgLog   = Join-Path $Root '.tools\pg.log'
$Py      = Join-Path $Root '.venv\Scripts\python.exe'
$PidFile = Join-Path $Root '.tools\urbanguard.pid'
$AppLog  = Join-Path $Root '.tools\urbanguard.log'

function Write-Step($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "  $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "  $m" -ForegroundColor Red }

Write-Host ''
Write-Host 'UrbanGuard 개발 서버 기동' -ForegroundColor White
Write-Host '----------------------------------------'

# --- 1. 사전 점검 -----------------------------------------------------------
if (-not (Test-Path $Py)) {
    Write-Err "[오류] 가상환경이 없습니다: $Py"
    Write-Host '         python -m venv .venv 후 pip install -e . 를 먼저 실행하세요.'
    exit 1
}
if (-not (Test-Path (Join-Path $Root '.env'))) {
    Write-Warn2 '[경고] .env 가 없습니다. DB 접속 정보가 기본값으로 동작합니다.'
}

# 이미 떠 있으면 두 번 띄우지 않는다.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Warn2 "[안내] 포트 $Port 가 이미 사용 중입니다."
    Write-Host  "         기존 서비스를 내리려면:  .\stop.ps1 -Port $Port"
    exit 1
}

# --- 2. PostgreSQL ----------------------------------------------------------
& (Join-Path $PgBin 'pg_isready.exe') -h 127.0.0.1 -p 5433 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Ok 'PostgreSQL 이미 실행 중 (127.0.0.1:5433)'
} else {
    Write-Step 'PostgreSQL 기동 중...'
    & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData -l $PgLog -o '-p 5433' start | Out-Null
    Start-Sleep -Seconds 3
    & (Join-Path $PgBin 'pg_isready.exe') -h 127.0.0.1 -p 5433 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'PostgreSQL 기동 완료'
    } else {
        Write-Err "[오류] PostgreSQL을 시작하지 못했습니다. 로그: $PgLog"
        exit 1
    }
}

# --- 3. 마이그레이션 --------------------------------------------------------
Write-Step 'DB 스키마 확인 중...'
# 변수에 담으면 stdout·stderr 가 화면에 찍히지 않고 조용히 수집된다.
$migrateOut = & $Py -m alembic upgrade head 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err '[오류] 마이그레이션 실패'
    $migrateOut | Select-Object -Last 8 | ForEach-Object { Write-Host "    $_" }
    exit 1
}
Write-Ok 'DB 스키마 최신 상태'

# --- 4. 계정 확인 -----------------------------------------------------------
$countOut = & $Py -c "from tot_dashboard.core.db import get_session; from tot_dashboard.core.models import User; db=get_session(); print(db.query(User).count()); db.close()" 2>&1
$hasUser = ($countOut | Where-Object { $_ -match '^\d+$' } | Select-Object -Last 1)
if ("$hasUser" -eq '0') {
    Write-Warn2 '[안내] 계정이 하나도 없습니다. 아래 명령으로 관리자를 먼저 만드세요:'
    Write-Host  '         .\.venv\Scripts\python.exe -m tot_dashboard.core.bootstrap --login-id admin --name 관리자 --dept 정보통신과'
    Write-Host ''
}

# --- 5. 서버 ----------------------------------------------------------------
$uvArgs = @('-m', 'uvicorn', 'tot_dashboard.service.main:app',
            '--host', '127.0.0.1', '--port', "$Port")
if ($Reload) { $uvArgs += '--reload' }

if ($Background) {
    # 창을 닫아도 살아 있도록 별도 프로세스로 띄우고 PID 를 남긴다.
    $proc = Start-Process -FilePath $Py -ArgumentList $uvArgs `
                          -WorkingDirectory $Root -WindowStyle Hidden `
                          -RedirectStandardOutput $AppLog `
                          -RedirectStandardError "$AppLog.err" -PassThru
    "$($proc.Id)" | Out-File -FilePath $PidFile -Encoding ascii -NoNewline

    # 실제로 응답할 때까지 기다린다 — 기동 실패를 성공으로 보고하지 않기 위해.
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        if ($proc.HasExited) { break }
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }
    Write-Host ''
    if ($ok) {
        Write-Ok  "기동 완료 (PID $($proc.Id))"
        Write-Ok  "주소:  http://127.0.0.1:$Port/"
        Write-Host "  종료:  .\stop.ps1" -ForegroundColor Gray
        Write-Host "  로그:  $AppLog" -ForegroundColor DarkGray
    } else {
        Write-Err '[오류] 서버가 응답하지 않습니다.'
        Write-Host "         로그를 확인하세요: $AppLog.err"
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
        exit 1
    }
} else {
    Write-Host ''
    Write-Ok  "주소:  http://127.0.0.1:$Port/"
    Write-Host '  종료:  이 창에서 Ctrl+C'
    Write-Host '----------------------------------------'
    Write-Host ''
    & $Py @uvArgs
    Write-Host ''
    Write-Host '  PostgreSQL은 계속 실행 중입니다.' -ForegroundColor DarkGray
    Write-Host '  함께 내리려면:  .\stop.ps1 -All' -ForegroundColor DarkGray
}
