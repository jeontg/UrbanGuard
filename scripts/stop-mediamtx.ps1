<#
.SYNOPSIS
    이 저장소 전용 CCTV 재배포 허브(MediaMTX) 인스턴스를 종료한다.

.DESCRIPTION
    ensure-mediamtx.ps1(기동 쪽)과 짝을 이루는 종료 스크립트다
    (stop-postgres.ps1과 같은 대칭 구조, 2026-08-28 신설).

    ★ 왜 pg_ctl처럼 PID 파일로 못 찾는가
        MediaMTX에는 pg_ctl 같은 관리 CLI가 없다 — 직접 프로세스 목록에서
        찾아야 한다. 이름만으로 고르면 이 장비의 다른 MediaMTX 인스턴스까지
        죽일 수 있으므로, `urbanguard-service.ps1::Get-UgProcess`와 같은
        방식으로 **명령줄에 이 저장소 경로(.tools\mediamtx\)가 있는 것만**
        고른다.

    ⚠️ 이미 안 떠 있으면 조용히 성공으로 처리한다 — "종료"의 목적은
        "안 뜬 상태로 만드는 것"이라, 이미 그 상태면 할 일이 없다.

.PARAMETER TimeoutSec
    종료를 기다리는 최대 시간(초). 기본 15.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop-mediamtx.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 15
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$MtxDir = Join-Path $Root '.tools\mediamtx'
$ApiPort = 9997  # core/settings.py::KEY_RESTREAM_API_PORT 기본값과 동일해야 한다

function Test-UgMediaMTX {
    $c = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

function Get-UgMediaMTXProcess {
    # ⚠️ 이름만으로 고르면 이 장비의 다른 MediaMTX 인스턴스까지 죽인다.
    #   반드시 명령줄에 이 저장소의 .tools\mediamtx 경로가 있어야 한다.
    $all = Get-CimInstance Win32_Process -Filter "Name='mediamtx.exe'" -ErrorAction SilentlyContinue
    if ($null -eq $all) { return @() }
    $mtxBack = $MtxDir
    $mtxFwd = $MtxDir -replace '\\', '/'
    return @($all | Where-Object {
        $c = $_.CommandLine
        if ([string]::IsNullOrWhiteSpace($c)) { return $false }
        ($c -like "*$mtxBack*") -or ($c -like "*$mtxFwd*")
    })
}

if (-not (Test-UgMediaMTX)) {
    Write-Host "[stop-mediamtx] OK - already down (port $ApiPort not listening)."
    exit 0
}

$procs = Get-UgMediaMTXProcess
if ($procs.Count -eq 0) {
    # 포트는 열려 있는데 우리 프로세스를 못 찾았다 — 다른 MediaMTX가 같은
    # 포트를 쓰고 있을 수 있다. 함부로 죽이지 않고 사실만 알린다.
    Write-Host "[stop-mediamtx] WARN - port $ApiPort is listening but no matching process found under $MtxDir. Leaving it alone."
    exit 1
}

Write-Host "[stop-mediamtx] Stopping MediaMTX (port $ApiPort)..."
foreach ($p in $procs) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    }
    catch {
        Write-Host "[stop-mediamtx] WARN - failed to stop PID $($p.ProcessId): $($_.Exception.Message)"
    }
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-UgMediaMTX)) { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-UgMediaMTX)) {
    Write-Host "[stop-mediamtx] OK - MediaMTX stopped (port $ApiPort)."
    exit 0
}

Write-Host "[stop-mediamtx] FAILED - port $ApiPort is still listening after stop attempt."
exit 1
