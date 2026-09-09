# "침수교통" 도메인 분리 — 침수(flood) / 교통(traffic) 완전 독립화

작성 2026-08-21 · UrbanGuard · 앤시정보기술(주)
**갱신 2026-08-21 — 계획 → 구현 완료. 실제 결과는 아래 「11. 구현 결과」 참고**

---

## 0. 결론 먼저

지금 "flood" 도메인 하나가 **침수**와 **교통위험**을 한 덩어리로 취급하고 있습니다
(`core/roles.py`의 `Domain.FLOOD` 주석부터 `"flood" = "침수·교통위험"`). 이를
**완전히 독립된 2개 도메인**(flood=침수, traffic=교통)으로 분리하는 작업입니다.

사용자 확인 결과, 이번 작업의 범위는 다음 3가지로 확정했습니다.

| # | 확정 사항 |
|---|---|
| 1 | **계산 로직까지 완전 분리** — 화면·이벤트·권한만이 아니라 `RiskEngine`의 교통
가중치(6%), `SemanticAgent`의 하천수위 참조까지 걷어내 두 계산을 완전히 독립시킴 |
| 2 | 강우+정체+하천수위가 섞인 혼합 판정은 **두 도메인에 각각 별도 이벤트 생성**(중복 허용) |
| 3 | 침수 위험도(`RiskEngine`)도 **독립적으로 SOLAPI 알림을 새로 발생**시킴(현재는 화면 표시용일 뿐 알림 트리거가 아님) |

**핵심 발견**: `traffic_weather/` 패키지가 이미 있지만 **DB·권한·도메인 체계와 전혀
연결되지 않은 고립된 PoC**입니다. "기존 교통 도메인을 승격"하는 게 아니라 **신규
도메인을 새로 구축**하는 작업이며, `traffic_weather`의 판정 로직(SemanticAgent,
RiskDecisionAgent, RISK_CATALOG)은 재료로 재사용합니다.

**작업량 감(느낌)**: 6개 조사 + 4개 설계를 거쳐 파악한 결과, 코드를 고쳐야 하는
지점이 **28개 파일**에 걸쳐 있습니다. 계산 엔진(6개 파일)과 DB(2개 마이그레이션)는
비교적 명확하고 위험이 낮지만, 화면 분리(5개 파일)와 하드코딩 리터럴 정리(10개
파일)는 손이 많이 갑니다. **1~2주 규모의 리팩터링**으로 판단합니다(실측 공수는
개발부 확인 필요).

**아직 확인 필요한 것 4가지**(6절에 상세, 기본값을 정해두고 진행 가능):
1. `traffic` 도메인 모델이 속도 추정까지 하는지(=지면평면 캘리브레이션 필요 여부)
2. `traffic` 화면 메뉴의 정확한 S-2X 화면번호(설계서 미확정)
3. `flood_risk_grade`(1~5) → 이벤트 등급(관심/주의/경계/심각) 매핑표 확정
4. 과거 `domain="flood"` 이벤트를 재분류할지(**권장: 안 함** — 근거 데이터 없음)

---

## 1. 왜 이 작업이 필요한가

- 화면 라벨("침수·교통위험" 탭 하나)과 이벤트 도메인이 하나로 뭉쳐 있어, 관제요원이
  "이게 침수 때문인지 정체 때문인지" 화면에서 구분하기 어렵습니다.
- SOLAPI 알림이 지금은 **교통·기상 판정에서만** 발생하고, 정작 침수 위험도
  (`RiskEngine`)는 화면 표시용일 뿐 알림을 독립적으로 못 냅니다 — 실제 물이
  차오르는데 교통 정체가 아직 안 심하면 알림이 안 나가는 구조적 공백입니다.
- 두 위험을 하나의 점수(가중합)로 섞어서 계산하다 보니, "왜 이 점수가 나왔는지"를
  분해해서 설명하기 어렵고, 두 신호 중 하나가 이상해도 전체 점수가 덜 민감하게
  반응합니다.

---

## 2. 지금 구조 — 무엇이 이미 분리돼 있고, 무엇이 뒤섞여 있나

