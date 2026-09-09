# UrbanGuard 인터페이스 정의서 v1

> 통합 도시안전 관제 솔루션 · 앤시정보기술(주)
> 작성 2026-08-08 11:33 · 대상 코드 `D:\dev-PoC\UrbanGuard`
> **이 문서는 실제 라우트와 가드 규칙표를 코드에서 추출해 작성했습니다.**
> 확보하지 못한 외부 규격은 「미확보」로 명시했습니다.

---

## 1. 개요

인터페이스를 세 가지로 나눕니다.

| 구분 | 대상 | 상태 |
|---|---|---|
| **내부 API** | 관제 화면 ↔ 서버 | ✅ 구현 (경로 76개) |
| **외부 수신 연계** | CCTV·기상·하천 등 우리가 받아 오는 것 | ⚠️ 일부 구현 |
| **외부 송신 연계** | 문자 발송, 상위기관 통보 | ⚠️ 문자만 구현 |

---

## 2. 공통 규약

### 2-1. 인증

| 항목 | 내용 |
|---|---|
| 방식 | **서명 쿠키** (`urbanguard_session`, itsdangerous) |
| 유효기간 | 8시간 (`URBANGUARD_SESSION_MAX_AGE`) |
| 서명 키 | `URBANGUARD_SECRET_KEY` 환경변수 |
| 페이로드 | `{uid, lid, role}` — 비밀번호·개인정보 미포함 |

> **⚠️ 키를 지정하지 않으면 프로세스마다 새 키가 생겨 재시작할 때마다 전원
> 로그아웃됩니다.** 개발 편의 동작이며 운영에서는 반드시 고정해야 합니다.

### 2-2. 인증 실패 응답이 화면과 API에서 다릅니다

| 대상 | 미인증 | 권한 부족 |
|---|---|---|
| 화면 (`/…`) | `303` → `/login?next=…` | `403` |
| API (`/api/…`, `/media/…`) | `401` JSON | `403` JSON |

브라우저가 fetch 응답으로 받은 **HTML 로그인 페이지를 데이터로 오해**하는 것을
막기 위해 API는 항상 JSON을 돌려줍니다.

### 2-3. ★ 기본 거부 (fail-closed)

**규칙표(`core/guard.py :: RULES`)에 등록되지 않은 `/api` 경로는 403입니다.**

새 API를 만들면서 권한 규칙 등록을 잊으면 동작하지 않습니다. 「보안이 빠진 채
조용히 열려 있는」 상태를 구조적으로 막기 위한 선택입니다.

### 2-4. 오류 응답 형식

```json
{ "detail": "이 기능에 대한 권한이 없습니다." }
```

| 코드 | 의미 |
|---|---|
| 400 | 입력값 검증 실패 |
| 401 | 미인증 |
| 403 | 권한 없음 / 규칙 미등록 |
| 404 | 대상 없음 |
| 409 | 이미 실행 중 (분석 동시 실행 충돌) |
| 503 | 기능 비활성 (분석기 미기동) |

---

## 3. 내부 API 명세

권한 열은 (자원 / 행위 / 도메인)이며, `core/roles.py` 매트릭스로 판정합니다.

