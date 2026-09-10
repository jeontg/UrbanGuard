# UrbanGuard 설치 가이드 (처음 설치하시는 분용)

이 문서는 **컴퓨터·개발 경험이 없어도** 따라 할 수 있도록 순서대로,
한 단계씩 작성했습니다. 각 단계마다 "이게 왜 필요한지"와 "제대로 됐는지
확인하는 방법"을 함께 적었습니다. **위에서부터 순서대로만 진행**하고,
중간 단계를 건너뛰지 마세요.

전체 소요 시간은 인터넷 속도에 따라 30분~1시간 정도입니다.

> ⚠️ 이 가이드는 **Windows + PowerShell** 기준입니다. 아래 모든 명령은
> `PowerShell`(파란색 또는 검은색 창, "Windows PowerShell"이라고 검색해서
> 실행)에 입력합니다. 명령 프롬프트(cmd)나 Git Bash에서는 명령 형식이
> 달라 오류가 날 수 있습니다.

---

## 0단계 — 시작하기 전에 (여기서 대부분의 오류가 예방됩니다)

### 0-1. Python 설치 확인

PowerShell을 열고 아래를 입력합니다:
```powershell
python --version
```
`Python 3.12.x`처럼 **3.11 이상 3.14 미만** 버전이 나오면 통과입니다.

- **"python은 인식할 수 없는..." 같은 오류가 나면** → Python이 설치돼
  있지 않은 것입니다. https://www.python.org/downloads/ 에서 설치하되,
  설치 화면 첫 페이지에서 **"Add python.exe to PATH" 체크박스를 반드시
  체크**하고 설치하세요(체크 안 하면 이 오류가 계속 납니다).
- **버전이 3.14 이상이거나 3.10 이하면** → 이 프로젝트가 지원하는
  범위(3.11~3.13)의 Python을 별도로 설치하고, 아래 단계에서 `python`
  대신 그 버전 실행파일의 전체 경로를 쓰세요(예:
  `C:\Users\내이름\AppData\Local\Programs\Python\Python312\python.exe`).

### 0-2. Git 설치 확인

```powershell
git --version
```
`git version 2.x.x`가 나오면 통과. 안 되면 https://git-scm.com/download/win
에서 설치(기본 옵션 그대로 "Next"만 눌러도 됩니다).

### 0-3. PowerShell 스크립트 실행 허용

Windows는 기본적으로 `.ps1` 스크립트 실행을 막아 놓습니다. 이 프로젝트는
설치·운영에 `.ps1` 스크립트를 많이 씁니다. 아래 명령으로 **지금 이
PowerShell 창에서만** 허용합니다(컴퓨터 전체 설정을 바꾸지 않는 안전한
방법입니다):
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
```
이 창을 닫고 새로 열면 다시 입력해야 합니다 — 아래 모든 단계를 **같은
PowerShell 창에서 이어서** 진행하시면 한 번만 하면 됩니다.

### 0-4. 한글 콘솔 출력 깨짐 방지

이 프로젝트는 한글 로그 메시지를 많이 출력하는데, Windows 콘솔 기본
설정과 부딪혀 실행 도중 알 수 없는 오류로 멈추는 경우가 실제로 있었습니다.
미리 방지합니다:
```powershell
$env:PYTHONIOENCODING = "utf-8"
chcp 65001
```
(이것도 이 창에서만 적용됩니다 — 새 창을 열면 다시 입력하세요.)

---

## 1단계 — 코드 받기

원하는 위치(예: `D:\dev`)로 이동한 뒤:
```powershell
git clone https://github.com/jeontg/UrbanGuard.git
cd UrbanGuard
```
**확인**: `dir`을 입력했을 때 `src`, `scripts`, `docs`, `README.md` 등이
보이면 성공입니다.

---

## 2단계 — Python 가상환경 만들고 패키지 설치

가상환경(이 프로젝트 전용 Python 공간)을 만듭니다:
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```
**확인**: 명령 프롬프트 맨 앞에 `(.venv)`가 붙으면 성공입니다. 이후
모든 명령은 **`(.venv)`가 붙어 있는 상태**에서 실행해야 합니다(창을
새로 열면 `.venv\Scripts\Activate.ps1`을 다시 실행해야 합니다).

