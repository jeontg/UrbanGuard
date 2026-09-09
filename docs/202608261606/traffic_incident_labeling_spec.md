# 낙하물·화재연기 라벨링 규격 및 데이터 파이프라인 — Phase 7

> 목적: 교통위험 미보유 탐지 기능 추가 계획(Phase 7)의 산출물 ①②③ —
> 라벨링 규격, 수집·라벨링 스크립트 사용법, 학습 진입점 — 을 정리한다.
> **이 문서는 탐지 기능을 추가하지 않는다.** 데이터가 없어 실제 학습·
> 검증은 이번 회차 범위 밖이다(`docs/pending_tasks.md` Phase 7 참고).
> 작성일: 2026-08-26

---

## 0. 왜 이 파이프라인이 필요한가

낙하물·화재연기는 **COCO에 없는 클래스**라 사전학습 모델을 그대로 쓸 수
없다. `core/vocabulary.py`에 어휘는 이미 등록돼 있지만
(`traffic_debris`·`traffic_fire_smoke`), `detectable=False`로 심어져
있다 — 화면이 "탐지한다"고 거짓말하지 않기 위해서다. 이 값을 `True`로
바꾸려면 **실제 학습된 모델이 있어야** 하고, 그러려면 라벨링된 데이터가
있어야 한다. 이 문서와 스크립트 4종이 그 첫 단계다.

---

## 1. 위험유형·클래스 정의

| 위험유형 코드(`core/vocabulary.py`) | 한글 라벨 | YOLO 클래스 id | YOLO 클래스명 |
|---|---|---|---|
| `traffic_debris` | 낙하물 | `0` | `debris` |
| `traffic_fire_smoke` | 화재·연기 | `1` | `fire_smoke` |

**클래스 판단 기준**
- **낙하물(`debris`)**: 도로 위에 있으면 안 되는 고정되지 않은 물체 —
  낙석·타이어 파편·화물 낙하물·쓰러진 표지판 등. 정상적으로 도로에
  있는 것(차량·연석·중앙분리대·맨홀 뚜껑)은 제외
- **화재·연기(`fire_smoke`)**: 화염이 보이거나, 명백히 연소로 인한
  연기가 보이는 경우. 안개·수증기·배기가스는 제외(구분이 애매하면
  박스를 그리지 않는다 — 확신 없는 라벨은 없는 것보다 나쁘다)

---

## 2. 라벨 형식 — YOLO 검출(박스) 포맷

`scripts/label_road_defects.py`와 같은 형식(`road_pothole`/`road_crack`이
이미 이 형식으로 라벨링돼 있다 — 형식을 통일하면 나중에 노면과 함께
멀티태스크 학습을 시도할 때도 데이터를 다시 만들 필요가 없다).

```
<클래스id> <중심x> <중심y> <너비> <높이>
```
좌표는 전부 이미지 너비·높이로 **정규화**(0~1)한 값이다. 예:
```
0 0.512300 0.348900 0.120000 0.085000
1 0.203100 0.611200 0.340000 0.410000
```

**빈 프레임(낙하물·화재연기가 전혀 없는 정상 장면)도 반드시 라벨링하세요**
— 박스 0개인 빈 `.txt` 파일로 저장합니다. 배경(네거티브) 샘플이 없으면
오탐이 줄지 않습니다 — 침수 세그멘테이션에서 실제로 겪은 문제입니다
(`docs/flood_water_dataset_workflow.md` §"1차 학습의 실패와 원인": 배경
샘플 0장으로 학습했더니 맑은 날 CCTV에서 최대 65% 오탐, 배경 15%를
추가하자 0.00%로 해결됨). **낙하물·화재연기도 같은 함정을 피하려면
전체 라벨링 데이터의 상당 비율(1차 목표 15% 이상)을 배경으로 채우세요.**

---

## 3. 폴더 구조

```
data/datasets/traffic_incident_own/
├── raw/
│   ├── live/<block_id>/<timestamp>.jpg      # collect_traffic_incident_frames.py 산출물(배경 위주)
│   └── <set_name>/<영상명>_<프레임번호>.jpg  # extract_traffic_incident_frames.py 산출물(사고·화재 영상)
├── labeled/<set>/
│   ├── images/                              # label_traffic_incident.py 가 복사한 원본
│   └── labels/                              # 같은 이름의 .txt (YOLO 포맷)
└── yolo/                                     # 학습 직전 최종 배치(images/ labels/ data.yaml)
```

`D:\dev-PoC_DATA\07_학습데이터_교통\traffic_incident_own\yolo\`가 보관소
경로다(`common/data_archive.py`의 `traffic_incident_yolo` 키, 기존
`traffic_vehicle_own`과 같은 07번 폴더 아래 — 낙하물·화재연기는 별도
도메인이 아니라 교통위험의 하위 위험유형이므로 새 번호를 만들지 않는다).

**`labeled/` → `yolo/` 로 옮길 때 만들어야 할 `data.yaml` 예시**:
```yaml
path: .
train: images/train
val: images/val
names:
  0: debris
  1: fire_smoke