### 3-1. 공통

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/health` | 상태 점검 | **인증 불필요** |
| POST | `/login` | 로그인 | 공개 |
| POST | `/logout` | 로그아웃 | 공개 |

#### `GET /api/health` 응답

```json
{
  "status": "ok",
  "block_count": 7,
  "case_count": 3,
  "notifications": { "sms_ready": true, "kakao_ready": false },
  "water_segmentation": { "backend": "ultralytics" },
  "crowd_sources": { "person_source": "mock", "all_mock": true },
  "continuous": {
    "flood": { "running": true, "targets": 7 },
    "crowd": { "running": true, "targets": [{"id":"BLOCK-CHORYANG","name":"초량교차로"}],
               "interval_sec": 5.0 },
    "road":  { "running": true, "targets": [...], "period_sec": 900.0,
               "last_round": [{"id":"...","defects":0,"note":""}] }
  }
}
```

> `crowd_sources.person_source` 가 `mock` 이면 **화면의 인원·위험행동 수치는
> 실제 영상에서 나온 값이 아닙니다.** 반드시 확인하세요.

### 3-2. 침수·교통위험

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/blocks` | 지점 목록 | monitor/view |
| GET | `/api/risk` | 전 지점 현재 위험도 | monitor/view/flood |
| GET | `/api/risk/{block_id}` | 지점 상세 | monitor/view/flood |
| GET | `/api/history` | 위험도 이력 | monitor/view/flood |
| GET | `/api/roi/{block_id}` | ROI 조회 | monitor/view/flood |
| GET | `/api/record/{block_id}` | 녹화 조회 | monitor/view/flood |
| GET | `/api/report/{block_id}` | 보고서 | report/view/flood |
| GET | `/api/flood-runs` | 분석 실행 이력 | monitor/view/flood |
| GET | `/api/flood-runs/{run_id}` | 실행 상세 | monitor/view/flood |

### 3-3. 인파관리

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/crowd/live` | 실시간 분석 상태 | monitor/view/crowd |
| GET | `/api/crowd/cameras` | **선택 탐지 대상 목록** | monitor/view/crowd |
| POST | `/api/crowd/analyze` | **선택 탐지 실행 (30초)** | detect/execute/crowd |
| POST | `/api/crowd/source` | 데이터 소스 전환 | crowd_source/edit/crowd |
| GET | `/api/cases` | 분석 사례 목록 | monitor/view/crowd |
| GET | `/api/cases/{case_id}` | 사례 상세 | monitor/view/crowd |
| POST | `/api/cases/{case_id}/notifications` | 통보 요청 | notify/**request**/crowd |

#### `POST /api/crowd/analyze`

요청
```json
{ "camera_id": "BLOCK-CHORYANG", "duration_sec": 30 }
```

응답
```json
{
  "camera_id": "BLOCK-CHORYANG", "camera_name": "초량교차로",
  "frames_analyzed": 5, "duration_sec": 33.0,
  "people_max": 14, "people_avg": 8.2, "density_max": 0.05,
  "events": [ { "eventType": "Intrusion", "trackId": 35,
                "confidence": 0.8, "evidenceText": "통제구역 진입" } ],
  "risk_level": "패닉분산", "severity": 3, "risk_score": 0.72,
  "snapshot": "<base64 JPEG — 사람 영역 마스킹됨>",
  "mask_status": "masked",
  "source": "mock",
  "note": ""
}
```

| 특이사항 | 내용 |
|---|---|
| **응답이 30초 이상 걸립니다** | 관측 시간만큼 동기 대기합니다. 호출부는 진행 표시가 필요합니다 |
| **동시 실행 불가** | 분석기가 하나뿐이라 `409` 를 돌려줍니다 |
| `severity` | 관측 구간의 **최댓값**입니다. 마지막 값이 아닙니다 |
| `snapshot` | 마스킹에 실패하면 **비어 있습니다.** `mask_status` 로 사유 확인 |
| `source` | `detector` 가 아니면 모의 데이터입니다 |

### 3-4. 도로 노면 관리

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/road/blocks` | 노면 현황 | monitor/view/road |
| GET | `/api/road/{block_id}` | 구간 상세 | monitor/view/road |
| GET | `/api/road-analysis/targets` | 분석 대상 목록 | detect/view/road |
| POST | `/api/road-analysis/run` | **분석 실행** | detect/execute/road |
| GET | `/api/road-report/{block_id}` | 점검 보고서 | report/view/road |

#### `POST /api/road-analysis/run`

```json
{ "mode": "cctv", "target": "BLOCK-BEXCO2", "duration_sec": 15 }
```

