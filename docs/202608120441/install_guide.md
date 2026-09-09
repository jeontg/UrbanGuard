# UrbanGuard 설치 가이드 v2

> 통합 도시안전 관제 솔루션 · 앤시정보기술(주)
> 작성 2026-08-08 · 갱신 2026-08-12 04:41 · 대상 코드 `D:\dev-PoC\UrbanGuard`
> **개발·시범 환경 기준입니다.** 운영 배포는 8절의 추가 조치가 필요합니다.

---

## 1. 사전 요구사항

### 1-1. 하드웨어 (권장)

| 항목 | 최소 | 권장 | 비고 |
|---|---|---|---|
| CPU | 4코어 | **8코어 이상** | ⚠️ 상시 탐지 지점 수에 비례 |
| 메모리 | 8 GB | **16 GB 이상** | AI 모델이 상주합니다 |
| 디스크 | 50 GB | 200 GB 이상 | 영상·제보 사진·DB·**업로드 동영상(1편 최대 500MB)** |
| GPU | 불필요 | — | **CPU 추론 전제로 설계** |
| 네트워크 | CCTV 스트림 대역 | | 지점당 수 Mbps |

> **⚠️ CPU 요구량은 상시 탐지로 켠 지점 수에 따라 크게 달라집니다.**
> 침수는 지점당 스레드가 초당 5회, 인파는 지점당 스레드가 5초마다 검출을
> 돌립니다. **시범운영 규모를 정하기 전에 실측이 필요합니다**(시험 결과서 5-1절).

### 1-2. 소프트웨어

| 항목 | 버전 | 확인 |
|---|---|---|
| OS | Windows 10/11 또는 Windows Server | 현재 Windows 11에서 검증 |
| **Python** | **3.12** | `python -V` |
| **PostgreSQL** | **17.x** | 현재 17.7로 검증 |
| PowerShell | 5.1 이상 | Windows 기본 |

> **Python 3.13은 검증하지 않았습니다.** 일부 영상 라이브러리의 사전 빌드
> 패키지가 3.13을 지원하지 않아 소스 빌드가 필요할 수 있습니다.

---

## 2. 설치 절차

### 2-1. 소스 배치

프로젝트 폴더를 대상 서버에 복사합니다. 이 문서는 `D:\dev-PoC\UrbanGuard` 를
기준으로 설명합니다.

### 2-2. 가상환경 생성과 의존성 설치

```bash
cd D:\dev-PoC\UrbanGuard; python -m venv .venv
```

```bash
cd D:\dev-PoC\UrbanGuard; .\.venv\Scripts\python.exe -m pip install --upgrade pip
```

```bash
cd D:\dev-PoC\UrbanGuard; .\.venv\Scripts\python.exe -m pip install -e .
```

> PyTorch·OpenCV·Ultralytics를 받으므로 **수 GB, 10분 이상** 걸릴 수 있습니다.

### 2-3. PostgreSQL 준비

**개발 환경**은 프로젝트 안에 로컬 인스턴스(`.tools\pgsql`, 포트 5433)를
두고 있으며, `run.ps1` 이 자동으로 기동합니다.

**운영 환경**은 별도 설치한 PostgreSQL을 쓰고 데이터베이스와 계정을 만듭니다.

```sql
CREATE DATABASE urbanguard ENCODING 'UTF8';
CREATE DATABASE urbanguard_test ENCODING 'UTF8';
CREATE USER urbanguard_app WITH PASSWORD '<강한 비밀번호>';
GRANT ALL PRIVILEGES ON DATABASE urbanguard TO urbanguard_app;
```

> **⚠️ 정렬(collate)** — 현재 `C` 로 운용 중입니다. 한글 이름 정렬이 사전순으로
> 나와야 한다면 `ko_KR.UTF-8` 로 다시 만들어야 하며, **DB 생성 후에는 바꿀 수
> 없습니다.** 발주 사양서를 먼저 확인하세요.

### 2-4. 환경변수 설정

`.env.example` 을 `.env` 로 복사하고 값을 채웁니다.

```bash
cd D:\dev-PoC\UrbanGuard; Copy-Item .env.example .env
```

#### 필수 항목

| 변수 | 설명 |
|---|---|
| `URBANGUARD_DATABASE_URL` | `postgresql+psycopg://user:pw@host:5433/urbanguard` |
| **`URBANGUARD_SECRET_KEY`** | **세션 서명 키. 반드시 고정값 지정** |

> **⚠️ 서명 키를 비워 두면 프로세스마다 새 키가 생겨 재시작할 때마다 전원
> 로그아웃됩니다.** 아래로 생성해 붙여 넣으세요.

