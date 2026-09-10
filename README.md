# UrbanGuard

부산·울산·경남 지역 공공기관을 위한 통합 도시 안전 관제 플랫폼. 4개
도메인을 API 게이트웨이 뒤에서 각자 독립 배포되는 서비스로 운영한다:

- **침수(flood)** — 지하차도·저지대 CCTV 실시간 수위·침수 위험 감지
- **교통위험(traffic)** — 강우×교통 융합 도로 위험도, 돌발상황 감지
- **인파관리(crowd)** — 다중운집 인파 밀집도·위험행동 감지
- **노면관리(road)** — 도로 노면 손상(포트홀·균열) 탐지

세 개의 초기 프로토타입(`underpath_flood_dashboard`, `flood3`, `SAM`)을
병합해 시작했다(`docs/integration_plan.md` 참고) — 지금은 그 병합 이후
API 게이트웨이 도입, 4개 도메인 독립 배포, 이벤트 관리·SOP·감사로그·
회원가입 승인 등 운영 기능, 노면 손상 탐지 모델 자체 학습까지 이어진
훨씬 큰 시스템이다. 아래 "아키텍처"가 현재 실제 구조다.

## 아키텍처

```
                    ┌─────────────────────┐
   브라우저  ──────▶│   nginx (:8080)      │  API 게이트웨이
                    └──────────┬──────────┘
                               │
       ┌───────────────┬──────┼──────┬───────────────┐
       ▼               ▼             ▼               ▼
 platform-shell   crowd-service  road-service   flood-service  traffic-service
   (:8033)          (:8034)       (:8035)         (:8036)        (:8037)
 인증·관리자 화면    인파관리 도메인  노면관리 도메인   침수 도메인     교통위험 도메인
 이벤트·SOP·감사
       │                  │             │               │              │
       └──────────────────┴─────────────┴───────────────┴──────────────┘
                               │
                      PostgreSQL (:5433)
                      MediaMTX (CCTV 재배포 허브)
```

- **platform-shell**(`service/main.py`)이 로그인·관리자 화면·이벤트/SOP/
  감사로그 등 공통 기능을 맡고, 4개 도메인 서비스(`service/{crowd,road,
  flood,traffic}_service.py`)가 각자 독립 프로세스로 실시간 탐지를
  수행한다 — 한 서비스가 죽어도 나머지·홈 화면은 영향받지 않는다.
- `core/`·`common/`은 네트워크 서비스가 아니라 **공유 라이브러리**로,
  모든 서비스가 같은 가상환경에서 그대로 임포트한다.
- 각 서비스는 정지 시켜도 재기동 시 자동으로 다시 켜지지 않도록
  관리자 화면(`/admin/services`)에서 지정할 수 있다(의도적 정지 상태가
  영속화됨).

## 준비물 — git에 없는 것

아래 4가지는 용량·라이선스·보안 문제로 버전 관리 대상이 아니다
(`.gitignore` 참고). 새 환경에서 실행하려면 별도로 확보해야 한다.

### 1. 모델 가중치 (`models/`)

| 파일 | 용도 | 확보 방법 |
|---|---|---|
| `models/yolo11s.pt` | 사람·차량 탐지(인파·교통위험 공용) | ultralytics 공식 사전학습 가중치(공개 다운로드) |
| `models/best.pt` | 침수 수면 세그멘테이션 | 이 프로젝트 자체 학습 결과물 — 재학습(`scripts/train_flood_water_cpu.py`) 또는 기존 배포본에서 이전 |
| 노면 손상 탐지(`combined_road_v1~v3`) | 포트홀·균열 탐지 | `scripts/train_road_combined.py`로 재학습(수 시간~수십 시간, CPU 기준) 필요 |

### 2. 포터블 바이너리 (`.tools/`)