| 계층 | 상태 |
|---|---|
| 판정 계산 | 이미 두 갈래로 독립 계산 중(물세그멘테이션 vs 강우·교통·하천 융합). 다만 `RiskEngine` 점수식에 교통 가중치(6%)가 섞여 있고, 하천수위(명백한 침수 신호)가 교통 쪽 `SemanticAgent`에 잘못 들어가 있음 |
| 알림 | `_notify_decision()` 트리거가 교통·기상 판정(`decision.alert`) 하나뿐 — 침수 위험도는 관여 안 함 |
| 이벤트 | `event_sync._sync_flood()`가 block당 이벤트 **1개만** 생성, 교통 레벨을 그대로 씀 |
| 화면 | `app.js`의 `floodSection()`(순수 침수)과 `card()`(교통 판정) 함수는 이미 분리돼 있으나, **같은 `<div class="card">` 안에 이어붙여 그려짐** — `/api/risk` 응답 자체가 두 도메인 데이터를 병합해서 내려줌 |
| DB | `domain` 컬럼은 전부 `varchar(16)` 자유 문자열, CHECK 제약 없음 — 새 도메인 값 자체는 마이그레이션 불필요 |
| 권한/화이트리스트 | `core/roles.py`의 `Domain` enum을 거치는 곳은 자동 대응되지만, 10개 파일에 "flood"/"crowd"/"road" 리터럴이 직접 박혀 있어 수동 수정 필요 |

---

## 3. 설계 — 계산 엔진 (담당: 개발부, 파일 6개)

### 3-1. `FloodMetrics`/`FloodMetricsEngine` 필드 재배치

실제 계산 방식을 근거로 4개 필드를 구분했습니다.

| 필드 | 계산 방식 | 판단 |
|---|---|---|
| `vehicles_touching_water`, `vehicles_tire_in_water`, `max_vehicle_submersion` | 차량 바운딩박스와 물 마스크의 순수 기하 교차 | **침수의 직접 물증** → flood에 잔류 |
| `traffic_state` | 교통 트래커 산출값 그대로 대입 | **순수 교통 신호** → flood에서 제거 |
| `stopped_vehicles_near_water` | "정지(교통 행동)" + "물 근처(위치)" 결합 | 어느 한쪽 소유가 아님 → flood 데이터클래스에서 제거, 필요 시 orchestrator(runner.py)가 조합 |

`FloodMetricsEngine.update()` 시그니처에서 `traffic: TrafficMetrics` 파라미터를
제거합니다. S-23 오프라인 파이프라인(`standalone_pipeline.py`)은 지금도 이 필드들을
직접 씁니다 — **`StandaloneFloodMetrics(FloodMetrics)`** 서브클래스를 신설해
`traffic_state`/`stopped_vehicles_near_water`를 그쪽에만 남기고, 실시간 경로는 절대
이 서브클래스를 쓰지 않도록 격리합니다(생성자 클래스명 교체만으로 S-23 무변경 유지).

### 3-2. `RiskEngine.score()` 순수화

`_raw_components()`에서 `traffic` 키만 삭제하면, 기존 정규화 로직
(`wsum = sum(weights[k] for k in raw)`)이 **자동으로 나머지 6개 요소의 비율을
그대로 유지한 채 재정규화**합니다(area 0.28→0.298, low_point 0.24→0.255 등, 상대
비율 불변). 별도 가중치 재산정은 불필요합니다 — `configs/risk_config.yaml`에서
`traffic: 0.06` 줄만 제거합니다.

`RiskPredictor`가 traffic_state에 오염되던 문제도 이 변경의 부수 효과로 함께
해결됩니다(애초에 `_raw_components()`가 그 필드를 안 읽으므로).

### 3-3. `SemanticAgent` 순수 교통화 + 하천수위는 flood로 이전

- `SemanticAgent.infer()`에서 `river` 파라미터와 관련 코드를 전부 제거
- 하천수위 전용 신규 `FloodRiverAgent`(flood 패키지)를 만들어 `river_provider.py`도
  `flood/` 아래로 이전
- `RISK_CATALOG`(WIR_*)를 완전히 다른 네임스페이스 2개로 분리:
  - `TRAFFIC_RISK_CATALOG`(`TWR_NORMAL/RAIN_ONSET/RAIN_CONGESTION/SEVERE_CONGESTION/SEVERE_GRIDLOCK`) — 순수 교통
  - `FLOOD_RIVER_CATALOG`(`FR_NORMAL/RIVER_ADVISORY/RIVER_ALERT/RIVER_DANGER`) — 순수 하천
  - 두 카탈로그가 서로 다른 코드값을 쓰므로, 강한 강우+정지차량 다발+하천경계가
    동시에 관측되면 `TWR_SEVERE_GRIDLOCK`(교통)과 `FR_RIVER_ALERT`(침수)가
    **같은 틱에 독립적으로 둘 다** 산출될 수 있음 — 확정사항 #2를 코드 레벨에서 보장

