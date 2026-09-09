"""Phase 2 프로토타입 2차 시도 — v1(15 epoch, road_defect_proto) 체크포인트에서
이어서 epoch을 늘려 재학습한다.

배경: v1 학습 결과(val mAP50 0.193)를 실제 부산 CCTV 프레임에 돌려보니
conf=0.01까지 낮춰도 0건 검출됐다(같은 conf로 RDD2022 검증 이미지는 7~15개
검출). docs/road_surface_management_plan.md Phase 2 "핵심 발견" 참고 — 이
차이가 "학습 부족" 때문인지 "촬영 조건 자체가 너무 다른 도메인 갭" 때문인지
구분하기 위해, epoch을 크게 늘려 재시도한다(사용자 결정: 옵션 1).

v1의 best.pt를 시작 가중치로 삼아 35 epoch을 추가 학습(계 50 epoch 상당).
결과는 별도 run(``road_defect_proto_v2``)에 저장해 v1 결과를 덮어쓰지 않는다.

Usage:
    python scripts/train_road_defect_prototype_v2.py
"""
from __future__ import annotations

from ultralytics import YOLO

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import resolve_dataset
from tot_dashboard.common.video_io import patch_cv2_imread_for_unicode_paths

# ★ 2026-08-25 — 2026-08-14에 데이터가 D:\dev-PoC_DATA로 옮겨진 뒤로 이
#   경로가 실제로는 빈 폴더였다(학습 화면 신설 중 발견). 프로젝트→보관소
#   순으로 자동 탐색한다.
_yolo_dir = resolve_dataset("road_rdd2022_yolo")
DATA_YAML = (_yolo_dir / "data.yaml") if _yolo_dir else (
    PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "yolo" / "data.yaml")
V1_BEST = PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "runs" / "road_defect_proto" / "weights" / "best.pt"
RUN_NAME = "road_defect_proto_v2"


def main() -> None:
    # 보관소 경로(D:\dev-PoC_DATA\01_학습데이터_노면\...)의 한글 폴더명을
    # ultralytics 내부 데이터로더가 cv2.imread로 읽다 조용히 실패하는 문제를
    # 막는다(2026-08-25, 침수 학습에서 실제로 겪음).
    patch_cv2_imread_for_unicode_paths()
    model = YOLO(str(V1_BEST))
    model.train(
        data=str(DATA_YAML),
        epochs=35,  # v1의 15 epoch에 이어 35 epoch 추가 (계 50 epoch 상당)
        imgsz=416,
        batch=16,
        device="cpu",
        workers=4,
        project=str(PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "runs"),
        name=RUN_NAME,
        exist_ok=True,
        patience=0,
        verbose=True,
    )
    best = PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "runs" / RUN_NAME / "weights" / "best.pt"
    print(f"[train_road_defect_prototype_v2] done. best checkpoint: {best}")


if __name__ == "__main__":
    main()
