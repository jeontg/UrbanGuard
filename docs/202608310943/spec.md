# UrbanGuard 아키텍처 명세서

작성일: 2026-08-31 · 대상: 현재(이 시점) 구현 상태 스냅샷 · 작성 범위: 코드 기반 실측(추측·계획 제외)

---

## 1. 개요

**UrbanGuard**는 지자체가 이미 보유한 CCTV(부산 ITS, 서울 TOPIS/spatic 등)
영상을 실시간으로 분석해 침수·교통위험·인파관리·노면관리 4개 도메인의
위험을 자동 판정하고, 관제요원의 대시보드·알림·이벤트 이력 관리를 지원하는
FastAPI 기반 단일 서비스입니다. 별도 CCTV·센서 구축 없이 기존 스트리밍
서버 주소만으로 동작하는 것을 전제로 설계돼 있습니다.

- **소프트웨어 구성**: 3개 선행 프로젝트("underpath_flood_dashboard"·
  "flood3"·"SAM")를 하나로 병합해 만들어졌습니다 — 이 이력이 지금도
  코드 곳곳(모듈 이름, 병합 시점 주석)에 남아 있습니다.
- **도메인 4개**: 침수·교통위험·인파관리·노면관리. 각 도메인은 카메라
  단위로 독립적으로 켜고 끌 수 있고("사용"·"상시" 2단계), 한 카메라가
  여러 도메인에 동시에 소속될 수 있습니다.
- **핵심 설계 철학(반복적으로 코드 주석에 등장)**: "실패해도 서비스는
  뜬다", "보정 안 된 값은 지어내지 않고 미보정으로 표시한다", "재배포
  서버가 죽으면 원본으로 몰래 전환하지 않고 관측 없음으로 정직하게
  실패한다." 12절에서 이 원칙들이 실제로 어디에 적용됐는지 정리합니다.

---

## 2. 전체 구성도

```
┌─────────────────────────────────────────────────────────────────┐
│  브라우저(관제요원)                                                │
│   대시보드 화면 · WHEP(WebRTC) 실시간 영상 재생                     │
└───────────────┬─────────────────────────────┬───────────────────┘
                │ HTTP/SSE                     │ WHEP(:8889)
                ▼                              ▼
┌─────────────────────────────────┐  ┌────────────────────────────┐
│ FastAPI 단일 프로세스(uvicorn)     │  │ MediaMTX (CCTV 재배포 허브)  │
│  - routes_*.py 다수(화면·API)     │  │  Control API :9997(loopback)│
│  - PipelineRunner(침수·교통 상시) │  │  RTSP :8554(loopback)       │
│  - CrowdContinuousWatcher        │◄─┤  WHEP :8889(공개)           │
│  - RoadContinuousWatcher         │  │  paths: DB가 원본(런타임 등록)│
│  - EventSync/EvidenceWorker      │  └───────┬────────────────────┘
│  - ffmpeg_relay 감시 스레드       │          │ RTSP publish(loopback만)
│  - video_quality 스캔 스레드      │  ┌───────┴────────────────────┐
└───────────────┬───────────────┘  │ ffmpeg 릴레이 프로세스(N개)   │
                │ SQLAlchemy        │  화이트리스트 카메라만, MediaMTX│
                ▼                   │  자체 HLS 디먹서 손상 우회      │
┌─────────────────────────────────┐  └────────────────────────────┘
│ PostgreSQL (URBANGUARD_DATABASE_URL)                              │
│  카메라·설정·이벤트·사용자·감사로그·관측이력·어휘(온톨로지) 등        │
└─────────────────────────────────────────────────────────────────┘
                ▲
                │ RTSP/HLS 원본
┌─────────────────────────────────┐
│ 원본 CCTV 서버 (부산 ITS · 서울 TOPIS/spatic 등, 지자체 소유)        │
└─────────────────────────────────┘
```

- **단일 FastAPI 프로세스**에 4개 도메인 로직·API·화면·백그라운드 스레드가
  전부 함께 뜹니다(마이크로서비스 분리 없음).
- **MediaMTX**는 선택 기능입니다(`restream.enabled`, 기본 꺼짐) — 켜지 않으면
  각 도메인이 원본 CCTV에 직접 붙습니다. 켜는 이유는 "같은 카메라에 3번째
  연결 시 원본 서버가 거절"하는 문제를 해결하기 위해서입니다(7-2절).
