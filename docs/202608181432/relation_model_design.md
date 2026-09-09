# 관계 모델 설계서 — Urban Ontology 1단계

> UrbanGuard · 앤시정보기술(주)
> 2026-08-18 14:32 · v1
> 판단 근거: 같은 폴더 `tech_adoption_judgment.md`
> **설계 문서입니다. 코드는 아직 바꾸지 않았습니다.**

---

## 0. 결론 먼저

○ **신규 테이블 8개 · 기존 테이블 변경 2건 · 추가 설치 `ltree` 하나.**
  별도 DBMS 없이 PostgreSQL 안에서 끝냅니다

○ **설계 원칙은 「화면이 쓰는 관계만 만든다」입니다.** 온톨로지는 크게 만들수록
  아무도 안 씁니다. **8개 표 전부 어느 화면이 쓰는지 명시**했습니다

○ ⚠️ **기존 문자열 키는 건드리지 않습니다.** `cameras.id` 가 `BLOCK-CHORYANG`
  같은 문자열이고 `events.block_id`·ROI 파일명이 여기에 묶여 있습니다.
  **정수 PK로 바꾸면 그 연결이 전부 끊깁니다**

○ ★ **가장 먼저 고칠 것은 「어휘가 코드에 흩어져 있다」는 사실입니다.**
  위험등급 4단계가 `calibration.py` 에 문자열로 박혀 있고,
  위험유형은 **정의 테이블 자체가 없습니다**

---

## 1. 지금 상태 — 무엇이 문제인가

### 1-1. 어휘가 테이블이 아니라 코드에 있습니다

| 어휘 | 현재 | 문제 |
|---|---|---|
| **위험등급** 관심·주의·경계·심각 | `core/calibration.py` 에 **문자열 반환**, `roles.py:213` 에 `CRITICAL_LEVELS` 집합 | 기관마다 등급 이름이 다를 수 있는데 **바꾸려면 코드를 고쳐야** 함 |
| **도메인** flood·crowd·road | `roles.Domain` **Enum** | 도메인 추가 시 배포 필요 |
| **위험유형** (`events.event_type`) | `String(32)` — **정의 테이블 없음** | 무엇이 올 수 있는지 **아무 데도 안 적혀 있음** |

경남 SFR-012는 「**최소 4단계 이상**」이라 합니다 — 5단계를 쓰는 기관이 있으면
지금 구조로는 **코드 수정**입니다.

### 1-2. 관계가 아예 없습니다

| 관계 | 현재 |
|---|---|
| 카메라 ↔ 센서 | **없음** |
| 카메라 ↔ 카메라 (인접) | **없음** |
| 카메라 ↔ 구역 | `sido`·`sigungu` **문자열 두 칸**뿐 |
| 위험유형 ↔ SOP | `sop_steps.domain`·`level` **문자열 매칭** |
| 카메라 방향 | **없음** — S-63 사각지대 분석 불가 |

---

## 2. 설계 원칙

| # | 원칙 | 이유 |
|---|---|---|
| 1 | **PostgreSQL 단일** — 추가 설치는 `ltree` 하나 | 폐쇄망 납품에서 설치 대상은 곧 비용 |
| 2 | **기존 테이블은 최소로 건드린다** | 문자열 키에 39지점·이벤트 이력이 묶여 있음 |
| 3 | **화면이 쓰는 관계만 만든다** | 안 쓰는 표는 썩는다 |
| 4 | **어휘는 데이터로, 코드에서 뺀다** | 기관마다 다름 · 배포 없이 바꿔야 함 |
| 5 | **표준은 나중에 매핑한다** | 데이터허브 표준 미확인 — `std_uri` 칸만 비워 둔다 |

> 5번이 중요합니다. **지금 표준을 못 고른다고 설계를 미루지 않습니다.**
> 각 어휘 표에 `std_uri` 칸을 두고 **비워 둔 채** 시작하면, 나중에
> NGSI-LD든 CityGML이든 **행만 채우면** 됩니다.

---

## 3. 3층 구조

```
 [1층] 어휘   무엇을 「말」로 인정하는가
        risk_levels · hazard_types
                 │
 [2층] 개체   세상에 있는 것
        zones · sensors        (+ 기존 cameras · sop_steps)
                 │
 [3층] 관계   무엇이 무엇과 이어지는가
        camera_links · camera_sensors · camera_zones
        zone_hazards · hazard_sop_map
```

