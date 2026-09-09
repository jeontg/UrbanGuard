<#
.SYNOPSIS
    PostgreSQL이 떠 있는지 확인하고, 안 떠 있으면 이 저장소 전용 인스턴스를 띄운다.

.DESCRIPTION
    왜 필요한가 (docs/pending_tasks.md 1-1, 2026-08-19 최초 발견 · 2026-08-21
    재발 — 절전 복귀 후 사라짐)
        PostgreSQL은 Windows 서비스로 등록돼 있지 않다. `pg_ctl` 로 수동으로
        띄운 프로세스라, 콘솔을 닫거나 컴퓨터가 절전에서 돌아오거나
        재부팅하면 **조용히 사라진다.** UrbanGuard 웹 서비스(serve.py)는
        자기 자신이 죽으면 스스로 되살아나지만, **DB가 없는 것은 스스로
        못 고친다** — 매 요청이 `connection timeout expired`로 실패할 뿐,
        프로세스 자체는 "떠 있는" 것처럼 보인다.

        그래서 UrbanGuard 를 띄우는 모든 경로(작업 스케줄러가 실행하는
        `urbanguard-run.cmd`, 사람이 직접 부르는 `urbanguard-service.ps1
        -Action start`)가 **먼저 이 스크립트를 거친다.**

    ★ 포트는 .env 에서 읽는다
        `core/db.py` 의 `URBANGUARD_DATABASE_URL` 과 다른 포트로 PostgreSQL을
        띄우면, **"떠 있는데 안 보이는"** 가장 헷갈리는 상태가 된다
        (2026-08-21 실제로 겪음 — 기본 포트 5432 로 떴는데 앱은 5433 을
        본다). 그래서 하드코딩하지 않고 `.env` 를 직접 읽는다.

    ⚠️ 이것은 Windows 서비스 등록이 **아니다**
        여전히 "누군가 UrbanGuard 를 한 번은 띄웠다"는 것이 전제다.
        재부팅 후 아무도 로그인하지 않으면 이 스크립트도 안 돈다.
        완전한 해법(무인 재부팅에도 뜸)은 관리자 권한으로 하는 윈도우
        서비스(NSSM) 등록이며 `-Action guide` 로 안내한다 — 반입 심의가
        선행돼야 해서 별도 과제로 남겨 뒀다(1-3).

.PARAMETER TimeoutSec
    PostgreSQL 기동을 기다리는 최대 시간(초). 기본 60.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ensure-postgres.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 60
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PgBin = Join-Path $Root '.tools\pgsql\bin'
$PgData = Join-Path $Root '.tools\pgdata'
$PgCtl = Join-Path $PgBin 'pg_ctl.exe'
$LogDir = Join-Path $Root 'data\logs'
$PgLog = Join-Path $LogDir 'pg-console.log'

function Get-UgPgPort {
    <#
      .env 의 URBANGUARD_DATABASE_URL 에서 포트를 읽는다. 파일이 없거나
      값을 못 읽으면 core/db.py 의 DEFAULT_URL 과 같은 5433 으로 되돌아간다
      — 두 곳이 어긋나면 안 되므로 반드시 같은 기본값을 쓴다.
    #>
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

if (Test-UgPostgres -PgPort $PgPort) {
    Write-Host "[ensure-postgres] OK - already listening on port $PgPort."
    exit 0
}

Write-Host "[ensure-postgres] PostgreSQL not responding on port $PgPort - starting this repo's own instance."

if (-not (Test-Path $PgCtl)) {
    Write-Host "[ensure-postgres] FAILED - pg_ctl not found: $PgCtl"
    exit 1
}
if (-not (Test-Path $PgData)) {
    Write-Host "[ensure-postgres] FAILED - data directory not found: $PgData"
    exit 1
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# ⚠️ pg_ctl start 는 postgres.exe 를 새로 띄우고, 그 프로세스는 서비스가
#   살아있는 내내 안 죽는다. Windows 에서 자식 프로세스는 부모가 상속
#   가능하게 표시해 둔 핸들을 물려받으므로, "이 스크립트를 부른 쪽"이 자기
#   출력을 파일(">>")이나 파이프("2>&1" 캡처)로 리다이렉트해 뒀다면 그
#   핸들이 postgres 에 상속돼 postgres 가 죽을 때까지 안 닫힌다 — 리다이렉트가
#   파일이면 그 파일에 대한 이후의 다른 ">>" 쓰기가 "다른 프로세스가 사용
#   중"으로 막히고(urbanguard-run.cmd 에서 실제로 겪음 → 이 스크립트를 부를
#   때 전용 로그 파일을 쓰도록 고쳐 해결, 2026-08-21), 리다이렉트가 파이프면
#   부른 쪽이 EOF 를 영원히 못 봐서 멈춘다(urbanguard-service.ps1 의
#   -Action restart 에서 실제로 겪음 → 그쪽을 파일 리다이렉트로 바꿔 해결,
#   2026-08-22). ⚠️ 이 스크립트 자신의 표준출력 핸들의 상속 플래그를 꺼서
#   막아 보려는 시도는 효과가 없었다(원인 미확인 — Write-Host 의 출력 경로가
#   그 OS 핸들을 그대로 안 타는 것으로 추정) — 그래서 근본 수정은 이 스크립트
#   가 아니라 "이 스크립트를 부르는 두 곳"에 있다. 이 스크립트를 새로 부르는
#   자리를 추가한다면, 그 호출도 파이프 캡처("2>&1"+대입)가 아니라 파일
#   리다이렉트를 쓰도록 하십시오.

# ⚠️ -o "-p $PgPort" 를 꼭 넘긴다 — 안 넘기면 postgresql.conf 의 기본 포트
#   (5432)로 뜨는데, 앱은 .env 의 포트(보통 5433)를 본다. 그러면 "프로세스는
#   떠 있는데 앱은 매 요청 타임아웃"이라는, 고장이 아닌 것처럼 보이는
#   가장 헷갈리는 상태가 된다(2026-08-21 실제로 겪음).
& $PgCtl -D $PgData -o "-p $PgPort" -l $PgLog -w -t $TimeoutSec start
$rc = $LASTEXITCODE

if ($rc -ne 0) {
    # ⚠️ 이 스크립트는 여러 곳(대화형 -Action start 와, 작업 스케줄러가 부르는
    #   urbanguard-run.cmd)에서 **거의 동시에** 불릴 수 있다 — 실제로 그래서
    #   실패한 적이 있다(2026-08-21). 한쪽이 postgres 를 막 띄우는 중이면
    #   다른 쪽의 pg_ctl 은 잠금 충돌로 곧바로 실패를 반환하는데, 그 순간에도
    #   **실제로는 첫 번째 호출이 정상적으로 띄우고 있는 중**이었다. 그래서
    #   pg_ctl 이 실패를 말해도 곧바로 포기하지 않고, 포트가 열리는지 잠깐
    #   더 기다린다 — 다른 호출이 성공했다면 여기서도 성공으로 본다.
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (Test-UgPostgres -PgPort $PgPort) { break }
        Start-Sleep -Milliseconds 500
    }
}

if (Test-UgPostgres -PgPort $PgPort) {
    Write-Host "[ensure-postgres] OK - PostgreSQL is listening on port $PgPort."
    exit 0
}

Write-Host "[ensure-postgres] FAILED - pg_ctl exited $rc and port $PgPort never opened. See $PgLog"
exit 1
