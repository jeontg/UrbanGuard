# 3개 프로젝트 통합 Implementation Plan — v2 (코드 재검증 후 수정판)

> ## ✅ 이 계획은 완료됐습니다 — 이력 문서입니다 (2026-08-08 확인)
>
> 세 프로젝트(underpath_flood_dashboard / flood3 / SAM)의 통합은 **끝났습니다.**
> 통합 결과물이 현재의 `tot_dashboard` 패키지이며, 그 위에 UrbanGuard 제품
> 계층이 올라가 있습니다.
>
> **이 문서는 「왜 이렇게 합쳤는가」를 남기기 위해 보존합니다.** 현재 구조를
> 알고 싶다면 아래를 보십시오.
>
> | 알고 싶은 것 | 문서 |
> |---|---|
> | 현재 시스템 구조 | `docs/202608131503/system_architecture.md` |
> | 화면 구성 | `docs/ui_design_spec.md` |
> | 데이터 구조 | `docs/202608131503/db_design_spec.md` |
> | 진행 상태 | `docs/202608131503/development_status.md` |
>
> **⚠️ 본문의 경로는 통합 전 원본 위치(`C:\Users\cwro7\Dropbox\work26\…`)입니다.**
> 현재 코드는 `D:\dev-PoC\UrbanGuard` 에 있습니다.

이 문서는 기존 계획(`integration_plan.md` v1)을 세 프로젝트의 **실제 소스 코드를 다시 읽고 대조**하여 검증한 뒤 수정한 최종판입니다. v1은 병렬 탐색 에이전트의 구조 조사 요약에 크게 의존했고, "중복 코드"로 분류한 여러 항목이 실제로는 겉모습만 비슷하고 내부 인터페이스가 다르다는 것이 이번 재검증에서 드러났습니다. 아래 내용은 실제 함수 시그니처, import 관계, 파일 해시 비교, git 상태 확인 등 **직접 확인한 증거**를 근거로 합니다.

대상 프로젝트
- **A. underpath_flood_dashboard** — `C:\Users\cwro7\Dropbox\work26\Sun_YOLO\underpath_flood_dashboard`
- **B. flood3** — `C:\Users\cwro7\Dropbox\work26\flood3`
- **C. SAM** — `C:\Users\cwro7\Dropbox\work26\SAM`
- **기존 계획**: `C:\Users\cwro7\Dropbox\work26\tot_dashboard\docs\integration_plan.md` (v1)

---

## 1. 검토 범위와 확인한 프로젝트 구조

v1 작성 이후 다음을 직접 열어 재확인했습니다(파일 경로/함수명 기준).

- **A**: `src/roi_utils.py`, `src/risk_engine.py`, `src/metrics.py`, `src/water_segmentation.py`, `src/object_detection.py`, `src/alert_engine.py`, `src/config_utils.py`, `src/video_processor.py`, `src/archive_manager.py`, `src/model_loader.py`, `app.py`(일부), `requirements.txt`
- **B**: `flood/roi_utils.py`, `flood/risk_engine.py`, `flood/flood_metrics.py`, `flood/water_segmentation.py`, `perception/detection_source.py`, `perception/traffic_tracker.py`, `models.py`, `service/runner.py`, `scripts/smoke_test_flood.py`, `configs/blocks.json`, `configs/roi/*.json`, `.env.example`, `orchestrator/notifier.py`, `requirements.txt`
- **C**: `app/notifier.py`, `SAM3_install.py`(경로 처리부), `notification.env.example`, `requirements.txt`, `.git` 상태
- **교차 검증**: `sha256sum`으로 A/B 모델 파일 동일성 확인, `git status`로 SAM 루트 `.git` 상태 확인, `grep`으로 K-water zip 미사용 여부 확인, `Pillow` 실제 import 여부 확인

이 재조사는 구조 탐색이 아니라 **v1의 구체적 주장 하나하나를 코드로 대조하는 검증** 작업이었습니다. 아래 2장부터는 이 검증 결과를 근거로 합니다.

---

## 2. 기존 implementation plan 평가표

