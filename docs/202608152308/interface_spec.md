# UrbanGuard 인터페이스 정의서 v6

> 통합 도시안전 관제 솔루션 · 앤시정보기술(주)
> 작성 2026-08-08 · 갱신 **2026-08-15 23:08** · 대상 코드 `D:\dev-PoC\UrbanGuard`
> **이 문서는 실제 라우트와 가드 규칙표를 코드에서 추출해 작성했습니다.**
> 확보하지 못한 외부 규격은 「미확보」로 명시했습니다.

---

## 1. 개요

인터페이스를 세 가지로 나눕니다.

| 구분 | 대상 | 상태 |
|---|---|---|
| **내부 API** | 관제 화면 ↔ 서버 | ✅ 구현 (경로 **104개** — 그중 `/api` **28개**) |
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
| GET | `/api/road/blocks` | **노면 현황** — S-80 지정 CCTV + 실제 결과 | monitor/view/road |
| **GET** | **`/api/road/live`** | **실시간 관제(S-44)** — 순회 진행·신선도·스트림 | monitor/view/road |
| **POST** | **`/api/road/live/focus`** | **집중 감시 시작** | **detect/execute/road** |
| **POST** | **`/api/road/live/focus/stop`** | 집중 감시 중지 | detect/execute/road |
| **POST** | **`/api/road/collect`** | **학습 데이터 수집 켜기·끄기** | **settings_sys/edit** |
| **POST** | **`/api/road/collect/video`** | **영상에서 학습 프레임 추출**(multipart) | **settings_sys/edit** |
| **GET** | **`/api/road/history/{camera_id}`** | **지점별 관측 이력** | monitor/view/road |
| GET | `/api/road/{block_id}` | 지점 상세 (탐지 결과) | monitor/view/road |
| GET | `/api/road-analysis/targets` | 분석 대상 목록 | detect/view/road |
| POST | `/api/road-analysis/run` | **분석 실행** | detect/execute/road |
| GET | `/api/road-report/{block_id}` | 점검 보고서 | report/view/road |

> ### ⚠️ 라우트 선언 순서에 제약이 있습니다
> `/api/road/live` 는 **`/api/road/{block_id}` 보다 먼저** 선언해야 합니다.
> 뒤에 두면 `live` 가 `block_id` 로 잡혀 「지점을 찾을 수 없습니다」가 됩니다.
> 회귀 테스트로 고정해 두었습니다.

> ### ⚠️ 권한이 경로별로 다릅니다
> 조회(`/api/road/live`)는 관제 권한이지만, **집중 감시 시작은 실행 권한**,
> **수집 전환은 시스템 설정 권한**입니다. 집중 감시는 분석을 반복 실행하고,
> 수집은 디스크에 영상을 쌓기 시작하는 결정이기 때문입니다. 규칙표에서
> 일반 `/api/road/` 규칙보다 **앞에** 두어야 의도대로 걸립니다.

#### `GET /api/road/blocks` 응답

```json
{
  "blocks": [
    {"block_id": "BLOCK-BUSANSTN", "name": "부산역",
     "mode": "selective", "source_type": "hls",
     "analyzed": true, "failed": false,
     "grade": 1, "grade_label": "정상",
     "defect_count": 0, "frames_analyzed": 3,
     "analyzed_at": "2026-08-12T04:20:11+00:00", "note": ""}
  ],
  "analyzed": 3, "mock": false
}
```

| 상태 | `analyzed` | `failed` | 뜻 |
|---|---|---|---|
| 미분석 | false | false | 아직 본 적 없음 |
| **분석 실패** | false | **true** | 시도했으나 **프레임 0장** |
| 분석 완료 | true | false | 결과 있음(0건도 포함) |

> **⚠️ `failed` 를 무시하면 안 됩니다.** 프레임 0장인데 손상 0건 → 「정상」으로
> 읽으면, 스트림이 끊겨 아무것도 못 본 구간을 점검 완료로 오해합니다.

