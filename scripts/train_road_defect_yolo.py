"""도로 노면 손상 탐지(YOLO) 학습 — 관리자 화면(신규)에서 쓰는 범용 진입점.

## 왜 새로 만들었나

기존 학습 스크립트 3종(``train_road_defect_prototype.py``·``_v2.py``·
``train_svrdd.py``)은 전부 **에폭·데이터셋·실행 이름이 코드에 고정**돼 있어
(v1은 인자 없음, v2는 v1 체크포인트 재사용이 하드코딩) 화면에서 관리자가
값을 바꿔 재실행하는 용도로 쓰기 어렵다. 이 스크립트는 그 세 번의 시행착오
(``docs/road_surface_management_plan.md`` Phase 2)에서 나온 두 데이터셋
(RDD2022·SVRDD)을 **화면에서 고를 수 있게** 감싼 것이며, 학습 로직 자체는
바뀌지 않았다.

⚠️ 2026-08-13 결론: 실시간 CCTV(240p)에서는 4회 학습 전부 실패했다
(``docs/road_surface_management_plan.md`` 머리말). 이 스크립트로 다시 학습해도
같은 도메인 갭이 있으면 같은 결과가 나온다 — **관리자 화면은 「그래도 다시
시도해 볼 수 있게」 열어 두는 것**이지, 이 결론을 뒤집는 새 근거를 만들지
않는다.

Usage:
    python scripts/train_road_defect_yolo.py --dataset rdd2022 --epochs 15
    python scripts/train_road_defect_yolo.py --dataset svrdd --epochs 12 --imgsz 640
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import resolve_dataset
from tot_dashboard.common.video_io import patch_cv2_imread_for_unicode_paths

DATASET_KEYS = {"rdd2022": "road_rdd2022_yolo", "svrdd": "road_svrdd_yolo"}
RUNS = PROJECT_ROOT / "data" / "training_runs" / "road"
BASE_WEIGHTS_DEFAULT = PROJECT_ROOT / "models" / "yolo11s.pt"


def main() -> None:
    # 학습 데이터가 보관소(D:\dev-PoC_DATA\01_학습데이터_노면\...)의 한글
    # 폴더명 아래 있다. ultralytics YOLO의 내부 데이터로더도 cv2.imread를
    # 쓰므로, 실제 학습이 시작되기 전에 전역으로 패치해 둔다(2026-08-25,
    # 침수 학습에서 겪은 것과 같은 문제 — 여기서는 미리 막는다).
    patch_cv2_imread_for_unicode_paths()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=sorted(DATASET_KEYS), default="rdd2022")
    ap.add_argument("--base-weights", default=None,
                    help="시작 가중치(생략 시 yolo11s.pt 사전학습)")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--imgsz", type=int, default=416)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--name", default=None, help="실행 이름(생략 시 자동 생성)")
    args = ap.parse_args()

    data_dir = resolve_dataset(DATASET_KEYS[args.dataset])
    if data_dir is None:
        raise SystemExit(
            f"[오류] 데이터셋을 찾을 수 없습니다({args.dataset}). "
            "학습 데이터 관리 화면에서 위치를 확인하십시오.")
    data_yaml = data_dir / "data.yaml"
    if not data_yaml.is_file():
        raise SystemExit(f"[오류] data.yaml이 없습니다: {data_yaml}")

    base = Path(args.base_weights) if args.base_weights else BASE_WEIGHTS_DEFAULT
    if not base.is_file():
        raise SystemExit(f"[오류] 시작 가중치가 없습니다: {base}")

    name = args.name or f"{args.dataset}_{int(time.time())}"
    print(f"[road-train] dataset={args.dataset} ({data_yaml}) "
          f"base={base.name} epochs={args.epochs} imgsz={args.imgsz} "
          f"device={args.device}")

    from ultralytics import YOLO

    t0 = time.time()
    model = YOLO(str(base))
    model.train(
        data=str(data_yaml),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=4,
        project=str(RUNS),
        name=name,
        exist_ok=True,
        patience=0,
        verbose=True,
    )
    best = RUNS / name / "weights" / "best.pt"
    print(f"\n[road-train] 완료 ({(time.time() - t0) / 60:.1f}분) -> {best}")


if __name__ == "__main__":
    main()
