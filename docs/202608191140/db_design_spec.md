# UrbanGuard 데이터베이스 설계서 v12

> 통합 도시안전 관제 솔루션 · 앤시정보기술(주)
> 작성 2026-08-08 · 갱신 **2026-09-01** · 대상 코드 `D:\dev-PoC\UrbanGuard`
> **이 문서는 실제 운영 중인 스키마를 DB에서 직접 읽어 작성했습니다.**
> 관련 문서: `system_architecture.md`(아키텍처) ·
> `interface_spec.md`(인터페이스) · `threshold_rationale.md`(임계값 근거) ·
> `../ui_design_spec.md`(화면 설계)

## v11 → v12 변경 이력 (가입 신청·승인 기능 신설)

로그인 화면에 셀프서비스 가입 신청을 추가하고, 관리자(사용자·권한 관리
화면)가 신청을 검토해 역할·담당 도메인을 정해 승인/거절하는 흐름을
붙이며(`docs/pending_tasks.md` 2026-09-01 항목 참고), 신청 상태를 담는
표 1개를 추가했습니다. `notifications`(S-50 알림 승인)와 같은 모양의
요청/승인 상태표입니다.

| # | 내용 |
|---|---|
| 1 | ★ **`signup_requests` 테이블 신설** — 가입 신청 (3-37절) |
| 2 | 표 **36개 → 37개** · 마이그레이션 head **`222303a5f105`** |

## v10 → v11 변경 이력 (API 게이트웨이 Phase 4 — 침수·교통위험 완전 분리)

침수·교통위험을 별도 프로세스(flood-service·traffic-service)로 분리하며,
홈 화면이 그 프로세스들의 인메모리 상태를 더 이상 직접 못 읽는 문제를
해결하는 표 1개를 추가했습니다(`docs/pending_tasks.md` 2026-08-31 Phase 4
항목 참고).

| # | 내용 |
|---|---|
| 1 | ★ **`live_detection_state` 테이블 신설** — 카메라별 「지금」 판정의 크로스 프로세스 조회 (3-36절) |
| 2 | 표 **35개 → 36개** · 마이그레이션 head **`e561ecab8f8c`** |

## v9 → v10 변경 이력 (AI 모델 학습 화면 신설)

관리자가 4개 탐지 도메인(침수·교통위험·인파관리·도로 노면) 모델을 화면에서
재학습할 수 있게 하며(`docs/pending_tasks.md` A-7 대응), 그 실행 기록을
남기는 표 1개를 추가했습니다.

| # | 내용 |
|---|---|
| 1 | ★ **`training_runs` 테이블 신설** — AI 모델 학습 실행 기록 (3-35절) |
| 2 | 표 **34개 → 35개** · 마이그레이션 head **`3fafb126d787`** |

## v8 → v9 변경 이력 (flood/traffic 도메인 분리)

「침수·교통위험」 하나로 묶여 있던 도메인을 **침수(flood)**와 **교통(traffic)**
2개 독립 도메인으로 나누는 작업의 DB 부분입니다
(`docs/202608210801/domain_split_flood_traffic_plan.md`).

| # | 내용 |
|---|---|
| 1 | ★ **`traffic_observations` 테이블 신설** — 교통 관측 이력 (3-34절) |
| 2 | `hazard_types` 에 **교통 세분류 5행 추가** — `traffic` / `traffic_rain_congestion` / `traffic_stalled_vehicle` / `traffic_queue_delay` / `traffic_impassable` |
| 3 | `level_thresholds` 는 **교통 행을 추가하지 않음** (3-34절·아래 사유) |
| 4 | 표 33개 → **34개** · 마이그레이션 head **`e4a91c2f7b38`** |

> ### ★ `domain` 컬럼 자체는 손대지 않았습니다
> `events`·`camera_domains`·`camera_rois`·`hazard_types` 등의 `domain` 은 전부
> `varchar(16)` 자유 문자열이고 DB 레벨 CHECK 제약이 없습니다. 그래서
> **도메인 값 하나를 늘리는 데는 스키마 마이그레이션이 필요 없었습니다** —
> 새 표(`traffic_observations`) 하나만 추가했습니다.

> ### ★ 교통에 `level_thresholds` 를 만들지 않은 이유
> 교통 등급 어휘는 침수·인파와 같은 4단계라 `kind` 를 나눌 필요가 없습니다
> (노면과 다른 점입니다). 다만 판정이 **강우량 × 속도저하 × 정지차량의
> 다변량 조합**이라, `(도메인, 등급, 최소값, 단위)` 구조인 이 표에는
> 변수를 하나밖에 담을 수 없습니다. 억지로 넣으면 화면에 뜨는 「근거 값」이
> 실제 판정과 달라지므로 **넣지 않았습니다.**

> ### ⚠️ 과거 이벤트는 재분류하지 않았습니다
> 지금까지 `domain='flood'` 로 쌓인 이벤트 중 실제로는 교통 판정이었던 것을
> 골라내려면 원본 판정 코드(`WIR_*`)가 필요한데, `b3e5f1a72c04` 백필에서
> 이미 대분류로 뭉개져 **DB에 남아 있지 않습니다.** 추정으로 재분류하면
> 오분류가 되므로 그대로 두고, 화면에 「도메인 분리 이전 기록 포함」을
> 안내하는 쪽으로 처리합니다.

---

## v7 → v8 변경 이력

> ### ⚠️ 이 판은 **누락 보완**입니다 — 그 사실부터 적습니다
> v7 이후 여러 회차에 걸쳐 표가 늘었는데 **설계서가 따라오지 못했습니다.**
> 2026-08-19 대조에서 **모델 33개 표 중 17개가 설계서에 없었습니다.**
> 절반이 넘습니다. **납품 산출물이 실제 스키마의 절반만 담고 있었습니다.**
>
> 어느 회차에서 빠졌는지 되짚는 대신, **지금 스키마를 모델에서 직접 읽어**
> 한 번에 채웠습니다.

| # | 내용 |
|---|---|
| 1 | 누락 **17개 표 전부 반영** (3-16 ~ 3-32절) |
| 2 | **어휘 3표** — `risk_levels` · `hazard_types` · `level_thresholds` (S-95) |
| 3 | **관계 5표** — `zones` · `sensors` · `camera_links` · `camera_sensors` · `camera_zones` |
| 4 | **대응 절차 3표** — `hazard_sop_map` · `sop_steps` · `event_sop_checks` |
| 5 | **기록 4표** — `event_evidence` · `video_disclosures` · `crowd_observations` · `detection_feedback` |
| 6 | **화면 2표** — `multiview_layouts` · `user_prefs` |
| 7 | 마이그레이션 head **`a1c7d90e4b52`** |
| 8 | 표 **16개 → 33개** |

> ### 표가 아니라 **왜 그렇게 만들었는지**를 함께 적었습니다
> 컬럼 목록만으로는 다음 사람이 같은 판단을 못 합니다. `auto` 를 왜 두었는지,
> 「모르겠다」를 왜 선택지로 두었는지, `ltree` 를 왜 안 썼는지를 적었습니다.

> ### 미해결도 그대로 적었습니다
> ~~`hazard_sop_map` 은 표만 있고 읽는 코드가 없습니다~~ →
> **2026-08-19 오후 해결**(3-24절). 읽는 코드 + S-98 화면.
> 다만 **`zone_id`(도·시군별)는 구역이 0행이라 아직 못 씁니다.**

---

## v6 → v7 변경 이력

| # | 내용 |
|---|---|
| 1 | ★ **`camera_domains.config` 에 `calibration` 키 신설** — 지점별 캘리브레이션 (3-11절) |
| 2 | ★ **`road_inspections.per_100m` 컬럼 추가** — 구간 보정 밀도 (3-13절) |
| 3 | 마이그레이션 9개 → **10개** (head `360aa4ff3b21`) |

> ### ★ 캘리브레이션을 새 테이블로 만들지 않은 이유
> 도메인마다 필요한 보정이 다르고(노면=구간 길이, 인파=지면 평면,
> 침수=침수심 대응표), 앞으로도 항목이 늘 수 있습니다. `camera_domains.config`
> (JSONB)가 이미 **「도메인마다 다른 부가 설정」** 자리이므로 그대로 씁니다.
> **테이블도 마이그레이션도 필요 없었습니다.**

---

## v5 → v6 변경 이력

| # | 내용 |
|---|---|
| 1 | ★ **`error_codes` 테이블 신설** — 오류 코드 사전 (3-14절) |
| 2 | ★ **`error_logs` 테이블 신설** — 오류 발생 이력, **동일 오류 집계** (3-15절) |
| 3 | `app_settings` 에 **운영 모델 키와 모델 비고** 추가 (3-9절) |
| 4 | 테이블 13개 → **15개** · 마이그레이션 8개 → **9개** (head `d7fb4badf051`) |

---

## v4 → v5 변경 이력

| # | 내용 |
|---|---|
| 1 | ★ **`road_inspections` 테이블 신설** — 노면 점검 이력의 영구 보관 (3-13절) |
| 2 | 13-8절 개정 — 최신 결과는 여전히 메모리지만 **이력은 DB에 남는다** |
| 3 | 테이블 12개 → **13개** |

---

## v3 → v4 변경 이력

| # | 내용 |
|---|---|
| 1 | `app_settings` 에 **`road_collect`** 키 추가 — 학습 데이터 자동 수집 on/off (3-9절) |
| 2 | 학습 데이터는 **DB가 아니라 파일**로 쌓인다는 점 명시 (13-9절) |
| 3 | 노면 실시간 관제(S-44)의 순회 상태·집중 감시도 **메모리 전용**임을 명시 |

---

## v2 → v3 변경 이력

| # | 내용 |
|---|---|
| 1 | `app_settings` 에 **`solution_name` · `logo_file`** 키 추가 (3-9절) |
| 2 | **인파 ROI 도형이 3종으로 확대** — 분석 영역·침입 금지·배회 감시 (3-12절) |
| 3 | 노면 탐지 결과는 **DB가 아니라 메모리**에 둔다는 점을 명시 (13-8절) |

---

## v1 → v2 변경 이력

| # | 내용 | 사유 |
|---|---|---|
| **1** | **`cameras` · `camera_domains` · `camera_rois` 3개 테이블 추가 (3-10 ~ 3-12절)** | **v1에서 통째로 누락됐습니다.** 마이그레이션 `81b687282d9d` 로 이미 반영돼 있던 테이블인데 명세에 빠져, v1의 「테이블 9개」는 사실과 달랐습니다 |
| 2 | ERD에 카메라 계층 추가 | 위와 같음 |
| 3 | 인덱스·관계·삭제정책에 카메라 3종 반영 | 위와 같음 |
| 4 | 마이그레이션 이력 6 → **7개**로 정정 | 카메라 리비전 누락 |
| 5 | 상시 탐지 구조 서술 추가 (3-11절) | 침수 전용이던 상시 탐지를 3개 도메인으로 확대 |
| 6 | 14절에서 `crowd_events` 항목 삭제 | 상시 구동 구조가 확정돼 `events` 로 흡수됨 |

---

## 1. 개요

### 1-1. 기본 정보

| 항목 | 값 | 비고 |
|---|---|---|
| DBMS | **PostgreSQL 17.7** | 사용자 지정. SQLite 폴백 없음 |
| 문자셋 | UTF8 | |
| 정렬(collate) | C | 한글 정렬이 필요하면 재검토 |
| **서버 시간대** | **Asia/Seoul** | ⚠️ 1-3절 필독 |
| 접속 URL | `URBANGUARD_DATABASE_URL` 환경변수 | `.env` 로 관리 |
| 스키마 관리 | Alembic (마이그레이션 **7개**) | head = `81b687282d9d` |
| 테이블 | **12개** (+ `alembic_version`) | 컬럼 **126개** |

### 1-2. SQLite 폴백을 두지 않은 이유

개발·테스트도 운영과 같은 PostgreSQL을 씁니다. 다른 DB로 테스트하면 방언 차이
(JSONB, 시퀀스, 타임존 처리)가 **배포 시점에야 드러납니다.** 테스트는 별도
데이터베이스(`urbanguard_test`)를 사용해 개발 데이터를 건드리지 않습니다.

### 1-3. ⚠️ 시간대 — 실제로 버그가 났던 지점

**PostgreSQL은 `timestamptz` 값을 서버 시간대(Asia/Seoul)로 돌려줍니다.**
파이썬 쪽에서 UTC 기준으로 날짜 키를 만들어 비교하면 **자정 근처 데이터가
하루 어긋납니다.**

> 2026-08-08 실제 발생: 통계 화면의 일자별 집계가 하루씩 밀림.
> `core/analytics.py` 의 `_aware()` 에서 `astimezone(timezone.utc)` 로 통일해 해결.

**규칙** — DB에서 읽은 시각을 날짜 문자열로 바꾸기 전에는 **반드시 UTC로
변환**합니다. 시각끼리의 뺄셈은 tz-aware 값이면 시간대가 달라도 안전합니다.

---

## 2. ERD