#### `POST /api/road-analysis/run`

```json
{ "mode": "cctv", "target": "BLOCK-BEXCO2", "duration_sec": 15 }
```

`mode` 는 `cctv`(실시간 15초 관측) 또는 `video`(파일 12장 표본)입니다.
**`cctv` 모드 결과는 노면 현황에 반영**되고, `video` 는 자료 분석이라 반영하지
않습니다.

**동영상 소스 지점도 `cctv` 모드로 분석합니다** — 스트림이 없으면 등록된
동영상 파일을 읽습니다.

> **⚠️ 같은 카메라를 다른 도메인이 상시로 보고 있으면 연결이 거부될 수
> 있습니다.** 2초 쉬었다 1회 재시도하며, 그래도 실패하면 사유를 `note` 에
> 담습니다.

> **⚠️ 현재 모델은 부산 CCTV에서 실사용 수준이 아닙니다.** 2026-08-13 SVRDD
> 시험에서 탐지가 나오기는 했으나 **전부 차선 도색 오탐**이었습니다.
> **0건이 「손상 없음」을 뜻하지 않고, 탐지가 있다고 「손상 있음」도 아닙니다.**

#### `GET /api/road/live` 응답 (S-44)

```json
{
  "generated_at": "2026-08-13T06:05:00+00:00",
  "continuous": {"running": true, "period_sec": 900, "target_count": 8,
                 "round": 12, "resume_in_sec": null,
                 "current": {"id": "BLOCK-OLYMPIC", "name": "올림픽교차로",
                             "index": 3, "total": 8, "elapsed_sec": 7}},
  "focus": {"active": true, "camera_id": "BLOCK-BUSANSTN",
            "period_sec": 60, "rounds": 7, "remaining_sec": 1265},
  "collect": {"enabled": false, "frames": 84, "size_mb": 7.8, "max_mb": 2048},
  "counts": {"total": 8, "analyzed": 3, "failed": 1, "stale": 1, "pending": 0},
  "points": [{"block_id": "...", "age_sec": 42, "stale": false,
              "analyzing": false, "pending": false, "stream_url": "...",
              "history": [{"defect_count": 0, "failed": false}]}]
}
```

| 필드 | 뜻 | 왜 필요한가 |
|---|---|---|
| `age_sec` | 마지막 관측으로부터 경과 초 | 순회가 15분 주기라 **지금 등급이 방금 것인지 15분 전 것인지** 구분해야 합니다 |
| `stale` | 기대 주기의 2배 초과 | 두 바퀴를 놓쳤으면 그 지점은 실제로 관측되지 않고 있습니다 |
| `analyzing` | 순회가 지금 이 지점을 보는 중 | 진행 상황이 안 보이면 멈춘 것과 구분되지 않습니다 |
| `pending` | 지정은 「상시」인데 워처가 아직 안 집음 | 배지만 보고 「관측 중」으로 오해하면 안 됩니다 |
| `current.index/total` | 순회 진행률 | 한 바퀴에 「지점 수 × 15초」가 걸립니다 |

#### `POST /api/road/live/focus`

```json
{ "camera_id": "BLOCK-BUSANSTN", "period_sec": 60, "ttl_min": 30 }
```

**한 번에 한 지점만** 감시합니다(분석기를 공유하므로 여럿을 동시에 짧은 주기로
돌리면 상시 순회가 굶습니다). 주기 30~600초, 시한 1~180분. 시한이 지나면
스스로 멈추고, **S-80에서 그 지점의 노면 사용을 끄면 감시도 함께 멈춥니다.**

#### `GET /api/road/history/{camera_id}`

```json
{
  "camera_id": "BLOCK-BUSANSTN", "name": "부산역", "mode": "continuous",
  "count": 5, "persisted": true,
  "history": [
    {"analyzed_at": "2026-08-14T10:20:00+00:00", "grade": 3,
     "defect_count": 4, "frames_analyzed": 6, "failed": false,
     "source": "continuous"}
  ]
}
```

