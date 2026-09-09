<#
.SYNOPSIS
    침수 독립 서비스(flood-service, 포트 8036)가 떠 있는지 확인하고,
    안 떠 있으면 새로 띄운다.

.DESCRIPTION
    ensure-road-service.ps1(Phase 2)과 완전히 같은 패턴 — 왜 필요한지·
    왜 이 방식인지는 그 스크립트 주석을 그대로 참고.

.PARAMETER TimeoutSec
    기동(포트 응답)을 기다리는 최대 시간(초). 기본 90 — 상시 지정된
    카메라마다 YOLO 모델 로드 + 실제 CCTV 스트림 접속을 **동기적으로**
    한다(``FloodPipelineRunner.__init__``가 모듈 임포트 시점에 전부
    구성한다). 실측(2026-08-31, 침수 2개소): 30초로는 부족했다 —
    crowd/road-service보다 느린 것은 이 두 서비스만 모듈 임포트 시점에
    실제 비디오 소스를 여는 무거운 초기화를 하기 때문이다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ensure-flood-service.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 90
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunCmd = Join-Path $Root 'scripts\urbanguard-flood-run.cmd'
$FloodPort = 8036

function Test-UgFloodService {
    $c = Get-NetTCPConnection -LocalPort $FloodPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

if (Test-UgFloodService) {
    Write-Host "[ensure-flood-service] OK - already listening on port $FloodPort."
    exit 0
}

# ⚠️ 2026-09-02 신설 — ensure-crowd-service.ps1과 같은 이유·같은 패턴.
# 관리자가 서비스 관리 화면에서 정지시켰으면 여기서 멈춘다.
$StateFile = Join-Path $Root 'data\config\service_state.json'
if (Test-Path $StateFile) {
    try {
        $state = Get-Content $StateFile -Raw | ConvertFrom-Json
        if ($state.stopped -contains 'flood') {
            Write-Host "[ensure-flood-service] SKIPPED - 관리자가 서비스 관리 화면에서 정지 상태로 지정했습니다. 다시 켜려면 관리자 화면에서 '시작'을 누르십시오."
            exit 0
        }
    }
    catch {
        # 상태 파일이 깨졌어도 평소대로 진행한다(fail-open).
    }
}

Write-Host "[ensure-flood-service] flood-service not responding on port $FloodPort - starting."

if (-not (Test-Path $RunCmd)) {
    Write-Host "[ensure-flood-service] FAILED - launcher not found: $RunCmd"
    exit 1
}

Start-Process -FilePath $env:ComSpec -ArgumentList '/c', "`"$RunCmd`"", "$FloodPort" `
    -WorkingDirectory $Root -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-UgFloodService) { break }
    Start-Sleep -Milliseconds 500
}

if (Test-UgFloodService) {
    Write-Host "[ensure-flood-service] OK - flood-service is listening on port $FloodPort."
    exit 0
}

Write-Host "[ensure-flood-service] FAILED - port $FloodPort never opened within ${TimeoutSec}s. See data\logs\serve-flood-console.log"
exit 1
