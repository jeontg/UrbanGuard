# UrbanGuard 설치 가이드

`git clone`으로 코드를 받은 뒤 실제로 서비스를 띄우기까지의 절차다.
아키텍처·프로젝트 구조 개요는 [README.md](README.md)를 먼저 참고한다.

## 0. 준비물 — git에 없는 것

용량·라이선스·보안 문제로 버전 관리 대상이 아닌 항목들이다
(`.gitignore` 참고). 아래 순서대로 준비한다.

### 0-1. 포터블 바이너리(PostgreSQL·MediaMTX·nginx)

기존 개발 PC에서 아래 4개를 통째로 전달받아, **새 저장소의 같은 상대
경로**(`<repo>\.tools\` 하위)에 그대로 둔다.

| 항목 | 원본 경로(전달하는 쪽) | 비고 |
|---|---|---|
| PostgreSQL 바이너리 | `D:\dev-PoC\UrbanGuard\.tools\pgsql\` (폴더 전체) | `bin\`·`lib\`·`share\`·`pgAdmin 4\` 포함, 약 902MB |
| PostgreSQL 원본 압축파일 | `D:\dev-PoC\UrbanGuard\.tools\pg.zip` | 약 317MB, 위 `pgsql\`의 원본 zip(보관용) |
| MediaMTX | `D:\dev-PoC\UrbanGuard\.tools\mediamtx\` (폴더 전체) | 핵심 파일 `mediamtx.exe`·`mediamtx.yml`·`auto.crt`·`auto.key`, 약 54MB |
| nginx | `D:\dev-PoC\UrbanGuard\.tools\nginx\` (폴더 전체) | 핵심 파일 `nginx.exe`·`conf\`, 약 23MB |

**옮기지 말 것**: `.tools\pgdata\`(192MB) — 원본 PC의 실제 DB 데이터다.
새 환경에서는 아래 3단계에서 자동으로 새로 초기화된다. 용량이 커서
git이 아닌 사내 파일 서버·공유 드라이브 등 별도 방법으로 전달한다.

### 0-2. 모델 가중치 (`models/`)

| 파일 | 용도 | 확보 방법 |
|---|---|---|
| `models/yolo11s.pt` | 사람·차량 탐지(인파·교통위험 공용) | ultralytics 공식 사전학습 가중치(공개 다운로드) |
| `models/best.pt` | 침수 수면 세그멘테이션 | 자체 학습 결과물 — 재학습(`scripts/train_flood_water_cpu.py`) 또는 기존 배포본에서 이전 |
| 노면 손상 탐지(`combined_road_v1~v3`) | 포트홀·균열 탐지 | `scripts/train_road_combined.py`로 재학습(CPU 기준 수 시간~수십 시간) |

### 0-3. `.env`

`.env.example`을 `.env`로 복사 후 채운다 — 기상청/HRFCO/ITS/부산시 Open
API 키, SOLAPI SMS/Kakao 키, `URBANGUARD_SECRET_KEY`(운영 배포 시 반드시
고정), `URBANGUARD_DATABASE_URL`. 비워도 안전한 기본값/폴백으로 뜨지만
알림·외부데이터 연동은 제한된다.

### 0-4. 외부 학습 데이터셋 (`data/datasets/`)

모델을 처음부터 재학습하려는 경우에만 필요. 별도 보관소나 각 도메인의
`scripts/prepare_*.py`/`scripts/train_*.py` 상단 주석이 안내하는 공개
데이터셋 출처에서 받는다.

## 1. Python 환경 구성

```bash
git clone https://github.com/jeontg/UrbanGuard.git
cd UrbanGuard
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

선택 확장:
```bash
pip install -e ".[dev,legacy-streamlit]"   # 레거시 Streamlit ROI 도구
pip install -e ".[dev,crowd-gpu]"          # SAM3 크라우드 파이프라인(로컬 CUDA GPU 필요)
```

## 2. PostgreSQL 기동 확인

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ensure-postgres.ps1
```

`.tools\pgsql\`를 보고 `.tools\pgdata\`를 새로 초기화한다(최초 1회,
정상 동작이다).

## 3. DB 스키마 적용

```bash
alembic upgrade head
```

## 4. 최초 관리자 계정 생성

```bash
urbanguard-bootstrap --login-id admin --name 홍길동 --dept 정보통신과
```

비밀번호는 인자로 받지 않는다(명령 이력에 평문으로 남는 것을 막기
위함). 미지정 시 임시 비밀번호를 화면에 **한 번만** 출력하니 반드시
기록해 둔다.

## 5. 전체 서비스 기동

```powershell
powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action start
```

PostgreSQL → nginx → MediaMTX → platform-shell·crowd·road·flood·traffic
5개 서비스 순서로 확인·기동한다.

## 6. 상태 확인

```powershell
powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action status
```

브라우저에서 `http://127.0.0.1:8080` 접속 → 로그인 화면이 뜨면 정상.
4단계에서 만든 계정으로 로그인한다.

## 7. (선택) 테스트

```bash
pytest
```

⚠️ 다른 pytest 실행이나 서비스·PostgreSQL 재기동과 동시에 돌리면 거짓
실패·대량 skip이 발생할 수 있다 — 한 번에 하나씩 실행한다.

## 문제가 생기면

`data\logs\` 아래 각 서비스 콘솔 로그에서 원인을 확인한다:

| 로그 파일 | 대상 |
|---|---|
| `serve-console.log` | platform-shell |
| `serve-crowd-console.log` / `serve-road-console.log` / `serve-flood-console.log` / `serve-traffic-console.log` | 각 도메인 서비스 |
| `ensure-postgres-console.log` | PostgreSQL |
| `ensure-mediamtx-console.log` | MediaMTX |
| `ensure-nginx-console.log` | nginx 게이트웨이 |

## 개별 서비스만 직접 기동하고 싶을 때

```bash
scripts\urbanguard-run.cmd            # platform-shell, :8033
scripts\urbanguard-crowd-run.cmd      # crowd-service, :8034
scripts\urbanguard-road-run.cmd       # road-service, :8035
scripts\urbanguard-flood-run.cmd      # flood-service, :8036
scripts\urbanguard-traffic-run.cmd    # traffic-service, :8037
```

게이트웨이(nginx, :8080) 경유로 접속하면, 관리자 화면에서 정지시킨
서비스만 502로 정직하게 실패하고 나머지는 그대로 동작한다.
