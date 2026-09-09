<#
.SYNOPSIS
    이 저장소 전용 API 게이트웨이(nginx) 인스턴스를 종료한다.

.DESCRIPTION
    ensure-nginx.ps1(기동 쪽)과 짝을 이루는 종료 스크립트다
    (stop-mediamtx.ps1과 같은 대칭 구조, 2026-08-31 신설).

    ★ nginx는 자체 종료 명령이 있다 — MediaMTX와 다르다
        MediaMTX에는 관리 CLI가 없어 프로세스를 직접 찾아 죽여야 했지만,
        nginx는 `nginx.exe -s quit`(진행 중인 연결을 끝내고 정상 종료)를
        지원한다. 이 방식을 우선 쓰고, 그래도 안 죽으면(설정이 꼬였거나
        마스터 프로세스가 이미 이상 상태) stop-mediamtx.ps1과 같은 방식
        (명령줄에 이 저장소의 .tools\nginx 경로가 있는 프로세스만 강제
        종료)으로 물러난다 — 이름만으로 고르면 이 장비의 다른 nginx
        인스턴스까지 죽일 수 있다.

    ⚠️ 이미 안 떠 있으면 조용히 성공으로 처리한다.

.PARAMETER TimeoutSec
    종료를 기다리는 최대 시간(초). 기본 15.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop-nginx.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 15
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$NginxDir = Join-Path $Root '.tools\nginx'
$NginxExe = Join-Path $NginxDir 'nginx.exe'
$GatewayPort = 8080  # configs\nginx_base.conf의 listen 지시자와 동일해야 한다

function Test-UgGateway {
    $c = Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

function Get-UgNginxProcess {
    # ⚠️ 이름만으로 고르면 이 장비의 다른 nginx 인스턴스까지 죽인다.
    #   반드시 명령줄/실행경로에 이 저장소의 .tools\nginx 경로가 있어야 한다.
    $all = Get-CimInstance Win32_Process -Filter "Name='nginx.exe'" -ErrorAction SilentlyContinue
    if ($null -eq $all) { return @() }
    $back = $NginxDir
    $fwd = $NginxDir -replace '\\', '/'
    return @($all | Where-Object {
        $ep = $_.ExecutablePath
        $c = $_.CommandLine
        (($ep -like "*$back*") -or ($ep -like "*$fwd*")) -or
        ((-not [string]::IsNullOrWhiteSpace($c)) -and (($c -like "*$back*") -or ($c -like "*$fwd*")))
    })
}

if (-not (Test-UgGateway)) {
    Write-Host "[stop-nginx] OK - already down (port $GatewayPort not listening)."
    exit 0
}

if (Test-Path $NginxExe) {
    Write-Host "[stop-nginx] Stopping gateway (port $GatewayPort) via nginx -s quit..."
    try {
        Start-Process -FilePath $NginxExe -ArgumentList '-s', 'quit' -WorkingDirectory $NginxDir -Wait -WindowStyle Hidden
    }
    catch {
        Write-Host "[stop-nginx] WARN - 'nginx -s quit' failed: $($_.Exception.Message)"
    }
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-UgGateway)) { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-UgGateway)) {
    Write-Host "[stop-nginx] OK - gateway stopped (port $GatewayPort)."
    exit 0
}

# 정상 종료가 안 됐다 — 우리 인스턴스만 골라 강제 종료로 물러난다.
Write-Host "[stop-nginx] 'quit' didn't stop it in time - falling back to force-kill of this repo's own instance."
$procs = Get-UgNginxProcess
if ($procs.Count -eq 0) {
    Write-Host "[stop-nginx] WARN - port $GatewayPort is listening but no matching process found under $NginxDir. Leaving it alone."
    exit 1
}
foreach ($p in $procs) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    }
    catch {
        Write-Host "[stop-nginx] WARN - failed to stop PID $($p.ProcessId): $($_.Exception.Message)"
    }
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-UgGateway)) { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-UgGateway)) {
    Write-Host "[stop-nginx] OK - gateway stopped (port $GatewayPort)."
    exit 0
}

Write-Host "[stop-nginx] FAILED - port $GatewayPort is still listening after stop attempt."
exit 1