`mode` 는 `cctv`(실시간 15초 관측) 또는 `video`(파일 12장 표본)입니다.

> **⚠️ 현재 모델은 확보한 영상·부산 CCTV 모두에서 탐지 0건입니다.**
> 오류가 아니라 도메인 갭에 따른 모델 한계이며, `defect_count: 0` 으로 그대로
> 드러납니다.

### 3-5. 알림·미디어

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/notifications/status` | 발송 채널 상태 | notify/view |
| GET | `/media/{case_id}/{path}` | 사례 미디어 | monitor/view |
| GET | `/media/flood-runs/{run_id}/{path}` | 침수 분석 산출물 | monitor/view |

### 3-6. 설정 (화면 폼 · POST)

CCTV 관리(S-80·81)는 화면 폼으로 동작합니다.

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| POST | `/settings/cameras/create` | CCTV 등록 | settings_ops/edit |
| POST | `/settings/cameras/{id}/update` | 정보 수정 | settings_ops/edit |
| POST | `/settings/cameras/{id}/domains` | **탐지서비스·상시 지정** | settings_ops/edit |
| POST | `/settings/cameras/{id}/delete` | 삭제 | settings_ops/edit |
| GET | `/settings/cameras/{id}/roi?domain=` | ROI 조회 | settings_ops/view |
| POST | `/settings/cameras/{id}/roi/{domain}` | ROI 저장 (JSON) | settings_ops/edit |

#### `POST /settings/cameras/{id}/domains` 폼 필드

| 필드 | 의미 |
|---|---|
| `use_flood` / `use_crowd` / `use_road` | 탐지 대상 지정 (체크박스) |
| `cont_flood` / `cont_crowd` / `cont_road` | **상시 탐지 지정** (체크박스) |

> **⚠️ 상시 지정 변경은 서비스를 재시작해야 반영됩니다.**

#### `POST /settings/cameras/{id}/roi/{domain}` 본문

```json
{
  "frame_width": 1280, "frame_height": 720,
  "shapes": { "road_roi": [[[120,340],[980,335],[1010,700],[90,705]]] }
}
```

응답 `{"ok": true, "message": "..."}` 또는 `{"ok": false, "errors": [...]}`

**좌표는 원본 해상도 기준입니다.** 도메인별 필수 도형은 DB 설계서 3-12절 참조.

---

## 4. 외부 연계 ★

### 4-1. 구현된 연계

| # | 대상 | 프로토콜 | 엔드포인트 | 상태 |
|---|---|---|---|---|
| 1 | **CCTV 영상** | HLS (HTTP) | 지점별 `.m3u8` (DB `cameras.source_url`) | ✅ 동작 |
| 2 | **기상청 초단기실황** | REST | `apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst` | ✅ 동작 |
| 3 | **한강홍수통제소 수위** | REST | `api.hrfco.go.kr/{key}/waterlevel/list/10M/{obs}.json` | ✅ 동작 |
| 4 | **문자 발송(SMS/알림톡)** | SDK | SOLAPI | ⚠️ 아래 |
| 5 | **영상 상황 서술(VLM)** | REST | Google Gemini | ⚠️ 선택 기능 |

#### ⚠️ 망분리 환경에서의 쟁점

**2·3·4·5번은 모두 외부 인터넷을 씁니다.** 지자체 관제망에서는 그대로 동작하지
않을 가능성이 높습니다. 배포 전 확인이 필요한 사항입니다.

| 항목 | 확인 필요 |
|---|---|
| 방화벽 정책 | 위 도메인의 아웃바운드 허용 여부 |
| 프록시 경유 | 사내 프록시 필수 여부와 인증 방식 |
| 대체 경로 | 차단 시 기관 내부 기상·수위 시스템 연계 가능 여부 |
| VLM | **외부 AI 서비스에 CCTV 영상을 보내는 것**에 대한 기관 승인 여부 |

> **5번 VLM은 특히 주의가 필요합니다.** 영상 프레임이 외부로 나갑니다.
> 개인정보·보안 검토 없이 운영에서 켜서는 안 됩니다. 현재는 선택 기능이며
> API 키가 없으면 자동으로 비활성입니다.

#### 4번 문자 발송 상세

| 항목 | 내용 |
|---|---|
| 제공자 | SOLAPI (SDK) |
| 채널 | SMS / 카카오 알림톡 |
| 설정 | `SOLAPI_API_KEY`, `SOLAPI_API_SECRET`, `SOLAPI_KAKAO_CHANNEL_ID` |
| 미설정 시 | **발송하지 않고 dry-run 으로 기록**합니다 |
| 안전장치 | 주민 경보는 **역할 무관 2인 승인** 후에만 발송 |

### 4-2. ❌ 미확보 — S-70 외부기관 연계

**규격을 받지 못해 착수할 수 없는 항목입니다.**

| # | 대상 | 필요한 것 | 현재 |
|---|---|---|---|
| 1 | 스마트도시 통합플랫폼 | 연계 규격서, 테스트 계정, 전문 포맷 | ❌ 없음 |
| 2 | 112 (경찰) | 연계 승인, 프로토콜 | ❌ 없음 |
| 3 | 119 (소방) | 연계 승인, 프로토콜 | ❌ 없음 |
| 4 | 재난안전상황실 | 연계 규격 | ❌ 없음 |
| 5 | **재난문자(CBS)** | 발송 권한·절차 | ❌ 없음 |

**현재 구현 상태** — 위 다섯 기관 통보는 **화면에서 「요청」까지만 처리**하고,
실제 전송은 하지 않습니다. 담당자가 기존 절차(유선·기관 시스템)로 처리하는
것을 전제로, 요청 사실과 승인 이력만 남깁니다.

> **⚠️ 이것은 미완성이 아니라 의도적 범위 제한입니다.** 검증되지 않은 경로로
> 112·119에 자동 통보하는 것은 위험하며, 재난문자는 발송 권한 자체가 지자체에
> 있습니다. 다만 **발주처가 자동 연계를 기대하고 있다면 범위 협의가 필요합니다.**

### 4-3. 연계 규격 요청 항목 (발주처 제출용)

S-70 착수를 위해 다음이 필요합니다.

1. 스마트도시 통합플랫폼 **연계 규격서**와 개발계 접속 정보
2. 112·119 연계 **승인 여부**와 담당 부서
3. 재난문자 발송 **절차와 권한 주체**
4. 관제망에서 외부 API **허용 정책**(4-1절 표)
5. 기관 내부 기상·수위 시스템 **대체 연계 가능성**

---

## 5. 인터페이스 시험 현황

| 구분 | 시험 방식 | 결과 |
|---|---|---|
| 내부 API 권한 | 자동 테스트 | ✅ 285건 통과에 포함 |
| CCTV HLS 수신 | 실스트림 접속 | ✅ 확인 (15초에 735프레임) |
| 인파 선택 탐지 | 실서버 30초 실행 | ✅ 확인 (단, 소스가 mock) |
| 노면 상시 순회 | 실서버 실행 | ✅ 확인 (탐지 0건 — 모델 한계) |
| 기상·하천 API | — | ⚠️ **미시험** |
| 문자 발송 | dry-run | ⚠️ **실발송 미시험** |
| 외부기관 연계 | — | ❌ 규격 미확보로 불가 |

---

## 참고
- `system_architecture.md` — 시스템 아키텍처 설계서
- `db_design_spec.md` — 데이터베이스 설계서 v2
- `security_design.md` — 보안 설계서
- `src/tot_dashboard/core/guard.py` — 경로별 권한 규칙표
- `src/tot_dashboard/common/notifier.py` — 문자 발송