- **PostgreSQL 전용**(SQLite 폴백 없음) — "배포 시점에 방언 차이가 드러나는
  것을 막기 위해" 의도적으로 단일 DB만 지원합니다.

---

## 3. 애플리케이션 기동 시퀀스 (`service/main.py::lifespan()`)

| 순서 | 단계 | 실패 시 |
|---|---|---|
| 1 | `PipelineRunner.start()` — 침수·교통위험 상시 탐지 루프 기동 | 유일하게 try/except로 감싸지 않은 핵심 단계 |
| 2 | 증거 캡처(S-88): `EvidenceWorker` 기동, 탐지 훅 등록 | 로그만 남기고 계속 |
| 3 | `CrowdContinuousWatcher`·`RoadContinuousWatcher` 기동 | 로그만 남기고 계속 |
| 4 | `EventSync(store)` 기동 — `RiskStore` 스냅샷을 `events` 테이블로 동기화 | 로그만 남기고 계속 |
| 5 | `ffmpeg_relay.start_watchdog()` — 릴레이 프로세스 감시 스레드 | 로그만 남기고 계속 |
| 6 | `video_quality.start_scanner()` — 화질 스캔 스레드(기본 꺼짐) | 로그만 남기고 계속 |
| 7 | 백그라운드 스레드로 `restream.sync_all_paths(db)` 실행 | 로그만 남기고 계속(호스트별 순차 등록이라 수십 초 걸릴 수 있어 백그라운드) |

설계 원칙: **1번을 제외한 모든 단계가 개별 `try/except`로 감싸여 있고
실패해도 서비스 자체는 뜹니다.** 종료 시 `_shutdown_workers()`가 역순으로
정지하며, 이 함수는 화면의 "서비스 재시작" 버튼(S-01)에서도 재사용됩니다.

미들웨어 순서: `AuthGuard`(세션·권한 검사, `PUBLIC` 경로는 통과) →
`ErrorCaptureMiddleware`(뒤에 등록해 AuthGuard 자체의 오류도 잡음).

---

## 4. 데이터 모델 (`core/models.py`, PostgreSQL)

| 그룹 | 테이블 | 요지 |
|---|---|---|
| 사용자·접근 | `User`·`UserDomain`·`UserPref`·`MultiviewLayout` | 역할(SYS/MGR/OPR), bcrypt 해시, 로그인 실패 잠금, MGR의 담당 도메인 |
| 감사·공개 | `AuditLog`·`VideoDisclosure` | append-only 감사로그, 영상 공개(열람/사본/원본) 대장 — 지자체 CCTV 규정 대응 |
| 카메라·도메인설정 | `Camera`·`CameraDomain`·`CameraRoi`·`CameraLink`·`CameraSensor`·`CameraZone`·`Zone`·`Sensor` | 카메라 1개가 여러 도메인에 소속(각자 사용/상시·ROI 별도), 카메라 간 관계(인접/상류/하류) |
| 이벤트·운영 | `Event`·`EventAction`·`EventEvidence`·`EventSopCheck`·`ShiftHandover`·`FacilityControl`·`CitizenReport` | 이벤트가 1급 단위, 액션 이력·증거·SOP 체크리스트가 딸림 |
| 알림 | `Notification` | 2인 승인 흐름(요청→승인→발송/반려/실패) |
| 관측 이력(시계열) | `RoadInspection`·`CrowdObservation`·`TrafficObservation` | 카메라별 시계열, 실패(`failed`)와 "위험 없음"을 구분 |
| 어휘·온톨로지 | `RiskLevel`·`HazardType`·`LevelThreshold`·`HazardSopMap` | 등급·위험유형·임계값·SOP 매핑을 코드가 아니라 DB 행으로 관리 |
| 운영·오류 | `ErrorCode`·`ErrorLog`·`SopStep`·`AppSetting`·`TrainingRun`·`DetectionFeedback` | 오류 사전, 설정 키-값, 재학습 실행 기록, 오탐/미탐 피드백 |

DB 접근은 **전부 SQLAlchemy ORM 직접 쿼리**입니다(내부 REST API 계층 없음).
세션은 `core/db.py::get_session()`으로 요청마다 새로 엽니다.

---

## 5. 인증·권한 (`core/roles.py`, `core/auth.py`, `core/security.py`)