```
                        ┌──────────────┐
                        │    users     │
                        │ (계정·역할)   │
                        └──────┬───────┘
              ┌────────────────┼────────────────────┬──────────────┐
              │ CASCADE        │ SET NULL           │ SET NULL     │ SET NULL
      ┌───────▼──────┐  ┌──────▼──────┐  ┌──────────▼────────┐  ┌──▼──────────┐
      │ user_domains │  │ audit_logs  │  │  notifications    │  │ app_settings│
      │ (담당 도메인) │  │ (감사 추적)  │  │ (통보 요청·승인)   │  │ (운영 설정) │
      └──────────────┘  └─────────────┘  └───────────────────┘  └─────────────┘

                        ┌──────────────┐
                        │    events    │◄──────────────┐
                        │ (이벤트=사건) │               │ SET NULL
                        └──────┬───────┘               │
                     CASCADE   │          ┌────────────┴──────────┐
                 ┌─────────────▼──────┐   │  facility_controls    │
                 │   event_actions    │   │  (시설물 제어 시도)     │
                 │   (조치 이력)       │   └───────────────────────┘
                 └────────────────────┘   ┌───────────────────────┐
                                          │   citizen_reports     │
                                 SET NULL │   (현장 제보)          │
                        events ◄──────────┴───────────────────────┘

  ── 카메라 계층 (탐지 대상 정의) ───────────────────────────────────

                        ┌──────────────┐
                        │   cameras    │   PK = 'BLOCK-CHORYANG' 형태의 지점 ID
                        │ (CCTV 지점)   │
                        └──────┬───────┘
                     CASCADE   │   CASCADE
              ┌────────────────┴────────────────┐
      ┌───────▼─────────┐              ┌────────▼────────┐
      │ camera_domains  │              │  camera_rois    │
      │ 지점 × 탐지서비스 │              │ 지점 × 탐지서비스 │
      │ enabled/continuous│            │ 도메인별 관심영역 │
      └─────────────────┘              └─────────────────┘
                                              │ SET NULL
                                        users ┘ (updated_by)
```


  -- 관계 계층 (2026-08-19 신설) ------------------------------------

      +--------------+        +------------------+   +--------------+
      | risk_levels  |<-------| level_thresholds |   | hazard_types |--+
      | (등급 어휘)   | CASCADE| (판정 구간)       |   | (위험 유형)   |  | 자기참조
      +--------------+        +------------------+   +-------+------+  | SET NULL
                                                     SET NULL |        |
                    +--------------+                  +-------v-----+  |
                    |   cameras    |                  |    zones    |<-+
                    +--+--------+--+                  |   (구역)     |
          CASCADE      |        |  CASCADE            +------+------+
    +------------------v-+   +--v---------------+            | CASCADE
    |   camera_links     |   |  camera_sensors  |     +------v-------+
    | 인접·상류·하류      |   |  지점 x 센서      |     | camera_zones |
    | (자기참조 · auto)   |   +------+-----------+     +--------------+
    +--------------------+  CASCADE |
                             +------v------+
                             |   sensors   |
                             +-------------+

    hazard_types --CASCADE--> hazard_sop_map --CASCADE--> sop_steps
                                                              | SET NULL
                                    events --CASCADE--> event_sop_checks

**중심은 `events` 입니다.** 관제요원이 다루는 단위가 「사건」이고, 조치·시설물
제어·제보가 모두 이벤트에 붙습니다(설계서 3절 「이벤트 중심」).

**카메라 계층은 그와 별도로 「무엇을 탐지할 것인가」를 정의합니다.** 세 탐지
서비스가 같은 CCTV를 공유하되 각자 다른 영역을 보므로, 지점(`cameras`)과
서비스별 설정(`camera_domains`·`camera_rois`)을 분리했습니다.

> `events.block_id` 는 `cameras.id` 와 값이 같지만 **외래키를 걸지 않았습니다.**
> 이유는 4절 마지막 항목에 적었습니다.

---

## 3. 테이블 명세

### 3-1. `users` — 운영 계정

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `login_id` | varchar(64) **UNIQUE** | N | 로그인 아이디 |
| 3 | `name` | varchar(64) | N | 성명 |
| 4 | `dept` | varchar(64) | N | 부서 |
| 5 | `role` | varchar(8) | N | `SYS` / `MGR` / `OPR` |
| 6 | `is_active` | boolean | N | 비활성 계정은 로그인 불가 |
| 7 | `pw_hash` | varchar(255) | N | **bcrypt 해시. 평문 미저장** |
| 8 | `pw_updated_at` | timestamptz | N | |
| 9 | `failed_count` | integer | N | 로그인 실패 누적 |
| 10 | `locked_until` | timestamptz | Y | 5회 실패 시 잠금 해제 시각 |
| 11 | `last_login_at` | timestamptz | Y | |
| 12 | `created_at` | timestamptz | N | |
| 13 | `updated_at` | timestamptz | N | |
| 14 | `must_change_password` | boolean | N | **임시 비밀번호 상태** |

`must_change_password` 가 참이면 **비밀번호 변경 화면 밖으로 나갈 수 없습니다.**
발급자(관리자)가 아는 비밀번호가 계속 살아 있으면 안 되기 때문입니다.

### 3-2. `user_domains` — 담당 도메인 매핑

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `user_id` | integer FK→users **CASCADE** | N | |
| 3 | `domain` | varchar(16) | N | `flood` / `crowd` / `road` |

**UNIQUE(user_id, domain)** — 중복 배정 방지.

부서담당자(MGR)만 이 매핑으로 범위가 제한됩니다. **SYS와 OPR은 전 도메인**입니다
— 관제요원은 도메인별로 근무하지 않고 한 사람이 셋을 동시에 봅니다.

### 3-3. `events` — 이벤트 ★ 중심 테이블

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `domain` | varchar(16) | N | flood / crowd / road |
| 3 | `block_id` | varchar(64) | N | 감시지점 ID |
| 4 | `place_name` | varchar(128) | N | 지점명 (표시용 사본) |
| 5 | `event_type` | varchar(32) | N | |
| 6 | `level` | varchar(16) | N | 현재 등급 |
| 7 | `peak_level` | varchar(16) | N | **최고 등급 — 별도 보존** |
| 8 | `status` | varchar(16) | N | `open` / `progress` / `closed` / `auto_held`(2026-09-02 신규) |
| 9 | `confidence` | double | Y | 모델 신뢰도 |
| 10 | `detail` | **jsonb** | Y | 도메인별 관측값 (수위·강수 등) |
| 11 | `detected_at` | timestamptz | N | |
| 12 | `updated_at` | timestamptz | N | |
| 13 | `closed_at` | timestamptz | Y | |
| 14 | `assignee_id` | integer FK→users SET NULL | Y | |
| 15 | `closed_by` | integer FK→users SET NULL | Y | |
| 16 | `false_positive` | boolean | N | 오탐 신고 여부 |
| 17 | `hazard_type_code` | varchar(32) | N | ★ → `hazard_types.code`. 빈 값이면 유형 미지정. 2026-08-19 신규 이벤트 자동 기입 + 과거 31건 백필 |
| 18 | `last_detected_at` | timestamptz | Y | ★ 2026-09-02 신규. 임계등급(주의) 이상으로 **재탐지된** 마지막 시각만 기록 — `updated_at`은 20초 주기 동기화·사람의 확인/종결로도 갱신돼 재탐지 판정에 못 씀. 자동 보류 정책의 판단 기준 |

**설계 판단 네 가지**

1. **`peak_level` 을 따로 둔 이유** — 등급이 내려가도 「경계까지 갔던 건」임을
   알아야 대응 적정성을 사후에 판단할 수 있습니다
2. **`place_name` 을 복제한 이유** — 지점이 삭제·개명돼도 과거 이벤트가 무엇을
   가리켰는지 남아야 합니다
3. **`detail` 을 JSONB로 둔 이유** — 도메인마다 관측 항목이 달라 컬럼으로 고정하면
   도메인 추가 때마다 마이그레이션이 필요합니다
4. **`auto_held` 는 종결이 아닌 이유** — 재탐지 없이 방치된 미해결 이벤트를
   시스템이 임의로 `closed`로 옮기면 AI 학습 피드백 대기열(`core/feedback.py`)이
   사람이 실제로 판단해 닫은 이벤트와 구분을 못 하게 됩니다. 그래서 `closed`와
   분리된 상태를 두고, 재탐지 시 `open_event_for()`가 `auto_held`를 찾지 못해
   새 이벤트가 열립니다(기존 `closed` 이벤트와 동일한 동작)

### 3-4. `event_actions` — 조치 이력

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `event_id` | integer FK→events **CASCADE** | N | |
| 3 | `user_id` | integer FK→users SET NULL | Y | |
| 4 | `login_id` | varchar(64) | N | **아이디 복제** (계정 삭제 대비) |
| 5 | `action` | varchar(32) | N | detected / acknowledge / memo / notify_* / false_positive / close |
| 6 | `memo` | text | N | |
| 7 | `created_at` | timestamptz | N | |

이벤트가 지워지면 이력도 함께 지웁니다(CASCADE). 이력만 남으면 의미가 없습니다.

### 3-5. `notifications` — 통보 요청·승인

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `case_id` | varchar(128) | N | 이벤트 ID (문자열) |
| 3 | `domain` | varchar(16) | N | |
| 4 | `risk_level` | varchar(16) | N | |
| 5 | `channel` | varchar(16) | N | `sms` / **`cbs`** ← 계층 판별에 사용 |
| 6 | `body` | text | N | 문안 |
| 7 | `recipients_count` | integer | N | 수신자 수 |
| 8 | `status` | varchar(16) | N | requested / approved / sent / rejected / failed |
| 9 | `needs_post_approval` | boolean | N | 「심각」 단독 발송 시 사후 승인 대상 |
| 10 | `reject_reason` | varchar(255) | N | |
| 11 | `requested_by` | integer FK→users SET NULL | Y | |
| 12 | `approved_by` | integer FK→users SET NULL | Y | **2인 승인이 컬럼으로 나타남** |
| 13 | `requested_at` | timestamptz | N | |
| 14 | `approved_at` | timestamptz | Y | |
| 15 | `sent_at` | timestamptz | Y | |

⚠️ **알려진 설계 부채** — 알림 계층(부서 통보/주민 경보)을 별도 컬럼 대신
`channel='cbs'` 로 판별합니다. 채널이 늘면 이 방식이 깨지므로 `tier` 컬럼
추가를 권합니다(13절).

### 3-6. `facility_controls` — 시설물 제어 시도

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `facility_id` | varchar(64) | N | `configs/facilities.json` 의 ID |
| 3 | `facility_name` | varchar(128) | N | 시설명 복제 |
| 4 | `mode` | varchar(16) | N | **시도 당시의 제어 모드** |
| 5 | `command` | varchar(32) | N | status / advise / remote |
| 6 | `result` | varchar(32) | N | ok / not_linked / blocked |
| 7 | `detail` | text | N | |
| 8 | `event_id` | integer FK→events SET NULL | Y | |
| 9 | `requested_by` | integer FK→users SET NULL | Y | |
| 10 | `login_id` | varchar(64) | N | |
| 11 | `created_at` | timestamptz | N | |

**성공·실패를 가리지 않고 모두 기록합니다.** 차단막이 내려갔는지는 인명과 직결되어,
「시도했으나 실패」가 남지 않으면 사후에 책임 소재를 가릴 수 없습니다.

`mode` 를 함께 저장하는 이유는 나중에 모드가 바뀌어도 **그 시점 상태**를 알아야
하기 때문입니다.

### 3-7. `citizen_reports` — 현장 제보

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `domain` | varchar(16) | N | 기본 `road` |
| 3 | `place_name` | varchar(128) | N | |
| 4 | `block_id` | varchar(64) | N | |
| 5 | **`lat`** | double | Y | ⚠️ 개인정보 (7절) |
| 6 | **`lng`** | double | Y | ⚠️ 개인정보 |
| 7 | `description` | text | N | |
| 8 | **`photo_path`** | varchar(255) | N | ⚠️ 파일명만. 실체는 파일시스템 |
| 9 | `mask_status` | varchar(16) | N | masked / no_target / unavailable / failed |
| 10 | `mask_count` | integer | N | 가린 영역 수 |
| 11 | `status` | varchar(16) | N | received / reviewed / converted / rejected |
| 12 | `disposition` | varchar(255) | N | 처리 메모 |
| 13 | `usable_for_training` | boolean | N | **재학습 데이터 판정** |
| 14 | `reported_by` | integer FK→users SET NULL | Y | |
| 15 | `login_id` | varchar(64) | N | |
| 16 | `reviewed_by` | integer FK→users SET NULL | Y | |
| 17 | `event_id` | integer FK→events SET NULL | Y | 보수 요청 전환 시 |
| 18 | `created_at` | timestamptz | N | |
| 19 | `reviewed_at` | timestamptz | Y | |

**사진은 DB에 넣지 않습니다.** `data/reports_photo/` 에 두고 파일명만 저장합니다
— DB 백업 용량이 사진 때문에 폭증하는 것을 피하기 위해서입니다.
⚠️ 따라서 **DB 백업만으로는 사진이 복구되지 않습니다**(12절).

### 3-8. `audit_logs` — 감사 추적

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | serial PK | N | |
| 2 | `user_id` | integer FK→users SET NULL | Y | |
| 3 | **`login_id`** | varchar(64) | N | 아이디 복제 |
| 4 | `dept` | varchar(64) | N | 부서 복제 (MGR 범위 조회용) |
| 5 | `action` | varchar(64) | N | login.* / user.* / notify.* / settings.* 등 |
| 6 | `target` | varchar(255) | N | |
| 7 | `before` | jsonb | Y | 변경 전 |
| 8 | `after` | jsonb | Y | 변경 후 |
| 9 | **`ip`** | varchar(64) | N | ⚠️ 개인정보 (7절) |
| 10 | `created_at` | timestamptz | N | |

**사용자가 삭제돼도 로그는 남아야 하므로 `SET NULL` 이고, 아이디를 복제**합니다.
계정을 지워 감사 기록을 지울 수 있으면 감사추적의 의미가 없습니다.

### 3-9. `app_settings` — 화면에서 바꾸는 운영 설정

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `key` | varchar(64) **PK** | N | `org_name` / `solution_name` / `logo_file` / `board_bg` / `facility_mode` / `road_collect` |
| 2 | `value` | text | N | |
| 3 | `updated_by` | integer FK→users SET NULL | Y | |
| 4 | `updated_at` | timestamptz | N | |

키-값 구조입니다. 항목이 늘 때마다 컬럼·마이그레이션을 추가하는 대신, **화면에서
다루는 소수의 값만** 여기 둡니다.