### 3-4. 신규 알림 트리거 `_notify_flood_risk()`

```python
def _notify_flood_risk(notifier, flood_m, risk, block_name, min_grade=4) -> None:
    if risk.risk_grade < min_grade:
        return
    notifier.send(event_key=f"{block_name}:flood_risk:grade{risk.risk_grade}",
                   message={...}, channels=["sms"])
```

- `event_key`에 등급을 포함시켜(`grade{N}`), 등급이 오를 때는 즉시 신규 이벤트로
  취급해 지연 없이 발송하고, 같은 등급 유지 중에는 쿨다운으로 스팸을 막습니다.
- `min_grade=4`("높음")부터 알림 — `configs/risk_config.yaml`에
  `notify_min_grade: 4`로 노출해 조정 가능하게 합니다.
- ⚠️ **이 임계값은 부산 실환경에서 검증되지 않았습니다** — 확정 전 재난 담당부서
  협의가 필요합니다(기존 `risk_config.yaml` 자체의 경고와 동일한 성격).

### 3-5. S-23 오프라인 파이프라인(`AlertEngine`)은 이번 범위에서 제외 (권장)

`standalone_pipeline.py`는 완전 자립형(자체 YOLO로 교통상태 계산)이라 분리
비용이 크고, 오프라인 사후분석이라 SOLAPI 실시간 알림과 무관합니다. 3-1에서 만든
`StandaloneFloodMetrics`로 영향만 차단하고, 실제 분리는 별도 과제로 미룹니다.

### 3-6. 모듈 이전 — `traffic_weather/` → `traffic/`

```
src/tot_dashboard/
  flood/
    river_provider.py     ← traffic_weather/perception/river_provider.py 이동
    river_risk.py          신규 — FloodRiverAgent
    river_ontology.py       신규 — FLOOD_RIVER_CATALOG
  traffic/                신규 — traffic_weather에서 승격
    ontology.py            TRAFFIC_RISK_CATALOG
    semantic_agent.py       river 제거된 버전
    risk_decision.py        카탈로그 주입형으로 일반화
    perception/...          (river_provider 제외 전부 이동)
```

`traffic_weather/`는 마이그레이션 기간 중 얇은 재-export shim으로 남겨두고,
참조부(`run_poc.py`, 기존 테스트)가 전부 옮겨진 뒤 제거합니다.

---

## 4. 설계 — 인프라 (Domain enum·하드코딩 리터럴, 파일 10개)

`core/roles.py`에 `Domain.TRAFFIC="traffic"` 추가가 기준점입니다. 이 enum을
import해서 쓰는 파일들(`routes_admin.py`,`main.py`,`routes_events.py` 등 9개)은
**자동 대응**되지만, 아래는 enum을 거치지 않고 리터럴을 직접 박아둔 곳입니다.