`history` 는 **최신 것부터** 옵니다(표는 최근 것부터 읽는 것이 자연스럽습니다).
`limit` 으로 개수를 바꿉니다(기본 50, 최대 200).

| 필드 | 뜻 |
|---|---|
| `source` | `continuous`(상시 순회) / `focus`(집중 감시) / `manual`(직접 실행) |
| `failed` | 프레임 0장 — **「손상 없음」이 아니라 「못 봄」** |
| `grade` | 실패 시 `null`. 0을 넣으면 화면에서 「정상」으로 읽힙니다 |
| **`persisted`** | **false 면 이력이 메모리에만 있어 재시작 시 사라집니다.** 화면이 그 사실을 알립니다 |

> **왜 필요한가** — 「지금 손상 4건」만으로는 나빠지고 있는지 알 수 없습니다.
> 어제도 4건이었는지 0건에서 늘어난 것인지가 보수 우선순위를 가릅니다.

> **⚠️ `events` 로 대신할 수 없습니다.** 이벤트는 손상이 잡혔을 때만 생겨,
> 「봤는데 없었다」와 「분석 실패」가 남지 않습니다.

#### `POST /api/road/collect/video` (multipart)

| 필드 | 값 |
|---|---|
| `file` | 영상 (mp4·avi·mov·mkv·webm, 500MB 이하) |
| `label` | 자료 이름 |
| `interval_sec` | 추출 간격(기본 2초) |
| `max_frames` | 최대 장수(기본 60, 상한 500) |

> **⚠️ 원본 영상은 보관하지 않습니다.** 프레임을 뽑아 **사람을 가린 뒤** 저장하고
> 원본은 지웁니다. 가리지 못한 프레임은 저장하지 않습니다.
> **차량번호판은 가리지 못합니다** — 응답 `note` 와 화면에 표시합니다.

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
| POST | `/settings/cameras/{id}/calibration` | ★ **지점별 캘리브레이션 저장** | settings_ops/edit |
| POST | `/settings/cameras/{id}/delete` | 삭제 | settings_ops/edit |
| GET | `/settings/cameras/{id}/roi?domain=` | ROI 조회 | settings_ops/view |
| POST | `/settings/cameras/{id}/roi/{domain}` | ROI 저장 (JSON) | settings_ops/edit |
| GET | `/settings/cameras/export.xlsx` | **목록 엑셀 내려받기** | settings_ops/view |
| GET | `/settings/cameras/template.xlsx` | **업로드 양식** | settings_ops/view |
| POST | `/settings/cameras/import/fetch` | **교통정보 API 수집** (미리보기) | settings_ops/edit |
| POST | `/settings/cameras/import/excel` | **엑셀 업로드** (미리보기) | settings_ops/edit |
| POST | `/settings/cameras/import/apply` | **선택 항목 등록** | settings_ops/edit |
| POST | `/settings/org` | 기관명·**솔루션명·로고**·배경색 | settings_sys/edit |
| POST | `/settings/org/logo/reset` | **기본 로고로 되돌리기** | settings_sys/edit |
| GET | `/branding/logo/{name}` | 기관 로고 파일 | **공개** |


#### `POST /settings/cameras/{id}/calibration` — 지점별 캘리브레이션 ★ 2026-08-15 신설

화면 단위를 **실제 물리 단위**로 바꾸는 값입니다. 이게 있어야 국내외 기준과
연결됩니다(`threshold_rationale.md`).