| 역할 | 성격 | 도메인 범위 |
|---|---|---|
| SYS | 시스템관리자(IT부서) | 전체 |
| MGR | 부서담당자 | **담당 도메인만**(`UserDomain`) |
| OPR | 관제요원 | 전체(교대 근무 특성상 도메인 제한 없음) |

- 권한 매트릭스(`PERMISSIONS`)는 (역할×자원×행위) 하드코딩 표 — VIEW/EDIT/
  EXECUTE/REQUEST/APPROVE 5행위, DASHBOARD/EVENT/FACILITY/NOTIFY/
  SETTINGS_OPS 등 약 20개 자원.
- **세션**: 서버 측 세션 테이블 없이 `itsdangerous` 서명 쿠키(8시간 만료).
  강제 로그아웃·동시 세션 제한 불가 — 알려진 한계(12절).
- **로그인 실패 5회 → 10분 잠금**, 비밀번호 9자 이상, bcrypt(passlib 미사용
  — 4.1+ 호환성 문제 회피).
- **알림 발송 승인**: MGR/SYS는 단독 발송 가능, OPR은 원칙적으로 승인
  필요(심각 등급 + 환경변수로 단독 발송 허용 시에도 사후 승인 플래그 남김).

---

## 6. 설정 시스템 (`core/settings.py`)

DB(`AppSetting` 테이블) 기반 key-value + 프로세스 메모리 캐시. 모든 설정이
`KEY_*` 상수 + `DEFAULTS` 딕셔너리 + 타입별 getter/setter 쌍으로 이뤄지며,
**잘못된 값은 에러 대신 안전한 기본값으로 눕힙니다**(fail-safe 관례).

| 그룹 | 대표 키 | 기본값 성향 |
|---|---|---|
| 도메인별 알림 최소 등급 | `flood.notify_min_grade`(1-5)·`traffic.notify_min_severity`(0-3)·`crowd.density_event_min_severity`(0-4)·`road.event_min_grade`(1-4) | 보수적(과다 알림 방지) |
| CCTV 재배포(MediaMTX) | `restream.enabled`·`*.host/port`·`restream.excluded_ids`·`restream.on_demand`·`restream.relay_ids` | **기본 전부 꺼짐** — 새 단일장애점이라 검증 후 명시적으로 켬 |
| 상시 화질 감시 | `video_quality.enabled`·`*.scan_interval_sec`·`*.warn_threshold`·`*.crit_threshold` | 기본 꺼짐(현재 운영에서는 켜짐으로 전환·900초 주기, 2026-08-30) |
| 강수 데이터 | `rainfall.backend`(synthetic/kma) | 카메라별 재정의 가능 |
| 인파 탐지 | `crowd.continuous_source`·`crowd.tile_grid` | — |
| 증거(S-88) | `evidence.levels`·`evidence.retention_months` | 0=자동 파기 안 함 |
| 지도 | `map.tile_url` 등 | 기본 꺼짐(망분리 환경 고려) |
| AI 모델 라우팅 | `model.<domain>`·`model_note.<domain>` | 도메인별 활성 모델 경로 + 사람이 쓰는 주의문구 |

---

## 7. CCTV 인프라

### 7-1. 카메라 레지스트리 (`core/cameras.py`)

카메라 1개(`Camera`)가 `CameraDomain`을 통해 여러 도메인에 동시 소속되고,
도메인마다 별도의 사용/상시 플래그와 ROI 도형 세트(`CameraRoi`)를 가집니다.
`for_domain(db, domain, continuous=...)`이 각 도메인 상시 감시 스레드가
대상을 찾는 공통 조회 함수입니다.

### 7-2. MediaMTX 재배포 허브 (`core/restream.py`)

**도입 이유**: 4개 탐지 서비스가 원본 CCTV에 각자 연결하면 같은 카메라에
3번째 연결이 시도될 때 원본 서버가 거절합니다. MediaMTX를 원본과 탐지
서비스 사이에 두어 원본 연결은 카메라당 1개만 유지하고, 그 뒤에서
RTSP(탐지용)·WHEP(브라우저용)로 나눠 줍니다.

- Control API(:9997)는 인증 없이 loopback 전용, RTSP(:8554)도 loopback
  전용(2026-08-29 하드코딩된 무인증 개방 위험을 발견해 조임). WHEP(:8889)
  만 원격 브라우저를 위해 열려 있고, `publish` 권한은 loopback에만 허용해
  제3자의 가짜 영상 주입을 막습니다.