| 키 | 뜻 | 기본값 |
|---|---|---|
| `org_name` | 기관명 — 상단·로그인·보고서 | 부산광역시 |
| `solution_name` | **가운데 상단 문구** | 통합 도시안전 관제 |
| `logo_file` | **좌측 상단 로고 파일명.** 비면 기본 로고 | (빈 값) |
| `board_bg` | 상황판 배경색 | `#0F1420` |
| `facility_mode` | 시설물 제어 모드 | 권고만 |
| **`road_collect`** | **노면 학습 데이터 자동 수집** (`on`/`off`) | **`off`** |
| **`model.flood`** / **`model.crowd`** / **`model.road`** | **도메인별 운영 모델** — 값은 모델 파일 경로 | (빈 값 = 코드 기본값) |
| **`model_note.flood`** / **`.crowd`** / **`.road`** | **모델 비고 — 사람이 내린 판단** | 확인된 사실로 초기화 |

> ### ⚠️ 모델 비고를 별도 키로 둔 이유 (2026-08-15 신설)
> 「부산 CCTV 실사용 불가」 같은 결론은 **파일이나 통계에서 자동으로
> 도출되지 않습니다.** 예전에는 이 문구가 코드에 문자열로 박혀 있어, 상황이
> 바뀌어도 화면이 그대로였습니다(제보 0건인데 「제보로 수집 중」이라고 표시).
> 지금은 **계산할 수 있는 것(상태·버전)은 계산**하고, **사람의 판단은 여기
> 적습니다.**

> ### ⚠️ 운영 모델 키가 비어 있으면 코드 기본값을 씁니다
> 반대로 값이 있는데 그 파일이 사라졌으면 **기동을 막지 않고 경고만** 남깁니다.
> 모델 설정 하나 때문에 서비스가 안 뜨면 안 됩니다.

> ### ⚠️ `road_collect` 의 기본값이 `off` 인 것은 의도입니다
> 켜면 도로 영상 프레임이 디스크에 계속 쌓입니다. **개인정보 검토를 거친 운영
> 판단이어야 하므로** 기본값을 꺼짐으로 두고, 바꾸는 데 **시스템 설정 권한**을
> 요구하며 **감사 로그**에 남깁니다. 개인정보 사전검토서 3-2-C·8절 참고.

> **로고 파일 자체는 DB에 넣지 않습니다.** `data/branding/` 에 두고 파일명만
> 기록합니다 — 이미지를 DB에 넣으면 백업·복제가 무거워집니다. 스키마가 중요한 설정(블록·ROI·임계값)은
각자의 파일을 씁니다.

---

### 3-10. `cameras` — CCTV 지점 ★ 탐지 대상의 원장

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | varchar(64) **PK** | N | `BLOCK-CHORYANG` 형태. **정수 대리키를 쓰지 않았습니다** — 아래 참고 |
| 2 | `name` | varchar(120) | N | 초량교차로 |
| 3 | `dept` | varchar(80) | N | 담당 부서. MGR 권한 범위 판정에 씀 |
| 4 | `lat` / `lng` | double precision | Y | 위경도. 국내 범위(위도 33~39, 경도 124~132) 검증 |
| 5 | `source_type` | varchar(16) | N | `hls` / `video` / `synthetic` |
| 6 | `source_url` | varchar(500) | N | HLS 주소. 미사용 시 빈 문자열 |
| 7 | `source_path` | varchar(500) | N | 파일 경로. 미사용 시 빈 문자열 |
| 8 | `cctv_name` | varchar(120) | N | 기관 CCTV 원 명칭(대조용) |
| 9 | `is_active` | boolean | N | 폐지 지점을 지우지 않고 내리는 스위치 |
| 10 | `note` | text | N | 운영 메모 |
| 11 | `sido` / `sigungu` | varchar | N | ★ 시/도 · 구/군 (2026-08-16 추가). 지역별 조회·필터 |
| 12 | `bearing_deg` | integer | Y | ★ **카메라가 보는 방위각** (2026-08-19 추가) |
| 13 | `tilt_deg` | integer | Y | 부각 |
| 14 | `fov_deg` | integer | Y | 화각 |
| 15 | `purpose` | varchar | N | 설치 목적 |
| 16 | `created_at` / `updated_at` | timestamptz | N | |

> ### ★ 방향 3종은 **사각지대 분석(S-63)의 선행 조건**입니다
> 어디를 보는지 모르면 「어디가 안 보이는지」도 말할 수 없습니다. 인접 관계
> 자동 생성(`camera_links`)에도 방위각을 씁니다.
>
> ⚠️ **39지점 방향각은 아직 비어 있습니다.** 담당자 지정이 「향후 적용」으로
> 확정돼(2026-08-19) S-96 화면이 「입력됨 n/전체」로 진척을 드러냅니다.

> **⚠️ `id` 를 문자열 자연키로 둔 이유** — 이 값은 이미 `events.block_id`,
> ROI 파일명, 기존 파이프라인 설정에 널리 쓰이고 있습니다. 정수 PK로 바꾸면
> **과거 이벤트와 지점의 연결이 전부 끊깁니다.** 자연키의 일반적 단점(값이
> 바뀌면 파급)은 감수하되, ID 변경을 화면에서 막았습니다.

**이관 이력** — 원래 `configs/blocks.json` 파일이 이 역할을 했습니다.
`scripts/migrate_blocks_to_cameras.py` 로 7개소를 옮겼으며, **원본 파일은
지우지 않았습니다**(폴백 경로로 남아 있음, 13-6절).

---

### 3-11. `camera_domains` — 지점 × 탐지서비스 ★ 상시 탐지 스위치

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) FK→cameras **CASCADE** | N | |
| 3 | `domain` | varchar(16) | N | `flood` / `crowd` / `road` |
| 4 | `enabled` | boolean | N | **① 이 서비스의 탐지 대상인가** |
| 5 | `continuous` | boolean | N | **② 상시로 계속 볼 것인가** |
| 6 | `config` | jsonb | Y | 도메인별 설정(침수 강우·하천, 인파 배회 임계 등) |

**UNIQUE (`camera_id`, `domain`)** — 한 지점당 서비스별로 한 행입니다.

관리자가 S-80에서 다루는 3단계가 그대로 이 테이블에 대응합니다.

| 단계 | 저장 위치 | 의미 |
|---|---|---|
| ① CCTV 등록 | `cameras` | 지점이 존재한다 |
| ② 탐지서비스 지정 | `camera_domains.enabled` | 이 서비스가 이 지점을 쓴다 |
| ③ 상시 탐지 지정 | `camera_domains.continuous` | 요청 없이도 계속 본다 |

**`continuous = false` 여도 `enabled = true` 면 「선택 탐지」로 쓸 수 있습니다.**
관제요원이 화면에서 지점을 골라 실행하는 방식입니다.

#### 도메인별 상시 탐지 주기

`continuous` 를 소비하는 주체가 도메인마다 다릅니다. 검출 비용이 크게 달라
같은 주기로 돌릴 수 없기 때문입니다.

| 도메인 | 구동 주체 | 방식 | 기본 주기 |
|---|---|---|---|
| `flood` | `service/runner.py` `PipelineRunner` | 지점별 스레드, 연속 | 약 5 fps |
| `crowd` | `service/continuous.py` `CrowdContinuousWatcher` | 지점별 스레드, 표본 | 지점당 5초 |
| `road` | `service/continuous.py` `RoadContinuousWatcher` | 단일 스레드 순회 | 지점당 15분 |

> **⚠️ 이 값을 바꾸면 서비스를 재시작해야 반영됩니다.** 분석 중인 스레드를
> 실행 중에 교체하면 추적(ByteTrack) 상태와 배회 타이머가 어긋납니다.
> 화면에도 재시작 안내를 띄웁니다.

> **⚠️ 켠 지점 수만큼 CPU를 씁니다.** 특히 인파는 지점당 스레드 1개와 검출
> 모델이 붙습니다. 지점을 늘릴 때는 서버 부하를 함께 확인해야 하며,
> `/api/health` 의 `continuous` 항목에서 도메인별 동작 상태를 볼 수 있습니다.

---

> ### ★ `config.calibration` — 지점별 캘리브레이션 (2026-08-15 신설)
>
> 국내외 안전 기준은 전부 **물리 단위**인데 우리가 재는 것은 **화면 단위**라,
> 그 간극을 지점마다 메우는 값입니다(`threshold_rationale.md` 8절).
>
> ```json
> {
>   "calibration": {
>     "section": {"length_m": 150.0},
>     "ground":  {"image_points": [[0,0],[100,0],[100,100],[0,100]],
>                 "world_points": [[0,0],[10,0],[10,10],[0,10]]},
>     "depth":   {"points": [[0.0, 0.0], [0.10, 5.0], [0.30, 20.0]]}
>   }
> }
> ```
>
> | 키 | 도메인 | 넣는 값 | 나오는 단위 |
> |---|---|---|---|
> | `section` | 노면 | 담당 구간 길이(m) | **건/100m** |
> | `ground` | 인파 | 화면 4점 ↔ 실제 4점(m) | **명/㎡** |
> | `depth` | 침수 | [면적비, 침수심cm] 2점 이상 | **추정 침수심(cm)** |
>
> **보정하지 않으면 보정한 척하지 않습니다.** 값이 없으면 모든 변환이
> `None` 을 돌려주고 화면은 「미보정」으로 표시합니다 — 0 이나 추정값을
> 주지 않습니다.
>
> **깨진 값은 항목 단위로 무시합니다.** 설정 하나 때문에 탐지가 멈추면
> 안 되기 때문입니다. 저장 시에는 검증을 거치므로(네 점 여부, 일직선 여부,
> 면적비 0~1, 구간 길이 상한) 정상 경로로는 깨진 값이 들어가지 않습니다.

---

### 3-12. `camera_rois` — 지점 × 탐지서비스별 관심영역

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) FK→cameras **CASCADE** | N | |
| 3 | `domain` | varchar(16) | N | `flood` / `crowd` / `road` |
| 4 | `frame_width` / `frame_height` | integer | N | **좌표의 기준 해상도** |
| 5 | `shapes` | jsonb | Y | 도형 묶음. 아래 참조 |
| 6 | `updated_by` | integer FK→users SET NULL | Y | |
| 7 | `updated_at` | timestamptz | N | |

**UNIQUE (`camera_id`, `domain`)**

#### 도메인마다 도형이 다릅니다

같은 카메라라도 보는 영역이 다릅니다. 침수는 도로면, 인파는 통제구역,
노면은 분석구간입니다. 그래서 ROI를 (지점 × 서비스) 단위로 나눴습니다.

**인파는 2026-08-12에 3종으로 늘렸습니다.** 그전에는 침입 금지 구역 하나뿐이라,
밀집도·배회를 화면 전체에서 계산했습니다. 교통 CCTV는 차도를 향해 있어
**보행 불가 영역까지 분모에 들어가는** 문제가 있었습니다.

| 인파 도형 | 쓰임 |
|---|---|
| `analysis_roi` | **이 안의 사람만** 인원·밀집도·이동지표에 넣음 |
| `intrusion_roi` | 침입 판정 |
| `loiter_roi` | **이 안에 선 사람만** 배회로 봄 (정류장 대기줄 오탐 회피) |

| 도메인 | 키 | 종류 | 필수 |
|---|---|---|---|
| `flood` | `road_roi` | polygon | **필수** |
| `flood` | `low_point_roi` | polygon | 선택 |
| `flood` | `lane_threshold_line` | line | 선택 |
| `crowd` | `analysis_roi` | polygon | 선택 |
| `crowd` | `intrusion_roi` | polygon | 선택 |
| `crowd` | `loiter_roi` | polygon | 선택 |
| `road` | `analysis_roi` | polygon | 선택 |

`shapes` 예시 (침수):

```json
{
  "road_roi": [[[120, 340], [980, 335], [1010, 700], [90, 705]]],
  "low_point_roi": [[[430, 520], [700, 515], [710, 690], [420, 690]]],
  "lane_threshold_line": [[100, 600], [1180, 590]]
}
```

> **⚠️ 좌표는 원본 해상도 기준으로 저장합니다.** 화면 표시 배율과 분리해야
> 브라우저 창 크기에 따라 영역이 어긋나지 않습니다. `frame_width/height` 가
> 그 기준값이며, 스트림 해상도가 바뀌면 ROI를 다시 잡아야 합니다.

---

### 3-13. `road_inspections` — 노면 점검 이력 ★ (2026-08-14 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) **idx** | N | ⚠️ **FK 를 걸지 않습니다** (아래) |
| 3 | `camera_name` | varchar(120) | N | 표시용 복제 |
| 4 | `grade` | integer | **Y** | 1~4. **분석 실패면 NULL** |
| 5 | `defect_count` | integer | N | 탐지 손상 수 |
| 6 | `frames_analyzed` | integer | N | 관측 프레임 수 |
| 7 | `failed` | boolean | N | **프레임 0장이면 참** |
| 8 | `source` | varchar(24) | N | `continuous` / `focus` / `manual` |
| 9 | **`per_100m`** | **double** | **Y** | **100m당 손상 건수. 구간 길이를 보정한 지점에만 값이 있음** |
| 10 | `note` | text | N | 실패 사유 등 |
| 11 | `analyzed_at` | timestamptz **idx** | N | 관측 시각 |

> ### ⚠️ `per_100m` 은 조회할 때 다시 계산하지 않습니다
> **관측 당시의 값을 그대로 저장합니다.** 나중에 구간 길이를 고치면
> 과거 기록의 의미가 조용히 바뀌기 때문입니다. 이력은 「그때 무엇이
> 사실이었나」이지 「지금 기준으로 다시 보면 어떤가」가 아닙니다.
> 개수만으로는 **긴 구간이 항상 불리해** 지점끼리 비교가 되지 않습니다.

복합 인덱스 `ix_road_inspections_camera_time (camera_id, analyzed_at)` —
화면은 언제나 「이 지점의 최근 N건」을 읽습니다.

**왜 이 테이블이 필요한가**

「지금 손상 4건」만으로는 **나빠지고 있는지** 알 수 없습니다. 어제도 4건이었는지
0건에서 늘어난 것인지가 보수 우선순위를 가르는데, 최신 1건으로는 구분되지
않습니다.

