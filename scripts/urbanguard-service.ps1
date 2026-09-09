<#
.SYNOPSIS
    UrbanGuard 서비스를 작업 스케줄러에 등록하고, 기동·종료·상태를 다룬다.

.DESCRIPTION
    왜 필요한가 (2026-08-18 사고)
        콘솔 창을 닫는 순간 감시(scripts/serve.py)까지 함께 죽었다. 되살릴
        것이 남지 않아 **다음 날까지 서비스가 없었다.** serve.py 는 「죽으면
        되살리는」 것까지만 하고, 「처음에 누가 띄우는가」는 못 한다.

        그 자리를 작업 스케줄러가 맡는다.

    ⚠️ 이 등록에는 한계가 있다 — 숨기지 않는다.
        관리자 권한이 없어 **부팅 시(ONSTART)로는 등록할 수 없다.**
        지금 등록하는 것은 **로그온 시**다. 즉 서버가 재부팅되면 **누군가
        로그인해야** 서비스가 뜬다. 무인 서버에서는 이것으로 부족하다.

        운영 납품에서는 관리자 권한으로 **윈도우 서비스(NSSM)** 로 등록해야
        한다. 그때 필요한 명령은 -Action guide 로 볼 수 있다.

    ★ 종료가 왜 두 단계인가
        감시(serve.py)를 남겨 둔 채 자식(uvicorn)만 죽이면 **5초 뒤 되살아난다.**
        그것이 감시의 존재 이유다. 그래서 감시를 **먼저** 끝내고, 그 다음 남은
        자식을 정리한다. 순서를 바꾸면 종료했다고 보고하고 실제로는 살아 있다.

    ★ 다른 파이썬을 죽이지 않는다
        같은 장비에 flood_t 의 서비스가 있고 그쪽도 python.exe 로 돈다.
        명령줄에 **이 저장소 경로가 들어 있는 프로세스만** 고른다.

.PARAMETER Action
    install    작업 스케줄러에 등록한다 (로그온 시 자동 기동)
    uninstall  등록을 지운다 (지우기 전에 종료한다)
    start      지금 띄운다 — PostgreSQL이 안 떠 있으면 먼저 띄운다
    stop       작업과 프로세스를 **모두** 끝낸다 — PostgreSQL도 함께 내린다(-KeepDb 로 예외)
    restart    stop 후 start
    status     작업·프로세스·포트·응답·PostgreSQL 상태를 확인해 보여 준다
    guide      윈도우 서비스(NSSM) 등록 명령을 보여 준다 (관리자 권한 필요)

.PARAMETER Port
    기본 8033.

.PARAMETER KeepDb
    stop/restart/uninstall 에서 PostgreSQL을 내리지 않는다. 다른 클라이언트가
    같은 DB에 붙어 있거나, 앱만 자주 재시작하며 DB는 그대로 두고 싶을 때 쓴다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action install
    powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action stop
    powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action restart -KeepDb
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'guide')]
    [string]$Action = 'status',
    [int]$Port = 8033,
    [string]$TaskName = 'UrbanGuard',
    # stop/restart/uninstall 이 PostgreSQL 도 함께 내리지 않게 한다.
    # 2026-08-26: start 쪽에 자동 확인·기동을 붙인 것과 짝을 맞춰 stop 쪽도
    # 기본으로 PostgreSQL을 함께 내린다 — 이 스위치로만 예외를 둔다(다른
    # 클라이언트가 같은 DB에 붙어 있거나, 앱만 자주 재시작하며 DB는 그대로
    # 두고 싶을 때).
    [switch]$KeepDb,
    # stop/restart 가 CCTV 재배포 허브(MediaMTX)를 내리지 않게 한다
    # (2026-08-28 신설) — -KeepDb 와 별개 스위치로 둔 이유: 둘은 서로 무관한
    # 외부 프로세스라(DB vs 미디어 서버), 하나를 유지하고 싶다고 해서 다른
    # 것까지 같이 유지하고 싶다는 보장이 없다.
    [switch]$KeepMediaMTX,
    # stop/restart 가 API 게이트웨이(nginx)를 내리지 않게 한다(2026-08-31
    # 신설, Phase 0) — MediaMTX와 같은 이유로 별개 스위치.
    [switch]$KeepNginx,
    # stop/restart 가 인파관리 독립 서비스(crowd-service)를 내리지 않게
    # 한다(2026-08-31 신설, Phase 1) — 다른 도메인과 독립 배포가 목적이라,
    # platform-shell만 재기동하고 crowd-service는 그대로 두고 싶을 때 쓴다.
    [switch]$KeepCrowdService,
    # stop/restart 가 노면관리 독립 서비스(road-service)를 내리지 않게
    # 한다(2026-08-31 신설, Phase 2) — KeepCrowdService와 같은 이유.
    [switch]$KeepRoadService,
    # stop/restart 가 침수·교통위험 독립 서비스(flood-service·
    # traffic-service)를 내리지 않게 한다(2026-08-31 신설, Phase 4) —
    # KeepCrowdService/KeepRoadService와 같은 이유.
    [switch]$KeepFloodService,
    [switch]$KeepTrafficService
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunCmd = Join-Path $Root 'scripts\urbanguard-run.cmd'
# 명령줄 대조용. 같은 경로가 슬래시 두 방향으로 다 나타난다
# (감시는 역슬래시, 자식 uvicorn 의 --app-dir 는 정슬래시로 넘어간다).
$RootBack = $Root
$RootFwd = $Root -replace '\\', '/'

function Write-Head($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "  [!] $text" -ForegroundColor Yellow }
function Write-Bad($text) { Write-Host "  [X] $text" -ForegroundColor Red }