> **"이 시스템에서 스크립트를 실행할 수 없으므로..." 오류가 나면** →
> 0-3단계를 건너뛰었기 때문입니다. 0-3단계 명령을 먼저 실행하세요.

패키지를 설치합니다(2~5분 정도 걸립니다, 처음이면 더 걸릴 수 있습니다):
```powershell
python -m pip install --upgrade pip
pip install -e ".[dev]"
```
**확인**: 마지막 줄에 `Successfully installed ...`가 나오고 빨간
`ERROR`가 없으면 성공입니다. 다 됐는지 아래로 재확인:
```powershell
pip check
```
아무것도 안 나오면(또는 "No broken requirements found") 정상입니다.

> **`ModuleNotFoundError` 가 나중에 서비스 실행 중 나타나면** → 이
> 단계가 중간에 실패했거나 인터넷이 끊겼을 가능성이 큽니다. 위
> `pip install -e ".[dev]"`를 다시 실행해 보세요(이미 설치된 것은
> 건너뛰고 빠진 것만 채웁니다).

> **다운로드가 너무 느리거나 멈추면** → `torch`(약 200MB 이상)가 원인일
> 수 있습니다. 인터넷 연결을 확인하고 다시 시도하세요. 중단됐다 다시
> 실행해도 이미 받은 것은 다시 안 받습니다.

---

## 3단계 — 포터블 바이너리 배치 (PostgreSQL·MediaMTX·nginx)

기존 개발 PC 담당자에게 아래 4개를 전달받습니다(용량이 커서 git에는
없습니다 — USB, 사내 파일 서버 등으로 전달받으세요):

| 받아야 할 것 | 놓을 위치(이 저장소 기준) |
|---|---|
| `pgsql` 폴더 전체 | `UrbanGuard\.tools\pgsql\` |
| `pg.zip` 파일 | `UrbanGuard\.tools\pg.zip` |
| `mediamtx` 폴더 전체 | `UrbanGuard\.tools\mediamtx\` |
| `nginx` 폴더 전체 | `UrbanGuard\.tools\nginx\` |

`.tools` 폴더가 없으면 새로 만들어서 그 안에 넣습니다. 다 넣은 뒤
**확인**:
```powershell
dir .tools\pgsql\bin\postgres.exe
dir .tools\mediamtx\mediamtx.exe
dir .tools\nginx\nginx.exe
```
세 명령 모두 파일이 있다고 나오면 성공입니다(에러 없이 파일 정보가
출력되면 됩니다).

---

## 4단계 — 환경변수 파일(`.env`) 만들기

```powershell
Copy-Item .env.example .env
```
메모장 등으로 `.env`를 열어 필요한 값을 채웁니다(외부 API 키 등). **다
비워 둬도 서비스는 기본값으로 뜹니다** — 알림·외부 기상/CCTV 연동만
안 될 뿐입니다. 지금 당장은 건너뛰어도 됩니다.

---

## 5단계 — 모델 가중치 파일 배치

`models` 폴더를 만들고 아래 파일을 넣습니다(기존 개발 PC나 담당자에게
받습니다 — git에 없습니다):

| 파일 | 놓을 위치 |
|---|---|
| `yolo11s.pt` | `UrbanGuard\models\yolo11s.pt` |
| `best.pt`(침수) | `UrbanGuard\models\best.pt` |

**확인**:
```powershell
dir models\yolo11s.pt
dir models\best.pt
```

> **모델 파일 없이 일단 진행하고 싶다면**: 이 두 파일이 없어도 6~9단계는
> 진행할 수 있으나, 침수·인파·교통 도메인의 실제 탐지 기능이 오류를 내며
> 시작하지 못할 수 있습니다 — **확인이 필요합니다**(도메인마다 동작이
> 다를 수 있음). 먼저 관리자 화면 로그인까지만 확인하고 싶다면 건너뛰고
> 계속 진행해도 됩니다.

---

## 6단계 — PostgreSQL(데이터베이스) 기동

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ensure-postgres.ps1
```
**확인**: `[ensure-postgres] OK - PostgreSQL is listening on port 5433.`
같은 메시지가 나오면 성공입니다.

