<#
.SYNOPSIS
    침수 독립 서비스(flood-service, 포트 8036)를 종료한다.

.DESCRIPTION
    stop-road-service.ps1(Phase 2)과 완전히 같은 패턴 — PID 재사용
    경쟁 상태 방지(2026-09-03 신설, ``Test-UgSameProcess``)도 포함해
    그대로 따른다. 근거는 stop-crowd-service.ps1의 .DESCRIPTION 참고.

.PARAMETER TimeoutSec
    종료를 기다리는 최대 시간(초). 기본 15.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop-flood-service.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 15
)

$ErrorActionPreference = 'Stop'

$FloodPort = 8036

function Test-UgFloodService {
    $c = Get-NetTCPConnection -LocalPort $FloodPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

function Get-UgFloodProcess {
    param([switch]$SupervisorOnly)
    $all = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    if ($null -eq $all) { return @() }
    $mine = $all | Where-Object {
        $c = $_.CommandLine
        (-not [string]::IsNullOrWhiteSpace($c)) -and ($c -like '*flood_service*')
    }
    if ($SupervisorOnly) { return @($mine | Where-Object { $_.CommandLine -like '*serve.py*' }) }
    return @($mine)
}

function Test-UgSameProcess {
    param($Snapshot)
    $fresh = Get-CimInstance Win32_Process -Filter "ProcessId=$($Snapshot.ProcessId)" -ErrorAction SilentlyContinue
    if ($null -eq $fresh) { return $false }
    return ($fresh.CreationDate -eq $Snapshot.CreationDate) -and
           ($fresh.CommandLine -eq $Snapshot.CommandLine)
}

if (-not (Test-UgFloodService)) {
    Write-Host "[stop-flood-service] OK - already down (port $FloodPort not listening)."
    exit 0
}

$killed = 0
foreach ($p in (Get-UgFloodProcess -SupervisorOnly)) {
    if (-not (Test-UgSameProcess $p)) {
        Write-Host "[stop-flood-service] SKIP - PID $($p.ProcessId) 는 확인 시점 사이 이미 사라졌습니다(재사용 위험 회피, 건드리지 않음)."
        continue
    }
    try {
        & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
        $killed++
    }
    catch { Write-Host "[stop-flood-service] WARN - taskkill PID $($p.ProcessId) 실패: $($_.Exception.Message)" }
}

Start-Sleep -Milliseconds 500

foreach ($p in (Get-UgFloodProcess)) {
    if (-not (Test-UgSameProcess $p)) {
        Write-Host "[stop-flood-service] SKIP - PID $($p.ProcessId) 는 확인 시점 사이 이미 사라졌습니다(재사용 위험 회피, 건드리지 않음)."
        continue
    }
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        $killed++
    }
    catch { }
}

if ($killed -eq 0) {
    Write-Host "[stop-flood-service] WARN - port $FloodPort is listening but no matching process (command line containing 'flood_service') found. Leaving it alone."
    exit 1
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-UgFloodService)) { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-UgFloodService)) {
    Write-Host "[stop-flood-service] OK - flood-service stopped (port $FloodPort)."
    exit 0
}

Write-Host "[stop-flood-service] FAILED - port $FloodPort is still listening after stop attempt."
exit 1