---

## 4. 신규 테이블 8개

### 4-1. `risk_levels` — 위험등급 어휘

| 컬럼 | 형 | 설명 |
|---|---|---|
| `code` | `varchar(16)` PK | `interest`·`caution`·`alert`·`severe` |
| `seq` | `int` NOT NULL | 1~N. **정렬과 비교의 유일한 기준** |
| `label` | `varchar(32)` NOT NULL | 「관심」「주의」「경계」「심각」 |
| `color` | `varchar(16)` | 화면 색 |
| `is_critical` | `bool` | 단독 발송 허용 등급인가 (`roles.py:213` 대체) |
| `std_uri` | `varchar(256)` | **표준 매핑용. 지금은 빈 칸** |
| `is_active` | `bool` | |

**초기 4행**은 현재 코드값(`관심`·`주의`·`경계`·`심각`)을 그대로 넣습니다.

> ⚠️ **기존 `events.level` 문자열은 그대로 둡니다.** 이 표는 「설명하는 표」로
> 시작하고, 화면·판정이 이 표를 읽도록 **단계적으로** 옮깁니다.
> 한 번에 바꾸면 이벤트 이력이 깨집니다.

**쓰는 화면** — S-01 등급 색·정렬, S-82 임계값, S-86 SOP 단계 조건

### 4-2. `hazard_types` — 위험유형 어휘

| 컬럼 | 형 | 설명 |
|---|---|---|
| `code` | `varchar(32)` PK | `flood_underpass`·`crowd_density`·`road_pothole`·`wildfire`… |
| `domain` | `varchar(16)` | flood·crowd·road (기존 도메인과 연결) |
| `parent_code` | `varchar(32)` FK→self | **계층**. `flood` → `flood_underpass` |
| `label` | `varchar(64)` NOT NULL | 「지하차도 침수」 |
| `source` | `varchar(16)` | `own`(자체) / `national`(국가표준) / `rfp`(발주요구) |
| `std_uri` | `varchar(256)` | 표준 매핑용. 지금은 빈 칸 |
| `is_active` | `bool` | |

**초기 행**은 우리 3도메인 + **경남 SFR-008이 명시한 유형**을 넣습니다.

| code | label | source | 근거 |
|---|---|---|---|
| `flood_underpass` | 지하차도 침수 | rfp | 경남 SFR-008 |
| `flood_drainage` | 배수로 침수 | rfp | 경남 SFR-008 |
| `flood_river` | 하천 범람 | rfp | 경남 SFR-008 |
| `crowd_density` | 인파 밀집 | own | |
| `road_pothole` | 노면 파손 | own | |
| `wildfire` | 산불 확산 | rfp | 경남 SFR-008 |
| `typhoon_damage` | 태풍 피해 | rfp | 경남 SFR-008 |

> ★ 이 표가 있으면 제안서에 **「발주 요구 유형을 데이터로 관리합니다」**를
> 근거와 함께 쓸 수 있습니다. 산불·태풍은 **모델이 없어도 어휘는 등록**해
> 두는 것이 맞습니다 — 확장 계획을 보여 주는 자리입니다.

**쓰는 화면** — S-02 필터, S-03 상세, S-83 이벤트 규칙, S-63 목적별 분류

### 4-3. `zones` — 구역 (행정 + 위험)

| 컬럼 | 형 | 설명 |
|---|---|---|
| `id` | `varchar(64)` PK | `ADM-BUSAN-DONG`·`HZ-FLOOD-CHORYANG` |
| `kind` | `varchar(16)` NOT NULL | `admin`(행정) / `hazard`(위험) / `custom` |
| `path` | `ltree` | `kr.busan.donggu` — **계층 질의용** |
| `name` | `varchar(128)` NOT NULL | |
| `hazard_type_code` | `varchar(32)` FK→`hazard_types` | `kind='hazard'` 일 때 |
| `source` | `varchar(32)` | `침수흔적도`·`인명피해우려지역` 등 출처 |
| `note` | `text` | |
| `is_active` | `bool` | |