function Get-UgProcess {
    <#
      이 저장소의 파이썬 프로세스만 고른다. **platform-shell(포트 8033)
      전용이다** — crowd-service·road-service는 별도 함수
      (Get-UgCrowdProcess류, scripts\stop-crowd-service.ps1 등)가 각자
      담당한다.

      ⚠️ 이름만으로 고르면 flood_t 서비스나 관계없는 파이썬까지 죽인다.
         반드시 명령줄에 이 저장소 경로가 있어야 한다.

      ⚠️ 2026-08-31 — API 게이트웨이 Phase 1(crowd-service) 도입 직후
         실기 재기동 중 발견한 사고: `-SupervisorOnly`가 `*serve.py*`
         만으로 걸렀는데, crowd-service·road-service도 같은
         `scripts\serve.py`를 감시자로 쓰게 되면서(2026-08-31, --app
         인자 추가) 이 필터가 **그 서비스들의 감시자까지 함께 골라
         `/T`로 죽여 버렸다** — platform-shell만 재기동하려 했는데
         옆 도메인 서비스까지 같이 죽는, Phase 1·2가 없애려던 바로 그
         결합이 이 스크립트 안에서 재발한 것이다. platform-shell의
         실행 파일(urbanguard-run.cmd)은 `--app`을 **넘기지 않고**
         기본값(main:app)에 기댄다 — 반면 crowd/road-service 실행
         파일은 항상 `--app ...`을 명시한다. 그래서 "명령줄에 --app이
         없는 serve.py만" 골라야 platform-shell 전용이 된다 — 이렇게
         해 두면 앞으로 어떤 도메인을 더 분리해도(다들 --app을 명시할
         것이므로) 자동으로 안전하다.
    #>
    param([switch]$SupervisorOnly, [switch]$ChildOnly)

    $all = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    if ($null -eq $all) { return @() }

    $mine = $all | Where-Object {
        $c = $_.CommandLine
        if ([string]::IsNullOrWhiteSpace($c)) { return $false }
        ($c -like "*$RootBack*") -or ($c -like "*$RootFwd*")
    }
    # ⚠️ "--app-dir"이 "--app"의 부분 문자열이라, uvicorn 자식은
    #    platform-shell 것이든 crowd/road-service 것이든 **전부**
    #    "--app-dir"를 인자로 받는다(serve.py가 항상 붙인다) — 그래서
    #    "--app "(뒤에 공백)처럼 정확히 그 플래그만 걸러야 한다. 감시자
    #    (serve.py) 자신의 명령줄에는 --app-dir이 아예 없으므로(그건
    #    자식에게만 넘기는 인자다) 이 구분이 안전하다.
    if ($SupervisorOnly) {
        return @($mine | Where-Object {
            $_.CommandLine -like '*serve.py*' -and $_.CommandLine -notlike '*--app *'
        })
    }
    if ($ChildOnly) { return @($mine | Where-Object { $_.CommandLine -like '*tot_dashboard.service.main:app*' }) }
    # 스위치 없이 부르는 경우("이 프로세스에 남은 게 있나" 같은 잔존 확인)
    # 는 platform-shell의 감시자+자식만 센다 — crowd-service·road-service
    # 를 platform-shell의 "남은 프로세스"로 잘못 세면 안 된다.
    return @($mine | Where-Object {
        ($_.CommandLine -like '*serve.py*' -and $_.CommandLine -notlike '*--app *') -or
        ($_.CommandLine -like '*tot_dashboard.service.main:app*')
    })
}

function Get-UgTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

# ⚠️★ 2026-09-03 신설 — PID 재사용 경쟁 상태 방지. 도메인 서비스 정지
# 스크립트(scripts\stop-{crowd,road,flood,traffic}-service.ps1)에서
# 실사용 중 실제로 재현된 문제와 같은 계열이다(근거·상세는 그 파일들의
# .DESCRIPTION 참고 — taskkill이 "PID N은 프로세스(PID M의 자식
# 프로세스)를 종료할 수 없습니다"라는 고유 메시지를 내는 것은 죽이려던
# PID가 이미 끝나고 그 번호가 무관한 새 프로세스로 재사용됐다는 신호다).
# `Get-UgProcess`가 스냅샷을 찍은 시점과 `Stop-UgTree`가 실제로 taskkill을
# 실행하는 시점 사이에 그 PID가 재사용됐을 수 있으므로, 죽이기 직전에
# `CommandLine`·`CreationDate`가 스냅샷 당시와 정확히 같은지 재확인한다.
function Test-UgSameProcess {
    param($Snapshot)
    $fresh = Get-CimInstance Win32_Process -Filter "ProcessId=$($Snapshot.ProcessId)" -ErrorAction SilentlyContinue
    if ($null -eq $fresh) { return $false }
    return ($fresh.CreationDate -eq $Snapshot.CreationDate) -and
           ($fresh.CommandLine -eq $Snapshot.CommandLine)
}

function Stop-UgTree {
    <#
      프로세스와 그 자식들을 끝낸다. 이미 죽었으면(또는 스냅샷 이후 PID가
      재사용됐으면) 조용히 지나간다.

      ⚠️ taskkill 을 PowerShell 에서 그냥 부르면 안 된다.
         ① /T 로 부모를 죽이면 자식도 함께 죽는데, 목록을 미리 뽑아 뒀다가
            그 자식을 또 죽이려 하면 "process not found" 가 난다.
         ② PowerShell 5.1 에서 네이티브 명령의 stderr 를 받으면 종료 코드가
            0 이어도 오류로 처리돼 **스크립트가 거기서 멈춘다.**
         실제로 그 때문에 종료는 다 해 놓고 「포트가 풀렸는지」 확인 단계를
         건너뛴 적이 있다. 확인 못 한 종료는 종료가 아니다.

      ⚠️★ 2026-09-03 — `Process`는 `Get-UgProcess`가 돌려준 스냅샷
         객체(ProcessId·CommandLine·CreationDate 포함) 그대로 넘겨야
         한다. 여기서 다시 `Test-UgSameProcess`로 확인해, 스냅샷 이후
         그 PID가 이미 사라지고 재사용됐으면 죽이지 않는다 — 무관한
         프로세스를 잘못 죽이는 사고가 "죽여야 할 프로세스를 못 찾는
         것"보다 훨씬 위험하다.
    #>
    param($Process)

    $ProcessId = $Process.ProcessId
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $false }
    if (-not (Test-UgSameProcess $Process)) {
        Write-Host "  SKIP - PID $ProcessId 는 확인 시점 사이 이미 사라졌습니다(재사용 위험 회피, 건드리지 않음)."
        return $false
    }
    # cmd 안에서 삼켜 stderr 가 PowerShell 로 올라오지 않게 한다.
    & $env:ComSpec /c "taskkill /PID $ProcessId /T /F >nul 2>&1"
    return $true
}