**`events` 로 대신할 수 없습니다.** 이벤트는 **손상이 잡혔을 때만** 생깁니다.
「봤는데 아무것도 없었다」와 「분석에 실패했다」는 남지 않는데, 점검 이력에서는
그 둘이 핵심입니다.

> ### ⚠️ `camera_id` 에 FK 를 걸지 않았습니다
> 카메라를 지워도 **점검 이력은 남아야 합니다.** 「그 지점을 언제 어떻게
> 점검했는가」는 카메라 등록 여부와 무관한 기록입니다. 그래서 이름도 복제해
> 둡니다(감사 로그와 같은 판단).

> ### ⚠️ `grade` 를 NULL 로 두는 이유
> 분석에 실패했을 때 0을 넣으면 화면에서 **「정상」으로 읽힙니다.** 못 본 것과
> 보았는데 이상 없는 것은 다르므로 `failed` 와 함께 명시적으로 구분합니다.

**보관 상한** — 지점당 **500건**(`URBANGUARD_ROAD_HISTORY_KEEP`). 넘으면
오래된 것부터 지웁니다. ⚠️ **파기 정책이 정해지기 전까지의 안전장치**이며,
정식 보존기간은 발주처 확인 사항입니다(8절).

**장애 시 동작** — 이 테이블을 쓸 수 없어도 **관제는 멈추지 않습니다.**
저장 실패는 삼키고, 조회 실패는 메모리 이력으로 내려갑니다.

**이관 이력** — 원래 `configs/roi/{block_id}.json` 파일이었습니다. 3건을
옮겼고 원본 파일은 남겨 두었습니다.

---

### 3-14. `error_codes` — 오류 코드 사전 ★ (2026-08-15 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `code` | varchar(32) **uniq** | N | `UG-CCTV-001` 형태 |
| 3 | `category` | varchar(16) **idx** | N | AUTH / DB / CCTV / AI / EXT / FILE / SYS |
| 4 | `title` | varchar(160) | N | 한 줄 오류명 |
| 5 | `severity` | varchar(8) | N | info / warn / error / critical |
| 6 | `cause` | text | N | 원인 — 운영자가 읽을 말로 |
| 7 | `resolution` | text | N | 해결방법 — ①②③ 순서로 |
| 8 | `builtin` | boolean | N | 제품 기본 제공 여부. **참이면 삭제 불가** |
| 9 | `is_active` | boolean | N | 거짓이면 목록에서 감춤 |
| 10 | `updated_by` | varchar(64) | N | 최근 수정자 아이디 |
| 11 | `created_at` / `updated_at` | timestamptz | N | |

**기본 코드 28종을 심습니다.** 추측이 아니라 **이 프로젝트에서 실제로 겪은
고장**에서 뽑았습니다 — DNS 장애 중 세그폴트(`UG-CCTV-002`), 한글 경로 저장
실패(`UG-FILE-004`), 권한 규칙 미등록 차단(`UG-AUTH-005`), 학습 데이터를
정리하다 모델까지 옮기는 사고(`UG-AI-001`) 등입니다.

> ### ⚠️ 기본 코드를 삭제할 수 없게 한 이유
> 지워도 **그 코드로 오류는 계속 쌓입니다.** 설명만 사라져
> 「UG-CCTV-002 가 300건」이라는 뜻 모를 목록이 남습니다. 감추려면
> `is_active` 를 끕니다.

> ### ⚠️ 다시 배포해도 덮어쓰지 않습니다
> 현장에서 알아낸 조치 방법을 담당자가 코드에 적어 두는데, 배포할 때마다
> 초기값으로 되돌리면 그 지식이 매번 사라집니다. 이미 있는 코드는 건드리지
> 않습니다.

---

### 3-15. `error_logs` — 오류 발생 이력 ★ (2026-08-15 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `code` | varchar(32) **idx** | N | ⚠️ **FK 를 걸지 않습니다** (아래) |
| 3 | `fingerprint` | varchar(64) **idx** | N | 같은 오류를 묶는 열쇠 |
| 4 | `severity` | varchar(8) | N | 사전에서 가져오되 없으면 기본값 |
| 5 | `message` | text | N | 오류 메시지 (최대 2,000자, **비밀번호·키 가림**) |
| 6 | `detail` | text | N | 스택트레이스 등 (최대 8,000자) |
| 7 | `source` | varchar(16) | N | web / worker / pipeline / manual |
| 8 | `path` | varchar(255) | N | URL 또는 `모듈:줄번호` |
| 9 | `method` | varchar(8) | N | HTTP 메서드 |
| 10 | `status_code` | integer | **Y** | HTTP 응답 코드 |
| 11 | `login_id` | varchar(64) | N | 발생 당시 사용자(복제) |
| 12 | `ip` | varchar(64) | N | |
| 13 | `count` | integer | N | **누적 발생 횟수** |
| 14 | `first_seen_at` | timestamptz | N | 처음 발생 |
| 15 | `last_seen_at` | timestamptz **idx** | N | 마지막 발생 |
| 16 | `resolved` | boolean | N | 처리완료 표시 |
| 17 | `resolved_by` / `resolved_at` | varchar(64) / timestamptz | N / **Y** | |
| 18 | `resolve_note` | text | N | 이번 건의 실제 조치 |

복합 인덱스 3개 — `(code, last_seen_at)` 목록 필터, `(resolved, last_seen_at)`
미처리 조회, `(fingerprint, resolved)` 집계 대상 탐색.

> ### ⚠️ 발생마다 한 행을 만들지 않습니다
> CCTV 재접속 실패나 DNS 장애는 **초당 수십 번** 납니다. 발생마다 행을 만들면
> 하룻밤에 수십만 행이 쌓여, 정작 중요한 오류 한 건이 그 속에 묻힙니다.
> 같은 지문은 한 행으로 합치고 `count` 만 올립니다 — 「몇 번, 언제부터
> 언제까지」가 남으므로 정보는 오히려 더 잘 보입니다.
>
> **지문 계산** — 코드 + 경로 + **정규화한 메시지**의 SHA-1. 메시지에서 시각·
> UUID·IP·메모리 주소·숫자를 지웁니다. 이걸 안 하면 「카메라 3 접속 실패」와
> 「카메라 17 접속 실패」가 다른 오류로 잡혀 목록이 다시 폭주합니다.
>
> **집계 창 24시간** — 이보다 오래된 건은 새 행이 됩니다. 지난주 장애와 오늘
> 장애가 한 줄로 합쳐지면 「언제부터」가 사라집니다.

> ### ⚠️ 처리완료 건에는 새 발생을 얹지 않습니다
> 조치했는데 또 났다는 사실이 가려지면 안 됩니다. 미처리 행만 집계 대상입니다.

> ### ⚠️ `code` 에 FK 를 걸지 않은 이유
> 두 가지입니다. 첫째, 사전에 **없는 코드로도 기록돼야** 합니다 — 처음 보는
> 오류를 「등록된 코드가 아니라서」 버리면 그 오류는 영원히 안 보입니다.
> 둘째, 사전에서 코드를 지워도 지난 이력은 남아야 합니다.

**삭제 정책** — **실제 삭제**입니다(사용자 지정). 감사 로그(`audit_logs`)와
달리 오류 이력은 지웁니다. 다만 **지운 사실과 내용은 감사 로그에 남깁니다** —
무엇을 몇 건 지웠는지가 남아야 「장애 기록을 지운 것 아니냐」는 물음에 답할 수
있습니다. 조건 없는 전체 삭제는 거부하며, 일괄 정리는 기본이 「처리완료 건만」
입니다.

**민감정보** — 비밀번호·API 키는 **저장 전에** 가립니다. 접속 문자열이 예외
메시지에 통째로 실려 오는 일이 흔합니다.
`postgresql+psycopg://user:***@host` 형태로 남습니다.

**보존기간** — ⚠️ **미정.** 발주처 확인 사항입니다(8절). 현재는 운영자가
화면에서 「N일 이전 처리완료 건」을 지우는 방식입니다.

---

### 3-16. `risk_levels` — 위험등급 어휘 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `code` | varchar(16) **PK** | N | `interest` / `caution` / `alert` / `severe` / `road_*` |
| 2 | `kind` | varchar(16) | N | ★ **`risk`(위험) / `maintenance`(정비).** 아래 설명 |
| 3 | `seq` | integer | N | 낮을수록 안전. **순위 비교의 유일한 근거** |
| 4 | `label` | varchar(32) | N | 관심 / 주의 / 경계 / 심각 · 양호 / 관찰 / 보수 필요 / 긴급 |
| 5 | `color` | varchar(16) | N | 화면 색 |
| 6 | `is_critical` | boolean | N | 참이면 즉시 대응 대상 |
| 7 | `std_uri` | varchar(256) | N | 국가 표준 대응 식별자(있으면) |
| 8 | `is_active` | boolean | N | 거짓이면 새 판정에 쓰지 않음 |

**`kind` 가 왜 필요한가 (2026-08-19 추가)** — 노면
「양호·관찰·보수 필요·긴급」은 **위험등급이 아니라 정비 등급**입니다.
「긴급」은 「지금 통제하라」가 아니라 **「빨리 보수하라」**이고, 시간 축이
실시간이 아니라 주·월 단위입니다.

구간 숫자를 기관이 S-95 화면에서 바꿀 수 있게 하려고 **같은 표에 담되**,
`kind='maintenance'` 인 행은 **이벤트 등급 비교·경보 판정에서 빠집니다**
(`core/vocabulary.py` 의 조회 함수들이 걸러냅니다).

⚠️ 섞어 세면 상황판에서 침수 「심각」과 노면 「긴급」 중 **무엇이 더 급한지
알 수 없게** 됩니다.

**등급 이름을 표로 뺀 이유** — 기관마다 부르는 말이 다릅니다. 코드에 박아 두면
기관이 바뀔 때마다 코드를 고쳐야 합니다. S-95 화면에서 바꿉니다.

> ### ★ 행안부 현장인파관리시스템과 같은 4등급입니다
> 「관심·주의·경계·심각」이 **국가 시스템의 위험경보와 글자 그대로
> 같습니다.** 맞추려고 한 것이 아니라 결과적으로 일치했습니다.

> ### ⚠️ `seq` 를 지우거나 겹치게 두면 안 됩니다
> 「같은 지점에 여러 건이 열렸을 때 **가장 급한 것**을 고른다」가 전부
> `seq` 비교입니다. 값이 같으면 무엇을 먼저 보여줄지 정할 수 없습니다.

> ### ⚠️ 등급을 **지우지 말고** `is_active` 를 끄십시오
> 지난 이벤트가 그 코드를 참조합니다. 지우면 **과거 이벤트의 등급이 사라집니다.**

---

### 3-17. `hazard_types` — 위험 유형 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `code` | varchar(32) **PK** | N | `flood.road` / `crowd.density` 형태 |
| 2 | `domain` | varchar(16) **idx** | N | flood / crowd / road |
| 3 | `parent_code` | varchar(32) | Y | → `hazard_types.code` (SET NULL). 상위 유형 |
| 4 | `label` | varchar(64) | N | 사람이 읽는 이름 |
| 5 | `source` | varchar(16) | N | 근거 출처 (`own` / 표준명) |
| 6 | `detectable` | boolean | N | ★ **우리가 실제로 탐지할 수 있는가** |
| 7 | `std_uri` | varchar(256) | N | 국가 표준 대응 식별자 |
| 8 | `is_active` | boolean | N | |

> ### ★ `detectable` 이 이 표의 핵심입니다
> 유형 목록에는 **탐지하지 못하는 것도 들어 있습니다**(예: 지하공간 침수).
> 체계를 갖추려면 필요하지만, **그것을 「감시 중」으로 보이게 하면 안 됩니다.**
> 이 칸이 거짓이면 화면에서 감시 대상으로 세지 않습니다.

> ### ⚠️ `parent_code` 는 **자기 참조**입니다
> 계층을 만들 때 순환(A→B→A)이 생기지 않게 하는 것은 **애플리케이션 책임**
> 입니다. DB 는 막아 주지 않습니다.

---

### 3-18. `level_thresholds` — 등급 판정 구간 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `domain` | varchar(16) **idx** | N | flood / crowd |
| 3 | `level_code` | varchar(16) | N | → `risk_levels.code` (CASCADE) |
| 4 | `min_value` | float | N | 이 값 **이상**이면 이 등급 |
| 5 | `unit` | varchar(16) | N | `cm` / `명/㎡` |
| 6 | `source_note` | text | N | ★ **왜 이 숫자인가** |
| 7 | `updated_at` | timestamptz | N | |
| 8 | `updated_by` | varchar(64) | N | |

**UNIQUE** `(domain, level_code)` — 한 도메인의 한 등급에 구간은 하나입니다.

기본값 — 침수 5 / 15 / 30 cm, 인파 3 / 4 / 5 명/㎡.

> ### ★ `source_note` 를 필수로 둔 이유
> 「30cm 는 왜 심각인가」에 답하지 못하면 **기관이 그 숫자를 못 씁니다.**
> 화면에서 값을 바꿀 수 있게 하되 **근거는 함께 남깁니다.**

> ### ⚠️ 구간을 못 읽으면 **관심으로 떨어뜨리지 않습니다**
> `calibration.flood_level()` 은 표 조회에 실패하면 `None` 을 돌려줍니다.
> 「관심」으로 바꾸면 **위험이 안전해 보입니다.**

---

### 3-19. `zones` — 구역 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | varchar(64) **PK** | N | |
| 2 | `kind` | varchar(16) **idx** | N | `admin`(행정) / `basin`(유역) / `road` 등 |
| 3 | `path` | varchar(512) **idx** | N | ★ 계층 경로 (`busan/jin/beomcheon`) |
| 4 | `name` | varchar(128) | N | |
| 5 | `hazard_type_code` | varchar(32) | Y | → `hazard_types.code` (SET NULL) |
| 6 | `source` | varchar(32) | N | 출처 |
| 7 | `note` | text | N | |
| 8 | `is_active` | boolean | N | |