| v1 항목 | 판정 | 근거 |
|---|---|---|
| A/B/C 목적·주요 기능 요약 | **VALID** | 재확인 결과 A(Streamlit 침수 프로토타입)/B(날씨×교통 융합+침수 이식)/C(SAM3 뷰어+GPU 파이프라인) 설명은 정확 |
| flood3이 A+C를 부분 이식한 "중간 진화형"이라는 핵심 전제 | **VALID, 근거 강화됨** | `flood/water_segmentation.py`·`flood/risk_engine.py`·`flood/roi_utils.py` 모두 docstring에 "Sun_YOLO ... 이식" 명시 확인. 추가로 `perception/traffic_tracker.py`의 `TrafficBehaviorTracker`도 "SAM `CrowdBehaviorTracker`를 교통용으로 재타게팅"이라고 docstring에 명시 — **v1이 놓친 세 번째 이식 경로**(SAM→flood3 트래커 패턴) 확인 |
| 모델 파일 중복(`best.pt`≡`water_seg_best.pt`, `yolo11s.pt` 동일) | **VALID (해시로 확정), 단 "중복 제거" 방침은 사용자 지시로 철회** | `sha256sum` 결과 두 쌍 모두 완전히 동일한 해시. 다만 사용자 확인 결과 `best.pt`(A, 직접 학습)가 정본이며 `water_seg_best.pt`(B)는 그 사본이 이미 flood3에 이식되어 라이브 서비스에 쓰이고 있는 것 — **원본 A/B의 기존 파일은 어느 쪽도 삭제하지 않고 그대로 둔다.** 통합 프로젝트에는 `best.pt`라는 이름으로 새 사본 1개만 추가한다(중복 제거가 아니라 "원본 보존 + 통합 저장소용 신규 사본" 방식으로 수정) |
| `roi_utils.py`를 "B가 상위호환, 그대로 병합" | **NEEDS REVISION** | B는 단순 상위호환이 아니라 **API가 변경**됨: `point_in_polygon(point, polygon)` → `point_in_polygons(point, polygons)`로 함수명·파라미터 의미(단일 폴리곤→폴리곤 리스트) 모두 변경. A의 `metrics.py`가 `roi_utils.point_in_polygon(...)`을 직접 호출하므로, 이 한 곳은 반드시 코드 수정이 필요(단순 파일 교체 불가) |
| `risk_engine.py`를 "거의 동일, B가 상위호환" | **VALID (세부 확인)** | `RiskResult`/`PredictionResult`/`RiskEngine`/`RiskPredictor`/`write_back`이 라인 단위로 거의 동일. 차이는 두 곳: ①생성자에서 B는 `alert_config` 없으면 `risk_config`로 폴백(A는 없으면 빈 dict) ②`_raw_components`에서 traffic_state 비교값이 다름(A: 문자열 `"stopped"/"jammed"`, B: `TrafficState.blocked/congested` enum). 이 두 번째 차이는 입력 데이터클래스(`FrameMetrics` vs `FloodMetrics`)가 다르기 때문이며, 병합 시 반드시 어느 traffic_state 어휘를 표준으로 할지 먼저 결정해야 함 |
| `metrics.py`/`flood_metrics.py`를 "거의 동일, B가 개선된 버전, 병합 가능" | **INCORRECT** | 가장 중요한 오류. B의 `FloodMetricsEngine.update()`는 `(water, vehicles: list[VehicleObject], person_points, traffic: TrafficMetrics, ...)`를 인자로 받아 **이미 계산된 결과**를 받기만 함. A의 `MetricsEngine.update()`는 `(water, detection: DetectionResult, ...)`를 받아 **자체적으로** 차량 속도(`_vehicle_speeds`)·정체 상태(`_traffic_state`)·최근 카운트(`_recent_counts`)를 계산함 — 이 세 메서드와 `avg_vehicle_pixel_speed`/`recent_person_count`/`recent_vehicle_count` 필드가 B에는 아예 없음. 즉 B는 A의 "부분집합"이 아니라, 객체탐지·추적 책임을 완전히 별도 컴포넌트(B의 `Perception`/`TrafficBehaviorTracker`)로 떼어낸 **다른 아키텍처**임. "A 베이스+B 기능 병합"은 그대로 실행 불가능 |
| `object_detection.py`↔`perception/detection_source.py`를 "common/detection.py로 통합" | **INCORRECT** | A의 `Detection`은 dataclass(`class_id/class_name/category/confidence/box/track_id`, ultralytics `.track()` 내장 추적 사용), B의 `Detection`은 Pydantic 모델(`bbox/conf/cls/track_id`, `models.py`에 정의)로 **필드명·타입·클래스 자체가 다름**. 더구나 B의 `YoloDetectionSource`는 `_COCO_VEHICLE_IDS = {2:"car",3:"motorcycle",5:"bus",7:"truck"}` 식으로 **클래스 ID를 하드코딩**하고 있어 A의 `model_loader.select_class_ids()`(이름 기반 매핑) 설계원칙을 따르지 않음. 하나의 `common/detection.py`로 단순 통합은 불가능하며, 오히려 B의 하드코딩을 A 패턴으로 고치는 별도 개선 작업이 필요 |
| Notifier: "C(SAM)의 실제 구현을 정본으로, B의 DryRunNotifier 대체" | **VALID, 강하게 확인됨** | B의 `orchestrator/notifier.py` 자체 docstring이 "Phase 2에서 SAM AlertNotifier(SOLAPI SMS/Webhook)를 이식한다"고 이미 명시 — v1의 방향이 flood3 저자의 원래 계획과 일치. 다만 세부 사항 1건 누락(아래 참조) |
| ".env.example 통합, SOLAPI 변수는 B/C 동일해 충돌 없음" | **INCORRECT** | 발신번호 변수명이 다름: B의 `.env.example`은 `SMS_FROM`(어떤 코드도 읽지 않는 미사용 플레이스홀더), C의 `notification.env.example`/`app/notifier.py`는 `SOLAPI_SENDER`(실제로 코드가 읽음). 통합 시 `SOLAPI_SENDER`로 표준화하고 `SMS_FROM`은 폐기해야 함. 또한 B에는 카카오 알림톡 변수(`SOLAPI_KAKAO_PF_ID`/`SOLAPI_KAKAO_TEMPLATE_ID`)가 아예 없어 신규 추가 필요 |
| `config_utils.py`(A) + `_load_yaml` 중복(B) → 공용화 | **VALID (중복 확정)** | `service/runner.py`와 `scripts/smoke_test_flood.py`의 `_load_yaml` 함수가 **글자 단위로 완전히 동일**함을 확인. 다만 이 함수는 A의 `config_utils.py`에 없는 "weights 딕셔너리만 별도 병합" 특수 로직을 갖고 있어, 공용 모듈은 A의 단순 병합과 B의 중첩 dict 병합을 모두 지원하도록 확장해야 함(단순 포팅 불가, 소폭 일반화 필요) |
| `sam3.pt` 경로 불일치 — v1은 "UNCLEAR" | **INCORRECT → 확정 필요 (지금은 CONFIRMED)** | 실제 파일은 `SAM/sam3.pt`(루트)에 있고, `SAM3_install.py`의 `SAM3_ROOT = os.environ.get("SAM3_ROOT", os.path.join(HERE, "SAM3"))` 기본값은 `SAM/SAM3`이므로 기대 경로는 `SAM/SAM3/sam3.pt`. `assert os.path.exists(CKPT_FILE)`이 기본 설정으로는 실패함. UNCLEAR가 아니라 **확정된 설정 불일치**로 재분류 |
| SAM 루트의 `.git` 폴더 상태 — v1은 "UNCLEAR" | **확정 (CONFIRMED, 별도 판정 불필요)** | `.git` 폴더는 물리적으로 존재하나 `git status`가 "not a git repository" 반환 — 손상되었거나 불완전한 `.git` 디렉터리로 확정. 정상 git 이력이 없다고 보고 진행하면 됨 |
| A `requirements.txt`에 `Pillow` 누락 — v1은 "UNCLEAR" | **VALID (확정)** | `app.py:497`에서 `from PIL import Image` 직접 사용 확인, `requirements.txt`에는 없음. 지금까지는 streamlit/ultralytics의 전이 의존성으로 우연히 동작 중. 통합 시 명시 필요 |
| K-water 데이터셋 zip(302MB) 미사용 | **VALID (확정)** | flood3 전체 `.py` 파일에서 파일명/유사 키워드 검색 결과 0건 |
| Python 3.13 일관성, SAM3 GPU 파이프라인만 3.12+/CUDA 요구 | **VALID** | 재확인 결과 그대로 |
| Streamlit(A) → FastAPI(B/C) 통일 권장 | **VALID (방향 유지)**, 단 **작업량 재평가 필요** | `app2.py`가 `st.session_state`를 9곳 이상에서 직접 사용하는 등 Streamlit 상태관리에 강하게 결합되어 있음을 확인 — "재구현"이 아니라 사실상 "재설계+재작성" 수준의 작업임을 명확히 해야 함(리스크 항목에서 별도 강조) |
| `archive_manager.py`(A) + `catalog.py`(C) → `case_archive.py`로 통합 | **NEEDS REVISION** | A의 `RunWriter`는 시계열 프레임 단위 CSV(`METRICS_COLUMNS`/`ALERT_COLUMNS` 하드코딩된 컬럼 리스트, `FrameMetrics` 전용) 기록기이고, C의 `CaseCatalog`는 사전 계산된 결과물을 가리키는 정적 JSON 매니페스트 판독기 — **데이터 모델과 책임이 근본적으로 다름**. "하나로 통합"이 아니라 "하나의 패키지 아래 공존하는 두 개의 별도 아카이브 방식"으로 재정의 필요 |
| `video_processor.Pipeline`을 flood3 서비스 레이어에 흡수 | **NEEDS REVISION** | A의 `Pipeline`은 `object_detection.py`+`metrics.py`(자체 탐지/추적)를 전제로 설계되어, B의 `PipelineRunner._block_loop`(별도 `Perception`/`TrafficBehaviorTracker` 사용)와 데이터 흐름이 호환되지 않음. `Pipeline`은 흡수 대상이 아니라 **독립 실행형 오프라인 배치 분석 도구**(단일 카메라, CLI)로 존속시켜야 함 |
| 단계별 계획의 각 Phase 대상 파일 서술 | **NEEDS REVISION** | 전반적으로 방향은 맞으나 Phase 2(Flood 도메인 병합)가 "메트릭 병합 가능"이라는 잘못된 전제 위에 서술되어 있어 재작성 필요(5장에서 재작성) |
| 테스트/검증 계획 | **NEEDS REVISION** | 회귀 비교 원칙은 타당하나 "동일 입력→동일 출력"이 성립하려면 어떤 아키텍처(A식 self-contained vs B식 외부공급)를 검증 대상으로 할지 먼저 명시해야 함(현재는 모호) |
| 롤백 전략 | **MISSING** | v1에 별도 장이 없었음(6장 "단계별 이전"에 "원본 삭제 안 함"이라는 한 문장만 존재). 이번 문서 7장에서 신설 |
| 각 Phase의 선행조건/완료조건 명시 | **MISSING** | v1은 "작업 내용"만 있고 "이 전 단계가 끝났다는 증거"·"이번 단계가 끝났다는 증거"가 구분되어 있지 않음. 5장에서 전 Phase에 추가 |
| GPU 크라우드 파이프라인 검증 방법 | **UNVERIFIED (그대로 유지)** | GPU 환경이 없어 이번 재조사에서도 실행 검증 불가. `SAM3/results/`가 비어있다는 사실도 재확인(로컬 실행 이력 없음 추정 유지) |