| 파일 | 위치 | 수정 내용 | 위험도 |
|---|---|---|---|
| `core/roles.py` | L24-31 | `TRAFFIC` 멤버 추가, `DOMAIN_LABELS`에 항목 추가, `FLOOD` 라벨을 "침수"로 축소 | 기준 |
| **`core/model_probe.py`** | **L406, L439-441** | 화이트리스트에 traffic 추가 + `crowd가 아니면 무조건 flood로 fallback`하던 로직에 `elif domain=="traffic"` 분기 신설. **안 고치면 traffic 모델 시험탐지가 조용히 flood(물세그멘테이션) 로직으로 돌아가 사람이 오판** | ★★★ 최우선 |
| **`service/routes_cameras.py`** | **L40 `DOMAIN_ORDER`, L321 화이트리스트** | `TRAFFIC` 추가. L321 안 고치면 traffic ROI/캘리브레이션 저장 시도가 전부 400 에러 | ★★★ 최우선 |
| `service/routes_analytics.py` | L125 | `/models` 화면 도메인 루프에 `("traffic","교통")` 추가 — 안 하면 traffic 모델이 화면에 안 보임 | ★★ |
| `core/settings.py` | L31-38, 87-88, 110-114 | `KEY_MODEL_TRAFFIC`, `MODEL_KEYS`, `MODEL_NOTE_KEYS`, `DEFAULTS`에 traffic 항목 추가 | ★★ |
| `core/model_registry.py` | L39, 129-134, 345 | `DOMAIN_LABELS`에 "교통" 추가, `_DOMAIN_HINTS`에 traffic 키워드를 **crowd보다 먼저** 배치(겹치는 범용 YOLO 키워드 때문), 정렬순서 | ★★ |
| `core/cameras.py` | `ROI_SHAPES`, `to_block_dict()` L490/L502 | traffic ROI 도형 신규 정의(권장: `congestion_roi`+`speed_line`), `to_block_dict()`를 `for dom in (CROWD, TRAFFIC): ...` 루프로 일반화(향후 도메인 추가 시 이 함수 재수정 불필요) | ★ |
| `core/auth.py` | L146-157, 219-223 | `dom_items`에 traffic 메뉴 튜플 추가(하위 메뉴: 실시간 관제, 분석 결과 이력) | ★ |
| `core/guard.py` | L44, RULES 목록, L117 | `TRAFFIC` 별칭 추가, `/api/traffic/...`·`^/traffic(/|$)` 라우트 규칙 신설. **`/api/risk`·`/api/history`·`/api/stream/risk`는 도메인 무관 공유 엔드포인트로 확인됨(5-1절 참고) — domain=None으로 유지** | ★★ |
| `core/camera_bulk.py` | L48-53, 106-108, 292-296 | 엑셀 `COLUMNS`에 `use_traffic`/`cont_traffic` 2열 추가(3곳 모두 동일 패턴, `DOMAINS` 리스트 기반 순회 코드는 이미 동적이라 무변경) | ★ |
| `core/sop.py` | L77-82 | flood의 기존 SOP 2건("차량 진입 차단 검토" 등)은 **flood에 남김**(트리거 원인이 침수 이벤트이므로) — traffic 자체 SOP는 위험유형 코드 체계 확정 후 별도 추가 | 낮음 |

### 4-1. 확인해서 해소한 사항 — `/api/risk` 등은 이미 도메인 무관 공유 엔드포인트

직접 코드를 확인한 결과, `/api/risk`·`/api/history`·`/api/stream/risk`
(`service/main.py:907-938`)는 `store.all()`/`store.all_history()`를 그대로
반환하는 **범용 엔드포인트**이며, 애초부터 "flood 전용"이 아니라 침수·교통 데이터를
한 dict에 병합해 나르는 통로였습니다. 반면 `/api/flood-runs`·`/api/flood-runs/{id}`
(`service/main.py:1774-1813`)는 S-23 오프라인 파이프라인(순수 침수) 결과만
다뤄 flood 전용이 맞습니다.

→ `guard.py`에서 `/api/risk`류는 **domain=None(도메인 무관, MONITOR 권한만
확인)** 으로 유지하는 것이 실제 구조와 맞습니다. flood 전용으로 태그하면 교통
전용 권한을 가진 사용자가 자기 데이터를 못 보는 상황이 생깁니다.

---

## 5. 설계 — DB 시드·마이그레이션 (파일 2~3개)

| 항목 | 결론 |
|---|---|
| `hazard_types` 시드 | traffic 세분류 5행 신설(`traffic`,`traffic_rain_congestion`,`traffic_stalled_vehicle`,`traffic_queue_delay`,`traffic_impassable`). **`RISK_CATALOG`(TWR_*/FR_*)와는 별개 네임스페이스로 유지** — 판정상태 코드를 유형 코드 칸에 넣으려던 것이 기존 버그(`normalize_hazard_code`)의 원인이므로 재발 방지 |
| `level_thresholds` | **신규 행 추가하지 않음.** traffic 판정은 다변량 조합이라 단일 스칼라 임계값 테이블에 억지로 넣으면 화면 근거값이 왜곡됨. `kind` 컬럼도 불필요(road와 달리 traffic은 기존 4단계 위험등급 체계를 그대로 씀) |
| `traffic_observations` 테이블 | **신설 필요**(crowd_observations 패턴). 컬럼: `camera_id, camera_name, rain_mm, speed_drop, queue_len, stalled_count, risk_code, risk_score, severity, drivers, failed, source, observed_at` |
| 마이그레이션 | 신규 alembic 리비전 **1개**(traffic_observations 생성), `down_revision=d2f4a8c916e3`(현재 HEAD). hazard_types 시드는 코드 리스트 추가 + 기동시 `seed_builtin()`으로 기존 관례 그대로 따름(별도 시드 마이그레이션 불필요) |
| 과거 이벤트 백필 | **재분류하지 않음(권장)** — 과거 `domain="flood"` 이벤트가 실제로 교통 유래였는지 판별할 원본 코드(WIR_*)가 이미 `b3e5f1a72c04`에서 대분류로 뭉개져 DB에 보존되지 않음. 추정으로 재분류하면 오분류 위험. 화면에는 "2026-08-21 도메인 분리 이전 기록 포함" 안내만 추가 |