> ### ★ `ltree` 를 쓰지 않고 `varchar` + `LIKE` 로 했습니다
> 계층 조회에 PostgreSQL 확장(`ltree`)을 쓸 수 있지만, **추가 설치가
> 필요합니다.** 폐쇄망 납품에서 설치 항목이 하나 늘면 그만큼 검토가
> 늘어납니다. 규모(구역 수백 개)에서 `path LIKE 'busan/jin/%'` 로 충분합니다.
> **추가 설치 0** 을 지켰습니다.

---

### 3-20. `sensors` — 외부 센서 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | varchar(64) **PK** | N | |
| 2 | `kind` | varchar(32) | N | 수위계 / 강우계 등 |
| 3 | `name` | varchar(128) | N | |
| 4 | `lat` / `lng` | float | Y | 좌표 |
| 5 | `source` | varchar(32) | N | 제공 기관 |
| 6 | `external_id` | varchar(128) | N | 제공처의 관측소 코드 |
| 7 | `last_seen_at` | timestamptz | Y | ★ **마지막으로 값이 들어온 시각** |
| 8 | `note` | text | N | |
| 9 | `is_active` | boolean | N | |

> ### ★ 우리 자리는 「예보」가 아니라 「지점 관측」입니다
> 국가 도시침수예보가 **CCTV 를 공식 입력으로 채택**했습니다(2026-06-19 시행).
> 센서는 그 반대 방향 — **영상 판정을 실측으로 뒷받침**하는 자리입니다.

> ### ⚠️ `last_seen_at` 이 오래됐으면 그 센서를 근거로 쓰면 안 됩니다
> 값이 안 들어오는 센서는 **틀린 값을 주는 것보다 낫지 않습니다.** 조용히
> 옛날 값을 쓰면 「실측으로 확인했다」는 말이 거짓이 됩니다.

---

### 3-21. `camera_links` — 지점 사이 관계 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `from_camera_id` | varchar(64) | N | → `cameras.id` (CASCADE) |
| 3 | `to_camera_id` | varchar(64) | N | → `cameras.id` (CASCADE) |
| 4 | `kind` | varchar(16) | N | `adjacent` / `upstream` / `downstream` |
| 5 | `distance_m` | integer | Y | 직선거리 |
| 6 | `bearing_deg` | integer | Y | 방위각 |
| 7 | `auto` | boolean | N | ★ **자동 생성 여부** |
| 8 | `note` | text | N | |
| 9 | `created_at` | timestamptz | N | |

**UNIQUE** `(from_camera_id, to_camera_id, kind)` ·
**INDEX** `(from_camera_id, kind)` · `(to_camera_id)`

> ### ★ `auto` 로 자동과 사람을 구분합니다
> **인접**은 좌표로 자동 생성할 수 있지만(반경 500m), **상·하류는 지형과
> 배수 계통을 아는 사람만 압니다.** 섞어 두면 자동 재생성이 사람이 넣은
> 값을 덮어씁니다.

> ### ⚠️ 재귀 조회가 출발점으로 되돌아옵니다 — 시험이 잡았습니다
> 인접은 **양방향**이라 A→B→A 로 자기 자신이 결과에 섞였습니다.
> 재귀 CTE 에 `camera_id <> :start` 를 넣어 막았습니다.

> ### ⚠️ 상·하류가 비어 있으면 **선행 경고가 아예 안 나옵니다**
> S-01 선행 경고와 S-03 주변 지점이 이 표에 기대고 있습니다. 담당자 지정은
> **「향후 적용」으로 확정**돼 있어(2026-08-19), 화면이 그 사실을 알립니다.

---

### 3-22. `camera_sensors` — 지점 × 센서 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) **idx** | N | → `cameras.id` (CASCADE) |
| 3 | `sensor_id` | varchar(64) **idx** | N | → `sensors.id` (CASCADE) |
| 4 | `role` | varchar(16) | N | `reference`(참고) / `primary`(주) |
| 5 | `distance_m` | integer | Y | |
| 6 | `note` | text | N | |

**UNIQUE** `(camera_id, sensor_id)`

---

### 3-23. `camera_zones` — 지점 × 구역 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) | N | → `cameras.id` (CASCADE) |
| 3 | `zone_id` | varchar(64) **idx** | N | → `zones.id` (CASCADE) |
| 4 | `coverage` | varchar(16) | N | `full` / `partial` / `edge` |
| 5 | `note` | text | N | |

**UNIQUE** `(camera_id, zone_id)`

> ### ★ `coverage` 가 필요한 이유
> 사각지대 분석(S-63)에서 **「구역 가장자리만 보이는 지점」을 「그 구역을
> 감시 중」으로 세면 안 됩니다.** 세면 실제로는 비어 있는 구역이 덮여 있는
> 것으로 나옵니다.

---

### 3-24. `hazard_sop_map` — 위험 유형 × 등급 → 대응 절차 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `hazard_type_code` | varchar(32) | N | → `hazard_types.code` (CASCADE) |
| 3 | `level_code` | varchar(16) | N | 등급 |
| 4 | `zone_id` | varchar(64) | N | 구역별로 절차가 다를 때. 빈 값이면 전역 |
| 5 | `sop_step_id` | integer | Y | → `sop_steps.id` (CASCADE) |
| 6 | `seq` | integer | N | 절차 순서 |

**UNIQUE** `(hazard_type_code, level_code, zone_id, sop_step_id)` ·
**INDEX** `(hazard_type_code, level_code)`

> ### ★ **2026-08-19 오후 — 읽는 코드와 화면(S-98)을 붙였습니다**
> `core/sop.py` 의 `mapped_steps()`·`steps_for_event()` 가 이 표를 읽고,
> **S-98 위험유형별 SOP 관리** 화면에서 사람이 채웁니다.
>
> ★ **더합니다. 갈아치우지 않습니다.** 세분류는 대분류의 조치를 포함하고
> 더 있습니다 — 지하차도 침수도 침수라서 「영상으로 현장 확인」은 그대로
> 필요합니다. 매핑을 한 줄 넣었다고 공통 단계가 사라지면 지금보다 나쁩니다.
>
> ⚠️ **연결이 비어 있으면 기존 동작과 완전히 같습니다** — 그래서 회귀
> 위험이 없습니다. 시험으로 못박았습니다.
>
> ⚠️ **`zone_id` 는 아직 비어 있습니다**(구역 0행). 경남 SFR-012 의
> 「도, 시·군별」을 쓰려면 구역 등록이 선행돼야 하고, 화면이 그 사실을
> 그대로 표시합니다.

---

### 3-25. `sop_steps` — 표준 대응 절차 ★ (2026-08-18 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `domain` | varchar(16) | N | |
| 3 | `level` | varchar(16) | N | |
| 4 | `seq` | integer | N | 순서 |
| 5 | `title` | varchar(160) | N | |
| 6 | `detail` | text | N | |
| 7 | `required` | boolean | N | 거짓이면 건너뛸 수 있음 |
| 8 | `builtin` | boolean | N | 제품 기본 제공. **참이면 삭제 불가** |
| 9 | `is_active` | boolean | N | |
| 10 | `updated_at` / `updated_by` | | N | |

**INDEX** `(domain, level, seq)`

---

### 3-26. `event_sop_checks` — 이벤트별 절차 이행 ★ (2026-08-18 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `event_id` | integer | N | → `events.id` (CASCADE) |
| 3 | `step_id` | integer | Y | → `sop_steps.id` (SET NULL) |
| 4 | `step_title` | varchar(160) | N | ★ **찍은 시점의 제목을 복사** |
| 5 | `user_id` | integer | Y | → `users.id` (SET NULL) |
| 6 | `login_id` | varchar(64) | N | ★ 계정이 지워져도 남는 아이디 |
| 7 | `skipped` | boolean | N | 건너뜀 |
| 8 | `note` | text | N | |
| 9 | `checked_at` | timestamptz | N | |

**UNIQUE** `(event_id, step_id)`

> ### ★ 제목과 아이디를 **복사해 둡니다**
> 절차 문구가 나중에 바뀌어도, **그때 무엇을 보고 찍었는지**가 남아야 합니다.
> 계정이 삭제돼도 `login_id` 로 누가 했는지 남습니다. **조치 이력은 지워지면
> 안 되는 기록**입니다.

---

### 3-27. `event_evidence` — 이벤트 증적 ★ (2026-08-18 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `event_id` | integer **idx** | N | → `events.id` (CASCADE) |
| 3 | `camera_id` | varchar(64) | N | |
| 4 | `domain` / `level` | varchar(16) | N | `level` **idx** |
| 5 | `kind` | varchar(16) | N | 스냅샷 / 영상 |
| 6 | `path` | varchar(512) | N | 파일 경로 |
| 7 | `bytes` | integer | N | |
| 8 | `sha256` | varchar(64) | N | ★ **무결성 확인용** |
| 9 | `duration_sec` | integer | N | |
| 10 | `captured_at` | timestamptz **idx** | N | |
| 11 | `note` | text | N | |
| 12 | `boxes` | **jsonb** | Y | ★ 2026-08-20 추가. **이벤트가 난 위치** — 도메인마다 실제 관측값만 (아래 설명) |
| 13 | `frame_w` | integer | Y | `boxes` 좌표가 기준으로 삼는 원본 프레임 너비(px) |
| 14 | `frame_h` | integer | Y | 〃 높이(px) |

> ### ★ `sha256` 를 남기는 이유
> 증적은 **나중에 다툼이 생겼을 때** 꺼내는 것입니다. 파일이 바뀌지 않았음을
> 보일 수 없으면 증적으로서 값어치가 줄어듭니다.

> ### ★ `boxes` — 2026-08-20 추가, S-88 팝업이 「이벤트가 발생한 부분」에 씀
> **지어낼 수 없는 것은 비워 둔다.** `nullable=True` 인 이유 — 그 틱에 물
> 픽셀이 없었다, 밀집도만으로 뜬 이벤트라 특정 사람이 없다 등 「없는 경우」가
> 실제로 있다. 도메인별 뜻:
>
> - **침수** — 세그멘테이션이 물이라고 판정한 마스크의 경계
> - **인파** — 그 이벤트(배회·침입)를 일으킨 트랙의 실제 추적 상자
> - **노면** — YOLO 가 그 프레임에서 실제로 찾은 손상 상자
>
> 기존 행은 전부 `boxes IS NULL` 로 남는다 — 재수집하지 않는다.

---

### 3-28. `video_disclosures` — 영상 반출 관리대장 ★ (2026-08-18 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `requested_at` | timestamptz **idx** | N | |
| 3 | `requester_org` | varchar(128) **idx** | N | 요청 기관 |
| 4 | `requester_name` | varchar(64) | N | 요청자 |
| 5 | `requester_contact` | varchar(64) | N | 연락처 |
| 6 | `legal_basis` | varchar(255) | N | ★ **법적 근거** |
| 7 | `purpose` | text | N | 목적 |
| 8 | `camera_ids` | text | N | 대상 지점 |
| 9 | `period_from` / `period_to` | timestamptz | Y | 대상 기간 |
| 10 | `method` | varchar(16) | N | `view`(열람) / `copy`(사본) |
| 11 | `masked` | boolean | N | 비식별 처리 여부 |
| 12 | `handled_at` / `handler_id` / `handler_login` | | Y/Y/N | 처리자 |
| 13 | `disposal_due` / `disposed_at` | timestamptz | Y | 파기 예정·완료 |
| 14 | `note` | text | N | |
| 15 | `corrects_id` | integer | Y | → `video_disclosures.id` (SET NULL) |
| 16 | `correction_reason` | text | N | ★ 정정 사유 |
| 17 | `created_at` / `created_by` | | N | |

> ### ★ 수정 대신 **정정 건을 새로 만듭니다**
> 관리대장은 고쳐 쓰면 대장이 아닙니다. 잘못 적었으면 **원본은 그대로 두고**
> 정정 건을 새로 만들어 `corrects_id` 로 잇습니다. 무엇을 왜 고쳤는지가 남습니다.

> ### ⚠️ 개인정보가 들어 있는 표입니다
> 요청자 이름·연락처가 들어갑니다. 7절 개인정보 항목 식별에 포함됩니다.

---

### 3-29. `crowd_observations` — 인파 관측 이력 ★ (2026-08-17 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) **idx** | N | |
| 3 | `camera_name` | varchar(120) | N | |
| 4 | `person_count` | integer | N | 인원 수 |
| 5 | `density_index` | float | N | 밀집도 |
| 6 | `mean_speed` | float | N | 평균 이동속도 |
| 7 | `surge` | float | N | 급증 배율 |
| 8 | `dispersion` / `divergence` | float | N | 분산 / 발산 |
| 9 | `risk_code` | varchar(32) | N | |
| 10 | `risk_score` | float | N | |
| 11 | `severity` | integer | N | |
| 12 | `drivers` | varchar(200) | N | ★ **무엇이 점수를 올렸는가** |
| 13 | `failed` | boolean | N | ★ **관측 실패 표시** |
| 14 | `source` | varchar(24) | N | |
| 15 | `observed_at` | timestamptz **idx** | N | |

**INDEX** `(camera_id, observed_at)`

> ### ★ `failed` 를 「0명」과 구분합니다
> 관측이 실패한 것과 **사람이 없는 것**은 전혀 다릅니다. 섞으면 카메라가
> 죽어 있는 동안 「한산함」으로 보입니다.

> ### ★ `drivers` — 점수만으로는 판단이 안 됩니다
> 「위험 72점」만 보면 무엇을 해야 할지 모릅니다. 밀집도인지 급증인지가
> 있어야 관제요원이 움직일 수 있습니다.

---

