<#
.SYNOPSIS
    노면관리 독립 서비스(road-service, 포트 8035)가 떠 있는지 확인하고,
    안 떠 있으면 새로 띄운다.

.DESCRIPTION
    ensure-crowd-service.ps1(Phase 1)과 완전히 같은 패턴 — 왜 필요한지·
    왜 이 방식인지는 그 스크립트 주석을 그대로 참고.

.PARAMETER TimeoutSec
    기동(포트 응답)을 기다리는 최대 시간(초). 기본 30(모델 로드 포함).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ensure-road-service.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunCmd = Join-Path $Root 'scripts\urbanguard-road-run.cmd'
$RoadPort = 8035

function Test-UgRoadService {
    $c = Get-NetTCPConnection -LocalPort $RoadPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

if (Test-UgRoadService) {
    Write-Host "[ensure-road-service] OK - already listening on port $RoadPort."
    exit 0
}

# ⚠️ 2026-09-02 신설 — ensure-crowd-service.ps1과 같은 이유·같은 패턴.
# 관리자가 서비스 관리 화면에서 정지시켰으면 여기서 멈춘다.
$StateFile = Join-Path $Root 'data\config\service_state.json'
if (Test-Path $StateFile) {
    try {
        $state = Get-Content $StateFile -Raw | ConvertFrom-Json
        if ($state.stopped -contains 'road') {
            Write-Host "[ensure-road-service] SKIPPED - 관리자가 서비스 관리 화면에서 정지 상태로 지정했습니다. 다시 켜려면 관리자 화면에서 '시작'을 누르십시오."
            exit 0
        }
    }
    catch {
        # 상태 파일이 깨졌어도 평소대로 진행한다(fail-open).
    }
}

Write-Host "[ensure-road-service] road-service not responding on port $RoadPort - starting."

if (-not (Test-Path $RunCmd)) {
    Write-Host "[ensure-road-service] FAILED - launcher not found: $RunCmd"
    exit 1
}

Start-Process -FilePath $env:ComSpec -ArgumentList '/c', "`"$RunCmd`"", "$RoadPort" `
    -WorkingDirectory $Root -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-UgRoadService) { break }
    Start-Sleep -Milliseconds 500
}

if (Test-UgRoadService) {
    Write-Host "[ensure-road-service] OK - road-service is listening on port $RoadPort."
    exit 0
}

Write-Host "[ensure-road-service] FAILED - port $RoadPort never opened within ${TimeoutSec}s. See data\logs\serve-road-console.log"
exit 1
