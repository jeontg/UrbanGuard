# PostgreSQL 자동 확인·기동/종료 로직 추가 — 작업 경위

앤시정보기술(주) · 2026-08-22 08:38

---

## 1. 배경

○ `urbanguard-start.cmd` 실행 시 `psycopg.errors.ConnectionTimeout` 로 서비스가
  뜨지 않는 장애 발생 (2026-08-21) — 원인은 PostgreSQL이 서비스로 등록돼 있지
  않고 `pg_ctl` 로 수동 기동한 프로세스라, 콘솔 종료·절전·재부팅 시 조용히
  죽어 있었기 때문 (`docs/pending_tasks.md` 1-1, 2026-08-19 최초 발견)

○ 사용자 요청
  - 시작 스크립트에 PostgreSQL 자동 확인·기동 로직 추가
  - 종료 스크립트에도 같은 방식으로 PostgreSQL 자동 확인·종료 로직 추가

○ 대안(Windows 서비스로 등록, NSSM)은 외부 바이너리 반입 심의가 선행돼야 해
  이번 범위에서 제외하고 별도 과제(1-3)로 남김 — 이번 작업은 **기존 `pg_ctl`
  방식 위에 "확인 후 필요하면 띄운다/내린다" 로직만 추가**

## 2. 변경 파일

| 파일 | 내용 |
|---|---|
| `scripts/ensure-postgres.ps1` (신규) | `.env`의 포트를 읽어 PostgreSQL 응답 확인, 안 뜨면 `pg_ctl start` |
| `scripts/stop-postgres.ps1` (신규) | 같은 방식으로 확인 후 `pg_ctl -m fast stop` |
| `scripts/urbanguard-run.cmd` | 서비스 기동 직전에 `ensure-postgres.ps1` 실행 (작업 스케줄러·사람 실행 공통 경로) |
| `scripts/urbanguard-service.ps1` | `-Action start/restart` 시 `Invoke-EnsurePostgres` 호출, `-Action stop`(기본, `-KeepDb` 없을 때) 시 `stop-postgres.ps1` 호출, `-Action status`에 PostgreSQL 상태 표시 추가 |

## 3. 검증 중 발견·수정한 문제 (실측 기준, 총 4건)

콜드 스타트(=PostgreSQL이 실제로 내려가 있어 새로 띄워야 하는 경우)를 실제로
재현해야만 드러나는 문제들이라, 코드만 읽어서는 못 찾았고 실행해서 확인함.

**① 동시 호출 경쟁 상태** — 대화형 `-Action start`와 작업 스케줄러의
`urbanguard-run.cmd`가 거의 동시에 `ensure-postgres.ps1`을 부르면, 한쪽이
띄우는 중에 다른 쪽의 `pg_ctl`이 잠금 충돌로 실패를 반환. 실제로는 첫 번째
호출이 정상 진행 중이었음. → **수정**: `pg_ctl` 실패 후 포트가 열리는지 10초
더 기다리는 유예 재확인 추가.

**② 핸들 상속으로 인한 로그 파일 잠금** — Windows에서 자식 프로세스는 부모의
"상속 가능" 표시된 핸들을 그대로 물려받는다. `ensure-postgres.ps1`이 실제로
`pg_ctl start`를 해야 하는 경우, 그 안에서 새로 뜬 `postgres.exe`가 호출자의
표준출력 리다이렉트 핸들까지 물려받아 버림. `postgres.exe`는 서비스가 살아있는
내내 안 죽으므로:
  - 리다이렉트가 파일(`>>`)이면 그 파일에 대한 이후의 다른 쓰기가 전부
    "다른 프로세스가 사용 중"으로 막힘 (`urbanguard-run.cmd`에서 재현)
  - 리다이렉트가 파이프(`2>&1` 캡처)면 호출자가 EOF를 영원히 못 봐서 응답
    없이 멈춤 (`urbanguard-service.ps1 -Action restart`에서 재현 — PostgreSQL
    자체는 정상 기동했는데도 호출자가 멈춰 있었음)
  → **수정**: `ensure-postgres.ps1`의 출력을 서비스 로그(`serve-console.log`)와
    분리된 전용 로그(`ensure-postgres-console.log`)로 보내고, `cmd.exe`의
    `>>` 파일 리다이렉트를 한 단계 거쳐 부름(파이프 캡처 금지) — cmd의 `>>`는
    "다 쓸 때까지 기다리는" 관로가 아니라 그 명령 하나만을 위해 파일을 열어
    붙여 쓰고 끝나므로, 그 자식 프로세스가 나중에 무엇을 더 파생시키든
    호출자가 멈추지 않음.