function Test-UgPort {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

function Wait-UgPort {
    param([bool]$WantListening, [int]$TimeoutSec = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ((Test-UgPort) -eq $WantListening) { return $true }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

# --- install ----------------------------------------------------------------

function Invoke-Install {
    Write-Head "작업 스케줄러 등록 — $TaskName"

    if (-not (Test-Path $RunCmd)) { throw "기동 스크립트가 없습니다: $RunCmd" }

    if (Get-UgTask) {
        Write-Warn2 '같은 이름의 작업이 이미 있어 지우고 다시 만듭니다.'
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $action = New-ScheduledTaskAction -Execute $env:ComSpec `
        -Argument "/c `"$RunCmd`" $Port" -WorkingDirectory $Root

    # 로그온 시 기동. 부팅 시(ONSTART)는 관리자 권한이 필요해 쓸 수 없다.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -Hidden `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    # ExecutionTimeLimit 0 = 시간 제한 없음. 기본값(3일)이면 사흘 뒤 관제가
    # 조용히 멎는다 — 가장 알아채기 어려운 고장이다.
    $settings.DisallowStartIfOnBatteries = $false

    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "UrbanGuard 통합 도시안전 관제 (포트 $Port) — 로그온 시 자동 기동" | Out-Null

    Write-Ok "등록했습니다. 로그온하면 자동으로 뜹니다 (포트 $Port)."
    Write-Warn2 '재부팅 후에는 **로그인해야** 뜹니다 — 무인 서버라면 -Action guide 를 보십시오.'
}

function Invoke-Uninstall {
    Write-Head "작업 스케줄러 등록 해제 — $TaskName"
    Invoke-Stop
    if (Get-UgTask) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok '작업을 지웠습니다.'
    }
    else {
        Write-Warn2 '등록된 작업이 없습니다.'
    }
}

# --- start / stop -----------------------------------------------------------

function Invoke-EnsurePostgres {
    <#
      PostgreSQL은 Windows 서비스가 아니라 pg_ctl 로 띄운 프로세스라, 콘솔을
      닫거나 절전에서 돌아오거나 재부팅하면 조용히 사라진다(docs\pending_tasks.md
      1-1, 2026-08-19 최초 발견 · 2026-08-21 재발). 안 보이면 여기서 직접
      띄운다 — 실제 로직은 scripts\ensure-postgres.ps1 하나에만 있고, 작업
      스케줄러가 부르는 urbanguard-run.cmd 도 같은 스크립트를 거친다. 여기서
      또 부르는 것은 **터미널에 바로 보이는 안내**를 위해서다 — 실패해도
      run.cmd 쪽에서 다시 확인하므로 중복 실행은 해롭지 않다.

      ⚠ powershell 을 직접 "& ... 2>&1" 로도, PowerShell 의 자체
        "Start-Process -RedirectStandardOutput/-RedirectStandardError" 로도
        부르지 않는다 — 겉모양은 파일 리다이렉트처럼 보여도, 두 방식 다
        내부적으로는 "자식의 출력이 끝(EOF)날 때까지" 기다리는 관로(파이프)를
        쓴다. ensure-postgres.ps1 이 실제로 pg_ctl start 를 해야 하는 경우
        (=PostgreSQL이 꺼져 있던 경우), 그 안에서 새로 뜬 postgres.exe 가
        그 관로의 쓰기핸들을 물려받아 버린다. postgres 는 서비스가 살아있는
        내내 안 죽으므로, 그 EOF 를 기다리는 쪽이 postgres 가 죽을 때까지
        영원히 멈춘다 — PostgreSQL 자체는 정상 기동했는데도 여기서 응답 없이
        멈추는 버그로 실제 재현됐다(2026-08-22, -Action restart 에서 두
        방식 모두로 재현 확인).

        대신 **cmd.exe 의 ">>" 리다이렉트**를 한 단계 거쳐서 부른다 —
        urbanguard-run.cmd 가 이미 같은 방식으로 이 문제를 피해 간다
        (2026-08-21). cmd.exe 의 ">>" 는 그 명령 하나만을 위해 파일을 새로
        열어 붙여 쓸 뿐, "다 쓸 때까지 읽어서 기다리는" 관로가 아니다 —
        cmd.exe 자신은 그 자식(powershell)이 끝나는 것만 보고 넘어가고,
        postgres 가 나중에 그 파일 핸들을 물려받아도 "이미 쓰기가 끝난
        파일을 나중에 읽는 것"은 걸리지 않는다(pg-console.log 를 postgres
        가 쓰는 중에도 Get-Content 로 계속 문제없이 읽어 온 것과 같은
        이유).

      ⚠ "Start-Process -Wait" 도 쓰지 않는다 — 겪어 보니 이것도 문제였다
        (2026-08-22, 파일 리다이렉트로 바꾼 뒤에도 여전히 재현됨). cmd.exe
        는 이미 정상 종료돼 프로세스 목록에서 사라졌는데도 "-Wait" 는 계속
        안 풀렸다 — 직계 자식 하나의 종료가 아니라, 그 아래서 파생된 전체
        프로세스 트리(잡 오브젝트로 추정)가 다 빌 때까지 기다리는 것으로
        보인다. postgres 는 서비스가 살아있는 내내 그 트리에 남아 있으므로
        똑같이 영원히 안 풀린다. 대신 ".NET Process 객체의
        WaitForExit(timeout)" 를 직접 쓴다 — 이건 "그 PID 하나"의 프로세스
        핸들만 보고 기다리는 원초적인 Win32 대기라, 자식이 무엇을 더
        파생시켰는지와 무관하다.
    #>
    Write-Head 'PostgreSQL 확인'
    $script = Join-Path $Root 'scripts\ensure-postgres.ps1'
    $logFile = Join-Path $Root 'data\logs\ensure-postgres-console.log'
    $before = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

    # ⚠️ 명령줄을 문자열로 만들어 "-ArgumentList '/c', $cmdArg" 로 넘기지
    #   않는다 — Start-Process 가 그 문자열을 다시 인용부호로 감싸 재구성
    #   하는데, 경로에 공백이 있으면(이 PC의 사용자 폴더가 한글이라 공백이
    #   섞여 있다) 그 재구성이 깨져서 cmd.exe 가 받는 실제 명령이 우리가
    #   쓴 것과 달라진다 — 실제로 겪었다(2026-08-22): ensure-postgres.ps1
    #   은 화면에 "OK" 를 찍었는데도 종료코드는 실패로 잡혔다. 대신 배치
    #   파일 "내용"에 그대로 적어서(문자열 보간, 재인용 없음) cmd.exe 가
    #   원래 뜻대로 그대로 읽게 한다.
    # ⚠️ 이 임시 파일들을 [System.IO.Path]::GetTempPath() (=%TEMP%, 이 PC에서는
    #   사용자 프로필 밑이라 경로에 "전태건" 같은 한글이 들어간다) 에 두지
    #   않는다. 바로 아래에서 이 경로를 "-Encoding ASCII" 로 배치파일 내용에
    #   그대로 적는데, ASCII 는 한글을 표현할 수 없어 각 글자가 "?" 로
    #   깨진다 — 실제로 겪었다(2026-08-22): 파일 경로가 "...\???\...\*.rc"
    #   가 돼 버려서 cmd.exe 가 "The filename, directory name, or volume
    #   label syntax is incorrect." 를 냈다. $Root 밑(=D:\dev-PoC\UrbanGuard\...)
    #   은 이 저장소 경로 자체가 순수 ASCII 라 이 문제가 없다.
    $tmpDir = Join-Path $Root 'data\logs'
    $tmpBat = Join-Path $tmpDir "ensure-pg-$PID.tmp.cmd"
    $rcFile = Join-Path $tmpDir "ensure-pg-$PID.tmp.rc"
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    Set-Content -Path $tmpBat -Encoding ASCII -Value @(
        '@echo off'
        "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$logFile`" 2>&1"
        "echo %ERRORLEVEL% > `"$rcFile`""
    )
    # ⚠️ .cmd 파일을 "-FilePath $tmpBat" 로 곧바로 넘기지 않고 cmd.exe 를
    #   "-FilePath $env:ComSpec -ArgumentList '/c', $tmpBat" 로 명시한다 —
    #   경로 하나만 넘기는 인자라 재인용 문제는 없다. .cmd 를 파일 확장자
    #   연결로 그냥 띄우면(=cmd.exe 를 명시하지 않으면) 그 실행 방식과
    #   무관하게 아래에서 설명하는 ExitCode 문제가 똑같이 생겼다.
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -NoNewWindow -PassThru
    $exited = $p.WaitForExit(60000)
    Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue
    if (-not $exited) {
        Write-Bad "PostgreSQL 확인이 60초 안에 끝나지 않았습니다 — data\logs\ensure-postgres-console.log 를 확인하십시오."
        return $false
    }
    # ⚠️ "$p.ExitCode" 를 믿지 않는다 — "-Wait" 대신 우리가 직접 WaitForExit
    #   를 부르는 이 조합에서, ExitCode 를 읽으면 실제로는 $null 이 나오는
    #   경우를 겪었다(2026-08-22) — 화면·로그에는 "OK" 가 찍혔는데도
    #   "$rc -ne 0" 이 늘 참이었다("$null -ne 0" 은 참이라서). 그래서 배치
    #   파일이 자기 종료코드를 직접 파일에 적게 하고 그 파일을 읽는다 —
    #   .NET Process 객체의 속성이 아니라 cmd.exe 자신이 쓴 값이라 믿을 수
    #   있다.
    $rc = if (Test-Path $rcFile) { [int]((Get-Content $rcFile -Raw).Trim()) } else { -1 }
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $logFile) {
        $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($before, 'Begin') | Out-Null
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $reader.EndOfStream) { Write-Host "  $($reader.ReadLine())" }
        }
        finally { $stream.Close() }
    }
    if ($rc -ne 0) {
        Write-Bad 'PostgreSQL을 띄우지 못했습니다 — 위 메시지와 data\logs\pg-console.log 를 확인하십시오.'
        return $false
    }
    return $true
}

function Invoke-EnsureMediaMTX {
    <#
      CCTV 재배포 허브(MediaMTX, 2026-08-28 신설). `Invoke-EnsurePostgres`와
      완전히 같은 이유로 같은 방식을 그대로 따른다 — `ensure-mediamtx.ps1`도
      필요하면 새 장기 실행 프로세스(mediamtx.exe)를 띄우는데, 이 함수가
      단순히 "& powershell ... 2>&1" 로 그 스크립트를 부르면 그 프로세스가
      살아 있는 내내 이 호출이 응답 없이 멈출 위험을 배제할 수 없다
      (`ensure-mediamtx.ps1`은 mediamtx.exe 를 Start-Process 로 띄워 이
      위험을 이미 피했지만, 이 저장소가 postgres 쪽에서 두 가지 "당연해
      보이는" 방법이 실제로는 안 되는 것을 겪은 뒤라 — 같은 안전한 패턴을
      그대로 재사용해 확신 없는 판단에 기대지 않는다).

      재배포 기능 자체(`restream.enabled`)가 꺼져 있어도 이 함수는 항상
      MediaMTX 프로세스를 띄운다 — 포트만 열어 두는 것은 무해하고, 나중에
      관리자가 설정을 켜는 순간 곧바로 쓸 수 있어야 한다.
    #>
    Write-Head 'CCTV 재배포 허브(MediaMTX) 확인'
    $script = Join-Path $Root 'scripts\ensure-mediamtx.ps1'
    $logFile = Join-Path $Root 'data\logs\ensure-mediamtx-console.log'
    $before = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

    $tmpDir = Join-Path $Root 'data\logs'
    $tmpBat = Join-Path $tmpDir "ensure-mtx-$PID.tmp.cmd"
    $rcFile = Join-Path $tmpDir "ensure-mtx-$PID.tmp.rc"
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    Set-Content -Path $tmpBat -Encoding ASCII -Value @(
        '@echo off'
        "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$logFile`" 2>&1"
        "echo %ERRORLEVEL% > `"$rcFile`""
    )
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -NoNewWindow -PassThru
    $exited = $p.WaitForExit(30000)
    Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue
    if (-not $exited) {
        Write-Bad "MediaMTX 확인이 30초 안에 끝나지 않았습니다 — data\logs\ensure-mediamtx-console.log 를 확인하십시오."
        return $false
    }
    $rc = if (Test-Path $rcFile) { [int]((Get-Content $rcFile -Raw).Trim()) } else { -1 }
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $logFile) {
        $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($before, 'Begin') | Out-Null
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $reader.EndOfStream) { Write-Host "  $($reader.ReadLine())" }
        }
        finally { $stream.Close() }
    }
    if ($rc -ne 0) {
        # ⚠️ 이건 PostgreSQL과 달리 **막지 않는다** — 재배포는 부가 기능이라,
        #   MediaMTX가 없어도 기존 방식(원본 CCTV 직결)대로 서비스는 정상
        #   기동한다. 재배포를 켜 둔 채 MediaMTX가 없으면 그 카메라들만
        #   "관측 없음"으로 정직하게 표시될 뿐이다(폴백 없음 정책).
        Write-Warn2 'MediaMTX를 띄우지 못했습니다 — 재배포 기능 없이 계속합니다(원본 CCTV 직결).'
        return $false
    }
    return $true
}

function Invoke-EnsureNginx {
    <#
      API 게이트웨이(nginx, 2026-08-31 신설, Phase 0). `Invoke-EnsureMediaMTX`
      와 완전히 같은 이유로 같은 방식을 그대로 따른다 — `ensure-nginx.ps1`도
      장기 실행 프로세스(nginx.exe)를 띄우므로, 같은 안전한 임시 배치파일
      경유 호출 패턴을 재사용한다.

      게이트웨이가 없어도 앱 자체(포트 8033)는 그대로 접속 가능하므로
      (Phase 0은 앞단에 프록시만 세울 뿐 8033 직접 접속을 막지 않는다),
      MediaMTX와 마찬가지로 실패해도 기동을 막지 않는다.
    #>
    Write-Head 'API 게이트웨이(nginx) 확인'
    $script = Join-Path $Root 'scripts\ensure-nginx.ps1'
    $logFile = Join-Path $Root 'data\logs\ensure-nginx-console.log'
    $before = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

    $tmpDir = Join-Path $Root 'data\logs'
    $tmpBat = Join-Path $tmpDir "ensure-nginx-$PID.tmp.cmd"
    $rcFile = Join-Path $tmpDir "ensure-nginx-$PID.tmp.rc"
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    Set-Content -Path $tmpBat -Encoding ASCII -Value @(
        '@echo off'
        "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$logFile`" 2>&1"
        "echo %ERRORLEVEL% > `"$rcFile`""
    )
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -NoNewWindow -PassThru
    $exited = $p.WaitForExit(15000)
    Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue
    if (-not $exited) {
        Write-Bad "게이트웨이 확인이 15초 안에 끝나지 않았습니다 — data\logs\ensure-nginx-console.log 를 확인하십시오."
        return $false
    }
    $rc = if (Test-Path $rcFile) { [int]((Get-Content $rcFile -Raw).Trim()) } else { -1 }
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $logFile) {
        $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($before, 'Begin') | Out-Null
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $reader.EndOfStream) { Write-Host "  $($reader.ReadLine())" }
        }
        finally { $stream.Close() }
    }
    if ($rc -ne 0) {
        Write-Warn2 '게이트웨이(nginx)를 띄우지 못했습니다 — 게이트웨이 없이 계속합니다(앱 포트로 직접 접속 가능).'
        return $false
    }
    return $true
}

function Invoke-EnsureCrowdService {
    <#
      인파관리 독립 서비스(crowd-service, 2026-08-31 신설, Phase 1).
      `Invoke-EnsureMediaMTX`/`Invoke-EnsureNginx`와 완전히 같은 이유로
      같은 방식(임시 배치파일 경유)을 그대로 따른다. DB에 붙으므로
      PostgreSQL이 먼저 확인된 뒤에만 불러야 한다(Invoke-Start 순서 참고).

      실패해도 platform-shell 기동을 막지 않는다 — 인파관리가 없어도
      나머지 3개 도메인은 정상 동작해야 한다.
    #>
    Write-Head '인파관리 서비스(crowd-service) 확인'
    $script = Join-Path $Root 'scripts\ensure-crowd-service.ps1'
    $logFile = Join-Path $Root 'data\logs\ensure-crowd-service-console.log'
    $before = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

    $tmpDir = Join-Path $Root 'data\logs'
    $tmpBat = Join-Path $tmpDir "ensure-crowd-$PID.tmp.cmd"
    $rcFile = Join-Path $tmpDir "ensure-crowd-$PID.tmp.rc"
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    Set-Content -Path $tmpBat -Encoding ASCII -Value @(
        '@echo off'
        "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$logFile`" 2>&1"
        "echo %ERRORLEVEL% > `"$rcFile`""
    )
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -NoNewWindow -PassThru
    $exited = $p.WaitForExit(40000)
    Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue
    if (-not $exited) {
        Write-Bad "인파관리 서비스 확인이 40초 안에 끝나지 않았습니다 — data\logs\ensure-crowd-service-console.log 를 확인하십시오."
        return $false
    }
    $rc = if (Test-Path $rcFile) { [int]((Get-Content $rcFile -Raw).Trim()) } else { -1 }
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $logFile) {
        $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($before, 'Begin') | Out-Null
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $reader.EndOfStream) { Write-Host "  $($reader.ReadLine())" }
        }
        finally { $stream.Close() }
    }
    if ($rc -ne 0) {
        Write-Warn2 '인파관리 서비스를 띄우지 못했습니다 — 인파관리 없이 계속합니다(나머지 도메인은 정상 동작).'
        return $false
    }
    return $true
}

function Invoke-EnsureRoadService {
    <#
      노면관리 독립 서비스(road-service, 2026-08-31 신설, Phase 2).
      `Invoke-EnsureCrowdService`와 완전히 같은 이유·같은 방식(임시
      배치파일 경유). 실패해도 platform-shell 기동을 막지 않는다.
    #>
    Write-Head '노면관리 서비스(road-service) 확인'
    $script = Join-Path $Root 'scripts\ensure-road-service.ps1'
    $logFile = Join-Path $Root 'data\logs\ensure-road-service-console.log'
    $before = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

    $tmpDir = Join-Path $Root 'data\logs'
    $tmpBat = Join-Path $tmpDir "ensure-road-$PID.tmp.cmd"
    $rcFile = Join-Path $tmpDir "ensure-road-$PID.tmp.rc"
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    Set-Content -Path $tmpBat -Encoding ASCII -Value @(
        '@echo off'
        "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$logFile`" 2>&1"
        "echo %ERRORLEVEL% > `"$rcFile`""
    )
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -NoNewWindow -PassThru
    $exited = $p.WaitForExit(40000)
    Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue
    if (-not $exited) {
        Write-Bad "노면관리 서비스 확인이 40초 안에 끝나지 않았습니다 — data\logs\ensure-road-service-console.log 를 확인하십시오."
        return $false
    }
    $rc = if (Test-Path $rcFile) { [int]((Get-Content $rcFile -Raw).Trim()) } else { -1 }
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $logFile) {
        $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($before, 'Begin') | Out-Null
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $reader.EndOfStream) { Write-Host "  $($reader.ReadLine())" }
        }
        finally { $stream.Close() }
    }
    if ($rc -ne 0) {
        Write-Warn2 '노면관리 서비스를 띄우지 못했습니다 — 노면관리 없이 계속합니다(나머지 도메인은 정상 동작).'
        return $false
    }
    return $true
}

function Invoke-EnsureFloodService {
    <#
      침수 독립 서비스(flood-service, 2026-08-31 신설, Phase 4).
      `Invoke-EnsureRoadService`와 완전히 같은 이유·같은 방식. 실패해도
      platform-shell 기동을 막지 않는다.
    #>
    Write-Head '침수 서비스(flood-service) 확인'
    $script = Join-Path $Root 'scripts\ensure-flood-service.ps1'
    $logFile = Join-Path $Root 'data\logs\ensure-flood-service-console.log'
    $before = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

    $tmpDir = Join-Path $Root 'data\logs'
    $tmpBat = Join-Path $tmpDir "ensure-flood-$PID.tmp.cmd"
    $rcFile = Join-Path $tmpDir "ensure-flood-$PID.tmp.rc"
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    Set-Content -Path $tmpBat -Encoding ASCII -Value @(
        '@echo off'
        "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$logFile`" 2>&1"
        "echo %ERRORLEVEL% > `"$rcFile`""
    )
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -NoNewWindow -PassThru
    # ⚠️ 침수 서비스는 상시 카메라마다 YOLO 모델+실제 스트림 접속을 모듈
    # 임포트 시점에 동기로 한다 — crowd/road-service보다 느리다(실측
    # 30초 초과). 안쪽 ensure-flood-service.ps1 자체 타임아웃(90초)보다
    # 넉넉히 잡는다.
    $exited = $p.WaitForExit(100000)
    Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue
    if (-not $exited) {
        Write-Bad "침수 서비스 확인이 100초 안에 끝나지 않았습니다 — data\logs\ensure-flood-service-console.log 를 확인하십시오."
        return $false
    }
    $rc = if (Test-Path $rcFile) { [int]((Get-Content $rcFile -Raw).Trim()) } else { -1 }
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $logFile) {
        $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($before, 'Begin') | Out-Null
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $reader.EndOfStream) { Write-Host "  $($reader.ReadLine())" }
        }
        finally { $stream.Close() }
    }
    if ($rc -ne 0) {
        Write-Warn2 '침수 서비스를 띄우지 못했습니다 — 침수 없이 계속합니다(나머지 도메인은 정상 동작).'
        return $false
    }
    return $true
}

function Invoke-EnsureTrafficService {
    <#
      교통위험 독립 서비스(traffic-service, 2026-08-31 신설, Phase 4).
      `Invoke-EnsureFloodService`와 완전히 같은 패턴.
    #>
    Write-Head '교통위험 서비스(traffic-service) 확인'
    $script = Join-Path $Root 'scripts\ensure-traffic-service.ps1'
    $logFile = Join-Path $Root 'data\logs\ensure-traffic-service-console.log'
    $before = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

    $tmpDir = Join-Path $Root 'data\logs'
    $tmpBat = Join-Path $tmpDir "ensure-traffic-$PID.tmp.cmd"
    $rcFile = Join-Path $tmpDir "ensure-traffic-$PID.tmp.rc"
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    Set-Content -Path $tmpBat -Encoding ASCII -Value @(
        '@echo off'
        "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$logFile`" 2>&1"
        "echo %ERRORLEVEL% > `"$rcFile`""
    )
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $tmpBat -NoNewWindow -PassThru
    # ⚠️ Invoke-EnsureFloodService와 같은 이유 — 교통은 보통 상시 카메라가
    # 더 많아(실측 8개소) 더 오래 걸린다.
    $exited = $p.WaitForExit(130000)
    Remove-Item $tmpBat -Force -ErrorAction SilentlyContinue
    if (-not $exited) {
        Write-Bad "교통위험 서비스 확인이 130초 안에 끝나지 않았습니다 — data\logs\ensure-traffic-service-console.log 를 확인하십시오."
        return $false
    }
    $rc = if (Test-Path $rcFile) { [int]((Get-Content $rcFile -Raw).Trim()) } else { -1 }
    Remove-Item $rcFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $logFile) {
        $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($before, 'Begin') | Out-Null
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $reader.EndOfStream) { Write-Host "  $($reader.ReadLine())" }
        }
        finally { $stream.Close() }
    }
    if ($rc -ne 0) {
        Write-Warn2 '교통위험 서비스를 띄우지 못했습니다 — 교통위험 없이 계속합니다(나머지 도메인은 정상 동작).'
        return $false
    }
    return $true
}

function Invoke-Start {
    Write-Head "기동 — 포트 $Port"

    # ⚠️ 2026-09-02 실사용 중 발견한 사고 — 예전엔 플랫폼-쉘 포트($Port)가
    # 이미 열려 있으면 여기서 곧바로 `return`해, 아래 PostgreSQL·MediaMTX·
    # 도메인 4개·nginx 확인을 **전혀** 안 거쳤다. 2026-08-31 API 게이트웨이
    # 도입 전(단일 프로세스 시절)에는 "플랫폼-쉘이 떴다 = 전부 떴다"가
    # 맞았지만, 지금은 5개가 별도 프로세스라 그 전제가 깨졌다 — 관리자가
    # 도메인 서비스만 따로 정지시킨 뒤 `urbanguard-start.cmd`를 다시
    # 돌려도(플랫폼-쉘은 이미 떠 있으니) 그 서비스들이 전혀 재확인되지
    # 않는 사고를 실제로 겪었다. 아래 확인들은 전부 자기 스스로 멱등
    # (이미 떠 있으면 바로 넘어감)이라 플랫폼-쉘 상태와 무관하게 항상
    # 돌려도 안전하다 — "플랫폼-쉘 자신을 새로 띄울지"만 뒤에서 따로 본다.
    if (-not (Invoke-EnsurePostgres)) {
        Write-Bad 'PostgreSQL 없이는 기동해도 매 요청이 타임아웃됩니다 — 기동을 멈춥니다.'
        return
    }

    # ⚠️ PostgreSQL과 달리 실패해도 기동을 막지 않는다 — 위 함수 안 주석 참고.
    Invoke-EnsureMediaMTX | Out-Null
    Invoke-EnsureCrowdService | Out-Null
    Invoke-EnsureRoadService | Out-Null
    Invoke-EnsureFloodService | Out-Null
    Invoke-EnsureTrafficService | Out-Null
    Invoke-EnsureNginx | Out-Null

    if (Test-UgPort) { Write-Warn2 "포트 $Port 가 이미 열려 있습니다. 플랫폼-쉘은 그대로 둡니다."; return }

    if (Get-UgTask) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host '  작업으로 띄웠습니다. 모델을 읽는 동안 기다립니다...'
    }
    else {
        Write-Warn2 '등록된 작업이 없어 스크립트를 직접 띄웁니다.'
        Start-Process -FilePath $env:ComSpec -ArgumentList "/c", "`"$RunCmd`"", "$Port" `
            -WorkingDirectory $Root -WindowStyle Hidden
    }

    if (Wait-UgPort -WantListening $true) { Write-Ok "떴습니다 — http://127.0.0.1:$Port" }
    else { Write-Bad '시간 안에 뜨지 않았습니다. data\logs\serve-console.log 를 보십시오.' }
}

function Invoke-Stop {
    Write-Head '종료 — 작업과 프로세스를 모두 끝냅니다'

    # ① 작업 자체를 끝낸다. 이걸 빼면 작업이 「실행 중」으로 남아, 다음
    #    Start 가 MultipleInstances=IgnoreNew 때문에 조용히 무시된다.
    if (Get-UgTask) {
        try {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            Write-Ok '작업을 끝냈습니다.'
        }
        catch { Write-Host '  (작업은 실행 중이 아니었습니다)' }
    }
    else { Write-Host '  (등록된 작업 없음 — 프로세스만 정리합니다)' }

    # ② ★ 감시를 **먼저** 죽인다. 순서를 바꾸면 자식이 5초 뒤 되살아난다.
    #    venv 의 python.exe 는 기본 인터프리터를 자식으로 다시 띄우는 껍데기라
    #    한 단계가 둘로 보인다. /T 로 바깥쪽을 잡으면 안쪽도 함께 죽는다.
    $killed = 0
    foreach ($p in (Get-UgProcess -SupervisorOnly)) {
        if (Stop-UgTree -Process $p) {
            Write-Host "  감시 종료 PID $($p.ProcessId)"
            $killed++
        }
    }

    # ③ 감시 없이 홀로 남은 자식이 있으면 정리한다.
    Start-Sleep -Milliseconds 500
    foreach ($p in (Get-UgProcess -ChildOnly)) {
        if (Stop-UgTree -Process $p) {
            Write-Host "  남은 서비스 종료 PID $($p.ProcessId)"
            $killed++
        }
    }
    if ($killed -eq 0) { Write-Host '  (돌고 있는 프로세스가 없었습니다)' }

    # ④ 「끝냈다」고 말하기 전에 실제로 포트가 풀렸는지 본다.
    if (Wait-UgPort -WantListening $false -TimeoutSec 30) {
        Write-Ok "포트 $Port 가 풀렸습니다."
    }
    else {
        Write-Bad "포트 $Port 가 아직 열려 있습니다. -Action status 로 확인하십시오."
    }

    $left = Get-UgProcess
    if ($left.Count -gt 0) { Write-Bad "남은 프로세스 $($left.Count)개 — PID $($left.ProcessId -join ', ')" }
    else { Write-Ok '남은 프로세스가 없습니다.' }

    # ⑤ PostgreSQL도 함께 내린다 — start 쪽에서 자동으로 띄우게 만든 것과
    #    짝을 맞춘다(2026-08-26). -KeepDb 를 주면 건너뛴다.
    if ($KeepDb) {
        Write-Host '  (PostgreSQL은 -KeepDb 로 그대로 둡니다)'
    }
    else {
        Write-Head 'PostgreSQL 종료'
        $script = Join-Path $Root 'scripts\stop-postgres.ps1'
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        $out | ForEach-Object { Write-Host "  $_" }
    }

    # ⑥ CCTV 재배포 허브(MediaMTX)도 함께 내린다 — PostgreSQL과 같은 층위
    #    (2026-08-28). `stop-mediamtx.ps1`은 기존에 떠 있는 프로세스에
    #    신호만 보낼 뿐 새 장기 실행 프로세스를 만들지 않으므로,
    #    `stop-postgres.ps1`과 같은 단순한 호출 방식으로 충분하다.
    if ($KeepMediaMTX) {
        Write-Host '  (MediaMTX는 -KeepMediaMTX 로 그대로 둡니다)'
    }
    else {
        Write-Head 'CCTV 재배포 허브(MediaMTX) 종료'
        $script = Join-Path $Root 'scripts\stop-mediamtx.ps1'
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        $out | ForEach-Object { Write-Host "  $_" }
    }

    # ⑦ API 게이트웨이(nginx)도 함께 내린다 — MediaMTX와 같은 층위
    #    (2026-08-31, Phase 0). `stop-nginx.ps1`도 기존에 떠 있는 프로세스에
    #    신호만 보낼 뿐 새 장기 실행 프로세스를 만들지 않으므로, 같은 단순한
    #    호출 방식으로 충분하다.
    if ($KeepNginx) {
        Write-Host '  (게이트웨이는 -KeepNginx 로 그대로 둡니다)'
    }
    else {
        Write-Head 'API 게이트웨이(nginx) 종료'
        $script = Join-Path $Root 'scripts\stop-nginx.ps1'
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        $out | ForEach-Object { Write-Host "  $_" }
    }

    # ⑧ 인파관리 서비스(crowd-service)도 함께 내린다(2026-08-31, Phase 1).
    if ($KeepCrowdService) {
        Write-Host '  (인파관리 서비스는 -KeepCrowdService 로 그대로 둡니다)'
    }
    else {
        Write-Head '인파관리 서비스(crowd-service) 종료'
        $script = Join-Path $Root 'scripts\stop-crowd-service.ps1'
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        $out | ForEach-Object { Write-Host "  $_" }
    }

    # ⑨ 노면관리 서비스(road-service)도 함께 내린다(2026-08-31, Phase 2).
    if ($KeepRoadService) {
        Write-Host '  (노면관리 서비스는 -KeepRoadService 로 그대로 둡니다)'
    }
    else {
        Write-Head '노면관리 서비스(road-service) 종료'
        $script = Join-Path $Root 'scripts\stop-road-service.ps1'
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        $out | ForEach-Object { Write-Host "  $_" }
    }

    # ⑩ 침수 서비스(flood-service)도 함께 내린다(2026-08-31, Phase 4).
    if ($KeepFloodService) {
        Write-Host '  (침수 서비스는 -KeepFloodService 로 그대로 둡니다)'
    }
    else {
        Write-Head '침수 서비스(flood-service) 종료'
        $script = Join-Path $Root 'scripts\stop-flood-service.ps1'
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        $out | ForEach-Object { Write-Host "  $_" }
    }

    # ⑪ 교통위험 서비스(traffic-service)도 함께 내린다(2026-08-31, Phase 4).
    if ($KeepTrafficService) {
        Write-Host '  (교통위험 서비스는 -KeepTrafficService 로 그대로 둡니다)'
    }
    else {
        Write-Head '교통위험 서비스(traffic-service) 종료'
        $script = Join-Path $Root 'scripts\stop-traffic-service.ps1'
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        $out | ForEach-Object { Write-Host "  $_" }
    }
}

# --- status -----------------------------------------------------------------

function Invoke-Status {
    Write-Head "상태 — $TaskName / 포트 $Port"

    # PostgreSQL은 Windows 서비스가 아니라서 여기서 확인해야만 보인다 —
    # 안 보면 "웹 서비스는 떴는데 DB만 없는" 상태를 아무도 모르고 지나간다.
    $script = Join-Path $Root 'scripts\ensure-postgres.ps1'
    if (Test-Path $script) {
        $envFile = Join-Path $Root '.env'
        $pgPort = 5433
        if (Test-Path $envFile) {
            $line = Get-Content $envFile -ErrorAction SilentlyContinue |
                Where-Object { $_ -match '^\s*URBANGUARD_DATABASE_URL\s*=' } | Select-Object -First 1
            if ($line -and ($line -match ':(\d+)/[^/\s]+\s*$')) { $pgPort = [int]$Matches[1] }
        }
        if (Get-NetTCPConnection -LocalPort $pgPort -State Listen -ErrorAction SilentlyContinue) {
            Write-Ok "PostgreSQL 응답 (포트 $pgPort)"
        }
        else {
            Write-Bad "PostgreSQL 응답 없음 (포트 $pgPort) — '-Action start' 가 자동으로 띄웁니다."
        }
    }

    # CCTV 재배포 허브(MediaMTX, 2026-08-28) — 재배포 기능(restream.enabled)
    # 자체는 기본 꺼짐이라 안 떠 있어도 서비스 운영에는 지장이 없지만,
    # "켜 뒀는데 서버가 안 떠 있다"는 상태를 조용히 지나치면 안 된다.
    $mtxApiPort = 9997
    if (Get-NetTCPConnection -LocalPort $mtxApiPort -State Listen -ErrorAction SilentlyContinue) {
        Write-Ok "MediaMTX 응답 (Control API 포트 $mtxApiPort)"
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$mtxApiPort/v3/paths/list" -UseBasicParsing -TimeoutSec 5
            $data = $r.Content | ConvertFrom-Json
            $pathCount = if ($data.items) { $data.items.Count } else { 0 }
            Write-Host "  등록된 재배포 경로: ${pathCount}개"
        }
        catch { Write-Warn2 "경로 목록 조회 실패: $($_.Exception.Message)" }
    }
    else {
        Write-Warn2 "MediaMTX 응답 없음 (포트 $mtxApiPort) — 재배포를 쓴다면 '-Action start' 로 띄우십시오. 꺼 두고 있다면 무해합니다."
    }

    # API 게이트웨이(nginx, 2026-08-31, Phase 0) — 앱 포트($Port) 직접
    # 접속도 여전히 가능하므로 안 떠 있어도 서비스 자체는 지장 없지만,
    # 게이트웨이를 거치는 걸 전제로 방화벽·DNS를 바꿔 둔 경우를 위해 알린다.
    $gwPort = 8080
    if (Get-NetTCPConnection -LocalPort $gwPort -State Listen -ErrorAction SilentlyContinue) {
        Write-Ok "API 게이트웨이 응답 (포트 $gwPort)"
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$gwPort/" -UseBasicParsing -TimeoutSec 10
            Write-Ok "게이트웨이 경유 응답 HTTP $($r.StatusCode)"
        }
        catch { Write-Warn2 "게이트웨이는 떠 있는데 경유 응답이 없습니다: $($_.Exception.Message)" }
    }
    else {
        Write-Warn2 "API 게이트웨이 응답 없음 (포트 $gwPort) — '-Action start' 로 띄우십시오. 지금은 앱 포트($Port)로 직접 접속됩니다."
    }

    # 인파관리 독립 서비스(crowd-service, 2026-08-31, Phase 1).
    $crowdPort = 8034
    if (Get-NetTCPConnection -LocalPort $crowdPort -State Listen -ErrorAction SilentlyContinue) {
        Write-Ok "인파관리 서비스 응답 (포트 $crowdPort)"
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$crowdPort/api/health" -UseBasicParsing -TimeoutSec 10
            Write-Ok "인파관리 서비스 헬스체크 HTTP $($r.StatusCode)"
        }
        catch { Write-Warn2 "인파관리 서비스는 떠 있는데 헬스체크 응답이 없습니다: $($_.Exception.Message)" }
    }
    else {
        Write-Warn2 "인파관리 서비스 응답 없음 (포트 $crowdPort) — '-Action start' 로 띄우십시오. 인파관리 화면·API가 동작하지 않습니다."
    }

    # 노면관리 독립 서비스(road-service, 2026-08-31, Phase 2).
    $roadPort = 8035
    if (Get-NetTCPConnection -LocalPort $roadPort -State Listen -ErrorAction SilentlyContinue) {
        Write-Ok "노면관리 서비스 응답 (포트 $roadPort)"
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$roadPort/api/health" -UseBasicParsing -TimeoutSec 10
            Write-Ok "노면관리 서비스 헬스체크 HTTP $($r.StatusCode)"
        }
        catch { Write-Warn2 "노면관리 서비스는 떠 있는데 헬스체크 응답이 없습니다: $($_.Exception.Message)" }
    }
    else {
        Write-Warn2 "노면관리 서비스 응답 없음 (포트 $roadPort) — '-Action start' 로 띄우십시오. 노면관리 화면·API가 동작하지 않습니다."
    }

    # 침수 독립 서비스(flood-service, 2026-08-31, Phase 4).
    $floodPort = 8036
    if (Get-NetTCPConnection -LocalPort $floodPort -State Listen -ErrorAction SilentlyContinue) {
        Write-Ok "침수 서비스 응답 (포트 $floodPort)"
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$floodPort/api/health" -UseBasicParsing -TimeoutSec 10
            Write-Ok "침수 서비스 헬스체크 HTTP $($r.StatusCode)"
        }
        catch { Write-Warn2 "침수 서비스는 떠 있는데 헬스체크 응답이 없습니다: $($_.Exception.Message)" }
    }
    else {
        Write-Warn2 "침수 서비스 응답 없음 (포트 $floodPort) — '-Action start' 로 띄우십시오. 침수 화면·API가 동작하지 않습니다."
    }

    # 교통위험 독립 서비스(traffic-service, 2026-08-31, Phase 4).
    $trafficPort = 8037
    if (Get-NetTCPConnection -LocalPort $trafficPort -State Listen -ErrorAction SilentlyContinue) {
        Write-Ok "교통위험 서비스 응답 (포트 $trafficPort)"
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$trafficPort/api/health" -UseBasicParsing -TimeoutSec 10
            Write-Ok "교통위험 서비스 헬스체크 HTTP $($r.StatusCode)"
        }
        catch { Write-Warn2 "교통위험 서비스는 떠 있는데 헬스체크 응답이 없습니다: $($_.Exception.Message)" }
    }
    else {
        Write-Warn2 "교통위험 서비스 응답 없음 (포트 $trafficPort) — '-Action start' 로 띄우십시오. 교통위험 화면·API가 동작하지 않습니다."
    }

    $t = Get-UgTask
    if ($t) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "  작업        : 등록됨 · 상태 $($t.State)"
        Write-Host "  마지막 실행 : $($info.LastRunTime)  (결과 $($info.LastTaskResult))"
        Write-Host "  다음 실행   : $($info.NextRunTime)"
    }
    else { Write-Warn2 '작업이 등록돼 있지 않습니다 — 재부팅·로그온 시 자동 기동하지 않습니다.' }

    $sup = Get-UgProcess -SupervisorOnly
    $child = Get-UgProcess -ChildOnly
    Write-Host "  감시 프로세스: $($sup.Count)개  $(if ($sup.Count) { '(PID ' + ($sup.ProcessId -join ', ') + ')' })"
    Write-Host "  서비스 프로세스: $($child.Count)개  $(if ($child.Count) { '(PID ' + ($child.ProcessId -join ', ') + ')' })"

    if (Test-UgPort) {
        Write-Ok "포트 $Port 열림"
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 10
            Write-Ok "응답 HTTP $($r.StatusCode)  ($($r.RawContentLength) 바이트)"
        }
        catch { Write-Bad "포트는 열렸는데 응답이 없습니다: $($_.Exception.Message)" }
    }
    else { Write-Warn2 "포트 $Port 닫힘 — 서비스가 돌고 있지 않습니다." }

    $log = Join-Path $Root 'data\logs\serve.log'
    if (Test-Path $log) {
        Write-Host ''
        Write-Host '  --- serve.log 마지막 3줄 ---'
        # ⚠️ -Encoding UTF8 를 빼면 안 된다. serve.log 는 BOM 없는 UTF-8 인데
        # PowerShell 5.1 의 기본값은 ANSI(cp949) 라 한글이 통째로 깨진다 —
        # 장애를 보려고 여는 자리에서 로그를 못 읽으면 없느니만 못하다.
        Get-Content $log -Tail 3 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
    }
}