---

## 6. 설계 — 이벤트·알림·화면 (파일 7개)

### 6-1. 이벤트 이중화

`event_sync._sync_flood()`를 `_sync_traffic()`(교통, 기존 `decision.level` 사용)
+ `_sync_flood_water()`(침수, `flood_risk_grade`→등급 매핑 신규 필요)로 분리합니다.

⚠️ **확인 필요 #3**: `flood_risk_grade`(1~5, 매우낮음~매우높음)를 이벤트 등급
어휘(관심/주의/경계/심각)로 바꾸는 매핑표가 새로 필요합니다. 계산 담당 설계의
`min_grade=4`(알림 임계값)와 일치시켜 아래를 권장 기본값으로 둡니다.

| flood_risk_grade | 1(매우낮음) | 2(낮음) | 3(보통) | 4(높음) | 5(매우높음) |
|---|---|---|---|---|---|
| 이벤트 등급 | 관심 | 관심 | 주의 | 경계 | 심각 |

또한 이번 기회에 `detail` 딕셔너리 조회 키 불일치 버그(`"rain_mm"`≠실제 키
`"rain_mm_h"` 등, 지금까지 두 항목이 한 번도 채워진 적 없음)를 함께 고칩니다.

### 6-2. 알림 event_key 정리

`_notify_decision`(교통, `f"{block}:{decision.risk_code}"`)과 `_notify_flood_risk`
(침수, `f"{block}:flood_risk:grade{N}"`)는 패턴상 절대 겹치지 않음을 확인했습니다.
`AlertNotifier.send()`에 domain 파라미터 추가는 **비권장**(이미 event_key로 충분,
YAGNI). S-50 승인 대기 큐는 `DOMAIN_LABELS`만 갱신되면 코드 수정 없이 자동으로
배지가 나뉩니다.

### 6-3. 화면 분리

- `index.html`의 `#tab-flood`(교통+침수 병합)를 `#tab-flood`(침수 전용)+
  `#tab-traffic`(교통 전용) 2개 탭으로 분리
- `app.js`의 `card(b)`를 `trafficCard(b)`(기존 card, `floodSection()` 호출 제거)와
  `floodCard(b)`(기존 `floodSection()`을 독립 카드로 승격)로 분리, 각각
  `#traffic-grid`/`#flood-grid`에 렌더링
- `/api/risk` 서버 응답은 **지금처럼 병합 형태 유지, 클라이언트에서만 분리 렌더링**
  (서버 분리는 `_snapshot()`까지 번지는 별도 작업이라 이번 범위 초과로 판단)
- `styles.css`는 `#tab-flood`/`#tab-traffic` 셀렉터 스코프만 이동(클래스명 자체는
  이미 `.flood*`/일반 클래스로 구분돼 있어 재작성 불필요)

### 6-4. 지도 마커(S-01) — 후속 과제로 분리 권장

`board_map.py`가 블록당 최고 등급 이벤트 1건만 표시하는 구조라, 침수+교통이 동시에
열리면 마커에 하나만 뜨는 문제가 더 자주 생깁니다. **1차는 팝업에 도메인별 이벤트
리스트를 추가**하는 낮은 난이도 방안을 권장하고, 마커 자체의 다중 표시는 화면
분리가 안정된 뒤 후속 스프린트로 미룹니다.

### 6-5. 작업 순서

**이벤트 → 알림 → 화면** 순서를 권장합니다. 화면을 먼저 나누면 카드는 분리돼
보이는데 알림은 여전히 하나로 오는 혼란이 생기므로, 데이터 계층부터 정리합니다.

---

## 7. 전체 작업 순서 (종합)

