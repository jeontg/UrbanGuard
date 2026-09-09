"""교통위험 — 차량검출(YOLO) 자체 학습. 관리자 화면(신규)에서 쓰는 진입점.

## 현재 상태 — 아직 자체 학습 데이터가 없다 (2026-08-25 확인)

지금 운영 중인 차량검출은 COCO 사전학습 YOLO11s를 **파인튜닝 없이 그대로**
쓴다(``traffic_weather/perception/detection_source.py``). 부산 CCTV 전용으로
학습한 적이 한 번도 없다 — 「4대 탐지 기능 기술 정리」(2026-08-25) §2-2 참고.

이 스크립트는 침수·도로 노면과 **같은 데이터 라벨링 형식(YOLO 상자 라벨)**을
쓰도록 미리 준비해 둔 것이다. 라벨링된 데이터가 없으면 실행 자체가 거부된다
— 데이터 없이 「학습했다」는 결과를 내면 안 되기 때문이다.

## 데이터가 준비되면

``data/datasets/traffic_vehicle_own/yolo/`` (또는 학습 데이터 보관소
``D:\\dev-PoC_DATA\\07_학습데이터_교통\\traffic_vehicle_own\\yolo\\``)에
Ultralytics YOLO 형식(``images/`` · ``labels/`` · ``data.yaml``, 클래스
``car``·``bus``·``truck``·``motorcycle``)으로 채워 넣으면 이 스크립트가
그대로 읽는다. 코드 수정이 필요 없다.

Usage:
    python scripts/train_traffic_vehicle_yolo.py --epochs 30
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import dataset_hint, resolve_dataset
from tot_dashboard.common.video_io import patch_cv2_imread_for_unicode_paths

RUNS = PROJECT_ROOT / "data" / "training_runs" / "traffic"
BASE_WEIGHTS_DEFAULT = PROJECT_ROOT / "models" / "yolo11s.pt"


def main() -> None:
    # 데이터가 준비되면 보관소(D:\dev-PoC_DATA\07_학습데이터_교통\...)의
    # 한글 폴더명 아래 놓일 것이다. ultralytics YOLO의 내부 데이터로더도
    # cv2.imread를 쓰므로 미리 전역 패치해 둔다(2026-08-25, 침수 학습에서
    # 실제로 겪은 문제).
    patch_cv2_imread_for_unicode_paths()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-weights", default=None,
                    help="시작 가중치(생략 시 yolo11s.pt 사전학습)")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--imgsz", type=int, default=416)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    data_dir = resolve_dataset("traffic_vehicle_yolo")
    if data_dir is None:
        raise SystemExit(
            "[오류] 교통 차량검출 학습 데이터가 없습니다.\n"
            f"       다음 위치에 YOLO 형식으로 채워 넣으십시오: "
            f"{dataset_hint('traffic_vehicle_yolo')}")
    data_yaml = data_dir / "data.yaml"
    if not data_yaml.is_file():
        raise SystemExit(f"[오류] data.yaml이 없습니다: {data_yaml}")

    base = Path(args.base_weights) if args.base_weights else BASE_WEIGHTS_DEFAULT
    if not base.is_file():
        raise SystemExit(f"[오류] 시작 가중치가 없습니다: {base}")

    name = args.name or f"traffic_vehicle_{int(time.time())}"
    print(f"[traffic-train] data={data_yaml} base={base.name} "
          f"epochs={args.epochs} imgsz={args.imgsz} device={args.device}")

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
    print(f"\n[traffic-train] 완료 ({(time.time() - t0) / 60:.1f}분) -> {best}")


if __name__ == "__main__":
    main()