**`ltree` 를 쓰는 이유** — 경남이 **도 + 18개 시·군** 2계층입니다.
「창원시 아래 전부」를 `path <@ 'kr.gyeongnam.changwon'` 한 줄로 찾습니다.

> ⚠️ `ltree` 는 **trusted 확장**이라 `urbanguard` 계정으로 설치됩니다(2026-08-18 실사).
> 그래도 **`path` 없이도 동작하도록** 설계합니다 — 확장이 막힌 현장에서는
> `varchar` 로 대체하고 `LIKE 'kr.busan.%'` 로 씁니다.

⚠️ **도형(폴리곤)은 이번에 넣지 않습니다.** PostGIS가 없습니다.
지금은 **구역의 존재와 계층**만 다루고, 지도 위 도형은 별도 판단입니다.

**쓰는 화면** — S-01 지역 필터, S-63 사각지대, S-60 기관별 통계

### 4-4. `sensors` — 외부 센서

| 컬럼 | 형 | 설명 |
|---|---|---|
| `id` | `varchar(64)` PK | |
| `kind` | `varchar(32)` NOT NULL | `water_level`·`rain_gauge`·`iot`… |
| `name` | `varchar(128)` NOT NULL | |
| `lat` · `lng` | `float` | |
| `source` | `varchar(32)` | `kma`(기상청)·`local`·`manual` |
| `external_id` | `varchar(128)` | 원 시스템의 ID |
| `last_seen_at` | `timestamptz` | **끊긴 센서 식별용** |
| `is_active` | `bool` | |

**근거** — 경남 SFR-005 「기존 센서 및 IoT 연동」.
**기상청 API 모듈이 이미 있으므로**(`docs/202608161334`) 첫 행은 거기서 채웁니다.

**쓰는 화면** — S-06 통합 타임라인, S-89 장비 상태 감시

### 4-5. `camera_links` — 카메라 ↔ 카메라 ★ 핵심

| 컬럼 | 형 | 설명 |
|---|---|---|
| `id` | `int` PK | |
| `from_camera_id` | `varchar(64)` FK→`cameras` | |
| `to_camera_id` | `varchar(64)` FK→`cameras` | |
| `kind` | `varchar(16)` NOT NULL | `adjacent`(인접) / `upstream`(상류) / `downstream`(하류) / `overlap`(시야 겹침) |
| `distance_m` | `int` | |
| `bearing_deg` | `int` | from → to 방위 |
| `auto` | `bool` | **좌표로 자동 생성했는가, 사람이 정했는가** |
| `note` | `text` | |

**UNIQUE(`from_camera_id`, `to_camera_id`, `kind`)**

**근거 두 가지**

> 경남 기대효과: 「**시·군 경계를 넘나드는** 산불 확산, 하천 범람, 도로 통제 등
> 재난 상황을 **연속 추적**하여 대응 단절 최소화」
> 202608172039 조사: 「알람 발생 카메라 **주변의 다중 카메라 Live 자동 확인**」 —
> KT GiGAeyes에는 있고 우리에겐 없던 기능

**`upstream`/`downstream` 이 침수의 핵심입니다.** 상류 지점이 먼저 차오르면
하류를 **미리** 경고할 수 있습니다. 이건 지금 우리가 못 하는 일입니다.

**초기 데이터** — 좌표 기준 **반경 500m 이내를 `adjacent`, `auto=true`** 로 자동 생성.
상·하류는 **사람이 지정**합니다(자동으로 알 수 없습니다).

**쓰는 화면** — S-03 인접 카메라, S-05 멀티뷰 연동, S-06 타임라인

### 4-6. `camera_sensors` — 카메라 ↔ 센서

| 컬럼 | 형 | 설명 |
|---|---|---|
| `id` | `int` PK | |
| `camera_id` | `varchar(64)` FK | |
| `sensor_id` | `varchar(64)` FK | |
| `role` | `varchar(16)` | `primary`(대표) / `reference`(참고) |
| `distance_m` | `int` | |

**UNIQUE(`camera_id`, `sensor_id`)** — **N:1 을 허용**합니다(센서 하나에 카메라 여럿).

**근거** — 경남 SFR-005 원문 그대로입니다.