| 순서 | 단계 | 파일 수 | 비고 |
|---|---|---|---|
| 1 | `core/roles.py`에 `Domain.TRAFFIC` 추가 | 1 | 모든 후속 작업의 기준점 |
| 2 | 계산 엔진 분리(3절: FloodMetrics/RiskEngine/SemanticAgent/FloodRiverAgent/traffic_weather→traffic 이전) | 6 | 회귀 테스트로 그린 확보 후 다음 단계 |
| 3 | DB 마이그레이션(traffic_observations) + hazard_types 시드 | 2~3 | |
| 4 | 카메라·권한 인프라(`routes_cameras.py`, `cameras.py`, `guard.py`, `auth.py`, `camera_bulk.py`) | 5 | traffic 카메라 등록이 가능해짐 |
| 5 | 모델 관리 인프라(`settings.py`, `model_registry.py`, `model_probe.py`, `routes_analytics.py`) | 4 | traffic 모델 등록·시험탐지 가능해짐(특히 model_probe.py 위험 fallback 제거) |
| 6 | 이벤트 이중화(`event_sync.py`) | 1 | |
| 7 | 알림 event_key 정리(`runner.py`) | 1 | |
| 8 | 화면 분리(`index.html`,`app.js`,`styles.css`,`board_map.py`,`home.html`) | 5 | |
| 9 | SOP/기타 마무리(`core/sop.py`) | 1 | |

각 단계 후 `pytest tests -q` 전체 통과를 확인하고, 특히 2단계 완료 후에는
`tests/flood/`, `tests/traffic_weather/`(→`tests/traffic/`) 전체를, 8단계 완료
후에는 실제 브라우저로 화면 분리를 확인합니다.

---

## 8. 테스트 영향

| 파일 | 영향 | 조치 |
|---|---|---|
| `tests/flood/test_alert_engine.py` | `FloodMetrics(traffic_state=...)` 생성자 kwarg | `StandaloneFloodMetrics`로 import 교체(기계적) |
| `tests/flood/test_flood_metrics_engine.py` | `engine.update(..., traffic=traffic, ...)` | `traffic=` kwarg 삭제 |
| `tests/flood/test_standalone_pipeline.py` | `StandaloneFloodMetrics` 생성 확인 | 서브클래스라 대부분 그대로 통과 |
| `tests/traffic_weather/test_semantic_and_decision.py` | `river=` kwarg 삭제 대상, `WIR_ROAD_IMPASSABLE`→`TWR_SEVERE_GRIDLOCK` 코드명 변경 | river 테스트는 신규 `tests/flood/test_river_risk.py`로 이전, 나머지는 코드명 치환 |
| 신규 `tests/flood/test_river_risk.py` | — | `FloodRiverAgent` 단독 검증 |
| 신규 `tests/service/test_notify_flood_risk.py` | — | 등급별 event_key/쿨다운/min_grade 게이팅 검증 |
| 신규 `tests/service/test_runner_dual_events.py` | — | 혼합 상황에서 flood/traffic 이벤트가 각각 독립 발생하는지 검증 |
| `tests/service/test_evidence*.py` 등 ~45곳 | `domain="flood"` 리터럴 fixture | 대부분 파라미터화 수준, 난이도 낮음 |

---

## 9. 확인이 필요한 사항 (권장 기본값과 함께)

| # | 항목 | 권장 기본값 | 확정 필요 사유 |
|---|---|---|---|
| 1 | traffic 모델이 속도 추정까지 하는지(=지면평면 캘리브레이션 필요 여부) | 1차는 차량 카운팅만(캘리브레이션 불필요), 속도 추정은 2차 과제 | 모델 스펙 미확정 |
| 2 | traffic 화면 메뉴 S-2X 화면번호 | 미확정 상태로 "실시간 관제"/"분석 결과 이력" 2개만 우선 배치 | UI 설계서(`docs/ui_design_spec.md`) 갱신 필요 |
| 3 | `flood_risk_grade`→이벤트 등급 매핑표 | 6-1절 표(1~2:관심, 3:주의, 4:경계, 5:심각) | 계산 담당자 최종 확인 |
| 4 | 과거 `domain="flood"` 이벤트 재분류 여부 | 재분류 안 함(5절) | 근거 데이터(WIR_* 원본코드) 미보존 확인됨 |
| 5 | `FR_*`(하천) 카탈로그 도입이 실제 새 탐지모델 추가를 의미하는지 | 아니오 — `hazard_types.flood_river.detectable`은 그대로 False 유지 | 계산 담당자 확인 후 필요시 변경 |
| 6 | traffic 모델 파일명 명명 규칙(`_DOMAIN_HINTS` 키워드 설계용) | 확정 전까지는 crowd와 안 겹치는 키워드(`vehicle`,`car`,`traffic`,`speed`,`congestion`) 임시 사용 | 실제 모델 파일 확보 후 재검토 |

---

## 10. 확인 사항

- **기존 폴더 `flood_t`는 이번 작업 어디에서도 건드리지 않았습니다** (git
  porcelain 39건, 작업 전후 동일).
