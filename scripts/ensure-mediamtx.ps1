<#
.SYNOPSIS
    CCTV 재배포 허브(MediaMTX)가 떠 있는지 확인하고, 안 떠 있으면 이 저장소
    전용 인스턴스를 띄운다.

.DESCRIPTION
    왜 필요한가 (2026-08-28 신설)
        4개 탐지 서비스(침수·교통위험·인파·노면)가 각자 원본 CCTV 서버에
        개별 연결하면, 같은 카메라에 세 번째 연결이 시도될 때 CCTV 서버가
        거절한다(실측, `road/live_analyzer.py` 주석). MediaMTX가 원본에는
        연결 1개만 열고 그 뒤에서 RTSP·WebRTC로 재배포한다.

        재배포 자체는 `core/settings.py`의 `restream.enabled` 설정(기본
        꺼짐)이 켜져 있을 때만 실제로 쓰인다 — 이 스크립트는 그 설정과
        무관하게 **항상 MediaMTX 프로세스를 띄운다**(포트만 열어 두는 것은
        무해하고, 설정이 나중에 켜져도 곧바로 쓸 수 있어야 한다). 카메라
        경로 채우기는 파이썬 쪽(`core/restream.py::sync_all_paths`, 서비스
        기동 시 1회)이 맡는다 — 이 스크립트는 프로세스 기동만 책임진다.

    ★ 왜 pg_ctl 처럼 자체적으로 로그 파일에 쓰게 못 하는가
        PostgreSQL의 `pg_ctl start -l <log>`는 postgres.exe를 완전히 분리된
        배경 프로세스로 만들고, `pg_ctl` 자신은 확인 후 즉시 반환한다.
        MediaMTX는 그런 자기 분리(daemonize) 기능이 없다 — 실행하면 그
        프로세스 자체가 서버로 남아 전면에서 계속 돈다. 그래서 이 스크립트가
        **직접 배경으로 띄우고 곧바로 반환**해야 한다(`urbanguard-run.cmd`가
        `serve.py`를 띄우는 것과 같은 구조).

    ⚠️ PowerShell 파이프 리다이렉트 금지 (ensure-postgres.ps1과 같은 함정)
        `Start-Process -RedirectStandardOutput/-RedirectStandardError`나
        PowerShell native `2>&1` 캡처는 내부적으로 "자식의 출력이 끝날 때까지"
        기다리는 관로를 쓴다. MediaMTX는 서비스가 살아 있는 내내 안 죽으므로,
        그 관로를 문 자기 자신이 죽을 때까지 이 스크립트가 응답 없이 멈춘다
        (`urbanguard-service.ps1::Invoke-EnsurePostgres`의 주석에 있는 실제
        재현 사례와 같은 원인). 대신 **cmd.exe의 ">>" 리다이렉트**를 한 단계
        거쳐서 배경으로 띄운다.

.PARAMETER TimeoutSec
    MediaMTX 기동(Control API 응답)을 기다리는 최대 시간(초). 기본 20.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ensure-mediamtx.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 20
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$MtxDir = Join-Path $Root '.tools\mediamtx'
$MtxExe = Join-Path $MtxDir 'mediamtx.exe'
$MtxYml = Join-Path $MtxDir 'mediamtx.yml'
$LogDir = Join-Path $Root 'data\logs'
$MtxLog = Join-Path $LogDir 'mediamtx.log'
# core/settings.py::KEY_RESTREAM_API_PORT 기본값(9997)과 반드시 같아야 한다
# — 두 곳이 어긋나면 "떠 있는데 앱은 다른 포트를 본다"는 헷갈리는 상태가
# 된다(ensure-postgres.ps1이 겪은 것과 같은 함정).
$ApiPort = 9997

function Test-UgMediaMTX {
    $c = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

if (Test-UgMediaMTX) {
    Write-Host "[ensure-mediamtx] OK - already listening on port $ApiPort."
    exit 0
}

Write-Host "[ensure-mediamtx] MediaMTX not responding on port $ApiPort - starting this repo's own instance."

if (-not (Test-Path $MtxExe)) {
    Write-Host "[ensure-mediamtx] FAILED - mediamtx.exe not found: $MtxExe"
    exit 1
}
if (-not (Test-Path $MtxYml)) {
    # 첫 실행 준비 — 커밋된 본(configs\mediamtx_base.yml)을 실제 설정
    # 위치로 복사한다. .tools\ 는 통째로 gitignore 대상이라 배포 때마다
    # 새로 만들어야 한다.
    $baseYml = Join-Path $Root 'configs\mediamtx_base.yml'
    if (Test-Path $baseYml) {
        Copy-Item $baseYml $MtxYml
        Write-Host "[ensure-mediamtx] $baseYml 을 $MtxYml 로 복사했습니다."
    }
    else {
        Write-Host "[ensure-mediamtx] FAILED - config not found: $MtxYml (본 파일도 없음: $baseYml)"
        exit 1
    }
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# ⚠️ 임시 배치 파일 경유 — urbanguard-service.ps1::Invoke-EnsurePostgres와
#   같은 이유(문자열 재인용 문제·인코딩 문제 회피)로, 내용을 그대로 적어
#   cmd.exe가 원래 뜻대로 읽게 한다. ASCII 인코딩을 쓰므로 이 임시 파일은
#   반드시 순수 ASCII 경로(이 저장소 루트) 아래 둔다.
$tmpBat = Join-Path $LogDir "ensure-mtx-$PID.tmp.cmd"
Set-Content -Path $tmpBat -Encoding ASCII -Value @(
    '@echo off'
    "cd /d `"$MtxDir`""
    "`"$MtxExe`" `"$MtxYml`" >> `"$MtxLog`" 2>&1"
)

# ⚠️ 이 프로세스는 서비스가 살아 있는 내내 계속 돌아야 한다 — WaitForExit를
#   부르지 않는다(urbanguard-service.ps1::Invoke-Start가 serve.py를 띄울 때
#   와 같은 방식 — "띄우고 곧바로 반환", pg_ctl처럼 "다 될 때까지 기다렸다
#   반환"이 아니다).
Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -WindowStyle Hidden

# 임시 배치 파일은 cmd.exe가 그 내용을 다 읽은 뒤에는 필요 없다 — 다만
# cmd.exe가 이 파일을 여전히 열어 두고 있을 수 있으니 삭제 실패는 무시한다.
Start-Sleep -Milliseconds 300
Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-UgMediaMTX) { break }
    Start-Sleep -Milliseconds 500
}

if (Test-UgMediaMTX) {
    Write-Host "[ensure-mediamtx] OK - MediaMTX is listening on port $ApiPort."
    exit 0
}

Write-Host "[ensure-mediamtx] FAILED - port $ApiPort never opened within ${TimeoutSec}s. See $MtxLog"
exit 1