---

## 3. 잘못된 내용과 누락된 내용 (상세 설명)

### 3-1. 가장 중요한 오류: "flood 메트릭 엔진은 병합 가능한 중복"이 아니다

v1은 `metrics.py`(A)와 `flood_metrics.py`(B)를 "B가 상위호환인 근중복"으로 분류하고 Phase 2에서 "B를 베이스로 A 기능을 병합"하라고 지시했습니다. 실제로는:

- A의 `MetricsEngine.update(water, detection, frame_number, timestamp_sec)`은 **탐지·추적·속도계산을 전부 스스로 수행**합니다(`_vehicle_speeds`, `_traffic_state`, `_recent_counts` 내부 메서드 보유).
- B의 `FloodMetricsEngine.update(water, vehicles, person_points, traffic, frame_number, timestamp_sec)`은 **이미 계산된 값을 받기만** 합니다. 차량 속도·정체 판정은 B의 `perception.traffic_tracker.TrafficBehaviorTracker`가 별도로 수행하고, 그 결과(`VehicleObject`, `TrafficMetrics`)를 파이프라인 상위 단계에서 넘겨받습니다.

이건 "기능이 더 많다/적다"의 문제가 아니라 **책임 분리 지점이 다른 별개의 아키텍처**입니다. 하나의 함수 시그니처로 억지로 합치면 둘 중 하나의 실행 모드가 깨집니다. 5장의 수정 계획에서는 이 둘을 "병합"하지 않고 **공유 가능한 부분(ROI 교차 계산, tire-zone 분석, expansion rate 계산 — 이 부분은 실제로 알고리즘이 100% 동일함)만 별도 헬퍼로 뽑아 재사용**하고, `update()` 진입점 자체는 두 가지 모드로 유지하는 방향으로 수정했습니다.

### 3-2. 두 번째 오류: object_detection 통합 계획의 실행 불가능성

`common/detection.py`로 통합한다는 v1의 계획은 실제로 실행하면 A와 B 양쪽에서 타입 에러가 발생합니다(dataclass vs Pydantic model, 필드명 불일치). 게다가 B의 `YoloDetectionSource`가 COCO 클래스 ID를 하드코딩하고 있다는 사실은 v1의 "8. 재사용 가능한 코드" 섹션이 칭찬했던 "이름 기반 클래스 매핑" 설계 원칙(A의 `model_loader.py`)을 B가 실제로는 따르지 않고 있다는 뜻이며, 이는 병합 계획이 아니라 **B 코드 개선 항목**으로 별도 취급해야 합니다.

### 3-3. 누락: SOLAPI 발신번호 변수명 불일치

v1은 ".env 통합 시 SOLAPI 변수 충돌 없음"이라 단정했으나, `SMS_FROM`(B, 미사용) vs `SOLAPI_SENDER`(C, 실사용)로 실제로는 다릅니다. 사소해 보이지만 통합 `.env.example`을 그대로 베끼면 실제 알림 발송이 조용히 실패(빈 발신번호)하는 원인이 될 수 있어 명시적으로 고쳐야 합니다.

### 3-4. 누락: alert_engine.py 이식 시 traffic_state 어휘 재작성이 필요하다는 점

v1 Phase 2는 "A의 alert_engine.py를 그대로 이식"이라고만 되어 있었습니다. 실제로 `alert_engine.py`는 `m.traffic_state in ("stopped", "jammed")`처럼 **A의 6가지 문자열 상태**(`empty/unknown/normal/slow/stopped/jammed`)를 직접 비교합니다. B의 `FloodMetrics.traffic_state`는 B의 `models.TrafficState` 4값 enum(`free/slow/congested/blocked`)입니다. "그대로 이식"은 불가능하며, `_evaluate_rules`/`_reason_for_confirmed`의 traffic_state 비교 로직(약 3곳)을 B의 enum에 맞춰 재작성해야 함을 명시해야 합니다.

### 3-5. 누락: archive_manager와 CaseCatalog는 통합이 아니라 공존