- 6개 조사 에이전트 + 4개 설계 에이전트를 거쳐 계획을 세웠고, 일부(2개)는
  범위가 너무 넓어 시간초과로 중단됐다가 범위를 좁혀 재실행·완료했습니다.

---

## 11. 구현 결과 (2026-08-21)

계획 1~8단계를 **전부 구현했습니다.** 아래는 계획과 달라진 판단과, 작업 중
찾은 결함입니다.

### 11-1. 계획과 다르게 한 것 (3건)

| # | 계획 | 실제 | 이유 |
|---|---|---|---|
| 1 | `traffic_weather/` → `traffic/` 폴더 이름 변경 | **하지 않음** | 여러 파일의 import 경로를 한꺼번에 옮기는 위험 대비 실익이 낮습니다. **기능 분리(카탈로그·계산 로직 독립)는 계획대로 전부** 했고, 폴더명만 남겼습니다 |
| 2 | `/api/risk` 등 공유 API를 `domain=None`(도메인 무관)으로 | **`(FLOOD, TRAFFIC)` 둘 중 하나로** | `None` 으로 열면 **인파만 담당하는 사용자에게까지 열려 권한이 느슨해집니다.** `roles.can()` 이 여러 도메인을 받도록 확장해, 실제 구조(두 도메인 공유)와 권한을 모두 지켰습니다 |
| 3 | 엑셀 열·기본값·예시행 3곳에 traffic 열 **수기 추가** | **`DOMAINS` 에서 생성** | 3곳에 같은 순서를 따로 적어 두는 구조라, 한 곳만 빠뜨리면 예시 행 열 수가 어긋나 **엉뚱한 칸에 값이 들어갑니다**(조용히 깨지는 종류). 생성으로 바꿔 원인을 없앴습니다 |

### 11-2. 작업 중 찾은 것

**① `_flood_summary()` 가 처음부터 교통 값을 읽고 있었습니다.**
홈 화면의 「침수·교통위험」 타일은 이름과 달리 `level`(교통·기상 판정 등급)만
읽었습니다. 즉 **침수 위험도는 홈 화면에 한 번도 나온 적이 없었습니다.**
`_traffic_summary()` 로 이름을 사실에 맞추고, 물 세그멘테이션을 읽는
`_flood_summary()` 를 새로 만들었습니다.

> 실측 확인: 분리 후 홈 화면이 **「교통위험 6건·최고 주의」** 와
> **「침수 정상·관측 4개소」** 를 각각 표시합니다 — 예전에는 이 상황이
> 「침수·교통위험 6건」 하나로만 보였습니다.

**② `/traffic` 경로가 없으면 교통 담당자가 상황판에 못 들어옵니다.**
`/flood` 는 가드가 침수 담당만 통과시킵니다. 경로를 추가했습니다.

**③ 이벤트 `detail` 이 한 번도 채워진 적이 없었습니다**(계획 6-1절에서 예고).
조회 키(`rain_mm`/`water_ratio`/`vehicles`)가 실제 스냅샷 키
(`rain_mm_h`/`water_area_ratio`/`n_vehicles`)와 달랐습니다. 함께 고쳤습니다.

### 11-3. 지어내지 않은 것

- **교통 SOP 단계** — 우회 안내·통제 요청 절차는 지자체 재난 대응 운영규정에
  달려 있어 개발사가 정할 수 없습니다. 도메인 무관 단계(통보·상황 종료 확인)가
  그대로 적용되며, 이는 빈틈이 아니라 「확정 전까지 기본 절차를 따른다」는
  뜻입니다. 코드에 그 판단을 적어 뒀습니다.
- **교통 모델 성능 수치** — 전용 모델을 아직 검증한 적이 없어 비고를
  「미검증 — 전용 모델 선정 전」로 두었습니다.
- **속도(km/h) 추정** — 지면 평면 보정 없이 픽셀 속도를 실제 속도처럼 보여
  주지 않습니다. 1차 시험 탐지는 차량 **대수**만 셉니다.

### 11-4. 시험

