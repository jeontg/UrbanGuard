<#
.SYNOPSIS
    인파관리 독립 서비스(crowd-service, 포트 8034)가 떠 있는지 확인하고,
    안 떠 있으면 새로 띄운다.

.DESCRIPTION
    왜 필요한가 (2026-08-31 신설, API 게이트웨이 도입 계획 Phase 1)
        `service/crowd_service.py`를 platform-shell(포트 8033)과 별개의
        독립 프로세스로 띄운다 — 인파관리를 다른 도메인과 무관하게
        기동·재기동할 수 있게 하는 것이 이번 분리의 목적이다.

    ★ ensure-mediamtx.ps1과 같은 패턴 — 장기 실행 프로세스를 배경으로 띄우고
      곧바로 반환한다. `urbanguard-crowd-run.cmd`가 `scripts\serve.py`
      (자가복구 감시자)를 거쳐 uvicorn을 띄우므로, 이 프로세스가 죽으면
      serve.py 자신이 재기동을 시도한다(platform-shell과 동일한 안전망).

    ⚠️ 아직 Windows 작업 스케줄러에는 등록하지 않는다(Phase 1 범위 밖) —
      이 스크립트가 매번 "안 떠 있으면 띄운다"를 확인하므로,
      `urbanguard-service.ps1 -Action start`를 부를 때마다 함께 확인된다.
      재부팅 시 자동 기동이 필요해지면 후속 단계에서 별도 작업으로
      등록한다.

.PARAMETER TimeoutSec
    기동(포트 응답)을 기다리는 최대 시간(초). 기본 30(모델 로드 포함).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ensure-crowd-service.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunCmd = Join-Path $Root 'scripts\urbanguard-crowd-run.cmd'
$CrowdPort = 8034

function Test-UgCrowdService {
    $c = Get-NetTCPConnection -LocalPort $CrowdPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

if (Test-UgCrowdService) {
    Write-Host "[ensure-crowd-service] OK - already listening on port $CrowdPort."
    exit 0
}

# ⚠️ 2026-09-02 신설 — 관리자가 서비스 관리(/admin/services) 화면에서
# 이 서비스를 "정지"시켰으면 여기서 멈춘다. 이 확인이 없으면
# `urbanguard-service.ps1 -Action start/restart`의 "전체 서비스 확인"
# 단계가 관리자의 의도와 무관하게 도로 띄워 버린다(실사용 중 자체
# 발견한 사고). `core/settings.py::set_admin_stopped_services()`가
# DB뿐 아니라 이 파일에도 함께 적어 둔다 — 이 스크립트는 DB를 못
# 읽으므로 파일로 전달받는다. exit 0 인 이유: 이건 실패가 아니라
# 관리자가 원한 정직한 결과다.
$StateFile = Join-Path $Root 'data\config\service_state.json'
if (Test-Path $StateFile) {
    try {
        $state = Get-Content $StateFile -Raw | ConvertFrom-Json
        if ($state.stopped -contains 'crowd') {
            Write-Host "[ensure-crowd-service] SKIPPED - 관리자가 서비스 관리 화면에서 정지 상태로 지정했습니다. 다시 켜려면 관리자 화면에서 '시작'을 누르십시오."
            exit 0
        }
    }
    catch {
        # 상태 파일이 깨졌어도 평소대로 진행한다(fail-open) — 파일 하나
        # 때문에 서비스가 영영 안 뜨면 안 된다.
    }
}

Write-Host "[ensure-crowd-service] crowd-service not responding on port $CrowdPort - starting."

if (-not (Test-Path $RunCmd)) {
    Write-Host "[ensure-crowd-service] FAILED - launcher not found: $RunCmd"
    exit 1
}

Start-Process -FilePath $env:ComSpec -ArgumentList '/c', "`"$RunCmd`"", "$CrowdPort" `
    -WorkingDirectory $Root -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-UgCrowdService) { break }
    Start-Sleep -Milliseconds 500
}

if (Test-UgCrowdService) {
    Write-Host "[ensure-crowd-service] OK - crowd-service is listening on port $CrowdPort."
    exit 0
}

Write-Host "[ensure-crowd-service] FAILED - port $CrowdPort never opened within ${TimeoutSec}s. See data\logs\serve-crowd-console.log"
exit 1