# --- guide ------------------------------------------------------------------

function Invoke-Guide {
    Write-Head '운영 납품 — 윈도우 서비스(NSSM) 등록'
    Write-Host @"
  지금 등록한 작업 스케줄러는 **로그온 시** 기동입니다. 무인 서버에서는
  로그인하는 사람이 없어 서비스가 뜨지 않습니다. 운영 납품에서는 아래처럼
  윈도우 서비스로 등록하십시오. **관리자 권한이 필요합니다.**

    nssm install UrbanGuard "$Root\.venv\Scripts\python.exe" ^
         "$Root\scripts\serve.py" --host 0.0.0.0 --port $Port
    nssm set UrbanGuard AppDirectory "$Root"
    nssm set UrbanGuard AppExit Default Restart
    nssm set UrbanGuard AppStdout "$Root\data\logs\serve-console.log"
    nssm set UrbanGuard AppStderr "$Root\data\logs\serve-console.log"
    nssm set UrbanGuard AppEnvironmentExtra URBANGUARD_WIN_SERVICE=1
    nssm start UrbanGuard

  ⚠️ 마지막 줄(URBANGUARD_WIN_SERVICE=1)을 빼면 S-87 화면이 감시 여부를
     「확인 불가」로 표시합니다. 확인 못 한 것을 확인한 척하지 않기 때문입니다.
     근거는 src\tot_dashboard\core\server_profile.py 를 보십시오.

  ⚠️ 관리자 권한으로 등록한 뒤에는 이 작업 스케줄러 등록을 지우십시오.
     둘이 같이 돌면 포트를 두고 다툽니다.
       powershell -File scripts\urbanguard-service.ps1 -Action uninstall
"@
}

# --- 실행 -------------------------------------------------------------------

Write-Host "UrbanGuard 서비스 관리 — $Root" -ForegroundColor White

switch ($Action) {
    'install' { Invoke-Install; Invoke-Status }
    'uninstall' { Invoke-Uninstall }
    'start' { Invoke-Start }
    'stop' { Invoke-Stop }
    'restart' { Invoke-Stop; Invoke-Start }
    'status' { Invoke-Status }
    'guide' { Invoke-Guide }
}

Write-Host ''