RunWriter(시계열 CSV, `FrameMetrics` 전용 컬럼 하드코딩)와 CaseCatalog(정적 매니페스트 JSON 판독)는 입출력 형태 자체가 다릅니다. "하나의 case_archive.py로 통합"이라는 표현은 오해를 부르므로, "하나의 패키지 아래 두 개의 별도 클래스로 공존"이라고 재정의했습니다.

### 3-6. 누락: sam3.pt 경로 문제와 SAM `.git` 상태는 이제 "확정된 사실"

v1에서 UNCLEAR로 남겨뒀던 두 항목을 이번에 직접 확인해 확정했습니다(2장 표 참조). 계획 실행 전 조사 항목이 아니라 **Phase 진행 중 바로 수정해야 할 확정 이슈**로 격상되었습니다.

### 3-7. 여전히 누락: Phase별 선행조건/완료조건, 롤백 전략

v1에는 "각 단계의 선행 조건", "완료 조건", "롤백 방법"이 명시적으로 분리되어 있지 않았습니다. 5~7장에서 전면 보강했습니다.

---

## 4. 주요 아키텍처 결정 사항 (재검증 결과 반영)

1. **Flood 메트릭은 "병합"하지 않고 "두 개의 진입점 + 공유 헬퍼"로 설계한다.** `flood_metrics_core.py`(공유: ROI 교차/면적 계산, tire-zone 분석, expansion rate — 알고리즘이 완전히 동일함을 확인)를 뽑아내고, 그 위에 (a) A식 자체완결형 `StandaloneFloodPipeline`(단일 카메라, 자체 탐지/추적)과 (b) B식 `FloodMetricsEngine`(외부에서 vehicles/traffic 주입받음, 다중 블록 서버용)을 **별도 클래스로 유지**한다.
2. **object_detection은 공용화하지 않는다.** A의 `object_detection.py`+`model_loader.py`는 "독립 실행 CLI/오프라인 분석" 경로 전용으로 남긴다. B의 `perception/detection_source.py`는 그대로 두되, `YoloDetectionSource`의 하드코딩된 COCO ID를 `model_loader.select_class_ids()` 방식으로 고치는 것을 **별도의 작은 개선 작업**(통합과 무관하게 이득이 있는 리팩터)으로 Phase 3에 포함한다.
3. **roi_utils.py는 B(다중 폴리곤) 버전을 정본으로 채택**하되, `point_in_polygon`→`point_in_polygons` 리네임에 따른 호출부 수정(A의 `metrics.py` 대응 로직 이전 시 반영)을 Phase 2 작업 항목에 명시적으로 포함한다.
4. **risk_engine.py는 그대로 통합**하되, 입력 타입을 어느 쪽 FloodMetrics/FrameMetrics로 할지, traffic_state를 B의 enum으로 표준화할지를 먼저 결정한다 → **B의 `TrafficState` enum을 표준으로 채택**(A의 alert_engine을 이 enum에 맞게 재작성하는 쪽이, 반대보다 변경량이 훨씬 적음: alert_engine 쪽 비교 로직 3곳만 고치면 됨).
5. **Notifier는 C(SAM)의 `AlertNotifier`를 그대로 승격**하되 `.env.example`에서 `SOLAPI_SENDER`로 변수명을 통일하고 카카오 변수를 추가한다.
6. **archive_manager(RunWriter)와 catalog.py(CaseCatalog)는 통합하지 않고 공존**시킨다 — `common/case_archive/run_writer.py`(시계열)와 `common/case_archive/catalog.py`(매니페스트)로 하위 모듈만 분리해서 배치.
7. **UI는 FastAPI로 통일**하되, Streamlit 전환은 "이식"이 아니라 "재설계"임을 계획에 명시하고 우선순위를 낮춘다(선택적 Phase로 분리, 필요 시 후순위).
8. **sam3.pt 경로 문제는 Phase 착수 전에 먼저 고친다**(코드 이동과 무관하게 지금 SAM 프로젝트에서도 깨져 있는 버그이므로).
9. **크라우드(SAM3) 도메인은 별도 실행환경(venv/컨테이너)으로 분리**하고 기본 설치에는 포함하지 않는다(Python 3.12+/CUDA 요구사항 분기, GPU 미보유 환경에서 검증 불가능하다는 점 반영).

---

## 5. 수정된 최종 Implementation Plan

### 5-1. 세 프로젝트의 정확한 역할과 경계

| 프로젝트 | 역할 | 통합 후 경계 |
|---|---|---|
| A (underpath_flood_dashboard) | 단일 카메라 침수 분석의 **원조 알고리즘 저장소** + 오프라인 배치/단일 프레임 분석 UI | `flood` 도메인의 알고리즘 정본 소스(물세그멘테이션 신뢰도 튜닝값, alert_engine의 5단계 규칙, 아카이브 CSV 포맷)로 남고, UI(Streamlit)는 참고용으로만 보존, 실행 경로는 CLI/배치 전용으로 재편 |
| B (flood3) | 다중 카메라 실시간 서버, 날씨×교통 시맨틱 융합, flood 도메인의 **실서비스 진입점** | 통합 프로젝트의 서비스 레이어(FastAPI)와 다중 블록 실행모델의 뼈대, flood 도메인의 "라이브 실행" 경로 정본 |
| C (SAM) | 크라우드 세이프티(SAM3) + 케이스 아카이브 뷰어 + **실제 동작하는 SOLAPI 알림 구현** | Notifier(공용 승격), 케이스 매니페스트/뷰어 패턴(공용화), SAM3 파이프라인은 독립 선택 모듈(GPU 필요)로 격리 |

### 5-2. 통합 후 유지해야 할 기능 목록 (기능 보존 체크리스트)

- [ ] A: 단일 프레임 분석, ROI 다각형 편집(형식 유지), 5단계 알림(정상~통제권고, persistence/cooldown 스무딩), 0~100 연속 위험도 점수 + 5/10초 예측, 실행결과 아카이브(CSV+영상+스냅샷)
- [ ] B: 4단계(관심/주의/경계/심각) 위험도 산정, 강수/하천/CCTV 외부 API 연동(KMA/HRFCO/ITS/BUSAN), VLM(Gemini) 상황 해석, 다중 블록 동시 실행(스레드), HLS 라이브 스트림 처리, 손상 프레임 감지
- [ ] C: SOLAPI SMS/카카오 알림톡 실제 발송(쿨다운/중복억제 포함), 케이스 매니페스트 기반 결과 뷰어, SAM3 크라우드 5단계 파이프라인(GPU 필요, 선택 모듈)

