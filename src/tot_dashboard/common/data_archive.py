"""학습 데이터 보관소(``D:\\dev-PoC_DATA``) 경로 해석.

## 왜 필요한가

2026-08-14, 서비스 운영에 쓰이지 않는 학습·평가·원본 데이터 약 14GB를
프로젝트 폴더(``data/datasets/``)에서 ``D:\\dev-PoC_DATA\\``로 옮겼다
(백업·복제·검색을 가볍게 하기 위함). 그런데 기존 학습 스크립트들
(``scripts/train_flood_water_cpu.py`` · ``train_road_defect_prototype_v2.py`` ·
``train_svrdd.py``)은 여전히 **프로젝트 폴더 경로를 하드코딩**하고 있었다 —
확인해 보니 그 경로들이 전부 빈 폴더가 돼 있었다. 이 모듈이 없으면 학습을
다시 돌릴 때마다 "데이터가 없다"는 혼란스러운 오류만 난다.

## 해석 규칙

**프로젝트 폴더에 실제 데이터가 있으면 그것을 먼저 쓴다** — 진행 중인
재학습이 새 데이터를 프로젝트 폴더에 두고 작업할 수도 있기 때문이다. 없으면
보관소로 물러난다. 사용자 확인(2026-08-25) — "보관소를 그대로 읽는다"는
결정에 따라 **보관소 데이터를 프로젝트로 복사하지 않는다.**

보관소 위치는 환경변수 ``URBANGUARD_DATA_ARCHIVE``로 바꿀 수 있다
(기본값 ``D:\\dev-PoC_DATA``).
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import PROJECT_ROOT

DATA_ARCHIVE_ROOT = Path(
    os.environ.get("URBANGUARD_DATA_ARCHIVE", r"D:\dev-PoC_DATA")
)

# 학습 스크립트가 쓰는 데이터셋 키 -> (프로젝트 내 상대경로, 보관소 내 상대경로).
# 두 경로는 D:\dev-PoC_DATA\README.md "2. 무엇이 어디에 있나"의 표와 일치한다.
_DATASET_MAP: dict[str, tuple[str, str]] = {
    "flood_labeled": (
        "data/datasets/flood_water_own/labeled",
        "02_학습데이터_침수/flood_water_own/labeled",
    ),
    "road_rdd2022_yolo": (
        "data/datasets/rdd2022_czech/yolo",
        "01_학습데이터_노면/rdd2022_czech/yolo",
    ),
    "road_svrdd_yolo": (
        "data/datasets/svrdd/yolo",
        "01_학습데이터_노면/svrdd/yolo",
    ),
    # 교통·인파는 아직 자체 라벨 데이터가 없다(2026-08-25 확인). 학습 화면은
    # 4개 도메인에 동일한 구조로 제공하되, 이 경로에 데이터가 채워지기 전까지
    # dataset_check()가 "준비 안 됨"을 보고한다 — docs/pending_tasks.md 참고.
    "traffic_vehicle_yolo": (
        "data/datasets/traffic_vehicle_own/yolo",
        "07_학습데이터_교통/traffic_vehicle_own/yolo",
    ),
    # 낙하물·화재연기(Phase 7, 2026-08-26). 별도 도메인이 아니라 교통위험
    # 안의 위험유형이라 traffic_vehicle_yolo와 같은 07번 폴더 아래 별도
    # 서브셋으로 둔다 — 새 번호를 만들지 않는다(D:\dev-PoC_DATA\README.md
    # 명명 관례). 이 회차는 데이터 자체가 없어(§ traffic_debris·
    # traffic_fire_smoke의 detectable=False 근거) resolve_dataset()이
    # 아직 None을 돌려준다 — 학습 진입점이 그걸 보고 정직하게 거부한다.
    "traffic_incident_yolo": (
        "data/datasets/traffic_incident_own/yolo",
        "07_학습데이터_교통/traffic_incident_own/yolo",
    ),
    "crowd_person_yolo": (
        "data/datasets/crowd_person_own/yolo",
        "08_학습데이터_인파/crowd_person_own/yolo",
    ),
    # 해외 근접촬영 데이터(Zenodo/Arcioni + Kaggle filtered) 통합본.
    # 2026-09-04, 부산 CCTV 근접촬영 실측에서도 손상 사례를 못 찾아 확보한
    # 대체 데이터원 — nc=2(pothole, crack). 상세: 보관소 폴더 README.md.
    "road_combined_v1": (
        "data/datasets/road_combined_v1",
        "01_학습데이터_노면/combined_road_v1",
    ),
    # v1 + Stellenbosch(Kaggle 미러, 실제 픽셀 좌표 라벨 확보) 추가.
    # 2026-09-05 — 원래 구글 드라이브 원본(13,110장)은 라벨 위치가 확인되지
    # 않았으나, Kaggle 미러에 train_df.csv/주석 txt로 실제 바운딩박스가
    # 포함돼 있음을 확인해 추가. v1은 재현성을 위해 그대로 보존하고 이
    # 키를 새로 둔다. 상세: 보관소 폴더 README.md.
    "road_combined_v2": (
        "data/datasets/road_combined_v2",
        "01_학습데이터_노면/combined_road_v2",
    ),
    # v2 + HRP4K(중국 차량탑재 4K, CC BY 4.0) 추가. 2026-09-05 — Zenodo
    # 배포본 자체의 train split 절반가량이 업로드 누락(라벨은 있는데
    # 이미지가 없음)임을 확인, 실사용 가능한 4,086장만 반영. 상세:
    # 보관소 폴더 README.md.
    "road_combined_v3": (
        "data/datasets/road_combined_v3",
        "01_학습데이터_노면/combined_road_v3",
    ),
}


def resolve_dataset(key: str) -> Path | None:
    """데이터셋 키 -> 실제로 존재하는 경로. 둘 다 없으면 ``None``.

    프로젝트 경로가 있으면(그리고 비어 있지 않으면) 그쪽을 우선한다.
    """
    if key not in _DATASET_MAP:
        raise KeyError(f"알 수 없는 데이터셋 키: {key}")
    proj_rel, archive_rel = _DATASET_MAP[key]
    proj_path = PROJECT_ROOT / proj_rel
    if proj_path.exists() and any(proj_path.iterdir()):
        return proj_path
    archive_path = DATA_ARCHIVE_ROOT / archive_rel
    if archive_path.exists() and any(archive_path.iterdir()):
        return archive_path
    return None


def dataset_hint(key: str) -> str:
    """데이터가 없을 때 화면에 보여줄 안내 — 어디에 두면 되는지."""
    if key not in _DATASET_MAP:
        raise KeyError(f"알 수 없는 데이터셋 키: {key}")
    proj_rel, archive_rel = _DATASET_MAP[key]
    return (f"{proj_rel} (프로젝트) 또는 "
            f"{DATA_ARCHIVE_ROOT / archive_rel} (보관소)")