> 「각 센서의 물리적 위치 정보와 **인근에 설치된 CCTV 카메라를 1:1 또는 N:1로
> 매핑**하여, 센서 이벤트 발생 시 **연관된 카메라 영상을 즉시 호출**」

**쓰는 화면** — S-06 타임라인, S-03 상세(센서값 병기)

### 4-7. `camera_zones` — 카메라 ↔ 구역

| 컬럼 | 형 | 설명 |
|---|---|---|
| `id` | `int` PK | |
| `camera_id` | `varchar(64)` FK | |
| `zone_id` | `varchar(64)` FK | |
| `coverage` | `varchar(16)` | `full` / `partial` / `edge` |

**UNIQUE(`camera_id`, `zone_id`)**

> ⚠️ 기존 `cameras.sido`·`sigungu` **문자열은 지우지 않습니다.** 화면·엑셀
> 내려받기가 그 값을 쓰고 있습니다. 이 표는 **위험구역 관계**부터 채우고,
> 행정구역은 나중에 옮깁니다.

**쓰는 화면** — S-63 사각지대, S-60 구역별 통계

### 4-8. `hazard_sop_map` — 위험유형 × 등급 → SOP

| 컬럼 | 형 | 설명 |
|---|---|---|
| `id` | `int` PK | |
| `hazard_type_code` | `varchar(32)` FK | |
| `level_code` | `varchar(16)` FK→`risk_levels` | 빈 값이면 전 등급 |
| `zone_id` | `varchar(64)` FK, NULL | **기관별 차등**용. NULL이면 공통 |
| `sop_step_id` | `int` FK→`sop_steps` | |
| `seq` | `int` | |

**근거** — 경남 SFR-012 「단계별 SOP를 자동으로 **도, 시·군별로** 제시」.
`zone_id` 칸이 그 「도, 시·군별」입니다.

> ⚠️ **기존 `sop_steps.domain`·`level` 매칭은 그대로 둡니다.** 이 표는
> **더 정밀한 매칭이 필요할 때 우선 적용**되는 덧표입니다. 비어 있으면
> 지금과 똑같이 동작합니다 — **회귀 위험이 없습니다.**

**쓰는 화면** — S-03 SOP 체크리스트, S-10 SOP 실행 보드, S-86 편집

---

## 5. 기존 테이블 변경 2건 — 둘 다 nullable 추가

### 5-1. `cameras` — 방향각 추가

| 추가 컬럼 | 형 | 이유 |
|---|---|---|
| `bearing_deg` | `int` NULL | 카메라가 향하는 방위(0~359) |
| `tilt_deg` | `int` NULL | 상하 각도 |
| `fov_deg` | `int` NULL | 화각 |
| `purpose` | `varchar(32)` NULL | 설치 목적 (`crime`·`disaster`·`traffic`…) |

**근거** — KLID SFR-14 「CCTV의 좌표정보 및 **방향각(상하/좌우)**을 활용해
GIS 시각화」「각 CCTV를 **설치 목적별로 분류**」

**S-63 사각지대 분석의 선행 조건**입니다. 방향을 모르면 커버리지를 그릴 수 없습니다.

> ⚠️ **39지점의 실제 방향각을 우리는 모릅니다.** 값은 비어 있게 시작하고
> S-80에서 입력받습니다. **추측해서 채우지 않습니다.**

### 5-2. `events` — 위험유형 코드 추가

| 추가 컬럼 | 형 | 이유 |
|---|---|---|
| `hazard_type_code` | `varchar(32)` NULL FK | 기존 `event_type` 문자열과 **병행** |

기존 `domain`·`event_type` 은 **그대로 둡니다**. 새 이벤트부터 코드를 채우고,
과거 이벤트는 NULL로 남습니다. **이력이 깨지지 않습니다.**

---

## 6. 관계 질의는 이렇게 합니다 (GraphDB 없이)

### 6-1. 인접 카메라 N홉 — 재귀 CTE

```sql
WITH RECURSIVE nearby(camera_id, depth) AS (
    SELECT :start_id, 0
  UNION
    SELECT l.to_camera_id, n.depth + 1
      FROM camera_links l
      JOIN nearby n ON l.from_camera_id = n.camera_id
     WHERE n.depth < :max_hop          -- 폭주 방지. 기본 2
       AND l.kind IN ('adjacent', 'downstream')
)
SELECT * FROM nearby WHERE depth > 0;
```