| 필드 | 도메인 | 형식 | 예 |
|---|---|---|---|
| `domain` | — | `flood`/`crowd`/`road` | 필수 |
| `section_length_m` | 노면 | 숫자(m) | `150` |
| `ground_image_points` | 인파·침수 | JSON 4점 | `[[0,0],[100,0],[100,100],[0,100]]` |
| `ground_world_points` | 인파·침수 | JSON 4점(m) | `[[0,0],[10,0],[10,10],[0,10]]` |
| `depth_points` | 침수 | JSON [면적비, cm] 2점 이상 | `[[0,0],[0.1,5],[0.3,20]]` |

**저장 전에 검증합니다** — 네 점 여부, 세 점이 일직선인지, 면적비가 0~1인지,
같은 면적비가 중복되는지, 구간 길이가 10km를 넘는지. 하나라도 걸리면 **400**
과 함께 사유를 돌려주고 **저장하지 않습니다.** 저장된 뒤에 발견하면 이미
늦기 때문입니다.

저장 성공 시 감사 로그(`settings.update`)에 남고, 설정 판번호가 올라
상시 탐지가 곧바로 새 값을 씁니다.

> ### 보정 결과가 나타나는 곳
> | 도메인 | 응답 필드 | 보정 전 |
> |---|---|---|
> | 노면 | `per_100m` · `density_level` | `null` · `"미보정"` |
> | 인파 | `per_m2` · `crowd_level` | `null` · `"미보정"` |
> | 침수 | `flood_depth_cm` · `flood_depth_level` · `flood_depth_saturated` | `null` · `"미보정"` · `false` |
>
> **보정 전에 0 을 주지 않습니다.** 0 을 주면 보정된 지점의 정상값과
> 구분되지 않아 화면이 거짓말하게 됩니다.
>
> `flood_depth_saturated` 는 **관측 범위를 넘었다**는 뜻입니다. 외삽하지
> 않고 마지막 관측값에 묶으므로, 그 값을 그대로 믿으면 안 됩니다.

#### 일괄 등록은 2단계입니다

수집·업로드는 **미리보기만** 만들고 DB를 건드리지 않습니다. 확인 후
`import/apply` 로 고른 행만 등록합니다 — 잘못된 파일 한 장으로 운영 설정이
지워지는 것을 막습니다.

기존 ID는 기본적으로 **건너뜁니다.** 덮어쓰려면 `update_existing` 을 함께
보내야 합니다.

#### 엑셀 양식

업로드와 내려받기가 **같은 17개 열**을 씁니다 — 받아서 고친 뒤 그대로 다시
올리는 것이 실무에서 가장 빠른 편집 방법입니다.

`ID · 이름 · 담당부서 · 위도 · 경도 · 소스종류 · 스트림주소 · 파일경로 ·
원CCTV명 · 사용여부 · 비고 · 침수사용 · 침수상시 · 인파사용 · 인파상시 ·
노면사용 · 노면상시`

#### 동영상 업로드

`/settings/cameras/create` · `/{id}/update` 에 `video_file` 을 함께 보내면
소스 종류가 `video` 로 바뀌고 경로가 자동으로 채워집니다.
mp4·avi·mov·mkv·webm · **최대 500MB**.

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

### 3-7. AI 모델 운영 (S-61) ★ 2026-08-15 신설

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| GET | `/models` | 모델 현황 · 시험대 · 운영 모델 지정 화면 | settings_sys/view |
| POST | `/models/select` | **운영 모델 지정** — 상시 탐지가 쓸 모델 | settings_sys/edit |
| POST | `/models/note` | **모델 비고 저장** — 사람이 내린 판단 | settings_sys/edit |
| POST | `/models/probe` | **시험 탐지 실행** | settings_sys/edit |
| GET | `/models/preview/{name}` | 시험 미리보기 이미지 (JPEG) | settings_sys/view |

#### `POST /models/probe` 폼 필드

| 필드 | 의미 | 기본 |
|---|---|---|
| `domain` | `flood` / `crowd` / `road` | 필수 |
| `model_key` | 모델 파일 경로(레지스트리 키) | 필수 |
| `target` | 대상 카메라 ID 또는 동영상 ID | 필수 |
| `kind` | `cctv` / `video` — 여는 방식이 다릅니다 | `cctv` |
| `duration_sec` | 관측 시간 (1~**30초 상한**) | 8 |
| `conf` | 탐지 임계값 | 0.25 |