```bash
cd D:\dev-PoC\UrbanGuard; .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

#### 선택 항목

| 변수 | 기본값 | 설명 |
|---|---|---|
| `URBANGUARD_COOKIE_SECURE` | 미설정 | **운영에서 `1` 필수** (HTTPS 전제) |
| `URBANGUARD_SESSION_MAX_AGE` | 28800 (8시간) | 세션 유효기간 |
| `URBANGUARD_MAX_FAILED_LOGIN` | 5 | 로그인 실패 잠금 횟수 |
| `URBANGUARD_LOCK_MINUTES` | 10 | 잠금 시간(분) |
| `URBANGUARD_MIN_PASSWORD_LEN` | 9 | 비밀번호 최소 길이 |
| `URBANGUARD_CROWD_INTERVAL` | 5 | 인파 상시 탐지 표본 간격(초) |
| `URBANGUARD_ROAD_PERIOD` | 900 | 노면 상시 탐지 주기(초) |
| `URBANGUARD_ROAD_DURATION` | 15 | 노면 1회 관측 길이(초) |
| `URBANGUARD_CROWD_START_DELAY` | 20 | 인파 워처 기동 유예(초) |
| `URBANGUARD_ROAD_START_DELAY` | 60 | 노면 워처 기동 유예(초) |
| **`ITS_API_KEY`** | — | **교통정보센터 CCTV 자동 수집(S-80)** |
| `KMA_SERVICE_KEY` | — | 기상청 API |
| `HRFCO_API_KEY` | — | 홍수통제소 API |
| `SOLAPI_API_KEY` / `_SECRET` / `_SENDER` | — | 문자 발송 |
| `GEMINI_API_KEY` | — | ⚠️ **VLM. 승인 없이 설정 금지** |

> **`.env` 는 형상관리에서 제외돼 있습니다.** 절대 커밋하지 마십시오.

### 2-5. 스키마 생성 (마이그레이션)

```bash
cd D:\dev-PoC\UrbanGuard; .\.venv\Scripts\python.exe -m alembic upgrade head
```

테이블 12개와 `alembic_version` 이 생성됩니다. 성공하면 head는 `81b687282d9d`
입니다.

### 2-6. 최초 관리자 계정 생성

```bash
cd D:\dev-PoC\UrbanGuard; .\.venv\Scripts\python.exe -m tot_dashboard.core.bootstrap --login-id admin --name 관리자 --dept 정보통신과
```

**임시 비밀번호가 화면에 한 번만 출력됩니다.** 반드시 기록하십시오.

> 이 계정으로 처음 로그인하면 **비밀번호 변경 화면에서 벗어날 수 없습니다.**
> 변경을 마쳐야 다른 화면을 쓸 수 있습니다.

### 2-7. AI 모델 파일 배치

`models/` 폴더에 다음이 필요합니다.

| 파일 | 용도 | 없으면 |
|---|---|---|
| `yolo11s.pt` | **개인정보 마스킹** | 마스킹 실패 → 사진·스냅샷 미표시 |
| 물 세그멘테이션 모델 | 침수 탐지 | 침수 지표 계산 중단 |
| 노면 손상 모델 | 노면 탐지 | 노면 분석 불가 |

경로는 `configs/water_config.yaml` 등에서 지정합니다.

---

### 2-8. 운영 데이터 폴더

서비스가 실행 중에 만드는 폴더입니다. **형상관리에서 제외돼 있으므로 백업
대상에 넣으셔야 합니다.**

| 폴더 | 내용 | 생성 시점 |
|---|---|---|
| `data/branding/` | 기관 로고 (S-85에서 업로드) | 로고를 올릴 때 |
| `data/videos/` | 분석용 동영상 (S-80에서 업로드) | 영상을 올릴 때 |

---

## 3. 기동과 종료

### 3-1. 기동

```bash
cd D:\dev-PoC\UrbanGuard; .\run.ps1
```

| 옵션 | 설명 |
|---|---|
| `-Background` | 백그라운드 실행 (`stop.ps1` 로 종료) |
| `-Port 8080` | 포트 변경 |
| `-Reload` | 코드 수정 시 자동 재시작 (**개발 전용**) |

`run.ps1` 은 PostgreSQL이 꺼져 있으면 자동으로 켜고, 마이그레이션 상태를
확인한 뒤 서비스를 올립니다.

> **⚠️ 기동에 1~3분 걸립니다.** AI 모델을 메모리에 올리기 때문입니다.
> 접속이 안 된다고 바로 다시 실행하지 마십시오.

### 3-2. 종료

```bash
cd D:\dev-PoC\UrbanGuard; .\stop.ps1
```

| 옵션 | 설명 |
|---|---|
| `-All` | PostgreSQL까지 함께 종료 |
| `-Status` | 종료하지 않고 상태만 확인 |

> `stop.ps1` 은 포트를 점유한 프로세스를 무조건 죽이지 않습니다. **이
> 프로젝트의 파이썬인지 명령줄로 먼저 확인**합니다 — 같은 포트를 쓰는 다른
> 프로그램을 끄지 않기 위해서입니다.

### 3-3. 정상 기동 확인

브라우저에서 `http://127.0.0.1:8000` 접속 시 로그인 화면이 나오면 정상입니다.