```
(`train`/`val` 분리는 이 문서가 강제하지 않는다 — 데이터가 실제로
쌓이면 그때 분리 비율을 정한다.)

---

## 4. 파이프라인 실행 순서

```bash
# ① 배경(정상) 프레임 수집 — 여러 시점에 반복 실행
python scripts/collect_traffic_incident_frames.py

# ② 확보한 사고·화재 영상에서 프레임 추출(저작권 확인 후)
python scripts/extract_traffic_incident_frames.py --video <경로> --every 15

# ③ 라벨링(사람이 직접 박스를 그린다 — 자동 탐지기 없음)
python scripts/label_traffic_incident.py --set live/BLOCK-OLYMPIC
python scripts/label_traffic_incident.py --folder data/datasets/traffic_incident_own/raw/<set_name>

# ④ labeled/ 를 yolo/ 로 정리(images/labels 배치 + data.yaml 작성) — 수작업 또는 별도 변환 스크립트
#    (flood 도메인의 convert_flood_masks_to_yolo.py 같은 전용 변환기가 필요해지면 그때 만든다)

# ⑤ 학습 — 데이터 없으면 여기서 거부된다
python scripts/train_traffic_incident_yolo.py --epochs 30
```

---

## 5. 필요 데이터 규모(추정)

⚠️ **확정치가 아닌 일반적 경험칙입니다**(`docs/flood_water_dataset_workflow.md`
§3과 같은 성격의 추정). 실제 필요량은 1차 학습 후 성능을 보고 조정해야
합니다.

| 단계 | 프레임 수(대략) | 비고 |
|---|---|---|
| 최소 실험 | 100~300장 | 파이프라인 동작 확인용(클래스당 최소 표본 확보) |
| 초기 실용 | 800~1,500장 | 배경 15% 이상 포함 |
| 안정 운영 | 3,000장 이상 | 낙하물·화재연기는 물체가 작고 다양해 침수(단일 큰 영역)보다 더 많은 표본이 필요할 가능성이 높음 |

**다양성이 수량보다 중요합니다** — 노면 손상(Phase 2)에서 도메인 갭으로
실패한 경험을 반복하지 않으려면, 실제 운영할 부산 CCTV에서 수집한 배경
데이터의 비중을 높여야 합니다. 공개 데이터셋(§ 별도 라이선스 조사
문서 `traffic_incident_dataset_license_investigation.md`)은 낙하물·
화재연기 **자체**의 다양성을 채우는 용도로만 쓰고, 배경은 반드시
부산 CCTV 실측으로 채우세요.

---

## 6. `.pseudo` 마커를 쓰지 않는 이유

`scripts/label_crowd_person_auto.py`는 **이미 검증된 프로덕션 탐지기**로
가라벨을 자동 생성하고 `.pseudo` 마커로 "사람이 아직 확인 안 함"을
표시한다. 이 파이프라인은 그 방식을 쓰지 않는다 — **낙하물·화재연기를
자동으로 찾아 줄 기존 탐지기가 아예 없기 때문**이다(COCO에 없는
클래스). 그래서 `label_road_defects.py`·`label_flood_water.py`와 같은
**전량 수동 라벨링** 방식을 따른다 — 사람이 그린 박스는 그 자체로
정답이므로 별도 "검수 대기" 표시가 필요 없다.

---

## 7. 이번 회차에 하지 않은 것 (범위 밖)

1. **실제 데이터 수집·라벨링** — 스크립트만 준비했다. 파이프라인을
   실제로 돌려 데이터를 쌓는 것은 별도 작업(우기를 기다려야 했던
   침수 사례처럼, 화재·낙하물도 드문 사건이라 장기간 필요)
2. **공개 데이터셋 채택** — `traffic_incident_dataset_license_investigation.md`
   가 후보를 조사했을 뿐, 법무 검토·최종 결정은 하지 않았다
3. **`/models`·학습 화면 UI 통합** — `core/analytics.py`의
   `MODEL_DOMAINS`·`training_jobs.py`의 `DOMAINS`에 새 행을 추가하면
   교통위험 이벤트 집계가 "차량 검출" 모델과 겹쳐 집계될 위험이 있다
   (도메인 축은 대분류 `traffic` 단위, 위험유형 축은 `hazard_type_code`
   단위로 서로 다르기 때문 — `core/vocabulary.py` 설계 원칙). 이 UI
   통합은 사고 의심(Phase 5)의 `detail["판정 근거"]` 표기처럼 정직하게
   지표를 나누는 방법을 **먼저 설계**해야 하므로 후속 과제로 남긴다
4. **`detectable=True` 전환** — 실제 학습·검증된 모델이 생기기 전까지는
   그대로 `False`로 둔다

---

## 참고

- `docs/202608261606/traffic_incident_dataset_license_investigation.md` — 데이터셋 후보 라이선스 조사
- `docs/flood_water_dataset_workflow.md` — 자체 데이터 구축 선례(배경 샘플 함정 포함)
- `docs/road_surface_management_plan.md` — 공개 데이터셋의 도메인 갭 실패 사례
- `C:\Users\전태건\.claude\plans\ticklish-discovering-pine.md` Phase 7