### 6-2. 구역 하위 전체 — `ltree`

```sql
SELECT * FROM zones WHERE path <@ 'kr.gyeongnam.changwon';
```

`ltree` 가 없는 현장에서는

```sql
SELECT * FROM zones WHERE path_text LIKE 'kr.gyeongnam.changwon.%';
```

### 6-3. 성능 전제

경남이 **4,500채널**입니다. 링크를 채널당 4개로 잡아도 **18,000행**입니다.
인덱스가 있으면 재귀 CTE 2홉은 **밀리초 단위**입니다.

> 실측 기준선 — 순수 SQL haversine 39지점 **7.2ms** (`docs/202608171820`).
> 관계 질의도 같은 방식으로 **실측한 뒤** 판단합니다.

---

## 7. 인덱스

| 표 | 인덱스 |
|---|---|
| `camera_links` | `(from_camera_id, kind)` · `(to_camera_id)` |
| `camera_sensors` | `(camera_id)` · `(sensor_id)` |
| `camera_zones` | `(zone_id)` · `(camera_id)` |
| `zones` | **GiST** `(path)` — `ltree` 전용 |
| `hazard_sop_map` | `(hazard_type_code, level_code)` |
| `hazard_types` | `(domain)` · `(parent_code)` |

---

## 8. 마이그레이션 순서

| # | 내용 | 되돌릴 수 있나 |
|---|---|---|
| 1 | `CREATE EXTENSION IF NOT EXISTS ltree` | 예 |
| 2 | 어휘 2표 생성 + **초기 행 삽입** | 예 |
| 3 | 개체 2표 생성 | 예 |
| 4 | 관계 4표 생성 | 예 |
| 5 | `cameras` 4칸 · `events` 1칸 **nullable 추가** | 예 |
| 6 | `camera_links` **자동 생성** (반경 500m, `auto=true`) | 예 — `auto=true` 만 지우면 됨 |

**6단계를 분리한 이유** — 자동 생성분과 사람이 정한 것을 **섞으면 안 됩니다.**
`auto` 칸이 그 구분입니다.

> ⚠️ **기존 데이터를 고치는 단계가 하나도 없습니다.** 전부 추가뿐이라
> 되돌리기가 쉽고, **회귀 시험이 깨질 이유가 없습니다.**

---

## 9. 이 설계가 여는 것 — 화면과의 대응

| 화면 | 필요한 관계 | 준비됨? |
|---|---|---|
| **S-03** 인접 카메라 · 센서값 병기 | `camera_links` · `camera_sensors` | 이 설계로 **가능** |
| **S-06** 통합 타임라인 | `camera_sensors` · `sensors` | **가능** |
| **S-10** SOP 실행 (기관별 차등) | `hazard_sop_map.zone_id` | **가능** |
| **S-63** 사각지대 | `cameras.bearing_deg` · `camera_zones` | **방향각 입력 후** 가능 |
| **S-05** 멀티뷰 연동 팝업 | `camera_links` | **가능** |
| 상·하류 선행 경고 (신규 가치) | `camera_links.kind='upstream'` | **가능** |

---

## 10. 범위 밖 — 이번에 하지 않는 것

| # | 항목 | 이유 |
|---|---|---|
| 1 | 도형(폴리곤) 저장 | PostGIS 없음 — 별도 판단 |
| 2 | RDF·OWL 추론 | 우리 규모에 과함 |
| 3 | 표준 URI 매핑 | 데이터허브 표준 **미확인** |
| 4 | 기존 `level`·`event_type` 문자열 **교체** | 이력이 깨짐. 단계적으로 |
| 5 | `pgvector` · RAG | 4단계 |
| 6 | 화면 구현 | 2단계 |

---

## 11. 확인이 필요한 것

| # | 항목 | 왜 |
|---|---|---|
| 1 | **위험등급 4단계 코드명**을 영문으로 할지 한글로 할지 | `risk_levels.code` 확정 |
| 2 | **인접 판정 반경** — 500m가 맞는지 | 6단계 자동 생성 기준 |
| 3 | **39지점의 방향각**을 누가 입력하는지 | S-63 선행 조건 |
| 4 | 위험유형에 **산불·태풍을 지금 넣을지** | 모델은 없지만 어휘만 등록하는 것이 맞다고 봄 |
| 5 | **경남 데이터허브 표준** | `std_uri` 채우는 시점 |