상태 점검:

```bash
curl http://127.0.0.1:8000/api/health
```

`"status": "ok"` 와 상시 탐지 대상 수가 나오면 정상입니다.

---

## 4. 초기 설정 절차

설치 직후 다음 순서로 진행하십시오.

| 순서 | 작업 | 화면 |
|---|---|---|
| 1 | 관리자 비밀번호 변경 | 최초 로그인 시 강제 |
| 2 | 기관 정보·**솔루션명·로고** 설정 | 설정 › 기관 정보 (S-85) |
| 3 | 운영 계정 생성 (MGR/OPR) | 관리자 › 사용자·권한 (S-90) |
| 4 | **CCTV 등록** — 개별 · **엑셀 일괄** · **교통정보 API 수집** | 설정 › CCTV 관리 (S-80) |
| 5 | **탐지서비스 지정** | 같은 화면 「탐지 지정」 |
| 6 | **ROI 설정** | 설정 › CCTV 관리 › ROI (S-81) |
| 7 | **상시 탐지 지정** | 같은 화면 「탐지 지정」 |
| 8 | **서비스 재시작** | ⚠️ 7번 반영에 필요 |
| 9 | 위험도 임계값 확인 | 설정 › 위험도 임계값 (S-82) |
| 10 | 알림 규칙 확인 | 설정 › 알림 규칙 (S-83) |

> **⚠️ 6번 ROI 설정 전에는 침수 탐지가 동작하지 않습니다.** 도로 영역이
> 필수이기 때문입니다.

> **⚠️ 8번을 빠뜨리면 상시 탐지가 켜지지 않습니다.** 화면에도 안내가 뜹니다.

---

## 5. 문제 해결

### 5-1. 기동이 안 됨

| 증상 | 원인 | 조치 |
|---|---|---|
| `python.exe 를 찾을 수 없습니다` | 가상환경 미생성 | 2-2절 수행 |
| `포트가 이미 사용 중` | 기존 서비스 실행 중 | `.\stop.ps1` |
| `데이터베이스 연결 실패` | PostgreSQL 미기동 / URL 오류 | 5-2절 |
| 1분 넘게 응답 없음 | **모델 로딩 중** | 정상. 기다리세요 |

### 5-2. 데이터베이스 연결 실패

```bash
cd D:\dev-PoC\UrbanGuard; .\.tools\pgsql\bin\pg_isready.exe -h 127.0.0.1 -p 5433
```

응답이 없으면 PostgreSQL이 꺼져 있습니다. `run.ps1` 이 자동으로 켜지만,
수동으로 켜려면 `.tools\pg.log` 를 확인하십시오.

### 5-3. 로그인이 계속 풀림

`URBANGUARD_SECRET_KEY` 가 비어 있습니다. 2-4절대로 고정 키를 넣고 재기동하세요.

### 5-4. 계정이 잠김

5회 실패 시 10분간 잠깁니다. 즉시 풀려면 시스템관리자가 S-90에서 비밀번호를
재발급하십시오.

### 5-5. CCTV 영상이 안 나옴

| 확인 | 방법 |
|---|---|
| 스트림 주소 | S-80에서 URL 확인 |
| 네트워크 | 관제망에서 해당 주소 접근 가능 여부 |
| 상시 탐지 여부 | **꺼져 있으면 영상이 들어오지 않습니다** |

### 5-6. 상시 탐지를 켰는데 안 돎

1. **서비스를 재시작했습니까?** 재시작 전에는 반영되지 않습니다.
2. `/api/health` 의 `continuous` 에서 대상 수를 확인하세요.
3. 대상이 0개소면 S-80에서 「사용」과 「상시 탐지」가 **둘 다** 체크됐는지 보세요.
4. 영상 소스가 없는 카메라는 자동으로 제외됩니다.

### 5-7. 인파 수치가 이상함

`/api/health` 의 `crowd_sources.person_source` 를 확인하십시오.
**`mock` 이면 실제 영상값이 아닙니다.** 화면에도 경고가 뜹니다.

### 5-9. 엑셀 기능이 안 됨

`openpyxl` 이 없으면 내려받기·업로드가 503으로 막힙니다.

```bash
cd D:\dev-PoC\UrbanGuard; .\.venv\Scripts\python.exe -m pip install openpyxl
```

### 5-10. 교통 CCTV 자동 수집 버튼이 잠겨 있음