### 3-30. `detection_feedback` — 탐지 피드백·오탐 판정 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `event_id` | integer **idx** | Y | → `events.id` (SET NULL). ★ **미탐이면 NULL** |
| 3 | `camera_id` | varchar(64) **idx** | N | |
| 4 | `domain` | varchar(16) | N | |
| 5 | `hazard_type_code` | varchar(32) | N | |
| 6 | `level` | varchar(16) | N | |
| 7 | `verdict` | varchar(20) **idx** | N | 아래 표 참고 |
| 8 | `reason` | text | N | ★ **오탐·미탐이면 필수** |
| 9 | `occurred_at` | timestamptz | Y | |
| 10 | `user_id` | integer | Y | → `users.id` (SET NULL) |
| 11 | `login_id` | varchar(64) | N | |
| 12 | `created_at` | timestamptz **idx** | N | |
| 13 | `used_for_training` | boolean | N | ★ **학습에 실제로 쓴 것만 참** |

| `verdict` | 뜻 |
|---|---|
| `true_positive` | 정탐 |
| `false_positive` | 오탐 |
| `unclear` | **판단 보류** |
| `false_negative` | **미탐** — 붙일 이벤트가 없어 `event_id` 가 NULL |

> ### ★ 「모르겠다」를 선택지로 둔 이유
> 흐린 화면에서 억지로 정·오탐을 고르게 하면 **학습 데이터가 오염됩니다.**
> 판단 보류가 정직한 답인 경우가 있습니다.

> ### ★ 오탐률 분모에서 **판단 보류와 미탐을 뺍니다**
> 판정이 0건이면 오탐률은 `0%` 가 아니라 **`None`(모름)** 입니다.
> 아무도 안 찍었는데 「오탐 0%」로 보이면 안 됩니다.

> ### ⚠️ **판정이 곧 학습이 아닙니다**
> 한 번 잘못 찍은 클릭이 모델을 바꾸면 안 되므로, 학습 편입에는 별도 확인
> 단계를 둡니다. `used_for_training` 은 **실제로 쓴 것만** 참입니다.

---

### 3-31. `multiview_layouts` — 멀티뷰 배치 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `user_id` | integer **idx** | N | → `users.id` (CASCADE) |
| 3 | `name` | varchar(64) | N | 배치 이름 |
| 4 | `tiles` | integer | N | 분할 수 (4 / 9 / 16) |
| 5 | `cameras` | jsonb | Y | ★ 칸 **순서대로** 담은 지점 ID |
| 6 | `is_default` | boolean | N | 기본 배치 |
| 7 | `updated_at` | timestamptz | N | |

**UNIQUE** `(user_id, name)`

근거 — 경남 **SFR-001** 「화면 분할, 배치 등을 자유롭게 구성하고 **저장**」.

> ### ★ 빈 칸은 **빈 문자열로 자리를 지킵니다**
> 목록을 당겨 버리면 사용자가 정한 배치가 무너집니다.

> ### ⚠️ 통상 사양(64~128채널)보다 **적게 열었습니다**
> 스냅샷 방식에서 그만큼 띄우면 서버가 감당하지 못합니다. **실측 없이 숫자를
> 늘리지 않았습니다.** 늘리는 것은 쉽고 되돌리는 것은 어렵습니다.

---

### 3-32. `user_prefs` — 사용자별 화면 설정 ★ (2026-08-19 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `user_id` | integer **idx** | N | → `users.id` (CASCADE) |
| 3 | `key` | varchar(64) | N | `multiview.running` 등 |
| 4 | `value` | varchar(255) | N | |
| 5 | `updated_at` | timestamptz | N | |

**UNIQUE** `(user_id, key)`

> ### ★ `app_settings` 와 자리가 다릅니다
> 그쪽은 **기관**이 정하는 값(기관명·임계값)이고, 여기는 **사람**이 정하는
> 값입니다. 야간 근무자와 주간 근무자가 같은 화면을 다르게 씁니다.

> ### ★ 브라우저(localStorage)에 두지 않은 이유
> 관제요원은 **자리를 옮겨 앉습니다.** 브라우저에 두면 옆자리 PC 로 가는 순간
> 설정이 사라지고, 본인은 **껐다고 생각한 것이 켜져 있는** 상태가 됩니다.
> 멈춤 설정에서 그것은 **없는 안전을 보는** 일입니다.

> ### ⚠️ 개인정보를 넣지 않습니다 — 화면 설정만 담는 자리입니다

> ### ⚠️ 알 수 없는 값은 **꺼짐으로 보지 않습니다**
> `"0"` 만 꺼짐입니다. 값이 깨졌을 때 관제 화면이 멎는 쪽으로 기울면 위험합니다.

---

### 3-33. `shift_handovers` — 교대 인수인계 ★ (2026-08-18 신설)

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `shift_date` | timestamptz | N | 근무일 |
| 3 | `shift_name` | varchar(32) | N | 주간 / 야간 등 |
| 4 | `from_user_id` | integer | Y | → `users.id` (SET NULL). 넘기는 사람 |
| 5 | `from_login` | varchar(64) | N | ★ 계정이 지워져도 남는 아이디 |
| 6 | `to_user_id` | integer | Y | → `users.id` (SET NULL). 받는 사람 |
| 7 | `to_login` | varchar(64) | N | |
| 8 | `summary` | text | N | 근무 중 요약 |
| 9 | `todo` | text | N | 넘길 일 |
| 10 | `open_events` | jsonb | Y | ★ **인계 시점에 열려 있던 이벤트** |
| 11 | `status` | varchar(16) | N | 작성중 / 제출 / 확인 |
| 12 | `submitted_at` | timestamptz | Y | 제출 시각 |
| 13 | `acknowledged_at` | timestamptz | Y | ★ **받는 사람이 확인한 시각** |
| 14 | `ack_note` | text | N | 확인 의견 |
| 15 | `created_at` / `updated_at` | timestamptz | N | |

> ### ★ 열린 이벤트를 **그 시점 그대로 박제**합니다
> 나중에 다시 조회하면 그동안 닫힌 것이 빠져, **인계 때 무엇이 열려 있었는지**
> 알 수 없게 됩니다. 인계는 「그때 무엇을 넘겼는가」가 전부입니다.

> ### ★ `acknowledged_at` 이 비어 있으면 **인계가 끝난 것이 아닙니다**
> 넘긴 사람만 적고 받는 사람이 안 봤으면, 아무도 모르는 채로 넘어갑니다.

---

### 3-34. `traffic_observations` — 교통 관측 이력 ★ (2026-08-21 신설)

flood/traffic 도메인 분리로 교통이 1급 도메인이 되면서, `crowd_observations`
와 같은 이유로 신설했습니다 — 판정을 매 틱 계산해 놓고 버리면 **평상시
기준선**을 만들 수 없습니다.

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) **idx** | N | |
| 3 | `camera_name` | varchar(120) | N | 카메라가 지워져도 남도록 복제 |
| 4 | `rain_mm_h` | float | N | 강수량 |
| 5 | `speed_drop` | float | N | 평상시 대비 평균속도 감소율(0~1) |
| 6 | `queue_len` | integer | N | 정체 대기열 |
| 7 | `stalled_count` | integer | N | 정지·고착 차량 수 |
| 8 | `risk_code` | varchar(32) | N | ★ `TWR_*` (아래 참고) |
| 9 | `risk_score` | float | N | |
| 10 | `severity` | integer | N | |
| 11 | `drivers` | varchar(200) | N | ★ **무엇이 점수를 올렸는가** |
| 12 | `failed` | boolean | N | ★ **관측 실패 표시** |
| 13 | `source` | varchar(24) | N | synthetic / hls / rtsp / video |
| 14 | `observed_at` | timestamptz **idx** | N | |

**INDEX** `(camera_id, observed_at)`

> ### ★ `risk_code` 는 `hazard_types.code` 와 **다른 네임스페이스**입니다
> 이 칸에는 `TWR_RAIN_CONGESTION` 처럼 **판정 결과 상태**가 들어가고,
> `hazard_types` 에는 `traffic_rain_congestion` 처럼 **무엇이 발생했나(유형)**
> 가 들어갑니다. 두 축을 같은 칸에 넣으려다 `normalize_hazard_code()` 가
> 모르는 코드를 대분류로 뭉개는 문제가 있었으므로, 매핑 표를 두지 않고
> 분리한 채로 씁니다.

> ### ★ `failed` 를 「정체 없음」과 구분합니다
> 프레임을 못 받은 것과 **실제로 원활한 것**은 다릅니다. 섞으면 카메라가
> 죽어 있는 동안 「원활」로 보입니다 — 인파·노면과 같은 이유입니다.

> ### ⚠️ 아직 기록하는 코드가 없습니다
> 이번 회차는 **표만** 만들었습니다. 실제 기록은 이벤트 이중화 작업 이후
> `service/event_sync.py` 에서 붙입니다 — 도메인 분리가 끝나기 전에 넣으면
> 침수·교통이 뒤섞인 값이 쌓입니다.

### 3-35. `training_runs` — AI 모델 학습 실행 기록 ★ (2026-08-25 신설)

관리자 화면(신규)에서 침수·교통위험·인파관리·도로 노면 4개 도메인의 모델
학습을 실행할 수 있게 하며, 그 실행 기록을 남깁니다.
`docs/pending_tasks.md` A-7(★★ 「재학습·MLOps — 성능 기록 표가 아예 없음」)
에서 지적된 공백을 메웁니다.

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `domain` | varchar(16) **idx** | N | flood / traffic / crowd / road |
| 3 | `params` | jsonb | Y | 학습 스크립트에 실제로 넘긴 인자(재현·감사용) |
| 4 | `status` | varchar(16) **idx** | N | queued / running / succeeded / failed / stopped |
| 5 | `pid` | integer | Y | 학습 서브프로세스 PID |
| 6 | `log_path` | varchar(500) | N | 표준출력·표준에러 로그 파일 경로 |
| 7 | `output_dir` | varchar(500) | N | 체크포인트·지표 CSV가 쌓이는 폴더 |
| 8 | `metrics` | jsonb | Y | ★ 도메인마다 키가 다릅니다(아래 참고) |
| 9 | `error` | text | N | 실패·중지 사유 |
| 10 | `started_by` | integer → users **FK** | Y | |
| 11 | `started_by_name` | varchar(64) | N | 계정이 사라져도 「누가 시켰나」가 남도록 복제 |
| 12 | `started_at` | timestamptz **idx** | N | |
| 13 | `finished_at` | timestamptz | Y | |

**INDEX** `domain` · `status` · `started_at`
**FK** `started_by` → `users.id` **SET NULL** (계정이 지워져도 학습 기록은 남는다)

> ### ★ `metrics` 를 JSONB 로 둔 이유
> 4개 도메인이 학습 스크립트·평가 지표가 전부 다릅니다 — 침수는 시맨틱
> 세그멘테이션(`val_f1`·`val_iou`), 교통·노면은 YOLO 객체검출
> (`precision`·`recall`·`mAP50`), 인파는 자체 IoU 매칭
> (`precision`·`recall`·`f1`). 공통 컬럼으로 강제하면 도메인마다 안 쓰는
> 컬럼이 늘어납니다.

> ### ⚠️ 학습 완료가 운영 반영을 뜻하지 않습니다
> 학습이 끝나도 새 체크포인트가 자동으로 운영에 적용되지 않습니다 —
> 「AI 모델 운영·설정」 화면에서 관리자가 직접 골라야 합니다. `output_dir`
> 은 그 화면에서 새 체크포인트를 찾을 때 쓰는 자리입니다.

> ### ⚠️ 재기동으로 실행 핸들을 잃을 수 있습니다
> `pid` 는 남지만, 서버가 재기동되면 실제 종료 코드를 다시 알 수 없습니다
> (`core/training_jobs.py` 참고). 그럴 때는 `status`를 `stopped`로,
> `error`에 "서버 재기동으로 결과를 알 수 없다"고 **사실대로** 적지,
> `succeeded`·`failed`로 추측해 단정하지 않습니다.

### 3-36. `live_detection_state` — 카메라별 「지금」 판정의 크로스 프로세스 조회 ★ (2026-08-31 신설)

API 게이트웨이 Phase 4로 침수·교통위험이 별도 프로세스(flood-service·
traffic-service)가 되면서, platform-shell의 홈 화면이 더 이상 그
프로세스들의 인메모리 상태(`RiskStore`)를 직접 읽을 수 없게 됐습니다 —
인파(`crowd_observations`)·노면(`road_inspections`의 최신 1건)이 이미
겪어 해결한 것과 같은 문제입니다.

`traffic_observations`(이력, 20초 주기 기록)와는 목적이 다릅니다. 이
표는 이력을 남기지 않고 **카메라·도메인당 행 하나만 계속 덮어씁니다**
(1초 주기 upsert) — 홈 화면이 필요한 것은 이력이 아니라 "지금 등급"
하나뿐입니다.

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `camera_id` | varchar(64) | N | |
| 3 | `domain` | varchar(16) | N | flood / traffic — 다른 도메인이 늘어나도 재사용 가능한 자유 문자열 |
| 4 | `level` | varchar(8) | N | 관심/주의/경계/심각, 판정 없으면 빈 문자열 |
| 5 | `available` | boolean | N | ★ 이번 틱에 실제로 판정이 돌았는가(아래 참고) |
| 6 | `updated_at` | timestamptz **idx** | N | |

**UNIQUE** `(camera_id, domain)` — 도메인별 최신 1건만 있으면 되므로
매 upsert가 이 행 하나를 계속 덮어씁니다.

> ### ★ `available` 이 왜 필요한가
> `level`이 있어도 그 값을 신뢰할 수 없는 경우가 있습니다 — 예: 재배포
> 서버 장애로 이번 틱은 판정을 아예 안 돌린 카메라. 각 도메인의
> `water_available`/`traffic_enabled` 개념과 같은 자리이며, 이 플래그가
> False면 조회 쪽(홈 화면)이 그 카메라를 "관측 없음"으로 걸러야 합니다.