> **"이미 5432 포트에서 PostgreSQL이 응답합니다" 같은 메시지가 나오면**
> → 이 컴퓨터에 **이미 다른 PostgreSQL이 설치돼 있는 경우**입니다. 이
> 프로젝트는 반드시 **5433번 포트**를 써야 합니다(기존 것과 충돌하지
> 않도록 일부러 다른 포트를 씁니다). 기존 PostgreSQL은 그대로 두고,
> 이 스크립트가 자기 것(`.tools\pgsql`)을 5433으로 새로 띄우는지
> 확인하세요.

> **최초 1회는 "data directory not found - initializing a new one"이라는
> 메시지와 함께 몇십 초 더 걸립니다 — 정상입니다.** 이때 데이터베이스를
> 새로 만들고 앱 전용 계정(`urbanguard`)까지 자동으로 만듭니다. 두 번째
> 실행부터는 이 과정 없이 바로 뜹니다.

---

## 7단계 — 데이터베이스 표(스키마) 만들기

```powershell
alembic upgrade head
```
**확인**: 여러 줄의 `INFO [alembic...]` 메시지가 나오고 마지막에
오류 없이 끝나면 성공입니다.

> **`ModuleNotFoundError: No module named 'alembic'`가 나면** → 2단계가
> 제대로 안 끝난 것입니다. `(.venv)`가 프롬프트에 붙어 있는지 먼저
> 확인하고(안 붙어 있으면 `.venv\Scripts\Activate.ps1` 재실행), 2단계의
> `pip install -e ".[dev]"`를 다시 실행하세요.

> **DB 접속 오류(`could not connect` 등)가 나면** → 6단계가 제대로 안 된
> 것입니다. 6단계로 돌아가 `ensure-postgres.ps1`이 성공 메시지를 내는지
> 다시 확인하세요.

> **`TypeError: cannot use a string pattern on a bytes-like object`가
> 나면** → 실제로 다른 PC 설치 중 나온 오류입니다. 원인은 그 PC의
> Windows 로캘 때문에 PostgreSQL이 `SQL_ASCII`라는 인코딩으로 초기화돼,
> 파이썬 쪽 라이브러리가 응답을 문자열 대신 바이트로 받아 생기는
> 충돌입니다(코드 결함이었고, 2026-09-11에 이미 고쳤습니다). **`git
> pull`로 최신 코드를 받은 뒤 다시 시도**하면 기존 데이터베이스를 새로
> 만들지 않고도 그대로 해결됩니다. 그래도 나면 `data\logs\pg-console.log`
> 내용과 함께 알려주세요.

---

## 8단계 — 첫 관리자 계정 만들기

```powershell
urbanguard-bootstrap --login-id admin --name 홍길동 --dept 정보통신과
```
`--name`, `--dept`는 원하는 값으로 바꿔도 됩니다. **화면에 임시
비밀번호가 딱 한 번 출력됩니다 — 반드시 메모해 두세요**(다시 볼 방법이
없습니다. 잊으면 `urbanguard-passwd`로 재설정).

---

## 9단계 — 전체 서비스 기동

```powershell
powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action start
```
PostgreSQL → nginx → MediaMTX → platform-shell·인파·노면·침수·교통위험
5개 서비스 순서로 확인·기동합니다. **몇 분 걸릴 수 있습니다**(각 서비스가
AI 모델을 불러오는 데 시간이 필요합니다).