`ITS_API_KEY` 가 없습니다. openapi.its.go.kr 에서 발급받아 `.env` 에 넣고
재시작하세요. 관제망에서 외부 접속이 막혀 있으면 키가 있어도 실패하며,
화면에 그 사유가 표시됩니다.

### 5-11. 노면 분석이 「연결이 거부되었습니다」로 실패

**같은 카메라를 다른 탐지서비스가 상시로 보고 있으면** CCTV 서버가 추가 연결을
거절합니다. 2초 후 1회 자동 재시도하지만, 계속 실패하면 **S-80에서 그 지점의
상시 지정을 줄이십시오.**

### 5-12. ROI 화면에 정지영상이 안 나옴

소스별로 확인하십시오.

| 소스 | 확인 |
|---|---|
| 동영상 | 파일이 서버에 있는지, 경로가 맞는지 |
| HLS | 주소와 네트워크. 관제망에서 외부 차단 여부 |
| 합성(synthetic) | **정지영상이 없습니다** — 실제 소스가 필요 |

### 5-8. 로그 위치

| 로그 | 경로 |
|---|---|
| 애플리케이션 | `.tools\urbanguard.log` (백그라운드 실행 시) |
| PostgreSQL | `.tools\pg.log` |

---

## 6. 백업

### 6-1. 데이터베이스

```bash
cd D:\dev-PoC\UrbanGuard; .\.tools\pgsql\bin\pg_dump.exe -h 127.0.0.1 -p 5433 -U postgres -Fc urbanguard -f backup_urbanguard.dump
```

### 6-2. 함께 백업해야 할 것

| 대상 | 경로 | 사유 |
|---|---|---|
| `.env` | 프로젝트 루트 | **서명 키가 바뀌면 전원 로그아웃** |
| 설정 파일 | `configs/` | 임계값·모델 설정 |
| 제보 사진 | 파일 저장소 | DB에 경로만 있음 |
| **기관 로고** | `data/branding/` | DB에 파일명만 있음 |
| **업로드 동영상** | `data/videos/` | DB에 경로만 있음. 용량 큼 |
| 모델 파일 | `models/` | 용량이 큼. 별도 보관 가능 |

> **⚠️ 백업이 자동화돼 있지 않습니다.** 운영 전 스케줄링이 필요합니다.

### 6-3. 복구

```bash
cd D:\dev-PoC\UrbanGuard; .\.tools\pgsql\bin\pg_restore.exe -h 127.0.0.1 -p 5433 -U postgres -d urbanguard -c backup_urbanguard.dump
```

---

## 7. 버전 갱신

```bash
cd D:\dev-PoC\UrbanGuard; .\stop.ps1
```

소스 교체 후:

```bash
cd D:\dev-PoC\UrbanGuard; .\.venv\Scripts\python.exe -m pip install -e . ; .\.venv\Scripts\python.exe -m alembic upgrade head ; .\run.ps1
```

> **갱신 전 반드시 백업하십시오.** 마이그레이션은 되돌리기 어렵습니다.

---

## 8. ⚠️ 운영 배포 전 필수 조치

**개발 설치만으로는 운영에 쓸 수 없습니다.**

| # | 항목 | 현재 | 조치 |
|---|---|---|---|
| **1** | **HTTPS** | ❌ HTTP | nginx 등에서 TLS 종단 |
| **2** | **`URBANGUARD_COOKIE_SECURE=1`** | ❌ | HTTPS와 동시 적용 |
| **3** | **운영 서명 키 재발급** | ⚠️ 개발 키 | 신규 발급 |
| 4 | DB 계정 분리 | ❌ | 앱 계정에서 DDL 권한 제거 |
| 5 | 정기 백업 자동화 | ❌ | 작업 스케줄러 등록 |
| 6 | 로그 수집 | ⚠️ | `logging` 전환 후 파일 수집 |
| 7 | 서비스 자동 시작 | ❌ | Windows 서비스 등록 |
| 8 | 방화벽 정책 | — | 외부 API 허용 여부 확인 |
| 9 | **VLM 비활성 확인** | — | `GEMINI_API_KEY` 미설정 유지 |
| 10 | **업로드 폴더 백업 대상 포함** | ❌ | `data/branding/` · `data/videos/` |
| 11 | 업로드 파일 바이러스 검사 | ❌ | 기관 지침이 요구하면 백신 연동 |

> **⚠️ uvicorn 워커는 1개를 유지하십시오.** 늘리면 워커마다 분석 스레드가
> 중복 기동돼 CPU가 배로 듭니다.

---

## 참고
- `system_architecture.md` — 시스템 아키텍처 설계서
- `security_design.md` — 보안 설계서 (6절 상세)
- `operator_manual.md` — 운영자 매뉴얼
- `test_plan_report.md` — 시험 계획·결과서
- `run.ps1` / `stop.ps1` — 기동·종료 스크립트
