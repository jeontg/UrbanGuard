<#
.SYNOPSIS
    이 저장소 전용 PostgreSQL 인스턴스를 정상 종료한다.

.DESCRIPTION
    ensure-postgres.ps1(기동 쪽)과 짝을 이루는 종료 스크립트다
    (2026-08-26, 사용자 요청 — "종료 시에도 같은 방식으로").

    ★ 왜 pg_ctl 을 그대로 쓰는가
        `-D $PgData` 로 **이 저장소의 데이터 디렉터리**를 지정해서 부르므로,
        그 디렉터리의 `postmaster.pid` 가 가리키는 프로세스만 종료 대상이
        된다 — 다른 PostgreSQL 인스턴스가 이 장비에 떠 있어도 건드리지
        않는다.

    ★ `-m fast` 를 쓰는 이유
        `smart`(기본값)는 모든 클라이언트 연결이 끊길 때까지 기다린다 —
        관제 화면이 계속 폴링 중이면 영원히 안 끝날 수 있다. `-m fast`는
        진행 중인 트랜잭션만 안전하게 되돌리고 즉시 종료한다(체크포인트는
        수행). 데이터 손상 없이 바로 끝내는 표준적인 방법이다.

    ⚠️ 이미 안 떠 있으면 조용히 성공으로 처리한다 — "종료"의 목적은
        "안 뜬 상태로 만드는 것"이라, 이미 그 상태면 할 일이 없다.

.PARAMETER TimeoutSec
    종료를 기다리는 최대 시간(초). 기본 30.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop-postgres.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PgBin = Join-Path $Root '.tools\pgsql\bin'
$PgData = Join-Path $Root '.tools\pgdata'
$PgCtl = Join-Path $PgBin 'pg_ctl.exe'

function Get-UgPgPort {
    # ensure-postgres.ps1 과 같은 방식으로 읽는다 — 두 스크립트가 다른
    # 포트를 보면 "종료했다는 포트"와 "실제 뜬 포트"가 어긋난다.
    $envFile = Join-Path $Root '.env'
    if (Test-Path $envFile) {
        $line = Get-Content $envFile -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\s*URBANGUARD_DATABASE_URL\s*=' } |
            Select-Object -First 1
        if ($line -and ($line -match ':(\d+)/[^/\s]+\s*$')) {
            return [int]$Matches[1]
        }
    }
    return 5433
}

function Test-UgPostgres {
    param([int]$PgPort)
    $c = Get-NetTCPConnection -LocalPort $PgPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

$PgPort = Get-UgPgPort

if (-not (Test-UgPostgres -PgPort $PgPort)) {
    Write-Host "[stop-postgres] OK - already down (port $PgPort not listening)."
    exit 0
}

if (-not (Test-Path $PgCtl)) {
    Write-Host "[stop-postgres] FAILED - pg_ctl not found: $PgCtl"
    exit 1
}

Write-Host "[stop-postgres] Stopping PostgreSQL (port $PgPort, fast shutdown)..."
& $PgCtl -D $PgData -m fast -w -t $TimeoutSec stop
$rc = $LASTEXITCODE

# ⚠️ ensure-postgres.ps1 과 같은 이유로, pg_ctl 의 종료코드만으로 곧바로
#   판정하지 않는다 — 다른 호출과 겹쳤을 가능성에 대비해 포트가 실제로
#   닫혔는지 직접 다시 본다.
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-UgPostgres -PgPort $PgPort)) { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-UgPostgres -PgPort $PgPort)) {
    Write-Host "[stop-postgres] OK - PostgreSQL stopped (port $PgPort)."
    exit 0
}

Write-Host "[stop-postgres] FAILED - pg_ctl exited $rc but port $PgPort is still listening."
exit 1