### 5-3. 권장 통합 아키텍처 (수정)

```
tot_dashboard/
├── pyproject.toml                     # extras: [crowd-gpu], [legacy-streamlit]
├── .env.example                       # SOLAPI_SENDER 표준화 + 카카오 변수 추가
├── src/tot_dashboard/
│   ├── models.py                      # B의 models.py 확장(정본)
│   ├── common/
│   │   ├── config.py                  # A의 config_utils + B의 _load_yaml 패턴(중첩 weights 병합 지원 확장)
│   │   ├── video_io.py                # A의 video_processor 범용부 + C의 transcode_h264
│   │   ├── roi.py                     # B의 다중폴리곤 버전 정본(point_in_polygons)
│   │   ├── models_loader.py           # A의 model_loader.py 그대로
│   │   ├── notifier.py                # C의 AlertNotifier 정본 (SOLAPI_SENDER 표준)
│   │   └── case_archive/
│   │       ├── run_writer.py          # A의 RunWriter(시계열 CSV) — 통합 아님, 공존
│   │       └── catalog.py             # C의 CaseCatalog(매니페스트) — 통합 아님, 공존
│   ├── flood/
│   │   ├── water_segmentation.py      # B 버전 정본(손상프레임 감지 포함), conf 기본값 0.10로 통일
│   │   ├── metrics_core.py            # ★ 신규: ROI교차/면적/expansion/tire-zone 등 A·B 공통 알고리즘만 추출
│   │   ├── standalone_pipeline.py     # ★ A의 MetricsEngine+object_detection 기반, 자체완결형(CLI/배치 전용)
│   │   ├── flood_metrics_engine.py    # B의 FloodMetricsEngine(외부 vehicles/traffic 주입받는 라이브 서버용)
│   │   ├── alert_engine.py            # A 이식 + traffic_state를 B의 TrafficState enum으로 재작성(3개소)
│   │   ├── risk_engine.py             # A/B 통합본(트래픽 enum 표준화 반영)
│   │   └── visualization.py
│   ├── traffic_weather/               # B의 perception/agents/knowledge/orchestrator (report_generator만)
│   ├── crowd/                         # C의 SAM3 파이프라인 (선택적 extra, lazy import)
│   └── service/                       # B의 service/ + C의 app/ 라우트 병합(FastAPI)
├── configs/ / models/ / data/ / scripts/ / tests/
└── legacy/                            # 마이그레이션 기간 중 원본 3개 프로젝트 보관
```

### 5-4. 파일 이동 매핑 (수정본 — 변경/신규 부분만 발췌, 나머지는 v1과 동일)

| 원본 | v1 계획 | v2 수정 |
|---|---|---|
| A `src/metrics.py` | `flood/flood_metrics.py`로 B와 "병합" | `flood/standalone_pipeline.py`로 이동(공유 알고리즘은 `metrics_core.py`로 추출), **B와 병합하지 않음** |
| B `flood/flood_metrics.py` | A와 "병합" | `flood/flood_metrics_engine.py`로 이름 변경 이동, `metrics_core.py`의 공유 헬퍼 사용하도록 리팩터 |
| A `src/roi_utils.py` | B 버전으로 대체 | 동일하나, A `src/metrics.py`(→`standalone_pipeline.py`) 내 `roi_utils.point_in_polygon` 호출 1곳을 `point_in_polygons`로 명시적 수정 필요(작업 항목화) |
| A `src/object_detection.py` | `common/detection.py`로 통합 | `flood/standalone_pipeline.py`가 직접 사용하는 전용 모듈로 그대로 유지(공용화 취소) |
| B `perception/detection_source.py` | 그대로 이동 | 그대로 이동 + `YoloDetectionSource`의 하드코딩 COCO ID를 `common/models_loader.select_class_ids()` 사용하도록 개선(선택 작업, Phase 3) |
| A `src/alert_engine.py` | "그대로 이식" | 이식 + `_evaluate_rules`/`_reason_for_confirmed`의 traffic_state 비교 로직을 B `TrafficState` enum 기준으로 재작성 |
| C `app/notifier.py` | `common/notifier.py`로 승격 | 동일 + `.env.example`에서 `SOLAPI_SENDER` 표준화, `SMS_FROM` 폐기 |
| A `src/archive_manager.py` + C `app/catalog.py` | `common/case_archive.py`로 "통합" | `common/case_archive/run_writer.py` + `common/case_archive/catalog.py`로 **분리 보관**(공유 스키마 강제하지 않음) |
| A `app.py`/`app2.py` | `docs/legacy/`로 참고 보관, 기능 재구현 | 동일(우선순위를 명시적으로 낮춤 — 5-7절 참조) |

### 5-5. 공통화할 코드 vs 개별 도메인에 남길 코드

**진짜로 공통화 가능(코드 100% 또는 거의 동일함을 확인):**
- `config.py`(YAML 로드+기본값 병합, 단 중첩 weights 병합 지원 추가 필요)
- `roi.py`(다중 폴리곤 기하 연산)
- `models_loader.py`(YOLO 로더, 이름 기반 클래스 매핑)
- `notifier.py`(SOLAPI 알림)
- `video_io.py`(비디오 프로브/샘플링, H.264 트랜스코딩)
- `metrics_core.py`(ROI 교차 픽셀 계산, tire-zone 분석, expansion rate — A/B 알고리즘이 확인 결과 완전히 동일)

**공통화하면 안 되는 것(아키텍처가 다름을 확인):**
- 객체탐지+추적 파이프라인 자체(A: 일체형, B: 분리형) — 각 도메인 진입점에 남긴다
- 실행결과 아카이브 포맷(A: 시계열 CSV, C: 정적 매니페스트) — 공존시키되 강제 통합하지 않는다
- alert_engine의 5단계 규칙 vs risk_decision의 4단계 규칙 — 서로 다른 목적(내부 세분화 vs 대외 공식 등급)이므로 유지하되, traffic_state **어휘**만 표준화한다

### 5-6. import / entry point / CLI 통합 방법

- 모든 모듈은 `src/tot_dashboard/` 아래 절대 import로 통일(`sys.path.insert` 제거). `pyproject.toml`의 src-layout + `pip install -e .`로 해결.
- CLI 진입점은 `pyproject.toml`의 `[project.scripts]`로 등록: 예) `tot-flood-standalone`(A식 배치 분석), `tot-service`(B+C 통합 FastAPI 서버 실행), `tot-crowd-pipeline`(C의 SAM3, extras 설치 시에만 활성).
- Streamlit 앱(A)은 `pyproject.toml`의 `[project.optional-dependencies] legacy-streamlit`에만 포함, 기본 설치에서 제외.

