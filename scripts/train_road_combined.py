"""해외 근접촬영 통합 데이터(Zenodo/Arcioni + Kaggle filtered)로 노면
손상 모델을 학습한다.

무엇을 확인하려는 학습인가
    2026-09-04, 부산 CCTV 39개 중 근접촬영이 되는 곳까지 실측으로 다시
    훑었지만 실제 손상 사례를 찾지 못했다(``docs/pending_tasks.md`` 참고).
    그래서 해외 근접촬영 공개 데이터셋으로 방향을 틀어 Zenodo(로마·
    Sacrofano, 근접촬영, 실손상 확인)와 Kaggle(스톡사진·오분류 걸러낸
    ``filtered/``)을 ``combined_road_v1``으로 합쳤다(train 3,008장 ·
    valid 623장, ``nc=2`` — pothole·crack).

    이번 학습의 목적은 SVRDD 때와 같다 — 「성능이 얼마나 나오는가」가
    아니라 **「이 데이터로 신호가 잡히기는 하는가」** 를 먼저 본다.

⚠️ 이 환경은 GPU 가 없다 (torch 2.13.0+**cpu**)
    UrbanGuard 4개 도메인 서비스도 상시 CPU를 쓰고 있어, 처음부터
    본격(``--full``) 학습을 걸면 서비스 응답성에 영향을 줄 수 있다
    (사용자 결정, 2026-09-04) — 기본값은 **빠른 신호 확인(quick)**.

Usage:
    python scripts/train_road_combined.py              # 빠른 신호 확인 (기본)
    python scripts/train_road_combined.py --full        # 본격 학습
    python scripts/train_road_combined.py --resume       # 중단된 run 이어서
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import resolve_dataset
from tot_dashboard.common.video_io import patch_cv2_imread_for_unicode_paths

DATASET_KEY = "road_combined_v3"
_DATA_DIR = resolve_dataset(DATASET_KEY)
DATA_YAML = (_DATA_DIR / "data.yaml") if _DATA_DIR else (
    PROJECT_ROOT / "data" / "datasets" / DATASET_KEY / "data.yaml")
# 학습 산출물(체크포인트·로그)은 보관소 관례(D:\dev-PoC_DATA\03_학습결과_평가\
# svrdd_runs 등)를 그대로 따라 프로젝트 폴더를 무겁게 하지 않는다.
RUNS = Path(r"D:\dev-PoC_DATA\03_학습결과_평가\road_combined_runs")
# 사전학습 가중치에서 출발한다. 맨바닥에서 시작하면 CPU 로는 수렴하지 않는다.
BASE_WEIGHTS = PROJECT_ROOT / "models" / "yolo11s.pt"


def _last_epoch(results_csv: Path) -> int:
    """results.csv 에 기록된 마지막 에폭. 읽지 못하면 0."""
    try:
        lines = [l for l in results_csv.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        return int(float(lines[-1].split(",")[0]))
    except Exception:  # noqa: BLE001
        return 0


def main() -> None:
    # 보관소 경로("...노면\combined_road_v1\...")의 한글 폴더명을 ultralytics
    # 내부 데이터로더가 cv2.imread 로 읽다 조용히 실패하는 문제를 막는다.
    patch_cv2_imread_for_unicode_paths()
    ap = argparse.ArgumentParser(description="노면 손상(통합 해외 데이터) 학습")
    ap.add_argument("--full", action="store_true",
                    help="본격 학습(에폭·해상도 상향, CPU 에서는 수일 소요)")
    ap.add_argument("--epochs", type=int, default=None)
    ap.add_argument("--imgsz", type=int, default=None)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--fraction", type=float, default=None,
                    help="학습셋 중 사용할 비율(0~1)")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--name", default=None)
    ap.add_argument("--resume", action="store_true",
                    help="중단된 run 을 이어서 학습한다(weights/last.pt 필요)")
    ap.add_argument("--dataset", default=DATASET_KEY,
                    choices=["road_combined_v1", "road_combined_v2", "road_combined_v3"],
                    help="v1(Zenodo+Kaggle, 3,631장) / v2(+Stellenbosch, 7,984장) / "
                         "v3(+HRP4K, 12,070장, 기본값)")
    args = ap.parse_args()

    global DATA_YAML
    if args.dataset != DATASET_KEY:
        _dir = resolve_dataset(args.dataset)
        DATA_YAML = (_dir / "data.yaml") if _dir else (
            PROJECT_ROOT / "data" / "datasets" / args.dataset / "data.yaml")

    # ── 이어서 학습 ────────────────────────────────────────────────────────
    if args.resume:
        name = args.name or "road_combined_quick"
        last = RUNS / name / "weights" / "last.pt"
        if not last.is_file():
            print(f"[오류] 이어서 학습할 체크포인트가 없습니다: {last}")
            return
        done = _last_epoch(RUNS / name / "results.csv")
        print(f"[road_combined] 이어서 학습 — {name} (완료 {done}에폭까지) · {args.device}")

        from ultralytics import YOLO

        t0 = time.time()
        YOLO(str(last)).train(resume=True)
        best = RUNS / name / "weights" / "best.pt"
        print(f"\n[road_combined] 학습 완료 — {(time.time() - t0) / 60:.1f}분")
        print(f"[road_combined] 체크포인트: {best}")
        return

    if not DATA_YAML.is_file():
        print(f"[오류] 데이터셋이 없습니다: {DATA_YAML}")
        return
    if not BASE_WEIGHTS.is_file():
        print(f"[오류] 사전학습 가중치가 없습니다: {BASE_WEIGHTS}")
        return

    if args.full:
        epochs = args.epochs or 150
        imgsz = args.imgsz or 640
        fraction = args.fraction or 1.0
        name = args.name or "road_combined_full"
    else:
        # 빠른 신호 확인 — 「신호가 잡히는가」만 본다.
        epochs = args.epochs or 25
        imgsz = args.imgsz or 416
        fraction = args.fraction if args.fraction is not None else 1.0
        name = args.name or "road_combined_quick"

    print(f"[road_combined] {'본격' if args.full else '빠른 신호 확인'} 학습 — "
          f"epochs {epochs} · imgsz {imgsz} · fraction {fraction} · {args.device}")
    if args.device == "cpu":
        print("[road_combined] ⚠ CPU 학습입니다. 진행 상황은 아래 run 폴더에 계속 쌓입니다.")

    from ultralytics import YOLO

    t0 = time.time()
    model = YOLO(str(BASE_WEIGHTS))
    model.train(
        data=str(DATA_YAML),
        epochs=epochs,
        imgsz=imgsz,
        batch=args.batch,
        fraction=fraction,
        device=args.device,
        workers=4,
        project=str(RUNS),
        name=name,
        exist_ok=True,
        patience=0,          # 조기 종료를 끄고 지정한 에폭을 다 돈다
        verbose=True,
    )
    best = RUNS / name / "weights" / "best.pt"
    print(f"\n[road_combined] 학습 완료 — {(time.time() - t0) / 60:.1f}분")
    print(f"[road_combined] 체크포인트: {best}")


if __name__ == "__main__":
    main()