- **on-demand**(`restream.on_demand`): 등록된 33개 카메라 중 실사용 9개를
  제외한 24개가 24시간 상시로 원본을 끌어와 하루 239GB가 유입되던 문제를,
  "리더(시청자)가 있을 때만 원본에 붙는다"로 해결.
- **폴백 없음 정책**: MediaMTX가 죽으면 원본 직결로 몰래 전환하지 않고
  "관측 없음"으로 판정을 아예 멈춥니다(정직한 실패).

### 7-3. ffmpeg 릴레이 (`core/ffmpeg_relay.py`)

**도입 이유**: MediaMTX 자신의 HLS 디먹서가 일부 카메라 영상을 간헐적으로
손상시키는 것을 원본 직결 대조로 실측 확인(MediaMTX 공식 이슈 #3088과
동일 증상, 최신 버전까지 미수정). 화이트리스트(`restream.relay_ids`)에
오른 카메라만 ffmpeg(`-c copy`)가 원본을 대신 읽어 MediaMTX에 재발행합니다.

**신뢰성 사고 이력(2026-08-30)**: 배포 다음날 릴레이 8개가 밤새 전부
죽었는데 감시 스레드가 하나도 되살리지 못한 사고가 있었습니다. 원인 3가지
(로깅 미설정으로 출력 소실, 로그 파일 핸들 누수, 카메라 1곳 예외가 감시
루프 전체를 막는 구조)를 찾아 고쳤고, 강제로 프로세스를 죽여 실제 복구를
확인하는 실기검증까지 마쳤습니다. 지금은 **죽으면 재시작**뿐 아니라
**15분마다 선제적으로 스스로 재기동**(원본이 스스로 스트림 세션을 리셋하는
것에 미리 대비)까지 합니다.

### 7-4. 상시 화질 감시 (`core/video_quality.py`, 2026-08-30 신설)

**도입 이유**: 손상 카메라를 그동안 "관제요원이 화면을 보다가 우연히
제보"로만 발견해 왔습니다. 재배포 경로(RTSP)에 짧게(3초) 붙어 ffmpeg
자신이 보고하는 실제 디코더 오류를 세어 카메라별 등급(정상/주의/손상 심각)
을 매기고 `/api/health`·"CCTV 관리" 화면에 상시 노출합니다.

⚠ 배포 직후 실기검증 중 자체 발견한 결함: on-demand 카메라가 새로 연결할
때 다음 키프레임을 기다리며 겪는 정상적인 시작 잡음을 손상으로 잘못 세어
스캔한 카메라 전부가 "손상 심각"으로 나온 적이 있습니다. 원인 신호 3종을
제거해 수정했고, 수정 후 결과가 지난 회차의 수동 조사(누적 로그 집계)와
정확히 일치하는 것까지 확인했습니다. 현재 운영 상태: **켜짐**, 900초 주기.

---

## 8. 도메인 파이프라인

4개 도메인 모두 카메라 1대당 워커(스레드)가 있고, 실시간 판정 결과는
`RiskStore`(침수·교통) 또는 각 도메인 전용 스토어에 쌓여 `/api/*` 로
대시보드에 서빙됩니다.

### 8-1. 침수

```
RTSP/HLS 프레임(교통위험용 YOLO 소스와 공유)
  → 물 세그멘테이션(torchvision LRASPP[BSD, 실사용] 또는 YOLO11-seg[AGPL])
  → FloodMetricsEngine(면적비율·확산율·차량침수·인명위험 등)
  → RiskEngine(가중합 0-100점 → 1-5등급, `configs/risk_config.yaml`)
  → RiskPredictor(10초 뒤 추세·ETA 예측, 순수 선형외삽)
  → 독립 하천수위 판정(FloodRiverAgent, FR_* 네임스페이스)
  → 알림(flood.notify_min_grade 이상, SOLAPI)
```
- 카메라별 침수심 대응표(`core/calibration.py::DepthTable`)로 면적비율을
  cm로 환산 — **보정 안 된 카메라는 "미보정"으로 정직하게 표시**(현재
  실제로 보정된 카메라 0곳).
- `is_likely_corrupted_frame()` 휴리스틱이 손상 프레임을 걸러 판정을
  건너뛰며, 이 건너뜀 횟수는 `/api/health`에 노출됩니다(2026-08-30 추가).

### 8-2. 교통위험