> ### ★ 두 도메인을 표 하나로 같이 담는 이유
> `_flood_summary()`/`_traffic_summary()`가 읽는 모양이 사실상 같습니다
> (카메라별 최신 등급 1개). 표를 두 개 만들면 같은 조회 로직을 두 번
> 베끼게 됩니다 — `domain` 컬럼 하나로 충분합니다.

### 3-37. `signup_requests` — 가입 신청 ★ (2026-09-01 신설)

로그인 화면 셀프서비스 가입 신청 → 관리자 승인 흐름의 상태를 담습니다.
`notifications`(S-50 알림 승인)와 같은 모양(요청/승인/거절 상태 +
요청자·승인자 FK + 감사로그)의 요청/승인 표입니다.

| # | 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|---|
| 1 | `id` | integer **PK** | N | |
| 2 | `login_id` | varchar(64) **idx** | N | 신청한 아이디. 승인 전까지는 `users.login_id`처럼 유일할 필요가 없다(같은 아이디로 대기 신청은 앱 단에서 막지만, 거절 후 재신청은 허용) |
| 3 | `name` | varchar(64) | N | |
| 4 | `dept` | varchar(64) | N | |
| 5 | `requested_role` | varchar(8) | N | SYS/MGR/OPR — 신청자가 희망한 역할일 뿐, 최종 역할은 승인 시 관리자가 다시 정한다 |
| 6 | `requested_domains` | varchar(255) | N | 콤마 구분 도메인 값. `user_domains`처럼 조인 테이블로 안 만드는 이유는 승인 전까지는 참고 정보일 뿐 권한 판정에 쓰이지 않기 때문 |
| 7 | `reason` | text | N | 신청 사유(선택 입력) |
| 8 | `pw_hash` | varchar(255) | N | ★ 신청 시점에 이미 검증·해시된 값. 승인 시 그대로 `users.pw_hash`로 옮긴다 — 관리자는 비밀번호를 알 수 없다 |
| 9 | `status` | varchar(16) **idx** | N | pending → approved / rejected |
| 10 | `ip` | varchar(64) | N | 남용 방지(같은 IP 반복 제출 제한) 조회용 |
| 11 | `reviewed_by` | integer FK→users | Y | SET NULL |
| 12 | `reviewed_at` | timestamptz | Y | |
| 13 | `reject_reason` | text | N | |
| 14 | `created_user_id` | integer FK→users | Y | 승인으로 실제 만들어진 계정. SET NULL |
| 15 | `created_at` | timestamptz **idx** | N | |
| 16 | `updated_at` | timestamptz | N | |

**INDEX** `(login_id, status)` — 대기 중인 중복 신청 조회. `(ip,
created_at)` — 남용 방지 조회.

> ### ★ 비밀번호를 신청 시점에 미리 해시해 두는 이유
> 신청자가 신청서에서 직접 정한 비밀번호를 관리자가 알 필요도, 다시
> 물을 필요도 없게 합니다. 관리자는 역할·담당 도메인만 정해 승인하고,
> 그 순간 이 해시를 그대로 `users.pw_hash`로 옮깁니다 — 그래서
> `users.must_change_password`도 False로 만듭니다(관리자가 임시
> 비밀번호를 발급하는 기존 경로와의 핵심 차이).

---

## 4. 관계와 삭제 정책

| 참조 | 정책 | 근거 |
|---|---|---|
| `user_domains.user_id` → users | **CASCADE** | 계정이 사라지면 배정도 무의미 |
| `event_actions.event_id` → events | **CASCADE** | 이벤트 없는 조치 이력은 의미 없음 |
| `audit_logs.user_id` → users | **SET NULL** | ★ 계정을 지워도 감사 기록은 남아야 함 |
| `notifications.requested_by/approved_by` | SET NULL | 발송 이력 보존 |
| `facility_controls.requested_by` | SET NULL | 제어 이력 보존 |
| `citizen_reports.reported_by/reviewed_by` | SET NULL | 제보 이력 보존 |
| `events.assignee_id/closed_by` | SET NULL | 이벤트 이력 보존 |
| `citizen_reports.event_id` → events | SET NULL | 이벤트가 지워져도 제보는 남음 |
| `signup_requests.reviewed_by/created_user_id` | SET NULL | 계정이 지워져도 가입 신청 기록은 보존 |
| `facility_controls.event_id` → events | SET NULL | 동일 |
| `camera_domains.camera_id` → cameras | **CASCADE** | 지점이 사라지면 서비스 지정도 무의미 |
| `camera_rois.camera_id` → cameras | **CASCADE** | 지점이 사라지면 ROI도 무의미 |
| `camera_rois.updated_by` → users | SET NULL | 편집자 계정이 지워져도 ROI는 유지 |

**원칙** — 이력성 테이블은 `SET NULL` + **식별자 복제**(`login_id`)로,
사람이 사라져도 「누가 했는지」가 남습니다. 종속 테이블만 `CASCADE` 입니다.

### ⚠️ `events.block_id` 에 외래키를 걸지 않은 이유

값은 `cameras.id` 와 같지만 FK가 없습니다. 의도한 선택입니다.

- **폐지된 지점의 과거 이벤트가 남아야 합니다.** FK + CASCADE면 지점을 지울 때
  사건 기록이 함께 사라지고, RESTRICT면 지점을 영영 못 지웁니다.
- 이벤트는 카메라가 아닌 **장소**에서 난 사건입니다. 카메라를 교체하거나 ID를
  바꿔도 사건의 존재는 달라지지 않습니다.

대신 **참조 무결성이 DB로 보장되지 않습니다.** 존재하지 않는 `block_id` 로
이벤트가 만들어질 수 있으므로, 화면은 `place_name` 복제 컬럼을 함께 표시해
지점이 지워진 뒤에도 사람이 읽을 수 있게 합니다(13-5절).

---

## 5. 인덱스

| 테이블 | 인덱스 | 대상 화면·질의 |
|---|---|---|
| `users` | `login_id` UNIQUE | 로그인 |
| `user_domains` | `(user_id, domain)` UNIQUE | 중복 배정 방지 |
| `events` | `status` | S-02 상태 필터 |
| `events` | `detected_at DESC` | 이벤트 목록 정렬 |
| `events` | `(domain, block_id)` | **중복 억제 조회** (열린 이벤트 찾기) |
| `notifications` | `status` | S-50 승인 대기함 |
| `notifications` | `requested_at DESC` | S-51 이력 |
| `audit_logs` | `created_at DESC` | S-91 최근순 |
| `audit_logs` | `dept` | MGR 본인 부서 범위 |
| `citizen_reports` | `status`, `created_at DESC` | S-43 |
| `facility_controls` | `created_at DESC` | S-11 제어 이력 |
| `camera_domains` | `(camera_id, domain)` UNIQUE (`uq_camera_domain`) | 상시 탐지 대상 조회 · 중복 지정 방지 |
| `camera_rois` | `(camera_id, domain)` UNIQUE (`uq_camera_roi`) | 도메인별 ROI 조회 |

> **현재 카메라 테이블에는 UNIQUE 외의 인덱스가 없습니다.** 지점이 7개소라
> 전체 스캔이 더 빠릅니다. 수백 개소로 늘면 `camera_domains(domain, continuous)`
> 복합 인덱스를 검토하세요 — 상시 탐지 대상 조회가 기동 시마다 도는 질의입니다.

---

## 6. 상태 전이도

### 이벤트 (`events.status`)

```
   [자동 탐지]
        │
        ▼
     open ──── 확인(acknowledge) ────► progress
   (미처리)                             (처리중)
        │                                  │
        └──── 종결 / 오탐 신고 ────────────┴──► closed
                                               (완료)
```
- 종결은 **MGR 이상**만 가능 (OPR 불가)
- 오탐 신고는 종결까지 함께 처리하며 `false_positive=true`
- **등급이 내려가도 자동으로 닫지 않습니다** — 종결은 사람의 판단

### 알림 (`notifications.status`)

```
  requested ──승인──► approved ──발송──► sent
  (승인 대기)              │                (완료)
       │                   └──발송 실패──► failed
       └──반려──► rejected
```
- **주민 경보(`channel='cbs'`)는 요청자 ≠ 승인자** (역할 무관)
- 「심각」 단독 발송 설정 시에만 예외이며 `needs_post_approval=true`

### 제보 (`citizen_reports.status`)

```
  received ──확인──► reviewed ──전환──► converted (이벤트 생성)
   (접수)                │
                        └──반려──► rejected
```

---

## 7. ★ 개인정보 항목 식별

**개인정보 처리방침·영향평가(PIA)의 입력값입니다.**

| 테이블 | 컬럼 | 구분 | 비고 |
|---|---|---|---|
| `users` | `login_id`, `name`, `dept` | 일반 개인정보 | 직원 정보 |
| `users` | `pw_hash` | — | bcrypt 해시. 복호화 불가 |
| `audit_logs` | `login_id`, `dept`, **`ip`** | 일반 개인정보 | 접속지 IP |
| `event_actions` | `login_id` | 일반 개인정보 | |
| `facility_controls` | `login_id` | 일반 개인정보 | |
| `citizen_reports` | `login_id` | 일반 개인정보 | |
| `citizen_reports` | **`lat`, `lng`** | ⚠️ **위치정보** | 제보자의 이동 기록이 될 수 있음 |
| `citizen_reports` | **사진 파일** | ⚠️ **영상정보** | 사람·차량번호 포함 가능 |
| `notifications` | `recipients_count` | — | 수를 셀 뿐 번호는 미저장 |

### 7-1. 수집하지 않는 것

- **주민등록번호·계좌번호 등 고유식별정보 없음**
- **수신자 전화번호를 DB에 저장하지 않습니다** — 발송 시점에 설정에서 읽고 수만 기록
- 제보자 연락처를 받지 않습니다

### 7-2. 적용한 보호 조치

| 대상 | 조치 |
|---|---|
| 비밀번호 | bcrypt 해시. 평문 미저장·미로깅 |
| 제보 사진 | **사람 영역 픽셀화 후에만 저장. 원본 즉시 삭제** |
| 제보 사진 | 담당자가 언제든 영구 파기 가능 |
| 사진 접근 | 파일명 검증으로 경로 조작 차단 + 로그인·권한 확인 |
| 감사 로그 | 접근 이력 자체를 기록 |

### 7-3. ⚠️ 미해결

| 항목 | 상태 |
|---|---|
| **차량번호판 마스킹** | **미구현** — 전용 모델 없음. 담당자 육안 확인에 의존 |
| 개인정보 영향평가 대상 여부 | **미확인** — 지자체 개인정보 담당 확인 필요 |
| 위치정보 수집 동의 절차 | 미구현 — 현재는 사용자가 버튼을 눌러야 첨부 |

---

## 8. ★ 보존기간·파기 정책

### ⚠️ 현재 상태 — **파기 정책이 구현되어 있지 않습니다**

모든 테이블이 무한히 증가합니다. 자동 파기 코드가 없습니다.
**시범운영 전에 반드시 수립해야 합니다.**

### 8-1. 권고안 (확인 필요)

| 테이블 | 권고 보존기간 | 근거 |
|---|---|---|
| `audit_logs` | **3년 이상** | 공공기관 감사 대응 |
| `events` / `event_actions` | 3~5년 | 재난 대응 기록 |
| `notifications` | 3년 이상 | 통보 이력은 분쟁 시 근거 |
| `facility_controls` | **5년 이상** | 시설 제어는 인명 관련 |
| `citizen_reports` (레코드) | 1~3년 | |
| **`citizen_reports` 사진** | **처리 완료 후 즉시 또는 6개월** | 개인정보 최소보관 원칙 |
| `app_settings` | 영구 | 현재 설정값 |

> ⚠️ **위 숫자는 제 권고안이며 검증된 법정 기준이 아닙니다.**
> 공공기관은 기록물 관리 규정에 따라 보존연한이 다르므로 **발주처 확인이
> 필수**입니다. 특히 감사로그 보존연한은 기관 지침을 따라야 합니다.

### 8-2. 구현 시 고려사항

- 학습용으로 판정된 제보 사진(`usable_for_training=true`)은 별도 보관 정책 필요
  — 학습 목적 보관이 개인정보 목적 외 이용에 해당하는지 확인
- 파기 작업 자체도 감사 로그에 남겨야 함
- `events` 파기 시 `event_actions` 는 CASCADE로 함께 삭제됨

---

## 9. 보안

### 9-1. 적용된 것

| 항목 | 내용 |
|---|---|
| 비밀번호 | bcrypt |
| 접근 제어 | 전역 가드 미들웨어. **규칙 미등록 경로는 기본 거부** |
| 감사 추적 | 로그인·계정변경·통보·설정변경·제어 |
| 경로 조작 | 제보 사진 파일명 화이트리스트 검증 |

### 9-2. ⚠️ 미적용 — 운영 전 필요

| 항목 | 현재 | 필요 |
|---|---|---|
| **DB 계정 분리** | 앱이 단일 계정 `urbanguard` 로 전권 | 읽기전용 계정(통계·백업) 분리 권장 |
| **감사로그 무결성** | "수정·삭제 금지"가 코드 주석에만 | DB 권한으로 `UPDATE/DELETE` 회수 검토 |
| **전송 암호화** | HTTP | 운영 시 HTTPS + `URBANGUARD_COOKIE_SECURE=1` |
| **저장 암호화** | 없음 | 고유식별정보가 없어 의무 대상은 아닐 것으로 보이나 **확인 필요** |
| 세션 관리 | 서명 쿠키 | 강제 로그아웃·동시접속 제한 불가. 필요 시 `sessions` 테이블 |
| 비밀번호 이력 | 직전 값만 비교 | 「최근 N개 재사용 금지」 요구 시 이력 테이블 |

---

## 10. 용량 산정

### 10-1. 행 크기 추정

| 테이블 | 행당 대략 | 비고 |
|---|---|---|
| `events` | ~0.5 KB | `detail` JSONB 포함 |
| `event_actions` | ~0.3 KB | |
| `audit_logs` | ~0.5 KB | `before`/`after` JSONB |
| `notifications` | ~0.5 KB | |
| `citizen_reports` | ~0.5 KB | **사진 제외** |
| 제보 사진 (파일) | **1~4 MB/장** | DB 밖 |