| 항목 | 결과 |
|---|---|
| 신규 | `test_river_risk.py`(6) · `test_notify_flood_risk.py`(4) · `test_domain_split_wiring.py`(27) · `test_event_sync_domain_split.py`(10) · `test_dashboard_domain_tabs.py`(6) · `test_level_admin.py` 추가분(20 — 침수 6 + 교통·인파·노면 14) |
| 마이그레이션 | `e4a91c2f7b38` — **업·다운·업 확인** |
| 화면 | 실제 브라우저로 확인 — 탭 분리·CSS 스코프 적용(`display:grid` 2열)·카드 수(교통 13/침수 4)·S-95 알림기준 4개 폼 렌더링·저장 확인·콘솔 오류 0 |
| **전체 회귀(최종)** | **1,475건 통과 · 3 skip · 실패 0** (2026-08-26 기준) |

`test_domain_split_wiring.py` 는 **enum 을 순회**하므로, 앞으로 도메인이 늘면
시험도 자동으로 늘어 「어디를 함께 고쳐야 하는지」를 알려 줍니다.

### 11-5. 확인이 필요한 채로 남은 것

9절의 확인사항 중 **3번(등급 대응표)** 은 권장 기본값
`{1~2:관심, 3:주의, 4:경계, 5:심각}` 으로 구현했습니다 — **재난 담당부서
확인이 필요합니다.** (사용자 확인: 이 대응표는 그대로 두기로 함, 2026-08-21)

**알림 발동 등급은 코드에 고정하지 않고 관리자가 화면에서 조정할 수 있게
만들었습니다** — S-95 위험등급 관리(`/settings/levels`)에 「침수 위험도
알림 발동 등급」 절을 신설했습니다.

| 항목 | 내용 |
|---|---|
| 저장 위치 | `AppSetting`(`flood.notify_min_grade`), 기존 설정 캐시 재사용 |
| 코드 | `core/settings.py`의 `flood_notify_min_grade()`/`set_flood_notify_min_grade()` (1~5 범위 밖 값은 가장 가까운 값으로 눕힘) |
| 화면 | `/settings/levels` → 새 절, `R.SETTINGS_OPS` EDIT 권한(관제요원은 조회만) |
| 반영 | `runner.py`가 매 틱 `ug_settings.flood_notify_min_grade()`를 읽어 `_notify_flood_risk()`에 전달 — 재기동 없이 다음 판정부터 반영 |
| 검증 | 신규 시험 6건(`test_level_admin.py`) + 실제 브라우저로 2등급 저장 → DB 반영 확인 |

나머지(9절 1·2·5·6번)는 열려 있습니다.

### 11-6. 교통·인파·노면도 같은 방식으로 관리자가 조정 (2026-08-26)

사용자 요청으로 **교통·인파·노면에도 같은 종류의 조정 기능**을 추가했습니다.
다만 세 도메인의 「경보 발동」 방식이 서로 달라, 각 도메인의 **실제 게이트를
그대로** 노출했습니다(새로 지어내지 않음).

| 도메인 | 기존 자동 알림 | 조정한 것 | 기본값(=기존 동작) |
|---|---|---|---|
| 침수 | 있음(`_notify_flood_risk`, 11-절 기존) | 알림 발동 등급(1~5) | 4(높음) |
| 교통 | 있음(`RiskDecisionAgent`) | **SOLAPI 알림** 발동 심각도(0~3) | 2(경계) |
| 인파 | **없음**(승인 큐 수동 발송뿐) | **이벤트 생성** 심각도(0~4) | 3(군중급증위험) |
| 노면 | **없음** | **이벤트 생성** 등급(1~4) | 1(양호) |

⚠️★★ **작업 중 실측으로 찾은 것** — `core/events.py`의
`record_detection()`이 **도메인 공통으로 「주의」 미만 이벤트를 만들지
않습니다**(`EVENT_THRESHOLD="주의"`). 직접 호출해 확인했습니다:
`level="관심"` → `None` 반환(이벤트 안 만듦), `level="주의"` → 이벤트 생성.

이 때문에 **인파의 「정상」·「군중밀집」, 노면의 「양호」·「관찰」을 골라도
실제로는 아무것도 바뀌지 않습니다** — 화면에 그 사실을 안내문으로 넣었고,
`core/settings.py`의 각 getter 함수 docstring에도 남겼습니다. 이 설정이
실제로 여닫는 범위는 인파는 「이동흐름혼란」부터, 노면은 「보수 필요」부터입니다.

교통은 이 문제가 없습니다 — SOLAPI 알림은 `record_detection()`을 거치지
않는 별개 경로(`_notify_decision`)라서, 「관심」·「주의」로 낮춰도 실제로
알림이 나갑니다.

시험 14건 추가(`test_level_admin.py`), 실제 브라우저로 4개 폼 모두 기본값
표시·저장 확인.