**확인**:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action status
```
5개 서비스가 전부 "실행 중" 비슷한 상태로 나오면 성공입니다.

> **Windows 방화벽이 "이 앱이 네트워크 접근을 허용하시겠습니까?"라고
> 물어보면** → **"허용"을 누르세요.** 거부하면 서비스가 CCTV 영상을
> 못 받아옵니다.

---

## 10단계 — 접속 확인

인터넷 브라우저(크롬 등)를 열고:
```
http://127.0.0.1:8080
```
로그인 화면이 뜨면 **성공입니다.** 8단계에서 만든 계정(`admin` + 메모해
둔 임시 비밀번호)으로 로그인해 보세요. 첫 로그인 시 비밀번호를 새로
정하라고 나올 수 있습니다.

> **로그인 후 화면에 "상시 침수로 지정된 카메라가 없습니다" 같은 안내가
> 보이는 것은 정상입니다** — 새 DB에는 카메라가 하나도 등록돼 있지
> 않습니다. CCTV 관리(S-80) 화면에서 카메라를 등록·지정해야 실제 탐지가
> 시작됩니다.

---

## (선택) 11단계 — 자동 테스트 실행

정말 제대로 됐는지 더 확실히 확인하고 싶다면:
```powershell
pytest
```
숫자가 잔뜩 나오고 마지막에 `passed`가 대부분이면 정상입니다(`skipped`는
문제 없음, `failed`가 많으면 위 단계 중 하나를 다시 확인).

⚠️ 이 명령은 **한 번에 하나만** 실행하세요 — 서비스를 재기동하는 중이거나
다른 `pytest`가 이미 돌고 있으면 엉뚱하게 실패로 보일 수 있습니다.

---

## 문제가 안 풀릴 때 — 로그 확인하는 법

`data\logs\` 폴더 안의 파일을 열어보면 각 부분이 실제로 뭐라고
오류를 냈는지 볼 수 있습니다:

| 로그 파일 | 무엇을 확인할 때 |
|---|---|
| `serve-console.log` | platform-shell(로그인 화면 자체)이 안 뜰 때 |
| `serve-crowd-console.log` | 인파관리 화면 문제 |
| `serve-road-console.log` | 노면관리 화면 문제 |
| `serve-flood-console.log` | 침수 화면 문제 |
| `serve-traffic-console.log` | 교통위험 화면 문제 |
| `ensure-postgres-console.log` | 6단계(DB) 문제 |
| `ensure-mediamtx-console.log` | CCTV 영상이 안 나올 때 |
| `ensure-nginx-console.log` | `http://127.0.0.1:8080` 자체가 안 열릴 때 |

메모장으로 열어서 맨 아래쪽(가장 최근 내용)을 보시면 됩니다.

## 처음부터 다시 하고 싶을 때

여기까지 하다 꼬여서 처음부터 다시 하고 싶다면, `UrbanGuard` 폴더를
통째로 지우고 **1단계부터** 다시 시작하는 것이 가장 확실합니다(단,
`.tools\`와 `models\`는 미리 다른 곳에 복사해 두면 3·5단계를 또 할
필요가 없습니다).

## 개별 서비스만 따로 켜고 싶을 때

```powershell
scripts\urbanguard-run.cmd            # platform-shell(로그인 화면), :8033
scripts\urbanguard-crowd-run.cmd      # 인파관리, :8034
scripts\urbanguard-road-run.cmd       # 노면관리, :8035
scripts\urbanguard-flood-run.cmd      # 침수, :8036
scripts\urbanguard-traffic-run.cmd    # 교통위험, :8037
```
`http://127.0.0.1:8080`(게이트웨이)으로 접속하면, 관리자 화면에서
정지시킨 서비스만 오류로 표시되고 나머지는 정상 동작합니다.

---

더 자세한 아키텍처·프로젝트 구조는 [README.md](README.md)를 참고하세요.