---

## 12. ⚠️ 구현하면서 설계와 달라진 것 (2026-08-18 추가)

같은 날 구현했고, **네 가지가 설계와 달라졌습니다.** 설계서를 고치지 않고
아래에 남깁니다 — 무엇을 왜 바꿨는지가 설계 자체보다 중요합니다.

| # | 설계 | 구현 | 이유 |
|---|---|---|---|
| 1 | `zones.path` 를 **`ltree`** 로 | **`varchar` + `LIKE 'prefix.%'`** | `ltree` 가 trusted 라 쓸 수는 있지만, **안 쓰면 추가 설치가 0** 이 된다. 경남이 도+18시군이라 수백 행이라 성능 차이가 없다 |
| 2 | 어휘 초기 행을 **마이그레이션에** | **`core/vocabulary.py` 로 분리** | 시험 DB 는 속도 때문에 `create_all` 을 써서 마이그레이션을 타지 않는다. **오류 코드 사전·SOP 가 이미 쓰는 방식**과 같게 맞췄다 |
| 3 | `hazard_types` 에 없던 칸 | **`detectable` 추가** | ⚠️ **어휘 등록과 탐지 가능은 다르다.** 이 칸이 없으면 화면이 「우리는 산불도 탐지한다」는 거짓말을 만든다 |
| 4 | 위험유형 **7행** | **11행** | 상위 코드(`flood`·`crowd`·`road`)와 `crowd_loitering` 을 더했다. 상위가 없으면 「침수 전체」로 묶어 볼 수 없다 |

### ★ 시험이 잡은 실제 결함

`neighbors()` 의 재귀 CTE 가 **출발 지점을 결과에 되돌려 넣고 있었습니다.**

인접은 양방향이라 `A → B → A` 로 **두 홉 만에 자기 자신**이 됩니다.
`depth > 0` 만으로는 걸러지지 않습니다 — 되돌아온 A 는 depth 2 이기 때문입니다.

```sql
 WHERE depth > 0
   AND camera_id <> CAST(:start AS varchar)   -- ← 이 줄이 없었다
```

`test_neighbors_two_hop_reaches_c` 가 잡았습니다. **화면에 붙이기 전에 잡혀
다행입니다** — 인접 카메라 목록에 자기 자신이 섞여 나왔을 것입니다.

---

## 13. 구현 결과

| 항목 | 결과 |
|---|---|
| 신규 표 | **8개** |
| 기존 표 변경 | `cameras` **4칸** · `events` **1칸** (전부 nullable) |
| 추가 설치 | **없음** (`ltree` 도 안 씀) |
| 마이그레이션 | `b3f7d21ce940` — **업/다운 양방향 검증 완료** |
| 신규 모듈 | `core/relations.py` · `core/vocabulary.py` |
| 신규 시험 | `tests/core/test_relations.py` **25건** |
| 어휘 초기값 | 위험등급 **4행** · 위험유형 **11행**(탐지 가능 8 · 어휘만 3) |

---

## 14. 아직 안 한 것

| # | 항목 | 언제 |
|---|---|---|
| 1 | S-80 화면에 **방향각 입력 칸** | 2단계 |
| 2 | `build_adjacency()` 를 **화면에서 실행** | 2단계 |
| 3 | 상·하류 **지정 화면** | 2단계 |
| 4 | `hazard_sop_map` 을 **SOP 조회에 연결** | 2단계 — 지금은 표만 있고 읽는 코드가 없다 |
| 5 | `events.hazard_type_code` **채우기** | 판정 코드 수정 필요 |
| 6 | `risk_levels` 를 **판정에 실제로 사용** | `calibration.py` 수정 — 회귀 위험이 있어 별도 회차 |

> 4~6번이 중요합니다. **표를 만들었다고 쓰이는 것이 아닙니다.**
> 지금은 **비어 있어도 기존 동작이 그대로**인 상태이고, 이건 의도한 것입니다.