**동시에 하나만 실행됩니다.** 이미 돌고 있으면 **409** 를 돌려줍니다 — CPU
추론 두 개가 겹치면 둘 다 느려지고 스트림도 놓칩니다.

응답은 화면(HTML)이며, 결과에 **탐지 지표 + 관측 프레임 수 + 소요 시간 +
미리보기 이미지**가 포함됩니다.

> ### ⚠️ 시험 결과는 어디에도 기록되지 않습니다
> 이벤트·노면 현황에 남기지 않습니다. 관제 화면에 시험 결과가 섞이면 그 화면을
> 믿을 수 없게 됩니다. 실행 사실만 감사 로그에 남습니다.

#### `POST /models/select` — 반영 시점이 도메인마다 다릅니다

| 도메인 | `applied` | 뜻 |
|---|---|---|
| road | `true` | **다음 순회부터 즉시 반영** |
| flood | `false` | 저장만 됨. **재기동해야** 실제로 바뀜 |
| crowd | `false` | 저장만 됨. **재기동해야** 실제로 바뀜 |

응답 화면에 그 차이를 그대로 표시합니다. 「저장했습니다」로만 끝내면 바뀐 줄
알고 관제하게 됩니다.

#### `GET /models/preview/{name}` — 경로 조작 방지

파일명은 **32자리 16진수 + `.jpg`** 만 허용합니다. 그 밖의 값은 404 입니다.
이미지는 **사람을 가린 뒤에만** 저장되며, 가리지 못하면 저장하지 않습니다.

---

### 3-8. 오류 관리 (S-92 · S-93) ★ 2026-08-15 신설

**전부 시스템관리자 전용**입니다. 오류 메시지에는 내부 경로·질의·스택트레이스가
그대로 실려 오므로 열람 범위를 넓히면 시스템 내부 구조가 함께 퍼집니다.

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| GET | `/admin/errors` | 발생 이력 목록·검색 | system_error/view |
| POST | `/admin/errors/resolve` | 처리완료 표시·해제 | system_error/edit |
| POST | `/admin/errors/delete` | **선택 삭제 (실제 삭제)** | system_error/edit |
| POST | `/admin/errors/cleanup` | **조건 일괄 삭제** | system_error/edit |
| GET | `/admin/error-codes` | 오류 코드 사전 | system_error/view |
| POST | `/admin/error-codes/save` | 코드 등록·수정 | system_error/edit |
| POST | `/admin/error-codes/delete` | 코드 삭제 (기본 코드 불가) | system_error/edit |

#### `GET /admin/errors` 질의 문자열

| 이름 | 값 |
|---|---|
| `q` | 메시지·경로·코드·상세 부분 일치 |
| `code` | 정확히 일치 |
| `category` | AUTH / DB / CCTV / AI / EXT / FILE / SYS |
| `severity` | info / warn / error / critical |
| `source` | web / worker / pipeline / manual |
| `resolved` | `0` 미처리 / `1` 처리완료 / 빈 값 전체 |
| `days` | 1 / 7 / 30 (0 또는 빈 값이면 전체) |

최대 **200건**까지 내려줍니다. 조치·삭제 후에도 조건이 유지되도록
POST → 리다이렉트 → GET 방식이며, 새로고침으로 삭제가 다시 실행되지 않습니다.

#### 삭제

**실제 삭제**입니다. 되돌릴 수 없습니다. 다만 **무엇을 몇 건 지웠는지는 감사
로그**(`error.delete`)에 남습니다. `cleanup` 은 조건이 하나도 없으면 거부하며,
기본값은 「처리완료 건 중 N일 이전」입니다.

#### 신규 자원 `system_error`