### 5-7. 설정/환경변수/의존성/데이터 경로 통합 방법 (수정)

- `.env.example`: B(KMA/HRFCO/ITS/BUSAN/GEMINI/WEBHOOK) + C(SOLAPI_API_KEY/SECRET/**SOLAPI_SENDER**/ALERT_RECIPIENTS/SOLAPI_KAKAO_PF_ID/SOLAPI_KAKAO_TEMPLATE_ID/NOTIFICATION_DRY_RUN/NOTIFICATION_COOLDOWN_SECONDS) 통합, `SMS_FROM`은 사용하지 않음(폐기).
- `pyproject.toml` 의존성: 3개 requirements.txt 합집합 확인 결과 **버전 충돌 없음**(fastapi/uvicorn/jinja2/pydantic/opencv-python/ultralytics/PyYAML 등 범위 표기가 서로 호환). `Pillow`를 A 사용분 포함해 명시적으로 추가. `torch`도 ultralytics 전이 의존성이 아니라 명시 의존성으로 승격(현재 세 프로젝트 모두 암묵적으로만 존재).
- 모델 경로: **A/B 원본 프로젝트의 모델 파일은 어느 것도 삭제·이동하지 않는다**(사용자 명시적 지시). 통합 프로젝트 `models/` 디렉터리에는 `best.pt`(A 원본에서 새로 복사, 회원님이 직접 학습시킨 물 세그멘테이션 정본)와 `yolo11s.pt`(A/B 어느 쪽에서 복사해도 무방, 해시 동일 확인됨, 범용 사람/차량 탐지용) 사본만 새로 추가한다. B의 `water_seg_best.pt`는 A의 `best.pt`와 동일 파일이 이미 이식되어 있던 것이므로, 통합 후에는 `best.pt`라는 이름을 표준으로 쓰고 `water_seg_best.pt`라는 이름은 신규 코드에서 더 이상 참조하지 않는다(원본 B 프로젝트의 파일 자체는 그대로 둠). `sam3.pt`는 리포 밖에 두고 `SAM3_ROOT` 환경변수로 참조 — **이 경로 불일치는 지금 SAM 프로젝트에서도 실제로 깨져 있으므로, 이동 전에 먼저 SAM 원본에서 검증/수정할지, 통합 후 새 경로로 한 번에 정리할지 결정 필요**(9장 참조).
- `configs/blocks.json` 스키마에 `domain` 필드 추가, A의 단일 카메라 설정(`config/roi_config.json`)은 `configs/roi/BLOCK-UNDERPATH01.json` 형태로 블록 1개로 편입(B의 다중폴리곤 자동 업그레이드 로직이 A의 구 포맷도 읽어냄을 확인했으므로 별도 변환 스크립트 없이 파일 복사만으로 가능).

### 5-8. 단계별 구현 순서 (선행조건/완료조건 포함, 재작성)

