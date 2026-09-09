<#
.SYNOPSIS
    인파관리 독립 서비스(crowd-service, 포트 8034)를 종료한다.

.DESCRIPTION
    ensure-crowd-service.ps1(기동 쪽)과 짝을 이루는 종료 스크립트다.

    ★ 감시자(serve.py)를 먼저 죽인다 — 순서를 바꾸면 자식(uvicorn)이
      되살아난다(``urbanguard-service.ps1::Invoke-Stop``과 같은 이유).
      venv의 python.exe는 실제 인터프리터를 자식으로 다시 띄우는 껍데기라
      한 단계가 둘로 보일 수 있어, ``/T``(트리 전체)로 taskkill한다.

    ⚠️ 이름만으로 고르면 platform-shell(main:app)이나 다른 파이썬까지
       죽인다 — 반드시 명령줄에 ``crowd_service``가 있는 것만 고른다.

    ⚠️★ 2026-09-03 신설 — PID 재사용 경쟁 상태(실사용 중 발견: 관리자가
       서비스 2개를 거의 동시에 정지시켰을 때 전체 서비스가 죽는 사고가
       실제로 발생, ``data\logs\admin-service-ops-console.log``에 그
       증거가 남아 있다 — "PID 21316은 프로세스(PID 14660의 자식
       프로세스)를 종료할 수 없습니다"라는 taskkill 고유 메시지는
       taskkill이 **자기 자신을 실행 중인 프로세스 트리를 실수로
       죽이려던 것을 스스로 막았을 때만** 나온다. 즉 원래 죽이려던
       PID는 이미 끝났고, 그 번호가 짧은 시간 안에 **완전히 무관한
       다른 프로세스**로 재사용된 것이다). ``Get-UgCrowdProcess``가
       스냅샷을 찍은 시점과 실제 taskkill 실행 시점 사이에 그 PID가
       이미 끝나고 Windows가 같은 번호를 다른 프로세스에 재사용했을
       수 있다 — 여러 서비스를 동시에 정지시키면(짧은 시간에 프로세스
       생성·종료가 몰려) 이 틈이 넓어진다. ``CommandLine``만으로는
       재확인이 안 된다(재사용된 새 프로세스가 우연히 같은 커맨드라인일
       수도 있으므로) — ``CreationDate``까지 원래 스냅샷과 정확히 같은
       PID인지 재확인해야 진짜 "같은 프로세스"임을 보장한다. 어긋나면
       이미 사라진 것으로 보고 **죽이지 않는다** — 무관한 프로세스를
       잘못 죽이는 사고가 "죽여야 할 프로세스를 못 찾는 것"보다 훨씬
       위험하다.

.PARAMETER TimeoutSec
    종료를 기다리는 최대 시간(초). 기본 15.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop-crowd-service.ps1
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 15
)

$ErrorActionPreference = 'Stop'

$CrowdPort = 8034

function Test-UgCrowdService {
    $c = Get-NetTCPConnection -LocalPort $CrowdPort -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

function Get-UgCrowdProcess {
    param([switch]$SupervisorOnly)
    $all = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    if ($null -eq $all) { return @() }
    $mine = $all | Where-Object {
        $c = $_.CommandLine
        (-not [string]::IsNullOrWhiteSpace($c)) -and ($c -like '*crowd_service*')
    }
    if ($SupervisorOnly) { return @($mine | Where-Object { $_.CommandLine -like '*serve.py*' }) }
    return @($mine)
}

# PID 재사용 경쟁 상태 방지(위 .DESCRIPTION 참고) — 죽이기 직전에 그 PID가
# 스냅샷 당시와 정말 같은 프로세스인지(CreationDate·CommandLine 둘 다) 다시
# 확인한다. 어긋나면 이미 사라진 것으로 보고 건드리지 않는다.
function Test-UgSameProcess {
    param($Snapshot)
    $fresh = Get-CimInstance Win32_Process -Filter "ProcessId=$($Snapshot.ProcessId)" -ErrorAction SilentlyContinue
    if ($null -eq $fresh) { return $false }
    return ($fresh.CreationDate -eq $Snapshot.CreationDate) -and
           ($fresh.CommandLine -eq $Snapshot.CommandLine)
}

if (-not (Test-UgCrowdService)) {
    Write-Host "[stop-crowd-service] OK - already down (port $CrowdPort not listening)."
    exit 0
}

# 감시자(serve.py)를 먼저 죽인다 — /T로 그 자식(uvicorn)까지 함께.
$killed = 0
foreach ($p in (Get-UgCrowdProcess -SupervisorOnly)) {
    if (-not (Test-UgSameProcess $p)) {
        Write-Host "[stop-crowd-service] SKIP - PID $($p.ProcessId) 는 확인 시점 사이 이미 사라졌습니다(재사용 위험 회피, 건드리지 않음)."
        continue
    }
    try {
        & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
        $killed++
    }
    catch { Write-Host "[stop-crowd-service] WARN - taskkill PID $($p.ProcessId) 실패: $($_.Exception.Message)" }
}

Start-Sleep -Milliseconds 500

# 감시자 없이 홀로 남은 프로세스가 있으면 정리한다.
foreach ($p in (Get-UgCrowdProcess)) {
    if (-not (Test-UgSameProcess $p)) {
        Write-Host "[stop-crowd-service] SKIP - PID $($p.ProcessId) 는 확인 시점 사이 이미 사라졌습니다(재사용 위험 회피, 건드리지 않음)."
        continue
    }
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        $killed++
    }
    catch { }
}

if ($killed -eq 0) {
    Write-Host "[stop-crowd-service] WARN - port $CrowdPort is listening but no matching process (command line containing 'crowd_service') found. Leaving it alone."
    exit 1
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-UgCrowdService)) { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-UgCrowdService)) {
    Write-Host "[stop-crowd-service] OK - crowd-service stopped (port $CrowdPort)."
    exit 0
}

Write-Host "[stop-crowd-service] FAILED - port $CrowdPort is still listening after stop attempt."
exit 1