| 역할 | 권한 |
|---|---|
| 시스템관리자(SYS) | 전체 |
| 부서담당자(MGR) | 없음 |
| 관제요원(OPR) | 없음 |

메뉴에도 SYS 에게만 표시됩니다.

---

### 3-9. 계정 복구 (서버 CLI) ★ 2026-08-15 신설

HTTP 가 아니라 **서버에서 실행하는 명령**입니다. **아무도 로그인할 수 없는
상황**을 푸는 도구라 로그인을 요구할 수 없고, 그렇다면 **서버 접근 권한**
말고는 기댈 방벽이 없습니다.

```
urbanguard-passwd --list                        시스템관리자 계정 확인 (변경 없음)
urbanguard-passwd --login-id admin              비밀번호 초기화
urbanguard-passwd --login-id admin --unlock     잠금만 해제
urbanguard-passwd --login-id admin --from-stdin 비밀번호를 표준입력으로
```

| 원칙 | 이유 |
|---|---|
| 비밀번호를 **인자로 받지 않음** | PowerShell·bash 이력에 평문으로 남습니다 |
| 확인 없이는 안 바꿈 | 대화 불가 환경에서는 `--yes` 를 명시해야 합니다 |
| **감사 로그에 「서버 CLI」로 기록** | 서버에서 직접 푼 것 자체가 감사 대상입니다 |
| 초기화 시 잠금도 함께 해제 | 새 비밀번호를 줬는데 잠겨 있으면 여전히 못 들어옵니다 |

화면(S-90)의 초기화·잠금 해제도 **같은 코드**를 씁니다 — 두 경로가 갈라지면
「한쪽만 실패 횟수를 안 지운다」 같은 어긋남이 생깁니다.

> **기존 세션은 어떻게 되나** — 초기화하면 `must_change_password` 가 켜지고,
> 가드가 비밀번호 변경 화면과 로그아웃 외의 모든 경로를 막습니다. 토큰 자체를
> 무효화하지는 않지만 그 토큰으로 할 수 있는 일이 남지 않습니다.

---

## 4. 외부 연계 ★

### 4-1. 구현된 연계

| # | 대상 | 프로토콜 | 엔드포인트 | 상태 |
|---|---|---|---|---|
| 1 | **CCTV 영상** | HLS (HTTP) | 지점별 `.m3u8` (DB `cameras.source_url`) | ✅ 동작 |
| 1-2 | **국가교통정보센터 CCTV 목록** | REST | `openapi.its.go.kr:9443/cctvInfo` | ⚠️ **키 미발급 — 미시험** |
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
| 내부 API 권한 | 자동 테스트 | ✅ 345건 통과에 포함 |
| CCTV HLS 수신 | 실스트림 접속 | ✅ 8초에 465프레임 |
| 인파 선택 탐지 | 실서버 30초 실행 | ✅ (단, 소스가 mock) |
| 노면 선택 탐지 | 실서버 실행 | ✅ 부산역 3프레임 26.6초 |
| **엑셀 내려받기·업로드 왕복** | 실서버 | ✅ 미리보기·선택 등록 확인 |
| **동영상 업로드(317MB)** | 실서버 | ✅ |
| **로고 교체·크기 표기** | 실서버 | ✅ |
| **교통정보 API 수집** | — | ❌ **키 미발급으로 미시험** |
| 기상·하천 API | — | ⚠️ 미시험 |
| 문자 발송 | dry-run | ⚠️ 실발송 미시험 |
| 외부기관 연계 | — | ❌ 규격 미확보로 불가 |

---

## 참고
- `system_architecture.md` — 시스템 아키텍처 설계서
- `db_design_spec.md` — 데이터베이스 설계서 v2
- `security_design.md` — 보안 설계서
- `src/tot_dashboard/core/guard.py` — 경로별 권한 규칙표
- `src/tot_dashboard/common/notifier.py` — 문자 발송