```
YOLO 차량·보행자 탐지 → ByteTrack(또는 자체 중심점 추적) 추적
  → 속도(px/s, 정지·정체 판정 기준)·km/h(지면 보정 있을 때만 실측 표시)
  → SemanticAgent(강우×감속×정체 조합 → TWR_* 코드, `configs/traffic_risk_config.yaml`)
  → 돌발상황 3종(보행자 도로진입·역주행·사고의심 군집, 신규 모델 없이
    기존 추적 결과 재사용)
  → VLM 상황설명(Gemini 또는 폐쇄망 대응 OpenAI 호환 서버, 실패 시
    규칙기반 문장으로 대체 — 절대 아무것도 안 내지는 않음)
  → 알림(traffic.notify_min_severity 이상, SOLAPI)
```
- 사고의심은 전체 도로 정체와 구분하기 위해 `blocked` 상태에서는 판정을
  아예 안 하며, **SMS는 발송하지 않고 화면에만 표시**합니다(사고 라벨
  데이터 없음, 확정을 피함).

### 8-3. 인파관리

```
사람 탐지 — 두 경로 공존
  (A) SAM3(GPU 전용 CLI, 텍스트 프롬프트 "person") — 검증되지 않아
      실제 대시보드에는 미연결, 향후 GPU 확보 시 검증 대상으로 보존
  (B) 타일분할 torchvision 탐지기(2×2 격자, 실제 서비스 경로) —
      전체화면 탐지 대비 인원 인식률이 크게 개선됨을 실측 확인
  → CrowdBehaviorTracker(추적 → 급증(surge)·흐름혼란(dispersion)·
    발산(divergence))
  → 밀집도(density_index, 화면 격자 점유율 — ㎡ 단위 아님)
  → SemanticRiskAgent(0-4 심각도: 정상→군중밀집→흐름혼란→급증위험→패닉분산)
  → 배회·침입·낙상 이벤트(behavior_events.py, 추가 모델 없이 추적 결과 재사용)
```
- ㎡ 단위(Fruin LOS 등 국제 기준) 임계값 체계(`core/calibration.py`)는
  이미 있지만 **지면 보정을 한 카메라가 0곳**이라 실시간 판정에는 아직
  연결되지 않고 화면 격자 점유율 방식만 씁니다.

### 8-4. 노면관리

```
YOLO 결함탐지(포트홀·균열, AGPL 라이선스 — 상용 납품 전 법무검토 필요)
  → 15분 주기 순회(카메라 1대당 15초 관측 창, 단일 스레드 라운드로빈)
  → _grade_from() — 포트홀 개수 기반 1-4등급(**"3개" 기준 근거 없음을
    코드 스스로 명시**)
```
- 실측 결과 부산 CCTV 3곳 664회 관측 중 657회가 탐지 0건 — RDD2022
  학습데이터(근접 촬영)와 실제 CCTV(원거리) 사이 도메인 격차로 추정,
  모델 재학습이 필요한 상태로 남아 있습니다.
- 구간 길이 보정(`RoadSection`)도 39곳 중 0곳 완료 — 정비 등급이 항상
  "미보정"으로 표시됩니다.

---

## 9. 알림 체계

| 경로 | 대상 도메인 | 방식 |
|---|---|---|
| 자동 | 침수·교통위험 | 판정이 임계값을 넘으면 `AlertNotifier`(SOLAPI)가 즉시 발송 |
| 수동 2인 승인 | 4개 도메인 전체 | OPR 요청 → MGR 승인 → 발송(S-50/S-51 화면), 심각+환경변수 조건 시 OPR 단독 발송도 가능(사후 승인 플래그) |

인파·노면은 자동 SMS 경로가 없고, 이벤트 생성 임계값(`crowd.density_event_min_severity`·
`road.event_min_grade`)이 사실상의 알림 게이트 역할을 합니다.
`NOTIFICATION_DRY_RUN`(기본 true)이 실제 발송 여부를 최종적으로 막습니다.

---

## 10. 공통 인프라

