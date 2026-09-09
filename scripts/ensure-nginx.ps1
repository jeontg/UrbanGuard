<#
.SYNOPSIS
    API 게이트웨이(nginx)가 떠 있는지 확인하고, 안 떠 있으면 이 저장소
    전용 인스턴스를 띄운다.

.DESCRIPTION
    왜 필요한가 (2026-08-31 신설, 계획서
    C:\Users\전태건\.claude\plans\ticklish-discovering-pine.md Phase 0)
        도메인별 독립 배포(인파관리·노면관리 등을 별도 서비스로 분리)를
        하려면 브라우저가 접속하는 진입점이 하나로 고정돼 있어야 한다.
        지금은 앱(uvicorn, 127.0.0.1:8033)에 브라우저가 직접 붙는 구조라,
        서비스를 여러 개로 쪼개면 사용자가 포트를 여러 개 기억해야 한다.
        nginx를 앞단에 두어 경로별로 알맞은 백엔드로 나눠 보낸다.

        지금 단계(Phase 0)는 백엔드를 아직 안 쪼갰다 — upstream이
        127.0.0.1:8033(기존 앱) 하나뿐이다. Phase 1(인파관리)부터
        upstream/location이 하나씩 늘어난다(configs/nginx_base.conf 참고).

    ★ ensure-mediamtx.ps1과 같은 패턴을 그대로 따른다
        - 커밋된 본(configs\nginx_base.conf)을 .tools\nginx\conf\nginx.conf
          로 복사하되, **이미 있으면 덮어쓰지 않는다** — 관리자가 현장에서
          손으로 조정한 값(예: 포트, 타임아웃)이 재기동마다 사라지면 안
          된다. 다시 처음부터 받고 싶으면 그 파일을 지우고 이 스크립트를
          다시 돌리면 된다.
        - `.tools\` 는 통째로 gitignore 대상이라 이 저장소를 새로 받으면
          바이너리·설정이 없다 — 이 스크립트가 확인만 하고, nginx.exe
          자체가 없으면 설치 안내만 하고 실패한다(바이너리를 자동으로
          내려받지 않는다 — 망분리 운영망에서는 반입 절차를 거쳐야 하는
          파일이라, 스크립트가 조용히 인터넷에 접속하면 안 된다).

    ★ nginx는 MediaMTX와 달리 자체적으로 로그 파일에 쓴다
        MediaMTX는 stdout에 로그를 찍어서 cmd.exe ">>" 리다이렉트를 거쳐야
        했지만(ensure-mediamtx.ps1 주석 참고), nginx는 nginx.conf의
        access_log/error_log 지시자가 가리키는 파일에 스스로 쓴다. 그래서
        여기서는 그 우회가 필요 없고, `Start-Process`로 바로 띄운다.

.PARAMETER TimeoutSec
    nginx 기동(포트 응답)을 기다리는 최대 시간(초). 기본 10.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ensure-nginx.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 10
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$NginxDir = Join-Path $Root '.tools\nginx'
$NginxExe = Join-Path $NginxDir 'nginx.exe'
$NginxConfDir = Join-Path $NginxDir 'conf'
$NginxConf = Join-Path $NginxConfDir 'nginx.conf'
$LogDir = Join-Path $NginxDir 'logs'
# configs/nginx_base.conf의 listen 지시자와 반드시 같아야 한다 — 두 곳이
# 어긋나면 "떠 있는데 어느 포트인지 헷갈리는" 상태가 된다(ensure-mediamtx.ps1
# 이 이미 겪은 것과 같은 함정).
$GatewayPort = 8080

function Test-UgGateway {
    $c = Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

if (Test-UgGateway) {
    Write-Host "[ensure-nginx] OK - already listening on port $GatewayPort."
    exit 0
}

Write-Host "[ensure-nginx] gateway not responding on port $GatewayPort - starting this repo's own instance."

if (-not (Test-Path $NginxExe)) {
    Write-Host "[ensure-nginx] FAILED - nginx.exe not found: $NginxExe"
    Write-Host "[ensure-nginx] nginx.org(Windows용 stable)에서 받아 .tools\nginx\ 에 압축을 풀어야 합니다 — 이 스크립트는 바이너리를 자동으로 내려받지 않습니다(망분리 반입 절차 대상)."
    exit 1
}
if (-not (Test-Path $NginxConf)) {
    # 첫 실행 준비 — 커밋된 본(configs\nginx_base.conf)을 실제 설정
    # 위치로 복사한다. 이미 있으면 건드리지 않는다(위 설명 참고).
    $baseConf = Join-Path $Root 'configs\nginx_base.conf'
    if (Test-Path $baseConf) {
        Copy-Item $baseConf $NginxConf
        Write-Host "[ensure-nginx] $baseConf 을 $NginxConf 로 복사했습니다."
    }
    else {
        Write-Host "[ensure-nginx] FAILED - config not found: $NginxConf (본 파일도 없음: $baseConf)"
        exit 1
    }
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# nginx는 실행 파일 위치를 기준(prefix)으로 conf/logs 상대경로를 찾는다
# — WorkingDirectory를 반드시 nginx 설치 폴더로 맞춘다.
Start-Process -FilePath $NginxExe -WorkingDirectory $NginxDir -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-UgGateway) { break }
    Start-Sleep -Milliseconds 300
}

if (Test-UgGateway) {
    Write-Host "[ensure-nginx] OK - gateway is listening on port $GatewayPort."
    exit 0
}

Write-Host "[ensure-nginx] FAILED - port $GatewayPort never opened within ${TimeoutSec}s. See $LogDir\error.log / gateway_error.log"
exit 1
