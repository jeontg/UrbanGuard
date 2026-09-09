<#
.SYNOPSIS
    교통위험 독립 서비스(traffic-service, 포트 8037)가 떠 있는지 확인하고,
    안 떠 있으면 새로 띄운다.

.DESCRIPTION
    ensure-flood-service.ps1(Phase 4)과 완전히 같은 패턴.

.PARAMETER TimeoutSec
    기동(포트 응답)을 기다리는 최대 시간(초). 기본 120 — `ensure-flood-
    service.ps1`과 같은 이유이되, 상시 교통 카메라가 보통 더 많아
    (실측 8개소) 순차 접속 시간이 더 길다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ensure-traffic-service.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 120
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunCmd = Join-Path $Root 'scripts\urbanguard-traffic-run.cmd'
$TrafficPort = 8037

function Test-UgTrafficService {
    $c = Get-NetTCPConnection -LocalPort $TrafficPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

if (Test-UgTrafficService) {
    Write-Host "[ensure-traffic-service] OK - already listening on port $TrafficPort."
    exit 0
}

# ⚠️ 2026-09-02 신설 — ensure-crowd-service.ps1과 같은 이유·같은 패턴.
# 관리자가 서비스 관리 화면에서 정지시켰으면 여기서 멈춘다. 오늘 실제로
# 겪은 사고(교통위험을 정지시켰는데 플랫폼-쉘 재기동의 전체 확인
# 단계가 도로 켜 버림)가 이 스크립트에서 재발하지 않게 한다.
$StateFile = Join-Path $Root 'data\config\service_state.json'
if (Test-Path $StateFile) {
    try {
        $state = Get-Content $StateFile -Raw | ConvertFrom-Json
        if ($state.stopped -contains 'traffic') {
            Write-Host "[ensure-traffic-service] SKIPPED - 관리자가 서비스 관리 화면에서 정지 상태로 지정했습니다. 다시 켜려면 관리자 화면에서 '시작'을 누르십시오."
            exit 0
        }
    }
    catch {
        # 상태 파일이 깨졌어도 평소대로 진행한다(fail-open).
    }
}

Write-Host "[ensure-traffic-service] traffic-service not responding on port $TrafficPort - starting."

if (-not (Test-Path $RunCmd)) {
    Write-Host "[ensure-traffic-service] FAILED - launcher not found: $RunCmd"
    exit 1
}

Start-Process -FilePath $env:ComSpec -ArgumentList '/c', "`"$RunCmd`"", "$TrafficPort" `
    -WorkingDirectory $Root -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-UgTrafficService) { break }
    Start-Sleep -Milliseconds 500
}

if (Test-UgTrafficService) {
    Write-Host "[ensure-traffic-service] OK - traffic-service is listening on port $TrafficPort."
    exit 0
}

Write-Host "[ensure-traffic-service] FAILED - port $TrafficPort never opened within ${TimeoutSec}s. See data\logs\serve-traffic-console.log"
exit 1