PostgreSQL·MediaMTX·nginx를 시스템 설치 없이 쓰기 위한 포터블 배포본.
`scripts/ensure-postgres.ps1`/`ensure-mediamtx.ps1`/`ensure-nginx.ps1`이
각각 `.tools\pgsql\`, `.tools\mediamtx\`, `.tools\nginx\`에 있다고
가정한다. 개발 PC 간 파일 공유(사내 파일 서버 등)로 전달하거나, 시스템에
직접 설치 후 스크립트를 그에 맞게 조정한다. (`.tools\pgdata\`는 실제 DB
데이터라 옮기지 말 것 — 새 환경에서 최초 기동 시 새로 초기화된다.)

### 3. `.env`

`.env.example`을 `.env`로 복사 후 채운다 — 기상청/HRFCO/ITS/부산시
Open API 키, SOLAPI SMS/Kakao 키, `URBANGUARD_SECRET_KEY`(운영 배포 시
반드시 고정), `URBANGUARD_DATABASE_URL`. 비워도 안전한 기본값/폴백으로
뜨지만 알림·외부데이터 연동은 제한된다.

### 4. 외부 학습 데이터셋 (`data/datasets/`)

노면·침수·인파 모델을 처음부터 재학습하려는 경우에만 필요. 별도
보관소(`D:\dev-PoC_DATA`, 이 저장소 밖)에 있거나 각 도메인의
`scripts/prepare_*.py`/`scripts/train_*.py` 상단 주석이 안내하는
공개 데이터셋 출처에서 새로 받는다.

## 설치

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

### DB 스키마 적용

```bash
alembic upgrade head
```

### 최초 관리자 계정 생성

```bash
urbanguard-bootstrap --login-id admin --name 홍길동 --dept 정보통신과
```
(비밀번호는 인자로 받지 않는다 — 명령 이력에 평문으로 남는 것을 막기
위함. 미지정 시 임시 비밀번호를 한 번만 화면에 출력한다.)

## 실행

**Windows, 전체 5개 서비스 + PostgreSQL + nginx 게이트웨이 한 번에**:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action start
powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action status
powershell -ExecutionPolicy Bypass -File scripts\urbanguard-service.ps1 -Action stop
```
(`-Action guide`로 무인 서버용 Windows 서비스(NSSM) 등록 방법 확인 —
기본 등록은 로그온 시 자동 기동이라 무인 서버에는 부족하다.)

**개별 서비스 직접 기동**(포트 기본값):
```bash
scripts\urbanguard-run.cmd            # platform-shell, :8033
scripts\urbanguard-crowd-run.cmd      # crowd-service, :8034
scripts\urbanguard-road-run.cmd       # road-service, :8035
scripts\urbanguard-flood-run.cmd      # flood-service, :8036
scripts\urbanguard-traffic-run.cmd    # traffic-service, :8037
```

게이트웨이(nginx, :8080) 경유로 접속하면 관리자 화면에서 정지시킨
서비스만 502로 정직하게 실패하고 나머지는 그대로 동작한다.

기타 CLI(콘솔 스크립트로 설치됨):
```bash
tot-flood-standalone --video path/to/video.mp4   # 자체 완결형 침수 파이프라인(단일 카메라)
tot-traffic-poc [--video path/to/video.mp4]      # 교통위험 Phase-0 콘솔 PoC
tot-crowd-pipeline pipeline --input crowd.mp4    # 인파(SAM3) CLI — crowd-gpu 확장 + GPU 필요
urbanguard-passwd                                 # 계정 비밀번호 복구
```

## 테스트

```bash
pytest
```
⚠️ 다른 pytest 실행이나 서비스·PostgreSQL 재기동과 동시에 돌리면
거짓 실패·대량 skip이 발생할 수 있다 — 한 번에 하나씩 실행한다.

## 프로젝트 구조 (요약)

```
src/tot_dashboard/
  core/            공유 라이브러리 — 인증·이벤트·감사로그·설정·DB 등
  common/          범도메인 유틸(카메라·ROI·비디오 IO 등)
  flood/           침수 도메인 알고리즘
  traffic_weather/ 교통위험 도메인 알고리즘
  crowd/           인파관리 도메인 알고리즘
  road/            노면관리 도메인 알고리즘
  service/         FastAPI 서비스(platform-shell + 4개 도메인 서비스)
migrations/        Alembic DB 마이그레이션
scripts/           배포·운영·학습 스크립트(PowerShell/Python)
tests/             pytest 스위트
docs/              설계·계획 문서(생성일시 폴더별 정리)
```

## 알려진 한계

- 인파(SAM3) 도메인의 GPU 의존 코드(`crowd/sam3_model.py`,
  `crowd/sam3_pipeline.py`)는 CUDA GPU가 없는 환경에서 검증되지 않았다.
- 무인 서버 자동 기동은 Windows 서비스(NSSM) 등록이 필요하다 —
  `urbanguard-service.ps1 -Action install`은 로그온 시 기동만 한다.
- 노면 손상 탐지 모델은 부산 CCTV 원본 영상으로는 탐지 신호가 거의
  없어(각도·해상도 한계), 해외 근접촬영 공개 데이터셋으로 학습한
  모델을 실제 CCTV에 적용했을 때의 일반화 성능은 별도 검증이 필요하다.