#### Phase 0 — 준비 및 스켈레톤
- **선행조건**: 없음(최초 단계). 단, `tot_dashboard`가 실제 통합 대상 루트인지 사용자 확인 필요(9장 결정사항 #1).
- **대상 파일**: 신규 `pyproject.toml`, `src/tot_dashboard/__init__.py`, `.gitignore`
- **작업**: git 저장소 초기화(사용자 확인 후), src-layout 패키지 정의, 3개 requirements.txt 합집합 등록
- **완료조건**: `pip install -e .` 성공 + `python -c "import tot_dashboard"` 성공 + `pytest`(빈 스위트) 통과

#### Phase 1 — 공용 유틸리티(`common/`) 이식
- **선행조건**: Phase 0 완료
- **대상 파일**: A `config_utils.py`→`common/config.py`(B의 중첩 weights 병합 로직 추가 확장), A `model_loader.py`→`common/models_loader.py`(무수정 이식), B `flood/roi_utils.py`→`common/roi.py`(정본), A `video_processor.py`의 `probe_video/read_first_frame/iter_source/get_preview_frame/list_folder_images`→`common/video_io.py`
- **작업**: 각 파일 복사 후 import 경로만 수정. `common/config.py`는 A식 단순 병합과 B식 중첩 weights 병합을 모두 지원하는 함수 2개(`load_config_simple`, `load_config_with_nested_merge`)로 노출.
- **완료조건**: `common/roi.py`로 A의 `config/roi_config.json`(구형식)과 B의 `configs/roi/BLOCK-*.json`(신형식) 둘 다 로드 성공하는 pytest 통과. `common/models_loader.py`로 `water_seg_best.pt`/`yolo11s.pt` 로드 성공.

#### Phase 2 — Flood 도메인: 공유 알고리즘 추출 + 두 진입점 분리 이식 (v1과 가장 크게 달라진 단계)
- **선행조건**: Phase 1 완료(`common/roi.py`, `common/config.py` 사용 가능)
- **대상 파일**:
  - 신규 `flood/metrics_core.py` — A `metrics.py`와 B `flood_metrics.py`에서 **알고리즘이 동일함을 확인한** `_tire_zone_analysis`, `_expansion_rate`, `_point_on_mask`/`_count_points_on_mask`, ROI 마스크 교차 계산(`_ensure_masks`)을 추출
  - A `src/metrics.py`+`src/object_detection.py` → `flood/standalone_pipeline.py`(자체완결형, `metrics_core.py` 사용하도록 리팩터)
  - B `flood/flood_metrics.py` → `flood/flood_metrics_engine.py`(외부주입형, `metrics_core.py` 사용하도록 리팩터)
  - B `flood/water_segmentation.py` → `flood/water_segmentation.py`(정본, conf 기본값 0.10)
  - B `flood/risk_engine.py` → `flood/risk_engine.py`(정본)
  - A `src/alert_engine.py` → `flood/alert_engine.py` + traffic_state 비교 로직 3개소를 B `TrafficState` enum 기준으로 재작성
- **작업**: 위 파일 이동 + 리팩터. 특히 `standalone_pipeline.py`에서 `common/roi.py`의 `point_in_polygons` 호출로 명시적 수정(기존 `point_in_polygon` 호출 제거).
- **완료조건**:
  - A의 `scripts/e2e_test.py`/`risk_test.py`를 `standalone_pipeline.py` 기준으로 포팅해 실행 → **원본 실행 결과(`metrics.csv`, `alert_log.csv`)와 프레임별 수치가 동일**(회귀 diff 0건)
  - B의 `scripts/smoke_test_flood.py`를 `flood_metrics_engine.py` 기준으로 포팅해 실행 → 크래시 없이 water_area_ratio가 0~1 범위, 원본과 동일한 risk_score 산출
  - `alert_engine.py`가 B의 `TrafficState` 값(`free/slow/congested/blocked`)으로 5단계 규칙을 올바르게 평가하는 단위 테스트 통과(각 traffic_state 값에 대해 최소 1건씩 규칙 발동 케이스 테스트)

#### Phase 3 — Traffic/Weather 도메인 이식 + detection_source 개선
- **선행조건**: Phase 1 완료(Phase 2와 독립적으로 병행 가능)
- **대상 파일**: B `perception/*`, `agents/*`, `knowledge/ontology.py`, `orchestrator/report_generator.py`
- **작업**: 거의 그대로 이식. **개선 작업**: `perception/detection_source.py`의 `YoloDetectionSource`가 하드코딩한 `_COCO_VEHICLE_IDS`/`_COCO_PERSON_IDS`를 `common/models_loader.select_class_ids()`로 대체(class 이름 기반 매핑으로 전환).
- **완료조건**: `run_poc.py` 포팅 버전 실행 시 콘솔 출력이 원본과 동일. 개선된 `YoloDetectionSource`가 원본과 동일한 COCO 클래스(사람/차/버스/트럭/오토바이)를 인식하는지 회귀 테스트.

#### Phase 4 — Notifier 통합
- **선행조건**: 없음(Phase 1~3과 독립적으로 언제든 가능)
- **대상 파일**: C `app/notifier.py`→`common/notifier.py`, B `orchestrator/notifier.py`(DryRunNotifier) 폐기, `.env.example` 갱신
- **작업**: `SOLAPI_SENDER`로 변수명 표준화, 카카오 변수 추가, B의 파이프라인이 `common/notifier.py`를 사용하도록 연결부 교체
- **완료조건**: `NOTIFICATION_DRY_RUN=true`로 B 파이프라인 실행 시 콘솔에 dry-run 알림 로그 정상 출력

#### Phase 5 — Crowd 도메인 이식(SAM3)
- **선행조건**: Phase 0 완료(다른 Phase와 독립)
- **대상 파일**: C `SAM3_install.py` 분할 이식, `sam3.pt` 경로 문제 수정
- **작업**: `SAM3_ROOT`/`CKPT_FILE` 경로 로직을 이동 전에 먼저 점검(9장 결정사항 #2), extras로 격리
- **완료조건**: GPU 없는 환경에서는 import만 성공(순수 로직 단위 테스트만 통과), GPU 환경에서는 별도로 1회 검증(6장 참조)

#### Phase 6 — 케이스 아카이브 공존 배치
- **선행조건**: Phase 2, Phase 5 부분 완료
- **대상 파일**: A `archive_manager.py`→`common/case_archive/run_writer.py`, C `catalog.py`→`common/case_archive/catalog.py`(통합 아님, 별도 모듈)
- **완료조건**: flood 도메인 실행 시 `run_writer.py`로 CSV 아카이브 정상 생성, crowd 데모 케이스는 `catalog.py`로 정상 조회 — 두 기능이 서로 간섭하지 않음을 확인

#### Phase 7 — 통합 서비스 레이어(FastAPI)
- **선행조건**: Phase 2, 3, 4, 6 완료
- **대상 파일**: B `service/main.py` + C `app/main.py` → `service/main.py`
- **완료조건**: `/api/health`, `/api/blocks`, `/api/cases`, `/api/risk` 등 전 엔드포인트 응답 확인, 브라우저에서 flood 블록 + crowd 데모 케이스 + 알림 dry-run 전 과정 수동 시연 성공

#### Phase 8 — 설정/의존성/대용량 파일 최종 정리
- **선행조건**: Phase 1~7 완료
- **작업**: `.gitignore`에 `*.pt`/대용량 zip 제외, `sam3.pt`/K-water zip을 repo 밖으로 이동, README에 다운로드 절차 문서화. **A/B 원본 프로젝트 폴더의 모델 파일(`best.pt`, `water_seg_best.pt`, `yolo11s.pt` 등)은 삭제하지 않고 원래 위치에 그대로 둔다** — 통합 프로젝트는 필요한 파일의 새 사본만 갖는다.
- **완료조건**: 새 가상환경에서 클론→설치→서버 기동 전체 과정 재현 성공, `git status`에 대용량 파일 없음, A/B 원본 폴더의 모델 파일이 그대로 남아있는지 확인

#### Phase 9 — 최종 정리
- **선행조건**: 전 Phase 완료 + 전체 회귀 테스트 통과
- **작업**: SAM의 일회성 스크립트/venv 폐기, 원본 3개 프로젝트를 별도 백업 위치로 이동(삭제 아님)
- **완료조건**: 사용자 최종 데모 승인

---

## 6. 단계별 테스트 및 완료 기준 (요약)

| Phase | 테스트 명령 | 통과 기준 |
|---|---|---|
| 0 | `pip install -e .`, `pytest` | 설치/임포트 성공, 빈 테스트 통과 |
| 1 | `pytest tests/common/` | 신구 ROI/설정 포맷 모두 로드 성공 |
| 2 | 포팅된 `e2e_test.py`/`risk_test.py`/`smoke_test_flood.py` 실행 후 원본과 CSV diff | 프레임별 수치 diff 0건(부동소수 오차 허용 범위 내) |
| 3 | 포팅된 `run_poc.py` 실행 | 콘솔 출력 원본과 동일, 개선된 detection_source 회귀 테스트 통과 |
| 4 | dry-run 알림 발송 테스트 | 콘솔 로그 정상 출력, 실제 발송 없음 확인 |
| 5 | `python -c "import tot_dashboard.crowd"`(CPU) / GPU 환경 별도 실행 | CPU: import 성공. GPU: 별도 검증(위험요소 참조) |
| 6 | `scripts/validate_dashboard.py`(일반화) | flood run + crowd case 둘 다 매니페스트 검증 통과 |
| 7 | `TestClient`로 API 엔드포인트 호출 + 브라우저 수동 시연 | 전 엔드포인트 200 응답, UI 수동 확인 |
| 8 | 새 venv에서 전체 셋업 재현 | 처음부터 끝까지 재현 성공 |

**중요 원칙**: 각 Phase는 이전 Phase와 **독립적으로 되돌릴 수 있어야** 하며, Phase 2/3/4/5는 서로 병행 가능(의존관계 없음)하므로 반드시 순서대로 할 필요는 없습니다. 단, Phase 7(서비스 레이어)은 2/3/4/6이 끝나야 시작 가능합니다.

---

## 7. 롤백 전략

- **원칙**: 원본 3개 프로젝트(A/B/C)는 Phase 9 전까지 **원래 경로에 그대로 둔 채 절대 수정하지 않는다**. 모든 이동은 "복사" 후 새 경로에서 작업하며, 원본은 각 Phase의 회귀 비교 기준(golden output)으로만 사용한다.
- **Phase 단위 롤백**: 각 Phase는 독립된 git 브랜치(또는 최소 별도 커밋)로 진행하고, 완료조건을 통과하지 못하면 해당 브랜치만 되돌리고 이전 Phase 상태로 복귀한다(다른 Phase에 영향 없음 — Phase 간 의존관계를 5-8절처럼 명시적으로 설계했기 때문에 가능).
- **모델/데이터 파일 롤백**: 사용자 지시에 따라 A/B 원본의 모델 파일은 애초에 삭제하지 않으므로 이 항목의 롤백 리스크는 없음. 통합 프로젝트 `models/` 안의 신규 사본(`best.pt`, `yolo11s.pt`)만 문제가 생기면 원본에서 다시 복사하면 되므로 원복이 항상 가능.
- **서비스 레이어 롤백**(Phase 7): 기존 B의 `service/`, C의 `app/`을 각각 `run_dashboard.ps1`/`run_dashboard2.ps1`로 그대로 실행 가능한 상태로 원본 위치에 유지해두면, 통합 서비스에 문제가 생겨도 원본 두 대시보드로 즉시 되돌아갈 수 있다.
- **최종 삭제 시점**: 원본 3개 프로젝트 폴더는 Phase 9에서 "삭제"가 아니라 **별도 백업 위치로 이동**(예: 외장 드라이브나 `archive_pre_merge/`)하며, 사용자 명시적 승인 없이는 삭제하지 않는다.

---

## 8. 위험 요소와 미확인 사항

### 재확인으로 새로 드러난 위험

1. **Flood 메트릭 이중 아키텍처 유지 비용**: `standalone_pipeline.py`와 `flood_metrics_engine.py`를 분리 유지하면 향후 기능 추가 시 두 곳에 각각 반영해야 할 수 있음(예: 새로운 위험 신호 추가 시). `metrics_core.py` 공유 헬퍼를 최대한 두껍게 만들어 이 비용을 줄이는 것이 Phase 2의 핵심 설계 목표여야 함.
2. **alert_engine의 traffic_state 재작성이 실제 규칙 발동 시점을 미묘하게 바꿀 수 있음**: A의 6개 상태(`empty/unknown/normal/slow/stopped/jammed`)를 B의 4개 상태(`free/slow/congested/blocked`)로 축소 매핑하면, 예를 들어 A의 "unknown"(추적 꺼짐, 판단 불가) 상태에 해당하는 규칙이 B 체계에는 없어 동작이 달라질 수 있음 — Phase 2 완료조건의 회귀 테스트에서 반드시 traffic_state별 케이스를 모두 커버해야 함.
3. **B `.env.example`의 `SMS_FROM` 변수가 이미 어딘가 실사용 중일 가능성**: 이번 조사에서는 B의 어떤 `.py`도 `SMS_FROM`을 읽지 않음을 확인했으나(현재 B의 DryRunNotifier는 env를 전혀 읽지 않음), 만약 사용자가 별도로 참조하는 문서/스크립트가 있다면 변수명 변경 시 혼란 소지. 통합 전 확인 권장.
4. **sam3.pt 경로 문제는 통합과 무관하게 SAM 원본에서도 이미 깨져 있는 버그**: 통합 작업 중 고칠지, 사용자가 원본에서 먼저 고치길 원하는지 결정 필요(9장).

### 기존 위험(유지)

5. Streamlit→FastAPI 전환은 재구현 수준의 작업(3-1/2-표 참조).
6. 대용량 바이너리(`sam3.pt` 3.21GB, K-water zip 302MB) 관리 방식 미정(Git LFS vs 외부 스토리지).
7. GPU 파이프라인 검증은 이번에도 실행 불가(환경 없음) — GPU 머신에서 별도 1회 검증 필요.
8. 외부 API(KMA/HRFCO/ITS/BUSAN/GEMINI/SOLAPI) 실 키 확보 현황 미확인.
9. pytest 부재로 인한 수동 검증 의존도 — Phase별 완료조건에 자동화 테스트를 최대한 반영했으나 여전히 사람이 브라우저로 확인해야 하는 부분(Phase 7) 존재.

### 미확인 사항(그대로 남음)

- 현재 `tot_dashboard`가 실제 통합 대상 루트인지 (9장 #1)
- SAM 루트 `.git`이 왜 손상됐는지(과거 어떤 작업으로 깨졌는지)까지는 원인 미상 — 결과(비정상 상태)만 확인됨
- `cwro_data/*.png`(flood3), `data/case_manifest.template.json` 외 SAM의 `crowd_manage/` venv 내부 패키지 버전 등은 이번 재조사 범위 밖

---

## 9. 구현 시작 전에 반드시 결정해야 할 사항

1. **통합 대상 루트가 정말 `tot_dashboard`인지** — 이 문서와 v1 모두 이를 가정만 하고 있음. 확답 필요.
2. **`sam3.pt` 경로 불일치를 지금(SAM 원본에서) 고칠지, 통합 시점에 새 경로 체계로 흡수해 자연히 해결할지** — 전자는 SAM 단독으로도 이득, 후자는 작업량 절약. 사용자 선호 확인 필요.
3. **Streamlit UI(A) 웹 재구현을 이번 통합 범위에 포함할지, 후속 작업으로 미룰지** — 재구현 비용이 상당함이 확인되었으므로(`st.session_state` 9곳 이상 사용) 우선순위 결정 필요.
4. **대용량 파일(3.21GB+302MB) 저장 전략** — Git LFS 도입 여부, 또는 외부 네트워크 드라이브/클라우드 스토리지 경로 지정.
5. **크라우드(SAM3) 도메인을 이번 1차 통합 범위에 포함할지** — GPU 환경이 없어 이번에도 실행 검증이 불가능했으므로, GPU 머신이 확보되기 전까지는 "코드만 이식하고 검증은 보류"할지, 아예 후속 Phase로 완전히 분리할지 결정 필요.
6. **`alert_engine`의 traffic_state 매핑 규칙**(A의 6상태→B의 4상태로 축소 시 어떤 규칙을 어떻게 유지할지)을 개발자가 판단할지, 도메인 전문가(사용자)가 임계값을 재정의할지 — 알림 등급에 직접 영향을 주는 부분이라 임의로 정하면 안 됨.