**③ `Start-Process -Wait`의 잡 오브젝트 대기** — `-Wait`가 직계 자식 하나의
종료가 아니라, 그 아래서 파생된 프로세스 트리 전체가 빌 때까지 기다리는
것으로 보임(실측: cmd.exe는 이미 정상 종료돼 프로세스 목록에서 사라졌는데도
`-Wait` 호출은 계속 안 풀림). `postgres.exe`가 그 트리에 남아있는 한 영원히
안 풀림. → **수정**: `-Wait` 대신 `.NET Process` 객체의 `WaitForExit(timeout)`을
직접 사용 — 지정한 PID 하나의 프로세스 핸들만 보고 기다리는 원초적인 Win32
대기라 자식이 무엇을 더 파생시켰는지와 무관함.

**④ 임시 파일 경로의 한글 사용자명이 ASCII 인코딩으로 손상** — 종료코드를
파일로 전달하려고 만든 임시 파일을 `%TEMP%`(이 PC에서는 사용자 프로필 경로에
한글 "전태건"이 포함)에 두고, 그 경로를 배치파일 내용에 `-Encoding ASCII`로
적었더니 한글이 전부 `?`로 깨져 존재하지 않는 경로가 됨 — cmd.exe가
"The filename, directory name, or volume label syntax is incorrect." 오류.
→ **수정**: 임시 파일을 저장소 경로(`$Root\data\logs\`, 순수 ASCII) 밑에 두도록
변경.

부수적으로 발견: `Start-Process -PassThru`(`-Wait` 없이 `WaitForExit`를 직접
호출하는 조합)에서 `.ExitCode` 속성이 실제로는 `$null`을 반환하는 경우가
있었음(원인 미확인 — PowerShell/.NET 내부 동작으로 추정, ⚠ 추가 확인 필요).
`$null -ne 0`은 참이라 항상 실패로 오판됨 → 배치파일이 자기 종료코드를 직접
파일에 적어 그 값을 읽는 방식으로 우회.

## 4. 검증 결과 (실측)

- `-Action start` : PostgreSQL 콜드 스타트 → 서비스 기동 → HTTP 200 확인
- `-Action stop` : 서비스 종료 → 포트 8033 해제 확인 → PostgreSQL 종료 →
  포트 5433 해제 확인
- `-Action restart` : 위 stop → start 전체 사이클 정상 완료, HTTP 200 확인
- `-Action status` : PostgreSQL 응답 여부·작업 상태·서비스 프로세스·HTTP 응답
  모두 정확히 표시
- `urbanguard-run.cmd` 직접 실행(작업 스케줄러가 실제로 타는 경로) : 콜드
  스타트부터 `Uvicorn running on http://127.0.0.1:8033`까지 정상
- `flood_t` 폴더 `git status --porcelain` 39건 — 작업 전후 무변경 확인
- Python 소스 변경 없음(PowerShell/배치 스크립트만 수정) — pytest 회귀 대상
  없음으로 판단, 별도 실행 생략

## 5. 남은 한계 (⚠ 확인이 필요합니다)

- 이번 수정은 **Windows 서비스 등록이 아닙니다.** 여전히 "누군가 UrbanGuard를
  한 번은 띄웠다"가 전제입니다 — 완전 무인 재부팅 대응은 1-3(NSSM, 반입 심의
  선행 필요)에서 다룹니다.
- ②의 "PowerShell 캡처/리다이렉트가 postgres.exe에 핸들을 물려주는" 현상은
  향후 이 스크립트들을 다시 부를 코드를 추가할 때도 같은 패턴(cmd.exe의 `>>`
  경유, `-Wait` 대신 `WaitForExit`)을 따라야 재발을 막을 수 있습니다 —
  각 파일에 발견 경위를 주석으로 남겨 뒀습니다.