### 10-2. 시나리오 — 지점 20개소, 3교대 운영

| 항목 | 가정 | 연간 |
|---|---|---|
| 이벤트 | 지점당 하루 1건 | 7,300건 ≈ **4 MB** |
| 조치 이력 | 이벤트당 4건 | 29,200건 ≈ **9 MB** |
| 감사 로그 | 하루 500건 | 182,500건 ≈ **90 MB** |
| 통보 | 하루 5건 | 1,825건 ≈ **1 MB** |
| **DB 소계** | | **약 105 MB/년** |
| 제보 사진 | 하루 10장 × 2 MB | **약 7 GB/년** ← 지배적 |

**DB 자체는 작습니다. 용량을 결정하는 것은 제보 사진입니다.**
사진 보존기간(8절)이 서버 용량 산정의 핵심 변수입니다.

권고: DB 볼륨 **10 GB 이상**(3년 + 여유), 사진 저장소 **별도 산정**.

---

## 11. 초기 데이터와 마이그레이션

### 11-1. 초기 구축 순서

```bash
# 1. 스키마 생성
.\.venv\Scripts\python.exe -m alembic upgrade head

# 2. 최초 시스템관리자 생성 (임시 비밀번호 자동 발급)
.\.venv\Scripts\python.exe -m tot_dashboard.core.bootstrap `
    --login-id admin --name 관리자 --dept 정보통신과
```

- 계정이 하나도 없으면 로그인이 불가능하므로 **부트스트랩이 필수**입니다
- 발급된 임시 비밀번호는 **한 번만 출력**되며, 최초 로그인 시 변경이 강제됩니다
- `app_settings` 는 비어 있어도 코드 기본값으로 동작합니다
  (`org_name`, `board_bg=#0F1420`, `facility_mode=advise`)

### 11-2. 마이그레이션 이력

| 순서 | 리비전 | 내용 |
|---|---|---|
| 1 | `090da80fde89` | users, user_domains, audit_logs, notifications |
| 2 | `b93ae00191f5` | app_settings |
| 3 | `f58f8822b3f4` | users.must_change_password |
| 4 | `667d7b9a3ffe` | events, event_actions |
| 5 | `d7eb8f22aa38` | facility_controls |
| 6 | `2213c99ed3a2` | citizen_reports |
| **7** | **`81b687282d9d`** | **cameras, camera_domains, camera_rois** ← **head** |

`alembic_version` 실측값이 `81b687282d9d` 로, 위 7번까지 반영된 상태입니다.

**교훈** — 3번에서 기존 행이 있는 테이블에 기본값 없는 `NOT NULL` 컬럼을 추가하려다
실패했습니다. `server_default` 를 주고 이후 걷어내는 방식으로 해결했습니다.
운영 DB에 컬럼을 추가할 때 같은 문제가 재발할 수 있습니다.

---

## 12. 운영

### 12-1. 백업

```bash
# 스키마 + 데이터
pg_dump -h <host> -p <port> -U urbanguard -Fc urbanguard > urbanguard_YYYYMMDD.dump
```

⚠️ **DB 백업만으로는 복구되지 않는 것**

| 항목 | 위치 | 별도 백업 필요 |
|---|---|---|
| 제보 사진 | `data/reports_photo/` | ✅ |
| ROI 설정 | `configs/roi/*.json` | ✅ |
| 감시지점·임계값·알림규칙 | `configs/*.yaml`, `blocks.json` | ✅ |
| 시설 정의 | `configs/facilities.json` | ✅ |
| 브랜드 자산 | `docs/brand/` | ✅ |

**설정이 DB가 아니라 파일에 있는 항목이 많습니다.** DB만 백업하면 복구 후
지점·ROI·임계값이 모두 사라집니다.

### 12-2. 복구

```bash
pg_restore -h <host> -p <port> -U urbanguard -d urbanguard -c urbanguard_YYYYMMDD.dump
```
복구 후 `configs/` 와 `data/reports_photo/` 를 함께 되돌린 뒤 서비스를 재시작합니다.

### 12-3. 개발 → 운영 이관

| 항목 | 주의 |
|---|---|
| PostgreSQL 버전 | 개발 17.7. 운영도 **17.x 권장** |
| 개발용 인스턴스 | `.tools/pgsql` — **폴더 안에만 존재**. 폴더 삭제 시 DB도 소멸 |
| 접속 정보 | `.env` 의 `URBANGUARD_DATABASE_URL` 만 교체 |
| 세션 키 | `URBANGUARD_SECRET_KEY` **반드시 재발급** |
| 시간대 | 운영 서버 시간대 확인 (1-3절) |

---

## 13. ⚠️ 알려진 제약과 개선 과제

### 13-1. 이벤트 중복 방지가 애플리케이션에만 있습니다 ★

「같은 (도메인, 지점)에 열린 이벤트는 하나」 규칙이 **코드에만 있고 DB 제약이
없습니다.** 동기화 스레드와 인파 API가 동시에 같은 지점을 처리하면 중복 이벤트가
생길 수 있습니다.

**개선안** — 부분 유니크 인덱스
```sql
CREATE UNIQUE INDEX uq_event_open_per_block
  ON events (domain, block_id)
  WHERE status IN ('open', 'progress');
```
현재 운영 규모(지점 7개소, 20초 주기)에서는 발생 확률이 낮지만, 지점이 늘고
인파 도메인이 상시 구동되면 실제 문제가 됩니다.

### 13-2. 알림 계층이 `channel` 값에 얹혀 있습니다

`channel='cbs'` 로 주민 경보를 판별합니다. 채널이 늘면 깨지므로 `tier` 컬럼
분리를 권합니다.

### 13-3. `notifications.case_id` 가 문자열입니다

이벤트 ID를 varchar로 담고 있어 **외래키가 없습니다.** 이벤트가 삭제돼도 통보
이력이 끊긴 채 남습니다. 정수 FK로 전환을 검토해야 합니다.
(기존 crowd 케이스 ID와의 호환 때문에 문자열로 시작한 흔적입니다.)

### 13-4. 파기 정책 미구현 (8절)

### 13-5. `place_name` 등 표시용 복제 컬럼

정규화 관점에서는 중복이지만, **이력 보존을 위한 의도적 비정규화**입니다.
지점명이 바뀌어도 과거 기록이 당시 이름을 유지합니다.

### 13-6. 카메라 설정 파일이 폴백으로 남아 있습니다 ★

`cameras` 3종으로 이관을 마쳤지만, `configs/blocks.json` 과 `configs/roi/*.json`
을 **지우지 않았습니다.** DB를 읽지 못할 때 서비스가 죽는 대신 파일로 내려가도록
한 조치입니다.

- 위험 — **두 곳의 값이 달라지면 어느 쪽이 맞는지 알기 어렵습니다.**
  DB를 고쳐도 파일은 그대로이므로, 폴백이 발동한 순간 옛 설정으로 돌아갑니다.
- 현재 판정 — 환경변수 `TOT_BLOCKS_PATH` 가 있으면 파일, 없으면 DB입니다.
- 조치 — 시범운영에서 DB 경로가 안정적으로 확인되면 **폴백을 제거**하고
  파일은 백업(`configs/_backup/`)으로만 남기는 것을 권고합니다.

### 13-8. 노면 **최신 결과**는 DB에 없습니다 (이력은 있습니다) ★

지점별 **최신 1건**은 `road/results.py` 의 메모리 딕셔너리에 있습니다.
재시작하면 화면이 「미분석」로 돌아갑니다.

**다만 2026-08-14 부터 관측 이력은 `road_inspections` 에 남습니다**(3-13절).
재시작해도 「이 지점이 어떻게 변해 왔는지」는 이어집니다. 최신 상태만 잃습니다.

- 손상이 잡히면 `events` 에도 남습니다
- 「봤는데 없었다」와 「분석 실패」는 **이벤트에 남지 않으므로** 이력 테이블이
  따로 필요했습니다 — 프레임 0장인 관측을 「이상 없음」으로 오해하면 못 본
  구간을 점검 완료로 처리하게 됩니다

### 13-8-B. (구) 노면 탐지 결과는 DB에 없습니다

지점별 최신 결과는 `road/results.py` 의 **메모리 딕셔너리**에 있습니다.
재시작하면 화면이 「미분석」로 돌아갑니다.

- 손상이 잡히면 `events` 에는 남으므로 **이력 자체가 사라지지는 않습니다**
- 다만 「지금 이 지점의 최신 점검 상태」는 복원되지 않습니다
- 점검 이력을 표로 관리하려면 `road_inspections` 같은 테이블이 필요합니다(14절)

### 13-7. 상시 탐지 변경 — 노면은 즉시, 인파는 재시작 (2026-08-13 개선)

`camera_domains.continuous` 를 바꿨을 때의 반영 시점이 도메인마다 다릅니다.

| 도메인 | 반영 | 근거 |
|---|---|---|
| **노면** | **즉시** | 정적인 대상을 순회하는 방식이라 목록을 갈아끼워도 잃을 상태가 없습니다. `core/config_rev.py` 가 저장 시 신호를 보내 자고 있던 순회 스레드를 깨웁니다 |
| 인파 | 재시작 | 카메라마다 ByteTrack 추적기를 둡니다. 실행 중 교체하면 추적 ID와 배회 타이머가 초기화돼, 「같은 사람이 N초 이상 머물렀는가」 판정을 한동안 못 합니다 |
| 침수 | 재시작 | 지점별 연속 분석 스레드 |

즉 **목록을 즉시 반영하는 대가로 탐지를 놓치는 도메인은 그대로 두었습니다.**
화면(S-80)의 안내 문구도 도메인별로 나눠 표시합니다.

⚠️ 노면도 **진행 중인 관측(15초)이 끝난 뒤** 목록을 다시 읽습니다. 그 짧은
틈은 S-44 화면에 「반영 대기」로 표시합니다 — 「상시」 배지만 보고 관측 중이라고
읽으면 안 되기 때문입니다.

### 13-9. 학습 데이터는 DB가 아니라 파일입니다 ★ (2026-08-13 신규)

노면 AI 학습용 프레임은 `data/datasets/road_cctv_own/raw/<지점>/` 에 **JPG
파일**로 쌓입니다. DB에는 켜짐/꺼짐(`app_settings.road_collect`)만 있습니다.

- 이미지를 DB에 넣으면 백업·복제가 감당되지 않습니다
- 출처·마스킹 상태는 폴더마다 `meta.jsonl` 에 한 줄씩 남깁니다
  (수집 시각·경로·해상도·`mask_status`·`plate_masked`)
- **총량 상한 2GB**, 지점당 하루 8장, 같은 지점 최소 30분 간격
- ⚠️ **파기 기능이 없습니다.** 상한에 닿으면 수집이 멈출 뿐 지우지 않습니다

---

## 14. 향후 추가 예정 테이블

미구현 화면에 필요한 테이블입니다. **미리 자리를 잡아두어 나중에 구조가 어긋나지
않게** 합니다.

| 테이블(안) | 화면 | 주요 컬럼(안) |
|---|---|---|
| `shift_handovers` | S-04 교대 인수인계 | from_user_id, to_user_id, note, event_ids(jsonb), created_at |
| `resources` | S-10 자원·출동 | id, type(순찰차/펌프차/인력), name, status |
| `dispatches` | S-10 | event_id, resource_id, dispatched_at, arrived_at, note |
| `cbs_requests` | S-12 재난문자 | event_id, grade, area, body, requested_by, exported_at |
| `integration_status` | S-70 외부 연계 | target(통합플랫폼/112/119/재난), state, last_sync_at |
| `road_inspections` | S-40 노면 점검 이력 | camera_id, grade, defect_count, frames, analyzed_at |
| `sessions` | (보안 요건 시) | 강제 로그아웃·동시접속 제한용 |
| `password_history` | (보안 요건 시) | 재사용 금지 |

---

## 15. 확인이 필요한 사항

| # | 항목 | 확인처 | 영향 |
|---|---|---|---|
| **1** | **감사로그 등 보존연한** | 발주처 / 기록물 관리 규정 | 8절 파기 정책 전체 |
| **2** | **개인정보 영향평가 대상 여부** | 지자체 개인정보 담당 | 7절 · 제보 기능 범위 |
| 3 | 저장 암호화 의무 대상 여부 | 법무 / 개인정보 담당 | 9-2절 |
| 4 | DB 계정 분리 정책 | 지자체 정보통신 | 9-2절 |
| 5 | 운영 DB 사양·버전 | 지자체 인프라 | 10절 용량 산정 |
| 6 | 제보 사진 저장소 위치·용량 | 지자체 인프라 | 10-2절 |
| 7 | 학습 목적 사진 보관의 적법성 | 개인정보 담당 | 8-2절 |
| 8 | 한글 정렬 요구 여부 | 발주 사양서 | 1-1절 lc_collate |

**1·2번이 최우선입니다.** 둘 다 시범운영 전에 답이 있어야 하며, 특히 파기 정책은
현재 **구현 자체가 없어** 데이터가 무한히 쌓이는 상태입니다.

---

## 참고
- `system_architecture.md` — 시스템 아키텍처 설계서
- `interface_spec.md` — 인터페이스 정의서
- `security_design.md` — 보안 설계서 (9절의 상세)
- `privacy_assessment.md` — 개인정보 영향평가 사전검토서 (7·8절의 상세)
- `../ui_design_spec.md` — 화면 설계서 (6-4절 스키마 스케치는 이 문서로 대체)
- `src/tot_dashboard/core/models.py` — 실제 ORM 정의
- `migrations/versions/` — 마이그레이션 25개(누적, 이전 회차부터 갱신되지 않고 있던 값을 2026-08-25에 실측으로 정정)
- `../_archive/202608080514_db_design_spec.md` — v1 (카메라 3테이블 누락)