| 모듈 | 역할 |
|---|---|
| `core/events.py`·`core/vocabulary.py` | 도메인 공통 이벤트 상태기계, 위험등급·위험유형 어휘(DB 시드) |
| `core/analytics.py` | 위험유형별 오탐률(표본 20건 미만은 비율 대신 표본 수만 표시), 모델별 운영현황 |
| `common/video_io.py`·`cctv_capture.py`·`stream_guard.py` | 영상 입출력, ffmpeg 캡처, DNS 사전확인(과거 DNS 장애로 재연결 폭주→세그폴트 사고 재발 방지) |
| `common/tracking.py` | ByteTrack API 변경 대응 어댑터(교통·인파 공용) |
| `common/vlm.py` | Gemini/폐쇄망 OpenAI 호환 서버 공용 VLM 클라이언트, 타임아웃 필수 |
| `common/notifier.py` | SOLAPI SMS/카카오 알림톡 래퍼 |

---

## 11. 설계 원칙(교차 도메인)

- **실패해도 서비스는 뜬다** — 기동 시퀀스 대부분이 개별 try/except.
- **지어내지 않는다** — 미보정 값은 None/미보정으로 표시(침수심·인파 ㎡·
  노면 구간거리·km/h 전부 동일 원칙), VLM 실패 시 규칙기반 문장으로만
  대체(수치 조작 없음).
- **정직한 실패 표시** — 재배포 서버 장애 시 원본 직결로 몰래 전환하지
  않고 "관측 없음".
- **도메인 분리(2026-08-21)** — 침수·교통위험의 판정·알림·코드 네임스페이스
  (`FR_*` vs `TWR_*`)가 완전히 독립적이라 같은 틱에 둘 다 알림이 나갈 수
  있고, 카메라별로 각각 켜고 끌 수 있습니다.
- **단일 탐지기 재사용** — 교통위험의 YOLO 차량·보행자 탐지 결과를 침수의
  "차량 침수"·"인명 위험" 판정이 그대로 재사용(중복 탐지 없음).

---

## 12. 알려진 한계·미결 과제 (요지, 상세는 `docs/pending_tasks.md`)

- 인파·노면 도메인은 실시간 판정 임계값이 아직 코드 상수이며(교통위험은
  2026-08-24에 이미 화면·설정파일로 분리됨), 물리단위(㎡·구간거리) 기반
  임계값 체계는 만들어져 있으나 카메라별 보정이 하나도 안 끝나 실제
  판정과 분리돼 있습니다(항목 2-12·2-13).
- 노면 결함탐지 모델이 실제 CCTV에서 사실상 탐지하지 못하는 상태(항목 3-6).
- 39개 지점 중 구간 길이 보정 0곳(항목 3-7), 방향각·상하류 관계 다수 비어
  있음(항목 3-3·3-4).
- 세션이 서명 쿠키 방식이라 강제 로그아웃·동시 세션 제한 불가.
- 상시 화질 감시의 경고(3건)·심각(15건) 임계치가 실측 데이터 대비 다소
  높게 잡혀 있어 조정 필요(2026-08-30 회차 기록).

---

## 13. 부록

### 13-1. 디렉터리 맵

```
src/tot_dashboard/
  common/       — 도메인 공용 유틸리티(영상 IO·VLM·알림·추적 등)
  core/         — 카메라·설정·인증·이벤트·재배포·화질감시 등 인프라
  flood/        — 침수 도메인(세그멘테이션·지표·위험엔진·예측)
  traffic_weather/ — 교통위험 도메인(탐지·추적·판정·VLM), agents/knowledge/perception 하위 구조
  crowd/        — 인파관리 도메인(SAM3·타일분할 탐지·행동이벤트·위험판정)
  road/         — 노면관리 도메인(결함탐지·순회분석·보고서)
  service/      — FastAPI 앱 본체, routes_*.py, 상시 감시(continuous.py), 이벤트 동기화
```
160개 파이썬 모듈(2026-08-31 기준).

### 13-2. 배포·운영 스크립트 (`scripts/*.ps1`)

| 스크립트 | 역할 |
|---|---|
| `urbanguard-service.ps1` | 전체 기동/종료/재시작/상태 확인(작업 스케줄러 등록) |
| `ensure-postgres.ps1` / `stop-postgres.ps1` | 저장소 전용 PostgreSQL 기동/종료 |
| `ensure-mediamtx.ps1` / `stop-mediamtx.ps1` | 저장소 전용 MediaMTX 기동/종료(설정 켜짐 여부와 무관하게 포트는 항상 기동) |

개발 미리보기(`.claude/launch.json`)는 `serve.py`(자가복구 감시자 포함,
포트 8033)를 기본으로 사용합니다 — 이 방식으로 띄워야 화면의 "서비스
재시작" 버튼이 실제로 동작합니다.
