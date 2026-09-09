"""Phase 2 프로토타입 파인튜닝 — CPU에서 짧게 돌려 "탐지가 되는지" 여부만 검증.

GPU가 없는 환경이라 정식 성능 목표(Phase 3에서 별도 설정)를 위한 학습이 아니라,
scripts/prepare_rdd2022_yolo.py로 만든 데이터로 파이프라인이 끝까지 동작하고
실제 도로 이미지에서 박스가 나오는지만 육안으로 확인하기 위한 최소 학습이다.

베이스 체크포인트(yolov8n.pt)는 ultralytics가 최초 실행 시 공식 릴리스에서
자동으로 받아온다(이 프로젝트가 이미 의존 중인 ultralytics 패키지의 표준 동작,
~6MB).

Usage:
    python scripts/train_road_defect_prototype.py
"""
from __future__ import annotations

from ultralytics import YOLO

from tot_dashboard.common.config import PROJECT_ROOT

DATA_YAML = PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "yolo" / "data.yaml"
RUN_NAME = "road_defect_proto"


def main() -> None:
    model = YOLO("yolov8n.pt")
    model.train(
        data=str(DATA_YAML),
        epochs=15,
        imgsz=416,
        batch=16,
        device="cpu",
        workers=4,
        project=str(PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "runs"),
        name=RUN_NAME,
        exist_ok=True,
        patience=0,  # no early-stop -- this is a short fixed prototype run
        verbose=True,
    )
    best = PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "runs" / RUN_NAME / "weights" / "best.pt"
    print(f"[train_road_defect_prototype] done. best checkpoint: {best}")


if __name__ == "__main__":
    main()
